"""The `lineart` scene: ink-on-paper figures drawn by the vendored line-art-animation library (MIT, see
yamlreel/lineart/LICENSE-line-art-animation) in headless Chromium. If Chromium or playwright is missing, or the
page fails, the scene falls back to a typed line so a render never stops on it."""
from __future__ import annotations

import asyncio
import atexit
import base64
import io
import json
import os
import threading
from concurrent.futures import Future
from importlib import resources
from typing import Any, ClassVar

from PIL import Image

from .scenebase import Ctx, Scene
from .shots import CHROMIUM_ARGS

KINDS = ("presenter", "carry")


def _tabs() -> int:
    """How many pages draw at once. Each extra page costs about 200 MB of browser memory at 1080x1920, so a
    machine with less than 12 GB gets two and a larger one three. YAMLREEL_LINEART_PAGES (1 to 4) overrides it."""
    asked = os.environ.get("YAMLREEL_LINEART_PAGES", "")
    if asked.isdigit() and 1 <= int(asked) <= 4:
        return int(asked)
    try:
        gb = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2 ** 30
    except (AttributeError, ValueError, OSError):
        return 2
    return 3 if gb >= 12 else 2


TABS = _tabs()          # pages drawing at once: frames asked for ahead of time are drawn side by side
# Frames asked for ahead of time and not collected yet, at most. It only guards against frames that are never
# collected (a jump in time), so it must stay well above the render's look-ahead (render.FRAMES_AHEAD): dropping a
# frame that is about to be collected means drawing it twice. A waiting frame is about 6 MB at 1080x1920.
AHEAD_MAX = 8
# With no browser named, playwright's own Chromium is tried first, then an installed Chrome or Edge.
# The page is local and every request from it is refused, so which Chromium draws it does not matter.
CHANNELS = (None, "chrome", "msedge")


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(c)


class _Tab:
    def __init__(self, page: Any, cdp: Any):
        self.page, self.cdp = page, cdp


