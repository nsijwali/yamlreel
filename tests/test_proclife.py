"""Hardening slice 3: every wait on the encoder, a subprocess or the browser is bounded, and every error path,
interruption included, stops what the render started (docs/specs/hardening.md)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from yamlreel import proclife
from yamlreel import render as render_mod
from yamlreel.config import Config, ConfigError, load_config
from yamlreel.proclife import Deadlines, FramePipe, ProcessTimeout
from yamlreel.publish import STAGING_PREFIX
from yamlreel.render import RenderError, Runtime, ffmpeg_exe, render
from yamlreel.specs import validate

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake encoders are executable scripts")
needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")
FRAME = b"\x00" * (1 << 20)   # 1 MB: a few of these fill any pipe buffer
QUICK = 15.0                  # every failure below must hand control back well within this


def _script(tmp_path: Path, name: str, body: str) -> str:
    """An executable stand-in for ffmpeg running `body` (Python)."""
    p = tmp_path / name
    p.write_text(f"#!{sys.executable}\nimport sys, time, signal\n{body}\n")
    p.chmod(0o755)
    return str(p)


NEVER_READS = "time.sleep(120)"
EXITS_EARLY = "sys.stderr.write('encoder gave up\\n'); sys.exit(3)"
READS_THEN_HANGS = "sys.stdin.buffer.read()\ntime.sleep(120)"
IGNORES_TERM = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\ntime.sleep(120)"


def _feeders() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "yamlreel-frames" and t.is_alive()]


# --- Deadlines ---------------------------------------------------------------------------------------------------

def test_deadlines_defaults_are_generous_and_configurable():
    d = Deadlines()
    assert d.encoder_stall >= 300 and d.subprocess >= 1800 and d.browser_frame >= 180
    assert d.finish_for(600) == d.encoder_finish + 2 * 600
    d = Deadlines.from_config({"encoder_stall": 5, "subprocess": 7.5})
    assert (d.encoder_stall, d.subprocess, d.browser_frame) == (5.0, 7.5, 180.0)


@pytest.mark.parametrize("raw,match", [
    ({"encoder_stal": 5}, "timeouts.encoder_stal: unknown field"),
    ({"subprocess": 0}, "between 1 and 86400"),
    ({"subprocess": True}, "between 1 and 86400"),
    ({"browser_frame": "10"}, "between 1 and 86400"),
    ([1], "must be a mapping"),
])
def test_bad_deadlines_are_refused(raw, match):
    with pytest.raises(ValueError, match=match):
        Deadlines.from_config(raw)


def _isolated_config(tmp_path, monkeypatch, user: str, project: str | None = None):
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "user.yaml"))
    (tmp_path / "user.yaml").write_text(user)
    if project is not None:
        (tmp_path / "yamlreel.config.yaml").write_text(project)
    return load_config(cwd=tmp_path)


def test_timeouts_come_from_the_user_config_only(tmp_path, monkeypatch):
    cfg = _isolated_config(tmp_path, monkeypatch, "timeouts: {encoder_stall: 42}\n")
    assert Deadlines.from_config(cfg.timeouts).encoder_stall == 42
    with pytest.raises(ConfigError, match="timeouts are only read from the user config"):
        _isolated_config(tmp_path, monkeypatch, "", "timeouts: {encoder_stall: 1}\n")
    with pytest.raises(ConfigError, match="between 1 and 86400"):
        _isolated_config(tmp_path, monkeypatch, "timeouts: {encoder_stall: -1}\n", "")


def test_the_cli_runtime_carries_the_configured_deadlines(tmp_path, monkeypatch):
    from yamlreel.cli import _runtime
    cfg = Config(timeouts={"browser_frame": 9})
    rt = _runtime({"_dir": str(tmp_path)}, cfg)
    assert rt.deadlines.browser_frame == 9 and rt.deadlines.encoder_stall == Deadlines().encoder_stall


# --- one-shot subprocesses ------------------------------------------------------------------------------------

def test_a_hung_subprocess_is_stopped_at_its_deadline():
    t0 = time.monotonic()
    with pytest.raises(ProcessTimeout, match="within 0.5s"):
        proclife.run([sys.executable, "-c", "import time; time.sleep(60)"], 0.5, "testing")
    assert time.monotonic() - t0 < QUICK


@posix_only
def test_stop_kills_a_process_that_ignores_terminate(tmp_path):
    proc = subprocess.Popen([_script(tmp_path, "stubborn", IGNORES_TERM)], stdout=subprocess.PIPE)
    assert proc.stdout.readline().strip() == b"ready"
    t0 = time.monotonic()
    proclife.stop(proc, grace=0.5)
    assert proc.poll() is not None and time.monotonic() - t0 < QUICK
    proc.stdout.close()


def test_mixdown_and_polish_turn_a_timeout_into_their_own_errors(monkeypatch):
    from yamlreel import mixdown, polish

    def hang(cmd, timeout, what):
        raise ProcessTimeout(f"ffmpeg did not finish {what} within {timeout:g}s")

    monkeypatch.setattr(proclife, "run", hang)
    with pytest.raises(mixdown.MixError, match="did not finish"):
        mixdown._run(["ffmpeg"], "mastering", 3)
    with pytest.raises(mixdown.MixError, match="did not finish"):
        mixdown.measure("ffmpeg", Path("x.wav"), 3)
    with pytest.raises(polish.PolishError, match="did not finish"):
        polish._run(["ffmpeg"], "mixing")


# --- the frame pipe -------------------------------------------------------------------------------------------

def _pipe(tmp_path: Path, body: str, **deadlines) -> tuple[FramePipe, object]:
    log = open(tmp_path / "enc.log", "wb")
    return FramePipe([_script(tmp_path, "enc", body)], log, Deadlines(**deadlines), 1.0), log


@posix_only
def test_an_encoder_that_stops_reading_is_stopped_after_the_stall_deadline(tmp_path):
    pipe, log = _pipe(tmp_path, NEVER_READS, encoder_stall=0.5)
    t0 = time.monotonic()
    with pytest.raises(ProcessTimeout, match="took no frame for 0.5s"):
        with pipe:
            for _ in range(64):
                pipe.put(FRAME)
    log.close()
    assert time.monotonic() - t0 < QUICK
    assert pipe.proc.poll() is not None and not pipe.feeder.is_alive()


@posix_only
def test_an_encoder_that_exits_early_ends_the_frames_and_reports_its_code(tmp_path):
    pipe, log = _pipe(tmp_path, EXITS_EARLY, encoder_stall=5)
    t0 = time.monotonic()
    with pipe:
        for _ in range(64):
            if pipe.broken:
                break
            pipe.put(FRAME)
        rc = pipe.finish()
    log.close()
    assert rc == 3 and pipe.broken and time.monotonic() - t0 < QUICK
    assert not pipe.feeder.is_alive() and "encoder gave up" in (tmp_path / "enc.log").read_text()


@posix_only
def test_an_encoder_that_never_finishes_is_stopped_after_the_finish_deadline(tmp_path):
    pipe, log = _pipe(tmp_path, READS_THEN_HANGS, encoder_finish=0.5, finish_per_second=0.0)
    with pytest.raises(ProcessTimeout, match="did not finish encoding within 0.5s"):
        with pipe:
            pipe.put(FRAME)
            pipe.finish()
    log.close()
    assert pipe.proc.poll() is not None and not pipe.feeder.is_alive()


@posix_only
@pytest.mark.parametrize("error", [ValueError("drawing failed"), KeyboardInterrupt()])
def test_a_drawing_error_or_cancellation_stops_the_encoder_and_the_feeder(tmp_path, error):
    pipe, log = _pipe(tmp_path, NEVER_READS, encoder_stall=60)
    t0 = time.monotonic()
    with pytest.raises(type(error)):
        with pipe:
            for _ in range(3):
                pipe.put(FRAME)    # the feeder is now blocked writing to a pipe nobody reads
            raise error
    log.close()
    assert time.monotonic() - t0 < QUICK
    assert pipe.proc.poll() is not None and not pipe.feeder.is_alive()


# --- render() as a whole --------------------------------------------------------------------------------------

def _spec(tmp_path: Path) -> dict:
    raw = {"music": {"style": "none"}, "scenes": [{"type": "title", "title": f"T{n}", "seconds": 2} for n in range(3)]}
    return {**validate(raw, Config(), base_dir=tmp_path), "_dir": str(tmp_path)}


@pytest.fixture
def spies(monkeypatch):
    """Every encoder process render starts, and how often the render closed its browsers."""
    from yamlreel import lineart_scene
    seen: dict[str, list] = {"procs": [], "closed": []}
    real_popen = subprocess.Popen

    def popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen["procs"].append(p)
        return p

    real_close = lineart_scene._Page.close_all.__func__
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(lineart_scene._Page, "close_all",
                        classmethod(lambda cls: (seen["closed"].append(1), real_close(cls))[1]))
    return seen


def _assert_released(tmp_path: Path, seen: dict) -> None:
    assert seen["procs"] and all(p.poll() is not None for p in seen["procs"])
    assert not _feeders()
    assert seen["closed"] == [1]
    assert not list((tmp_path / "out" / "square").glob(STAGING_PREFIX + "*"))


def _render(tmp_path: Path, **deadlines) -> dict:
    return render(_spec(tmp_path), "square", tmp_path / "out", fps=10, scale=0.5, contact=False,
                  runtime=Runtime(roots=[tmp_path], deadlines=Deadlines(**deadlines)))


@posix_only
def test_render_with_an_encoder_that_stops_reading_returns_and_releases_everything(tmp_path, monkeypatch, spies):
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: _script(tmp_path, "enc", NEVER_READS))
    t0 = time.monotonic()
    with pytest.raises(RenderError, match="took no frame"):
        _render(tmp_path, encoder_stall=1)
    assert time.monotonic() - t0 < QUICK
    _assert_released(tmp_path, spies)


@posix_only
def test_render_with_an_encoder_that_exits_early_reports_it_and_releases_everything(tmp_path, monkeypatch, spies):
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: _script(tmp_path, "enc", EXITS_EARLY))
    with pytest.raises(RenderError, match="encoder gave up"):
        _render(tmp_path)
    _assert_released(tmp_path, spies)


@needs_ffmpeg
@pytest.mark.parametrize("error", [RuntimeError("scene failed to draw"), KeyboardInterrupt()])
def test_a_drawing_exception_or_cancellation_mid_render_releases_everything(tmp_path, monkeypatch, spies, error):
    real = render_mod.Timeline.frame_at
    calls = []

    def frame_at(self, t):
        calls.append(t)
        if len(calls) == 12:
            raise error
        return real(self, t)

    monkeypatch.setattr(render_mod.Timeline, "frame_at", frame_at)
    with pytest.raises(type(error)):
        _render(tmp_path)
    _assert_released(tmp_path, spies)


@needs_ffmpeg
def test_a_normal_render_still_publishes_and_releases_everything(tmp_path, spies):
    res = _render(tmp_path)
    assert res["video"].exists() and spies["procs"][0].returncode == 0
    _assert_released(tmp_path, spies)


# --- the browser ------------------------------------------------------------------------------------------------

def test_a_browser_job_that_never_answers_times_out_and_is_cancelled():
    from yamlreel.lineart_scene import _Page
    page = _Page.__new__(_Page)   # only the waiting logic: no browser is started
    page.timeout = 0.2
    job: Future = Future()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="did not answer within 0.2s"):
        page._wait(job)
    assert job.cancelled() and time.monotonic() - t0 < QUICK


def test_a_lineart_frame_that_times_out_falls_back_to_typed_text(tmp_path, monkeypatch):
    from yamlreel import lineart_scene
    from yamlreel.scenebase import Ctx
    from yamlreel.theme import get_theme

    class Hung:
        def frame(self, *a):
            raise TimeoutError("the browser did not answer within 0.2s (timeouts.browser_frame)")

    monkeypatch.setattr(lineart_scene._Page, "get", classmethod(lambda cls, ctx: Hung()))
    ctx = Ctx(W=270, H=480, theme=get_theme("sumi"), workdir=tmp_path, base_dir=tmp_path, browser_timeout=0.2)
    scene = lineart_scene.LineArtScene({"type": "lineart", "text": "hello", "seconds": 2}, ctx)
    img = scene.frame(0.5)
    assert img.size == (270, 480) and any("TimeoutError" in w for w in ctx.warnings)


def test_the_user_subprocess_deadline_reaches_polish_mixdown_and_voice_speed(monkeypatch, tmp_path):
    import numpy as np
    from yamlreel import ffmpegtools, mixdown, polish
    from yamlreel import voice as voice_mod
    seen = []

    def fake_run(cmd, timeout, what):
        seen.append(timeout)
        raise ProcessTimeout(f"stopped after {timeout:g}s")

    monkeypatch.setattr(proclife, "run", fake_run)
    monkeypatch.setattr(ffmpegtools, "ffmpeg_exe", lambda: "ffmpeg")
    assert proclife.subprocess_seconds() == Deadlines().subprocess
    with proclife.subprocess_deadline(42):
        with pytest.raises(polish.PolishError):
            polish._run(["ffmpeg"], "mixing")
        with pytest.raises(mixdown.MixError):
            mixdown._run(["ffmpeg"], "mastering")
        with pytest.raises(mixdown.MixError):
            mixdown.measure("ffmpeg", tmp_path / "x.wav")
        with pytest.raises(voice_mod.VoiceError, match="stopped after 42s"):
            voice_mod.change_speed(np.zeros(4410, dtype=np.float32), 1.1)
    assert seen == [42, 42, 42, 42] and proclife.subprocess_seconds() == Deadlines().subprocess


def test_cli_audit_uses_the_timeouts_from_the_user_config(tmp_path, monkeypatch):
    from yamlreel import polish
    from yamlreel.cli import main
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "user.yaml"))
    (tmp_path / "user.yaml").write_text("timeouts: {subprocess: 33}\n")
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(polish, "audit_video", lambda v, t: seen.append(proclife.subprocess_seconds()) or
                        polish.Report(polish.Probe(1, 1, 1, None, False), None, polish.Loop(0, 0)))
    assert main(["audit", "x.mp4"]) == 0 and seen == [33.0]
