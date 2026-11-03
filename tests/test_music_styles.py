"""S5: the synthesised music styles, per-scene energy, narration ducking and per-style defaults."""
from __future__ import annotations

import subprocess
import tracemalloc
import wave
from pathlib import Path

import numpy as np
import pytest

from yamlreel import music, render as render_mod
from yamlreel.cli import main
from yamlreel.config import Config
from yamlreel.render import RenderError, Runtime, _prepare_audio, energy_sections, ffmpeg_exe, speech_regions
from yamlreel.specs import SpecError, validate

SR = music.SR
STYLES = music.SYNTH_STYLES
TITLE = {"type": "title", "title": "x"}


# --- the 0.2.2 koto, copied verbatim: the built-in style must stay bit-identical to it -------------------

NOTE_OFFSETS = music.NOTE_OFFSETS
SCALES = {"hirajoshi": [0, 2, 3, 7, 8], "in": [0, 1, 5, 7, 8], "yo": [0, 2, 5, 7, 9], "iwato": [0, 1, 5, 6, 10]}


def legacy__midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def legacy__pluck(freq: float, dur: float, rng: np.random.Generator) -> np.ndarray:
    """Karplus-Strong string, computed one period at a time."""
    period = max(2, int(round(SR / freq)))
    n = int(SR * dur)
    buf = np.convolve(rng.uniform(-1, 1, period), np.ones(3) / 3, "same")
    blocks = n // period + 1
    out = np.empty(blocks * period)
    for b in range(blocks):
        out[b * period:(b + 1) * period] = buf
        buf = 0.498 * (buf + np.roll(buf, -1))
    out = out[:n]
    return out * np.exp(-np.arange(n) / SR * 1.2)


def legacy__pad(freq: float, dur: float, amp: float) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    s = sum(np.sin(2 * np.pi * freq * k * t + k) * a for k, a in [(1, 1.0), (2, 0.25), (3, 0.08)])
    env = np.minimum(1, t / 2.0) * np.minimum(1, np.maximum(dur - t, 0) / 2.5)
    return s * env * amp


def legacy_compose(duration: float, bpm: float = 72, root: str = "D", scale: str = "hirajoshi",
            seed: int = 7, density: float = 0.7) -> np.ndarray:
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose from {', '.join(SCALES)}")
    if root not in NOTE_OFFSETS:
        raise ValueError(f"unknown root {root!r}; choose from {', '.join(NOTE_OFFSETS)}")
    rng = np.random.default_rng(seed)
    root_midi = 60 + NOTE_OFFSETS[root]  # octave above middle C region
    pool = [root_midi + 12 * o + s for o in (0, 1) for s in SCALES[scale]] + [root_midi + 24]
    beat = 60.0 / bpm
    mix = np.zeros(int(SR * (duration + 4)))

    def add(x: np.ndarray, t: float) -> None:
        i = int(t * SR)
        mix[i:i + len(x)] += x[: len(mix) - i]

    idx = 2
    t = 0.0
    last_note_t = max(0.0, duration - 3.5)
    while t < last_note_t:
        if t == 0.0 or rng.random() < density:
            add(legacy__pluck(legacy__midi_hz(pool[idx]), 3.5, rng) * 0.55, t)
            if rng.random() < 0.18 and t + beat * 0.5 < last_note_t:  # light grace note
                add(legacy__pluck(legacy__midi_hz(pool[min(idx + 1, len(pool) - 1)]), 2.5, rng) * 0.3, t + beat * 0.5)
        idx = int(np.clip(idx + rng.choice([-2, -1, -1, 0, 1, 1, 2]), 0, len(pool) - 1))
        t += beat * 2
    add(legacy__pluck(legacy__midi_hz(pool[0]), 4.0, rng) * 0.6, last_note_t)  # resolve on the tonic

    add(legacy__pad(legacy__midi_hz(root_midi - 12), duration + 2, 0.10), 0)
    add(legacy__pad(legacy__midi_hz(root_midi - 5), duration + 2, 0.06), 0)
    tt = np.arange(int(SR * duration)) / SR
    air = np.convolve(rng.normal(0, 1, len(tt)), np.ones(60) / 60, "same")
    mix[: len(tt)] += air * 0.075 * np.sin(np.pi * tt / duration) ** 2

    ir_n = int(SR * 2.2)
    ir = rng.normal(0, 1, ir_n) * np.exp(-np.arange(ir_n) / SR * 2.6)
    ir[0] = 0
    size = len(mix) + ir_n
    wet = np.fft.irfft(np.fft.rfft(mix, size) * np.fft.rfft(ir, size), size)[: len(mix)]
    wet /= np.abs(wet).max() + 1e-9
    out = mix / (np.abs(mix).max() + 1e-9) * 0.7 + wet * 0.45
    out /= np.abs(out).max() + 1e-9
    return (out * 0.8)[: int(SR * duration)].astype(np.float32)


