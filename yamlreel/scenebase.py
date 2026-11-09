"""Shared scene plumbing: the render context, the Scene base class and the appear helper."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, ClassVar

from PIL import Image, ImageDraw

from .textkit import TextBlock, blit, ease_out, make_block
from .theme import Theme


@dataclass
class Ctx:
    W: int
    H: int
    theme: Theme
    workdir: Path
    base_dir: Path
    roots: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium: str | None = None
    warnings: list[str] = field(default_factory=list)
    _bg: Image.Image | None = None

    @property
    def vertical(self) -> bool:
        return self.H > self.W

    @property
    def u(self) -> float:
        return min(self.W, self.H) / 1080

    @property
    def fs(self) -> float:
        """Text scale: vertical frames are read on phones, so type is larger."""
        return 1.2 if self.vertical else 1.0

    @property
    def margin(self) -> int:
        return int(self.W * 0.08)

    @property
    def cy(self) -> float:
        """Vertical centre of the content area (clear of the caption zone)."""
        return self.H * (0.42 if self.vertical else 0.45)

    @property
    def caption_y(self) -> float:
        return self.H * (0.76 if self.vertical else 0.88)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def background(self) -> Image.Image:
        if self._bg is None:
            t, W, H = self.theme, self.W, self.H
            # soft radial vignette: lighter centre, darker edges
            import numpy as np
            yy, xx = np.mgrid[0:H, 0:W]
            r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H * 0.45) / (H / 2)) ** 2)
            k = np.clip(1.0 - 0.10 * r, 0.82, 1.0)[..., None]
            base = np.array(t.bg, dtype=np.float32)[None, None, :] * k
            img = Image.fromarray(np.clip(base, 0, 255).astype("uint8"), "RGB")
            if t.frame:
                d = ImageDraw.Draw(img)
                inset = int(36 * self.u)
                d.rectangle([inset, inset, W - inset, H - inset], outline=t.line, width=max(1, int(2 * self.u)))
            self._bg = img
        return self._bg.copy()

    def block(self, text: str, kind: str, size: float, max_w: float, **kw: Any) -> TextBlock:
        b = make_block(text, kind, size, max_w, **kw)
        if b.shrunk:
            self.warn(f"text shrunk to fit ({b.size}px): {text[:40]!r}")
        return b

    @property
    def display(self) -> str:
        return "serif_bold" if self.theme.display == "serif" else "sans_bold"

    @property
    def body(self) -> str:
        return self.theme.display


def _appear(t: float, start: float, dur: float = 0.5) -> float:
    return ease_out((t - start) / dur)


KICKER_GAP = 16   # px at u == 1 between the bottom of a kicker and the top of its title


def make_kicker(ctx: Ctx, spec: dict[str, Any], max_w: float) -> TextBlock | None:
    """The block for a scene's `kicker` (a small tracked tag above the title), or None when it has none. Tracked text
    never wraps, so it shrinks to fit `max_w`; the timeline warns about that."""
    text = spec.get("kicker")
    if not text:
        return None
    u = ctx.u
    text = str(text).upper()
    requested, floor = 28 * u, max(6.0, 10 * u)
    size = requested
    while True:
        # the tracking shrinks with the font, so the whole tag scales and the floor is a size, not a width
        blk = make_block(text, "sans_bold", size, max_w, tracking=7 * u * size / requested, min_size=size)
        if blk.width - 4 <= max_w + 1 or size - 2 < floor:
            break
        size -= 2
    over = blk.width - 4 - max_w
    if over > 1 and len(text) > 1:
        # the font cannot get smaller (it has a floor of its own): close the tracking up instead, down to none
        tracking = max(0.0, 7 * u * size / requested - over / (len(text) - 1))
        blk = make_block(text, "sans_bold", size, max_w, tracking=tracking, min_size=size)
    if size < requested - 1 or over > 1:
        blk = replace(blk, shrunk=True)
    if blk.width - 4 > max_w + 1:
        ctx.warn("the kicker is wider than the frame; shorten it")
    return blk


def kicker_shift(ctx: Ctx, kicker: TextBlock | None, title_top: float) -> float:
    """How far the title and everything under it must move down for the kicker to sit KICKER_GAP above the title and
    no higher than 5% of the frame. 0 without a kicker, and when there is room above the title already."""
    if kicker is None:
        return 0.0
    return max(0.0, 0.05 * ctx.H + kicker.height + KICKER_GAP * ctx.u - title_top)


class Scene:
    # The spec keys a scene reads. specs.py rejects anything else, and a test keeps these tables in step
    # with what the constructors actually read.
    COMMON_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"type", "seconds", "caption", "narration", "narration_audio", "transition", "narration_words", "subtitles",
         "keywords", "step", "energy"})
    FIELDS: ClassVar[frozenset[str]] = frozenset()
    kicker: TextBlock | None = None    # scenes that take a `kicker` set these when they have one
    kicker_box: tuple[float, float, float, float] | None = None

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        self.spec = spec
        self.ctx = ctx
        self.duration = float(spec["seconds"])
        self.caption = spec.get("caption")
        self._static: dict[str, Image.Image] = {}

    def place_kicker(self, x: float, title_top: float, centred: bool) -> None:
        """Put the kicker KICKER_GAP above a title that starts at `title_top` and is anchored at `x` (its left edge, or
        its middle when `centred`). Call it with the title's final top, after the kicker has moved it down."""
        k = self.kicker
        if k is not None:
            x0 = x - k.width / 2 if centred else x
            y1 = title_top - KICKER_GAP * self.ctx.u
            self.kicker_box = (x0, y1 - k.height, x0 + k.width, y1)

    def draw_kicker(self, img: Image.Image, t: float) -> None:
        if self.kicker is not None and self.kicker_box is not None:
            blit(img, self.kicker, self.kicker_box[:2], self.ctx.theme.accent, _appear(t, 0.05), "tl")

    def layout_warnings(self, caption: tuple[float, float, float, float] | None) -> list[str]:
        """Layout problems found without drawing, as messages the timeline prefixes with `scenes[i]:`. `caption` is
        the caption pill's box (x0, y0, x1, y1) when this scene has one. Most scenes have nothing to report."""
        return []

    def static_layer(self, key: str, draw: Callable[[Image.Image], None]) -> Image.Image:
        """A fresh copy of the background with `draw(image)` applied to it. `draw` runs once per scene and key and
        its result is kept, so chrome that never changes (a table frame, a track) is not redrawn on every frame.
        Callers get a copy and may draw on it."""
        base = self._static.get(key)
        if base is None:
            base = self.ctx.background()
            draw(base)
            self._static[key] = base
        return base.copy()

    def cached_layers(self) -> int:
        """How many static layers this scene is holding (tests and the timeline's memory bound use it)."""
        return len(self._static)

    def release(self) -> None:
        """Drop the cached static layers. They are rebuilt on demand, so a scene may be released and drawn again."""
        self._static.clear()

    def frame(self, t: float) -> Image.Image:  # pragma: no cover
        raise NotImplementedError
