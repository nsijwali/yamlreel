"""Timeline assembly and ffmpeg encoding."""
from __future__ import annotations

import contextlib
import math
import re
import shutil  # noqa: F401  (tests patch render.shutil.which; ffmpeg discovery lives in ffmpegtools)
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from . import contactpages
from . import music as music_mod
from . import proclife, profilereport
from . import shorts as shorts_mod
from . import subtitles as subs_mod
from . import voice as voice_mod
from .badge import Badge, make_badge, step_plan
from .browserlife import closing_browsers
from .config import HARD_LIMITS
from .ffmpegtools import AUDIO_FORMATS, ffmpeg_exe  # noqa: F401  (both re-exported: render.ffmpeg_exe is a test seam)
from .publish import PublishError, Staging
from .renderplan import RenderError, RenderPlan, only_label, plan_durations, scene_windows  # noqa: F401  (re-exported)
from . import renderplan
from .safety import UnsafeInput, resolve_local
from .scenes import Ctx, Scene, build_scene, make_caption
from .shots import CaptureError
from .specs import FORMATS, SpecError, check_frame_budget
from .textkit import blit, ease, ease_out, make_block
from .theme import font
from .watermark_fit import WatermarkSizeError, fit_watermark

FADE_IN = 0.25  # the default transition length, seconds (config.DEFAULTS["transition"])
SLIDE_EASING = "in_out"
ZOOM_FROM = 1.08  # a zoom transition starts with the new frame this much larger
CONTACT_DEFAULT_FRACS = (0.35, 0.92)
MAX_CONTACT_FRAMES = 8
CONTACT_COLS = 4  # tiles across a default sheet
YT_TITLE_MAX = 100
YT_DESC_MAX = 5000
SHORTS_MAX_SECONDS = shorts_mod.SHORTS_MAX_SECONDS  # the one rule lives in shorts.py
FRAMES_AHEAD = 3  # scenes are told this many frames early that a frame is coming (Scene.prefetch)
FRAME_QUEUE = proclife.FRAME_QUEUE  # frames waiting for ffmpeg at most (about 25 MB at 1080x1920)
MAX_WATERMARK_PIXELS = 16_000_000  # a watermark image larger than this is refused


@dataclass
class Runtime:
    """What this run is allowed to read and reach (never taken from the spec)."""
    roots: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium: str | None = None
    voice: "voice_mod.VoiceServer | None" = None  # where narration is synthesised (trusted setting)
    deadlines: proclife.Deadlines = field(default_factory=proclife.Deadlines)  # `timeouts:` (trusted setting)

    @classmethod
    def for_spec(cls, spec: dict[str, Any]) -> "Runtime":
        return cls(roots=[Path(spec.get("_dir", "."))])


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "video"


