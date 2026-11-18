"""Hardening follow-up: SIGTERM/SIGHUP clean up like Ctrl-C, and staging folders left by a killed render are swept."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yamlreel import stalestaging
from yamlreel.publish import STAGING_PREFIX, Staging
from yamlreel.render import ffmpeg_exe
from yamlreel.signalexit import Terminated, exit_on_signals

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX signals and executable scripts")
ROOT = Path(__file__).resolve().parents[1]


# --- signals ----------------------------------------------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_becomes_an_exception_and_the_old_handler_comes_back(sig):
    before = signal.getsignal(sig)
    cleaned = []
    with pytest.raises(Terminated) as e:
        with exit_on_signals():
            try:
                os.kill(os.getpid(), sig)
                time.sleep(5)   # the handler runs here
            finally:
                cleaned.append(1)
    assert e.value.code == 128 + sig and cleaned == [1]
    assert signal.getsignal(sig) == before


# --- stale staging folders --------------------------------------------------------------------------------------

def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _staging(dest: Path, name: str, owner: str | None, age: float = 0.0) -> Path:
    d = dest / f"{STAGING_PREFIX}{name}"
    d.mkdir(parents=True)
    (d / "video.mp4").write_text("half")
    if owner is not None:
        (d / stalestaging.OWNER_FILE).write_text(owner)
    if age:
        t = time.time() - age
        os.utime(d, (t, t))
    return d


@posix_only
def test_a_new_staging_sweeps_folders_of_dead_or_day_old_renders_only(tmp_path):
    host = socket.gethostname()
    dead = _staging(tmp_path, "dead", f"{_dead_pid()} {host}")
    old = _staging(tmp_path, "old", f"{os.getppid()} {host}", age=2 * 86400)
    alive = _staging(tmp_path, "alive", f"{os.getppid()} {host}")
    other_host = _staging(tmp_path, "elsewhere", f"{_dead_pid()} another-host")
    unowned = _staging(tmp_path, "new", None)
    (tmp_path / "video.mp4").write_text("the previous output")
    with Staging(tmp_path) as st:
        assert (st.dir / stalestaging.OWNER_FILE).read_text().split()[0] == str(os.getpid())
        assert not dead.exists() and not old.exists()
        assert alive.exists() and other_host.exists() and unowned.exists()
    assert (tmp_path / "video.mp4").read_text() == "the previous output"


# --- a real render cancelled by a signal ------------------------------------------------------------------------

SLOW_ENCODER = """#!{python}
import os, sys, time
open(os.environ["FAKE_ENCODER_PID"], "w").write(str(os.getpid()))
while sys.stdin.buffer.read(65536):
    time.sleep(0.05)
time.sleep(60)
"""


def _env(tmp_path: Path, path: str) -> dict[str, str]:
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return {"PATH": path, "TMPDIR": str(tmp_path / "tmp"), "YAMLREEL_CONFIG": str(tmp_path / "no-config.yaml"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "FAKE_ENCODER_PID": str(tmp_path / "encoder.pid"),
            "PYTHONPATH": str(ROOT), "HOME": os.environ.get("HOME", str(tmp_path))}


def _render_cmd(tmp_path: Path) -> list[str]:
    return [sys.executable, "-m", "yamlreel", "render", str(tmp_path / "s.yaml"), "--out", str(tmp_path / "o"),
            "--format", "square", "--fps", "10", "--preview"]


@posix_only
@pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg for the first, successful render")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_mid_render_stops_the_encoder_and_keeps_the_previous_outputs(tmp_path, sig):
    (tmp_path / "s.yaml").write_text("music: {style: none}\nscenes:\n" +
                                     "".join(f"  - {{type: title, title: T{k}, seconds: 3}}\n" for k in range(3)))
    real_dir = str(Path(ffmpeg_exe()).parent)
    first = subprocess.run(_render_cmd(tmp_path), env=_env(tmp_path, f"{real_dir}:/usr/bin:/bin"), cwd=tmp_path,
                           capture_output=True, text=True, timeout=300)
    assert first.returncode == 0, first.stderr
    folder = tmp_path / "o" / "square"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}

    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "ffmpeg").write_text(SLOW_ENCODER.format(python=sys.executable))
    (fake / "ffmpeg").chmod(0o755)
    (tmp_path / "s.yaml").write_text((tmp_path / "s.yaml").read_text().replace("T0", "Changed"))
    proc = subprocess.Popen(_render_cmd(tmp_path), env=_env(tmp_path, f"{fake}:/usr/bin:/bin"), cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pid_file = tmp_path / "encoder.pid"
    deadline = time.monotonic() + 60
    while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    assert pid_file.exists(), proc.communicate(timeout=30)
    time.sleep(0.5)   # mid-render: frames are flowing to the slow encoder
    staged = list(folder.glob(STAGING_PREFIX + "*"))
    assert staged, "the render should be writing into a staging folder"
    proc.send_signal(sig)
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 128 + sig, err
    encoder = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(encoder, 0)
    assert not list(folder.glob(STAGING_PREFIX + "*"))
    assert not list((tmp_path / "tmp").glob("yamlreel-*")), "a work or plan folder was left behind"
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    shutil.rmtree(fake)