def test_koto_is_bit_identical_to_0_2_2():
    for kwargs in ({}, {"bpm": 90, "root": "G", "scale": "yo", "seed": 3}, {"seed": 11, "scale": "iwato"}):
        for seconds in (3.0, 14.5):
            assert np.array_equal(music.compose(seconds, **kwargs), legacy_compose(seconds, **kwargs))
    joined = np.concatenate(list(music.blocks("koto", 40.0)))
    assert np.array_equal(joined, legacy_compose(40.0))      # the block path hands out the same samples


def test_koto_wav_bytes_match_0_2_2(tmp_path):
    new = music.write_wav(tmp_path / "a.wav", legacy_compose(5.0))
    streamed = music.write_wav_stream(tmp_path / "b.wav", music.blocks("koto", 5.0))
    assert new.read_bytes() == streamed.read_bytes()


# --- the new styles -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("style", STYLES)
def test_a_style_is_deterministic_and_the_exact_length(style):
    d = music.STYLE_DEFAULTS[style]
    a = music.generate(style, 7.3, d["bpm"], d["root"], d["scale"], seed=5)
    b = music.generate(style, 7.3, d["bpm"], d["root"], d["scale"], seed=5)
    c = music.generate(style, 7.3, d["bpm"], d["root"], d["scale"], seed=6)
    assert a.dtype == np.float32 and len(a) == int(SR * 7.3)
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    assert 0.2 < float(np.abs(a).max()) <= 1.0 and np.isfinite(a).all()


@pytest.mark.parametrize("style", STYLES)
def test_a_style_is_not_silent_at_the_end_and_never_clips(style):
    d = music.STYLE_DEFAULTS[style]
    x = music.generate(style, 20.0, d["bpm"], d["root"], d["scale"], sections=[(0, 20, 2)])
    assert float(np.abs(x).max()) < 0.95 and float(np.abs(x[-SR * 3:]).max()) > 0.01


@pytest.mark.parametrize("scale", sorted(music.SCALES))
def test_every_scale_works_with_every_style(scale):
    for style in STYLES:
        x = music.generate(style, 4.0, 90, "Eb", scale)
        assert len(x) == 4 * SR and float(np.abs(x).max()) > 0.05


def test_blocks_cover_the_track_in_30_second_pieces_and_join_seamlessly():
    parts = list(music.blocks("lofi", 75.0, 78, "D", "dorian"))
    assert [len(p) for p in parts] == [30 * SR, 30 * SR, 15 * SR]
    joined = np.concatenate(parts)
    for edge in (30 * SR, 60 * SR):                        # no jump where a block ends and the next begins
        assert float(np.abs(np.diff(joined[edge - 2:edge + 2])).max()) < 0.2


def _hf_energy(x: np.ndarray) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2     # windowed: a cut at the end leaks into every band
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    return float(spec[freqs > 2000].sum())


@pytest.mark.parametrize("style", ("lofi", "pulse"))
def test_high_energy_has_more_top_end_than_low(style):
    d = music.STYLE_DEFAULTS[style]
    low = music.generate(style, 16.0, d["bpm"], d["root"], d["scale"], sections=[(0, 16, 0)])
    high = music.generate(style, 16.0, d["bpm"], d["root"], d["scale"], sections=[(0, 16, 2)])
    assert _hf_energy(high) > 1.2 * _hf_energy(low)


@pytest.mark.parametrize("style", ("ambient", "piano"))
def test_energy_changes_the_other_styles_too(style):
    d = music.STYLE_DEFAULTS[style]
    low = music.generate(style, 16.0, d["bpm"], d["root"], d["scale"], sections=[(0, 16, 0)])
    high = music.generate(style, 16.0, d["bpm"], d["root"], d["scale"], sections=[(0, 16, 2)])
    assert not np.array_equal(low, high) and float(np.sqrt((high ** 2).mean())) > float(np.sqrt((low ** 2).mean()))


def test_koto_ignores_energy():
    assert np.array_equal(music.generate("koto", 6.0, sections=[(0, 6, 2)]), music.generate("koto", 6.0))


