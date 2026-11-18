"""A `motion` scene's image element, decoded and pre-scaled only while frames need it.

The image is opened, checked and pre-scaled once when the scene is built, so every problem (a missing file, a
decompression bomb, a path outside the allowed folders) is still reported with its element path before rendering; then
the pixels are dropped. The first frame that draws the element decodes and pre-scales it again, which gives exactly
the same pixels, charges it to the render's MediaBudget and keeps it until the timeline releases the scene
(MotionScene.release) or the budget needs the room for a more recently drawn picture.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from . import motion_image
from .mediabudget import MediaBudget, MediaBudgetError
from .shots import CaptureError


class MotionImage:
    BYTES_PER_PIXEL = 4   # RGBA

    def __init__(self, source: str, base_dir: Path, roots: list[Path], where: str, w: float | None, h: float | None,
                 unit: float, bound: float, budget: MediaBudget):
        self._source, self._base, self._roots, self.where = source, base_dir, roots, where
        self._budget = budget
        raw = motion_image.load(source, base_dir, roots, where)   # errors name the element now, not mid-render
        dw, dh = motion_image.display_size(raw.size, w, h)
        self.w, self.h = dw * unit, dh * unit                     # pixels at scale 1
        self._target = (self.w * bound, self.h * bound)           # the largest size it is drawn at
        self.size = motion_image.prescale(raw, *self._target).size
        self._img: Image.Image | None = None                      # nothing is kept until a frame needs it

    @property
    def nbytes(self) -> int:
        return self.size[0] * self.size[1] * self.BYTES_PER_PIXEL

    @property
    def loaded(self) -> bool:
        return self._img is not None

    def get(self) -> Image.Image:
        """The pre-scaled picture, decoded now if it is not held."""
        if self._img is None:
            try:
                self._budget.charge(self, self.nbytes, self._forget)
            except MediaBudgetError as e:
                raise CaptureError(f"{self.where}: {e}") from e
            img = motion_image.prescale(motion_image.load(self._source, self._base, self._roots, self.where),
                                        *self._target)
            if img.size != self.size:   # the file changed since the scene was built
                self._budget.drop(self)
                raise CaptureError(f"{self.where}: the image changed while rendering ({self.size} -> {img.size})")
            self._img = img
        else:
            self._budget.touch(self)
        return self._img

    def _forget(self) -> None:
        self._img = None

    def drop(self) -> None:
        self._img = None
        self._budget.drop(self)
