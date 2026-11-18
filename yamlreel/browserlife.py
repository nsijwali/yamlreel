"""The browser a render starts (headless Chromium for line-art frames) is the render's to close.

Line-art scenes share one browser per frame size (lineart_scene._Page) so that frames drawn ahead come from warm
pages. That browser belongs to the render that started it: `closing_browsers()` closes it when the render ends,
whether it finished, failed or was interrupted, so no Chromium outlives its render.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_BROWSER_SECONDS = 180.0   # proclife.Deadlines.browser_frame


@contextmanager
def closing_browsers() -> Iterator[None]:
    try:
        yield
    finally:
        from .lineart_scene import _Page   # late: a render without line-art never imports the browser code
        _Page.close_all()
