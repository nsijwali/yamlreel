"""voiceover.debreath: silence quiet unvoiced runs (breaths) in place."""
import time

import numpy as np
import pytest

from yamlreel import voice as voice_mod
from yamlreel.specs import SpecError, validate
from yamlreel.voice import SAMPLE_RATE

# a run may reach one analysis window into neighbouring speech, then fades
FADE = int((voice_mod.DEBREATH_FADE_MS / 1000 + voice_mod.DEBREATH_WIN) * SAMPLE_RATE)


def tone(seconds, amp=0.3, hz=220):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (np.sin(2 * np.pi * hz * t) * amp).astype(np.float32)


def noise(seconds, amp=0.01, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SAMPLE_RATE)) * amp).astype(np.float32)


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
