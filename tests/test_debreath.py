"""voiceover.debreath: silence quiet unvoiced runs (breaths) in place."""
import time

import numpy as np
import pytest

from yamlreel import voice as voice_mod
from yamlreel.specs import SpecError, validate
from yamlreel.voice import SAMPLE_RATE

# a run may reach one analysis window into neighbouring speech at its start, then fades
FADE = int((voice_mod.DEBREATH_FADE_MS / 1000 + voice_mod.DEBREATH_WIN) * SAMPLE_RATE)


def test_speech_right_after_a_breath_keeps_its_first_samples():
    # the run ends with the last breath frame: nothing after that frame is zeroed, only faded
    a, n = tone(1.0), int(0.2 * SAMPLE_RATE)
    clip = np.concatenate([a, noise(0.2), tone(1.0)])
    out = voice_mod.debreath(clip)
    end = len(a) + n
    fade = int(voice_mod.DEBREATH_FADE_MS / 1000 * SAMPLE_RATE)
    gap = int(0.003 * SAMPLE_RATE)   # the breath frames may end up to one hop before the tone starts
    assert np.abs(out[end + gap:end + fade]).max() > 0.0
    assert np.array_equal(out[end + fade:], clip[end + fade:])


def tone(seconds, amp=0.3, hz=220):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (np.sin(2 * np.pi * hz * t) * amp).astype(np.float32)


def noise(seconds, amp=0.01, seed=0):
    """Breath-like noise: white noise low-passed to a spectral centroid near 2 kHz (as measured on real breaths in
    cloned-voice clips), at the same RMS as before."""
    rng = np.random.default_rng(seed)
    w = rng.standard_normal(int(seconds * SAMPLE_RATE) + 7)
    x = np.convolve(w, np.ones(8) / 8, "valid")[: int(seconds * SAMPLE_RATE)]
    return (x / (x.std() + 1e-12) * amp).astype(np.float32)


def secs(a):
    return len(a) / SAMPLE_RATE


def clip_with(mid):
    a, b = tone(1.0), tone(1.0)
    return np.concatenate([a, mid, b]), len(a), len(mid)


def test_breath_between_tones_is_silenced_and_tones_untouched():
    clip, s, n = clip_with(noise(0.2))
    out = voice_mod.debreath(clip)
    assert np.array_equal(out[: s - FADE], clip[: s - FADE])
    assert np.array_equal(out[s + n + FADE:], clip[s + n + FADE:])
    inner = out[s + int(0.04 * SAMPLE_RATE): s + n - int(0.04 * SAMPLE_RATE)]
    assert np.abs(inner).max() == 0.0


def test_short_burst_is_kept():
    clip, *_ = clip_with(noise(0.06))
    assert np.array_equal(voice_mod.debreath(clip), clip)


def test_quiet_voiced_tone_is_kept():
    clip, *_ = clip_with(tone(0.3, amp=0.01))
    assert np.array_equal(voice_mod.debreath(clip), clip)


def test_loud_noise_is_kept():
    clip, *_ = clip_with(noise(0.2, amp=0.2))
    assert np.array_equal(voice_mod.debreath(clip), clip)


def test_clip_without_breath_is_equal():
    clip = tone(1.0)
    assert np.array_equal(voice_mod.debreath(clip), clip)


@pytest.mark.parametrize("clip", [np.zeros(0, np.float32), np.zeros(100, np.float32), np.zeros(SAMPLE_RATE, np.float32)])
def test_empty_short_and_silent_clips_are_returned(clip):
    assert np.array_equal(voice_mod.debreath(clip), clip)


def test_length_never_changes():
    clip, *_ = clip_with(noise(0.25))
    assert len(voice_mod.debreath(clip)) == len(clip)


def test_leading_breath_is_silenced():
    clip = np.concatenate([noise(0.2), tone(1.0)])
    out = voice_mod.debreath(clip)
    assert np.abs(out[: int(0.15 * SAMPLE_RATE)]).max() == 0.0
    assert np.array_equal(out[int(0.3 * SAMPLE_RATE):], clip[int(0.3 * SAMPLE_RATE):])


