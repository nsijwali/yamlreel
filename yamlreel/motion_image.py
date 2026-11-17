"""Local images for `motion` scenes: loaded once, guarded against decompression bombs, pre-scaled to the largest size
they will be drawn at, and counted against a byte budget. URLs are not accepted (use a `shot` scene)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from . import shots
from .safety import resolve_local
from .shots import CaptureError

MAX_IMAGES = 8
MAX_PRESCALE_PIXELS = 4_000_000


def load(source: str, base_dir: Path, roots: list[Path], where: str) -> Image.Image:
    """The image as RGBA. Errors name `where` (for example `elements[2].source`); a path outside the allowed folders
    raises safety.UnsafeInput, which names the path."""
    if shots.is_url(source):
        raise CaptureError(f"{where}: must be a local file; use a shot scene for web pages")
    p = resolve_local(source, base_dir, roots)
    if not p.is_file():
        raise CaptureError(f"{where}: image not found: {p}")
    try:
        with Image.open(p) as im:
            if im.width * im.height > shots.MAX_IMAGE_PIXELS:
                raise CaptureError(f"{where}: {p.name} is {im.width}x{im.height}; the limit is "
                                   f"{shots.MAX_IMAGE_PIXELS} pixels (MAX_IMAGE_PIXELS)")
            return im.convert("RGBA")
    except (OSError, Image.DecompressionBombError) as e:
        raise CaptureError(f"{where}: cannot read {p.name}: {e}") from e


def display_size(size: tuple[int, int], w: float | None, h: float | None) -> tuple[float, float]:
    """The size an image is shown at, from `w`, `h` or both (the other follows the aspect ratio)."""
    iw, ih = size
    if w is not None and h is not None:
        return w, h
    if w is not None:
        return w, w * ih / iw
    assert h is not None
    return h * iw / ih, h


def prescale(img: Image.Image, w: float, h: float, cap: int = MAX_PRESCALE_PIXELS) -> Image.Image:
    """The image scaled to w x h pixels: at most `cap` pixels in all, and never larger than the source."""
    tw, th = max(1, round(w)), max(1, round(h))
    if tw * th > cap:
        k = (cap / (tw * th)) ** 0.5
        tw, th = max(1, int(tw * k)), max(1, int(th * k))
    tw, th = min(tw, img.width), min(th, img.height)
    return img if (tw, th) == img.size else img.resize((tw, th), Image.LANCZOS)
