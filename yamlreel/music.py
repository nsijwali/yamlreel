"""Original background music (no licence or credit needed). Deterministic for a given seed.

`koto` (the default) is a seeded walk over a Japanese pentatonic scale: plucked strings, a low drone, a faint
breath swell and reverb. It is built in one buffer, exactly as in 0.2.2, so its output never changes.

`ambient`, `lofi`, `pulse` and `piano` are synthesised from a global, seeded event list and rendered in 30 second
blocks (see `blocks`), so memory stays near 30 MB however long the track is. Each has three layers that the
per-scene `energy` setting fades in and out. `duck_gain` and `apply_duck` lower the music under known speech.
"""
from __future__ import annotations

import bisect
import wave
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import synth

SCALES = {
    "hirajoshi": [0, 2, 3, 7, 8],
    "in": [0, 1, 5, 7, 8],
    "yo": [0, 2, 5, 7, 9],
    "iwato": [0, 1, 5, 6, 10],
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
}
# What a style plays when the spec leaves bpm, root or scale out. Any scale works with any style.
STYLE_DEFAULTS: dict[str, dict[str, object]] = {
    "koto": {"bpm": 72, "root": "D", "scale": "hirajoshi"},
    "ambient": {"bpm": 60, "root": "C", "scale": "major"},
    "lofi": {"bpm": 78, "root": "D", "scale": "dorian"},
    "pulse": {"bpm": 100, "root": "A", "scale": "minor"},
    "piano": {"bpm": 76, "root": "G", "scale": "major"},
}
ENERGY_LEVELS = {"low": 0, "mid": 1, "high": 2}
NOTE_OFFSETS = {"C": 0, "C#": 1, "D": 2, "Eb": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}
SR = 44100


def _midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _pluck(freq: float, dur: float, rng: np.random.Generator) -> np.ndarray:
    """Karplus-Strong string, computed one period at a time."""
    period = max(2, int(round(SR / freq)))
    n = int(SR * dur)
    buf = np.convolve(rng.uniform(-1, 1, period), np.ones(3) / 3, "same")
    blocks = n // period + 1
    out = np.empty(blocks * period)
    for b in range(blocks):
        out[b * period:(b + 1) * period] = buf
        buf = 0.498 * (buf + np.roll(buf, -1))
    out = out[:n]
    return out * np.exp(-np.arange(n) / SR * 1.2)


def _pad(freq: float, dur: float, amp: float) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    s = sum(np.sin(2 * np.pi * freq * k * t + k) * a for k, a in [(1, 1.0), (2, 0.25), (3, 0.08)])
    env = np.minimum(1, t / 2.0) * np.minimum(1, np.maximum(dur - t, 0) / 2.5)
    return s * env * amp


def compose(duration: float, bpm: float = 72, root: str = "D", scale: str = "hirajoshi",
            seed: int = 7, density: float = 0.7) -> np.ndarray:
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose from {', '.join(SCALES)}")
    if root not in NOTE_OFFSETS:
        raise ValueError(f"unknown root {root!r}; choose from {', '.join(NOTE_OFFSETS)}")
    rng = np.random.default_rng(seed)
    root_midi = 60 + NOTE_OFFSETS[root]  # octave above middle C region
    pool = [root_midi + 12 * o + s for o in (0, 1) for s in SCALES[scale]] + [root_midi + 24]
    beat = 60.0 / bpm
    mix = np.zeros(int(SR * (duration + 4)))

    def add(x: np.ndarray, t: float) -> None:
        i = int(t * SR)
        mix[i:i + len(x)] += x[: len(mix) - i]

    idx = 2
    t = 0.0
    last_note_t = max(0.0, duration - 3.5)
    while t < last_note_t:
        if t == 0.0 or rng.random() < density:
            add(_pluck(_midi_hz(pool[idx]), 3.5, rng) * 0.55, t)
            if rng.random() < 0.18 and t + beat * 0.5 < last_note_t:  # light grace note
                add(_pluck(_midi_hz(pool[min(idx + 1, len(pool) - 1)]), 2.5, rng) * 0.3, t + beat * 0.5)
        idx = int(np.clip(idx + rng.choice([-2, -1, -1, 0, 1, 1, 2]), 0, len(pool) - 1))
        t += beat * 2
    add(_pluck(_midi_hz(pool[0]), 4.0, rng) * 0.6, last_note_t)  # resolve on the tonic

    add(_pad(_midi_hz(root_midi - 12), duration + 2, 0.10), 0)
    add(_pad(_midi_hz(root_midi - 5), duration + 2, 0.06), 0)
    tt = np.arange(int(SR * duration)) / SR
    air = np.convolve(rng.normal(0, 1, len(tt)), np.ones(60) / 60, "same")
    mix[: len(tt)] += air * 0.075 * np.sin(np.pi * tt / duration) ** 2

    ir_n = int(SR * 2.2)
    ir = rng.normal(0, 1, ir_n) * np.exp(-np.arange(ir_n) / SR * 2.6)
    ir[0] = 0
    size = len(mix) + ir_n
    wet = np.fft.irfft(np.fft.rfft(mix, size) * np.fft.rfft(ir, size), size)[: len(mix)]
    wet /= np.abs(wet).max() + 1e-9
    out = mix / (np.abs(mix).max() + 1e-9) * 0.7 + wet * 0.45
    out /= np.abs(out).max() + 1e-9
    return (out * 0.8)[: int(SR * duration)].astype(np.float32)


