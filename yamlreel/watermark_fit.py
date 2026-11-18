"""The size an image watermark is drawn at, decided from the image header before any pixel is decoded or resized.

The source image's pixel count is limited when it is opened (render.MAX_WATERMARK_PIXELS), but the size it is
scaled to must be bounded too: a 100000x1 logo scaled to a 56 px height would be 5,600,000 px wide (about 1.2 GB as
RGBA). The watermark therefore fits inside a box: the requested height, and at most MAX_WIDTH_FRAC of the frame
width, keeping its aspect ratio. Ordinary logos are far narrower than that box, so they come out exactly as before.
"""
from __future__ import annotations

MAX_WIDTH_FRAC = 0.5  # a watermark is never wider than half the frame


class WatermarkSizeError(ValueError):
    """The image cannot be drawn as a watermark at any visible size (for example 1 px tall once it fits the frame)."""


def fit_watermark(src_w: int, src_h: int, height: int, frame_w: int) -> tuple[int, int]:
    """(width, height) to resize a `src_w` x `src_h` image to: `height` tall unless that would make it wider than
    MAX_WIDTH_FRAC of `frame_w`, in which case it is shrunk (aspect kept) to that width."""
    if src_w < 1 or src_h < 1:
        raise WatermarkSizeError(f"watermark image is {src_w}x{src_h}; it has no pixels")
    h = max(1, int(height))
    w = max(1, int(src_w * h / src_h))  # the long-standing formula: unchanged for every logo that fits
    max_w = max(1, int(frame_w * MAX_WIDTH_FRAC))
    if w > max_w:
        w = max_w
        h = int(src_h * max_w / src_w)
        if h < 1:
            raise WatermarkSizeError(
                f"watermark image is {src_w}x{src_h}; at most {max_w} px wide (half the frame) it would be less than "
                "1 px tall. Use a less extreme aspect ratio")
    return w, h
