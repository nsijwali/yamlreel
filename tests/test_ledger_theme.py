"""The `ledger` theme: flat colours, contrast pairs, fonts, and no gradient anywhere in its background."""
from __future__ import annotations

import numpy as np
import pytest

from yamlreel import ledger_theme as L
from yamlreel.scenebase import Ctx
from yamlreel.theme import THEMES, font, get_theme, use_faces


def _lum(c: tuple[int, int, int]) -> float:
    def f(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (f(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_tokens():
    t = THEMES["ledger"]
    assert (t.bg, t.fg, t.accent) == ((0xEF, 0xEE, 0xE9), (0x1A, 0x1A, 0x1A), (0xE5, 0x38, 0x1C))
    assert t.flat and t.bg2 is None and t.glow is None and not t.frame
    assert t.accent2 not in (t.accent,) and ratio(t.accent2, t.bg) < 2   # accent2 is a neutral shade, not a 2nd accent
    assert t.fg != (0, 0, 0) and t.bg != (255, 255, 255)                  # B-22


@pytest.mark.parametrize("surface", ["bg", "card"])
def test_body_text_contrast(surface):
    t = THEMES["ledger"]
    assert ratio(t.fg, getattr(t, surface)) >= 4.5
    assert ratio(t.muted, getattr(t, surface)) >= 4.5
    assert ratio(t.muted, t.accent2) >= 4.5


@pytest.mark.parametrize("surface", ["bg", "card"])
def test_accent_is_large_text_only(surface):
    t = THEMES["ledger"]
    assert 3.0 <= ratio(t.accent, getattr(t, surface))      # large text (>= 24 px) and shapes; below 4.5 by design


def test_background_is_one_solid_colour(tmp_path):
    ctx = Ctx(108, 192, THEMES["ledger"], tmp_path, tmp_path)
    px = np.asarray(ctx.background())
    assert (px == np.array(THEMES["ledger"].bg)).all()


def test_fonts_ship_and_render_log_glyphs(tmp_path):
    from pathlib import Path
    for name, _ in L.FACE_SET.values():
        assert (Path(L.FONT_DIR) / name).is_file()
    Ctx(108, 192, THEMES["ledger"], tmp_path, tmp_path)           # selects the faces
    mono, sans = font("mono", 48), font("sans_bold", 96)
    assert "IBMPlexMono-Regular" in mono.path and "Archivo-Medium" in sans.path
    for ch in "0123456789#:,":
        assert mono.getlength(ch) == mono.getlength("0")           # monospaced digits and punctuation
        assert mono.getmask(ch).getbbox() is not None              # drawn, not a .notdef box
    use_faces(None)


def test_custom_theme_extends_ledger_stays_flat():
    t = get_theme("mine", {"mine": {"extends": "ledger", "accent": "#112233"}})
    assert t.flat and t.faces == "ledger" and t.accent == (0x11, 0x22, 0x33)
