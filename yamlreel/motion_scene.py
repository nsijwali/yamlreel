"""The `motion` scene: text, rectangles and circles that enter, move, pulse and exit, drawn with Pillow only.

Time comes from `anim` (a pure function of `t`). Drawing keeps the frames cheap:

* every shape is its own sprite, supersampled on its own and only inside its own box, clipped to the frame (no
  full-frame supersampled layer per frame). A sprite is cached under a key that holds no position, so an element
  that only translates reuses it;
* anchors are whole pixels and every animated value is quantised (`anim.quantize`), so a cache hit gives exactly the
  pixels a redraw would;
* text at rest is pasted exactly as `textkit.blit` pastes it (sharp, at 1x). While it is scaled or rotated, a
  larger "hi" mask is resampled instead, because font sizes are whole pixels;
* a frame whose key equals the previous one comes back as a copy of the previous frame.

Nothing here talks to a browser. `release()` empties every cache; `retained_bytes()`, `max_layer_pixels()` and
`placements(t)` are accessors for tests and debugging.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Hashable

from PIL import Image, ImageChops, ImageDraw

from dataclasses import replace

from . import anim, motion_image, motion_paths
from .motion_assets import MotionImage
from .anim import Node, QState
from .motion_camera import MAX_CAMERA_KEYS, CameraKey, Viewport, camera_at, max_zoom  # noqa: F401
from .scenebase import _SS_MARGIN, Ctx, Scene, _appear, downsample
from .shots import CaptureError
from .textkit import TextBlock, blit, layout_words, wrap
from .theme import font

MOTION_CACHE_BYTES = 48 * 1024 * 1024     # sprites and text masks together, one LRU per scene
MAX_IMAGE_BYTES = 48 * 1024 * 1024        # the pre-scaled images of one scene (decoded while it is drawn)
MAX_LAYER_PIXELS = 12_000_000             # a raster layer, in pixels: 48 MB of RGBA
MAX_HI_MASK_PIXELS = 4_000_000            # a text mask kept for scaled drawing
TEXT_SUPER_MAX = 3.0                      # the largest sharpness factor of that mask
SPRITE_PAD = 12                           # 1x pixels around a supersampled shape: keeps the filter off the layer edge
FLASH_PULSE_SECONDS = 0.7
FLASH_TAG_SECONDS = 0.3
FLASH_WIDTH_STEPS = 64                    # stroke width multiplier quantum
FLASH_WIDTH_GAIN = 1.5

QUALITY_FACTORS = {"draft": 1, "standard": 2, "high": 3}
MOTION_COLORS = ("accent", "accent2", "fg", "muted", "card", "line", "bg")
FILL_CHOICES = (*MOTION_COLORS, "none")
TEXT_FONTS = ("display", "body", "mono")
TEXT_ALIGNS = ("left", "center", "right")
SPLITS = ("none", "words")

# The spec keys the scene reads. specs.py rejects anything else and a test keeps these in step with the code.
COMMON_ELEMENT_FIELDS: frozenset[str] = frozenset(
    {"kind", "id", "x", "y", "vx", "vy", "at", "enter", "emphasis", "end", "exit", "easing", "layer", "opacity",
     "scale", "rotate", "moves"})
MOVE_FIELDS: frozenset[str] = frozenset({"at", "seconds", "easing", "x", "y", "vx", "vy", "scale", "rotate", "opacity"})
ENTER_FIELDS: frozenset[str] = frozenset({"type", "seconds", "easing"})
EMPHASIS_FIELDS: frozenset[str] = frozenset({"type", "at", "seconds", "repeat"})
FLASH_FIELDS: frozenset[str] = frozenset({"node", "at", "text", "color"})   # the same as DiagramScene.FLASH_FIELDS
CAMERA_FIELDS: frozenset[str] = frozenset({"at", "seconds", "zoom", "focus", "vfocus", "easing"})
POSITION_FIELDS: frozenset[str] = frozenset({"x", "y", "vx", "vy", "scale", "rotate"})   # an arrow or path has none
MAX_IMAGES = motion_image.MAX_IMAGES

Rgb = tuple[int, int, int]
Box = tuple[float, float, float, float]


# --- small helpers --------------------------------------------------------------------------------------

def _rgba(c: Rgb, a: int = 255) -> tuple[int, int, int, int]:
    return (c[0], c[1], c[2], a)


def _rot_extents(hw: float, hh: float, degrees: float) -> tuple[float, float]:
    """Half width and height of the box of a (2 hw) x (2 hh) rectangle turned by `degrees`."""
    a = math.radians(degrees)
    ca, sa = abs(math.cos(a)), abs(math.sin(a))
    return ca * hw + sa * hh, sa * hw + ca * hh


def _rrect_points(hw: float, hh: float, r: float, n: int = 8) -> list[tuple[float, float]]:
    """The outline of a rounded rectangle centred on 0, clockwise on screen, each corner an arc of `n` segments."""
    r = max(0.0, min(r, hw, hh))
    pts: list[tuple[float, float]] = []
    for cx, cy, a0 in ((hw - r, -hh + r, -90), (hw - r, hh - r, 0), (-hw + r, hh - r, 90), (-hw + r, -hh + r, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _rrect_loop(hw: float, hh: float, r: float, n: int = 8) -> list[tuple[float, float]]:
    """The outline of `_rrect_points`, starting at the top-left corner (where a drawn rectangle begins)."""
    pts = _rrect_points(hw, hh, r, n)
    return pts[-1:] + pts[:-1]


def _turn(pts: list[tuple[float, float]], degrees: float, ox: float, oy: float, f: int) -> list[tuple[float, float]]:
    a = math.radians(degrees)
    ca, sa = math.cos(a), math.sin(a)
    return [((ox + x * ca - y * sa) * f, (oy + x * sa + y * ca) * f) for x, y in pts]


def _round_half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def _fmt_s(x: float) -> str:
    return f"{x:g}"


class _Lru:
    """One byte-budgeted LRU for sprites and text masks. Entries are rebuilt on demand, so eviction is always safe."""

    def __init__(self, budget: int):
        self.budget = budget
        self.bytes = 0
        self._d: OrderedDict[Hashable, tuple[Any, int]] = OrderedDict()

    def get(self, key: Hashable) -> Any:
        hit = self._d.get(key)
        if hit is None:
            return _MISS
        self._d.move_to_end(key)
        return hit[0]

    def put(self, key: Hashable, value: Any, nbytes: int) -> None:
        old = self._d.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        if nbytes > self.budget:   # one entry may never be bigger than the whole budget: use it once, do not keep it
            return
        self._d[key] = (value, nbytes)
        self.bytes += nbytes
        while self.bytes > self.budget and len(self._d) > 1:
            _, (_, n) = self._d.popitem(last=False)
            self.bytes -= n

    def clear(self) -> None:
        self._d.clear()
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._d)


_MISS = object()


@dataclass(frozen=True)
class Look:
    """How a flash changes one element: a colour (None: its own) and the stroke width multiplier in 1/64ths."""
    color: Rgb | None = None
    wq: int = FLASH_WIDTH_STEPS


NO_LOOK = Look()
Sprite = tuple[Image.Image, tuple[int, int]]


@dataclass
class Leaf:
    """A drawn element: its Node, the registry entry that draws it and what `prepare` made for it."""
    node: Node
    kind: "ElementKind"
    prep: Any
    el_id: str | None
    chain: tuple[str, ...]         # the ids of this element and of the groups above it
    label: str


@dataclass
class PlanItem:
    """One drawn element in one frame: its quantised state (camera applied), flash look, the geometry of a kind that
    has no anchor, and its box on screen."""
    q: QState
    leaf: Leaf
    look: Look
    geo: Hashable = None
    box: Box | None = None


@dataclass(frozen=True)
class Placement:
    name: str
    kind: str
    x: int
    y: int


# --- the element kinds ----------------------------------------------------------------------------------

class ElementKind:
    """One element kind. `FIELDS` are its own fields (plus COMMON_ELEMENT_FIELDS minus `REJECTS`), `PRESETS` the
    enter, emphasis and exit presets it supports. A new kind is a new entry in KINDS and a field table; the engine
    does not change. A kind that is not `POSITIONAL` (arrows and paths) is placed by its own points, not by an anchor,
    and works out its picture from `plan_geo`."""

    FIELDS: ClassVar[frozenset[str]] = frozenset()
    PRESETS: ClassVar[frozenset[str]] = anim.COMMON_PRESETS
    REJECTS: ClassVar[frozenset[str]] = frozenset()   # common fields this kind does not take
    POSITIONAL: ClassVar[bool] = True
    DRAWN: ClassVar[bool] = True                      # False: a group, which only carries its children

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> Any:   # once, in __init__
        raise NotImplementedError

    def bbox(self, prep: Any, q: QState) -> Box:
        """The box in pixels, relative to the anchor, rotation included."""
        raise NotImplementedError

    def screen_box(self, prep: Any, q: QState, geo: Hashable) -> Box | None:
        b = self.bbox(prep, q)
        return (q.ix + b[0], q.iy + b[1], q.ix + b[2], q.iy + b[3])

    def plan_geo(self, scene: "MotionScene", leaf: "Leaf", q: QState, cam: Viewport,
                 by_id: dict[str, Box]) -> Hashable:
        """For a kind without an anchor: the quantised geometry this frame (None: nothing to draw)."""
        return None

    def shape_key(self, prep: Any, q: QState, look: Look) -> Hashable:
        """Everything that changes the sprite's pixels except its position."""
        raise NotImplementedError

    def raster(self, prep: Any, q: QState, look: Look, f: int, win: tuple[int, int, int, int]) -> Image.Image:
        """An opaque-drawn RGBA layer of the window `win` (1x pixels, relative to the anchor) at f times the size."""
        raise NotImplementedError

    def paint(self, scene: "MotionScene", canvas: Image.Image, leaf: Leaf, q: QState, look: Look, geo: Hashable) -> None:
        scene.paint_sprite(canvas, leaf, q, look)


