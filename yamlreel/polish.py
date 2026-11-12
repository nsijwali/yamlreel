"""Optional post-production add-on: audit and polish a finished video.

Nothing in the render path imports this module. It works on any video file (one yamlreel rendered, or one made by
another tool), needs no new dependency (numpy and ffmpeg, which yamlreel already uses) and changes no spec field.

- `audit_video`: loudness (integrated LUFS, loudness range, true peak), a loop check (does the last frame lead
  cleanly into the first?) and short-form format hints.
- `polish_video`: mix timed sound effects over the video's own audio, then limit the peaks and, if asked,
  normalise the loudness. The effects are synthesised here, or are your own recordings (`file:`).
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

SR = 44100
MAX_EVENTS = 500
MAX_FILE_SECONDS = 30.0
DEFAULT_GAIN = 0.5
MAX_GAIN = 2.0
TARGET_LUFS = -14.0          # what YouTube, Shorts and Reels roughly aim for
LUFS_TOLERANCE = 2.0
TRUE_PEAK_CEILING = -1.0     # dBTP; AAC encoding can overshoot a hotter file
LOOP_FLOOR = 0.004           # mean pixel step (0-1) that no loop check flags


class PolishError(ValueError):
    """A problem with the input or the tools; the CLI prints it as `error: ...`."""


def _ffmpeg() -> str:
    from .render import ffmpeg_exe
    exe = ffmpeg_exe()
    if not exe:
        raise PolishError("ffmpeg was not found. Install it, or run: pip install 'yamlreel[ffmpeg]'")
    return exe


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    done = subprocess.run(cmd, capture_output=True)
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise PolishError(f"ffmpeg failed while {what}: {' | '.join(tail) or 'no message'}")
    return done


# --- probe -----------------------------------------------------------------------------------------------

@dataclass
class Probe:
    seconds: float
    width: int
    height: int
    fps: float | None
    has_audio: bool


def probe(video: Path) -> Probe:
    if not video.is_file():
        raise PolishError(f"{video} is not a file")
    done = subprocess.run([_ffmpeg(), "-hide_banner", "-protocol_whitelist", "file", "-i", str(video)],
                          capture_output=True)       # no output file: ffmpeg exits 1 after printing the streams
    text = done.stderr.decode("utf-8", "replace")
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    size = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    if not dur or not size:
        raise PolishError(f"{video} does not look like a video file")
    fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    h, m, s = dur.groups()
    return Probe(seconds=int(h) * 3600 + int(m) * 60 + float(s), width=int(size[1]), height=int(size[2]),
                 fps=float(fps[1]) if fps else None, has_audio=bool(re.search(r"Stream #.*Audio:", text)))


# --- audit -----------------------------------------------------------------------------------------------

@dataclass
class Audio:
    lufs: float
    lra: float
    true_peak: float


def measure_audio(video: Path) -> Audio | None:
    """Integrated loudness, loudness range and true peak (EBU R128), or None when the video has no audio."""
    if not probe(video).has_audio:
        return None
    done = subprocess.run([_ffmpeg(), "-hide_banner", "-nostats", "-protocol_whitelist", "file", "-i", str(video),
                           "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True)
    if done.returncode != 0:
        raise PolishError("ffmpeg could not measure the audio")
    text = done.stderr.decode("utf-8", "replace")
    summary = text.rsplit("Summary:", 1)[-1]
    vals = []
    for label, unit in (("I:", "LUFS"), ("LRA:", "LU"), ("Peak:", "dBFS")):
        m = re.search(rf"{re.escape(label)}\s+(-?(?:inf|\d+(?:\.\d+)?))\s*{unit}", summary)
        if not m:
            raise PolishError("could not read the loudness summary from ffmpeg")
        vals.append(float(m[1]))
    return Audio(*vals)


@dataclass
class Loop:
    jump: float        # mean pixel difference between the last and first frame, 0-1
    step: float        # median pixel difference between neighbouring frames, 0-1

    @property
    def threshold(self) -> float:
        return max(LOOP_FLOOR, 3 * self.step)

    @property
    def clean(self) -> bool:
        return self.jump <= self.threshold


def measure_loop(video: Path, max_seconds: float = 180.0) -> Loop:
    info = probe(video)
    if info.seconds > max_seconds:
        raise PolishError(f"loop check reads every frame; {video.name} is {info.seconds:.0f}s (limit {max_seconds:.0f}s)")
    side = 64
    done = _run([_ffmpeg(), "-loglevel", "error", "-protocol_whitelist", "file", "-i", str(video), "-an",
                 "-vf", f"scale={side}:{side},format=gray", "-f", "rawvideo", "-"], "reading frames")
    frames = np.frombuffer(done.stdout, np.uint8)
    frames = frames[: len(frames) // (side * side) * side * side].reshape(-1, side * side).astype(np.float32) / 255
    if len(frames) < 2:
        raise PolishError("the video has fewer than two frames")
    steps = np.abs(np.diff(frames, axis=0)).mean(axis=1)
    return Loop(jump=float(np.abs(frames[-1] - frames[0]).mean()), step=float(np.median(steps)))


@dataclass
class Report:
    info: Probe
    audio: Audio | None
    loop: Loop
    lines: list[str] = field(default_factory=list)
    warnings: int = 0


def audit_video(video: Path, target_lufs: float = TARGET_LUFS) -> Report:
    info = probe(video)
    audio = measure_audio(video)
    loop = measure_loop(video)
    rep = Report(info, audio, loop)

    def note(ok: bool, msg: str) -> None:
        rep.lines.append(f"{'ok  ' if ok else 'warn'}: {msg}")
        rep.warnings += 0 if ok else 1

    vertical = info.height > info.width
    fps = f", {info.fps:g} fps" if info.fps else ""
    rep.lines.append(f"info: {info.width}x{info.height}{fps}, {info.seconds:.1f}s, "
                     f"{'with' if info.has_audio else 'no'} audio")
    if vertical:
        note(info.seconds <= 60, f"vertical, {info.seconds:.1f}s" + ("" if info.seconds <= 60 else
             " (YouTube Shorts count only videos of 60 seconds or less)"))
    if audio is None:
        note(False, "no audio track")
    elif audio.lufs == float("-inf"):
        note(False, "the audio is silent")
    else:
        off = audio.lufs - target_lufs
        note(abs(off) <= LUFS_TOLERANCE, f"loudness {audio.lufs:.1f} LUFS (target {target_lufs:g}, range {audio.lra:.1f} LU)"
             + ("" if abs(off) <= LUFS_TOLERANCE else "; quieter than other videos" if off < 0 else "; louder than the target"))
        note(audio.true_peak <= TRUE_PEAK_CEILING, f"true peak {audio.true_peak:.1f} dBTP"
             + ("" if audio.true_peak <= TRUE_PEAK_CEILING else f" (above {TRUE_PEAK_CEILING:g}; may clip after upload)"))
    note(loop.clean, f"loop: last-to-first jump {loop.jump:.3f}, normal frame step {loop.step:.3f}"
         + ("" if loop.clean else "; the end does not lead into the start, so a replay shows a cut"))
    return rep


# --- synthesised effects -----------------------------------------------------------------------------------

def _decay(d: float, k: float) -> np.ndarray:
    return np.exp(-np.arange(int(d * SR)) / SR * k)


def _lp(x: np.ndarray, w: int) -> np.ndarray:
    return np.convolve(x, np.ones(w) / w, "same")


def _band(rng: np.random.Generator, d: float, lo: int, hi: int) -> np.ndarray:
    """Band-limited noise, normalised to a peak of 1: smooth with a short window minus smooth with a long one."""
    w = rng.normal(0, 1, int(d * SR))
    x = _lp(w, lo) - _lp(w, hi)
    return x / (np.max(np.abs(x)) or 1.0)


def _thump(f: float, d: float = 0.16) -> np.ndarray:
    x = np.arange(int(d * SR)) / SR
    return np.sin(2 * np.pi * (f * np.exp(-x * 9)) * x) * _decay(d, 22)


def _tink(freqs: list[float], d: float, k: float, amps: list[float]) -> np.ndarray:
    x = np.arange(int(d * SR)) / SR
    return sum(a * np.sin(2 * np.pi * f * x) for f, a in zip(freqs, amps)) * _decay(d, k)


def _place(out: np.ndarray, sig: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(at * SR)
    out[i:i + len(sig)] += gain * sig[: len(out) - i]


def _tap(rng):
    f = rng.uniform(1600, 2000)
    out = np.zeros(int(0.3 * SR))
    _place(out, _tink([f, f * 1.52, f * 2.31], 0.22, 16, [1, 0.55, 0.3]), 0, 0.5)
    _place(out, _thump(rng.uniform(150, 190)), 0, 0.5)
    return out


def _clatter(rng):
    out = np.zeros(int(0.8 * SR))
    _place(out, _thump(95, 0.35), 0, 0.6)
    for dl, fr in ((0, 2100), (0.07, 3100), (0.15, 1650), (0.24, 2750), (0.34, 3900)):
        _place(out, _tink([fr, fr * 1.47, fr * 2.2], 0.3, 14, [1, 0.6, 0.35]), dl, 0.4 * (1 - dl))
    return out


def _splash(rng):
    return _band(rng, 0.45, 4, 40) * _decay(0.45, 7)


def _scrape(rng):
    return _band(rng, 0.4, 8, 70) * np.hanning(int(0.4 * SR))


def _whoosh(rng):
    d = 0.5
    return _band(rng, d, 6, 60) * np.sin(np.linspace(0, np.pi, int(d * SR))) ** 2


def _pop(rng):
    d = 0.1
    x = np.arange(int(d * SR)) / SR
    return np.sin(2 * np.pi * (200 + 500 * np.exp(-x * 40)) * x) * _decay(d, 45)


def _click(rng):
    out = np.zeros(int(0.05 * SR))
    _place(out, _band(rng, 0.01, 2, 12), 0, 0.6)
    _place(out, _tink([3800, 5200], 0.04, 90, [1, 0.4]), 0, 0.4)
    return out


def _tick(rng):
    return _tink([2200, 3300], 0.06, 60, [1, 0.4])


def _rustle(rng):
    d = 0.3
    return _lp(rng.normal(0, 1, int(d * SR)), 25) * np.hanning(int(d * SR))


def _ping(rng):
    return _tink([1320, 1320 * 2.76, 1320 * 5.4], 0.7, 7, [1, 0.35, 0.15])


EFFECTS = {"tap": _tap, "thump": lambda rng: _thump(110, 0.3), "clatter": _clatter, "splash": _splash,
           "scrape": _scrape, "whoosh": _whoosh, "pop": _pop, "click": _click, "tick": _tick,
           "rustle": _rustle, "ping": _ping}


def synth_effect(kind: str, seed: int = 0) -> np.ndarray:
    """One effect, deterministic for a seed, scaled to a peak of 1."""
    if kind not in EFFECTS:
        raise PolishError(f"unknown effect {kind!r}; use one of: {', '.join(sorted(EFFECTS))}")
    sig = np.asarray(EFFECTS[kind](np.random.default_rng(seed)), dtype=np.float64)
    return sig / (np.max(np.abs(sig)) or 1.0)


# --- events file -------------------------------------------------------------------------------------------

@dataclass
class Event:
    at: float
    gain: float
    kind: str | None = None
    file: Path | None = None


def load_events(path: Path, allow: list[Path] | None = None) -> list[Event]:
    """Read `sfx:` events: `{at: seconds, kind: tap|...|file: recording.wav, gain: 0-2}`. A `file:` must stay inside
    the events file's folder or a folder in `allow`, so a shared events file cannot read other files."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise PolishError(f"cannot read {path}: {e}") from None
    items = raw.get("sfx") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise PolishError(f"{path.name}: expected a non-empty `sfx:` list")
    if len(items) > MAX_EVENTS:
        raise PolishError(f"{path.name}: at most {MAX_EVENTS} events")
    roots = [path.parent.resolve(), *[Path(a).expanduser().resolve() for a in (allow or [])]]
    events = []
    for n, it in enumerate(items, 1):
        where = f"{path.name}: sfx item {n}"
        if not isinstance(it, dict):
            raise PolishError(f"{where} must be a mapping")
        extra = set(it) - {"at", "kind", "file", "gain"}
        if extra:
            raise PolishError(f"{where}: unknown field {sorted(extra)[0]!r} (use at, kind, file, gain)")
        at, gain = it.get("at"), it.get("gain", DEFAULT_GAIN)
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at <= 3600:
            raise PolishError(f"{where}: `at` must be seconds from the start, 0 to 3600")
        if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not 0 <= gain <= MAX_GAIN:
            raise PolishError(f"{where}: `gain` must be 0 to {MAX_GAIN:g}")
        if ("kind" in it) == ("file" in it):
            raise PolishError(f"{where}: give exactly one of `kind` or `file`")
        if "kind" in it:
            if it["kind"] not in EFFECTS:
                raise PolishError(f"{where}: unknown kind {it['kind']!r}; use one of: {', '.join(sorted(EFFECTS))}")
            events.append(Event(float(at), float(gain), kind=it["kind"]))
            continue
        if not isinstance(it["file"], str) or not it["file"]:
            raise PolishError(f"{where}: `file` must be a path")
        f = (path.parent / it["file"]).expanduser().resolve()
        if not any(f.is_relative_to(r) for r in roots):
            raise PolishError(f"{where}: {it['file']} is outside the events file's folder; "
                              "move it there or pass --allow-path DIR")
        if not f.is_file():
            raise PolishError(f"{where}: {it['file']} does not exist")
        events.append(Event(float(at), float(gain), file=f))
    return events


