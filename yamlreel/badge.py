"""The step badge: `STEP n/N · LABEL` and an N-segment progress bar, centred at the top of a scene.

N is the number of distinct `step` labels in the spec, in order of first appearance, and n is the position of
this scene's label among them, so consecutive scenes with the same label share one badge. The badge is rendered
once per label to an RGB layer and a mask, and the timeline pastes it on every frame of the scene.
"""
from __future__ import annotations

from typing import Sequence

from PIL import Image, ImageDraw

from .scenebase import Ctx
from .textkit import make_block

TOP = 0.035        # the badge starts this share of the frame height from the top
MAX_WIDTH = 0.84   # share of the frame width


def step_plan(labels: Sequence[str | None]) -> list[tuple[int, int, str] | None]:
    """For each scene's `step` label: (n, N, label), or None for a scene without one. A repeated label keeps
    the number it first got."""
    order: list[str] = []
    for lab in labels:
        if lab and lab not in order:
            order.append(lab)
    return [(order.index(lab) + 1, len(order), lab) if lab else None for lab in labels]


def badge_text(n: int, total: int, label: str) -> str:
    return f"STEP {n}/{total} · {label.upper()}"


class Badge:
    """A rendered badge: `rgb` and `mask` are pasted at `xy`; `box` is the (x0, y0, x1, y1) it covers."""

    def __init__(self, rgb: Image.Image, mask: Image.Image, xy: tuple[int, int], text: str):
        self.rgb, self.mask, self.xy, self.text = rgb, mask, xy, text

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.xy[0], self.xy[1], self.xy[0] + self.rgb.width, self.xy[1] + self.rgb.height

    def paste(self, img: Image.Image) -> None:
        img.paste(self.rgb, self.xy, self.mask)


def make_badge(ctx: Ctx, n: int, total: int, label: str) -> Badge:
    th, u = ctx.theme, ctx.u
    size = 27 * u * (1.2 if ctx.vertical else 1.0)
    max_w = ctx.W * MAX_WIDTH
    gap = 14 * u
    step_blk = make_block(f"STEP {n}/{total}", "sans_bold", size, max_w, tracking=4 * u, min_size=10)
    dot_blk = make_block("·", "sans_bold", size, 60 * u, min_size=10)
    label_blk = make_block(label.upper(), "sans_bold", size, max(60.0, max_w - step_blk.width - dot_blk.width - 2 * gap),
                           tracking=4 * u, min_size=10)
    if label_blk.shrunk:
        ctx.warn(f"step label shrunk to fit: {label[:40]!r}")
    row_w = step_blk.width + dot_blk.width + label_blk.width + 2 * gap
    row_h = max(step_blk.mask.height, label_blk.mask.height)
    bar_h = max(4, int(round(7 * u)))
    bar_gap = 8 * u
    seg_gap = max(3, int(round(6 * u)))
    bar_w = int(max(row_w, ctx.W * 0.3))
    w, h = bar_w, int(row_h + bar_gap + bar_h)
    rgb = Image.new("RGB", (w, h), th.bg)
    mask = Image.new("L", (w, h), 0)
    x = (w - row_w) / 2
    for blk, colour in ((step_blk, th.accent2), (dot_blk, th.muted), (label_blk, th.fg)):
        pos = (int(round(x)), int((row_h - blk.mask.height) / 2))
        rgb.paste(colour, pos, blk.mask)
        mask.paste(255, pos, blk.mask)
        x += blk.width + gap
    seg_w = (w - (total - 1) * seg_gap) / total
    d_rgb, d_mask = ImageDraw.Draw(rgb), ImageDraw.Draw(mask)
    y0 = int(row_h + bar_gap)
    if seg_w < 2:  # too many steps for separate segments: one bar, filled up to step n
        for box, fill in (([0, y0, w - 1, y0 + bar_h - 1], th.line),
                          ([0, y0, max(0, round(w * n / total) - 1), y0 + bar_h - 1], th.accent)):
            d_rgb.rounded_rectangle(box, bar_h // 2, fill=fill)
            d_mask.rounded_rectangle(box, bar_h // 2, fill=255)
    else:
        for k in range(total):
            x0 = k * (seg_w + seg_gap)
            box = [round(x0), y0, round(x0 + seg_w) - 1, y0 + bar_h - 1]
            d_rgb.rounded_rectangle(box, bar_h // 2, fill=th.accent if k < n else th.line)
            d_mask.rounded_rectangle(box, bar_h // 2, fill=255)
    xy = (int((ctx.W - w) / 2), int(ctx.H * TOP))
    return Badge(rgb, mask, xy, badge_text(n, total, label))