def test_process_clip_order_debreath_then_trim():
    clip = np.concatenate([noise(0.3), tone(1.0)])
    plain = voice_mod.process_clip(clip, {})
    only_debreath = voice_mod.process_clip(clip, {"debreath": True})
    both = voice_mod.process_clip(clip, {"debreath": True, "trim": True})
    assert np.array_equal(plain, clip)  # debreath off leaves the breath
    assert len(only_debreath) == len(clip) and not np.array_equal(only_debreath, clip)
    assert secs(both) < secs(only_debreath) - 0.2
    assert secs(voice_mod.process_clip(clip, {"trim": True})) > secs(both) + 0.2  # trim alone misses the breath


def _spec(vo):
    return {"scenes": [{"type": "title", "title": "T"}], "voiceover": vo}


def test_spec_accepts_debreath_bool():
    assert validate(_spec({"debreath": True}))["voiceover"]["debreath"] is True


def test_spec_rejects_non_bool_debreath():
    with pytest.raises(SpecError, match="debreath"):
        validate(_spec({"debreath": "yes"}))


def test_unknown_voiceover_key_lists_debreath():
    with pytest.raises(SpecError, match="debreath"):
        validate(_spec({"debreth": True}))


def test_sixty_second_clip_is_fast():
    parts = []
    for k in range(40):
        parts += [tone(1.0, hz=180 + k), noise(0.2, seed=k), tone(0.3)]
    clip = np.concatenate(parts)[: 60 * SAMPLE_RATE]
    t0 = time.perf_counter()
    out = voice_mod.debreath(clip)
    assert time.perf_counter() - t0 < 2.0
    assert len(out) == len(clip) and not np.array_equal(out, clip)


# --- consonants survive (the "bucket" -> "bug", "key" -> dropped bug) ----------------------------------------------

def _hiss(seconds, amp, taps, seed=1):
    """Noise high-passed (`taps` > 0: white minus a `taps`-sample average) like s, sh or a t release."""
    rng = np.random.default_rng(seed)
    w = rng.standard_normal(int(seconds * SAMPLE_RATE) + taps)
    x = w[taps:] - np.convolve(w, np.ones(taps) / taps, "valid")[1:]
    return (x / x.std() * amp).astype(np.float32)


def test_a_word_final_consonant_burst_next_to_silence_survives():
    # vowel, then a 120 ms "t"/"s"-like release at -35 dBFS, then silence: unvoiced, quiet and long enough to have been
    # zeroed by the old thresholds; its centroid is far above a breath's
    clip = np.concatenate([tone(0.6), _hiss(0.12, 0.018, 4), np.zeros(int(0.4 * SAMPLE_RATE), np.float32), tone(0.6)])
    assert np.array_equal(voice_mod.debreath(clip), clip)


def test_a_soft_low_centroid_edge_next_to_a_vowel_survives():
    # a breathy vowel edge: low-passed noise (centroid well under 1.5 kHz) 18 dB under the speech, against the vowel
    rng = np.random.default_rng(3)
    w = rng.standard_normal(int(0.15 * SAMPLE_RATE) + 63)
    edge = np.convolve(w, np.ones(64) / 64, "valid")[: int(0.15 * SAMPLE_RATE)]
    edge = (edge / edge.std() * 0.027).astype(np.float32)
    clip = np.concatenate([np.zeros(int(0.3 * SAMPLE_RATE), np.float32), edge, tone(0.8)])
    assert np.array_equal(voice_mod.debreath(clip), clip)


def test_a_noise_only_breath_in_a_pause_is_removed():
    # silence, a 250 ms breath (centroid near 2 kHz, about 34 dB under the speech), silence, speech
    quiet = np.zeros(int(0.2 * SAMPLE_RATE), np.float32)
    clip = np.concatenate([tone(0.8), quiet, noise(0.25, amp=0.004), quiet, tone(0.8)])
    out = voice_mod.debreath(clip)
    s = int(1.0 * SAMPLE_RATE)
    assert np.abs(out[s + int(0.03 * SAMPLE_RATE): s + int(0.22 * SAMPLE_RATE)]).max() == 0.0
    assert np.array_equal(out[: int(0.8 * SAMPLE_RATE)], clip[: int(0.8 * SAMPLE_RATE)])
