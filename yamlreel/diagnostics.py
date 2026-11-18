"""The versions of the tools a render depends on: `python -m yamlreel.diagnostics`, `render --profile` and CI print it.

FFmpeg and the browser are separate programs whose versions change pixels and timing, so every report names them.
Each probe has a deadline and never raises: a missing tool is reported as such.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from importlib import metadata

from . import __version__
from .ffmpegtools import ffmpeg_exe

PROBE_SECONDS = 20.0


def _first_line(cmd: list[str]) -> str | None:
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=PROBE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or done.stderr).decode("utf-8", "replace").strip()
    return text.splitlines()[0].strip() if done.returncode == 0 and text else None


def _package(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


def ffmpeg_version() -> str:
    exe = ffmpeg_exe()
    if exe is None:
        return "not found"
    return _first_line([exe, "-version"]) or f"{exe} (version unknown)"


def chromium_version(chromium: str | None = None) -> str:
    """The browser line-art scenes and URL captures use: `chromium` (or $YAMLREEL_CHROMIUM), else playwright's own."""
    exe = chromium or os.environ.get("YAMLREEL_CHROMIUM") or None
    if exe is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "not available (playwright is not installed)"
        try:
            with sync_playwright() as p:
                exe = p.chromium.executable_path
        except Exception as e:   # playwright's driver could not start: report it, never fail the report
            return f"unknown ({type(e).__name__})"
        if not exe or not os.path.exists(exe):
            return "not installed (run: playwright install chromium)"
    return _first_line([exe, "--version"]) or f"{exe} (version unknown)"


def report(chromium: str | None = None) -> dict[str, str]:
    return {
        "yamlreel": __version__,
        "python": f"{platform.python_version()} ({sys.platform})",
        "pillow": _package("pillow"),
        "numpy": _package("numpy"),
        "pyyaml": _package("pyyaml"),
        "playwright": _package("playwright"),
        "ffmpeg": ffmpeg_version(),
        "chromium": chromium_version(chromium),
    }


def lines(chromium: str | None = None) -> list[str]:
    return [f"{k:<10} {v}" for k, v in report(chromium).items()]


if __name__ == "__main__":
    print("\n".join(lines()))