class Timeline:
    def __init__(self, spec: dict[str, Any], fmt: str, workdir: Path, scale: float = 1.0,
                 runtime: Runtime | None = None, durations: list[float] | None = None,
                 words: dict[int, subs_mod.SceneWords] | None = None):
        W, H = FORMATS[fmt]
        W, H = int(W * scale), int(H * scale)
        self.fmt = fmt
        self.spec = spec
        rt = runtime or Runtime.for_spec(spec)
        self.rt = rt
        self.ctx = Ctx(W, H, spec["_theme_obj"], workdir, Path(spec.get("_dir", ".")), roots=list(rt.roots),
                       allow_private_hosts=rt.allow_private_hosts, chromium=rt.chromium,
                       browser_timeout=rt.deadlines.browser_frame)
        scenes_spec = [dict(s) for s in spec["scenes"]]
        for s, seconds in zip(scenes_spec, durations if durations is not None else plan_durations(spec)):
            s["seconds"] = seconds
        self.scenes: list[Scene] = []
        for i, s in enumerate(scenes_spec):
            try:
                self.scenes.append(build_scene(s, self.ctx))
            except CaptureError as e:
                raise CaptureError(f"scenes[{i}].source: {e}") from e
        self.starts: list[float] = []
        t = 0.0
        for sc in self.scenes:
            self.starts.append(t)
            t += sc.duration
        self.total = t
        default_tr = spec.get("transition") or {"type": "fade", "seconds": FADE_IN}
        self._transitions = [dict(s.get("transition") or default_tr) for s in scenes_spec]
        for i, (tr, sc) in enumerate(zip(self._transitions, self.scenes)):
            if i > 0 and tr["type"] != "cut" and tr["seconds"] > sc.duration:
                self.ctx.warn(f"scenes[{i}]: the {tr['type']} transition ({tr['seconds']:g}s) is longer than the "
                              f"scene ({sc.duration:.2f}s); shortened to fit")
                tr["seconds"] = sc.duration
        self._caps = {i: make_caption(self.ctx, sc.caption) for i, sc in enumerate(self.scenes) if sc.caption}
        for i, sc in enumerate(self.scenes):
            box = None
            if i in self._caps:
                cw, ch = self._caps[i][0].size
                x0, y0 = (self.ctx.W - cw) / 2, self.ctx.caption_y - ch / 2
                box = (x0, y0, x0 + cw, y0 + ch)
            for msg in sc.layout_warnings(box):
                self.ctx.warn(f"scenes[{i}]: {msg}")
        self._subs: dict[int, subs_mod.SubtitleTrack] = {}  # scene index -> its subtitles
        self._cap_from: dict[int, float] = {}  # a subtitled scene's caption waits until its speech has ended
        for i, sw in (words or {}).items():
            cfg = subs_mod.scene_settings(spec, scenes_spec[i])
            if cfg is None:
                continue
            cues = subs_mod.build_cues(list(sw.words), int(cfg["words"]), cfg["keywords"], bool(cfg["numbers"]),
                                       limit=self.scenes[i].duration)
            self._subs[i] = subs_mod.SubtitleTrack(self.ctx, cues, cfg)
            if i in self._caps:
                self._cap_from[i] = subs_mod.caption_start(sw, cues)
        if self._cap_from:
            ids = sorted(self._cap_from)
            self.ctx.warn(f"scene{'s' if len(ids) != 1 else ''} {', '.join(map(str, ids[:8]))}{', ...' if len(ids) > 8 else ''}: "
                          "the caption appears after the narration ends, so it does not cover the subtitles")
        self.cut_in: set[int] = set()  # scenes that begin with a hard cut whatever their transition (`render --only`)
        self.draw_seconds: dict[str, list[float]] = {}  # scene type -> [seconds spent drawing, frames], for --profile
        self._current = -1  # the scene frame_at last drew; scenes far from it give their cached layers back
        self._last: dict[int, Image.Image] = {}  # at most one entry: the last frame of the scene before this one
        self._wm = self._make_watermark(spec.get("watermark"))
        self._badges: dict[int, Badge] = {}  # scene index -> its STEP n/N badge (one image per distinct step)
        made: dict[tuple[int, int, str], Badge] = {}
        for i, plan in enumerate(step_plan([s.get("step") for s in scenes_spec])):
            if plan is not None:
                if plan not in made:   # one badge image per distinct step, built only once
                    made[plan] = make_badge(self.ctx, *plan)
                self._badges[i] = made[plan]
        if self._badges and self._wm:
            rgb, _, (wx, wy) = self._wm
            bx0, by0, bx1, by1 = next(iter(self._badges.values())).box
            if wx < bx1 and wx + rgb.width > bx0 and wy < by1 and wy + rgb.height > by0:
                self.ctx.warn("the watermark overlaps the step badge at the top; move it with watermark.position "
                              "or make it smaller")
        for i, sc in enumerate(self.scenes):
            if sc.kicker is not None and sc.kicker.shrunk:
                self.ctx.warn(f"scenes[{i}]: kicker shrunk to {sc.kicker.size}px to fit; shorten it")
            badge = self._badges.get(i)
            if sc.kicker_box is not None and badge is not None:
                kx0, ky0, kx1, ky1 = sc.kicker_box
                bx0, by0, bx1, by1 = badge.box
                if kx0 < bx1 and kx1 > bx0 and ky0 < by1 and ky1 > by0:
                    self.ctx.warn(f"scenes[{i}]: the kicker overlaps the step badge; drop one of them")

    def _make_watermark(self, wm: dict[str, Any] | None) -> tuple[Image.Image, Image.Image, tuple[int, int]] | None:
        """Pre-render the watermark once: (rgb, mask, top-left position)."""
        if not wm:
            return None
        c = self.ctx
        u = c.u
        if wm.get("image"):
            try:
                p = resolve_local(wm["image"], c.base_dir, c.roots)
            except UnsafeInput as e:
                raise RenderError(f"watermark image: {e}") from e
            if not p.is_file():
                raise RenderError(f"watermark image not found: {p}")
            try:
                with Image.open(p) as im:
                    if im.width * im.height > MAX_WATERMARK_PIXELS:
                        raise RenderError(f"watermark image is {im.width}x{im.height}; the limit is "
                                          f"{MAX_WATERMARK_PIXELS} pixels (MAX_WATERMARK_PIXELS)")
                    size = fit_watermark(im.width, im.height, int(wm.get("size", 56) * u), c.W)  # before any decode
                    logo = im.convert("RGBA")
            except WatermarkSizeError as e:
                raise RenderError(str(e)) from e
            except OSError as e:
                raise RenderError(f"cannot read watermark image: {e}") from e
            logo = logo.resize(size, Image.LANCZOS)
            rgb, mask = logo.convert("RGB"), logo.getchannel("A")
        else:
            blk = make_block(str(wm["text"]), c.body, wm.get("size", 30) * u * (1.1 if c.vertical else 1.0),
                             c.W * 0.5, tracking=1.5 * u)
            rgb = Image.new("RGB", (blk.width, blk.mask.height), c.theme.fg)
            mask = blk.mask
        mask = mask.point(lambda v: int(v * wm["opacity"]))
        pos = wm.get("position") or ("top-left" if c.vertical else "bottom-right")
        # vertical: the Shorts UI covers the right edge and the bottom, so default to the top-left
        inset = int(60 * u)
        x = inset if pos.endswith("left") else c.W - inset - rgb.width
        y = inset if pos.startswith("top") else c.H - int(48 * u) - rgb.height
        return rgb, mask, (x, y)

    @property
    def warnings(self) -> list[str]:
        return self.ctx.warnings

    def locate(self, t: float) -> tuple[int, float]:
        t = min(max(t, 0.0), self.total - 1e-6)
        for i in range(len(self.scenes) - 1, -1, -1):
            if t >= self.starts[i]:
                return i, t - self.starts[i]
        return 0, 0.0

    def prefetch(self, t: float) -> None:
        """Tell the scene at `t` that `frame_at(t)` comes soon (see Scene.prefetch)."""
        i, lt = self.locate(t)
        self.scenes[i].prefetch(lt)

    def _last_frame(self, i: int) -> Image.Image:
        if i not in self._last:
            sc = self.scenes[i]
            self._last.clear()  # only the previous scene's frame is ever needed (about 6 MB in vertical)
            self._last[i] = self._scene_frame(i, max(0.0, sc.duration - 1 / 30))
        return self._last[i]

    def _scene_frame(self, i: int, lt: float) -> Image.Image:
        sc = self.scenes[i]
        t0 = time.perf_counter()
        img = sc.frame(lt)
        spent = self.draw_seconds.setdefault(str(sc.spec.get("type")), [0.0, 0])
        spent[0] += time.perf_counter() - t0
        spent[1] += 1
        if i in self._caps:
            pill, mask = self._caps[i]
            a = ease_out((lt - self._cap_from.get(i, 0.35)) / 0.25)
            if a > 0:
                m = mask if a >= 1 else mask.point(lambda v: int(v * a))
                img.paste(pill, (int((self.ctx.W - pill.width) / 2), int(self.ctx.caption_y - pill.height / 2)), m)
        track = self._subs.get(i)
        if track is not None:
            track.draw(img, lt)
        badge = self._badges.get(i)
        if badge is not None:
            badge.paste(img)
        return img

    def subtitle_cues(self) -> list[subs_mod.Cue]:
        """Every subtitle cue of the video with absolute times, in order, none overlapping the next."""
        out: list[subs_mod.Cue] = []
        for i in sorted(self._subs):
            off = self.starts[i]
            for c in self._subs[i].cues:
                out.append(subs_mod.Cue(c.start + off, c.end + off, c.words, c.keywords))
        return out

    def frame_at(self, t: float) -> Image.Image:
        i, lt = self.locate(t)
        if i != self._current:  # only scene i and the one before it (for the transition) can be drawn next
            self._current = i
            for k, other in enumerate(self.scenes):
                if k != i and k != i - 1:
                    other.release()
        img = self._scene_frame(i, lt)
        tr = self._transitions[i]
        if i > 0 and i not in self.cut_in and tr["type"] != "cut" and lt < tr["seconds"]:
            img = self._transition(tr["type"], self._last_frame(i - 1), img, lt / tr["seconds"])
        if self._wm:  # constant on every frame, including transitions and the poster
            rgb, mask, xy = self._wm
            img.paste(rgb, xy, mask)
        return img

    @staticmethod
    def _transition(kind: str, old: Image.Image, new: Image.Image, a: float) -> Image.Image:
        """The frame `a` (0..1) of the way through the transition from `old` into `new`."""
        if kind == "slide":  # push: the old frame leaves to the left, the new one enters from the right
            w = new.width
            shift = int(round(ease(SLIDE_EASING, a) * w))
            out = Image.new("RGB", new.size)
            out.paste(old, (-shift, 0))
            out.paste(new, (w - shift, 0))
            return out
        if kind == "zoom":  # the new frame settles from ZOOM_FROM to 1.0 while it fades in
            z = ZOOM_FROM - (ZOOM_FROM - 1.0) * ease("out", a)
            w, h = new.size
            cw, ch = w / z, h / z
            box = ((w - cw) / 2, (h - ch) / 2, (w + cw) / 2, (h + ch) / 2)
            return Image.blend(old, new.resize((w, h), Image.BILINEAR, box=box), a)
        return Image.blend(old, new, a)  # fade

    def poster_time(self) -> float:
        p = self.spec.get("poster", {}) or {}
        idx = int(p.get("scene", 0))
        if not 0 <= idx < len(self.scenes):
            raise RenderError(f"poster.scene {idx} out of range")
        sc = self.scenes[idx]
        at = float(p.get("at", sc.duration * 0.92))
        return self.starts[idx] + min(at, sc.duration - 0.05)


