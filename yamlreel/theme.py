"""Colour themes and font loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from .config import describe_value, unknown_key

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Theme:
    name: str
    bg: Color
    fg: Color
    muted: Color
    accent: Color
    accent2: Color
    card: Color
    line: Color
    display: str  # font family for headings: "serif" or "sans"
    frame: bool  # thin inset border


THEMES: dict[str, Theme] = {
    # Ink-black with vermilion and gold: calm, Japanese-inspired.
    "sumi": Theme("sumi", (20, 17, 15), (240, 232, 218), (160, 150, 135), (214, 96, 70),
                  (201, 162, 75), (36, 31, 28), (78, 68, 60), "serif", True),
    # Warm paper with indigo ink.
    "washi": Theme("washi", (233, 223, 199), (31, 47, 77), (105, 105, 100), (168, 60, 40),
                   (158, 120, 40), (245, 238, 220), (140, 128, 105), "serif", True),
    # Neutral dark technical look for system design.
    "midnight": Theme("midnight", (11, 18, 32), (232, 238, 248), (140, 155, 180), (79, 157, 255),
                      (255, 196, 87), (22, 32, 54), (56, 72, 104), "sans", False),
    # Cream, ink and burnt orange; every other colour is a blend of those three.
    "editorial": Theme("editorial", (243, 238, 227), (17, 17, 17), (103, 101, 97), (217, 84, 30),
                       (62, 61, 59), (249, 246, 238), (193, 189, 181), "serif", True),
}

_FONT_FILES = {
    "serif": "DejaVuSerif.ttf",
    "serif_bold": "DejaVuSerif-Bold.ttf",
    "sans": "DejaVuSans.ttf",
    "sans_bold": "DejaVuSans-Bold.ttf",
    "mono": "DejaVuSansMono.ttf",
    "mono_bold": "DejaVuSansMono-Bold.ttf",
    "cjk": "NotoSerifCJK-Regular.ttc",
}
_FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/dejavu-sans-fonts",       # Fedora
    "/usr/share/fonts/dejavu-serif-fonts",
    "/usr/share/fonts/dejavu-sans-mono-fonts",
    "/usr/share/fonts/TTF",                     # Arch
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "C:/Windows/Fonts",
]
# Per-user font folders, where fonts installed without admin rights land (Font Book and Homebrew casks on
# macOS, a per-user install on Windows). Searched after the system folders. Resolved at lookup time;
# _load results stay cached until set_font_dirs().
def _user_font_dirs() -> list[str]:
    """Per-user font folders, skipping any that cannot be resolved (a container user with no HOME and no
    passwd entry must still find fonts in the other folders, never in a cwd-relative '~' folder)."""
    out = []
    home = os.path.expanduser("~")
    if home != "~":
        out.append(os.path.join(home, "Library", "Fonts"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    return out


class FontError(RuntimeError):
    pass


_EXTRA_FONT_DIRS: list[str] = []
_THEME_COLORS = ("bg", "fg", "muted", "accent", "accent2", "card", "line")


def set_font_dirs(dirs: list[str]) -> None:
    """Extra directories searched before the system ones (from config)."""
    _EXTRA_FONT_DIRS[:] = [d for d in dirs if d]
    _load.cache_clear()


def parse_color(value: object, where: str) -> Color:
    """'#rrggbb' / 'rrggbb' or [r, g, b]."""
    if isinstance(value, str):
        h = value.strip().lstrip("#")
        if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    elif isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in value):
        return (value[0], value[1], value[2])
    raise ValueError(f"{where}: colour must be '#rrggbb' or [r, g, b]")


def get_theme(name: str, custom: dict | None = None) -> Theme:
    """Built-in theme, or one defined under `themes:` in config or the spec.

    A custom theme may set `extends` (a built-in or another custom theme) and override any colour,
    `display` (serif|sans) and `frame` (true|false)."""
    custom = custom or {}
    seen: list[str] = []

    def build(n: str) -> Theme:
        if n in seen:
            raise ValueError(f"theme {describe_value(n)} extends itself ({' -> '.join([*seen, n])})")
        seen.append(n)
        if n in custom:
            d = custom[n]
            if not isinstance(d, dict):
                raise ValueError(f"themes.{n} must be a mapping")
            allowed = {*_THEME_COLORS, "extends", "display", "frame"}
            for key in d:
                if key not in allowed:
                    raise unknown_key(f"themes.{n}", key, allowed, "a theme")
            base = build(d.get("extends", "sumi"))
            fields = {c: parse_color(d[c], f"themes.{n}.{c}") if c in d else getattr(base, c) for c in _THEME_COLORS}
            display = d.get("display", base.display)
            if display not in ("serif", "sans"):
                raise ValueError(f"themes.{n}.display must be serif or sans")
            return Theme(n, display=display, frame=bool(d.get("frame", base.frame)), **fields)
        if n in THEMES:
            return THEMES[n]
        raise ValueError(f"unknown theme {describe_value(n)}; choose from {', '.join([*THEMES, *custom])}")

    return build(name)


def _find(filename: str) -> str | None:
    dirs = [d for d in [*_EXTRA_FONT_DIRS, *_FONT_DIRS, *_user_font_dirs()] if d]
    for d in dirs:
        p = Path(d) / filename
        if p.exists():
            return str(p)
    return None


@lru_cache(maxsize=256)
def _load(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path = _find(_FONT_FILES[kind])
    if path is None:
        raise FontError(
            f"font {_FONT_FILES[kind]!r} not found. Install DejaVu fonts (macOS: brew install --cask "
            "font-dejavu; Debian/Ubuntu: sudo apt install fonts-dejavu-core; Fedora: sudo dnf install dejavu-sans-fonts "
            "dejavu-serif-fonts dejavu-sans-mono-fonts; Arch: sudo pacman -S ttf-dejavu), or point font_dir in your user config (or YAMLREEL_FONT_DIR) "
            "at a folder that holds them"
        )
    return ImageFont.truetype(path, size)


def has_cjk(text: str) -> bool:
    return any(ord(ch) >= 0x2E80 for ch in text)


def font(kind: str, size: float, text: str = "") -> ImageFont.FreeTypeFont:
    """Return a font; CJK text falls back to a CJK face when one is installed."""
    size = max(8, int(round(size)))
    if has_cjk(text) and _find(_FONT_FILES["cjk"]):
        return _load("cjk", size)
    return _load(kind, size)
