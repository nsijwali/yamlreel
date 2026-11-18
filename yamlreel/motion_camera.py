"""The camera of a `motion` scene: zoom and pan keyframes, applied to coordinates before anything is drawn.

A world point P (pixels) maps to S = (P - F) * zoom + C, where F = (fx * W, fy * H) is the focus and C = (W/2, H/2)
the middle of the frame. Zoom 1 with focus (0.5, 0.5) is the identity. Pure: no PIL."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .textkit import ease

MAX_CAMERA_KEYS = 16


@dataclass(frozen=True)
class Viewport:
    zoom: float = 1.0
    fx: float = 0.5
    fy: float = 0.5

    @property
    def identity(self) -> bool:
        return self.zoom == 1.0 and self.fx == 0.5 and self.fy == 0.5


IDENTITY = Viewport()


@dataclass(frozen=True)
class CameraKey:
    at: float
    seconds: float
    zoom: float
    fx: float
    fy: float
    easing: str


def camera_at(keys: Sequence[CameraKey], t: float) -> Viewport:
    """The viewport at `t`: the identity before the first key, each key easing from the one before it."""
    cur = IDENTITY
    for k in keys:
        if t < k.at:
            return cur
        tgt = Viewport(k.zoom, k.fx, k.fy)
        if k.seconds <= 0 or t >= k.at + k.seconds:
            cur = tgt
            continue
        p = ease(k.easing, (t - k.at) / k.seconds)
        return Viewport(cur.zoom + (tgt.zoom - cur.zoom) * p, cur.fx + (tgt.fx - cur.fx) * p, cur.fy + (tgt.fy - cur.fy) * p)
    return cur


def to_screen(vp: Viewport, x: float, y: float, W: float, H: float) -> tuple[float, float]:
    return (x - vp.fx * W) * vp.zoom + W / 2, (y - vp.fy * H) * vp.zoom + H / 2


def max_zoom(keys: Sequence[CameraKey]) -> float:
    return max([1.0] + [k.zoom for k in keys])
