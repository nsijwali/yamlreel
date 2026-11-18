"""Render planning: everything about a render that does not depend on the format.

`build()` loads and processes the narration, reads subtitle word timings, decides every scene's length (narration
floors, beat snapping), checks the length and frame limits, and works out the `--only` windows. The result is a typed
`RenderPlan`. render() makes one per call when it is not given one; the CLI makes one per run and hands it to every
format, so narration is processed once and the audio tracks (narration, music, the `mix:` master), which the plan
caches in its own folder, are prepared once.

Kept free of the renderer: render.py imports this module, never the other way round. render passes its own
`plan_durations` in (`durations_fn`), so code and tests that replace `render.plan_durations` keep working.
"""
from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

import numpy as np

from . import music as music_mod
from . import subtitles as subs_mod
from . import voice as voice_mod
from .config import HARD_LIMITS
from .safety import UnsafeInput, resolve_local
from .specs import SpecError, check_frame_budget


class RenderError(RuntimeError):
    pass


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


def scene_windows(only: list[int]) -> list[tuple[int, int]]:
    """Runs of consecutive scene numbers in `only` (sorted, unique) as (first, last) pairs: [1, 2, 3, 5] -> [(1, 3), (5, 5)]."""
    out: list[tuple[int, int]] = []
    for i in only:
        if out and out[-1][1] == i - 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def only_label(windows: list[tuple[int, int]]) -> str:
    """The file-name part for a set of windows: '2-4', '1_5', '0-1_3'."""
    return "_".join(str(a) if a == b else f"{a}-{b}" for a, b in windows)


@dataclass
class AudioTracks:
    """The audio inputs of the encoder, made once per plan: music and narration, or one mastered `mix:` track."""
    key: tuple[tuple[float, ...], float]       # (scene starts, total) they were made for
    audio: Path | None
    narration: Path | None
    duck: tuple[list[tuple[float, float]], float] | None
    mix: dict[str, float] | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RenderPlan:
    spec: dict[str, Any]                  # the working copy: music override applied, scenes stretched by narration
    fps: int
    only: list[int] | None
    music_override: str | None
    clips: dict[int, np.ndarray]          # scene index -> processed narration samples
    words: dict[int, subs_mod.SceneWords]
    sub_warnings: list[str]
    durations: list[float]
    starts: list[float]
    total: float
    windows: list[tuple[int, int]]        # `--only` runs of scenes (empty for a full render)
    spans: list[tuple[float, float]]      # the same runs as seconds of the full video
    tag: str                              # file-name suffix: "" or "-scenes-<label>"
    seconds_spent: float                  # narration and timing, for --profile
    workdir: Path                         # the plan's own folder (audio tracks shared by every format)
    audio: AudioTracks | None = None

    def matches(self, fps: int, only: list[int] | None, music_override: str | None) -> bool:
        return (self.fps, self.only, self.music_override) == (fps, only, music_override)

    def close(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> "RenderPlan":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def build(spec: dict[str, Any], *, fps: int, n_formats: int, only: list[int] | None, music_override: str | None,
          voice: voice_mod.VoiceServer | None, roots: list[Path], cache: Path,
          durations_fn: Callable[..., list[float]] | None = None, clock: Callable[[], float] | None = None
          ) -> RenderPlan:
    """The plan for rendering `spec` (any format). Raises RenderError with the same messages render() always gave."""
    import time
    clock = clock or time.perf_counter
    durations_fn = durations_fn or plan_durations
    t0 = clock()
    spec = {**spec, "scenes": [dict(sc) for sc in spec["scenes"]]}  # narration stretches scene lengths: work on a copy
    if music_override in music_mod.STYLE_DEFAULTS or music_override == "none":
        spec["music"] = music_mod.with_style(spec["music"], music_override)  # before beat snapping reads the bpm
    base = Path(spec.get("_dir", "."))
    cache.mkdir(parents=True, exist_ok=True)
    # The spec limits were checked on load; check again as narration stretches the scenes (prepare_narration stops
    # loading clips once they alone pass the limit) and after beat snapping has rounded them, and in case the caller
    # never validated. Absolute limits always apply.
    limits = spec.get("limits") or {}
    limit = min(limits.get("max_seconds", HARD_LIMITS["max_seconds"]), HARD_LIMITS["max_seconds"])
    try:
        clips = voice_mod.prepare_narration(spec, voice, cache, lambda p: resolve_local(p, base, roots),
                                            max_seconds=limit)
    except (voice_mod.VoiceError, UnsafeInput) as e:
        raise RenderError(f"voiceover: {e}") from e
    try:
        words, sub_warnings = subs_mod.scene_words(spec, clips, lambda p: resolve_local(p, base, roots))
    except (subs_mod.SubtitleError, UnsafeInput) as e:
        raise RenderError(f"subtitles: {e}") from e
    durations = durations_fn(spec, subs_mod.raise_floors(spec, words, voice_mod.scene_floors(spec, clips)))
    total = sum(durations)
    if total > limit:
        raise RenderError(f"after narration and beat snapping the video is {total:.1f}s; "
                          f"the limit is {limit:g}s (limits.max_seconds)")
    starts = [sum(durations[:k]) for k in range(len(durations))]
    windows = scene_windows(only) if only is not None else []
    spans = [(starts[a], starts[b] + durations[b]) for a, b in windows]   # seconds in the full video
    try:
        check_frame_budget(sum(b - a for a, b in spans) if only is not None else total, fps, n_formats)
    except SpecError as e:
        raise RenderError(str(e)) from e
    return RenderPlan(spec=spec, fps=fps, only=None if only is None else list(only), music_override=music_override,
                      clips=clips, words=words, sub_warnings=sub_warnings, durations=durations, starts=starts,
                      total=total, windows=windows, spans=spans,
                      tag=f"-scenes-{only_label(windows)}" if only is not None else "",
                      seconds_spent=clock() - t0, workdir=Path(tempfile.mkdtemp(prefix="yamlreel-")))


def frame_runs(plan: RenderPlan, total: float) -> list[tuple[int, int]]:
    """Frames of each window: the full video's indices ceil(a*fps) .. ceil(b*fps)-1; a full render is one window."""
    fps = plan.fps
    if plan.only is None:
        return [(0, int(round(total * fps)))]
    return [(math.ceil(a * fps), min(math.ceil(b * fps), int(round(total * fps)))) for a, b in plan.spans]
