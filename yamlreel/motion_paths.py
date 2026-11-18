"""Path geometry for `motion` scenes: arc length, draw-on prefixes, Catmull-Rom smoothing, dashes and arrow clipping.

Pure functions on lists of (x, y) pixel points; no PIL, no state."""
from __future__ import annotations

import math
from typing import Sequence

Point = tuple[float, float]

MAX_PATH_POINTS = 200      # points of a `path` element
MAX_PATH_SAMPLES = 2000    # points after smoothing
ARROW_GAP = 10.0           # in u: how far an arrow stops short of the box it points at (as diagram does)


def polyline_length(pts: Sequence[Point], closed: bool = False) -> float:
    total = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    if closed and len(pts) > 2:
        total += math.dist(pts[-1], pts[0])
    return total


def polyline_prefix(pts: Sequence[Point], frac: float, closed: bool = False) -> list[Point]:
    """The first `frac` (0..1) of the path by arc length: the vertices it passes and the point where it stops."""
    line = list(pts) + ([pts[0]] if closed and len(pts) > 2 else [])
    if not line:
        return []
    frac = min(max(frac, 0.0), 1.0)
    if frac <= 0.0:
        return [line[0]]
    want = polyline_length(line) * frac
    out = [line[0]]
    walked = 0.0
    for a, b in zip(line, line[1:]):
        seg = math.dist(a, b)
        if walked + seg >= want - 1e-9:
            t = (want - walked) / seg if seg > 0 else 0.0
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            return out
        out.append(b)
        walked += seg
    return out


def catmull_rom(pts: Sequence[Point], closed: bool = False, max_samples: int = MAX_PATH_SAMPLES) -> list[Point]:
    """A smooth curve through `pts` (uniform Catmull-Rom). At most `max_samples` points come back."""
    n = len(pts)
    if n < 3:
        return list(pts)
    segments = n if closed else n - 1
    per = max(1, min(16, (max_samples - 1) // segments))
    out: list[Point] = []

    def at(i: int) -> Point:
        return pts[i % n] if closed else pts[min(max(i, 0), n - 1)]

    for s in range(segments):
        p0, p1, p2, p3 = at(s - 1), at(s), at(s + 1), at(s + 2)
        for k in range(per):
            t = k / per
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3)
                             for a, b, c, d in zip(p0, p1, p2, p3)))   # type: ignore[arg-type]
    out.append(pts[0] if closed else pts[-1])
    return out[:max_samples]


def dash_segments(a: Point, b: Point, length: float, seg: float, gap: float, phase: float = 0.0) -> list[tuple[Point, Point]]:
    """Dashes along a -> b, as far as `length`. `phase` shifts the pattern along the line (0 starts with a dash)."""
    full = math.dist(a, b)
    if full <= 0 or length <= 0:
        return []
    ux, uy = (b[0] - a[0]) / full, (b[1] - a[1]) / full
    period = seg + gap
    out: list[tuple[Point, Point]] = []
    pos = -(phase % period)
    while pos < length:
        s, e = max(pos, 0.0), min(pos + seg, length)
        if e > s:
            out.append(((a[0] + ux * s, a[1] + uy * s), (a[0] + ux * e, a[1] + uy * e)))
        pos += period
    return out


def exit_distance(ux: float, uy: float, w: float, h: float, pad: float) -> float:
    """How far along the unit direction (ux, uy) a ray leaves a w x h box centred on its start, plus `pad`
    (a port of the `exit_t` in diagram edges)."""
    tx = (w / 2) / abs(ux) if abs(ux) > 1e-6 else 1e9
    ty = (h / 2) / abs(uy) if abs(uy) > 1e-6 else 1e9
    return min(tx, ty) + pad


def arrow_span(a: Point, a_size: tuple[float, float], b: Point, b_size: tuple[float, float],
               pad_a: float, pad_b: float) -> tuple[Point, Point, float, float] | None:
    """Where an arrow between two boxes (centre, (w, h)) starts and ends, and its unit direction. A point end has
    size (0, 0) and no pad. None when the boxes leave no room for it."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    s = exit_distance(ux, uy, *a_size, pad_a) if a_size != (0, 0) else 0.0
    f = dist - (exit_distance(ux, uy, *b_size, pad_b) if b_size != (0, 0) else 0.0)
    if f <= s:
        return None
    return (a[0] + ux * s, a[1] + uy * s), (a[0] + ux * f, a[1] + uy * f), ux, uy