def test_level_curve_snaps_to_beats_and_slides_over_one_beat():
    beat = 1.0
    t = np.array([0.0, 1.0, 3.4, 3.5, 4.0, 4.5, 6.0, 9.0])
    lvl = music.level_curve(t, [(0.0, 4.2, 0), (4.2, 9.0, 2)], beat)       # 4.2 snaps to 4.0
    assert list(lvl[:3]) == [0, 0, 0] and lvl[3] == 0 and lvl[4] == pytest.approx(1.0) and lvl[5] == 2 and lvl[7] == 2
    assert music.level_curve(t, None, beat).tolist() == [1.0] * len(t)
    assert (music.level_curve(np.linspace(0, 3, 50), [(0, 0.2, 2), (0.2, 3, 0)], 2.0) >= 0).all()   # sections shorter than a beat


# --- streaming ----------------------------------------------------------------------------------------------

def test_streamed_wav_equals_the_joined_blocks(tmp_path):
    parts = list(music.blocks("piano", 42.0, 76, "G", "major"))
    streamed = music.write_wav_stream(tmp_path / "s.wav", iter(parts))
    whole = music.write_wav(tmp_path / "w.wav", np.concatenate(parts))
    assert streamed.read_bytes() == whole.read_bytes()
    with wave.open(str(streamed)) as w:
        assert w.getnframes() == 42 * SR and w.getnchannels() == 1


