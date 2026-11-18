"""A `shot` scene's picture, decoded only while the scene is being drawn.

The capture (or the check of a local image) happens when the scene is built, so every problem is still reported
with its scene number before rendering starts; only the size is read then. The pixels are decoded the first time a
frame needs them, counted against the render's MediaBudget, and given back by `drop()` when the timeline releases
the scene or the budget needs the room. Decoding the same file again gives the same pixels, so frames do not change.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from .mediabudget import MediaBudget, MediaBudgetError
from .shots import CaptureError, MAX_IMAGE_PIXELS, _open_image


def image_size(p: Path) -> tuple[int, int]:
    """The size of the image at `p` from its header (no decode), with the same checks as decoding it."""
    try:
        with Image.open(p) as im:
            if im.width * im.height > MAX_IMAGE_PIXELS:
                raise CaptureError(f"{p.name}: image is {im.width}x{im.height}; the limit is {MAX_IMAGE_PIXELS} pixels")
            return im.size
    except (OSError, Image.DecompressionBombError) as e:
        raise CaptureError(f"cannot read image {p.name}: {e}") from e


class ShotImage:
    BYTES_PER_PIXEL = 3   # decoded as RGB

    def __init__(self, path: Path, budget: MediaBudget):
        self.path = path
        self.size = image_size(path)
        self._budget = budget
        self._img: Image.Image | None = None

    @property
    def nbytes(self) -> int:
        return self.size[0] * self.size[1] * self.BYTES_PER_PIXEL

    @property
    def loaded(self) -> bool:
        return self._img is not None

    def get(self) -> Image.Image:
        """The decoded picture (decoded now if it is not held)."""
        if self._img is None:
            try:
                self._budget.charge(self, self.nbytes, self._forget)
            except MediaBudgetError as e:
                raise CaptureError(f"{self.path.name}: {e}") from e
            img = _open_image(self.path)
            if img.size != self.size:   # the file changed since the scene was built
                self._budget.drop(self)
                raise CaptureError(f"{self.path.name} changed while rendering ({self.size} -> {img.size})")
            self._img = img
        else:
            self._budget.touch(self)
        return self._img

    def _forget(self) -> None:
        """The budget took the room back: forget the pixels (decoded again if needed)."""
        self._img = None

    def drop(self) -> None:
        """Give the pixels back now."""
        self._img = None
        self._budget.drop(self)