class _Page:
    """One headless browser per frame size, shared by every lineart scene of a render and closed at exit.

    It holds a few identical pages. The browser is driven from one background thread, so a frame that the render
    asks for ahead of time (`ahead`) is drawn while earlier frames are still being encoded. Every page draws the
    same pixels for the same time, so it makes no difference which page a frame comes from."""

    _open: ClassVar[dict[tuple, "_Page"]] = {}
    _pw: ClassVar[Any] = None
    _loop: ClassVar[asyncio.AbstractEventLoop | None] = None
    _thread: ClassVar[threading.Thread | None] = None
    _failed: ClassVar[dict[tuple, Exception]] = {}   # a browser that did not start is not tried again this render

    @classmethod
    def _run(cls, coro: Any) -> Future:
        """Start `coro` on the browser thread."""
        if cls._loop is None:
            cls._loop = asyncio.new_event_loop()
            cls._thread = threading.Thread(target=cls._loop.run_forever, name="yamlreel-lineart", daemon=True)
            cls._thread.start()
        return asyncio.run_coroutine_threadsafe(coro, cls._loop)

    def __init__(self, W: int, H: int, colors: dict[str, str], chromium: str | None):
        from playwright.async_api import async_playwright   # ImportError here: the scene falls back to typed text
        self.W, self.H, self.colors = W, H, colors
        self.browser: Any = None
        self._tabs: list[_Tab] = []
        self._ahead: dict[tuple[str, float], Future] = {}
        self._growing = False
        self.dropped = 0   # frames asked for ahead of time and forgotten before they were collected
        self._clip = {"x": 0, "y": 0, "width": W, "height": H, "scale": 1}
        try:
            self._run(self._start(async_playwright, chromium)).result()
        except BaseException:
            self.close()
            raise

    async def _start(self, async_playwright: Any, chromium: str | None) -> None:
        if _Page._pw is None:
            _Page._pw = await async_playwright().start()
        first: Exception | None = None
        for how in ([{"executable_path": chromium}] if chromium else [{"channel": c} if c else {} for c in CHANNELS]):
            try:
                self.browser = await _Page._pw.chromium.launch(args=CHROMIUM_ARGS, **how)
                break
            except Exception as e:
                first = first or e
        if self.browser is None:
            raise first   # type: ignore[misc]   # what playwright said about its own Chromium (how to install it)
        self._engine = resources.files("yamlreel").joinpath("lineart/engine.js").read_text("utf-8")
        self._free: asyncio.Queue[_Tab] = asyncio.Queue()
        await self._add_tab()

    async def _add_tab(self) -> None:
        context = await self.browser.new_context(viewport={"width": self.W, "height": self.H}, service_workers="block")
        await context.route("**/*", lambda route: route.abort())   # the page is local; nothing may leave it
        page = await context.new_page()
        await page.set_content("<!doctype html><meta charset=utf-8><body></body>")
        await page.add_script_tag(content=self._engine)
        await page.evaluate("([w, h, c]) => window.LA.setup(w, h, c)", [self.W, self.H, self.colors])
        try:   # Chromium's own capture call, with its fast PNG encoder: the same pixels, sooner
            cdp = await context.new_cdp_session(page)
            # the frame size is set per session; without it this session would capture the window, not the frame
            await cdp.send("Emulation.setDeviceMetricsOverride",
                           {"width": self.W, "height": self.H, "deviceScaleFactor": 1, "mobile": False})
        except Exception:
            cdp = None
        tab = _Tab(page, cdp)
        self._tabs.append(tab)
        self._free.put_nowait(tab)

    async def _more_tabs(self) -> None:
        """The other pages, opened the first time a frame is asked for ahead of time. If one cannot be opened the
        render carries on with the pages it has."""
        while len(self._tabs) < TABS:
            try:
                await self._add_tab()
            except Exception:
                return

    async def _png(self, tab: _Tab) -> bytes:
        """The page as PNG bytes (lossless either way; the direct call skips Playwright's slower encoder settings)."""
        if tab.cdp is not None:
            try:
                shot = await tab.cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True,
                                                                     "captureBeyondViewport": False, "clip": self._clip})
                return base64.b64decode(shot["data"])
            except Exception:
                tab.cdp = None   # an older or different browser: use the standard screenshot from here on
        return await tab.page.screenshot(type="png")

    async def _draw(self, payload: dict[str, Any], key: str, t: float, duration: float) -> tuple[Any, Image.Image]:
        """One frame, from whichever page is free: what the engine reported, and the decoded picture."""
        tab = await self._free.get()
        try:
            info = await tab.page.evaluate("([s, k, t, d]) => window.LA.frame(s, k, t, d)", [payload, key, t, duration])
            img = await asyncio.to_thread(_decode, await self._png(tab))
            if img.size != (self.W, self.H) and tab.cdp is not None:
                tab.cdp = None   # the direct capture missed the frame size: take the standard screenshot instead
                img = await asyncio.to_thread(_decode, await self._png(tab))
            return info, img
        finally:
            self._free.put_nowait(tab)

    def ahead(self, payload: dict[str, Any], key: str, t: float, duration: float) -> None:
        """Start drawing a frame that `frame` will ask for soon."""
        if (key, t) in self._ahead:
            return
        if not self._growing:
            self._growing = True
            self._run(self._more_tabs())
        while len(self._ahead) >= AHEAD_MAX:   # asked for and never collected (a jump in time): forget the oldest
            self._ahead.pop(next(iter(self._ahead))).cancel()
            self.dropped += 1
        self._ahead[(key, t)] = self._run(self._draw(payload, key, t, duration))

    def frame(self, payload: dict[str, Any], key: str, t: float, duration: float) -> tuple[Any, Image.Image]:
        """The frame at `t`: the one already being drawn if it was asked for ahead of time, else drawn now."""
        started = self._ahead.pop((key, t), None)
        if started is not None and not started.cancelled():
            return started.result()
        return self._run(self._draw(payload, key, t, duration)).result()

    # The two captures of the first page as it stands, for the test that compares them.
    @property
    def _cdp(self) -> Any:
        return self._tabs[0].cdp

    def png(self) -> bytes:
        return self._run(self._png(self._tabs[0])).result()

    def standard_png(self) -> bytes:
        return self._run(self._tabs[0].page.screenshot(type="png")).result()

    def close(self) -> None:
        for started in self._ahead.values():
            started.cancel()
        self._ahead.clear()
        if self.browser is not None:
            try:
                self._run(self.browser.close()).result(timeout=20)
            except Exception:
                pass

    @classmethod
    def get(cls, ctx: Ctx) -> "_Page":
        t = ctx.theme
        colors = {"bg": _hex(t.bg), "ink": _hex(t.fg), "accent": _hex(t.accent), "muted": _hex(t.muted), "line": _hex(t.line)}
        key = (ctx.W, ctx.H, tuple(sorted(colors.items())), ctx.chromium)
        if key in cls._failed:
            raise cls._failed[key]
        if key not in cls._open:
            try:
                cls._open[key] = cls(ctx.W, ctx.H, colors, ctx.chromium)
            except Exception as e:
                cls._failed[key] = e
                raise
        return cls._open[key]

    @classmethod
    def close_all(cls) -> None:
        for p in cls._open.values():
            p.close()
        cls._open.clear()
        cls._failed.clear()
        if cls._pw is not None:
            try:
                cls._run(cls._pw.stop()).result(timeout=20)
            except Exception:
                pass
            cls._pw = None
        if cls._loop is not None:
            cls._loop.call_soon_threadsafe(cls._loop.stop)
            if cls._thread is not None:
                cls._thread.join(timeout=5)
            if not cls._loop.is_running():
                cls._loop.close()
            cls._loop = cls._thread = None