def write_wav(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def with_style(music: dict, style: str) -> dict:
    """The spec's music settings with `style` swapped in (`--music lofi`). Only the keys the spec left to its
    style (`_filled`) take the new style's defaults; ones the author wrote stay."""
    out = dict(music)
    out["style"] = style
    defaults = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["koto"])
    for key in music.get("_filled", ()):
        out[key] = defaults[key]
    return out


# ---------------------------------------------------------------------------------------------------------
# Synthesised styles
# ---------------------------------------------------------------------------------------------------------

BLOCK_SECONDS = 30
MAX_NOTE = 12.0          # longest event, release included; also the width of the carry between blocks
LAYERS = 3               # 0 always on, 1 from `mid`, 2 at `high`
SYNTH_STYLES = ("ambient", "lofi", "pulse", "piano")
DUCK_ATTACK = 0.15       # seconds the music takes to go down before speech starts
DUCK_RELEASE = 0.5       # and to come back after it ends
DUCK_MIN_GAP = 0.6       # a pause shorter than this keeps the music down


class Event(NamedTuple):
    t: float
    dur: float
    voice: str
    midi: float
    vel: float
    layer: int
    seed: int


def _degree_note(scale: list[int], root_midi: int, k: int) -> int:
    return root_midi + scale[k % len(scale)] + 12 * (k // len(scale))


def _chord(scale: list[int], root_midi: int, degree: int, size: int = 3) -> list[int]:
    return [_degree_note(scale, root_midi, degree + 2 * i) for i in range(size)]


def _progression(n: int) -> list[int]:
    """i - VI - III - VII for a seven note scale, spread the same way over shorter ones."""
    return [0, int(5 * n / 7), int(2 * n / 7), n - 1]


class _Plan(NamedTuple):
    beat: float
    scale: list[int]
    root: int            # pitch class offset 0..11
    duration: float
    rng: np.random.Generator


def _ev(t: float, dur: float, voice: str, midi: float, vel: float, layer: int, rng: np.random.Generator) -> Event:
    return Event(max(0.0, t), min(dur, MAX_NOTE), voice, midi, vel, layer, int(rng.integers(0, 2**31)))


def _ambient(p: _Plan) -> list[Event]:
    beats = 8 if p.beat <= 1.1 else 4
    chord_len = beats * p.beat
    prog = _progression(len(p.scale))
    out: list[Event] = []
    walk = 0
    t, bar = 0.0, 0
    while t < p.duration:
        deg = prog[bar % 4]
        for m in _chord(p.scale, 48 + p.root, deg):
            out.append(_ev(t, chord_len, "pad", m, 0.5, 0, p.rng))
        tones = [m + 24 for m in _chord(p.scale, 48 + p.root, deg)]
        for b in range(0, beats, 2):                       # a sparse bell motif above the pad
            if p.rng.random() < 0.45:
                walk = int(np.clip(walk + p.rng.choice([-1, 0, 1]), 0, len(tones) - 1))
                out.append(_ev(t + b * p.beat, 3.5, "bell", tones[walk], 0.4, 1, p.rng))
        for b in range(beats):                             # soft sub pulse on every beat
            out.append(_ev(t + b * p.beat, 1.2, "sub", 36 + p.root + p.scale[deg % len(p.scale)], 0.55, 2, p.rng))
        t += chord_len
        bar += 1
    return out


def _lofi(p: _Plan) -> list[Event]:
    prog = _progression(len(p.scale))
    out: list[Event] = []
    bar_len = 4 * p.beat

    def eighth(bar: int, k: int) -> float:                # k 0..7; off-beats land 55% into the beat
        return bar * bar_len + (k // 2) * p.beat + (0.55 * p.beat if k % 2 else 0.0)

    def human(t: float) -> float:
        return t + float(p.rng.normal(0, 0.006))

    bar = 0
    while bar * bar_len < p.duration:
        deg = prog[bar % 4]
        seventh = _chord(p.scale, 53 + p.root, deg, 4)
        for k, vel in ((0, 0.8), (5, 0.6)):
            for m in seventh:
                out.append(_ev(human(eighth(bar, k)), 2.4 * p.beat, "epiano", m, vel * float(p.rng.uniform(0.85, 1)), 0, p.rng))
        for k in range(8):                                  # layer 1: hats and kick
            out.append(_ev(human(eighth(bar, k)), 0.08, "hat", 0, 0.5 if k % 2 == 0 else 0.35, 1, p.rng))
        for k in (0, 4):
            out.append(_ev(human(eighth(bar, k)), 0.45, "kick", 0, 0.9, 1, p.rng))
        if p.rng.random() < 0.35:
            out.append(_ev(human(eighth(bar, 5)), 0.45, "kick", 0, 0.6, 1, p.rng))
        root_note = 36 + p.root + p.scale[deg % len(p.scale)]
        for k in (2, 6):                                    # layer 2: snare on 2 and 4, bass
            out.append(_ev(human(eighth(bar, k)), 0.25, "snare", 0, 0.8, 2, p.rng))
        for k, vel in ((0, 0.8), (4, 0.65)):
            out.append(_ev(human(eighth(bar, k)), 1.8 * p.beat, "sub", root_note, vel, 2, p.rng))
        bar += 1
    return out


_ARP_PATTERNS = ([0, 1, 2, 3, 2, 1, 2, 1], [0, 2, 1, 3, 2, 3, 1, 2], [0, 1, 2, 1, 3, 2, 1, 2])


def _pulse(p: _Plan) -> list[Event]:
    prog = _progression(len(p.scale))
    out: list[Event] = []
    bar_len = 4 * p.beat
    bar = 0
    while bar * bar_len < p.duration:
        deg = prog[bar % 4]
        triad = _chord(p.scale, 57 + p.root, deg)
        notes = [triad[0], triad[1], triad[2], triad[0] + 12]
        pattern = _ARP_PATTERNS[int(p.rng.integers(0, len(_ARP_PATTERNS)))]
        for k, idx in enumerate(pattern):
            if p.rng.random() < 0.93:
                out.append(_ev(bar * bar_len + k * p.beat / 2, 1.2, "pluck", notes[idx] + 12, 0.8 if k % 4 == 0 else 0.55, 0, p.rng))
        root_note = 36 + p.root + p.scale[deg % len(p.scale)]
        for b in (0, 2):
            out.append(_ev(bar * bar_len + b * p.beat, 2 * p.beat, "sub", root_note, 0.8, 1, p.rng))
            out.append(_ev(bar * bar_len + b * p.beat, 0.45, "kick", 0, 0.9, 2, p.rng))
        bar += 1
    return out


def _piano(p: _Plan) -> list[Event]:
    prog = _progression(len(p.scale))
    pool = [_degree_note(p.scale, 60 + p.root, k) for k in range(2 * len(p.scale) + 1)]
    out: list[Event] = []
    bar_len = 4 * p.beat
    idx = len(p.scale)
    bar = 0
    while bar * bar_len < p.duration:
        deg = prog[bar % 4]
        chord = _chord(p.scale, 60 + p.root, deg)
        for b in range(8):                                    # eighth-note grid, many rests
            if b == 0:
                idx = min(range(len(pool)), key=lambda i: min(abs(pool[i] - c) for c in chord) + 0.01 * abs(i - idx))
            elif p.rng.random() < 0.5:
                idx = int(np.clip(idx + p.rng.choice([-2, -1, -1, 0, 1, 1, 2]), 0, len(pool) - 1))
            else:
                continue
            t = bar * bar_len + b * p.beat / 2
            dur = float(p.rng.choice([1.5, 2.0, 3.0])) * p.beat
            vel = 0.8 if b % 4 == 0 else 0.6
            out.append(_ev(t, dur + 0.5, "piano", pool[idx], vel, 0, p.rng))
            out.append(_ev(t, dur + 0.5, "piano", min(pool[idx] + 12, 96), vel * 0.5, 2, p.rng))   # octave doubling
        low = _degree_note(p.scale, 36 + p.root, deg)
        fifth = _degree_note(p.scale, 36 + p.root, deg + 4)
        for m in (low, fifth):
            out.append(_ev(bar * bar_len, 3.5 * p.beat + 0.5, "piano", m, 0.55, 1, p.rng))
        bar += 1
    return out


_BUILDERS = {"ambient": _ambient, "lofi": _lofi, "pulse": _pulse, "piano": _piano}


def build_events(style: str, duration: float, bpm: float, root: str, scale: str, seed: int) -> list[Event]:
    """The whole track as a time-sorted list of notes. Cheap: no audio is made here."""
    plan = _Plan(60.0 / bpm, SCALES[scale], NOTE_OFFSETS[root], duration, np.random.default_rng(seed))
    return sorted((e for e in _BUILDERS[style](plan) if e.t < duration), key=lambda e: e.t)


# peak gain of each voice before the style's mix; kept here so the voices stay at plain amplitudes
_VOICE_GAIN = {"pad": 0.30, "bell": 0.35, "sub": 0.55, "epiano": 0.16, "kick": 0.85, "snare": 0.5, "hat": 0.35,
               "pluck": 0.30, "piano": 0.22}


def _render_event(e: Event) -> np.ndarray:
    hz = synth.midi_hz(e.midi)
    rng = np.random.default_rng(e.seed)
    if e.voice == "pad":
        x = synth.pad(hz, max(0.5, e.dur - 2.0), attack=1.6, release=2.0)
    elif e.voice == "bell":
        x = synth.bell(hz, e.dur)
    elif e.voice == "sub":
        x = synth.sub(hz, e.dur)
    elif e.voice == "epiano":
        x = synth.epiano(hz, e.dur, e.vel)
    elif e.voice == "kick":
        x = synth.kick(e.dur)
    elif e.voice == "snare":
        x = synth.snare(rng, e.dur)
    elif e.voice == "hat":
        x = synth.hat(rng, e.dur)
    elif e.voice == "pluck":
        x = synth.soft_pluck(hz, e.dur)
    else:  # piano
        x = synth.partials(hz, e.dur, [1 / k ** 1.1 for k in range(1, 11)], decay=1.1, inharmonicity=0.0004,
                           decay_slope=0.35)
    x = x * (e.vel * _VOICE_GAIN[e.voice])
    n = min(len(x), int(synth.SR * e.dur) or 1, int(MAX_NOTE * synth.SR))
    x = x[:n]
    r = min(n, 441)
    x[n - r:] *= np.linspace(1.0, 0.0, r)          # no click if a note is cut at its nominal length
    return x.astype(np.float32)


# (pre-gain into the soft clip, reverb level) per style, set so a track peaks at about 0.75
_STYLE_MIX = {"ambient": (1.6, 0.6), "lofi": (1.1, 0.25), "pulse": (1.3, 0.35), "piano": (1.5, 0.5)}
_CEILING = 0.9


def level_curve(t: np.ndarray, sections: list[tuple[float, float, int]] | None, beat: float) -> np.ndarray:
    """Energy (0 low, 1 mid, 2 high) at times `t`. Section edges snap to the nearest beat and the level slides
    between neighbours over one beat, centred on the edge."""
    if not sections:
        return np.ones(len(t))
    xs: list[float] = []
    ys: list[float] = []
    for a, b, lvl in sections:
        a, b = round(a / beat) * beat, round(b / beat) * beat
        xs += [a + beat / 2, b - beat / 2]
        ys += [lvl, lvl]
    xs_arr = np.maximum.accumulate(np.array(xs))
    return np.interp(t, xs_arr, np.array(ys, dtype=float))


def blocks(style: str, duration: float, bpm: float = 72, root: str = "D", scale: str = "hirajoshi", seed: int = 7,
           sections: list[tuple[float, float, int]] | None = None) -> Iterator[np.ndarray]:
    """The track as float32 blocks of at most BLOCK_SECONDS, exactly `int(SR * duration)` samples in all.

    `sections` is [(start, end, level)] with level 0..2 (see ENERGY_LEVELS); koto ignores it."""
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose from {', '.join(SCALES)}")
    if root not in NOTE_OFFSETS:
        raise ValueError(f"unknown root {root!r}; choose from {', '.join(NOTE_OFFSETS)}")
    total = int(SR * duration)
    if style == "koto":
        full = compose(duration, bpm, root, scale, seed)
        for i in range(0, total, SR * BLOCK_SECONDS):
            yield full[i:i + SR * BLOCK_SECONDS]
        return
    if style not in _BUILDERS:
        raise ValueError(f"unknown style {style!r}; choose from koto, {', '.join(SYNTH_STYLES)}")
    events = build_events(style, duration, bpm, root, scale, seed)
    starts = [int(e.t * SR) for e in events]
    gain, wet_level = _STYLE_MIX[style]
    reverb = synth.Reverb(np.random.default_rng([seed, 1]), seconds=1.8, decay=3.0)
    block = SR * BLOCK_SECONDS
    tail = int(MAX_NOTE * SR)
    carry = np.zeros((LAYERS, tail), dtype=np.float32)
    nxt = 0
    for b0 in range(0, total, block):
        n = min(block, total - b0)
        layers = np.zeros((LAYERS, n + tail), dtype=np.float32)
        layers[:, :tail] += carry[:, : min(tail, n + tail)]
        hi = bisect.bisect_left(starts, b0 + n)
        for k in range(nxt, hi):
            x = _render_event(events[k])
            i = starts[k] - b0
            layers[events[k].layer, i:i + len(x)] += x[: n + tail - i]
        nxt = hi
        carry = layers[:, n:n + tail].copy()
        bar = np.random.default_rng([seed, 2, b0 // block])
        if style == "ambient":
            tt = (b0 + np.arange(n)) / SR
            layers[0, :n] += (synth.air(bar, n) * 0.5 * (0.6 + 0.4 * np.sin(TWO_PI_20 * tt))).astype(np.float32)
        elif style == "lofi":
            layers[0, :n] += (synth.crackle(bar, n, 6.0) * 0.12).astype(np.float32)
        mix = layers[0, :n].copy()
        for i in range(0, n, SR * 5):                     # gate the layers in 5 s slices: small temporaries
            sl = slice(i, min(n, i + SR * 5))
            lvl = level_curve((b0 + np.arange(sl.start, sl.stop)) / SR, sections, 60.0 / bpm)
            mix[sl] += (np.clip(lvl, 0, 1) * layers[1, sl] + np.clip(lvl - 1, 0, 1) * layers[2, sl]).astype(np.float32)
        del layers
        mix *= gain
        yield synth.soft_clip(mix + reverb.process(mix) * wet_level, 1.0, _CEILING)


TWO_PI_20 = 2 * np.pi / 20.0


def generate(style: str, duration: float, bpm: float = 72, root: str = "D", scale: str = "hirajoshi", seed: int = 7,
             sections: list[tuple[float, float, int]] | None = None) -> np.ndarray:
    """`blocks` joined into one array (fine for short tracks and tests)."""
    return np.concatenate(list(blocks(style, duration, bpm, root, scale, seed, sections)))


def write_wav_stream(path: Path, parts: Iterable[np.ndarray]) -> Path:
    """Write blocks to a 16 bit mono WAV as they arrive; byte-identical to `write_wav` of their concatenation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        for part in parts:
            w.writeframes((np.clip(part, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


# ---------------------------------------------------------------------------------------------------------
# Ducking
# ---------------------------------------------------------------------------------------------------------

def merge_regions(regions: Iterable[tuple[float, float]], min_gap: float = DUCK_MIN_GAP) -> list[tuple[float, float]]:
    """Sort speech intervals and join those closer than `min_gap`, so the music stays down across short pauses."""
    out: list[list[float]] = []
    for a, b in sorted(regions):
        if out and a - out[-1][1] < min_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def duck_gain(start: int, n: int, regions: list[tuple[float, float]], level: float) -> np.ndarray:
    """Per-sample gain for samples start..start+n: 1 away from speech, `level` during it, a linear ramp down over
    DUCK_ATTACK before it and back up over DUCK_RELEASE after it. `regions` should come from `merge_regions`."""
    t = (start + np.arange(n)) / SR
    m = np.zeros(n)
    t0, t1 = t[0], t[-1]
    for a, b in regions:
        if b + DUCK_RELEASE < t0 or a - DUCK_ATTACK > t1:
            continue
        down = np.clip((t - (a - DUCK_ATTACK)) / DUCK_ATTACK, 0, 1)
        up = np.clip(((b + DUCK_RELEASE) - t) / DUCK_RELEASE, 0, 1)
        m = np.maximum(m, np.minimum(down, up))
    return (1.0 - (1.0 - level) * m).astype(np.float32)


def apply_duck(parts: Iterable[np.ndarray], regions: list[tuple[float, float]], level: float) -> Iterator[np.ndarray]:
    regions = merge_regions(regions)
    pos = 0
    for part in parts:
        yield part * duck_gain(pos, len(part), regions, level) if len(part) else part
        pos += len(part)


def read_wav_blocks(path: Path) -> Iterator[np.ndarray]:
    """A mono 16 bit WAV as float32 blocks of BLOCK_SECONDS."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected a mono 16 bit WAV")
        while True:
            raw = w.readframes(w.getframerate() * BLOCK_SECONDS)
            if not raw:
                return
            yield np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767
