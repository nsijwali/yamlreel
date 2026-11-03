"""Shared scene plumbing: the render context, the Scene base class and the appear helper."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar

from PIL import Image, ImageDraw

from .textkit import TextBlock, ease_out, make_block
from .theme import Theme


@dataclass
class Ctx:
    W: int
    H: int
    theme: Theme
    workdir: Path
    base_dir: Path
    roots: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium: str | None = None
    warnings: list[str] = field(default_factory=list)
    _bg: Image.Image | None = None

    @property
    def vertical(self) -> bool:
        return self.H > self.W

    @property
    def u(self) -> float:
        return min(self.W, self.H) / 1080

    @property
    def fs(self) -> float:
        """Text scale: vertical frames are read on phones, so type is larger."""
        return 1.2 if self.vertical else 1.0

    @property
    def margin(self) -> int:
        return int(self.W * 0.08)

    @property
    def cy(self) -> float:
        """Vertical centre of the content area (clear of the caption zone)."""
        return self.H * (0.42 if self.vertical else 0.45)

    @property
    def caption_y(self) -> float:
        return self.H * (0.76 if self.vertical else 0.88)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def background(self) -> Image.Image:
        if self._bg is None:
            t, W, H = self.theme, self.W, self.H
            # soft radial vignette: lighter centre, darker edges
            import numpy as np
            yy, xx = np.mgrid[0:H, 0:W]
            r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H * 0.45) / (H / 2)) ** 2)
            k = np.clip(1.0 - 0.10 * r, 0.82, 1.0)[..., None]
            base = np.array(t.bg, dtype=np.float32)[None, None, :] * k
            img = Image.fromarray(np.clip(base, 0, 255).astype("uint8"), "RGB")
            if t.frame:
                d = ImageDraw.Draw(img)
                inset = int(36 * self.u)
                d.rectangle([inset, inset, W - inset, H - inset], outline=t.line, width=max(1, int(2 * self.u)))
            self._bg = img
        return self._bg.copy()

    def block(self, text: str, kind: str, size: float, max_w: float, **kw: Any) -> TextBlock:
        b = make_block(text, kind, size, max_w, **kw)
        if b.shrunk:
            self.warn(f"text shrunk to fit ({b.size}px): {text[:40]!r}")
        return b

    @property
    def display(self) -> str:
        return "serif_bold" if self.theme.display == "serif" else "sans_bold"

    @property
    def body(self) -> str:
        return self.theme.display


def _appear(t: float, start: float, dur: float = 0.5) -> float:
    return ease_out((t - start) / dur)


class Scene:
    # The spec keys a scene reads. specs.py rejects anything else, and a test keeps these tables in step
    # with what the constructors actually read.
    COMMON_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"type", "seconds", "caption", "narration", "narration_audio", "transition", "narration_words", "subtitles",
         "keywords"})
    FIELDS: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        self.spec = spec
        self.ctx = ctx
        self.duration = float(spec["seconds"])
        self.caption = spec.get("caption")
        self._static: dict[str, Image.Image] = {}

    def static_layer(self, key: str, draw: Callable[[Image.Image], None]) -> Image.Image:
        """A fresh copy of the background with `draw(image)` applied to it. `draw` runs once per scene and key and
        its result is kept, so chrome that never changes (a table frame, a track) is not redrawn on every frame.
        Callers get a copy and may draw on it."""
        base = self._static.get(key)
        if base is None:
            base = self.ctx.background()
            draw(base)
            self._static[key] = base
        return base.copy()

    def cached_layers(self) -> int:
        """How many static layers this scene is holding (tests and the timeline's memory bound use it)."""
        return len(self._static)

    def release(self) -> None:
        """Drop the cached static layers. They are rebuilt on demand, so a scene may be released and drawn again."""
        self._static.clear()

    def frame(self, t: float) -> Image.Image:  # pragma: no cover
        raise NotImplementedError
