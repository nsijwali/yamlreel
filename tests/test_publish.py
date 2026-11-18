"""Hardening slice 2: a failed render never destroys the previous successful outputs (docs/specs/hardening.md)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fakes import _tone_wav_bytes
from yamlreel import render as render_mod
from yamlreel.config import Config
from yamlreel.publish import STAGING_PREFIX, PublishError, Staging
from yamlreel.render import RenderError, Runtime, ffmpeg_exe, render
from yamlreel.specs import validate

needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")


# --- the staging folder ---------------------------------------------------------------------------------------

def test_publish_moves_every_output_with_the_video_last_and_deletes_stale_names(tmp_path, monkeypatch):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "subtitles.srt").write_text("old")
    moved: list[str] = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: moved.append(Path(b).name) or real(a, b))
    with Staging(dest) as st:
        assert st.dir.parent == dest and st.dir.name.startswith(STAGING_PREFIX)
        for name in ("video.mp4", "poster.jpg", "contact.png"):
            st.path(name).write_text(name)
        st.remove("subtitles.srt")
        published = st.publish(last="video.mp4")
    assert moved == ["poster.jpg", "contact.png", "video.mp4"]
    assert published == [dest / n for n in moved]
    assert sorted(p.name for p in dest.iterdir()) == ["contact.png", "poster.jpg", "video.mp4"]


def test_a_missing_staged_output_publishes_nothing(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "poster.jpg").write_text("old poster")
    st = Staging(dest)
    st.path("poster.jpg").write_text("new poster")
    st.path("video.mp4")   # asked for, never written (the encoder produced nothing)
    with pytest.raises(PublishError, match="video.mp4"):
        st.publish(last="video.mp4")
    assert (dest / "poster.jpg").read_text() == "old poster"
    assert [p.name for p in dest.iterdir()] == ["poster.jpg"]


def test_an_exception_inside_the_block_discards_the_staging_folder(tmp_path):
    dest = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        with Staging(dest) as st:
            st.path("video.mp4").write_text("half")
            raise KeyboardInterrupt
    assert list(dest.iterdir()) == []


def test_staged_names_are_plain_file_names(tmp_path):
    with Staging(tmp_path) as st:
        for bad in ("../video.mp4", "a/b.png"):
            with pytest.raises(ValueError):
                st.path(bad)
            with pytest.raises(ValueError):
                st.remove(bad)


# --- a failed render keeps the previous outputs --------------------------------------------------------------

def _spec(tmp_path: Path, srt: bool = True) -> dict:
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(0.8))
    raw = {"music": {"style": "none"}, "subtitles": {"srt": True} if srt else False,
           "scenes": [{"type": "title", "title": "One", "seconds": 1.5, "narration_audio": "tone.wav",
                       "narration": "hello there"},
                      {"type": "title", "title": "Two", "seconds": 1.0}]}
    return {**validate(raw, Config(), base_dir=tmp_path), "_dir": str(tmp_path)}


def _run(tmp_path: Path, spec: dict) -> dict:
    return render(spec, "square", tmp_path / "out", fps=6, scale=0.25, runtime=Runtime(roots=[tmp_path]))


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


@pytest.fixture
def first_render(tmp_path):
    res = _run(tmp_path, _spec(tmp_path))
    folder = tmp_path / "out" / "square"
    assert {"video.mp4", "poster.jpg", "contact.png", "subtitles.srt"} <= set(_snapshot(folder))
    assert res["video"] == folder / "video.mp4" and res["srt"] == folder / "subtitles.srt"
    assert res["contact"] == folder / "contact.png" and res["poster"] == folder / "poster.jpg"
    return folder, _snapshot(folder)


def _failing_encoder(tmp_path: Path) -> str:
    """A stand-in ffmpeg that reads its input and then fails, as a real encoder error would."""
    exe = tmp_path / "bad-ffmpeg"
    exe.write_text("#!/bin/sh\ncat > /dev/null\necho 'simulated encoder failure' >&2\nexit 1\n")
    exe.chmod(0o755)
    return str(exe)


@needs_ffmpeg
@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as the encoder")
def test_a_failed_encode_keeps_the_previous_outputs(tmp_path, first_render, monkeypatch):
    folder, before = first_render
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: _failing_encoder(tmp_path))
    with pytest.raises(RenderError, match="simulated encoder failure"):
        _run(tmp_path, _spec(tmp_path, srt=False))   # would also have deleted subtitles.srt on success
    assert _snapshot(folder) == before
    assert not list(folder.glob(STAGING_PREFIX + "*"))


@needs_ffmpeg
def test_a_failed_contact_sheet_keeps_the_previous_outputs(tmp_path, first_render, monkeypatch):
    folder, before = first_render

    def broken(*a, **kw):
        raise RuntimeError("simulated contact-sheet failure")

    monkeypatch.setattr(render_mod, "contact_sheet", broken)
    with pytest.raises(RuntimeError, match="contact-sheet"):
        _run(tmp_path, _spec(tmp_path, srt=False))
    assert _snapshot(folder) == before
    assert not list(folder.glob(STAGING_PREFIX + "*"))


@needs_ffmpeg
def test_a_successful_render_replaces_every_output_and_removes_a_stale_subtitle_file(tmp_path, first_render):
    folder, before = first_render
    spec = _spec(tmp_path, srt=False)
    spec["scenes"][1]["title"] = "Changed"
    res = _run(tmp_path, spec)
    after = _snapshot(folder)
    assert "subtitles.srt" not in after and res["srt"] is None
    assert after["video.mp4"] != before["video.mp4"] and after["contact.png"] != before["contact.png"]
    assert not list(folder.glob(STAGING_PREFIX + "*"))


@needs_ffmpeg
def test_an_only_render_does_not_touch_the_full_outputs(tmp_path, first_render):
    folder, before = first_render
    res = render(_spec(tmp_path), "square", tmp_path / "out", fps=6, scale=0.25, runtime=Runtime(roots=[tmp_path]),
                 only=[1])
    after = _snapshot(folder)
    assert {k: after[k] for k in before} == before
    assert res["video"] == folder / "video-scenes-1.mp4" and res["video"].exists()
