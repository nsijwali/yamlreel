"""Foley sound effects for scene cues, added to the `sfx.py` set: synthesised in numpy, no files, no licences.

    creak    wood under load (a crate on a rope bridge)        spark    electric crackle (a fan or a cable sparks)
    scribble a pen writing on paper (`seconds` sets the length) shutter  a camera shutter and flash
    zip      something fast shoots past (a card down a pipe)   chime    a calm resolved bell chord (the payoff)

Each maker takes (rng, pitch in semitones) and returns float samples at `SR`; `sfx.render_sound` peak-normalises
them to LEVELS. Every sound is deterministic for its (seed, pitch). The module only needs numpy, so `sfx.py` can
register it at import time without a cycle.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

SR = 44100            # must equal synth.SR (sfx.py asserts it when it registers these)
TWO_PI = 2.0 * np.pi
Maker = Callable[[np.random.Generator, float], np.ndarray]
TimedMaker = Callable[[np.random.Generator, float, float], np.ndarray]


def _t(d: float) -> np.ndarray:
    return np.arange(max(1, int(d * SR))) / SR


def _band(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Zero-phase band-pass by an FFT mask with second-order skirts (lo 0: low-pass, hi 0: high-pass)."""
    f = np.fft.rfftfreq(len(x), 1 / SR)
    g = np.ones_like(f)
    if lo > 0:
        g /= np.sqrt(1 + (lo / np.maximum(f, 1e-3)) ** 4)
    if hi > 0:
        g /= np.sqrt(1 + (f / hi) ** 4)
    return np.fft.irfft(np.fft.rfft(x) * g, len(x))


def _resonate(x: np.ndarray, freq: float, decay: float) -> np.ndarray:
    """x convolved with a short decaying sine (a body resonance), via FFT."""
    t = _t(min(0.12, 6.0 / decay))
    kernel = np.sin(TWO_PI * freq * t) * np.exp(-t * decay)
    n = len(x) + len(kernel) - 1
    y = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(kernel, n), n)
    return y[: len(x)]


def creak(rng: np.random.Generator, pitch: float) -> np.ndarray:
    """Stick-slip: an irregular train of tiny impulses (about 70 to 130 a second) through wooden body resonances."""
    d = 0.75
    t = _t(d)
    x = np.zeros(len(t))
    rate = (70 + 60 * np.sin(np.pi * t / d) ** 2) * 2 ** (pitch / 12)
    pos = 0.0
    while pos < d:
        i = int(pos * SR)
        x[i] = rng.uniform(0.5, 1.0)
        pos += 1.0 / rate[min(i, len(rate) - 1)] * rng.uniform(0.8, 1.25)
    body = _resonate(x, 620 * 2 ** (pitch / 12), 90) + 0.6 * _resonate(x, 1380 * 2 ** (pitch / 12), 140)
    shape = np.sin(np.pi * t / d) ** 0.7
    return _band(body, 180, 3200) * shape


def spark(rng: np.random.Generator, pitch: float) -> np.ndarray:
    """Four to six bright crackles in 0.4 s, each a few ms of high noise, plus a faint electric buzz."""
    t = _t(0.45)
    x = np.zeros(len(t))
    for _ in range(int(rng.integers(4, 7))):
        a = int(rng.uniform(0.0, 0.36) * SR)
        n = int(rng.uniform(0.004, 0.022) * SR)
        burst = rng.normal(0, 1, n) * np.exp(-np.arange(n) / SR * rng.uniform(150, 400)) * rng.uniform(0.5, 1.0)
        x[a:a + n] += burst[: len(x) - a]
    crack = _band(x, 2500 * 2 ** (pitch / 12), 12000)
    buzz = np.sign(np.sin(TWO_PI * 120 * t)) * np.exp(-t * 9) * 0.05
    return crack + _band(buzz, 300, 3000)


