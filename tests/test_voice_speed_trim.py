"""voiceover.speed and voiceover.trim, and the editorial theme."""
import numpy as np
import pytest

from fakes import _fake_voice_server
from yamlreel import render as render_mod
from yamlreel import voice as voice_mod
from yamlreel.config import Config
from yamlreel.scenes import Ctx, build_scene
from yamlreel.specs import SpecError, validate
from yamlreel.theme import THEMES, get_theme
from yamlreel.voice import SAMPLE_RATE, VoiceServer

TITLE = {"type": "title", "title": "T"}
needs_ffmpeg = pytest.mark.skipif(render_mod.ffmpeg_exe() is None, reason="ffmpeg not available")


def tone(seconds, amp=0.3):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * amp).astype(np.float32)


def quiet(seconds):
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def secs(a):
    return len(a) / SAMPLE_RATE


# --- trim ----------------------------------------------------------------------------------------------------

def test_trim_removes_leading_and_trailing_silence():
    out = voice_mod.trim_silence(np.concatenate([quiet(0.7), tone(1.0), quiet(0.9)]))
    assert secs(out) == pytest.approx(1.0 + 2 * voice_mod.TRIM_PAD, abs=0.05)


def test_trim_shortens_inner_gap_to_150ms():
    out = voice_mod.trim_silence(np.concatenate([tone(0.5), quiet(0.8), tone(0.5)]))
    assert secs(out) == pytest.approx(1.0 + voice_mod.TRIM_KEEP_GAP, abs=0.05)


def test_trim_keeps_short_inner_gap():
    clip = np.concatenate([tone(0.5), quiet(0.2), tone(0.5)])
    assert secs(voice_mod.trim_silence(clip)) == pytest.approx(1.2, abs=0.05)


def test_trim_leaves_clip_without_silence():
    clip = tone(1.0)
    assert secs(voice_mod.trim_silence(clip)) == pytest.approx(1.0, abs=0.005)


def test_trim_returns_all_quiet_clip_unchanged():
    clip = quiet(1.0)
    assert voice_mod.trim_silence(clip) is clip
    faint = tone(1.0, amp=1e-4)  # uniformly faint is not "silence" relative to itself, but must never be emptied
    assert len(voice_mod.trim_silence(faint)) > 0


def test_trim_does_not_clip_speech_edges():
    body = tone(1.0)
    out = voice_mod.trim_silence(np.concatenate([quiet(0.5), body, quiet(0.5)]))
    assert np.abs(out).max() == pytest.approx(np.abs(body).max())
    assert np.abs(out).sum() == pytest.approx(np.abs(body).sum(), rel=1e-3)   # every sample of the speech survives


def test_trim_is_relative_to_clip_loudness():
    out = voice_mod.trim_silence(np.concatenate([quiet(0.5), tone(1.0, amp=0.003), quiet(0.5)]))
    assert secs(out) < 1.2


def test_trim_handles_empty_clip():
    assert len(voice_mod.trim_silence(np.zeros(0, dtype=np.float32))) == 0


# --- speed ---------------------------------------------------------------------------------------------------

def test_speed_one_is_identity_and_skips_ffmpeg(monkeypatch):
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: pytest.fail("ffmpeg must not be looked up"))
    clip = tone(1.0)
    assert voice_mod.process_clip(clip, {}) is clip
    assert voice_mod.process_clip(clip, {"speed": 1.0, "trim": False}) is clip


@needs_ffmpeg
def test_speed_125_shortens_clip_by_about_20_percent():
    out = voice_mod.process_clip(tone(2.0), {"speed": 1.25})
    assert secs(out) == pytest.approx(2.0 / 1.25, abs=0.06)


def test_speed_without_ffmpeg_fails_clearly(monkeypatch):
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: None)
    with pytest.raises(voice_mod.VoiceError, match="voiceover.speed needs ffmpeg"):
        voice_mod.process_clip(tone(0.5), {"speed": 1.1})


def test_trim_alone_needs_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: pytest.fail("ffmpeg must not be looked up"))
    assert secs(voice_mod.process_clip(np.concatenate([quiet(1), tone(0.5)]), {"trim": True})) < 0.7


@needs_ffmpeg
def test_trim_runs_before_speed():
    clip = np.concatenate([quiet(1.0), tone(1.0), quiet(1.0)])
    out = voice_mod.process_clip(clip, {"trim": True, "speed": 1.25})
    assert secs(out) == pytest.approx((1.0 + 2 * voice_mod.TRIM_PAD) / 1.25, abs=0.08)


def test_trim_keeps_gaps_long_enough_for_the_speed_up():
    """A gap is trimmed to 150 ms *after* the speed-up, so the subtitles still see a pause at every sentence end."""
    clip = np.concatenate([tone(0.5), quiet(0.8), tone(0.5), quiet(0.8), tone(0.5)])
    plain = voice_mod.trim_silence(clip)
    fast = voice_mod.trim_silence(clip, 1.3)
    assert secs(fast) - secs(plain) == pytest.approx(2 * voice_mod.TRIM_KEEP_GAP * 0.3, abs=0.05)
    assert (secs(fast) - 1.5) / 2 / 1.3 >= voice_mod.TRIM_KEEP_GAP - 0.03


def test_process_clip_passes_speed_to_trim(monkeypatch):
    seen = []
    monkeypatch.setattr(voice_mod, "trim_silence", lambda a, speed=1.0: seen.append(speed) or a)
    monkeypatch.setattr(voice_mod, "change_speed", lambda a, s: a)
    voice_mod.process_clip(tone(0.5), {"trim": True, "speed": 1.3})
    assert seen == [1.3]


