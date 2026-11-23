"""The `ledger` theme: flat drafting paper, near-black ink, one vermilion accent.

One idea: a database's log is a printed ledger. The look is solid colour only (no gradient, no light pool, no vignette),
Archivo for display and body (weights 400 and 500) and IBM Plex Mono for real log and printout lines. Both fonts are SIL
OFL 1.1 and ship in `fonts/ledger/` beside their licences.

This module holds only data, so `theme.py` registers the theme with a thin call site and nothing here imports it back.
`accent2` is not a second accent: it is the ledger's darker paper shade, for blocks and tracks. The ledger has one
accent, so `accent2` stays neutral.
"""
from __future__ import annotations

from pathlib import Path

Color = tuple[int, int, int]

PAPER: Color = (239, 238, 233)       # #EFEEE9 drafting paper: the ground
SHEET: Color = (252, 252, 249)       # #FCFCF9 a printed sheet laid on the ground
SHADE: Color = (218, 217, 210)       # #DAD9D2 darker paper: blocks, fold shadows (flat, never a gradient)
INK: Color = (26, 26, 26)            # #1A1A1A near-black ink
INK_MUTED: Color = (92, 91, 86)      # #5C5B56 ink tinted to the paper hue, for secondary text
RULE: Color = (184, 183, 175)        # #B8B7AF ledger rules and perforations (decoration, never text)
VERMILION: Color = (229, 56, 28)     # #E5381C the one accent

COLORS: dict[str, Color] = {
    "bg": PAPER, "fg": INK, "muted": INK_MUTED, "accent": VERMILION, "accent2": SHADE, "card": SHEET, "line": RULE,
}

FONT_DIR = str(Path(__file__).resolve().parent / "fonts" / "ledger")
# A logical font kind -> (file, index). Weights are 400 and 500 only: "bold" kinds draw Medium.
FACE_SET: dict[str, tuple[str, int]] = {
    "sans": ("Archivo-Regular.ttf", 0), "sans_bold": ("Archivo-Medium.ttf", 0),
    "serif": ("Archivo-Regular.ttf", 0), "serif_bold": ("Archivo-Medium.ttf", 0),
    "mono": ("IBMPlexMono-Regular.ttf", 0), "mono_bold": ("IBMPlexMono-Medium.ttf", 0),
}
