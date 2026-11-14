"""The `lineart` scene: ink-on-paper figures drawn by the vendored line-art-animation library (MIT, see
yamlreel/lineart/LICENSE-line-art-animation) in headless Chromium. If Chromium or playwright is missing, or the
page fails, the scene falls back to a typed line so a render never stops on it."""
from __future__ import annotations

import atexit
import io
import json
from importlib import resources
from typing import Any, ClassVar

from PIL import Image

from .scenebase import Ctx, Scene
from .shots import CHROMIUM_ARGS

KINDS = ("presenter", "carry")


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(c)


class _Page:
    """One headless page per frame size, shared by every lineart scene of a render and closed at exit."""

    _open: ClassVar[dict[tuple, "_Page"]] = {}
    _pw: ClassVar[Any] = None    # playwright's sync API allows one running instance per process, so every page shares it

    def __init__(self, W: int, H: int, colors: dict[str, str], chromium: str | None):
        from playwright.sync_api import sync_playwright
        if _Page._pw is None:
            _Page._pw = sync_playwright().start()
        self.pw = _Page._pw
        try:
            self.browser = self.pw.chromium.launch(args=CHROMIUM_ARGS, **({"executable_path": chromium} if chromium else {}))
            context = self.browser.new_context(viewport={"width": W, "height": H}, service_workers="block")
            context.route("**/*", lambda route: route.abort())   # the page is local; nothing may leave it
            self.page = context.new_page()
            self.page.set_content("<!doctype html><meta charset=utf-8><body></body>")
            self.page.add_script_tag(content=resources.files("yamlreel").joinpath("lineart/engine.js").read_text("utf-8"))
            self.page.evaluate("([w, h, c]) => window.LA.setup(w, h, c)", [W, H, colors])
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            self.browser.close()
        except Exception:
            pass

    @classmethod
    def get(cls, ctx: Ctx) -> "_Page":
        t = ctx.theme
        colors = {"bg": _hex(t.bg), "ink": _hex(t.fg), "accent": _hex(t.accent), "muted": _hex(t.muted), "line": _hex(t.line)}
        key = (ctx.W, ctx.H, tuple(sorted(colors.items())), ctx.chromium)
        if key not in cls._open:
            cls._open[key] = cls(ctx.W, ctx.H, colors, ctx.chromium)
        return cls._open[key]

    @classmethod
    def close_all(cls) -> None:
        for p in cls._open.values():
            p.close()
        cls._open.clear()
        if cls._pw is not None:
            try:
                cls._pw.stop()
            except Exception:
                pass
            cls._pw = None


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

    def _typed(self) -> Scene:
        from .scenes import TypeScene
        if self._fallback is None:
            text = self.payload["text"] or " / ".join(self.payload["items"]) or " "
            self._fallback = TypeScene({**self.spec, "type": "type", "text": text}, self.ctx)
        return self._fallback

    def frame(self, t: float) -> Image.Image:
        if self._fallback is not None:
            return self._fallback.frame(t)
        try:
            page = _Page.get(self.ctx).page
            info = page.evaluate("([s, k, t, d]) => window.LA.frame(s, k, t, d)", [self.payload, self.key, float(t), self.duration])
            if info and info.get("shrunk"):
                self.ctx.warn("lineart text was shrunk to fit the frame; shorten it for a bigger, easier read")
            img = Image.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB")
        except Exception as e:   # no playwright, no Chromium, or the page failed: keep the render going
            self.ctx.warn(f"lineart scene drawn as typed text ({type(e).__name__}: {str(e)[:80]}); "
                          "pip install 'yamlreel[capture]' and playwright install chromium to draw it")
            self._fallback = self._typed()
            return self._fallback.frame(t)
        if img.size != (self.ctx.W, self.ctx.H):
            img = img.resize((self.ctx.W, self.ctx.H), Image.LANCZOS)
        return img
