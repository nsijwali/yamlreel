"""Hardening slice 6: tool versions in diagnostics, and peak memory and deadlines in `render --profile`."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from yamlreel import diagnostics, peakmem
from yamlreel.cli import main
from yamlreel.render import ffmpeg_exe

ROOT = Path(__file__).resolve().parents[1]


def test_the_report_names_every_tool_and_never_raises(monkeypatch):
    monkeypatch.setattr(diagnostics, "ffmpeg_exe", lambda: None)
    rep = diagnostics.report(chromium="/no/such/chromium")
    assert set(rep) >= {"yamlreel", "python", "pillow", "numpy", "ffmpeg", "playwright", "chromium"}
    assert rep["ffmpeg"] == "not found" and rep["chromium"] == "/no/such/chromium (version unknown)"


@pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")
def test_the_ffmpeg_version_is_reported():
    assert diagnostics.ffmpeg_version().startswith("ffmpeg version")


def test_a_hung_tool_probe_is_bounded(monkeypatch):
    monkeypatch.setattr(diagnostics, "PROBE_SECONDS", 0.3)
    assert diagnostics._first_line([sys.executable, "-c", "import time; time.sleep(30)"]) is None


def test_python_m_yamlreel_diagnostics_prints_the_versions():
    done = subprocess.run([sys.executable, "-m", "yamlreel.diagnostics"], capture_output=True, text=True, cwd=ROOT,
                          timeout=120, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                                            "YAMLREEL_CHROMIUM": "/no/such/chromium"})
    assert done.returncode == 0, done.stderr
    assert "ffmpeg" in done.stdout and "chromium" in done.stdout and "yamlreel" in done.stdout


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs the resource module")
def test_peak_memory_is_reported_in_bytes():
    blob = bytearray(64 * 2 ** 20)   # 64 MB touched: the peak is at least that
    blob[::4096] = b"x" * len(blob[::4096])
    assert peakmem.peak_rss_bytes() >= 64 * 2 ** 20
    assert peakmem.mb(None) == "n/a" and peakmem.mb(3 * 2 ** 20) == "3 MB"


@pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")
def test_render_profile_prints_peak_memory_deadlines_and_versions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "user.yaml"))
    monkeypatch.setenv("YAMLREEL_CHROMIUM", "/no/such/chromium")
    (tmp_path / "user.yaml").write_text("timeouts: {encoder_stall: 77}\n")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.yaml").write_text("music: {style: none}\nscenes:\n  - {type: title, title: Hi, seconds: 1}\n")
    assert main(["render", "s.yaml", "--out", "o", "--format", "square", "--fps", "4", "--preview", "--profile"]) == 0
    out = capsys.readouterr().out
    assert "peak memory: yamlreel" in out and " MB" in out and "decoded scene pictures" in out
    assert "encoder_stall 77s" in out and "browser_frame 180s" in out
    assert "tools:" in out and "ffmpeg version" in out
