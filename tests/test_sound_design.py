"""Scene sound cues (`sfx:`), the mastered `mix:` path, the `drive` music style and the `studio` theme."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from yamlreel import music, sfx
from yamlreel.render import ffmpeg_exe, render, scene_cues
from yamlreel.scenebase import Ctx, studio_background
from yamlreel.specs import SpecError, validate
from yamlreel.theme import THEMES, font, use_faces

needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def no_personal_config(tmp_path_factory, monkeypatch):
    empty = tmp_path_factory.mktemp("no-config")
    for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(empty / "config.yaml"))
    monkeypatch.chdir(empty)


MOTION = {"type": "motion", "seconds": 2, "elements": [{"kind": "text", "text": "hi"}]}


# --- sfx ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sound", [s for s in sfx.SOUNDS if s != "hush"])
def test_every_sound_is_deterministic_at_its_level_and_ends_quietly(sound):
    a = sfx.render_sound(sound, seed=3)
    assert np.array_equal(a, sfx.render_sound(sound, seed=3))
    assert np.abs(a).max() == pytest.approx(sfx.LEVELS[sound], rel=1e-6)
    assert abs(a[-1]) < 1e-3                       # the tail fade: no click when a sound ends
    assert np.isfinite(a).all()


def test_unknown_sound_is_refused():
    with pytest.raises(ValueError, match="unknown sound"):
        sfx.render_sound("boing")


def test_timed_sounds_take_their_length():
    assert len(sfx.render_sound("riser", seconds=0.5)) == int(0.5 * sfx.SR)
    assert len(sfx.render_sound("riser")) == int(sfx.DEFAULT_SECONDS["riser"] * sfx.SR)


def test_cue_track_places_a_cue_at_its_sample_and_is_silent_before_it():
    x = sfx.cue_track([sfx.Cue(1.0, "tick")], 2.0, reverb=0)
    assert len(x) == 2 * sfx.SR
    first = int(np.flatnonzero(np.abs(x) > 1e-6)[0])
    assert abs(first - sfx.SR) <= 2


def test_cue_track_without_sounding_cues_is_silent_and_hush_makes_no_sound():
    assert not np.any(sfx.cue_track([sfx.Cue(0.5, "hush", seconds=0.4)], 1.0))
    assert not np.any(sfx.cue_track([], 1.0))


def test_hush_gain_drops_to_the_floor_inside_and_is_one_outside():
    g = sfx.hush_gain([sfx.Cue(1.0, "hush", seconds=0.5)], 0, 2 * sfx.SR)
    assert g[int(0.5 * sfx.SR)] == pytest.approx(1.0)
    assert g[int(1.25 * sfx.SR)] == pytest.approx(sfx.HUSH_FLOOR)
    assert g[int(1.9 * sfx.SR)] == pytest.approx(1.0)
    assert np.all(np.abs(np.diff(g)) < 0.01)      # ramps, never a step


# --- validation ----------------------------------------------------------------------------------------------

def test_scene_sfx_is_validated():
    spec = validate({"mix": True, "scenes": [{**MOTION, "sfx": [{"at": 0.5, "sound": "riser", "seconds": 1.0, "gain": 0.5, "pitch": 2}]}]})
    assert spec["scenes"][0]["sfx"][0] == {"at": 0.5, "sound": "riser", "seconds": 1.0, "gain": 0.5, "pitch": 2.0}


@pytest.mark.parametrize("bad, msg", [
    ([{"at": 0.5, "sound": "boing"}], "sound"),
    ([{"sound": "tick"}], "needs `at` and `sound`"),
    ([{"at": 0.5, "sound": "tick", "seconds": 1}], "take a length"),
    ([{"at": 0.5, "sound": "tick", "gain": 3}], "gain"),
    ([{"at": 0.5, "sound": "tick", "loud": 1}], "loud"),
    ([], "at least 1"),
    ({"at": 1}, "list"),
])
def test_bad_scene_sfx_is_refused(bad, msg):
    with pytest.raises(SpecError, match=msg):
        validate({"scenes": [{**MOTION, "sfx": bad}]})


def test_mix_is_validated():
    assert validate({"scenes": [MOTION], "mix": True})["mix"] == {}
    spec = validate({"scenes": [MOTION], "mix": {"loudness": -16, "true_peak": -1, "music_db": -2, "voice_chain": False}})
    assert spec["mix"] == {"loudness": -16.0, "true_peak": -1.0, "music_db": -2.0, "voice_chain": False}
    assert "mix" not in validate({"scenes": [MOTION]})
    for bad in ({"loudness": -2}, {"true_peak": 1}, {"loud": -14}, "loud"):
        with pytest.raises(SpecError):
            validate({"scenes": [MOTION], "mix": bad})


def test_scene_cues_land_on_frames_of_the_video_clock():
    spec = validate({"mix": True, "scenes": [MOTION, {**MOTION, "sfx": [{"at": 0.51, "sound": "pop"}]}]})
    (cue,) = scene_cues(spec, [0.0, 2.0], 30)
    assert cue.t == pytest.approx(round(2.51 * 30) / 30)
    assert cue.sound == "pop"


# --- music -------------------------------------------------------------------------------------------------

def test_drive_pumps_on_the_beat_and_keeps_the_sub_under_the_low_mids():
    x = music.generate("drive", 12, 104, "D", "minor", 5)
    assert len(x) == int(12 * music.SR) and np.abs(x).max() < 1.0
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / music.SR)
    sub, lowmid = spec[(f >= 20) & (f < 120)].sum(), spec[(f >= 120) & (f < 500)].sum()
    assert 10 * np.log10(sub / lowmid) < -2.0
    g = music._pump(0, music.SR, 60 / 104)
    assert g.min() == pytest.approx(1 - music.PUMP_DEPTH, abs=0.02) and g.max() <= 1.0


# --- theme ---------------------------------------------------------------------------------------------------

def test_studio_background_is_a_gradient_and_classic_themes_keep_no_faces(tmp_path):
    img = np.asarray(studio_background(THEMES["studio"], 108, 192)).astype(int)
    assert img[5, 54].sum() > img[186, 54].sum()            # lighter at the top than the bottom
    assert all(THEMES[n].bg2 is None and THEMES[n].faces is None for n in ("sumi", "washi", "midnight", "editorial"))
    Ctx(108, 192, THEMES["studio"], tmp_path, tmp_path)
    Ctx(108, 192, THEMES["midnight"], tmp_path, tmp_path)     # a classic theme resets the faces
    assert font("sans_bold", 20).getname()[0].startswith("DejaVu")


def test_a_missing_face_falls_back_to_dejavu(monkeypatch):
    from yamlreel import theme
    monkeypatch.setitem(theme.FACE_SETS, "nope", {"sans_bold": ("No Such Font.ttc", 0)})
    use_faces("nope")
    try:
        assert font("sans_bold", 21).getname()[0].startswith("DejaVu")
    finally:
        use_faces(None)


# --- the mix render path -------------------------------------------------------------------------------------

@needs_ffmpeg
def test_mix_renders_cues_and_hits_the_loudness_target(tmp_path):
    spec = validate({"scenes": [{**MOTION, "seconds": 3, "sfx": [{"at": 0.2, "sound": "hit"}, {"at": 1.0, "sound": "swell", "seconds": 1.5}]}],
                     "formats": ["vertical"], "music": {"style": "drive", "seed": 2}, "mix": {"loudness": -16}}, base_dir=tmp_path)
    res = render(spec, "vertical", tmp_path, fps=10, scale=0.25, contact=False)
    m = res["mix"]
    assert m["lufs"] == pytest.approx(-16, abs=0.5) and m["tp"] <= -1.4
    assert Path(res["video"]).stat().st_size > 0


@needs_ffmpeg
def test_without_mix_there_are_no_mix_numbers(tmp_path):
    spec = validate({"scenes": [MOTION], "formats": ["vertical"], "music": {"style": "drive"}}, base_dir=tmp_path)
    assert render(spec, "vertical", tmp_path, fps=10, scale=0.25, contact=False)["mix"] is None


# --- review fixes ----------------------------------------------------------------------------------------------

def test_sfx_without_mix_is_an_error_not_silence():
    with pytest.raises(SpecError, match="only with `mix:`"):
        validate({"scenes": [{**MOTION, "sfx": [{"at": 0.2, "sound": "hit"}]}]})


def test_the_whole_spec_has_a_cue_limit():
    cues = [{"at": 0.01 * k, "sound": "tick"} for k in range(40)]
    n = sfx.MAX_CUES // 40 + 1
    with pytest.raises(SpecError, match=f"limit is {sfx.MAX_CUES} \\(MAX_CUES\\)"):
        validate({"limits": {"max_scenes": 1000}, "mix": True, "scenes": [{**MOTION, "sfx": cues}] * n})
    validate({"mix": True, "scenes": [{**MOTION, "sfx": cues}] * (sfx.MAX_CUES // 40)})


def test_the_mix_path_has_a_length_limit():
    from yamlreel import mixdown, specs
    assert specs.MAX_MIX_SECONDS == mixdown.MAX_MIX_SECONDS
    with pytest.raises(SpecError, match="MAX_MIX_SECONDS"):
        validate({"limits": {"max_seconds": 900}, "mix": True, "scenes": [{**MOTION, "seconds": 301}] * 2})
    with pytest.raises(mixdown.MixError, match="at most 600s"):
        mixdown.master(Path("."), "ffmpeg", 601, None, None, [], {}, 1.0, 1.0)


def test_cues_past_their_scene_are_dropped_with_a_warning():
    spec = validate({"mix": True, "scenes": [{**MOTION, "sfx": [{"at": 1.5, "sound": "pop"}, {"at": 2.5, "sound": "hit"}]},
                                             {**MOTION, "sfx": [{"at": 9, "sound": "ding"}]}]})
    warned: list[str] = []
    cues = scene_cues(spec, [0.0, 2.0], 30, [2.0, 2.0], warned.append)
    assert [c.sound for c in cues] == ["pop"]
    assert len(warned) == 2 and "scenes[0].sfx[1]" in warned[0] and "scenes[1].sfx[0]" in warned[1]


def test_hush_gain_touches_only_its_window_and_agrees_across_blocks():
    cue = [sfx.Cue(5.0, "hush", seconds=0.5)]
    whole = sfx.hush_gain(cue, 0, 10 * sfx.SR)
    parts = np.concatenate([sfx.hush_gain(cue, k * sfx.SR, sfx.SR) for k in range(10)])
    assert np.array_equal(whole, parts)
    assert whole.dtype == np.float32 and np.all(whole[: 4 * sfx.SR] == 1.0)


def test_a_bad_wav_is_a_mix_error(tmp_path):
    from yamlreel import mixdown
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not a wav")
    with pytest.raises(mixdown.MixError, match="cannot read"):
        mixdown.read_wav(bad)


@needs_ffmpeg
def test_hot_cues_are_scaled_not_clipped_before_mastering(tmp_path):
    from yamlreel import mixdown
    cues = [sfx.Cue(0.2, "hit", 2.0), sfx.Cue(0.2, "thud", 2.0)]
    out, info = mixdown.master(tmp_path, ffmpeg_exe(), 2.0, None, None, cues, {"sfx_db": 12}, 1.0, 1.0)
    raw = mixdown.read_wav(tmp_path / "mix-raw.wav")
    assert np.abs(raw).max() <= mixdown.HEADROOM + 1e-3          # the old code wrote it clipped at 1.0
    assert info["tp"] <= -1.4 and info["lufs"] > info["lufs_before"]   # impacts alone cannot reach -14 under the ceiling


@needs_ffmpeg
def test_silence_stays_silent(tmp_path):
    from yamlreel import mixdown
    out, info = mixdown.master(tmp_path, ffmpeg_exe(), 1.0, None, None, [sfx.Cue(0.2, "hush")], {}, 1.0, 1.0)
    assert info["gain_db"] == 0.0 and "silent" in info["note"]
    assert not np.any(mixdown.read_wav(out))


@needs_ffmpeg
def test_a_48k_music_file_is_decoded_for_the_mix(tmp_path):
    import wave
    song = tmp_path / "song.wav"
    t = np.arange(48000 * 3) / 48000
    with wave.open(str(song), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes((np.repeat(0.3 * np.sin(2 * np.pi * 220 * t), 2) * 32767).astype("<i2").tobytes())
    spec = validate({"scenes": [MOTION], "formats": ["vertical"], "music": {"style": "song.wav"}, "mix": True},
                    base_dir=tmp_path)
    spec["_dir"] = str(tmp_path)
    res = render(spec, "vertical", tmp_path, fps=10, scale=0.25, contact=False)
    assert res["mix"]["lufs"] == pytest.approx(-14, abs=0.5)