def _decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


atexit.register(_Page.close_all)


class LineArtScene(Scene):
    """`kind: presenter` (default): an ink figure walks in and points at the line, which is written out beside it.
    `kind: carry`: one ink figure per item carries a box of data to it."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "text", "items"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        self.kind = spec.get("kind", "presenter")
        self.payload = {"kind": self.kind, "text": str(spec.get("text", "")), "items": list(spec.get("items", []))}
        self.key = json.dumps(self.payload, sort_keys=True)
        self._fallback: Scene | None = None
        self._no_ahead = False

    def _typed(self) -> Scene:
        from .scenes import TypeScene
        if self._fallback is None:
            text = self.payload["text"] or " / ".join(self.payload["items"]) or " "
            self._fallback = TypeScene({**self.spec, "type": "type", "text": text}, self.ctx)
        return self._fallback

    def prefetch(self, t: float) -> None:
        if self._fallback is not None or self._no_ahead:
            return
        try:
            _Page.get(self.ctx).ahead(self.payload, self.key, float(t), self.duration)
        except Exception:
            self._no_ahead = True   # `frame` meets the same problem and reports it

    def frame(self, t: float) -> Image.Image:
        if self._fallback is not None:
            return self._fallback.frame(t)
        try:
            info, img = _Page.get(self.ctx).frame(self.payload, self.key, float(t), self.duration)
            if info and info.get("shrunk"):
                self.ctx.warn("lineart text was shrunk to fit the frame; shorten it for a bigger, easier read")
        except Exception as e:   # no playwright, no Chromium, or the page failed: keep the render going
            self.ctx.warn(f"lineart scene drawn as typed text ({type(e).__name__}: {str(e)[:80]}); "
                          "pip install 'yamlreel[capture]' and playwright install chromium to draw it")
            self._fallback = self._typed()
            return self._fallback.frame(t)
        if img.size != (self.ctx.W, self.ctx.H):
            img = img.resize((self.ctx.W, self.ctx.H), Image.LANCZOS)
        return img