@dataclass(frozen=True)
class ShapePrep:
    w: float          # full size in pixels at scale 1 (a circle's w is its diameter)
    h: float
    radius: float
    fill: Rgb | None
    stroke: Rgb | None
    width: float      # stroke width in pixels at scale 1


class RectKind(ElementKind):
    FIELDS = frozenset({"w", "h", "radius", "fill", "stroke", "stroke_width"})
    PRESETS = anim.COMMON_PRESETS | {"draw"}

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> ShapePrep:
        u = scene.ctx.u
        fill, stroke = el.get("fill", "card"), el.get("stroke", "accent2")
        return ShapePrep(el["w"] * u, el["h"] * u, el.get("radius", 18) * u,
                         scene.color(fill) if fill != "none" else None,
                         scene.color(stroke) if stroke != "none" else None, el.get("stroke_width", 4) * u)

    def _look(self, prep: ShapePrep, look: Look) -> tuple[Rgb | None, Rgb | None, float]:
        fill, stroke, width = prep.fill, prep.stroke, prep.width
        if look.color is not None:
            if stroke is not None:
                stroke = look.color
                width = width * look.wq / FLASH_WIDTH_STEPS
            else:
                fill = look.color
        return fill, stroke, width

    def bbox(self, prep: ShapePrep, q: QState) -> Box:
        hx, hy = _rot_extents(prep.w * q.scale / 2, prep.h * q.scale / 2, q.rotate)
        return (-hx, -hy, hx, hy)

    def shape_key(self, prep: ShapePrep, q: QState, look: Look) -> Hashable:
        fill, stroke, width = self._look(prep, look)
        return ("rect", q.sq, q.rq, q.dq, fill, stroke, round(width, 3))

    def raster(self, prep: ShapePrep, q: QState, look: Look, f: int, win: tuple[int, int, int, int]) -> Image.Image:
        fill, stroke, width = self._look(prep, look)
        sc = q.scale
        w, h = prep.w * sc, prep.h * sc
        ox, oy = -win[0], -win[1]
        layer = Image.new("RGBA", ((win[2] - win[0]) * f, (win[3] - win[1]) * f), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        sw = int(width * sc * f) if stroke is not None else 0
        if q.dq < anim.Q_DRAW:   # drawing on: the outline from the top-left corner, the fill fades in at the end
            p = q.dq / anim.Q_DRAW
            fa = anim.clamp((p - 0.7) / 0.3)
            if fill is not None and fa > 0:
                if q.rq == 0:
                    d.rounded_rectangle([(ox - w / 2) * f, (oy - h / 2) * f, (ox + w / 2) * f, (oy + h / 2) * f],
                                        int(prep.radius * sc * f), fill=_rgba(fill, int(255 * fa)))
                else:
                    d.polygon(_turn(_rrect_points(w / 2, h / 2, prep.radius * sc), q.rotate, ox, oy, f),
                              fill=_rgba(fill, int(255 * fa)))
            ink = stroke or fill
            lw = sw or int(prep.width * sc * f)
            if ink is not None and p > 0 and lw > 0:
                inset = lw / f / 2
                loop = _turn(_rrect_loop(w / 2 - inset, h / 2 - inset, prep.radius * sc - inset), q.rotate, ox, oy, f)
                part = motion_paths.polyline_prefix(loop, p, closed=True)
                if len(part) >= 2:
                    d.line(part, fill=_rgba(ink), width=lw, joint="curve")
            return layer
        if q.rq == 0:
            box = [(ox - w / 2) * f, (oy - h / 2) * f, (ox + w / 2) * f, (oy + h / 2) * f]
            d.rounded_rectangle(box, int(prep.radius * sc * f), fill=_rgba(fill) if fill else None,
                                outline=_rgba(stroke) if sw > 0 and stroke else None, width=max(sw, 1))
        else:
            if fill is not None:
                d.polygon(_turn(_rrect_points(w / 2, h / 2, prep.radius * sc), q.rotate, ox, oy, f), fill=_rgba(fill))
            if sw > 0 and stroke is not None:
                inset = sw / f / 2   # a line is centred on its path; the unrotated outline sits inside the box
                pts = _turn(_rrect_points(w / 2 - inset, h / 2 - inset, prep.radius * sc - inset), q.rotate, ox, oy, f)
                d.line([*pts, pts[0]], fill=_rgba(stroke), width=sw, joint="curve")
        return layer


class CircleKind(ElementKind):
    FIELDS = frozenset({"r", "fill", "stroke", "stroke_width"})
    PRESETS = anim.COMMON_PRESETS | {"draw"}

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> ShapePrep:
        u = scene.ctx.u
        fill, stroke = el.get("fill", "accent"), el.get("stroke", "none")
        return ShapePrep(2 * el["r"] * u, 2 * el["r"] * u, el["r"] * u,
                         scene.color(fill) if fill != "none" else None,
                         scene.color(stroke) if stroke != "none" else None, el.get("stroke_width", 4) * u)

    _look = RectKind._look

    def bbox(self, prep: ShapePrep, q: QState) -> Box:
        r = prep.radius * q.scale
        return (-r, -r, r, r)

    def shape_key(self, prep: ShapePrep, q: QState, look: Look) -> Hashable:
        fill, stroke, width = self._look(prep, look)
        drawing = q.dq < anim.Q_DRAW
        return ("circle", q.sq, q.rq if drawing else 0, q.dq if drawing else anim.Q_DRAW, fill, stroke, round(width, 3))

    def raster(self, prep: ShapePrep, q: QState, look: Look, f: int, win: tuple[int, int, int, int]) -> Image.Image:
        fill, stroke, width = self._look(prep, look)
        sc = q.scale
        r = prep.radius * sc
        ox, oy = -win[0], -win[1]
        layer = Image.new("RGBA", ((win[2] - win[0]) * f, (win[3] - win[1]) * f), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        box = [(ox - r) * f, (oy - r) * f, (ox + r) * f, (oy + r) * f]
        sw = int(width * sc * f)
        if q.dq >= anim.Q_DRAW:
            d.ellipse(box, fill=_rgba(fill) if fill else None,
                      outline=_rgba(stroke) if sw > 0 and stroke else None, width=max(sw, 1))
            return layer
        p = q.dq / anim.Q_DRAW   # an arc from 12 o'clock, clockwise; the fill fades in over the last 30%
        fa = anim.clamp((p - 0.7) / 0.3)
        if fill is not None and fa > 0:
            d.ellipse(box, fill=_rgba(fill, int(255 * fa)))
        ink = stroke or fill
        if ink is not None and p > 0:
            start = -90 + q.rotate
            d.arc(box, start, start + 360 * p, fill=_rgba(ink), width=max(1, sw or int(prep.width * sc * f)))
        return layer


@dataclass
class TextPrep:
    blk: TextBlock
    font_kind: str
    text: str
    lines: list[str]
    align: str
    color: Rgb
    s_max: float
    chars: int
    typing: bool
    super_s: float = 1.0       # the sharpness factor of the hi mask, set when it is first built


class TextKind(ElementKind):
    FIELDS = frozenset({"text", "size", "font", "max_w", "align", "split", "stagger", "color"})
    PRESETS = anim.COMMON_PRESETS | {"draw"}

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> TextPrep:
        return scene.text_prep(str(el["text"]), el.get("font", "display"), el.get("size", 64), el.get("max_w", 0.84),
                               el.get("align", "center"), scene.color(el.get("color", "fg")), el)

    def bbox(self, prep: TextPrep, q: QState) -> Box:
        hx, hy = _rot_extents(prep.blk.width * q.scale / 2, prep.blk.height * q.scale / 2, q.rotate)
        return (-hx, -hy, hx, hy)

    def shape_key(self, prep: TextPrep, q: QState, look: Look) -> Hashable:
        return ("text", q.sq, q.rq, q.dq, look.color)

    def paint(self, scene: "MotionScene", canvas: Image.Image, leaf: Leaf, q: QState, look: Look, geo: Hashable) -> None:
        prep: TextPrep = leaf.prep
        b = self.screen_box(prep, q, None)
        W, H = scene.ctx.W, scene.ctx.H
        if b is None or b[2] <= 0 or b[0] >= W or b[3] <= 0 or b[1] >= H:
            return   # entirely off the frame
        color = look.color or prep.color
        alpha = q.oq / anim.Q_OPACITY
        typed = prep.typing and q.dq < prep.chars
        rest = q.sq == anim.Q_SCALE and q.rq == 0
        if rest and not typed:
            blit(canvas, prep.blk, (q.ix, q.iy), color, alpha, "mc")
            return
        got = scene.text_mask(leaf, q, typed)
        if got is None or alpha <= 0:
            return
        mask, pos = got
        canvas.paste(color, pos, mask if alpha >= 1 else mask.point(lambda v: int(v * alpha)))


class GroupKind(ElementKind):
    """Carries its children: they move, scale, turn and fade with it. A group draws nothing itself."""
    FIELDS = frozenset({"children", "stagger"})
    DRAWN = False

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> None:
        return None


@dataclass(frozen=True)
class ArrowPrep:
    src: tuple[str, Any]       # ("id", element id) or ("pt", (x, y) as frame fractions)
    dst: tuple[str, Any]
    color: Rgb
    width: float               # pixels at zoom 1
    dashed: bool
    head: bool


class ArrowKind(ElementKind):
    """An arrow between two elements (it follows them when they move) or two points. It has no anchor of its own."""
    FIELDS = frozenset({"from", "to", "color", "stroke_width", "dashed", "head"})
    PRESETS = anim.COMMON_PRESETS | {"draw"}
    REJECTS = POSITION_FIELDS
    POSITIONAL = False

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> ArrowPrep:
        def end(v: Any) -> tuple[str, Any]:
            return ("id", str(v)) if isinstance(v, str) else ("pt", (float(v[0]), float(v[1])))
        return ArrowPrep(end(el["from"]), end(el["to"]), scene.color(el.get("color", "accent2")),
                         el.get("stroke_width", 5) * scene.ctx.u, bool(el.get("dashed", False)), bool(el.get("head", True)))

    def plan_geo(self, scene: "MotionScene", leaf: Leaf, q: QState, cam: Viewport, by_id: dict[str, Box]) -> Hashable:
        prep: ArrowPrep = leaf.prep
        W, H = scene.ctx.W, scene.ctx.H

        def end(e: tuple[str, Any]) -> tuple[motion_paths.Point, tuple[float, float]] | None:
            if e[0] == "id":
                b = by_id.get(e[1])
                return None if b is None else (((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), (b[2] - b[0], b[3] - b[1]))
            from .motion_camera import to_screen
            return to_screen(cam, e[1][0] * W, e[1][1] * H, W, H), (0.0, 0.0)

        a, b = end(prep.src), end(prep.dst)
        if a is None or b is None:
            return None
        pad = motion_paths.ARROW_GAP * scene.ctx.u * cam.zoom
        span = motion_paths.arrow_span(a[0], a[1], b[0], b[1], pad, pad)
        if span is None:
            return None
        (x1, y1), (x2, y2), _, _ = span
        return (round(x1 * 16), round(y1 * 16), round(x2 * 16), round(y2 * 16), round(cam.zoom * anim.Q_SCALE))

    def screen_box(self, prep: ArrowPrep, q: QState, geo: Any) -> Box | None:
        if geo is None:
            return None
        x1, y1, x2, y2, zq = geo
        pad = (prep.width / 2 + 22 * (prep.width / 5)) * zq / anim.Q_SCALE
        return (min(x1, x2) / 16 - pad, min(y1, y2) / 16 - pad, max(x1, x2) / 16 + pad, max(y1, y2) / 16 + pad)

    def paint(self, scene: "MotionScene", canvas: Image.Image, leaf: Leaf, q: QState, look: Look, geo: Hashable) -> None:
        if geo is None:
            return
        prep: ArrowPrep = leaf.prep
        box = self.screen_box(prep, q, geo)
        assert box is not None
        color = look.color or prep.color
        key = ("arrow", geo, q.dq, color, look.wq if look.color else FLASH_WIDTH_STEPS, prep.dashed, prep.head, prep.width)
        scene.paint_free(canvas, leaf, q, box, key, lambda f, win: self._raster(prep, q, geo, look, color, f, win))

    def _raster(self, prep: ArrowPrep, q: QState, geo: Any, look: Look, color: Rgb, f: int,
                win: tuple[int, int, int, int]) -> Image.Image:
        x1, y1, x2, y2, zq = geo
        zoom = zq / anim.Q_SCALE
        mult = look.wq / FLASH_WIDTH_STEPS if look.color else 1.0
        layer = Image.new("RGBA", ((win[2] - win[0]) * f, (win[3] - win[1]) * f), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        a = ((x1 / 16 - win[0]) * f, (y1 / 16 - win[1]) * f)
        b = ((x2 / 16 - win[0]) * f, (y2 / 16 - win[1]) * f)
        p = q.dq / anim.Q_DRAW
        full = math.dist(a, b) or 1.0
        ux, uy = (b[0] - a[0]) / full, (b[1] - a[1]) / full
        end = (a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p)
        width = max(1, int(prep.width * zoom * mult * f))
        col = _rgba(color)
        k = prep.width / 5 * zoom * f    # the head and the dashes follow the stroke width as diagram's do at 5u
        if prep.dashed:
            for s0, s1 in motion_paths.dash_segments(a, b, full * p, 22 * k, 14 * k):
                d.line([s0, s1], fill=col, width=width)
        else:
            d.line([a, end], fill=col, width=width)
        if prep.head and p >= 0.98:
            ah = 22 * k
            px, py = -uy, ux
            d.polygon([b, (b[0] - ux * ah + px * ah * 0.55, b[1] - uy * ah + py * ah * 0.55),
                       (b[0] - ux * ah - px * ah * 0.55, b[1] - uy * ah - py * ah * 0.55)], fill=col)
        return layer


@dataclass(frozen=True)
class PathPrep:
    points: tuple[motion_paths.Point, ...]   # world pixels, smoothed when asked
    closed: bool
    color: Rgb
    fill: Rgb | None
    width: float
    W: int
    H: int


class PathKind(ElementKind):
    """A polyline or smooth curve through frame-fraction points, drawn on by arc length. It has no anchor."""
    FIELDS = frozenset({"points", "closed", "smooth", "color", "stroke_width", "fill"})
    PRESETS = anim.COMMON_PRESETS | {"draw"}
    REJECTS = POSITION_FIELDS
    POSITIONAL = False

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> PathPrep:
        W, H = scene.ctx.W, scene.ctx.H
        pts = [(float(px) * W, float(py) * H) for px, py in el["points"]]
        closed = bool(el.get("closed", False))
        if el.get("smooth"):
            pts = motion_paths.catmull_rom(pts, closed)
        fill = el.get("fill", "none")
        return PathPrep(tuple(pts), closed, scene.color(el.get("color", "fg")),
                        scene.color(fill) if fill != "none" and closed else None,
                        el.get("stroke_width", 5) * scene.ctx.u, W, H)

    def plan_geo(self, scene: "MotionScene", leaf: Leaf, q: QState, cam: Viewport, by_id: dict[str, Box]) -> Hashable:
        return (round(cam.zoom * anim.Q_SCALE), round(cam.fx * 4096), round(cam.fy * 4096))

    def _screen(self, prep: PathPrep, geo: Any) -> list[motion_paths.Point]:
        from .motion_camera import to_screen
        vp = Viewport(geo[0] / anim.Q_SCALE, geo[1] / 4096, geo[2] / 4096)
        return [to_screen(vp, x, y, prep.W, prep.H) for x, y in prep.points]

    def screen_box(self, prep: PathPrep, q: QState, geo: Any) -> Box | None:
        pts = self._screen(prep, geo)
        pad = prep.width * geo[0] / anim.Q_SCALE * 1.5
        return (min(x for x, _ in pts) - pad, min(y for _, y in pts) - pad,
                max(x for x, _ in pts) + pad, max(y for _, y in pts) + pad)

    def paint(self, scene: "MotionScene", canvas: Image.Image, leaf: Leaf, q: QState, look: Look, geo: Hashable) -> None:
        prep: PathPrep = leaf.prep
        box = self.screen_box(prep, q, geo)
        assert box is not None
        color = look.color or prep.color
        key = ("path", geo, q.dq, color, look.wq if look.color else FLASH_WIDTH_STEPS, prep.fill)
        scene.paint_free(canvas, leaf, q, box, key, lambda f, win: self._raster(prep, q, geo, look, color, f, win))

    def _raster(self, prep: PathPrep, q: QState, geo: Any, look: Look, color: Rgb, f: int,
                win: tuple[int, int, int, int]) -> Image.Image:
        zoom = geo[0] / anim.Q_SCALE
        mult = look.wq / FLASH_WIDTH_STEPS if look.color else 1.0
        layer = Image.new("RGBA", ((win[2] - win[0]) * f, (win[3] - win[1]) * f), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        pts = [((x - win[0]) * f, (y - win[1]) * f) for x, y in self._screen(prep, geo)]
        p = q.dq / anim.Q_DRAW
        fa = anim.clamp((p - 0.7) / 0.3) if q.dq < anim.Q_DRAW else 1.0
        if prep.fill is not None and fa > 0 and len(pts) >= 3:
            d.polygon(pts, fill=_rgba(prep.fill, int(255 * fa)))
        part = motion_paths.polyline_prefix(pts, p, prep.closed)
        if len(part) >= 2:
            d.line(part, fill=_rgba(color), width=max(1, int(prep.width * zoom * mult * f)), joint="curve")
        return layer


@dataclass
class ImagePrep:
    asset: MotionImage  # pre-scaled to the largest size it is drawn at, decoded on demand (motion_assets.py)
    w: float            # pixels at scale 1
    h: float
    radius: float

    @property
    def img(self) -> Image.Image:
        return self.asset.get()


class ImageKind(ElementKind):
    """A local picture, pre-scaled once, resampled for each scale it is drawn at."""
    FIELDS = frozenset({"source", "w", "h", "radius"})

    def prepare(self, el: dict[str, Any], scene: "MotionScene") -> ImagePrep:
        ctx = scene.ctx
        where = f"{scene.where}.source"
        asset = MotionImage(str(el["source"]), ctx.base_dir, ctx.roots, where, el.get("w"), el.get("h"), ctx.u,
                            scene.scale_bound(el), ctx.media)
        scene.add_image(asset, where)
        return ImagePrep(asset, asset.w, asset.h, el.get("radius", 0) * ctx.u)

    def bbox(self, prep: ImagePrep, q: QState) -> Box:
        hx, hy = _rot_extents(prep.w * q.scale / 2, prep.h * q.scale / 2, q.rotate)
        return (-hx, -hy, hx, hy)

    def paint(self, scene: "MotionScene", canvas: Image.Image, leaf: Leaf, q: QState, look: Look, geo: Hashable) -> None:
        prep: ImagePrep = leaf.prep
        W, H, m = scene.ctx.W, scene.ctx.H, _SS_MARGIN
        b = self.screen_box(prep, q, None)
        assert b is not None
        if b[2] <= 0 or b[0] >= W or b[3] <= 0 or b[1] >= H:
            return
        tw, th = max(1, round(prep.w * q.scale)), max(1, round(prep.h * q.scale))
        if q.rq == 0:
            # only the part of the image the frame shows (plus the margin) is resampled, however large it is drawn
            x0, y0 = q.ix - tw // 2, q.iy - th // 2
            win = (max(x0, -m), max(y0, -m), min(x0 + tw, W + m), min(y0 + th, H + m))
            if win[2] <= win[0] or win[3] <= win[1]:
                return
            rel = (win[0] - q.ix, win[1] - q.iy, win[2] - q.ix, win[3] - q.iy)
            key = ("img", leaf.node.index, q.sq, 0, rel)
            sprite = scene.lru_get(key)
            if sprite is None:
                iw, ih = prep.img.size
                src = ((win[0] - x0) * iw / tw, (win[1] - y0) * ih / th, (win[2] - x0) * iw / tw, (win[3] - y0) * ih / th)
                im = prep.img.resize((win[2] - win[0], win[3] - win[1]), Image.LANCZOS, box=src)
                if prep.radius > 0:
                    mask = _rounded_mask((tw, th), prep.radius * q.scale, (win[0] - x0, win[1] - y0, win[2] - x0, win[3] - y0))
                    im.putalpha(ImageChops.multiply(im.getchannel("A"), mask))
                scene._note_layer(im.width, im.height)
                sprite = scene.lru_put(key, (im, (win[0] - q.ix, win[1] - q.iy)), im.width * im.height * 4)
        else:
            hx, hy = _rot_extents(tw / 2, th / 2, q.rotate)
            if (2 * hx + 2) * (2 * hy + 2) > MAX_LAYER_PIXELS:   # the turned image would be too big: draw it smaller
                k = 0.98 * (MAX_LAYER_PIXELS / ((2 * hx + 2) * (2 * hy + 2))) ** 0.5   # rounding and the turn's edge
                scene.ctx.warn("a motion image is too large to draw at this size and is drawn smaller")
                tw, th = max(1, int(tw * k)), max(1, int(th * k))
            key = ("img", leaf.node.index, q.sq, q.rq, (tw, th))
            sprite = scene.lru_get(key)
            if sprite is None:
                im = prep.img.resize((tw, th), Image.LANCZOS) if prep.img.size != (tw, th) else prep.img.copy()
                if prep.radius > 0:
                    im.putalpha(ImageChops.multiply(im.getchannel("A"), _rounded_mask((tw, th), prep.radius * q.scale)))
                im = im.rotate(-q.rotate, Image.BICUBIC, expand=True)
                scene._note_layer(im.width, im.height)
                sprite = scene.lru_put(key, (im, (-(im.width // 2), -(im.height // 2))), im.width * im.height * 4)
        im, off = sprite
        scene.paste_with_opacity(canvas, im, (q.ix + off[0], q.iy + off[1]), q.oq)


def _resample_box(src: Image.Image, size: tuple[int, int], box: tuple[float, float, float, float]) -> Image.Image:
    """`src.resize(size, LANCZOS, box=box)` with the two passes in a fixed order (across, then down). Left to itself
    Pillow picks the order from the shapes, and the 8-bit intermediate clamps the filter's overshoot differently in
    each order, so a window of a mask and the whole mask would disagree by up to about 10% at glyph edges."""
    w, h = size
    across = src.resize((w, src.height), Image.LANCZOS, box=(box[0], 0, box[2], src.height))
    return across.resize((w, h), Image.LANCZOS, box=(0, box[1], w, box[3]))


def _rounded_mask(size: tuple[int, int], radius: float, window: tuple[int, int, int, int] | None = None) -> Image.Image:
    """An antialiased rounded-rectangle mask of a `size` image (drawn at 2x and reduced). With `window` (x0, y0, x1, y1
    inside that image) only that part is made."""
    x0, y0, x1, y1 = window or (0, 0, size[0], size[1])
    big = Image.new("L", ((x1 - x0) * 2, (y1 - y0) * 2), 0)
    ImageDraw.Draw(big).rounded_rectangle([-x0 * 2, -y0 * 2, size[0] * 2 - 1 - x0 * 2, size[1] * 2 - 1 - y0 * 2],
                                          int(radius * 2), fill=255)
    return big.resize((x1 - x0, y1 - y0), Image.LANCZOS)


KINDS: dict[str, ElementKind] = {"text": TextKind(), "rect": RectKind(), "circle": CircleKind(), "group": GroupKind(),
                                 "arrow": ArrowKind(), "path": PathKind(), "image": ImageKind()}


# --- the scene ------------------------------------------------------------------------------------------

class MotionScene(Scene):
    FIELDS: ClassVar[frozenset[str]] = frozenset({"elements", "stagger", "title", "flash", "quality", "camera"})

    _frame_cache = True   # test hook: False draws every frame from scratch

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        W, H, u = ctx.W, ctx.H, ctx.u
        self.f = QUALITY_FACTORS[spec.get("quality", "standard")]
        self.stagger = float(spec.get("stagger", anim.DEFAULT_STAGGER))
        self.title = ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * ctx.margin,
                               align="center") if spec.get("title") else None
        self.title_top = H * 0.09
        self._lru = _Lru(MOTION_CACHE_BYTES)
        self._last_img: Image.Image | None = None
        self._last_key: Hashable = None
        self._max_layer = 0
        self._luts: dict[int, list[int]] = {}
        self._image_bytes = 0
        self._images: list[MotionImage] = []   # decoded while drawn, given back by release()
        self.camera: list[CameraKey] = []
        for ck in spec.get("camera", []):
            fx, fy = ck.get("vfocus", ck.get("focus", [0.5, 0.5])) if ctx.vertical else ck.get("focus", [0.5, 0.5])
            self.camera.append(CameraKey(float(ck["at"]), float(ck.get("seconds", 0.8)), float(ck.get("zoom", 1.0)),
                                         float(fx), float(fy), ck.get("easing", "in_out")))
        self._smult = max_zoom(self.camera)       # the largest scale the camera and the groups above an element give it
        self.where = "elements[0]"
        self.leaves: dict[int, Leaf] = {}
        self.roots: list[Node] = []
        self.layers: dict[int, int] = {}          # node index -> effective layer
        self.root_of: dict[int, int] = {}         # node index -> index of its top-level element
        self.rest: list[tuple[str, Box]] = []     # (label, final rest box in pixels) of each top-level element
        self._index = 0
        elements = spec["elements"]
        times = anim.auto_times([el.get("at") for el in elements], self.stagger)
        for n, (el, at) in enumerate(zip(elements, times)):
            self.where = f"elements[{n}]"
            self.roots.append(self._build_element(el, at, None))
        self.flash = []
        for fl in spec.get("flash", []):
            tag = ctx.block(str(fl["text"]), "sans_bold", (36 if ctx.vertical else 30) * u, W * 0.4,
                            align="center") if fl.get("text") else None
            self.flash.append((fl["node"], float(fl["at"]), self.color(fl.get("color", "accent")), tag))
        self._flash_for: dict[int, tuple[int, ...]] = {}
        for index, leaf in self.leaves.items():
            self._flash_for[index] = tuple(n for n, fl in enumerate(self.flash) if fl[0] in leaf.chain)
        self._flash_leaves = [tuple(i for i, lf in self.leaves.items() if fl[0] in lf.chain) for fl in self.flash]
        self.positional = {i: lf.kind.POSITIONAL for i, lf in self.leaves.items()}

    # -- building ----------------------------------------------------------------------------------------

    def color(self, name: str) -> Rgb:
        return getattr(self.ctx.theme, name)

    def _px(self, el: dict[str, Any], axis: str) -> float | None:
        """A frame fraction of `el` (or of a move) in pixels; `vx`/`vy` win in vertical frames."""
        v = el.get("v" + axis) if self.ctx.vertical else None
        if v is None:
            v = el.get(axis)
        return None if v is None else v * (self.ctx.W if axis == "x" else self.ctx.H)

    def _next(self) -> int:
        self._index += 1
        return self._index - 1

    def scale_bound(self, el: dict[str, Any]) -> float:
        """The largest scale an element can reach: its own, a move's, a pop's overshoot, a pulse, then the camera and the
        groups above it."""
        top = max([float(el.get("scale", 1.0))] + [float(m["scale"]) for m in el.get("moves", []) if "scale" in m])
        pop = (el.get("enter") or {}).get("type") == "pop"
        return top * (1.1 if pop else 1.0) * (1.08 if el.get("emphasis") else 1.0) * self._smult

    def add_image(self, asset: MotionImage, where: str) -> None:
        self._image_bytes += asset.nbytes
        if self._image_bytes > MAX_IMAGE_BYTES:
            raise CaptureError(f"{where}: the images of this scene need {self._image_bytes // 2**20} MB; the limit is "
                               f"{MAX_IMAGE_BYTES // 2**20} MB (MAX_IMAGE_BYTES)")
        self._images.append(asset)

    def _build_element(self, el: dict[str, Any], at: float, parent: tuple[Node | None, tuple[str, ...], int, int] | None
                       ) -> Node:
        """The Node of one element (and of its children). `parent` is None for a top-level element, else
        (parent node, its flash ids, its layer, its root index); a child's x and y are offsets from its parent."""
        ctx = self.ctx
        kind = KINDS[el["kind"]]
        child = parent is not None
        enter_raw = el.get("enter") or {"type": "none" if child else anim.DEFAULT_ENTER}
        enter = anim.preset_timing("enter", enter_raw["type"], enter_raw.get("seconds"),
                                   enter_raw.get("easing") or el.get("easing"))
        end = el.get("end")
        exit_ = None
        if end is not None:
            ex = el.get("exit") or {"type": anim.DEFAULT_EXIT}
            exit_ = anim.preset_timing("exit", ex["type"], ex.get("seconds"), ex.get("easing") or el.get("easing"))
        pulse = None
        if el.get("emphasis"):
            em = el["emphasis"]
            pulse = anim.Pulse(float(em.get("at", anim.pulse_start(at, enter.seconds))),
                               float(em.get("seconds", anim.EMPHASIS_PRESETS["pulse"])), int(em.get("repeat", 1)))
        moves = tuple(anim.Move(float(m["at"]), float(m.get("seconds", 0.6)), m.get("easing", "in_out"),
                                self._px(m, "x"), self._px(m, "y"), m.get("scale"), m.get("rotate"), m.get("opacity"))
                      for m in el.get("moves", []))
        x, y = self._px(el, "x"), self._px(el, "y")
        if x is None:
            x = 0.0 if child or not kind.POSITIONAL else ctx.W / 2
        if y is None:
            y = 0.0 if child or not kind.POSITIONAL else ctx.H / 2
        scale, rotate, opacity = float(el.get("scale", 1.0)), float(el.get("rotate", 0.0)), float(el.get("opacity", 1.0))
        layer = el.get("layer")
        label = str(el.get("id") or (str(el["text"])[:30] if el["kind"] == "text" else el["kind"]))
        el_id = str(el["id"]) if el.get("id") is not None else None
        chain = (parent[1] if parent else ()) + ((el_id,) if el_id else ())
        eff_layer = layer if layer is not None else (parent[2] if parent else 0)
        common = dict(end=end, exit=exit_, emphasis=pulse, moves=moves, layer=layer)
        index = self._next()
        root = parent[3] if parent else index
        self.layers[index], self.root_of[index] = eff_layer, root
        outer = self._smult
        if el["kind"] == "group":   # what is inside can grow with it
            self._smult = outer * max([scale] + [float(m["scale"]) for m in el.get("moves", []) if "scale" in m])
        prep = kind.prepare(el, self)
        draws = enter.type == "draw"
        if el["kind"] == "group":
            node = self._group(el, index, at, enter, x, y, scale, rotate, opacity, common, chain, eff_layer, root, label)
        elif isinstance(prep, TextPrep) and el.get("split", "none") == "words":
            node = self._split_words(el, prep, index, at, enter, x, y, scale, rotate, opacity, common, chain, eff_layer,
                                     root, label)
        else:
            if isinstance(prep, TextPrep) and draws:
                prep.typing = True
            node = Node(index, el["kind"], at, enter, x, y, scale, rotate, opacity,
                        draw_steps=prep.chars if isinstance(prep, TextPrep) and draws else None, **common)
            self.leaves[index] = Leaf(node, kind, prep, el_id, chain, label)
        self._smult = outer
        self._check_times(node, label)
        if not child:
            box = self._rest_box(node, node.x, node.y, next((m.scale for m in reversed(node.moves) if m.scale is not None),
                                                            node.scale), moved=True)
            if box is not None:
                self.rest.append((label, box))
                if box[2] <= 0 or box[0] >= ctx.W or box[3] <= 0 or box[1] >= ctx.H:
                    ctx.warn(f"motion element {label!r} sits outside the frame at rest")
        return node

    def _group(self, el: dict[str, Any], index: int, at: float, enter: anim.Timing, x: float, y: float, scale: float,
               rotate: float, opacity: float, common: dict[str, Any], chain: tuple[str, ...], layer: int, root: int,
               label: str) -> Node:
        """A group: its children follow it. A child without `at` starts `stagger` after the one before it, the first
        with the group; a child's enter is `none` unless it says otherwise."""
        gstagger = float(el.get("stagger", 0.0))
        kids: list[Node] = []
        k = 0
        where = self.where
        for n, kid in enumerate(el["children"]):
            if "at" in kid:
                kid_at = float(kid["at"])
            else:
                kid_at = at + k * gstagger
                k += 1
            self.where = f"{where}.children[{n}]"
            kids.append(self._build_element(kid, kid_at, (None, chain, layer, root)))
        self.where = where
        return Node(index, "group", at, enter, x, y, scale, rotate, opacity, children=tuple(kids), **common)

    def _split_words(self, el: dict[str, Any], prep: TextPrep, index: int, at: float, enter: anim.Timing, x: float,
                     y: float, scale: float, rotate: float, opacity: float, common: dict[str, Any],
                     chain: tuple[str, ...], layer: int, root: int, label: str) -> Node:
        """A `split: words` text is a group: the line keeps the moves, the pulse and the exit, each word gets the
        enter preset and starts `stagger` after the word before it."""
        words = prep.text.split()
        fnt = font(prep.font_kind, prep.blk.size, prep.text)
        boxes, block_w, block_h = layout_words(words, fnt, self.ctx.W * el.get("max_w", 0.84))
        starts = anim.word_times(at, len(words), float(el.get("stagger", anim.WORD_STAGGER)))
        kids: list[Node] = []
        for box, word, w_at in zip(boxes, words, starts):
            w_prep = self.text_prep(word, el.get("font", "display"), el.get("size", 64), 10.0, "center", prep.color, el,
                                    px=prep.blk.size)   # the size the line was laid out at, which may have shrunk
            w_index = self._next()
            self.layers[w_index], self.root_of[w_index] = layer, root
            node = Node(w_index, "text", w_at, enter, box.x + box.w / 2 - block_w / 2, box.y + box.h / 2 - block_h / 2)
            kids.append(node)
            self.leaves[w_index] = Leaf(node, KINDS["text"], w_prep, None, chain, label)
        parent = Node(index, "text", at, anim.preset_timing("enter", "none"), x, y, scale, rotate, opacity,
                      children=tuple(kids), **common)
        return parent

    def text_prep(self, text: str, font_kind: str, size: float, max_w: float, align: str, color: Rgb,
                  el: dict[str, Any], px: int | None = None) -> TextPrep:
        """The block of a text (`size` is in u; `max_w` a fraction of the frame width, or 10 for 'unlimited'; `px`
        fixes the size in pixels, for the words of a split text)."""
        ctx = self.ctx
        kind = {"display": ctx.display, "body": ctx.body, "mono": "mono"}[font_kind]
        size_px = px or int(round(size * ctx.u * ctx.fs))   # whole pixels, so the shrink loop and the font agree
        width = ctx.W * max_w
        blk = ctx.block(text, kind, size_px, width, align=align)
        lines = wrap(text, font(kind, blk.size, text), width)
        s_max = self.scale_bound(el)
        return TextPrep(blk, kind, text, lines, align, color, s_max, sum(len(ln) for ln in lines), False)

    def _check_times(self, node: Node, label: str) -> None:
        """Warnings about timing that is legal but probably not what the author meant."""
        ctx, dur = self.ctx, self.duration
        if node.at >= dur:
            ctx.warn(f"motion element {label!r} enters at {_fmt_s(round(node.at, 3))}s, after the {_fmt_s(dur)}s scene ends")
        for m in node.moves:
            if m.at >= dur:
                ctx.warn(f"motion element {label!r} moves at {_fmt_s(round(m.at, 3))}s, after the {_fmt_s(dur)}s scene ends")
        if node.end is not None and node.end >= dur:
            ctx.warn(f"motion element {label!r} exits at {_fmt_s(round(node.end, 3))}s, after the {_fmt_s(dur)}s scene ends")
        x, y = node.x, node.y
        for m in node.moves:
            nx, ny = (x if m.x is None else m.x), (y if m.y is None else m.y)
            dist = math.hypot(nx - x, ny - y)
            if m.seconds > 1 and 0 < dist / (m.seconds * 30) < 0.5:
                ctx.warn(f"motion element {label!r} moves less than half a pixel per frame for over a second; "
                         "it will step visibly")
            x, y = nx, ny

    def _rest_box(self, node: Node, x: float, y: float, scale: float, moved: bool = False) -> Box | None:
        """The box of an element (its children included) at rest, in pixels: `x`, `y` and `scale` are its own anchor
        and size. None for kinds without an anchor (arrows and paths)."""
        if moved and node.moves:
            for m in node.moves:
                x, y = (x if m.x is None else m.x), (y if m.y is None else m.y)
        if node.children:
            boxes = [self._rest_box(c, x + c.x * scale, y + c.y * scale, scale * c.scale) for c in node.children]
            boxes = [b for b in boxes if b is not None]
            if not boxes:
                return None
            return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        leaf = self.leaves.get(node.index)
        if leaf is None or not leaf.kind.POSITIONAL:
            return None
        b = leaf.kind.bbox(leaf.prep, QState(node, 0, 0, int(round(scale * anim.Q_SCALE)), 0, anim.Q_OPACITY, anim.Q_DRAW))
        return (x + b[0], y + b[1], x + b[2], y + b[3])

    def layout_warnings(self, caption: tuple[float, float, float, float] | None) -> list[str]:
        """The title and the caption pill each cover whatever element sits under them at rest. Warn, never move."""
        c = self.ctx
        areas: list[tuple[str, Box]] = []
        if self.title:
            x0 = c.W / 2 - self.title.width / 2
            areas.append(("the title", (x0, self.title_top, x0 + self.title.width, self.title_top + self.title.height)))
        if caption is not None:
            areas.append(("the caption", caption))
        out = []
        for name, (ax0, ay0, ax1, ay1) in areas:
            for label, (bx0, by0, bx1, by1) in self.rest:
                if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                    out.append(f"{name} overlaps motion element {label!r}; move the element "
                               f"{'up' if name == 'the caption' else 'down'} ({'vy' if c.vertical else 'y'})")
        return out

    # -- text masks ---------------------------------------------------------------------------------------

    def _draw_lines(self, prep: TextPrep, size: int, width: int, lines: list[str], limit: int | None) -> Image.Image:
        """The text of `prep` at `size`, laid out as make_block lays it out; `limit` shows only that many characters."""
        fnt = font(prep.font_kind, size, prep.text)
        lh = int(size * 1.28)
        mask = Image.new("L", (max(width, 1), max(lh * len(lines) + int(size * 0.3), 1)), 0)
        d = ImageDraw.Draw(mask)
        left = limit
        for i, line in enumerate(lines):
            if left is not None and left <= 0:
                break
            lw = fnt.getlength(line)
            x = {"left": 0, "center": (width - lw) / 2, "right": width - lw}[prep.align]
            d.text((x, i * lh), line if left is None else line[:left], font=fnt, fill=255)
            if left is not None:
                left -= len(line)
        return mask

    def _typed(self, leaf: Leaf, count: int) -> Image.Image:
        prep: TextPrep = leaf.prep
        key = ("type", leaf.node.index, count)
        hit = self._lru.get(key)
        if hit is _MISS:
            hit = self._draw_lines(prep, prep.blk.size, prep.blk.width, prep.lines, count)
            self._note_layer(hit.width, hit.height)
            self._lru.put(key, hit, hit.width * hit.height)
        return hit

    def _hi(self, leaf: Leaf) -> Image.Image:
        """The mask scaled drawing resamples: the text at `s_max` times its size, capped at TEXT_SUPER_MAX and
        MAX_HI_MASK_PIXELS (past the cap a scaled-up text is upscaled and softer, with one warning)."""
        prep: TextPrep = leaf.prep
        key = ("hi", leaf.node.index)
        hit = self._lru.get(key)
        if hit is not _MISS:
            return hit
        cap = min(TEXT_SUPER_MAX, math.sqrt(MAX_HI_MASK_PIXELS / max(1, prep.blk.width * prep.blk.height)))
        s = max(1.0, min(prep.s_max, cap))
        if prep.s_max > cap + 1e-9:
            self.ctx.warn(f"motion text {leaf.label!r} scales up to {prep.s_max:.1f}x, past the sharp limit; it will "
                          "look soft at its largest")
        prep.super_s = s
        if s <= 1.0:
            hit = prep.blk.mask
        else:
            size = int(round(prep.blk.size * s))
            fnt = font(prep.font_kind, size, prep.text)
            width = int(max((fnt.getlength(ln) for ln in prep.lines), default=0)) + 4
            hit = self._draw_lines(prep, size, width, prep.lines, None)
            self._note_layer(hit.width, hit.height)
        self._lru.put(key, hit, hit.width * hit.height)
        return hit

    def _note_layer(self, w: int, h: int) -> None:
        """Record a raster allocation (a layer, a mask or an image sprite) for `max_layer_pixels`."""
        self._max_layer = max(self._max_layer, w * h)

    def text_mask(self, leaf: Leaf, q: QState, typed: bool) -> tuple[Image.Image, tuple[int, int]] | None:
        """The mask of a text that is typing, scaled or turned, and where to paste it (its top-left on the frame), or
        None when none of it shows. Only the part inside the frame plus the filter margin is resampled, so the mask
        never holds more than the frame can show however large the scale composes to."""
        prep: TextPrep = leaf.prep
        W, H, m = self.ctx.W, self.ctx.H, _SS_MARGIN
        s = q.scale
        mw, mh = prep.blk.mask.size
        tw, th = max(1, int(round(mw * s))), max(1, int(round(mh * s)))
        bw, bh = max(1, int(round(prep.blk.width * s))), max(1, int(round(prep.blk.height * s)))
        if q.rq == 0:
            ox, oy = int(q.ix - bw / 2), int(q.iy - bh / 2)       # where blit puts the block
            vis = (max(ox, -m), max(oy, -m), min(ox + tw, W + m), min(oy + th, H + m))
            if vis[2] <= vis[0] or vis[3] <= vis[1]:
                return None
            rel = (vis[0] - ox, vis[1] - oy, vis[2] - ox, vis[3] - oy)
            whole = rel == (0, 0, tw, th)
            key = ("mask", leaf.node.index, q.sq, 0, q.dq if typed else -1, None if whole else rel)
            hit = self._lru.get(key)
            if hit is not _MISS:
                return hit, (vis[0], vis[1])
            src = self._typed(leaf, q.dq) if typed else self._hi(leaf)
            if whole and src.size == (tw, th):
                mask = src
            else:
                sw, sh = src.size
                box = (rel[0] * sw / tw, rel[1] * sh / th, rel[2] * sw / tw, rel[3] * sh / th)
                mask = _resample_box(src, (rel[2] - rel[0], rel[3] - rel[1]), box)
            self._note_layer(mask.width, mask.height)
            self._lru.put(key, mask, mask.width * mask.height)
            return mask, (vis[0], vis[1])
        # turned about the middle of the block (the mask has room for descenders, so it is padded to put the block's
        # middle in its middle)
        eh = 2 * th - bh
        hx, hy = _rot_extents(tw / 2, eh / 2, q.rotate)
        if q.ix + hx <= -m or q.ix - hx >= W + m or q.iy + hy <= -m or q.iy - hy >= H + m:
            return None
        # One path for every turned text: an affine sample of the source mask over the window the frame shows, kept
        # relative to the anchor, so a text that is cut by the frame edge draws the same pixels as when it is whole
        # (the earlier whole-mask path resized, rotated with expand and truncated its paste: up to 3 px off).
        src = self._typed(leaf, q.dq) if typed else self._hi(leaf)
        rel = (max(math.floor(-hx), -m - q.ix), max(math.floor(-hy), -m - q.iy),
               min(math.ceil(hx), W + m - q.ix), min(math.ceil(hy), H + m - q.iy))
        key = ("maskw", leaf.node.index, q.sq, q.rq, q.dq if typed else -1, rel)
        hit = self._lru.get(key)
        if hit is _MISS:
            sw, sh = src.size
            a = math.radians(q.rotate)
            c, sn = math.cos(a), math.sin(a)
            kx, ky = sw / tw, sh / th
            x0, y0 = rel[0], rel[1]           # Pillow samples output pixel centres itself (no extra +0.5)
            data = (kx * c, kx * sn, kx * (c * x0 + sn * y0 + tw / 2),
                    -ky * sn, ky * c, ky * (-sn * x0 + c * y0 + eh / 2 - (th - bh)))
            hit = src.transform((rel[2] - rel[0], rel[3] - rel[1]), Image.AFFINE, data, Image.BICUBIC)
            self._note_layer(hit.width, hit.height)
            self._lru.put(key, hit, hit.width * hit.height)
        return hit, (q.ix + rel[0], q.iy + rel[1])

    # -- sprites ------------------------------------------------------------------------------------------

    def _window(self, ix: int, iy: int, box: Box, f: int
                ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
        """(the sprite's layer window, the window it would have without clipping), both in 1x pixels relative to the
        anchor (ix, iy); None when the box is entirely outside the frame. The layer is the box plus SPRITE_PAD, clipped
        to the frame plus the filter margin, so it never holds more than the frame can show."""
        W, H = self.ctx.W, self.ctx.H
        if ix + box[2] <= 0 or ix + box[0] >= W or iy + box[3] <= 0 or iy + box[1] >= H:
            return None
        pad = SPRITE_PAD if f > 1 else 2
        full = (math.floor(box[0]) - pad, math.floor(box[1]) - pad, math.ceil(box[2]) + pad, math.ceil(box[3]) + pad)
        m = _SS_MARGIN
        win = (max(full[0], -m - ix), max(full[1], -m - iy), min(full[2], W + m - ix), min(full[3], H + m - iy))
        return win, full

    def paint_sprite(self, canvas: Image.Image, leaf: Leaf, q: QState, look: Look) -> None:
        kind, prep = leaf.kind, leaf.prep
        self._paint(canvas, leaf.node.index, q.ix, q.iy, kind.bbox(prep, q), kind.shape_key(prep, q, look), q.oq,
                    lambda f, win: kind.raster(prep, q, look, f, win))

    def paint_free(self, canvas: Image.Image, leaf: Leaf, q: QState, box: Box, key: Hashable,
                   make: Callable[[int, tuple[int, int, int, int]], Image.Image]) -> None:
        """Paint a kind that has no anchor: `box` is in screen pixels and `make(f, win)` draws the layer of `win`
        (screen pixels) at f times the size."""
        self._paint(canvas, leaf.node.index, 0, 0, box, key, q.oq, make)

    def _paint(self, canvas: Image.Image, index: int, ix: int, iy: int, box: Box, shape: Hashable, oq: int,
               make: Callable[[int, tuple[int, int, int, int]], Image.Image]) -> None:
        f = self.f
        while True:
            placed = self._window(ix, iy, box, f)
            if placed is None:
                return
            win, full = placed
            if (win[2] - win[0]) * (win[3] - win[1]) * f * f <= MAX_LAYER_PIXELS or f == 1:
                break
            f -= 1
            self.ctx.warn("a motion element is too large to supersample at this quality; it is drawn at a lower factor")
        key = (index, shape, f, None if win == full else win)
        sprite = self._lru.get(key)
        if sprite is _MISS:
            layer = make(f, win)
            self._max_layer = max(self._max_layer, layer.width * layer.height)
            if f == 1:
                sprite = (layer, (0, 0)) if layer.getbbox() else None
            else:
                small = downsample(layer, f)
                sprite = None if small is None else (small[0], small[1])
            self._lru.put(key, sprite, 0 if sprite is None else sprite[0].width * sprite[0].height * 4)
        if sprite is None:
            return
        img, off = sprite
        self.paste_with_opacity(canvas, img, (ix + win[0] + off[0], iy + win[1] + off[1]), oq)

    def paste_with_opacity(self, canvas: Image.Image, img: Image.Image, pos: tuple[int, int], oq: int) -> None:
        if oq >= anim.Q_OPACITY:
            canvas.paste(img, pos, img)
        else:
            lut = self._luts.get(oq)
            if lut is None:
                lut = self._luts[oq] = [int(v * oq / anim.Q_OPACITY + 0.5) for v in range(256)]
            canvas.paste(img, pos, img.getchannel("A").point(lut))

    def lru_get(self, key: Hashable) -> Any:
        hit = self._lru.get(key)
        return None if hit is _MISS else hit

    def lru_put(self, key: Hashable, value: Any, nbytes: int) -> Any:
        self._lru.put(key, value, nbytes)
        return value

    # -- frames -------------------------------------------------------------------------------------------

    def _look(self, index: int, t: float) -> Look:
        active = [n for n in self._flash_for.get(index, ()) if t >= self.flash[n][1]]
        if not active:
            return NO_LOOK
        _, at, color, _ = self.flash[active[-1]]
        mult = 1 + FLASH_WIDTH_GAIN * max(0.0, 1 - (t - at) / FLASH_PULSE_SECONDS)
        return Look(color, int(math.floor(mult * FLASH_WIDTH_STEPS + 0.5)))

    def _apply_camera(self, states: list[QState], cam: Viewport) -> list[QState]:
        """The states as the camera sees them: anchors, offsets and scales are changed before anything is drawn. A
        member keeps its offset from its top-level element (scaled), so a card and its label stay together."""
        W, H = self.ctx.W, self.ctx.H
        z, fx, fy = cam.zoom, cam.fx * W, cam.fy * H
        root_at = {q.node.index: (q.ix, q.iy) for q in states if self.root_of[q.node.index] == q.node.index}
        root_to: dict[int, tuple[int, int]] = {}
        out: list[QState] = []
        for q in states:
            if not self.positional.get(q.node.index, True):
                out.append(q)
                continue
            r = self.root_of[q.node.index]
            if r not in root_to:
                rx, ry = root_at[r]
                root_to[r] = (_round_half_up((rx - fx) * z + W / 2), _round_half_up((ry - fy) * z + H / 2))
            rx, ry = root_at[r]
            sx, sy = root_to[r]
            out.append(replace(q, ix=sx + _round_half_up((q.ix - rx) * z), iy=sy + _round_half_up((q.iy - ry) * z),
                               sq=_round_half_up(q.sq * z)))
        return out

    def _plan(self, t: float) -> list[PlanItem]:
        """The drawn leaves at `t`, in draw order, each with its quantised state (camera applied), flash look,
        geometry and screen box."""
        states = anim.quantize(anim.resolve(self.roots, t, anim.SLIDE_DISTANCE * self.ctx.u))
        cam = camera_at(self.camera, t)
        if not cam.identity:
            states = self._apply_camera(states, cam)
        items = [PlanItem(q, self.leaves[q.node.index], self._look(q.node.index, t))
                 for q in states if q.node.index in self.leaves]
        by_id: dict[str, Box] = {}
        for it in items:   # anchored elements first: an arrow is drawn between their boxes
            if it.leaf.kind.POSITIONAL:
                it.box = it.leaf.kind.screen_box(it.leaf.prep, it.q, None)
                for name in it.leaf.chain:
                    b = by_id.get(name)
                    by_id[name] = it.box if b is None or it.box is None else (
                        min(b[0], it.box[0]), min(b[1], it.box[1]), max(b[2], it.box[2]), max(b[3], it.box[3]))
        for it in items:
            if not it.leaf.kind.POSITIONAL:
                it.geo = it.leaf.kind.plan_geo(self, it.leaf, it.q, cam, by_id)
                it.box = it.leaf.kind.screen_box(it.leaf.prep, it.q, it.geo)
        items.sort(key=lambda it: (self.layers[it.q.node.index], it.q.node.index))
        return items

    def placements(self, t: float) -> list[Placement]:
        """Where each visible element is anchored at `t`, in draw order (the pixel a text is blitted at)."""
        return [Placement(it.leaf.el_id or f"{it.leaf.node.kind}{it.leaf.node.index}", it.leaf.node.kind, it.q.ix, it.q.iy)
                for it in self._plan(t)]

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        plan = self._plan(t)
        ta = int(math.floor(255 * _appear(t, 0.05) + 0.5)) if self.title else 0
        tags = []
        for n, (_, at, _, tag) in enumerate(self.flash):
            if tag is not None and t >= at:
                tags.append((n, int(math.floor(255 * _appear(t, at, FLASH_TAG_SECONDS) + 0.5))))
        key = (anim.state_key([it.q for it in plan]), tuple((it.look.color, it.look.wq, it.geo) for it in plan), ta,
               tuple(tags))
        if self._frame_cache and self._last_img is not None and key == self._last_key:
            return self._last_img.copy()   # callers draw the caption, subtitles and badge onto what they get
        canvas = c.background()
        if self.title and ta:
            blit(canvas, self.title, (c.W / 2, self.title_top), c.theme.fg, ta / 255, "tc")
        boxes: dict[int, Box] = {}
        for it in plan:
            it.leaf.kind.paint(self, canvas, it.leaf, it.q, it.look, it.geo)
            if it.box is not None:
                boxes[it.q.node.index] = it.box
        for n, alpha in tags:
            shown = [boxes[i] for i in self._flash_leaves[n] if i in boxes]
            if not shown or alpha <= 0:
                continue
            x0, y0 = min(b[0] for b in shown), min(b[1] for b in shown)
            x1, y1 = max(b[2] for b in shown), max(b[3] for b in shown)
            _, _, color, tag = self.flash[n]
            assert tag is not None
            if c.vertical:   # arrows run vertically in a vertical frame, so the tag goes beside the element
                blit(canvas, tag, (x1 + 22 * c.u, (y0 + y1) / 2), color, alpha / 255, "ml")
            else:
                blit(canvas, tag, ((x0 + x1) / 2, y1 + 20 * c.u), color, alpha / 255, "tc")
        if self._frame_cache:
            self._last_img, self._last_key = canvas.copy(), key
        return canvas

    # -- memory -------------------------------------------------------------------------------------------

    def retained_bytes(self) -> int:
        """Bytes this scene holds on to: the LRU of sprites and masks, the last frame and the decoded images."""
        last = 0 if self._last_img is None else self._last_img.width * self._last_img.height * 3
        return self._lru.bytes + last + sum(a.nbytes for a in self._images if a.loaded)

    def max_layer_pixels(self) -> int:
        """The most pixels any raster layer has had so far (always at most MAX_LAYER_PIXELS)."""
        return self._max_layer

    def cached_layers(self) -> int:
        return super().cached_layers() + len(self._lru)

    def release(self) -> None:
        super().release()
        self._lru.clear()
        self._last_img = None
        self._last_key = None
        for asset in self._images:
            asset.drop()
