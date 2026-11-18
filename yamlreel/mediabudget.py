"""One budget for the decoded pictures a render holds at once, across all its scenes.

Each picture is already limited on its own (shots.MAX_IMAGE_PIXELS, motion_scene.MAX_IMAGE_BYTES), but a spec with
many scenes could still hold every one of them decoded at the same time. The budget counts the bytes per owner:

- `charge(owner, n, release)`: a picture that can be decoded again later (a `shot` scene's screenshot). When the
  budget is full, the least recently used other owners are asked to `release()` theirs first.
- `pin(owner, n, where)`: a picture that stays decoded for the render (a `motion` scene's pre-scaled images). Pinned
  bytes are never evicted; pinning past the budget is a clear error instead of an out-of-memory crash.

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
        self._pinned: dict[object, int] = {}

    def _mb(self, n: int) -> str:
        return f"{n / 2 ** 20:.0f} MB"

    def charge(self, owner: object, nbytes: int, release: Callable[[], None]) -> None:
        """`owner` now holds `nbytes` it can decode again; others are released, least recently used first, to fit."""
        self.drop(owner)
        if nbytes > self.limit - sum(self._pinned.values()):
            raise MediaBudgetError(f"one picture needs {self._mb(nbytes)}; with the pinned images the limit is "
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

    def pin(self, owner: object, nbytes: int, where: str) -> None:
        """`owner` keeps `nbytes` for the whole render. Releases loaded pictures if needed; raises MediaBudgetError
        when the pinned pictures alone would exceed the limit."""
        pinned = sum(self._pinned.values()) + nbytes
        if pinned > self.limit:
            raise MediaBudgetError(f"{where}: the images of all scenes together need {self._mb(pinned)}; the limit is "
                                   f"{self._mb(self.limit)} (MAX_DECODED_MEDIA_BYTES)")
        self._pinned[owner] = self._pinned.get(owner, 0) + nbytes
        self.used += nbytes
        while self.used > self.limit and self._loaded:
            _, (n, give_back) = self._loaded.popitem(last=False)
            self.used -= n
            give_back()
        self.peak = max(self.peak, self.used)

    def holds(self, owner: object) -> bool:
        return owner in self._loaded