# --- validation ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("speed", [0.79, 1.31, 0, -1, "fast", True, None])
def test_speed_out_of_range_rejected(speed):
    with pytest.raises(SpecError, match=r"voiceover.speed must be a number between 0.8 and 1.3"):
        validate({"scenes": [TITLE], "voiceover": {"speed": speed}})


@pytest.mark.parametrize("trim", [1, "yes", None])
def test_trim_must_be_bool(trim):
    with pytest.raises(SpecError, match=r"voiceover.trim must be true or false"):
        validate({"scenes": [TITLE], "voiceover": {"trim": trim}})


def test_speed_and_trim_accepted():
    vo = validate({"scenes": [TITLE], "voiceover": {"speed": 1.08, "trim": True}})["voiceover"]
    assert vo["speed"] == 1.08 and vo["trim"] is True
    assert validate({"scenes": [TITLE], "voiceover": {"speed": 1}})["voiceover"]["speed"] == 1.0


# --- cache and timing ----------------------------------------------------------------------------------------

def _narrated(**vo):
    return validate({"scenes": [{"type": "title", "title": "T", "narration": "Hello there", "seconds": 1}],
                     "music": {"style": "none"}, "voiceover": vo}, Config())


def test_cache_untouched_and_key_unchanged_and_scene_follows_processed_clip(tmp_path):
    srv, calls, url = _fake_voice_server(seconds=3.0)
    try:
        server = VoiceServer(url=url)
        plain = _narrated()
        voice_mod.prepare_narration(plain, server, tmp_path, lambda p: p)
        files = sorted(tmp_path.glob("narration-*.wav"))
        assert len(files) == 1 and len(calls) == 1
        raw, mtime = files[0].read_bytes(), files[0].stat().st_mtime_ns

        trimmed = _narrated(trim=True)
        clips = voice_mod.prepare_narration(trimmed, server, tmp_path, lambda p: p)
        assert sorted(tmp_path.glob("narration-*.wav")) == files          # same key, no new file
        assert files[0].read_bytes() == raw and files[0].stat().st_mtime_ns == mtime
        assert len(calls) == 1                                            # not spoken again
        assert voice_mod.narration_cache_key(server, "Hello there", None, None) in files[0].name
        # the fake tone has no silence to cut, so the clip is the same length; rendering again does not stack
        again = voice_mod.prepare_narration(_narrated(trim=True), server, tmp_path, lambda p: p)
        assert len(again[0]) == len(clips[0])
    finally:
        srv.shutdown()


def test_scene_length_follows_processed_clip(tmp_path, monkeypatch):
    raw = np.concatenate([quiet(1.0), tone(2.0), quiet(1.0)])
    wav = voice_mod.write_wav(tmp_path / "n.wav", raw)
    scene = {"type": "title", "title": "T", "narration_audio": "n.wav", "seconds": 1}
    spec = validate({"scenes": [scene], "music": {"style": "none"}, "voiceover": {"trim": True, "gap": 0.5}}, Config())
    clips = voice_mod.prepare_narration(spec, None, tmp_path, lambda p: wav)
    assert secs(clips[0]) < 2.2
    assert spec["scenes"][0]["seconds"] == pytest.approx(secs(clips[0]) + 0.5)
    assert voice_mod.scene_floors(spec, clips)[0] == pytest.approx(secs(clips[0]) + 0.5)
    untrimmed = validate({"scenes": [scene], "music": {"style": "none"}}, Config())
    voice_mod.prepare_narration(untrimmed, None, tmp_path, lambda p: wav)
    assert untrimmed["scenes"][0]["seconds"] > spec["scenes"][0]["seconds"] + 1.5


# --- editorial theme -----------------------------------------------------------------------------------------

def _blend(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def test_editorial_theme_colours():
    t = THEMES["editorial"]
    assert (t.bg, t.fg, t.accent) == ((243, 238, 227), (17, 17, 17), (217, 84, 30))
    assert t.display == "serif" and t.frame
    assert t.muted == _blend(t.bg, t.fg, 0.62) and t.line == _blend(t.bg, t.fg, 0.22)
    assert t.accent2 == _blend(t.bg, t.fg, 0.80)
    # only blends of cream, ink and orange: each colour lies on the cream-ink line (accent itself is a base colour)
    for c in (t.muted, t.accent2, t.line):
        assert all(min(a, b) - 1 <= v <= max(a, b) + 1 for v, a, b in zip(c, t.bg, t.fg))
    assert get_theme("editorial") is t


def _lum(c):
    ch = [v / 255 for v in c]
    ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def test_editorial_contrast_is_readable():
    t = THEMES["editorial"]
    ratio = lambda a, b: (max(_lum(a), _lum(b)) + 0.05) / (min(_lum(a), _lum(b)) + 0.05)
    assert ratio(t.fg, t.bg) > 12
    assert ratio(t.accent, t.bg) > 3.0  # about 3.5:1: large text and highlights only


def test_editorial_theme_renders_a_scene(tmp_path):
    spec = validate({"theme": "editorial", "scenes": [{"type": "title", "title": "Hello", "caption": "cap"}]}, Config())
    scene = build_scene(spec["scenes"][0], Ctx(540, 960, get_theme("editorial"), tmp_path, tmp_path))
    frame = scene.frame(0.5)
    assert frame.size == (540, 960)
