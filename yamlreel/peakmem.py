"""Peak memory of this process (and of the largest finished child, such as ffmpeg), for `render --profile`.

`resource` exists on Linux and macOS only; elsewhere the numbers are None and the profile says so.
"""
from __future__ import annotations

import sys


def _ru_maxrss(who: str) -> int | None:
    try:
        import resource
    except ImportError:   # Windows
        return None
    peak = resource.getrusage(getattr(resource, who)).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)   # bytes on macOS, KiB on Linux


def peak_rss_bytes() -> int | None:
    """The most resident memory this process has used so far, in bytes."""
    return _ru_maxrss("RUSAGE_SELF")


def peak_child_rss_bytes() -> int | None:
    """The most resident memory any finished child process (ffmpeg, the browser) has used, in bytes."""
    return _ru_maxrss("RUSAGE_CHILDREN")


def mb(n: int | None) -> str:
    return "n/a" if n is None else f"{n / 2 ** 20:.0f} MB"
