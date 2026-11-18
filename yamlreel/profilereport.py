"""The resource part of `render --profile`: peak memory, the deadlines in force and the tool versions."""
from __future__ import annotations

from typing import Any

from . import diagnostics
from .peakmem import mb, peak_child_rss_bytes, peak_rss_bytes
from .proclife import Deadlines


def measure(media_peak: int, plan_seconds: float) -> dict[str, Any]:
    """What render() adds to its profile at the end of a format."""
    return {"peak_rss": peak_rss_bytes(), "peak_child_rss": peak_child_rss_bytes(), "media_peak": media_peak,
            "plan_seconds": plan_seconds}


def resource_lines(p: dict[str, Any], deadlines: Deadlines) -> list[str]:
    d = deadlines
    return [
        f"  peak memory: yamlreel {mb(p.get('peak_rss'))} (process, so far), largest finished child "
        f"(ffmpeg) {mb(p.get('peak_child_rss'))}, decoded scene pictures at most {mb(p.get('media_peak'))}",
        f"  narration and timing plan: {p.get('plan_seconds', 0.0):.2f}s (made once, shared by every format)",
        f"  subprocess deadlines (timeouts:): encoder_stall {d.encoder_stall:g}s, encoder_finish {d.encoder_finish:g}s "
        f"+ {d.finish_per_second:g}s per video second, subprocess {d.subprocess:g}s, browser_frame {d.browser_frame:g}s",
    ]


def version_lines(chromium: str | None) -> list[str]:
    return ["tools:", *(f"  {line}" for line in diagnostics.lines(chromium))]