def test_600_seconds_of_lofi_stays_under_150_mb(tmp_path):
    tracemalloc.start()
    try:
        out = music.write_wav_stream(tmp_path / "long.wav", music.blocks("lofi", 600.0, 78, "D", "dorian"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 150 * 1024 * 1024, f"{peak / 1e6:.0f} MB"
    with wave.open(str(out)) as w:
        assert w.getnframes() == 600 * SR


# --- ducking -----------------------------------------------------------------------------------------------

def test_duck_gain_follows_the_specified_ramps():
    g = music.duck_gain(0, 10 * SR, music.merge_regions([(2.0, 4.0)]), 0.35)
    at = lambda s: float(g[int(s * SR)])
    assert at(0.5) == 1.0 and at(3.0) == pytest.approx(0.35, abs=1e-6)
    assert at(2.0 - 0.15) == pytest.approx(1.0, abs=1e-3) and at(2.0 - 0.075) == pytest.approx((1 + 0.35) / 2, abs=1e-3)
    assert at(2.0) == pytest.approx(0.35, abs=1e-3)                       # fully down when speech starts
    assert at(4.0) == pytest.approx(0.35, abs=1e-3) and at(4.25) == pytest.approx((1 + 0.35) / 2, abs=1e-3)
    assert at(5.0) == 1.0 and at(8.0) == 1.0


def test_short_pauses_stay_ducked_and_long_ones_recover():
    regions = music.merge_regions([(1.0, 2.0), (2.5, 3.0), (6.0, 7.0)])
    assert regions == [(1.0, 3.0), (6.0, 7.0)]
    g = music.duck_gain(0, 10 * SR, regions, 0.4)
    assert float(g[int(2.25 * SR)]) == pytest.approx(0.4) and float(g[int(4.5 * SR)]) == 1.0


def test_duck_is_independent_of_how_the_track_is_cut_into_blocks():
    x = np.ones(5 * SR, dtype=np.float32)
    whole = next(music.apply_duck([x], [(1.0, 2.0)], 0.3))
    pieces = np.concatenate(list(music.apply_duck([x[:SR * 3 // 2], x[SR * 3 // 2:]], [(1.0, 2.0)], 0.3)))
    assert np.array_equal(whole, pieces)


def test_speech_regions_come_from_clip_placement():
    clips = {1: np.zeros(SR * 2, dtype=np.float32), 3: np.zeros(SR, dtype=np.float32)}
    assert speech_regions(clips, [0.0, 5.0, 9.0, 12.0]) == [(5.3, 7.3), (12.3, 13.3)]


def _spec_with(**music_kw):
    return validate({"scenes": [TITLE], "music": music_kw}, Config())


def test_generated_music_is_ducked_in_the_file_it_writes(tmp_path):
    spec = _spec_with(style="pulse")
    plain = _prepare_audio(spec, 8.0, tmp_path, None, Runtime(roots=[tmp_path]))
    a = np.array(list(music.read_wav_blocks(plain))[0])
    ducked_path = _prepare_audio(spec, 8.0, tmp_path / "d", None, Runtime(roots=[tmp_path]), None, ([(2.0, 5.0)], 0.35))
    b = np.array(list(music.read_wav_blocks(ducked_path))[0])
    mid = slice(int(3 * SR), int(4 * SR))
    assert float(np.abs(b[mid]).max()) == pytest.approx(0.35 * float(np.abs(a[mid]).max()), rel=0.02)
    assert np.allclose(b[: SR], a[: SR], atol=1e-4) and np.allclose(b[-SR:], a[-SR:], atol=1e-4)


@pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")
def test_file_music_with_duck_is_decoded_through_the_whitelist(tmp_path, monkeypatch):
    src = tmp_path / "track.wav"
    music.write_wav(src, np.full(8 * SR, 0.5, dtype=np.float32))
    (tmp_path / "w").mkdir()
    seen: list[list[str]] = []
    real = subprocess.run
    monkeypatch.setattr(render_mod.subprocess, "run", lambda cmd, **kw: (seen.append(list(cmd)), real(cmd, **kw))[1])
    spec = validate({"scenes": [TITLE], "music": {"style": str(src)}}, Config())
    out = _prepare_audio(spec, 8.0, tmp_path / "w", None, Runtime(roots=[tmp_path]), None, ([(2.0, 5.0)], 0.5), ffmpeg_exe())
    cmd = seen[0]
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file" and cmd[cmd.index("-format_whitelist") + 1] == render_mod.AUDIO_FORMATS
    assert "-ac" in cmd and cmd[cmd.index("-ar") + 1] == str(SR)
    x = np.concatenate(list(music.read_wav_blocks(out)))
    assert float(x[int(3.5 * SR)]) == pytest.approx(0.25, abs=0.01) and float(x[int(7 * SR)]) == pytest.approx(0.5, abs=0.01)


@pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")
def test_file_music_without_duck_is_used_as_is(tmp_path):
    src = tmp_path / "track.wav"
    music.write_wav(src, np.zeros(SR, dtype=np.float32))
    spec = validate({"scenes": [TITLE], "music": {"style": str(src)}}, Config())
    assert _prepare_audio(spec, 1.0, tmp_path / "w", None, Runtime(roots=[tmp_path]), None, None, ffmpeg_exe()) == src.resolve()


def test_unreadable_music_file_with_duck_is_a_render_error(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    spec = validate({"scenes": [TITLE], "music": {"style": str(bad)}}, Config())
    (tmp_path / "w").mkdir()
    with pytest.raises(RenderError, match="music file"):
        _prepare_audio(spec, 1.0, tmp_path / "w", None, Runtime(roots=[tmp_path]), None, ([(0.0, 1.0)], 0.4), ffmpeg_exe() or "ffmpeg")


# --- spec: defaults, energy, duck ----------------------------------------------------------------------------

def test_per_style_defaults_fill_only_missing_keys():
    m = _spec_with(style="lofi", bpm=90)
    assert (m["music"]["bpm"], m["music"]["root"], m["music"]["scale"]) == (90, "D", "dorian")
    assert m["music"]["_filled"] == ["root", "scale"]
    koto = validate({"scenes": [TITLE]}, Config())["music"]
    assert (koto["bpm"], koto["root"], koto["scale"], koto["_filled"]) == (72, "D", "hirajoshi", ["bpm", "root", "scale"])
    for style, d in music.STYLE_DEFAULTS.items():
        got = _spec_with(style=style)["music"]
        assert (got["bpm"], got["root"], got["scale"]) == (d["bpm"], d["root"], d["scale"])
    assert _spec_with(style="none")["music"]["bpm"] == 72          # snap still needs a bpm


def test_a_validated_spec_validates_again_with_the_same_music():
    once = _spec_with(style="piano", root="A")
    twice = validate({"scenes": [TITLE], "music": once["music"]}, Config())
    assert twice["music"] == once["music"] and once["music"]["_filled"] == ["bpm", "scale"]
    with pytest.raises(SpecError, match="_filled is set by yamlreel"):
        _spec_with(style="koto", _filled=["volume"])


def test_music_override_refills_only_the_filled_keys():
    spec = _spec_with(style="koto", bpm=100)
    got = music.with_style(spec["music"], "lofi")
    assert (got["style"], got["bpm"], got["root"], got["scale"]) == ("lofi", 100, "D", "dorian")
    assert music.with_style(_spec_with(style="pulse")["music"], "piano")["bpm"] == 76


def test_the_override_drives_beat_snapping_through_render(tmp_path, monkeypatch):
    seen = {}
    real = render_mod.plan_durations
    monkeypatch.setattr(render_mod, "plan_durations", lambda spec, floors=None: (seen.update(bpm=spec["music"]["bpm"]), real(spec, floors))[1])
    spec = validate({"scenes": [{**TITLE, "seconds": 1}], "formats": ["square"], "music": {"snap": True}}, Config())
    try:
        render_mod.render(spec, "square", tmp_path, fps=1, scale=0.05, music_override="pulse", contact=False)
    except RenderError:
        pass
    assert seen["bpm"] == 100


def test_music_keys_and_styles_are_validated():
    with pytest.raises(SpecError, match=r"music.style must be one of"):
        _spec_with(style="disco")
    with pytest.raises(SpecError, match="music.scale must be one of"):
        _spec_with(style="lofi", scale="blues")


def test_scene_energy_and_voiceover_duck_are_validated():
    ok = validate({"scenes": [{**TITLE, "energy": "high"}], "voiceover": {"duck": True}}, Config())
    assert ok["scenes"][0]["energy"] == "high" and ok["voiceover"]["duck"] is True
    assert "duck" not in validate({"scenes": [TITLE]}, Config())["voiceover"]       # off unless asked for
    with pytest.raises(SpecError, match=r"scenes\[0\].energy must be one of low, mid, high"):
        validate({"scenes": [{**TITLE, "energy": "loud"}]}, Config())
    with pytest.raises(SpecError, match="voiceover.duck must be true or false"):
        validate({"scenes": [TITLE], "voiceover": {"duck": "yes"}}, Config())


def test_energy_sections_default_to_mid_per_scene():
    spec = validate({"scenes": [{**TITLE, "energy": "low"}, TITLE, {**TITLE, "energy": "high"}]}, Config())
    assert energy_sections(spec, [0.0, 3.0, 6.0], [3.0, 3.0, 3.0]) == [(0.0, 3.0, 0), (3.0, 6.0, 1), (6.0, 9.0, 2)]


# --- the CLI --------------------------------------------------------------------------------------------------

def test_music_command_writes_each_style_with_its_defaults(tmp_path, capsys):
    out = tmp_path / "t.wav"
    assert main(["music", "--style", "lofi", "--seconds", "3", "--out", str(out)]) == 0
    d = music.STYLE_DEFAULTS["lofi"]
    expect = music.generate("lofi", 3.0, d["bpm"], d["root"], d["scale"])
    with wave.open(str(out)) as w:
        assert w.getnframes() == len(expect)
    with pytest.raises(SystemExit, match="unknown scale"):
        main(["music", "--seconds", "2", "--scale", "nope", "--out", str(out)])


def test_music_override_names_are_built_in_not_paths():
    from yamlreel.cli import _runtime
    spec = validate({"scenes": [TITLE]}, Config())
    spec["_dir"] = "/spec"
    for name in ("koto", "ambient", "lofi", "pulse", "piano", "none"):
        assert len(_runtime(spec, Config(), name).roots) == 1
    assert len(_runtime(spec, Config(), "/tmp/x/track.mp3").roots) == 2


def test_story_turns_on_energy_and_duck_by_itself():
    from yamlreel import story
    assert story._supports() == (True, True)


def test_render_ducks_only_under_speech_and_keeps_the_constant_level_by_default(tmp_path):
    """Same spec twice: duck on and off. During speech the two mixes match (both sit at music_level); after the
    speech and its ramp, the ducked music is back at full level while the default stays low."""
    import subprocess
    from fakes import _fake_voice_server
    from yamlreel.voice import VoiceServer
    rms = {}
    for duck in (False, True):
        srv, _, url = _fake_voice_server(seconds=1.0)
        try:
            spec = validate({"scenes": [{"type": "title", "title": "T", "seconds": 6, "narration": "Line", "energy": "high"}],
                             "music": {"style": "lofi", "volume": 1.0}, "voiceover": {"duck": duck, "music_level": 0.3}}, Config())
            res = render_mod.render(spec, "square", tmp_path / str(duck), fps=5, scale=0.1, contact=False,
                                    runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
        finally:
            srv.shutdown()
        raw = subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-i", str(res["video"]), "-vn", "-ac", "1", "-ar", "8000",
                              "-f", "s16le", "-"], capture_output=True, check=True).stdout
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        rms[duck] = [float(np.sqrt(np.mean(a[int(x * 8000):int(y * 8000)] ** 2))) for x, y in ((0.4, 1.2), (3.0, 4.5))]
    assert rms[True][1] > 2.0 * rms[False][1]                       # music back at full level after the speech
    assert rms[True][0] == pytest.approx(rms[False][0], rel=0.25)   # and the same level under it
