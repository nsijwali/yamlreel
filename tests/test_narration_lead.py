"""`voiceover.lead`: when a scene's narration starts (narration_lead.py)."""
import numpy as np
import pytest

from fakes import _fake_voice_server
from yamlreel import subtitles as subs
from yamlreel import voice as voice_mod
from yamlreel.config import Config
from yamlreel.narration_lead import DEFAULT_LEAD, MAX_LEAD, narration_lead
from yamlreel.render import Runtime, render, speech_regions
from yamlreel.specs import SpecError, validate
from yamlreel.voice import SAMPLE_RATE, VoiceServer


def _spec(lead=None, narrate=True, **vo):
    if lead is not None:
        vo["lead"] = lead
    scenes = [{"type": "title", "title": f"T{n}", "seconds": 1.0, **({"narration": f"Line {n}"} if narrate else {})}
              for n in range(2)]
    return validate({"scenes": scenes, "music": {"style": "none", "snap": False}, "voiceover": vo}, Config())


def test_default_is_the_old_fixed_lead():
    assert DEFAULT_LEAD == voice_mod.NARRATION_LEAD == 0.3
    assert narration_lead(_spec()) == DEFAULT_LEAD
    assert narration_lead({"scenes": []}) == DEFAULT_LEAD     # no voiceover block at all


@pytest.mark.parametrize("lead", [0, 0.05, MAX_LEAD])
def test_valid_leads_are_kept(lead):
    assert narration_lead(_spec(lead)) == pytest.approx(lead)


@pytest.mark.parametrize("lead", [-0.1, MAX_LEAD + 0.01, "soon", True])
def test_bad_leads_are_refused(lead):
    with pytest.raises(SpecError, match="lead"):
        _spec(lead)


def test_scene_floor_uses_the_lead():
    clip = {0: np.zeros(SAMPLE_RATE, dtype=np.float32)}
    assert voice_mod.scene_floors(_spec(0, gap=0.2), clip)[0] == pytest.approx(1.2)    # gap wins over lead 0
    assert voice_mod.scene_floors(_spec(0.8, gap=0.2), clip)[0] == pytest.approx(1.8)  # lead wins over the gap
    assert voice_mod.min_scene_seconds(clip[0], 0.0, 0.0) == pytest.approx(1.0)


def test_speech_regions_and_track_follow_the_lead():
    clip = np.full(SAMPLE_RATE // 2, 0.5, dtype=np.float32)
    assert speech_regions({1: clip}, [0.0, 2.0], 0.0) == [(2.0, 2.5)]
    track = voice_mod.build_track({0: clip}, [0.0], 1.0, lead=0.0)
    assert np.flatnonzero(track)[0] == 0


def test_subtitles_and_caption_follow_the_lead():
    words = [subs.Word("one", 0.0, 0.3), subs.Word("two", 0.35, 0.6)]
    assert subs.build_cues(words, 2, lead=0.0)[0].start == pytest.approx(0.0)
    sw = subs.SceneWords(tuple(words), 0.6, "file")
    assert subs.caption_start(sw, None, 0.0) == pytest.approx(0.6 + subs.CAPTION_GAP)
    assert subs.caption_start(sw) == pytest.approx(DEFAULT_LEAD + 0.6 + subs.CAPTION_GAP)   # unchanged default


def test_a_render_with_lead_zero_speaks_on_the_first_sample(tmp_path, monkeypatch):
    srv, calls, url = _fake_voice_server(seconds=0.4, dc=0.5)
    tracks: list[np.ndarray] = []
    real_write = voice_mod.write_wav

    def spy_write(path, samples):
        if path.name == "narration.wav":
            tracks.append(np.array(samples))
        return real_write(path, samples)

    monkeypatch.setattr(voice_mod, "write_wav", spy_write)
    try:
        render(_spec(0, gap=0.2), "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
               runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
    finally:
        srv.shutdown()
    assert len(tracks) == 1
    nz = np.flatnonzero(tracks[0])
    assert nz[0] == 0                                          # first scene: speech from sample 0
    second = nz[np.flatnonzero(np.diff(nz) > 1)[0] + 1]
    assert second == pytest.approx(1.0 * SAMPLE_RATE, abs=2)   # second scene starts at 1.0 s, speech with it
