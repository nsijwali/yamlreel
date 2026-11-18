"""Shared scene plumbing: the render context, the Scene base class and the appear helper."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, ClassVar

from PIL import Image, ImageDraw

from .textkit import TextBlock, blit, ease_out, make_block
from .theme import Theme, use_faces


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

    def __post_init__(self) -> None:
        use_faces(self.theme.faces)   # the theme's font faces (none for the classic themes)

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
        if self._bg is None and self.theme.bg2 is not None:
            self._bg = studio_background(self.theme, self.W, self.H)
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


def studio_background(t: Theme, W: int, H: int) -> Image.Image:
    """The gradient background of a theme with `bg2`: `bg` at the top easing into `bg2` at the bottom, a soft light
    pool of `glow` behind the upper middle (where the content sits), a vignette, and a fixed ordered dither of under
    one level so the gradient does not band once it is encoded. Built once per render context."""
    import numpy as np
    assert t.bg2 is not None
    yy = (np.arange(H, dtype=np.float32) / max(1, H - 1))[:, None]
    xx = (np.arange(W, dtype=np.float32) / max(1, W - 1))[None, :]
    k = yy * yy * (3 - 2 * yy)                                     # smoothstep top -> bottom
    top, bottom = np.array(t.bg, np.float32), np.array(t.bg2, np.float32)
    img = top[None, None, :] * (1 - k[..., None]) + bottom[None, None, :] * k[..., None]
    if t.glow is not None:
        cy = 0.36 if H > W else 0.42
        d2 = ((xx - 0.5) / 0.75) ** 2 + ((yy - cy) / (0.42 if H > W else 0.6)) ** 2
        pool = np.exp(-d2 * 2.2)[..., None]
        img = img + (np.array(t.glow, np.float32) - top)[None, None, :] * pool * 0.55
    r2 = ((xx - 0.5) / 0.62) ** 2 + ((yy - 0.45) / 0.62) ** 2
    img = img * np.clip(1.0 - 0.22 * r2, 0.6, 1.0)[..., None]
    bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.float32) / 16 - 0.47
    img = img + np.tile(bayer, (H // 4 + 1, W // 4 + 1))[:H, :W, None]
    return Image.fromarray(np.clip(img + 0.5, 0, 255).astype("uint8"), "RGB")


SS = 2  # diagram supersampling
_SS_MARGIN = 16  # layer pixels kept around the drawn area: more than the reach of the LANCZOS filter (3 * factor, factor <= 5)


def downsample(layer: Image.Image, factor: int = SS) -> tuple[Image.Image, tuple[int, int]] | None:
    """The drawn part of a supersampled RGBA layer at 1x, and where to paste it: the same pixels as
    `layer.resize(1/factor, LANCZOS)`, in a fraction of the time, because only the area that has ink is resized.
    None for an empty layer. To match the whole-layer resize exactly, the crop sits on pixel bounds that are
    multiples of `factor` with a margin wider than the filter, and the two passes run in Pillow's order for a full
    frame (across, then down); left to itself Pillow picks the order by the shape of the image, and the other
    order rounds differently."""
    box = layer.getbbox()
    if box is None:
        return None
    x0 = max(0, (box[0] - _SS_MARGIN) // factor * factor)
    y0 = max(0, (box[1] - _SS_MARGIN) // factor * factor)
    x1 = min(layer.width, -(-(box[2] + _SS_MARGIN) // factor) * factor)
    y1 = min(layer.height, -(-(box[3] + _SS_MARGIN) // factor) * factor)
    part = layer.crop((x0, y0, x1, y1)).convert("RGBa")   # premultiplied, as Image.resize does for RGBA
    w, h = part.width // factor, part.height // factor
    small = part.resize((w, part.height), Image.LANCZOS).resize((w, h), Image.LANCZOS)
    return small.convert("RGBA"), (x0 // factor, y0 // factor)


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
         "keywords", "step", "energy", "sfx"})
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

    def prefetch(self, t: float) -> None:
        """A hint that `frame(t)` is about to be asked for. A scene that waits on something else to draw a frame
        (a browser) may start on it now; the frame it later returns must be the same either way."""

    def frame(self, t: float) -> Image.Image:  # pragma: no cover
        raise NotImplementedError
