"""Timeline assembly and ffmpeg encoding."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from . import music as music_mod
from . import subtitles as subs_mod
from . import voice as voice_mod
from .badge import Badge, make_badge, step_plan
from .config import HARD_LIMITS
from .safety import UnsafeInput, resolve_local
from .scenes import Ctx, Scene, build_scene, make_caption
from .shots import CaptureError
from .specs import FORMATS, SpecError, check_frame_budget
from .textkit import blit, ease, ease_out, make_block
from .theme import font

FADE_IN = 0.25  # the default transition length, seconds (config.DEFAULTS["transition"])
SLIDE_EASING = "in_out"
ZOOM_FROM = 1.08  # a zoom transition starts with the new frame this much larger
CONTACT_DEFAULT_FRACS = (0.35, 0.92)
MAX_CONTACT_FRAMES = 8
CONTACT_COLS = 4  # tiles across a default sheet
YT_TITLE_MAX = 100
YT_DESC_MAX = 5000
SHORTS_MAX_SECONDS = 180
MAX_WATERMARK_PIXELS = 16_000_000  # a watermark image larger than this is refused


class RenderError(RuntimeError):
    pass


@dataclass
class Runtime:
    """What this run is allowed to read and reach (never taken from the spec)."""
    roots: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium: str | None = None
    voice: "voice_mod.VoiceServer | None" = None  # where narration is synthesised (trusted setting)

    @classmethod
    def for_spec(cls, spec: dict[str, Any]) -> "Runtime":
        return cls(roots=[Path(spec.get("_dir", "."))])


def plan_durations(spec: dict[str, Any], floors: dict[int, float] | None = None) -> list[float]:
    """The one place scene lengths are decided. `floors` maps a scene index to the shortest length it may have
    (narrated scenes: speech plus gap).

    Beat snap on: a scene is rounded to the nearest whole beat (at least one beat), and a scene that would then be
    shorter than its floor is rounded up to the next beat, so speech is never cut. Snap off: max(length, floor)."""
    floors = floors or {}
    music = spec["music"]
    beat = 60.0 / float(music["bpm"]) if music.get("snap") and music.get("bpm") else None
    out: list[float] = []
    for i, sc in enumerate(spec["scenes"]):
        s = float(sc["seconds"])
        floor = floors.get(i, 0.0)
        if beat is None:
            d = max(s, floor)
        else:
            d = max(beat, round(s / beat) * beat)
            if d < floor:
                d = math.ceil(floor / beat - 1e-9) * beat
        out.append(d)
    return out


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
                       allow_private_hosts=rt.allow_private_hosts, chromium=rt.chromium)
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
        self._current = -1  # the scene frame_at last drew; scenes far from it give their cached layers back
        self._last: dict[int, Image.Image] = {}  # at most one entry: the last frame of the scene before this one
        self._wm = self._make_watermark(spec.get("watermark"))
        self._badges: dict[int, Badge] = {}  # scene index -> its STEP n/N badge (one image per distinct step)
        made: dict[tuple[int, int, str], Badge] = {}
        for i, plan in enumerate(step_plan([s.get("step") for s in scenes_spec])):
            if plan is not None:
                self._badges[i] = made.setdefault(plan, make_badge(self.ctx, *plan))
        if self._badges and self._wm:
            rgb, _, (wx, wy) = self._wm
            bx0, by0, bx1, by1 = next(iter(self._badges.values())).box
            if wx < bx1 and wx + rgb.width > bx0 and wy < by1 and wy + rgb.height > by0:
                self.ctx.warn("the watermark overlaps the step badge at the top; move it with watermark.position "
                              "or make it smaller")

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
                    logo = im.convert("RGBA")
            except OSError as e:
                raise RenderError(f"cannot read watermark image: {e}") from e
            h = int(wm.get("size", 56) * u)
            logo = logo.resize((max(1, int(logo.width * h / logo.height)), h), Image.LANCZOS)
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

    def _last_frame(self, i: int) -> Image.Image:
        if i not in self._last:
            sc = self.scenes[i]
            self._last.clear()  # only the previous scene's frame is ever needed (about 6 MB in vertical)
            self._last[i] = self._scene_frame(i, max(0.0, sc.duration - 1 / 30))
        return self._last[i]

    def _scene_frame(self, i: int, lt: float) -> Image.Image:
        sc = self.scenes[i]
        img = sc.frame(lt)
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
        if i > 0 and tr["type"] != "cut" and lt < tr["seconds"]:
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
                  fracs: tuple[float, ...] = CONTACT_DEFAULT_FRACS) -> Path:
    """Stills of every scene at the `fracs` points, each tile pasted into a sheet of the final size as it is
    rendered. By default each scene has one row of len(fracs) tiles; the default two stills are four tiles across, two scenes
    per row."""
    if cols is None:
        cols = CONTACT_COLS if fracs == CONTACT_DEFAULT_FRACS else len(fracs)
    if tile_w is None:
        tile_w = 480 if cols <= 4 else 320
    tile_h = int(tile_w * tl.ctx.H / tl.ctx.W)
    n_tiles = len(tl.scenes) * len(fracs)
    label_h = 24
    sheet = Image.new("RGB", (cols * tile_w, ((n_tiles + cols - 1) // cols) * (tile_h + label_h)), (16, 16, 16))
    d = ImageDraw.Draw(sheet)
    f = font("sans", 15)
    n = 0
    for i, sc in enumerate(tl.scenes):
        for frac in fracs:
            t = tl.starts[i] + sc.duration * frac
            x, y = (n % cols) * tile_w, (n // cols) * (tile_h + label_h)
            sheet.paste(tl.frame_at(t).resize((tile_w, tile_h)), (x, y + label_h))
            d.text((x + 6, y + 4), f"{i}:{sc.spec['type']} {t:.1f}s", font=f, fill=(220, 220, 220))
            n += 1
    sheet.save(path)
    return path


def energy_sections(spec: dict[str, Any], starts: list[float], durations: list[float]) -> list[tuple[float, float, int]]:
    """[(start, end, level)] per scene, from each scene's `energy` (default mid)."""
    return [(s, s + d, music_mod.ENERGY_LEVELS[sc.get("energy", "mid")])
            for sc, s, d in zip(spec["scenes"], starts, durations)]


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
    if "vertical" in fmt_list and total <= 60 and "Shorts" not in tags:
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
        warns.append(f"vertical video is {total:.0f}s; YouTube Shorts allow up to {SHORTS_MAX_SECONDS}s, "
                     "beyond that it is a normal video")
    return f"Title: {title}\n\nDescription:\n{desc}\n", warns


