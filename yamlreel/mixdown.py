"""The `mix:` render path: one mastered track from narration, music and scene sound effects.

Used only when a spec has a `mix:` block; without it render.py mixes exactly as before. Steps:

1. voice chain (ffmpeg): high-pass, a small low-mid dip, a presence lift and gentle compression, so the voice reads
   clearly over music on a phone speaker;
2. music: the spec's volume and fades (as the classic path), a "voice pocket" EQ (low shelf down, a dip where the
   voice lives), and the `hush` cues of the scenes (near-silences before key moments);
3. effects: the scenes' `sfx` cues (sfx.cue_track), on their own bus: never ducked;
4. master: measure the integrated loudness of the sum (EBU R128, ffmpeg ebur128), set the gain to the target, then a
   true-peak style limiter on a 4x oversampled signal; measured again and corrected once.

Every step is deterministic. The result is a mono 44.1 kHz WAV and the loudness it measured.
"""
from __future__ import annotations

import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import proclife
from . import sfx as sfx_mod

SR = 44100
DEFAULT_LOUDNESS = -14.0
DEFAULT_TRUE_PEAK = -1.5
VOICE_CHAIN = ("highpass=f=85:poles=2,equalizer=f=240:t=q:w=1.1:g=-2.5,equalizer=f=3600:t=q:w=1.0:g=3,"
               "equalizer=f=9500:t=q:w=0.8:g=1.5,acompressor=threshold=-21dB:ratio=2.6:attack=6:release=110:makeup=1.5")
POCKET = ((120.0, -3.0), (2800.0, -2.5))   # (Hz, dB): a low shelf under the first, a wide dip at the second
HEADROOM = 0.9           # the unmastered sum is scaled to at most this peak before it is written (never clipped)
MAX_MIX_SECONDS = 600.0  # the mix path holds whole tracks in memory: longer videos are refused
SILENT_LUFS = -69.5      # ffmpeg reports digital silence as about -70 LUFS


class MixError(RuntimeError):
    pass


@dataclass
class Loudness:
    lufs: float
    true_peak: float


def _run(cmd: list[str], what: str, timeout: float = proclife.Deadlines.subprocess) -> subprocess.CompletedProcess:
    try:
        done = proclife.run(cmd, timeout, what)
    except proclife.ProcessTimeout as e:
        raise MixError(str(e)) from e
    if done.returncode != 0:
        raise MixError(f"ffmpeg failed while {what}: {done.stderr.decode(errors='replace')[-400:]}")
    return done


