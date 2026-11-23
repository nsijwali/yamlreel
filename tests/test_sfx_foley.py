"""Foley sound cues (sfx_foley.py): creak, spark, scribble, shutter, zip, chime."""
from __future__ import annotations

import numpy as np
import pytest

from yamlreel import sfx, sfx_foley
from yamlreel.specs import SpecError, validate

OLD_SOUNDS = ("click", "pop", "tick", "thud", "riser", "hit", "ding", "drip", "whoosh", "swell", "stinger", "deny", "hush")
MOTION = {"type": "motion", "seconds": 3, "elements": [{"kind": "text", "text": "hi"}]}


def centroid(x: np.ndarray) -> float:
    mag = np.abs(np.fft.rfft(x))
    f = np.fft.rfftfreq(len(x), 1 / sfx.SR)
    return float((f * mag).sum() / mag.sum())


def test_foley_sounds_are_registered_after_the_old_ones():
    assert sfx.SOUNDS[: len(OLD_SOUNDS)] == OLD_SOUNDS          # old seed indexes, so old audio, are unchanged
    assert sfx.SOUNDS[len(OLD_SOUNDS):] == sfx_foley.NAMES
    assert set(sfx_foley.NAMES) == set(sfx_foley.MAKERS) | set(sfx_foley.TIMED) == set(sfx_foley.LEVELS)


@pytest.mark.parametrize("sound", sfx_foley.NAMES)
def test_each_foley_sound_renders_at_its_level_and_follows_pitch(sound):
    a = sfx.render_sound(sound, seed=5)
    assert np.isfinite(a).all() and np.abs(a).max() == pytest.approx(sfx_foley.LEVELS[sound], rel=1e-6)
    assert not np.array_equal(a, sfx.render_sound(sound, seed=5, pitch=5))


def test_scribble_takes_a_length_and_others_have_fixed_ones():
    assert len(sfx.render_sound("scribble", seconds=1.5)) == int(1.5 * sfx.SR)
    assert len(sfx.render_sound("scribble")) == int(sfx_foley.DEFAULT_SECONDS["scribble"] * sfx.SR)
    assert len(sfx.render_sound("zip")) == int(0.28 * sfx.SR)


def test_sounds_sit_in_their_own_registers():
    spark, creak, chime = (centroid(sfx.render_sound(s, seed=1)) for s in ("spark", "creak", "chime"))
    assert spark > 4000 > creak          # crackle is bright, wood is mid/low
    assert 900 < chime < 3000            # the bell chord around C6


def test_spark_is_bursty_and_chime_rings_on():
    sp = np.abs(sfx.render_sound("spark", seed=2))
    hop = int(0.005 * sfx.SR)
    frames = sp[: len(sp) // hop * hop].reshape(-1, hop).max(1)
    assert (frames < 0.05 * frames.max()).mean() > 0.3         # mostly gaps between crackles
    ch = np.abs(sfx.render_sound("chime", seed=2))
    assert ch[int(1.0 * sfx.SR):].max() > 0.05 * ch.max()       # still audible after a second


def test_cue_track_places_a_foley_cue():
    x = sfx.cue_track([sfx.Cue(0.5, "shutter")], 1.5, reverb=0)
    first = int(np.flatnonzero(np.abs(x) > 1e-6)[0])
    assert abs(first - sfx.SR // 2) <= 2


def test_specs_accept_foley_cues_and_scribble_seconds():
    cues = [{"at": 0.2, "sound": "creak"}, {"at": 0.5, "sound": "scribble", "seconds": 1.2}, {"at": 1, "sound": "chime"}]
    spec = validate({"mix": True, "scenes": [{**MOTION, "sfx": cues}]})
    assert [c["sound"] for c in spec["scenes"][0]["sfx"]] == ["creak", "scribble", "chime"]


def test_specs_refuse_a_length_on_a_fixed_foley_sound():
    with pytest.raises(SpecError, match="seconds"):
        validate({"mix": True, "scenes": [{**MOTION, "sfx": [{"at": 0.2, "sound": "chime", "seconds": 1.0}]}]})