AUDIO_FORMATS = "wav,mp3,ogg,flac,mov,aac,matroska,webm"


def ffmpeg_exe() -> str | None:
    """ffmpeg on PATH, else the static build from the optional imageio-ffmpeg package (`pip install imageio-ffmpeg`,
    or the `ffmpeg` extra), which works where no system package is available."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def render(spec: dict[str, Any], fmt: str, out_dir: Path, fps: int = 30, scale: float = 1.0,
           music_override: str | None = None, contact: bool = True,
           runtime: Runtime | None = None, n_formats: int = 1,
           contact_frames: int | None = None) -> dict[str, Any]:
    """Render one format. `n_formats` is how many formats this run renders in all, for the frame budget.
    `contact_frames` sets how many stills per scene the contact sheet has (default: two)."""
    ffmpeg = ffmpeg_exe()
    if ffmpeg is None:
        raise RenderError("ffmpeg not found. Install it (macOS: brew install ffmpeg; Debian/Ubuntu: sudo apt install "
                          "ffmpeg) or run: pip install imageio-ffmpeg")
    if not 1 <= fps <= 60:
        raise RenderError("fps must be between 1 and 60")
    rt = runtime or Runtime.for_spec(spec)
    spec = {**spec, "scenes": [dict(sc) for sc in spec["scenes"]]}  # narration stretches scene lengths: work on a copy
    if music_override in music_mod.STYLE_DEFAULTS or music_override == "none":
        spec["music"] = music_mod.with_style(spec["music"], music_override)  # before beat snapping reads the bpm
    out = out_dir / fmt
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yamlreel-") as td:
        work = Path(td)
        cache = out_dir / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        try:
            clips = voice_mod.prepare_narration(spec, rt.voice, cache, lambda p: resolve_local(p, Path(spec.get("_dir", ".")), rt.roots))
        except (voice_mod.VoiceError, UnsafeInput) as e:
            raise RenderError(f"voiceover: {e}") from e
        # The spec limits were checked on load; check again now that narration has stretched the scenes and beat
        # snapping has rounded them, and in case the caller never validated. Absolute limits always apply.
        limits = spec.get("limits") or {}
        limit = min(limits.get("max_seconds", HARD_LIMITS["max_seconds"]), HARD_LIMITS["max_seconds"])
        try:
            words, sub_warnings = subs_mod.scene_words(
                spec, clips, lambda p: resolve_local(p, Path(spec.get("_dir", ".")), rt.roots))
        except (subs_mod.SubtitleError, UnsafeInput) as e:
            raise RenderError(f"subtitles: {e}") from e
        durations = plan_durations(spec, subs_mod.raise_floors(spec, words, voice_mod.scene_floors(spec, clips)))
        total_seconds = sum(durations)
        if total_seconds > limit:
            raise RenderError(f"after narration and beat snapping the video is {total_seconds:.1f}s; "
                              f"the limit is {limit:g}s (limits.max_seconds)")
        try:
            check_frame_budget(total_seconds, fps, n_formats)
        except SpecError as e:
            raise RenderError(str(e)) from e
        tl = Timeline(spec, fmt, cache, scale, rt, durations=durations, words=words)
        tl.ctx.warnings.extend(w for w in sub_warnings if w not in tl.ctx.warnings)
        total = tl.total
        srt = None
        if words and (spec.get("subtitles") or {}).get("srt"):
            cues = tl.subtitle_cues()
            srt = out / "subtitles.srt"
            srt.write_text(subs_mod.srt_text(cues, bool(spec["subtitles"].get("uppercase"))), encoding="utf-8")
        else:
            (out / "subtitles.srt").unlink(missing_ok=True)   # a file from an earlier run would be taken for this one's
        narration = voice_mod.write_wav(work / "narration.wav", voice_mod.build_track(clips, tl.starts, total)) if clips else None
        duck = None
        if clips and (spec.get("voiceover") or {}).get("duck"):
            duck = (speech_regions(clips, tl.starts), float(spec["voiceover"].get("music_level", 0.35)))
        audio = _prepare_audio(spec, total, work, music_override, rt, energy_sections(spec, tl.starts, durations),
                               duck, ffmpeg)
        poster = tl.frame_at(tl.poster_time())
        poster.convert("RGB").save(out / "poster.jpg", quality=92)

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
        if inputs:
            vol = float(spec["music"].get("volume", 0.9))
            fade_out = max(0.0, total - 2.0)
            music_f = (f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={{vol}},"
                       f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.3f}:d=2.0")
            if audio and narration:
                level = float(spec["voiceover"].get("music_level", 0.35))  # music sits under the voice
                graph = (f"[1:a]{music_f.format(vol=vol if duck else vol * level)}[m];"
                         f"[2:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS[n];"
                         "[m][n]amix=inputs=2:duration=longest:normalize=0[a]")
                cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]"]
            elif audio:
                cmd += ["-af", music_f.format(vol=vol)]
            else:
                # Narration only: never fade the speech; just a tiny fade after it to avoid a click at the end.
                speech_end = max((tl.starts[i] + voice_mod.NARRATION_LEAD + len(c) / voice_mod.SAMPLE_RATE
                                  for i, c in clips.items()), default=0.0)
                end_fade = max(speech_end, total - 0.05)  # the fade starts after the last word, never inside it
                cmd += ["-af", f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={end_fade:.3f}:d=0.05"]
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        cmd += ["-movflags", "+faststart", "-t", f"{total:.3f}", str(out / "video.mp4")]
        errlog = open(work / "ffmpeg.log", "wb")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog)
        except OSError as e:
            errlog.close()
            raise RenderError(f"cannot run ffmpeg at {ffmpeg}: {e}") from e
        try:
            n_frames = int(round(total * fps))
            for n in range(n_frames):
                img = poster if n == 0 else tl.frame_at(n / fps)  # poster doubles as frame 0 (thumbnail)
                proc.stdin.write(img.convert("RGB").tobytes())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait()
        errlog.close()
        if rc != 0:
            raise RenderError("ffmpeg failed:\n" + (work / "ffmpeg.log").read_text(errors="replace")[-1500:])
        sheet = None
        if contact:
            fracs = contact_fracs(contact_frames)
            sheet = contact_sheet(tl, out / "contact.png", fracs=fracs)
    return {"video": out / "video.mp4", "poster": out / "poster.jpg", "contact": sheet, "srt": srt,
            "seconds": total, "warnings": tl.warnings, "size": (W, H)}