def contact_fracs(frames: int | None) -> tuple[float, ...]:
    """Where in each scene the contact sheet looks: the two default stills, or `frames` stills spread from just
    after the scene starts to just before it ends. Two stills are the default ones, so N=2 matches no flag."""
    if frames is None or frames == 2:
        return CONTACT_DEFAULT_FRACS
    if not 1 <= frames <= MAX_CONTACT_FRAMES:
        raise RenderError(f"contact frames must be between 1 and {MAX_CONTACT_FRAMES}")
    if frames == 1:
        return (0.92,)
    return tuple(0.08 + 0.84 * k / (frames - 1) for k in range(frames))


def contact_sheet(tl: Timeline, path: Path, cols: int | None = None, tile_w: int | None = None,
                  fracs: tuple[float, ...] = CONTACT_DEFAULT_FRACS, scenes: list[int] | None = None) -> Path:
    """Stills of every scene (or only the scene numbers in `scenes`) at the `fracs` points, each tile pasted into a
    sheet of the final size as it is rendered. By default each scene has one row of len(fracs) tiles; the default
    two stills are four tiles across, two scenes per row. Labels carry the full-video scene number and time."""
    if cols is None:
        cols = CONTACT_COLS if fracs == CONTACT_DEFAULT_FRACS else len(fracs)
    if tile_w is None:
        tile_w = 480 if cols <= 4 else 320
    tile_h = int(tile_w * tl.ctx.H / tl.ctx.W)
    chosen = list(range(len(tl.scenes))) if scenes is None else scenes
    label_h = 24
    tiles = [(i, frac) for i in chosen for frac in fracs]
    # A sheet larger than contactpages.MAX_SHEET_PIXELS is split into pages of whole rows (path, path-p2, ...), each
    # drawn and saved before the next, so no single huge image is ever allocated.
    per_page = cols * contactpages.rows_per_page(cols * tile_w, tile_h + label_h, contactpages.MAX_SHEET_PIXELS)
    f = font("sans", 15)
    for k, first in enumerate(range(0, max(1, len(tiles)), per_page)):
        page = tiles[first:first + per_page]
        sheet = Image.new("RGB", (cols * tile_w, ((len(page) + cols - 1) // cols) * (tile_h + label_h)), (16, 16, 16))
        d = ImageDraw.Draw(sheet)
        for n, (i, frac) in enumerate(page):
            sc = tl.scenes[i]
            t = tl.starts[i] + sc.duration * frac
            x, y = (n % cols) * tile_w, (n // cols) * (tile_h + label_h)
            sheet.paste(tl.frame_at(t).resize((tile_w, tile_h)), (x, y + label_h))
            d.text((x + 6, y + 4), f"{i}:{sc.spec['type']} {t:.1f}s", font=f, fill=(220, 220, 220))
        sheet.save(contactpages.page_path(path, k))
        del sheet, d
    return path


def _check_only(only: list[int], n_scenes: int) -> None:
    if not only or only != sorted(set(only)):
        raise RenderError("only: scene numbers must be a non-empty, sorted list without duplicates")
    for i in only:
        if not 0 <= i < n_scenes:
            raise RenderError(f"only: scene {i} does not exist; scenes are numbered 0 to {n_scenes - 1}")


def _slice_cues(cues: list[subs_mod.Cue], frame_runs: list[tuple[int, int]], fps: int) -> list[subs_mod.Cue]:
    """The cues inside each window, moved to slice time (windows play back to back) and clipped to the window."""
    out: list[subs_mod.Cue] = []
    shift = 0.0
    for n0, n1 in frame_runs:
        a, b = n0 / fps, n1 / fps
        for c in cues:
            if c.end > a and c.start < b:
                out.append(subs_mod.Cue(max(c.start, a) - a + shift, min(c.end, b) - a + shift, c.words, c.keywords))
        shift += b - a
    return out


def _cut_audio(label: str, spans: list[tuple[float, float]]) -> str:
    """Filter-graph text that pads the audio on `label` and plays only the (start, end) spans, one after another."""
    k = len(spans)
    outs = "".join(f"[s{n}]" for n in range(k))
    parts = [f"[{label}]apad,asplit={k}{outs}"]
    parts += [f"[s{n}]atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS[t{n}]" for n, (a, b) in enumerate(spans)]
    parts.append("".join(f"[t{n}]" for n in range(k)) + f"concat=n={k}:v=0:a=1[out]")
    return ";".join(parts)


def energy_sections(spec: dict[str, Any], starts: list[float], durations: list[float]) -> list[tuple[float, float, int]]:
    """[(start, end, level)] per scene, from each scene's `energy` (default mid)."""
    return [(s, s + d, music_mod.ENERGY_LEVELS[sc.get("energy", "mid")])
            for sc, s, d in zip(spec["scenes"], starts, durations)]


def scene_cues(spec: dict[str, Any], starts: list[float], fps: int, durations: list[float] | None = None,
               warn: Any = None) -> list[Any]:
    """The `sfx` cues of every scene on the video's clock, each on a frame boundary (scene start + `at`).

    With `durations`, a cue whose `at` is at or past its scene's (planned) length is dropped, and `warn(message)` is
    called for it, so a cue never sounds inside a later scene or after the video has ended."""
    from .sfx import Cue
    out = []
    for i, sc in enumerate(spec["scenes"]):
        for n, c in enumerate(sc.get("sfx") or []):
            at = float(c["at"])
            if durations is not None and at >= durations[i]:
                if warn is not None:
                    warn(f"scenes[{i}].sfx[{n}]: at {at:g}s is not inside the {durations[i]:.2f}s scene; the cue is dropped")
                continue
            t = round((starts[i] + at) * fps) / fps
            out.append(Cue(t, c["sound"], float(c.get("gain", 1.0)), c.get("seconds"), float(c.get("pitch", 0.0))))
    return out


def speech_regions(clips: dict[int, np.ndarray], starts: list[float]) -> list[tuple[float, float]]:
    """Where each narration clip is heard, as placed by `voice_mod.build_track`."""
    return [(starts[i] + voice_mod.NARRATION_LEAD, starts[i] + voice_mod.NARRATION_LEAD + len(c) / voice_mod.SAMPLE_RATE)
            for i, c in clips.items()]


def _decode_music(ffmpeg: str, src: Path, dst: Path) -> Path:
    """A music file as mono 44.1 kHz WAV, with the same guard as the final mix: real audio containers only."""
    cmd = [ffmpeg, "-loglevel", "error", "-y", "-protocol_whitelist", "file", "-format_whitelist", AUDIO_FORMATS,
           "-i", str(src), "-vn", "-ac", "1", "-ar", str(music_mod.SR), "-c:a", "pcm_s16le", str(dst)]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RenderError(f"music file: cannot decode {src.name}: {e}") from e
    if done.returncode != 0:
        raise RenderError(f"music file: cannot decode {src.name}: {done.stderr.decode(errors='replace')[-300:]}")
    return dst


def _prepare_audio(spec: dict[str, Any], total: float, workdir: Path, override: str | None,
                   rt: Runtime, sections: list[tuple[float, float, int]] | None = None,
                   duck: tuple[list[tuple[float, float]], float] | None = None,
                   ffmpeg: str | None = None) -> Path | None:
    """The music as a file. `duck` is (speech regions, music level): the music is lowered under speech here,
    in numpy, so generated music is made already ducked and a music file is decoded first."""
    m = dict(spec["music"])
    style = override or m["style"]
    if style == "none":
        return None
    if style in music_mod.STYLE_DEFAULTS:
        parts = music_mod.blocks(style, total, bpm=float(m["bpm"]), root=str(m["root"]), scale=str(m["scale"]),
                                 seed=int(m.get("seed", 7)), sections=sections)
        if duck:
            parts = music_mod.apply_duck(parts, *duck)
        return music_mod.write_wav_stream(workdir / "music.wav", parts)
    try:
        p = resolve_local(style, Path(spec.get("_dir", ".")), rt.roots)
    except UnsafeInput as e:
        raise RenderError(f"music file: {e}") from e
    if not p.is_file():
        raise RenderError(f"music file not found: {p}")
    if duck and duck[0] and ffmpeg:
        decoded = _decode_music(ffmpeg, p, workdir / "music-src.wav")
        return music_mod.write_wav_stream(workdir / "music.wav", music_mod.apply_duck(music_mod.read_wav_blocks(decoded), *duck))
    return p


def share_copy(spec: dict[str, Any], total: float, fmt_list: list[str]) -> tuple[str, list[str]]:
    meta = spec.get("meta", {})
    warns: list[str] = []
    title = str(meta.get("title", "")).strip()
    if not title:
        warns.append("meta.title is empty")
    if len(title) > YT_TITLE_MAX:
        warns.append(f"title is {len(title)} chars; YouTube allows {YT_TITLE_MAX}")
    tags = [str(t).lstrip("#") for t in meta.get("tags", [])]
    short_forms = [FORMATS[f] for f in fmt_list if f in FORMATS and shorts_mod.short_form(*FORMATS[f])]
    if any(shorts_mod.shorts_eligible(w, h, total) for w, h in short_forms) and "Shorts" not in tags:
        tags = ["Shorts", *tags]
    lines = [str(meta.get("description", "")).strip()]
    question = next((str(sc["question"]).strip() for sc in spec.get("scenes", []) if sc.get("type") == "cta"
                     and str(sc.get("question", "")).strip()), "")
    if question and question not in lines[0]:
        lines.append(question)  # the end card's question, so the description invites the same comment
    for link in meta.get("links", []):
        lines.append(str(link))
    for credit in meta.get("credits", []):
        lines.append(str(credit))
    lines.append(" ".join("#" + t for t in tags))
    desc = "\n".join(l for l in lines if l)
    if len(desc) > YT_DESC_MAX:
        warns.append(f"description is {len(desc)} chars; YouTube allows {YT_DESC_MAX}")
    if "vertical" in fmt_list and total > SHORTS_MAX_SECONDS:
        warns.append(f"vertical video is {total:.0f}s; YouTube Shorts allow up to {SHORTS_MAX_SECONDS:g}s, "
                     "beyond that it is a normal video")
    return f"Title: {title}\n\nDescription:\n{desc}\n", warns


def make_plan(spec: dict[str, Any], fps: int, out_dir: Path, runtime: Runtime | None = None, n_formats: int = 1,
              only: list[int] | None = None, music_override: str | None = None) -> RenderPlan:
    """The format-independent part of a render (narration, timing, limits), to pass to render() for each format.
    Close it (or use it in a `with`) when every format is done."""
    rt = runtime or Runtime.for_spec(spec)
    if not 1 <= fps <= 60:
        raise RenderError("fps must be between 1 and 60")
    if only is not None:
        _check_only(only, len(spec["scenes"]))
    return renderplan.build(spec, fps=fps, n_formats=n_formats, only=only, music_override=music_override,
                            voice=rt.voice, roots=rt.roots, cache=out_dir / ".cache",
                            durations_fn=plan_durations)   # render's global, so replacing it still takes effect


def _audio_tracks(plan: RenderPlan, tl: Timeline, rt: Runtime, ffmpeg: str,
                  durations: list[float]) -> renderplan.AudioTracks:
    """The encoder's audio inputs for this plan, made the first time a format asks and reused by the others (their
    scene starts are the same; a timeline with other starts gets its own)."""
    key = (tuple(tl.starts), tl.total)
    if plan.audio is not None and plan.audio.key == key:
        return plan.audio
    spec, clips, total, work, fps = plan.spec, plan.clips, tl.total, plan.workdir, plan.fps
    warnings: list[str] = []
    narration = voice_mod.write_wav(work / "narration.wav", voice_mod.build_track(clips, tl.starts, total)) if clips else None
    duck = None
    if clips and (spec.get("voiceover") or {}).get("duck"):
        duck = (speech_regions(clips, tl.starts), float(spec["voiceover"].get("music_level", 0.35)))
    audio = _prepare_audio(spec, total, work, plan.music_override, rt, energy_sections(spec, tl.starts, durations),
                           duck, ffmpeg)
    mix_numbers = None
    cues = scene_cues(spec, tl.starts, fps, durations, warnings.append) if spec.get("mix") is not None else []
    if spec.get("mix") is not None and (audio or narration or cues):
        # the `mix:` path: narration, music and scene effects become one mastered track (see mixdown.py)
        from . import mixdown
        if audio is not None and audio.parent != work:
            # a music file the spec named (no ducking made a copy of it): decode it to the mix's mono 44.1 kHz WAV
            audio = _decode_music(ffmpeg, audio, work / "music-src.wav")
        level = 1.0 if duck or not narration else float(spec["voiceover"].get("music_level", 0.35))
        try:
            audio, mix_numbers = mixdown.master(work, ffmpeg, total, narration, audio, cues,
                                                spec["mix"], float(spec["music"].get("volume", 0.9)), level,
                                                int(spec["music"].get("seed", 0) or 0), timeout=rt.deadlines.subprocess)
        except mixdown.MixError as e:
            raise RenderError(f"mix: {e}") from e
        narration = None
    plan.audio = renderplan.AudioTracks(key, audio, narration, duck, mix_numbers, warnings)
    return plan.audio


def render(spec: dict[str, Any], fmt: str, out_dir: Path, fps: int = 30, scale: float = 1.0,
           music_override: str | None = None, contact: bool = True,
           runtime: Runtime | None = None, n_formats: int = 1,
           contact_frames: int | None = None, only: list[int] | None = None,
           plan: RenderPlan | None = None) -> dict[str, Any]:
    """Render one format. `n_formats` is how many formats this run renders in all, for the frame budget.
    `contact_frames` sets how many stills per scene the contact sheet has (default: two). `only` (sorted, unique
    scene numbers) renders just those scenes, cut out of the full video: same plan, partial frames; the outputs get
    `-scenes-<label>` names and no poster is written. `plan` (from `make_plan`, made for the same fps, `only` and
    music override) lets several formats share one narration pass and one set of audio tracks; without it render
    makes its own and removes it at the end."""
    clock = time.perf_counter
    t_start = clock()
    ffmpeg = ffmpeg_exe()
    if ffmpeg is None:
        raise RenderError("ffmpeg not found. Install it (macOS: brew install ffmpeg; Debian/Ubuntu: sudo apt install "
                          "ffmpeg) or run: pip install imageio-ffmpeg")
    if not 1 <= fps <= 60:
        raise RenderError("fps must be between 1 and 60")
    if only is not None:
        _check_only(only, len(spec["scenes"]))
    rt = runtime or Runtime.for_spec(spec)
    out = out_dir / fmt
    out.mkdir(parents=True, exist_ok=True)
    cache = out_dir / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    # Outputs are written into a staging folder and replace the previous ones only once everything succeeded.
    # The browser that draws line-art frames is closed when this render ends, however it ends (closing_browsers).
    with contextlib.ExitStack() as stack:
        if plan is None:
            plan = stack.enter_context(make_plan(spec, fps, out_dir, rt, n_formats, only, music_override))
        elif not plan.matches(fps, only, music_override):
            raise RenderError("render: the plan was made for another fps, --only or music override")
        t_voice = clock()   # a plan made elsewhere did its narration and timing before this render started
        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="yamlreel-")))
        stage = stack.enter_context(Staging(out))
        stack.enter_context(closing_browsers())
        spec, clips, words, durations, windows, spans = (plan.spec, plan.clips, plan.words, plan.durations,
                                                         plan.windows, plan.spans)
        tl = Timeline(spec, fmt, cache, scale, rt, durations=durations, words=words)
        tl.ctx.warnings.extend(w for w in plan.sub_warnings if w not in tl.ctx.warnings)
        total = tl.total
        tag = plan.tag
        tl.cut_in = {a for a, _ in windows}
        frame_runs = renderplan.frame_runs(plan, total)
        n_frames = sum(n1 - n0 for n0, n1 in frame_runs)
        out_seconds = n_frames / fps if only is not None else total
        srt = None
        srt_name = f"subtitles{tag}.srt"
        if words and (spec.get("subtitles") or {}).get("srt"):
            cues = tl.subtitle_cues()
            if only is not None:
                cues = _slice_cues(cues, frame_runs, fps)
            if cues or only is None:
                srt = stage.path(srt_name)
                srt.write_text(subs_mod.srt_text(cues, bool(spec["subtitles"].get("uppercase"))), encoding="utf-8")
        if srt is None:
            stage.remove(srt_name)   # a file from an earlier run would be taken for this one's (deleted on publish)
        tracks = _audio_tracks(plan, tl, rt, ffmpeg, durations)   # made once per plan, shared by its formats
        tl.ctx.warnings.extend(w for w in tracks.warnings if w not in tl.ctx.warnings)
        audio, narration, duck, mix_numbers = tracks.audio, tracks.narration, tracks.duck, tracks.mix
        poster = tl.frame_at(tl.poster_time())   # also checks poster.scene
        if only is None:
            poster.convert("RGB").save(stage.path("poster.jpg"), quality=92)

        W, H = tl.ctx.W, tl.ctx.H
        cmd = [ffmpeg, "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
        # Audio inputs, in order: music (optional), narration (optional). Input 0 is the video.
        inputs: list[Path] = [a for a in (audio, narration) if a]
        for a in inputs:
            # ffmpeg sniffs the format from content, so a file named .mp3 could really be a concat/playlist
            # script that pulls in other files. Only accept real audio containers, local files only.
            cmd += ["-protocol_whitelist", "file", "-format_whitelist", AUDIO_FORMATS, "-i", str(a)]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
        if inputs and mix_numbers is not None:    # already mixed and mastered: just cut it to length
            whole = f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS"
            cut = _cut_audio("a", [(n0 / fps, n1 / fps) for n0, n1 in frame_runs]) if only is not None else None
            if cut:
                cmd += ["-filter_complex", f"[1:a]{whole}[a];{cut}", "-map", "0:v", "-map", "[out]"]
            else:
                cmd += ["-af", whole]
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        elif inputs:
            vol = float(spec["music"].get("volume", 0.9))
            fade_out = max(0.0, total - 2.0)
            music_f = (f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={{vol}},"
                       f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.3f}:d=2.0")
            cut = _cut_audio("a", [(n0 / fps, n1 / fps) for n0, n1 in frame_runs]) if only is not None else None
            if audio and narration:
                level = float(spec["voiceover"].get("music_level", 0.35))  # music sits under the voice
                graph = (f"[1:a]{music_f.format(vol=vol if duck else vol * level)}[m];"
                         f"[2:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS[n];"
                         "[m][n]amix=inputs=2:duration=longest:normalize=0[a]")
                if cut:
                    graph += ";" + cut
                cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[out]" if cut else "[a]"]
            elif audio:
                if cut:
                    cmd += ["-filter_complex", f"[1:a]{music_f.format(vol=vol)}[a];{cut}", "-map", "0:v", "-map", "[out]"]
                else:
                    cmd += ["-af", music_f.format(vol=vol)]
            else:
                # Narration only: never fade the speech; just a tiny fade after it to avoid a click at the end.
                speech_end = max((tl.starts[i] + voice_mod.NARRATION_LEAD + len(c) / voice_mod.SAMPLE_RATE
                                  for i, c in clips.items()), default=0.0)
                end_fade = max(speech_end, total - 0.05)  # the fade starts after the last word, never inside it
                fade = f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={end_fade:.3f}:d=0.05"
                if cut:
                    cmd += ["-filter_complex", f"[1:a]{fade}[a];{cut}", "-map", "0:v", "-map", "[out]"]
                else:
                    cmd += ["-af", fade]
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        video = stage.final(f"video{tag}.mp4")
        cmd += ["-movflags", "+faststart", "-t", f"{out_seconds:.3f}", str(stage.path(video.name))]
        errlog = open(work / "ffmpeg.log", "wb")
        try:
            # Frames go to ffmpeg from a second thread, so the next frame is drawn while this one is being read by the
            # encoder. FramePipe owns the encoder, that thread and the short queue between them: every wait has a
            # deadline, and an error or an interruption stops the encoder before anything waits for it.
            pipe = proclife.FramePipe(cmd, errlog, rt.deadlines, out_seconds)
        except OSError as e:
            errlog.close()
            raise RenderError(f"cannot run ffmpeg at {ffmpeg}: {e}") from e
        t_frames = clock()
        t_draw = t_pipe = 0.0
        try:
            with pipe:
                for (n0, n1), (a, _) in zip(frame_runs, spans or [(0.0, total)]):
                    for n in range(n0, n1):
                        if pipe.broken:
                            break
                        t0 = clock()
                        for m in (range(n + 1, n + FRAMES_AHEAD + 1) if n == n0 else (n + FRAMES_AHEAD,)):
                            if m < n1:
                                tl.prefetch(max(m / fps, a))
                        # the poster doubles as frame 0 (thumbnail); a slice has no poster
                        img = poster if n == 0 and only is None else tl.frame_at(max(n / fps, a))
                        t1 = clock()
                        # The bytes are copied out here, on the drawing thread: a scene may reuse its canvas for the
                        # next frame. Converting an RGB frame to RGB only copies it: skip that.
                        pipe.put((img if img.mode == "RGB" else img.convert("RGB")).tobytes())
                        t_draw += t1 - t0
                        t_pipe += clock() - t1
                rc = pipe.finish()
        except proclife.ProcessTimeout as e:
            raise RenderError(str(e)) from e
        finally:
            errlog.close()
        t_encoded = clock()
        if rc != 0:
            raise RenderError("ffmpeg failed:\n" + (work / "ffmpeg.log").read_text(errors="replace")[-1500:])
        sheet = None
        if contact:
            fracs = contact_fracs(contact_frames)
            sheet_name = f"contact{tag}.png"
            sheet = stage.final(contact_sheet(tl, stage.path(sheet_name), fracs=fracs, scenes=only).name)
            made = contactpages.extra_pages(stage.dir, sheet_name)
            for extra in made:
                stage.path(extra.name)        # published with the first page
            for old in contactpages.extra_pages(out, sheet_name)[len(made):]:
                stage.remove(old.name)        # pages an earlier, longer sheet had
        try:
            stage.publish(last=video.name)   # the video last: a new video always comes with its new sidecars
        except PublishError as e:
            raise RenderError(str(e)) from e
        if srt is not None:
            srt = stage.final(srt_name)
    t_end = clock()
    profile = {"frames": n_frames,
               "stages": [("narration and timing", t_voice - t_start), ("layout, music and poster", t_frames - t_voice),
                          ("draw frames", t_draw), ("send frames to ffmpeg", t_pipe),
                          ("finish encoding", t_encoded - t_frames - t_draw - t_pipe),
                          ("contact sheet and cleanup", t_end - t_encoded)],
               "total": t_end - t_start,
               "scene_types": {k: tuple(v) for k, v in tl.draw_seconds.items()},
               **profilereport.measure(tl.ctx.media.peak, plan.seconds_spent)}
    return {"profile": profile, "video": video, "poster": None if only is not None else out / "poster.jpg", "contact": sheet, "srt": srt,
            "seconds": out_seconds, "full_seconds": total, "windows": [(a, b, s0, s1) for (a, b), (s0, s1) in zip(windows, spans)],
            "warnings": tl.warnings, "size": (W, H), "mix": mix_numbers}
