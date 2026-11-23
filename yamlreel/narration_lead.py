"""How long each scene waits before its narration starts: `voiceover.lead`.

Every narrated scene used to start its speech a fixed 0.3 s after the scene began. That is a calm default, but a
Short that must speak its first word on frame 0 (viewers swipe away in the first seconds) needs it at 0. A spec can
now set it:

    voiceover: {voice: "...", lead: 0}

`lead` is seconds, from 0 to MAX_LEAD; without it the old DEFAULT_LEAD applies, so existing specs render the same.
The same value places the narration track, the music ducking regions, the subtitles and the caption pill, and sets
the shortest scene that holds a clip, so they all stay in step.
"""
from __future__ import annotations

from typing import Any

DEFAULT_LEAD = 0.3   # seconds of silence between a scene's start and its narration
MAX_LEAD = 2.0


def narration_lead(spec: dict[str, Any]) -> float:
    """The spec's `voiceover.lead`, or DEFAULT_LEAD when it is not set (the spec is already validated)."""
    vo = spec.get("voiceover") or {}
    return float(vo.get("lead", DEFAULT_LEAD))