def _decode(file: Path) -> np.ndarray:
    from .render import AUDIO_FORMATS
    done = _run([_ffmpeg(), "-loglevel", "error", "-protocol_whitelist", "file", "-format_whitelist", AUDIO_FORMATS,
                 "-i", str(file), "-t", f"{MAX_FILE_SECONDS:g}", "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                f"reading {file.name}")
    sig = np.frombuffer(done.stdout, "<f4").astype(np.float64)
    if not len(sig):
        raise PolishError(f"{file.name} has no audio")
    return sig


def render_events(events: list[Event], seconds: float) -> np.ndarray:
    """All events mixed into one mono track exactly `seconds` long. Recordings keep their own level (times gain);
    synthesised effects are scaled to a peak of 1 first."""
    out = np.zeros(int(round(seconds * SR)))
    cache: dict[Path, np.ndarray] = {}
    for i, e in enumerate(events):
        if e.at >= seconds:
            continue
        if e.kind:
            sig = synth_effect(e.kind, seed=i)
        else:
            sig = cache.setdefault(e.file, _decode(e.file))
        _place(out, sig, e.at, e.gain)
    return out


# --- polish ------------------------------------------------------------------------------------------------

def _write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def filter_graph(has_audio: bool, has_sfx: bool, seconds: float, loudness: float | None) -> str:
    """The ffmpeg filter graph; the final label is [out]."""
    if has_audio and has_sfx:
        head = ("[0:a]aresample=44100[a0];[1:a]aresample=44100[s];"
                "[a0][s]amix=inputs=2:duration=first:normalize=0[mix]")
    elif has_sfx:
        head = f"[1:a]atrim=0:{seconds:.3f}[mix]"
    else:
        head = "[0:a]anull[mix]"
    tail = "alimiter=limit=0.95"
    if loudness is not None:
        tail = f"loudnorm=I={loudness:g}:TP=-1.5:LRA=11,aresample=48000," + tail
    return f"{head};[mix]{tail}[out]"


def polish_video(video: Path, out: Path, sfx: Path | None = None, loudness: float | None = None,
                 allow: list[Path] | None = None, force: bool = False) -> Path:
    if sfx is None and loudness is None:
        raise PolishError("nothing to do: pass --sfx EVENTS.yaml and/or --loudness LUFS")
    if loudness is not None and not -40 <= loudness <= -5:
        raise PolishError("--loudness must be between -40 and -5 LUFS (YouTube and Shorts aim for about -14)")
    if out.resolve() == video.resolve():
        raise PolishError("the output would overwrite the input; choose another --out")
    if out.exists() and not force:
        raise PolishError(f"{out} already exists; pass --force to overwrite it")
    info = probe(video)
    events = load_events(sfx, allow) if sfx else []
    if not events and not info.has_audio:
        raise PolishError("the video has no audio to normalise; add effects with --sfx")
    with tempfile.TemporaryDirectory(prefix="yamlreel-polish-") as tmp:
        cmd = [_ffmpeg(), "-loglevel", "error", "-y", "-protocol_whitelist", "file", "-i", str(video)]
        if events:
            track = Path(tmp) / "sfx.wav"
            _write_wav(track, render_events(events, info.seconds))
            cmd += ["-i", str(track)]
        cmd += ["-filter_complex", filter_graph(info.has_audio, bool(events), info.seconds, loudness),
                "-map", "0:v", "-map", "[out]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-movflags", "+faststart", "-t", f"{info.seconds:.3f}", str(out)]
        _run(cmd, "mixing the audio")
    return out
