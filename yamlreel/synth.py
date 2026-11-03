"""Small synthesis primitives for the music styles: numpy only, every function pure and seeded by its caller.

Everything returns float32 (or float64 for intermediate maths) mono at `SR`. Notes are rendered one at a time into
a short array, so a whole track is never held in memory; see `music.py` for the block renderer.
"""
from __future__ import annotations

import numpy as np

SR = 44100
TWO_PI = 2.0 * np.pi


def midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _t(dur: float) -> np.ndarray:
    return np.arange(max(1, int(SR * dur))) / SR


def fir_lowpass(x: np.ndarray, taps: int) -> np.ndarray:
    """Hann-windowed moving average. `taps` of 1 is a pass-through. Keeps the length of `x`."""
    if taps <= 1:
        return x
    w = np.hanning(taps + 2)[1:-1]
    return np.convolve(x, w / w.sum(), "same")


def partials(freq: float, dur: float, amps: list[float], decay: float, detune_cents: float = 0.0,
             inharmonicity: float = 0.0, attack: float = 0.005, decay_slope: float = 0.0) -> np.ndarray:
    """Additive tone: partial k at k*freq (stretched by `inharmonicity`), amplitude amps[k-1], exp decay that
    gets `decay_slope` faster per partial. Partials above 18 kHz are dropped."""
    t = _t(dur)
    out = np.zeros(len(t))
    detune = 2 ** (detune_cents / 1200)
    for k, a in enumerate(amps, start=1):
        f = k * freq * detune * np.sqrt(1 + inharmonicity * k * k)
        if f > 18000:
            break
        out += a * np.sin(TWO_PI * f * t + k) * np.exp(-t * (decay + decay_slope * (k - 1)))
    env = np.minimum(1.0, t / attack) if attack > 0 else 1.0
    return out * env


def pad(freq: float, dur: float, attack: float, release: float) -> np.ndarray:
    """Detuned additive pad voice: three slightly detuned stacks, slow attack and release."""
    total = dur + release
    t = _t(total)
    out = np.zeros(len(t))
    for cents in (-7.0, 0.0, 6.0):
        f0 = freq * 2 ** (cents / 1200)
        for k, a in ((1, 1.0), (2, 0.35), (3, 0.14), (4, 0.06)):
            out += a * np.sin(TWO_PI * f0 * k * t + k * cents)
    up = np.minimum(1.0, t / attack)
    down = np.minimum(1.0, np.maximum(total - t, 0) / release)
    return out / 3.0 * up * down


def bell(freq: float, dur: float) -> np.ndarray:
    t = _t(dur)
    out = np.zeros(len(t))
    for ratio, a, dec in ((1.0, 1.0, 1.6), (2.76, 0.4, 2.6), (5.4, 0.15, 4.0)):
        out += a * np.sin(TWO_PI * freq * ratio * t) * np.exp(-t * dec)
    return out * np.minimum(1.0, t / 0.004)


def sub(freq: float, dur: float) -> np.ndarray:
    t = _t(dur)
    return np.sin(TWO_PI * freq * t) * np.exp(-t * 3.0) * np.minimum(1.0, t / 0.01)


def epiano(freq: float, dur: float, vel: float) -> np.ndarray:
    """Two-operator FM tine: a quiet 1:1 carrier with a bright, fast-decaying 14:1 bell on top."""
    t = _t(dur)
    index = 1.4 * vel * np.exp(-t * 4.0) + 0.25
    body = np.sin(TWO_PI * freq * t + index * np.sin(TWO_PI * freq * t))
    tine = 0.18 * vel * np.sin(TWO_PI * freq * 14 * t) * np.exp(-t * 14.0) if freq * 14 < 18000 else 0.0
    return (body * np.exp(-t * 1.8) + tine) * np.minimum(1.0, t / 0.006)


def kick(dur: float = 0.42) -> np.ndarray:
    """Sine sweep from about 135 Hz down to 45 Hz, plus a few milliseconds of beater click."""
    t = _t(dur)
    freq = 45 + 90 * np.exp(-t * 28)
    phase = TWO_PI * np.cumsum(freq) / SR
    click = (np.sin(TWO_PI * 3200 * t) + np.sin(TWO_PI * 5100 * t)) * np.exp(-t * 180.0) * 0.9
    return np.sin(phase) * np.exp(-t * 9.0) + click


def snare(rng: np.random.Generator, dur: float = 0.22) -> np.ndarray:
    t = _t(dur)
    noise = rng.normal(0, 1, len(t))
    noise = noise - fir_lowpass(noise, 9)            # keep the top end
    return (0.55 * noise * np.exp(-t * 20.0) + 0.5 * np.sin(TWO_PI * 185 * t) * np.exp(-t * 26.0)) * 0.9


def hat(rng: np.random.Generator, dur: float = 0.06) -> np.ndarray:
    t = _t(dur)
    noise = rng.normal(0, 1, len(t))
    return (noise - fir_lowpass(noise, 5)) * np.exp(-t * 70.0) * 0.5


def soft_pluck(freq: float, dur: float) -> np.ndarray:
    """Plucked synth voice: a few 1/k harmonics with a bright attack, then FIR-lowpassed so it stays soft."""
    t = _t(dur)
    out = np.zeros(len(t))
    for k in range(1, 7):
        if freq * k > 12000:
            break
        out += np.sin(TWO_PI * freq * k * t) / k * np.exp(-t * (5.0 + 2.5 * k))
    out *= np.minimum(1.0, t / 0.004)
    return fir_lowpass(out, 7)


def crackle(rng: np.random.Generator, n: int, per_second: float) -> np.ndarray:
    """Sparse vinyl-style pops: random impulses with random sign, lightly smoothed."""
    hits = (rng.random(n) < per_second / SR) * rng.normal(0, 1, n)
    return fir_lowpass(hits, 3)


def air(rng: np.random.Generator, n: int) -> np.ndarray:
    """Filtered noise: a soft breath."""
    return fir_lowpass(rng.normal(0, 1, n), 60)


class Reverb:
    """Streaming reverb: FFT overlap-add convolution of consecutive blocks with a fixed noise-burst impulse
    response. State is only the last `len(ir) - 1` samples, so memory does not grow with the track."""

    def __init__(self, rng: np.random.Generator, seconds: float = 1.8, decay: float = 3.0, nfft: int = 1 << 17):
        n = int(SR * seconds)
        ir = rng.normal(0, 1, n) * np.exp(-np.arange(n) / SR * decay)
        ir[0] = 0.0
        ir /= np.sqrt(np.sum(ir ** 2))      # unit energy: the wet level does not depend on the length
        self._m = n - 1
        self._nfft = nfft
        self._step = nfft - self._m
        self._h = np.fft.rfft(ir, nfft)
        self._tail = np.zeros(self._m, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        """Convolve one block; returns as many samples as it was given. The reverb tail rolls into the next call."""
        y = np.zeros(len(x) + self._m, dtype=np.float32)
        for i in range(0, len(x), self._step):
            seg = x[i:i + self._step]
            part = np.fft.irfft(np.fft.rfft(seg, self._nfft) * self._h, self._nfft)[: len(seg) + self._m]
            y[i:i + len(part)] += part
        y[: self._m] += self._tail
        self._tail = y[len(x):].copy()
        return y[: len(x)]


def soft_clip(x: np.ndarray, drive: float, ceiling: float) -> np.ndarray:
    """Fixed-gain tanh limiter: no look at the whole track, so blocks can be processed one at a time."""
    return (np.tanh(x * drive) * ceiling).astype(np.float32)
