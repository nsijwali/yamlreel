"""The optional polish add-on: audit (loudness, loop, format) and polish (effects mix, loudness)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from yamlreel import polish
from yamlreel.cli import main
from yamlreel.render import ffmpeg_exe

pytestmark = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")


def ff(*args: str) -> None:
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("clips")
    out = {k: d / f"{k}.mp4" for k in ("tone", "silent", "still", "jump")}
    ff("-f", "lavfi", "-i", "color=c=red:s=108x192:r=10:d=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out["tone"]))
    ff("-f", "lavfi", "-i", "color=c=red:s=108x192:r=10:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out["silent"]))
    ff("-f", "lavfi", "-i", "color=c=gray:s=108x192:r=10:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out["still"]))
    ff("-f", "lavfi", "-i", "color=c=black:s=108x192:r=10:d=1", "-f", "lavfi", "-i", "color=c=white:s=108x192:r=10:d=1",
       "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out["jump"]))
    return out


# --- audit ---------------------------------------------------------------------------------------------

def test_probe_reads_size_length_and_audio(clips):
    p = polish.probe(clips["tone"])
    assert (p.width, p.height, p.has_audio) == (108, 192, True)
    assert p.seconds == pytest.approx(2.0, abs=0.2)
    assert polish.probe(clips["silent"]).has_audio is False


def test_probe_refuses_a_non_video(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_text("not a video")
    with pytest.raises(polish.PolishError, match="does not look like a video"):
        polish.probe(f)
    with pytest.raises(polish.PolishError, match="not a file"):
        polish.probe(tmp_path / "missing.mp4")


def test_measure_audio_reports_finite_levels(clips):
    a = polish.measure_audio(clips["tone"])
    assert a is not None and -40 < a.lufs < 0 and a.true_peak < 0
    assert polish.measure_audio(clips["silent"]) is None


def test_loop_is_clean_for_a_still_and_flagged_for_a_jump(clips):
    assert polish.measure_loop(clips["still"]).clean
    jump = polish.measure_loop(clips["jump"])
    assert not jump.clean and jump.jump > 0.5


def test_audit_warns_without_audio_and_cli_strict_exit_code(clips, capsys):
    rep = polish.audit_video(clips["silent"])
    assert rep.warnings >= 1 and any("no audio track" in line for line in rep.lines)
    assert main(["audit", str(clips["silent"])]) == 0
    assert main(["audit", str(clips["silent"]), "--strict"]) == 1
    assert "loop:" in capsys.readouterr().out


def test_audit_flags_a_long_vertical_video(monkeypatch):
    long = polish.Probe(75.0, 1080, 1920, 30.0, True)
    monkeypatch.setattr(polish, "probe", lambda v: long)
    monkeypatch.setattr(polish, "measure_audio", lambda v: polish.Audio(-14.0, 3.0, -2.0))
    monkeypatch.setattr(polish, "measure_loop", lambda v: polish.Loop(0.0, 0.01))
    rep = polish.audit_video(Path("x.mp4"))
    assert any("60 seconds" in line and line.startswith("warn") for line in rep.lines)


# --- effects ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(polish.EFFECTS))
def test_every_effect_is_bounded_finite_and_deterministic(kind):
    a, b = polish.synth_effect(kind, 3), polish.synth_effect(kind, 3)
    assert np.array_equal(a, b)
    assert np.isfinite(a).all() and 0.99 <= np.max(np.abs(a)) <= 1.0
    assert 0.01 < len(a) / polish.SR < 1.0


def test_unknown_effect_is_named():
    with pytest.raises(polish.PolishError, match="unknown effect 'boom'"):
        polish.synth_effect("boom")


# --- events file -----------------------------------------------------------------------------------------

def events(tmp_path, text: str) -> Path:
    f = tmp_path / "events.yaml"
    f.write_text(text)
    return f


def test_events_load(tmp_path):
    ev = polish.load_events(events(tmp_path, "sfx:\n  - {at: 1.5, kind: tap}\n  - {at: 2, kind: pop, gain: 1}\n"))
    assert [(e.at, e.kind, e.gain) for e in ev] == [(1.5, "tap", 0.5), (2.0, "pop", 1.0)]


@pytest.mark.parametrize("body, msg", [
    ("sfx: []", "non-empty"),
    ("nope: 1", "non-empty"),
    ("sfx:\n  - 3", "must be a mapping"),
    ("sfx:\n  - {kind: tap}", "`at` must be seconds"),
    ("sfx:\n  - {at: -1, kind: tap}", "`at` must be seconds"),
    ("sfx:\n  - {at: true, kind: tap}", "`at` must be seconds"),
    ("sfx:\n  - {at: 1, kind: tap, gain: 9}", "`gain`"),
    ("sfx:\n  - {at: 1, kind: boom}", "unknown kind 'boom'"),
    ("sfx:\n  - {at: 1}", "exactly one"),
    ("sfx:\n  - {at: 1, kind: tap, file: a.wav}", "exactly one"),
    ("sfx:\n  - {at: 1, kind: tap, extra: 1}", "unknown field 'extra'"),
    ("sfx:\n  - {at: 1, file: gone.wav}", "does not exist"),
])
def test_events_are_validated(tmp_path, body, msg):
    with pytest.raises(polish.PolishError, match=msg):
        polish.load_events(events(tmp_path, body))


def test_recording_must_stay_inside_the_events_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.wav").write_bytes(b"x")
    inner = tmp_path / "inner"
    inner.mkdir()
    f = inner / "events.yaml"
    f.write_text("sfx:\n  - {at: 1, file: ../outside/a.wav}\n")
    with pytest.raises(polish.PolishError, match="outside the events file's folder"):
        polish.load_events(f)
    assert polish.load_events(f, allow=[outside])[0].file == (outside / "a.wav").resolve()


def test_render_events_places_sound_at_the_right_time():
    track = polish.render_events([polish.Event(1.0, 1.0, kind="click")], 2.0)
    assert len(track) == 2 * polish.SR
    assert np.abs(track[: polish.SR]).max() == 0
    assert np.abs(track[polish.SR: polish.SR + 2000]).max() > 0.1


# --- polish ----------------------------------------------------------------------------------------------

def test_filter_graph_variants():
    assert "amix=inputs=2" in polish.filter_graph(True, True, 5, None)
    assert "atrim=0:5.000" in polish.filter_graph(False, True, 5, None)
    assert "[0:a]anull" in polish.filter_graph(True, False, 5, -14)
    g = polish.filter_graph(True, False, 5, -14)
    assert "loudnorm=I=-14" in g and g.endswith("alimiter=limit=0.95[out]")


def test_polish_adds_effects_to_a_silent_video(clips, tmp_path):
    ev = events(tmp_path, "sfx:\n  - {at: 0.5, kind: tap}\n  - {at: 1.2, kind: clatter}\n")
    out = tmp_path / "out.mp4"
    polish.polish_video(clips["silent"], out, sfx=ev)
    info = polish.probe(out)
    assert info.has_audio and info.seconds == pytest.approx(polish.probe(clips["silent"]).seconds, abs=0.2)
    audio = polish.measure_audio(out)
    assert audio is not None and audio.lufs > -70 and audio.true_peak <= -0.5


def test_polish_normalises_loudness(clips, tmp_path):
    out = tmp_path / "loud.mp4"
    polish.polish_video(clips["tone"], out, loudness=-14)
    assert polish.measure_audio(out).lufs == pytest.approx(-14, abs=1.5)


def test_polish_refuses_unsafe_or_empty_requests(clips, tmp_path):
    out = tmp_path / "o.mp4"
    with pytest.raises(polish.PolishError, match="nothing to do"):
        polish.polish_video(clips["tone"], out)
    with pytest.raises(polish.PolishError, match="between -40 and -5"):
        polish.polish_video(clips["tone"], out, loudness=3)
    with pytest.raises(polish.PolishError, match="overwrite the input"):
        polish.polish_video(clips["tone"], clips["tone"], loudness=-14)
    with pytest.raises(polish.PolishError, match="no audio to normalise"):
        polish.polish_video(clips["silent"], out, loudness=-14)
    out.write_bytes(b"x")
    with pytest.raises(polish.PolishError, match="already exists"):
        polish.polish_video(clips["tone"], out, loudness=-14)


def test_cli_polish_writes_the_default_name(clips, tmp_path, capsys):
    src = tmp_path / "v.mp4"
    src.write_bytes(clips["tone"].read_bytes())
    assert main(["polish", str(src), "--loudness", "-14"]) == 0
    assert (tmp_path / "v-polished.mp4").is_file()
    assert main(["polish", str(src)]) == 1
    assert "nothing to do" in capsys.readouterr().err
