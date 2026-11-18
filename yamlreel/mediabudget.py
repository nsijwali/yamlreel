"""One budget for the decoded pictures a render holds at once, across all its scenes.

Each picture is already limited on its own (shots.MAX_IMAGE_PIXELS, motion_scene.MAX_IMAGE_BYTES), but a spec with
many scenes could still hold every one of them decoded at the same time. Every scene picture (a `shot` screenshot,
a `motion` image) can be decoded again, so the budget counts the bytes per owner with `charge(owner, n, release)`:
when it is full, the least recently used other owners are asked to `release()` theirs first. Only one picture larger
than the whole budget is an error.

`peak` is the most bytes ever held at once; the timeline keeps it near its working set (the scene being drawn and the
one before it, for transitions).
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Callable

MAX_DECODED_MEDIA_BYTES = 2 * 1024 ** 3   # all decoded scene pictures of one render together


class MediaBudgetError(RuntimeError):
    """The pictures a render must hold at once do not fit in MAX_DECODED_MEDIA_BYTES."""


class MediaBudget:
    def __init__(self, limit: int = MAX_DECODED_MEDIA_BYTES):
        self.limit = limit
        self.used = 0
        self.peak = 0
        self._loaded: OrderedDict[object, tuple[int, Callable[[], None]]] = OrderedDict()   # oldest first

    def _mb(self, n: int) -> str:
        return f"{n / 2 ** 20:.0f} MB"

    def charge(self, owner: object, nbytes: int, release: Callable[[], None]) -> None:
        """`owner` now holds `nbytes` it can decode again; others are released, least recently used first, to fit."""
        self.drop(owner)
        if nbytes > self.limit:
            raise MediaBudgetError(f"one picture needs {self._mb(nbytes)}; the limit is "
                                   f"{self._mb(self.limit)} (MAX_DECODED_MEDIA_BYTES)")
        while self.used + nbytes > self.limit and self._loaded:
            other, (n, give_back) = self._loaded.popitem(last=False)
            self.used -= n
            give_back()
        self._loaded[owner] = (nbytes, release)
        self.used += nbytes
        self.peak = max(self.peak, self.used)

    def touch(self, owner: object) -> None:
        """`owner` was just used: it is the last to be released."""
        if owner in self._loaded:
            self._loaded.move_to_end(owner)

    def drop(self, owner: object) -> None:
        """`owner` gave its picture back (or never had one)."""
        entry = self._loaded.pop(owner, None)
        if entry is not None:
            self.used -= entry[0]

    def holds(self, owner: object) -> bool:
        return owner in self._loaded
