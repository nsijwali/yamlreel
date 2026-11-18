"""The `dusk` theme: a quiet, cinematic look for lyric and mood videos.

Deep indigo that lightens slightly towards the bottom (the horizon at dusk), a faint violet haze behind the upper
middle, and one warm lamp colour (`accent`) kept for the single light in a scene. Headings are Georgia, body text is
Georgia Italic (glosses, quotes, translations) and sans text is Avenir Next, where the system has them; otherwise the
DejaVu defaults are used, as with every face set.

Kept out of theme.py so that module does not grow with every look. This module is plain data and imports nothing
from theme.py (so it can be imported first or alone); theme.py calls `register` at its bottom with its own tables and
its Theme class.
"""
from __future__ import annotations

from typing import Any, Callable

Color = tuple[int, int, int]

DUSK_FACES: dict[str, tuple[str, int]] = {
    "serif_bold": ("Georgia.ttf", 0),       # display: regular weight reads calmer than bold at lyric sizes
    "serif": ("Georgia Italic.ttf", 0),     # body
    "sans": ("Avenir Next.ttc", 5),
    "sans_bold": ("Avenir Next.ttc", 0),
    "mono": ("Menlo.ttc", 0),
    "mono_bold": ("Menlo.ttc", 1),
}

DUSK_FIELDS: dict[str, Any] = {
    "name": "dusk",
    "bg": (11, 16, 32),          # #0b1020, top of the gradient
    "fg": (243, 233, 216),       # warm paper white
    "muted": (142, 147, 181),    # cool grey-violet for secondary lines
    "accent": (255, 179, 71),    # #ffb347, the lamp
    "accent2": (214, 122, 128),  # dusty rose, for a second voice or a sad note
    "card": (24, 30, 62),
    "line": (52, 60, 104),
    "display": "serif",
    "frame": False,
    "bg2": (26, 33, 71),         # #1a2147, the lighter horizon at the bottom
    "glow": (54, 44, 78),        # a faint violet haze, not a spotlight
    "faces": "dusk",
}


def register(themes: dict[str, Any], face_sets: dict[str, dict[str, tuple[str, int]]],
             theme_cls: Callable[..., Any]) -> None:
    """Add the dusk theme (built with `theme_cls`, theme.Theme) and its face set to the built-in tables."""
    face_sets["dusk"] = DUSK_FACES
    themes["dusk"] = theme_cls(**DUSK_FIELDS)
