"""The one YouTube Shorts rule: which videos the platform accepts as Shorts, kept apart from length advice.

Platform eligibility: a square or vertical video of at most SHORTS_MAX_SECONDS. Editorial advice: Shorts that end
within EDITORIAL_MAX_SECONDS tend to be watched to the end; that is a tip, never a reason to fail a check.
"""
from __future__ import annotations

SHORTS_MAX_SECONDS = 180.0
EDITORIAL_MAX_SECONDS = 60.0


def short_form(width: int, height: int) -> bool:
    """Square or vertical: the shapes the Shorts player shows full screen."""
    return height >= width


def shorts_eligible(width: int, height: int, seconds: float) -> bool:
    """True when YouTube accepts the video as a Short (square or vertical, at most SHORTS_MAX_SECONDS)."""
    return short_form(width, height) and seconds <= SHORTS_MAX_SECONDS


def eligibility_text(seconds: float) -> str:
    """Why a short-form video of `seconds` is or is not a Short, for reports."""
    if seconds <= SHORTS_MAX_SECONDS:
        return f"eligible for YouTube Shorts (up to {SHORTS_MAX_SECONDS:g}s)"
    return f"YouTube Shorts allow up to {SHORTS_MAX_SECONDS:g}s; beyond that it is a normal video"


def editorial_note(seconds: float) -> str | None:
    """A length tip for a Short that is eligible but longer than EDITORIAL_MAX_SECONDS, else None. Advice only."""
    if EDITORIAL_MAX_SECONDS < seconds <= SHORTS_MAX_SECONDS:
        return (f"tip: {seconds:.0f}s is a valid Short; Shorts of {EDITORIAL_MAX_SECONDS:g}s or less are more often "
                "watched to the end")
    return None