def read_wav(path: Path) -> np.ndarray:
    """A 16 bit 44.1 kHz WAV as mono float32. Anything else is a MixError (render decodes other files first)."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2 or w.getframerate() != SR:
                raise MixError(f"{path.name}: expected 16 bit {SR} Hz WAV")
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32767
            return x.reshape(-1, w.getnchannels()).mean(axis=1) if w.getnchannels() > 1 else x
    except (wave.Error, EOFError, OSError) as e:
        raise MixError(f"{path.name}: cannot read it as a WAV file: {e}") from e


def write_wav(path: Path, x: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def measure(ffmpeg: str, path: Path, timeout: float = proclife.Deadlines.subprocess) -> Loudness:
    try:
        err = proclife.run([ffmpeg, "-hide_banner", "-nostats", "-protocol_whitelist", "file", "-i", str(path),
                            "-af", "ebur128=peak=true", "-f", "null", "-"], timeout, "measuring loudness"
                           ).stderr.decode(errors="replace")
    except proclife.ProcessTimeout as e:
        raise MixError(str(e)) from e
    tail = err.rsplit("Summary:", 1)[-1]
    i = re.search(r"I:\s+(-?(?:inf|[\d.]+)) LUFS", tail)
    p = re.search(r"Peak:\s+(-?(?:inf|[\d.]+)) dBFS", tail)
    if not i or not p:
        raise MixError("could not read the loudness of the mix")
    return Loudness(float(i[1]), float(p[1]))


def _shelf_eq(x: np.ndarray, bands: tuple[tuple[float, float], ...]) -> np.ndarray:
    """Static zero-phase EQ by an FFT gain curve: a low shelf (first band) and a wide bell (second)."""
    n = len(x)
    if n == 0:
        return x
    f = np.fft.rfftfreq(n, 1 / SR)
    (f_shelf, g_shelf), (f_bell, g_bell) = bands
    shelf = 10 ** (g_shelf / 20) + (1 - 10 ** (g_shelf / 20)) / (1 + (f_shelf / np.maximum(f, 1e-3)) ** 4)
    bell = 1 + (10 ** (g_bell / 20) - 1) * np.exp(-0.5 * (np.log2(np.maximum(f, 1e-3) / f_bell) / 0.9) ** 2)
    return np.fft.irfft(np.fft.rfft(x) * shelf * bell, n).astype(np.float32)


def _fit(x: np.ndarray, n: int) -> np.ndarray:
    return x[:n] if len(x) >= n else np.pad(x, (0, n - len(x)))


def master(work: Path, ffmpeg: str, total: float, narration: Path | None, music: Path | None,
           cues: list[sfx_mod.Cue], opts: dict[str, Any], music_volume: float, music_level: float,
           seed: int = 0, timeout: float = proclife.Deadlines.subprocess) -> tuple[Path, dict[str, float]]:
    """The mastered track for `total` seconds, and its numbers (loudness and true peak before and after)."""
    if total > MAX_MIX_SECONDS:
        raise MixError(f"the video is {total:.0f}s; the mix path handles at most {MAX_MIX_SECONDS:g}s (MAX_MIX_SECONDS)")
    n = int(round(total * SR))
    target = float(opts.get("loudness", DEFAULT_LOUDNESS))
    ceiling = float(opts.get("true_peak", DEFAULT_TRUE_PEAK))
    mix = np.zeros(n, dtype=np.float32)
    if narration is not None:
        voice_path = narration
        if opts.get("voice_chain", True):
            voice_path = work / "voice-chain.wav"
            _run([ffmpeg, "-loglevel", "error", "-y", "-protocol_whitelist", "file", "-i", str(narration),
                  "-af", VOICE_CHAIN, "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(voice_path)], "shaping the voice",
                 timeout)
        mix += _fit(read_wav(voice_path), n)
    if music is not None:
        m = _fit(read_wav(music), n) * np.float32(music_volume * music_level)
        t = np.arange(n) / SR
        m *= np.clip(t / 0.8, 0, 1).astype(np.float32)                                   # the classic fades
        m *= np.clip((total - t) / 2.0, 0, 1).astype(np.float32)
        m = _shelf_eq(m, POCKET)
        m *= sfx_mod.hush_gain(cues, 0, n)
        mix += m * np.float32(10 ** (float(opts.get("music_db", 0.0)) / 20))
    if any(c.sound != "hush" for c in cues):
        mix += sfx_mod.cue_track(cues, total, seed) * np.float32(10 ** (float(opts.get("sfx_db", 0.0)) / 20))
    peak = float(np.abs(mix).max()) if n else 0.0
    if peak > HEADROOM:          # hot cues or +dB settings: scale down instead of clipping; the loudness gain restores it
        mix *= np.float32(HEADROOM / peak)
    raw = write_wav(work / "mix-raw.wav", mix)
    before = measure(ffmpeg, raw, timeout)
    if before.true_peak == float("-inf") or before.lufs <= SILENT_LUFS:
        # silence stays silence: no gain (ffmpeg calls it about -70 LUFS, which would ask for +56 dB)
        return raw, {"lufs_before": before.lufs, "tp_before": before.true_peak, "lufs": before.lufs,
                     "tp": before.true_peak, "gain_db": 0.0, "note": "silent: left unmastered"}
    lim = 10 ** (ceiling / 20)
    out = work / "mix-master.wav"
    gain = target - before.lufs
    after = before
    for _ in range(2):    # the limiter takes a little loudness off the peaks: measure and correct once
        _run([ffmpeg, "-loglevel", "error", "-y", "-protocol_whitelist", "file", "-i", str(raw), "-af",
              f"volume={gain:.3f}dB,aresample={SR * 4},alimiter=limit={lim:.5f}:attack=1.5:release=60:level=disabled,"
              f"aresample={SR}", "-ac", "1", "-c:a", "pcm_s16le", str(out)], "mastering", timeout)
        after = measure(ffmpeg, out, timeout)
        if abs(after.lufs - target) <= 0.3:
            break
        gain += target - after.lufs
    return out, {"lufs_before": before.lufs, "tp_before": before.true_peak, "lufs": after.lufs, "tp": after.true_peak,
                 "gain_db": gain}