def scribble(rng: np.random.Generator, pitch: float, d: float) -> np.ndarray:
    """Paper noise shaped by quick strokes (6 to 10 a second), each a short rise and fall, with small gaps."""
    t = _t(d)
    paper = _band(rng.normal(0, 1, len(t)), 1800 * 2 ** (pitch / 12), 7000)
    env = np.zeros(len(t))
    pos = 0.0
    while pos < d:
        stroke = rng.uniform(0.07, 0.15)
        a, b = int(pos * SR), min(len(t), int((pos + stroke) * SR))
        if b > a:
            env[a:b] = np.sin(np.linspace(0, np.pi, b - a)) * rng.uniform(0.6, 1.0)
        pos += stroke + rng.uniform(0.01, 0.05)
    fade = np.minimum(1, np.minimum(t, d - t) / 0.02)
    return paper * env * fade


def shutter(rng: np.random.Generator, pitch: float) -> np.ndarray:
    """Two mechanical clicks 70 ms apart with a short bright flash tail."""
    t = _t(0.35)
    x = np.zeros(len(t))
    k = 2 ** (pitch / 12)
    for at, amp in ((0.0, 1.0), (0.07, 0.8)):
        i = int(at * SR)
        n = int(0.012 * SR)
        click = rng.normal(0, 1, n) * np.exp(-np.arange(n) / SR * 380)
        x[i:i + n] += amp * click
    clicks = _band(x, 900 * k, 9000) + 0.5 * _resonate(x, 2300 * k, 260)
    flash = _band(rng.normal(0, 1, len(t)), 4000, 14000) * np.exp(-np.maximum(t - 0.07, 0) * 22) * (t >= 0.07) * 0.2
    return clicks + flash


def zip_(rng: np.random.Generator, pitch: float) -> np.ndarray:
    """A fast rising swish (noise band and a thin tone, 400 Hz to 2.4 kHz in 0.28 s)."""
    d = 0.28
    t = _t(d)
    k = 2 ** (pitch / 12)
    f = 400 * k * 6 ** (t / d)
    tone = np.sin(TWO_PI * np.cumsum(f) / SR) * 0.25
    noise = rng.normal(0, 1, len(t))
    hi = _band(noise, 2000 * k, 9000) * (t / d)
    lo = _band(noise, 500 * k, 2000 * k) * (1 - t / d)
    shape = np.sin(np.pi * t / d) ** 1.2
    return (hi + lo + tone) * shape


def chime(rng: np.random.Generator, pitch: float) -> np.ndarray:
    """A soft major chord rolled upward (60 ms apart), harmonic partials and a long gentle decay."""
    t = _t(2.4)
    k = 2 ** (pitch / 12)
    x = np.zeros(len(t))
    for n, semi in enumerate((0, 4, 7, 12)):                     # C6 major, rolled
        i = int(n * 0.06 * SR)
        tt = t[: len(t) - i]
        f = 1046.5 * k * 2 ** (semi / 12)
        note = (np.sin(TWO_PI * f * tt) + 0.25 * np.sin(TWO_PI * 2 * f * tt) * np.exp(-tt * 3)
                + 0.08 * np.sin(TWO_PI * 3 * f * tt) * np.exp(-tt * 6))
        x[i:] += note * np.exp(-tt * 1.9) * np.minimum(1, tt / 0.006) * (1 - 0.12 * n)
    return x


MAKERS: dict[str, Maker] = {"creak": creak, "spark": spark, "shutter": shutter, "zip": zip_, "chime": chime}
TIMED: dict[str, TimedMaker] = {"scribble": scribble}
DEFAULT_SECONDS: dict[str, float] = {"scribble": 0.8}
LEVELS: dict[str, float] = {"creak": 0.30, "spark": 0.22, "scribble": 0.16, "shutter": 0.30, "zip": 0.24, "chime": 0.26}
NAMES: tuple[str, ...] = ("creak", "spark", "scribble", "shutter", "zip", "chime")
