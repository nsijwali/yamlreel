"""Designed sound effects for scene cues (`sfx:` on a scene), synthesised in numpy: no files, no licences.

Each sound has one job, so a viewer learns what it means:

    click    an element appears (soft UI tick)           pop      a chip or card lands
    tick     a counter steps                             thud     something breaks or goes down (low impact)
    riser    tension into a hit (noise + tone rising)    hit      the payoff lands (impact with a short tail)
    ding     a token is spent (small bell)               drip     a token refills
    whoosh   a camera push or a change of scene          swell    a slow fill (pad swell)
    stinger  the closing question (chord + bell)         deny     a request is refused (short falling buzz)
    hush     not a sound: the music drops out for `seconds` (a near-silence before a key moment)

`cue_track` places cues at exact sample positions and adds a small room reverb to the bus. Everything is pure and
seeded, so a spec renders the same audio every time. Nothing in the classic render path imports this module.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np

from . import synth

SR = synth.SR
TWO_PI = 2.0 * np.pi
SOUNDS = ("click", "pop", "tick", "thud", "riser", "hit", "ding", "drip", "whoosh", "swell", "stinger", "deny", "hush")
# A sound's level before the cue's own gain, set so the effects sit under the voice and over the music.
LEVELS = {"click": 0.22, "pop": 0.30, "tick": 0.16, "thud": 0.60, "riser": 0.30, "hit": 0.62, "ding": 0.26,
          "drip": 0.20, "whoosh": 0.26, "swell": 0.24, "stinger": 0.42, "deny": 0.26}
DEFAULT_SECONDS = {"riser": 1.2, "whoosh": 0.45, "swell": 1.6, "hush": 0.6}
MAX_CUES = 400     # sound cues in a whole spec (specs.py enforces it)
REVERB_WET = 0.16


class Cue(NamedTuple):
    t: float          # seconds from the start of the video
    sound: str
    gain: float = 1.0
    seconds: float | None = None
    pitch: float = 0.0   # semitones


def _t(d: float) -> np.ndarray:
    return np.arange(max(1, int(d * SR))) / SR


def _band_filter(x: np.ndarray, lo: float, hi: float, order: int = 2) -> np.ndarray:
    """Zero-phase band-pass by an FFT mask with Butterworth-shaped skirts (lo 0: low-pass, hi 0: high-pass)."""
    n = len(x)
    f = np.fft.rfftfreq(n, 1 / SR)
    g = np.ones_like(f)
    if lo > 0:
        g *= 1 / np.sqrt(1 + (lo / np.maximum(f, 1e-3)) ** (2 * order))
    if hi > 0:
        g *= 1 / np.sqrt(1 + (f / hi) ** (2 * order))
    return np.fft.irfft(np.fft.rfft(x) * g, n)


def _sweep_noise(rng: np.random.Generator, d: float, f0: float, f1: float, q: float = 0.6) -> np.ndarray:
    """Noise whose pass band slides from f0 to f1 (log), built by crossfading eight fixed bands."""
    n = int(d * SR)
    w = rng.normal(0, 1, n)
    centres = np.geomspace(min(f0, f1), max(f0, f1), 8)
    pos = np.log(np.geomspace(f0, f1, n))
    out = np.zeros(n)
    for c in centres:
        band = _band_filter(w, c * (1 - q / 2), c * (1 + q))
        weight = np.exp(-((pos - np.log(c)) / 0.35) ** 2)
        out += band * weight
    return out / (np.abs(out).max() + 1e-9)


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t * decay)


def _fade_tail(x: np.ndarray, ms: float = 8.0) -> np.ndarray:
    k = min(len(x), int(ms / 1000 * SR))
    if k:
        x[-k:] *= np.linspace(1, 0, k)
    return x


def click(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.06)
    k = 2 ** (pitch / 12)
    tone = np.sin(TWO_PI * 1900 * k * t) * np.exp(-t * 90) + 0.4 * np.sin(TWO_PI * 3100 * k * t) * np.exp(-t * 140)
    noise = _band_filter(rng.normal(0, 1, len(t)), 2000, 7000) * np.exp(-t * 400) * 0.5
    return (tone + noise) * np.minimum(1, t / 0.0008)


def pop(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.14)
    k = 2 ** (pitch / 12)
    f = (420 + 520 * (1 - np.exp(-t * 60))) * k
    body = np.sin(TWO_PI * np.cumsum(f) / SR) * _env(len(t), 0.002, 30)
    top = click(rng, pitch + 5)
    body[: len(top)] += 0.3 * top[: len(body)]
    return body


def tick(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.035)
    k = 2 ** (pitch / 12)
    return np.sin(TWO_PI * 2600 * k * t) * np.exp(-t * 160) * np.minimum(1, t / 0.0005)


def thud(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.7)
    k = 2 ** (pitch / 12)
    f = (42 + 85 * np.exp(-t * 14)) * k
    body = np.sin(TWO_PI * np.cumsum(f) / SR) * np.exp(-t * 5.5)
    knock = _band_filter(rng.normal(0, 1, len(t)), 150, 1200) * np.exp(-t * 45) * 0.35
    return (body + knock) * np.minimum(1, t / 0.002)


def riser(rng: np.random.Generator, pitch: float, d: float) -> np.ndarray:
    n = int(d * SR)
    t = np.arange(n) / SR
    noise = _sweep_noise(rng, d, 500, 6000)
    f = 180 * 2 ** (pitch / 12) * 2 ** (2.0 * t / d)          # two octaves up
    tone = np.sin(TWO_PI * np.cumsum(f) / SR) + 0.3 * np.sin(TWO_PI * np.cumsum(f * 1.5) / SR)
    shape = (t / d) ** 2.2
    return (noise * 0.8 + tone * 0.35) * shape


def hit(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(1.4)
    k = 2 ** (pitch / 12)
    low = thud(rng, pitch)
    out = np.zeros(len(t))
    out[: len(low)] += low
    stab = np.zeros(len(t))
    for semi in (0, 3, 7, 12):                                   # a minor stab, detuned pairs, quickly closing
        fr = 220 * k * 2 ** (semi / 12)
        for c in (-6, 6):
            stab += np.sin(TWO_PI * fr * 2 ** (c / 1200) * t) + 0.4 * np.sin(TWO_PI * 2 * fr * 2 ** (c / 1200) * t)
    stab = _band_filter(stab * np.exp(-t * 6), 120, 3500) * 0.12
    crack = _band_filter(rng.normal(0, 1, len(t)), 1500, 9000) * np.exp(-t * 35) * 0.45
    return (out + stab + crack) * np.minimum(1, t / 0.0015)


def ding(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.7)
    f = 1568 * 2 ** (pitch / 12)                                 # G6
    x = (np.sin(TWO_PI * f * t) * np.exp(-t * 6) + 0.35 * np.sin(TWO_PI * f * 2.76 * t) * np.exp(-t * 11)
         + 0.12 * np.sin(TWO_PI * f * 5.4 * t) * np.exp(-t * 18))
    return x * np.minimum(1, t / 0.001)


def drip(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.12)
    f = (900 + 1300 * (1 - np.exp(-t * 45))) * 2 ** (pitch / 12)
    return np.sin(TWO_PI * np.cumsum(f) / SR) * _env(len(t), 0.003, 28)


def whoosh(rng: np.random.Generator, pitch: float, d: float) -> np.ndarray:
    n = int(d * SR)
    shape = np.sin(np.linspace(0, np.pi, n)) ** 1.6
    return _sweep_noise(rng, d, 350 * 2 ** (pitch / 12), 2600 * 2 ** (pitch / 12), q=0.9) * shape


def swell(rng: np.random.Generator, pitch: float, d: float) -> np.ndarray:
    t = _t(d)
    k = 2 ** (pitch / 12)
    x = np.zeros(len(t))
    for semi in (0, 7, 12, 16):                                  # an open major voicing
        for c in (-8, 0, 7):
            x += np.sin(TWO_PI * 293.66 * k * 2 ** (semi / 12) * 2 ** (c / 1200) * t + semi)
    shape = np.sin(np.pi * np.clip(t / d, 0, 1)) ** 2
    return _band_filter(x, 150, 4000) * shape


def stinger(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(2.2)
    k = 2 ** (pitch / 12)
    x = np.zeros(len(t))
    for semi, a in ((0, 1.0), (7, 0.8), (12, 0.7), (15, 0.55), (19, 0.4)):     # minor add9-ish, bright top
        fr = 293.66 * k * 2 ** (semi / 12)
        x += a * synth.soft_pluck(fr, 2.2)[: len(t)]
    bell = ding(rng, pitch + 12)
    x[: len(bell)] += 0.5 * bell
    sub = np.sin(TWO_PI * 73.4 * k * t) * np.exp(-t * 3.5) * 0.6
    return (x + sub) * np.minimum(1, t / 0.002)


def deny(rng: np.random.Generator, pitch: float) -> np.ndarray:
    t = _t(0.22)
    k = 2 ** (pitch / 12)
    f = np.where(t < 0.1, 330, 247) * k
    sq = np.sign(np.sin(TWO_PI * np.cumsum(f) / SR))
    return _band_filter(sq, 200, 2400) * _env(len(t), 0.003, 9)


_MAKERS = {"click": click, "pop": pop, "tick": tick, "thud": thud, "hit": hit, "ding": ding, "drip": drip,
           "stinger": stinger, "deny": deny}
_TIMED = {"riser": riser, "whoosh": whoosh, "swell": swell}


def render_sound(sound: str, seed: int = 0, pitch: float = 0.0, seconds: float | None = None) -> np.ndarray:
    """One effect at its designed level (LEVELS), peak-normalised first; deterministic for (sound, seed, pitch)."""
    if sound not in LEVELS:
        raise ValueError(f"unknown sound {sound!r}; choose from {', '.join(SOUNDS)}")
    rng = np.random.default_rng([seed, SOUNDS.index(sound)])
    if sound in _TIMED:
        x = _TIMED[sound](rng, pitch, float(seconds or DEFAULT_SECONDS[sound]))
    else:
        x = _MAKERS[sound](rng, pitch)
    x = np.asarray(x, dtype=np.float64)
    x = x / (np.abs(x).max() + 1e-9) * LEVELS[sound]
    return _fade_tail(x)


def cue_track(cues: list[Cue], seconds: float, seed: int = 0, reverb: float = REVERB_WET) -> np.ndarray:
    """Every sounding cue mixed into one mono float32 track of `int(SR * seconds)` samples, with a short room
    reverb on the bus. `hush` cues make no sound (see `hush_gain`)."""
    n = int(SR * seconds)
    out = np.zeros(n + SR * 3)
    for k, c in enumerate(sorted(cues, key=lambda c: (c.t, c.sound))):
        if c.sound == "hush" or c.t >= seconds:
            continue
        x = render_sound(c.sound, seed + k, c.pitch, c.seconds) * c.gain
        i = int(round(c.t * SR))
        out[i:i + len(x)] += x[: len(out) - i]
    if reverb > 0 and np.any(out):
        rv = synth.Reverb(np.random.default_rng([seed, 99]), seconds=1.1, decay=5.0)
        out = out + rv.process(out.astype(np.float32)) * reverb
    return out[:n].astype(np.float32)


HUSH_FADE = 0.08
HUSH_FLOOR = 0.04   # about -28 dB: the bed is gone but nothing clicks


def hush_gain(cues: list[Cue], start: int, n: int) -> np.ndarray:
    """Per-sample music gain for samples start..start+n: HUSH_FLOOR inside each hush, with HUSH_FADE ramps."""
    g = np.ones(n, dtype=np.float32)
    for c in cues:
        if c.sound != "hush":
            continue
        a, b = c.t, c.t + float(c.seconds or DEFAULT_SECONDS["hush"])
        i0 = max(0, int(np.floor((a - HUSH_FADE) * SR)) - start)        # only the samples this hush can touch
        i1 = min(n, int(np.ceil((b + HUSH_FADE) * SR)) + 1 - start)
        if i1 <= i0:
            continue
        t = (start + np.arange(i0, i1)) / SR
        down = np.clip((t - (a - HUSH_FADE)) / HUSH_FADE, 0, 1)
        up = np.clip((b + HUSH_FADE - t) / HUSH_FADE, 0, 1)
        g[i0:i1] = np.minimum(g[i0:i1], (1 - (1 - HUSH_FLOOR) * np.minimum(down, up)).astype(np.float32))
    return g
