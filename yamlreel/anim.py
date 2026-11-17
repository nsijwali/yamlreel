"""The motion timeline: what every element of a `motion` scene looks like at scene-local time `t`.

Everything here is a pure function of the element tree and `t` (no PIL, no RNG, no frame counter, no state kept
between calls), so any frame can be drawn on its own, in any order. The scene turns the result into pixels.

An element has a start `at`, an enter preset, optional `moves`, an optional `emphasis` pulse, and an optional `end`
with an exit preset. `resolve` applies them in a fixed order (see `_own`), then composes group members onto their
parent. `quantize` snaps the result to the steps the caches are keyed on, so equal keys always draw equal pixels.

Easing: YAML offers exactly the four names of `textkit.EASINGS`. A per-element `easing` key overrides the "easing is
internal" rule of the 2026-11-03 spec on purpose: presets need an override to stay one word in YAML. `textkit.py`
itself is unchanged.

Units: positions are pixels here. The scene converts the spec's frame fractions and passes `slide` (60 * u).
Rotation is in degrees and positive turns clockwise on screen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .textkit import EASINGS, ease

MAX_ELEMENTS = 60        # drawable leaves in one scene (groups and split words expanded)
MAX_MOVES = 8
MAX_SPLIT_WORDS = 40
MAX_MOTION_TEXT = 300
MAX_GROUP_CHILDREN = 20
MAX_GROUP_DEPTH = 3
AUTO_START = 0.35        # a top-level element without `at` starts here, then every scene `stagger` later
DEFAULT_STAGGER = 0.4    # the scene's `stagger`
WORD_STAGGER = 0.08      # the `stagger` of a `split: words` text
SLIDE_DISTANCE = 60.0    # in u: how far a slide preset travels
POP_FROM = 0.6
PULSE_AMOUNT = 0.08
EMPHASIS_DELAY = 0.3     # a pulse without `at` starts this long after the enter ends

EASING_NAMES = tuple(EASINGS)

# name -> (seconds, easing)
ENTER_PRESETS: dict[str, tuple[float, str]] = {
    "none": (0.0, "out"), "fade": (0.4, "out"), "slide-up": (0.5, "out"), "slide-down": (0.5, "out"),
    "slide-left": (0.5, "out"), "slide-right": (0.5, "out"), "pop": (0.45, "back"), "draw": (0.8, "in_out")}
EXIT_PRESETS: dict[str, tuple[float, str]] = {
    "fade-out": (0.35, "in_out"), "slide-out": (0.4, "in_out"), "pop-out": (0.3, "in_out")}
EMPHASIS_PRESETS: dict[str, float] = {"pulse": 0.6}   # name -> seconds per cycle
DEFAULT_ENTER = "fade"
DEFAULT_EXIT = "fade-out"
COMMON_PRESETS = frozenset(set(ENTER_PRESETS) - {"draw"}) | frozenset(EXIT_PRESETS) | frozenset(EMPHASIS_PRESETS)

# quantisation steps (what every cache is keyed on)
Q_SCALE = 512
Q_ROTATE = 20            # steps per degree: 0.05 degree
Q_OPACITY = 255
Q_DRAW = 1024


@dataclass(frozen=True)
class Timing:
    type: str
    seconds: float
    easing: str


@dataclass(frozen=True)
class Move:
    at: float
    seconds: float
    easing: str
    x: float | None = None
    y: float | None = None
    scale: float | None = None
    rotate: float | None = None
    opacity: float | None = None


@dataclass(frozen=True)
class Pulse:
    at: float
    seconds: float
    repeat: int


@dataclass(frozen=True)
class Node:
    """One element, in pixels. A child's `x`/`y` are an offset from its parent's anchor."""
    index: int                       # document order (depth first); the draw order within a layer
    kind: str
    at: float
    enter: Timing
    x: float
    y: float
    scale: float = 1.0
    rotate: float = 0.0
    opacity: float = 1.0
    end: float | None = None
    exit: Timing | None = None
    emphasis: Pulse | None = None
    moves: tuple[Move, ...] = ()
    layer: int | None = None
    children: tuple["Node", ...] = ()
    draw_steps: int | None = None    # a type-on text: the number of characters, so draw progress is whole characters


@dataclass(frozen=True)
class State:
    """A visible element at one time, in world pixels (parents already applied)."""
    node: Node
    root: int                        # index of the top-level ancestor
    x: float
    y: float
    scale: float
    rotate: float
    opacity: float
    draw: float                      # 0..1, how much of a `draw` enter is done (1 when there is none)


@dataclass(frozen=True)
class QState:
    """A State snapped to its quantisation steps. Drawing reads only these values."""
    node: Node
    ix: int
    iy: int
    sq: int
    rq: int
    oq: int
    dq: int

    @property
    def scale(self) -> float:
        return self.sq / Q_SCALE

    @property
    def rotate(self) -> float:
        return self.rq / Q_ROTATE

    @property
    def opacity(self) -> float:
        return self.oq / Q_OPACITY

    @property
    def draw(self) -> float:
        steps = self.node.draw_steps
        return self.dq / (steps if steps else Q_DRAW)

    @property
    def key(self) -> tuple[int, int, int, int, int, int, int]:
        return (self.node.index, self.ix, self.iy, self.sq, self.rq, self.oq, self.dq)


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


# --- timing the validator and the renderer share --------------------------------------------------------

def preset_timing(group: str, name: str, seconds: float | None = None, easing: str | None = None) -> Timing:
    """The Timing of an enter (`group` "enter") or exit preset, with `seconds` and `easing` overriding its defaults."""
    table = ENTER_PRESETS if group == "enter" else EXIT_PRESETS
    secs, ease_name = table[name]
    if name == "none":
        return Timing(name, 0.0, easing or ease_name)
    return Timing(name, float(secs if seconds is None else seconds), easing or ease_name)


def auto_times(ats: Sequence[float | None], stagger: float) -> list[float]:
    """The start of each top-level element: its own `at`, else AUTO_START + k * stagger, where k counts the
    earlier elements that have no `at`."""
    out: list[float] = []
    k = 0
    for at in ats:
        if at is None:
            out.append(AUTO_START + k * stagger)
            k += 1
        else:
            out.append(float(at))
    return out


def word_times(at: float, count: int, stagger: float) -> list[float]:
    """The start of each word of a `split: words` text."""
    return [at + k * stagger for k in range(count)]


def pulse_start(at: float, enter_seconds: float) -> float:
    return at + enter_seconds + EMPHASIS_DELAY


def timing_problem(at: float, enter_seconds: float, end: float | None, moves: Sequence[tuple[float, float]],
                   pulse: tuple[float | None, float, int] | None) -> tuple[str, str] | None:
    """The first timing rule an element breaks, as (field path under the element, message), or None.
    `moves` are (at, seconds); `pulse` is (at or None, seconds per cycle, repeat)."""
    def secs(x: float) -> str:
        return f"{round(x, 3)}s"

    entered = at + enter_seconds
    if end is not None and end < entered:
        return "end", f"{secs(end)} is before the element has entered ({secs(entered)})"
    prev_end = entered
    for n, (m_at, m_seconds) in enumerate(moves):
        if m_at < prev_end - 1e-9:
            what = "the element has entered" if n == 0 else "the move before it ends"
            return f"moves[{n}].at", f"starts at {secs(m_at)}, before {what} ({secs(prev_end)})"
        prev_end = m_at + m_seconds
    if pulse is not None:
        p_at = pulse[0] if pulse[0] is not None else pulse_start(at, enter_seconds)
        if p_at < entered - 1e-9:
            return "emphasis.at", f"starts at {secs(p_at)}, before the element has entered ({secs(entered)})"
        p_end = p_at + pulse[1] * pulse[2]
        if end is not None and p_end > end + 1e-9:
            return "emphasis", f"ends at {secs(p_end)}, after the element ends ({secs(end)})"
    return None


# --- the state function ---------------------------------------------------------------------------------

def _base(n: Node, t: float) -> tuple[float, float, float, float, float]:
    """The rest state changed by `moves`, in time order. Fields a move does not set carry over."""
    cur = (n.x, n.y, n.scale, n.rotate, n.opacity)
    for m in n.moves:
        tgt = (cur[0] if m.x is None else m.x, cur[1] if m.y is None else m.y,
               cur[2] if m.scale is None else m.scale, cur[3] if m.rotate is None else m.rotate,
               cur[4] if m.opacity is None else m.opacity)
        if t < m.at:
            break
        if t >= m.at + m.seconds:
            cur = tgt
            continue
        p = ease(m.easing, (t - m.at) / m.seconds)
        return tuple(a * (1 - p) + b * p for a, b in zip(cur, tgt))   # type: ignore[return-value]
    return cur


def _own(n: Node, t: float, slide: float) -> tuple[float, float, float, float, float, float] | None:
    """x, y, scale, rotate, opacity, draw of `n` in its parent's frame, or None while it is hidden. The order is
    fixed: moves, enter, emphasis, exit; opacity is clamped to [0, 1] after every step."""
    if t < n.at:
        return None
    x, y, s, r, o = _base(n, t)
    o = clamp(o)
    draw = 1.0
    e = n.enter
    if e.type != "none":
        lin = clamp((t - n.at) / e.seconds)
        p = ease(e.easing, lin)
        if e.type == "fade":
            o = clamp(o * p)
        elif e.type == "slide-up":
            y += (1 - p) * slide
            o = clamp(o * p)
        elif e.type == "slide-down":
            y -= (1 - p) * slide
            o = clamp(o * p)
        elif e.type == "slide-left":
            x += (1 - p) * slide
            o = clamp(o * p)
        elif e.type == "slide-right":
            x -= (1 - p) * slide
            o = clamp(o * p)
        elif e.type == "pop":
            s *= POP_FROM + (1 - POP_FROM) * p
            o = clamp(o * min(1.0, 2 * lin))
        elif e.type == "draw":
            draw = clamp(p)
    em = n.emphasis
    if em is not None and em.at <= t < em.at + em.repeat * em.seconds:
        c = ((t - em.at) / em.seconds) % 1.0
        s *= 1 + PULSE_AMOUNT * math.sin(math.pi * c)
    if n.end is not None:
        ex = n.exit or preset_timing("exit", DEFAULT_EXIT)
        if t >= n.end + ex.seconds:
            return None
        if t >= n.end:
            q = ease(ex.easing, clamp((t - n.end) / ex.seconds))
            if ex.type == "fade-out":
                o = clamp(o * (1 - q))
            elif ex.type == "slide-out":
                y += q * slide
                o = clamp(o * (1 - q))
            elif ex.type == "pop-out":
                s *= 1 - 0.4 * q
                o = clamp(o * (1 - q))
    return x, y, s, r, o, draw


def resolve(roots: Sequence[Node], t: float, slide: float = SLIDE_DISTANCE) -> list[State]:
    """Every visible element at `t`, parents before their children, in document order."""
    out: list[State] = []
    for r in roots:
        _walk(r, None, r.index, t, slide, out)
    return out


def _walk(n: Node, parent: State | None, root: int, t: float, slide: float, out: list[State]) -> None:
    own = _own(n, t, slide)
    if own is None:
        return
    x, y, s, r, o, d = own
    if parent is None:
        st = State(n, root, x, y, s, r, o, d)
    else:
        a = math.radians(parent.rotate)
        ca, sa = math.cos(a), math.sin(a)
        ox, oy = x * parent.scale, y * parent.scale
        st = State(n, root, parent.x + ca * ox - sa * oy, parent.y + sa * ox + ca * oy,
                   parent.scale * s, parent.rotate + r, parent.opacity * o, d)
    out.append(st)
    for ch in n.children:
        _walk(ch, st, root, t, slide, out)


def quantize(states: Sequence[State]) -> list[QState]:
    """Snap states to whole pixels and the quantisation steps. A root is rounded to a pixel; a member sits at its
    root's pixel plus its rounded offset, so a card and its label never move against each other."""
    roots = {s.node.index: s for s in states if s.node.index == s.root}
    root_px = {i: (_round(s.x), _round(s.y)) for i, s in roots.items()}
    out: list[QState] = []
    for s in states:
        if s.node.index == s.root:
            ix, iy = root_px[s.root]
        else:
            rs = roots[s.root]
            rx, ry = root_px[s.root]
            ix, iy = rx + _round(s.x - rs.x), ry + _round(s.y - rs.y)
        steps = s.node.draw_steps
        dq = _round(s.draw * Q_DRAW) if not steps else int(math.floor(clamp(s.draw) * steps + 1e-9))
        out.append(QState(s.node, ix, iy, _round(s.scale * Q_SCALE), _round(s.rotate * Q_ROTATE),
                          _round(clamp(s.opacity) * Q_OPACITY), dq))
    return out


def state_key(states: Sequence[QState]) -> tuple[tuple[int, ...], ...]:
    """Equal keys mean equal pictures."""
    return tuple(q.key for q in states)
