"""Scene types. Each scene renders a PIL image for a local time t (seconds)."""
from __future__ import annotations

import math
import re
from typing import Any, ClassVar

from PIL import Image, ImageDraw, ImageFilter

from .scenebase import Ctx, Scene, _appear
from .shots import load_shot
from .textkit import TextBlock, blit, ease_in_out, ease_out, lerp_color, make_block, split_count
from .theme import Color, Theme, font

SS = 2  # diagram supersampling

# Options of a `shot` scene that are passed to the screenshot capture (and so are part of its cache key).
CAPTURE_OPTS = ("viewport", "scale", "click", "scroll_y", "wait_ms", "full_page")


class TitleScene(Scene):
    """Eyebrow, title, subtitle, optional URL line; elements ease in one by one."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"title", "eyebrow", "subtitle", "url", "rule"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, W = ctx.u, ctx.W
        maxw = W - 2 * ctx.margin
        s = spec
        self.items: list[tuple[TextBlock, Color, int]] = []
        th = ctx.theme
        if s.get("eyebrow"):
            self.items.append((ctx.block(str(s["eyebrow"]).upper(), "sans_bold", 28 * u, maxw, tracking=7 * u), th.accent2, 28))
        self.items.append((ctx.block(s["title"], ctx.display, 100 * u * ctx.fs, maxw, max_h=330 * u, align="center", spacing=1.15), th.fg, 30))
        if s.get("subtitle"):
            self.items.append((ctx.block(s["subtitle"], ctx.body, 42 * u, maxw, max_h=200 * u, align="center", spacing=1.3), th.muted, 40))
        if s.get("url"):
            self.items.append((ctx.block(s["url"], "sans_bold", 40 * u, maxw, align="center"), th.accent2, 56))
        self.rule = bool(s.get("rule", True)) and len(self.items) > 1

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        img = c.background()
        total = sum(b.height + g * c.u for b, _, g in self.items)
        y = c.cy - total / 2
        for i, (b, color, gap) in enumerate(self.items):
            a = _appear(t, 0.15 + i * 0.22)
            blit(img, b, (c.W / 2, y + (1 - a) * 22 * c.u), color, a, "tc")
            y += b.height + gap * c.u
            if self.rule and i == 1:
                ra = _appear(t, 0.5, 0.7)
                w = int(c.W * 0.12 * ra)
                d = ImageDraw.Draw(img)
                d.line([(c.W / 2 - w, y - gap * c.u * 0.5), (c.W / 2 + w, y - gap * c.u * 0.5)],
                       fill=c.theme.accent, width=max(2, int(3 * c.u)))
        return img


class TypeScene(Scene):
    """A line typed out with a blinking cursor, used as the opening hook."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"text"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        maxw = ctx.W - 2 * ctx.margin
        self.text = str(spec["text"])
        blk = ctx.block(self.text, ctx.display, 92 * ctx.u * ctx.fs, maxw, max_h=ctx.H * 0.5, align="center", spacing=1.2)
        self.size = blk.size
        self.fnt = font(ctx.display, self.size, self.text)
        from .textkit import wrap
        self.lines = wrap(self.text, self.fnt, maxw)
        self.lh = int(self.size * 1.2)

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        img = c.background()
        typing = max(0.3, self.duration * 0.65)
        shown = int(len(self.text) * min(1.0, max(0.0, (t - 0.2) / typing)))
        total_h = self.lh * len(self.lines)
        y0 = c.cy - total_h / 2
        d = ImageDraw.Draw(img)
        left = shown
        cursor_xy = None
        for i, line in enumerate(self.lines):
            lw = self.fnt.getlength(line)
            x0 = (c.W - lw) / 2
            part = line[: max(0, min(len(line), left))]
            if part:
                d.text((x0, y0 + i * self.lh), part, font=self.fnt, fill=c.theme.fg)
            cursor_xy = (x0 + self.fnt.getlength(part), y0 + i * self.lh)
            if left <= len(line):
                break
            left -= len(line) + 1
        if cursor_xy and (int(t * 2.4) % 2 == 0 or shown < len(self.text)):
            cx, cy = cursor_xy
            d.rectangle([cx + 4, cy + self.size * 0.1, cx + 4 + max(3, self.size * 0.06), cy + self.size * 1.0],
                        fill=c.theme.accent)
        return img


class ShotScene(Scene):
    """A screenshot with a slow camera move (zoom/pan to a focus point)."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"source", "title", "fit", "zoom", "focus", "focus_to", *CAPTURE_OPTS})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        opts = {k: spec[k] for k in CAPTURE_OPTS if k in spec}
        self.img = load_shot(spec["source"], opts, ctx.workdir, ctx.base_dir, ctx.roots,
                             ctx.allow_private_hosts, ctx.chromium)
        self.fit = spec.get("fit", "card" if ctx.W <= ctx.H else "cover")
        z = spec.get("zoom", [1.0, 1.0])
        self.z0, self.z1 = (z if isinstance(z, list) else [1.0, z])
        f0 = spec.get("focus", [0.5, 0.5])
        self.f0 = f0
        self.f1 = spec.get("focus_to", f0)
        sw, sh = self.img.size
        if self.fit == "card":
            maxw, maxh = ctx.W * (0.90 if ctx.vertical else 0.84), ctx.H * (0.56 if ctx.vertical else 0.74)
            s = min(maxw / sw, maxh / sh)
            self.cw, self.ch = int(sw * s), int(sh * s)
            self.cx, self.cyy = (ctx.W - self.cw) // 2, int(ctx.cy - self.ch / 2)
            r = int(22 * ctx.u)
            mask = Image.new("L", (self.cw, self.ch), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, self.cw - 1, self.ch - 1], r, fill=255)
            self.round_mask = mask
            pad = int(60 * ctx.u)
            sh_mask = Image.new("L", (self.cw + 2 * pad, self.ch + 2 * pad), 0)
            ImageDraw.Draw(sh_mask).rounded_rectangle([pad, pad, pad + self.cw, pad + self.ch], r, fill=150)
            self.shadow = sh_mask.filter(ImageFilter.GaussianBlur(24 * ctx.u))
            self.pad = pad
            self.radius = r

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        p = ease_in_out(t / self.duration)
        z = self.z0 + (self.z1 - self.z0) * p
        fx = self.f0[0] + (self.f1[0] - self.f0[0]) * p
        fy = self.f0[1] + (self.f1[1] - self.f0[1]) * p
        sw, sh = self.img.size
        if self.fit == "cover":
            s0 = max(c.W / sw, c.H / sh)
            vw, vh = c.W / (s0 * z), c.H / (s0 * z)
            out_size = (c.W, c.H)
        else:
            vw, vh = sw / z, sh / z
            out_size = (self.cw, self.ch)
        x0 = min(max(fx * sw - vw / 2, 0), sw - vw)
        y0 = min(max(fy * sh - vh / 2, 0), sh - vh)
        view = self.img.resize(out_size, Image.BICUBIC, box=(x0, y0, x0 + vw, y0 + vh))
        if self.fit == "cover":
            return view
        canvas = c.background()
        canvas.paste((0, 0, 0), (self.cx - self.pad, self.cyy - self.pad + int(14 * c.u)), self.shadow)
        canvas.paste(view, (self.cx, self.cyy), self.round_mask)
        ImageDraw.Draw(canvas).rounded_rectangle(
            [self.cx, self.cyy, self.cx + self.cw - 1, self.cyy + self.ch - 1], self.radius,
            outline=c.theme.line, width=max(1, int(2 * c.u)))
        return canvas


class BulletsScene(Scene):
    FIELDS: ClassVar[frozenset[str]] = frozenset({"items", "title", "stagger"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        th, u = ctx.theme, ctx.u
        self.items = [str(i) for i in spec["items"]]
        textw = ctx.W - 2 * ctx.margin - 70 * u
        self.head = ctx.block(str(spec.get("title", "")).upper(), "sans_bold", 30 * u, ctx.W - 2 * ctx.margin, tracking=6 * u) if spec.get("title") else None
        avail = ctx.H * (0.60 if ctx.vertical else 0.62)
        size = 56 * u * ctx.fs
        while True:
            blocks = [ctx.block(s, ctx.body, size, textw, spacing=1.25, min_size=size) for s in self.items]
            gap = size * 0.5
            total = sum(b.height for b in blocks) + gap * (len(blocks) - 1)
            if total <= avail or size <= 26:
                break
            size -= 2
        self.blocks, self.gap = blocks, gap
        self.total = total
        self.th = th
        stagger = spec.get("stagger")
        self.stagger = float(stagger) if stagger is not None else None  # replaces the computed step when set
        if self.stagger is not None and 0.5 + (len(self.items) - 1) * self.stagger + 0.5 > self.duration:
            ctx.warn(f"bullets stagger {self.stagger:g}s: the last of {len(self.items)} items is not fully shown "
                     f"before the {self.duration:g}s scene ends")

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        img = c.background()
        step = (self.stagger if self.stagger is not None
                else min(1.1, max(0.4, (self.duration - 1.2) / max(1, len(self.items)))))
        y = c.cy - self.total / 2 + (20 * c.u if self.head else 0)
        if self.head:
            a = _appear(t, 0.1)
            blit(img, self.head, (c.margin, y - 70 * c.u), self.th.accent2, a, "tl")
        d = ImageDraw.Draw(img)
        for i, b in enumerate(self.blocks):
            a = _appear(t, 0.5 + i * step)
            yy = y + (1 - a) * 18 * c.u
            r = 9 * c.u
            my = yy + b.size * 0.62
            if a > 0:
                col = lerp_color(self.th.bg, self.th.accent, a)
                d.polygon([(c.margin + r, my - r), (c.margin + 2 * r, my), (c.margin + r, my + r), (c.margin, my)], fill=col)
            blit(img, b, (c.margin + 56 * c.u, yy), self.th.fg, a, "tl")
            y += b.height + self.gap
        return img


class StatScene(Scene):
    FIELDS: ClassVar[frozenset[str]] = frozenset({"value", "label", "eyebrow", "count"})
    COUNT_CACHE = 64  # rendered value blocks kept while counting
    COUNT_SHARE = 0.6  # `count: true` counts for this share of the scene

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, maxw = ctx.u, ctx.W - 2 * ctx.margin
        self.eyebrow = ctx.block(str(spec["eyebrow"]).upper(), "sans_bold", 28 * u, maxw, tracking=7 * u) if spec.get("eyebrow") else None
        self.value = ctx.block(str(spec["value"]), ctx.display, 170 * u * ctx.fs, maxw, max_h=260 * u, align="center", spacing=1.1)
        self.label = ctx.block(str(spec["label"]), ctx.body, 44 * u, maxw, max_h=200 * u, align="center") if spec.get("label") else None
        self.count = None
        count = spec.get("count")
        if count:
            self.count = split_count(str(spec["value"]))
            self.count_from = float(count.get("from", 0)) if isinstance(count, dict) else 0.0
            secs = count.get("seconds") if isinstance(count, dict) else None
            self.count_seconds = float(secs) if secs is not None else self.duration * self.COUNT_SHARE
            self._blocks: dict[str, TextBlock] = {}
            self._maxw, self._maxh = maxw, 260 * u
            self._start = 0.1 + (1 if self.eyebrow else 0) * 0.3  # the value fades in, then counts
            from_text = self.count.format(self.count_from)
            if from_text != str(spec["value"]):
                wide = make_block(from_text, ctx.display, self.value.size, 10 * maxw, min_size=self.value.size).width
                if wide > self.value.width:
                    ctx.warn(f"count-up of {str(spec['value'])[:40]!r} starts wider ({from_text[:40]!r}) than its "
                             "final text, so it may crowd the edges or move while it counts")
            if self._start + self.count_seconds > self.duration:
                ctx.warn(f"count-up of {str(spec['value'])[:40]!r} does not finish before the scene ends")

    def shown_text(self, t: float) -> str:
        """The value text at scene time t: the counted number, or exactly the final text once it is done."""
        if self.count is None:
            return str(self.spec["value"])
        p = ease_out((t - self._start) / self.count_seconds)
        if p >= 1.0:
            return str(self.spec["value"])
        return self.count.format(self.count_from + (self.count.target - self.count_from) * p)

    def _value_block(self, text: str) -> TextBlock:
        """The value block for `text`, at the final text's size so the number does not jump while it counts."""
        if text == str(self.spec["value"]):
            return self.value
        blk = self._blocks.pop(text, None)
        if blk is None:
            c = self.ctx
            blk = make_block(text, c.display, self.value.size, self._maxw, max_h=self._maxh, align="center",
                             spacing=1.1, min_size=self.value.size)
        self._blocks[text] = blk  # most recently used last
        while len(self._blocks) > self.COUNT_CACHE:
            self._blocks.pop(next(iter(self._blocks)))
        return blk

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        img = c.background()
        value = self._value_block(self.shown_text(t)) if self.count is not None else self.value
        parts = [(b, col, g) for b, col, g in [
            (self.eyebrow, c.theme.accent2, 34), (self.value, c.theme.accent, 34), (self.label, c.theme.fg, 0)] if b]
        total = sum(b.height + g * c.u for b, _, g in parts)
        y = c.cy - total / 2
        for i, (b, col, g) in enumerate(parts):
            a = _appear(t, 0.1 + i * 0.3)
            blit(img, value if b is self.value else b, (c.W / 2, y + (1 - a) * 20 * c.u), col, a, "tc")
            y += b.height + g * c.u
        return img


_KEYWORDS = {"def", "class", "return", "import", "from", "if", "else", "elif", "for", "while", "in", "not", "and",
             "or", "with", "as", "try", "except", "raise", "yield", "await", "async", "const", "let", "var",
             "function", "export", "interface", "type", "new", "true", "false", "None", "True", "False", "null"}
_TOKEN = re.compile(r"(#.*|//.*)|(\"[^\"]*\"|'[^']*')|([A-Za-z_]\w*)|(\s+|.)")


def _colorize(line: str, th: Theme) -> list[Color]:
    cols: list[Color] = []
    for m in _TOKEN.finditer(line):
        txt = m.group(0)
        if m.group(1):
            col = th.muted
        elif m.group(2):
            col = th.accent2
        elif m.group(3) and txt in _KEYWORDS:
            col = th.accent
        else:
            col = th.fg
        cols.extend([col] * len(txt))
    return cols


class CodeScene(Scene):
    FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "highlight", "title"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u = ctx.u
        self.lines = str(spec["code"]).rstrip("\n").split("\n")
        self.highlight = set(spec.get("highlight", []))
        self.title = ctx.block(str(spec["title"]), "sans_bold", 30 * u, ctx.W - 2 * ctx.margin) if spec.get("title") else None
        self.pw = ctx.W - 2 * ctx.margin
        pad = 36 * u
        longest = max(len(l) for l in self.lines)
        size = min(40 * u, (self.pw - 2 * pad) / (longest * 0.602))
        size = min(size, (ctx.H * 0.6 - 2 * pad) / (len(self.lines) * 1.45))
        if size < 20 * u * 0.8:
            ctx.warn(f"code scene text is small ({int(size)}px); shorten lines or reduce line count")
        self.size = max(12, int(size))
        self.fnt = font("mono", self.size)
        self.adv = self.fnt.getlength("M")
        self.lh = int(self.size * 1.45)
        self.pad = int(pad)
        self.ph = self.lh * len(self.lines) + 2 * self.pad
        self.cols = [_colorize(l, ctx.theme) for l in self.lines]
        self.total_chars = sum(len(l) + 1 for l in self.lines)

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        th = c.theme
        img = c.background()
        d = ImageDraw.Draw(img)
        top = int(c.cy - self.ph / 2)
        if self.title:
            blit(img, self.title, (c.margin, top - 54 * c.u), th.accent2, _appear(t, 0.05), "tl")
        d.rounded_rectangle([c.margin, top, c.margin + self.pw, top + self.ph], int(20 * c.u), fill=th.card, outline=th.line, width=max(1, int(2 * c.u)))
        reveal = int(self.total_chars * min(1.0, max(0.0, (t - 0.3) / (self.duration * 0.6))))
        done = t > 0.3 + self.duration * 0.6
        for i, (line, cols) in enumerate(zip(self.lines, self.cols), start=1):
            if reveal <= 0:
                break
            y = top + self.pad + (i - 1) * self.lh
            n = min(len(line), reveal)
            if i in self.highlight and done:
                ha = _appear(t, 0.3 + self.duration * 0.6, 0.4)
                d.rectangle([c.margin + 6, y - 4, c.margin + self.pw - 6, y + self.lh - 8], fill=lerp_color(th.card, th.accent, 0.16 * ha))
                d.rectangle([c.margin + 6, y - 4, c.margin + 12, y + self.lh - 8], fill=lerp_color(th.card, th.accent, ha))
            for j in range(n):
                ch = line[j]
                if ch != " ":
                    d.text((c.margin + self.pad + j * self.adv, y), ch, font=self.fnt, fill=cols[j])
            reveal -= len(line) + 1
        return img


_COLOR_NAMES = ("accent", "accent2", "fg", "muted")


class DiagramScene(Scene):
    """Boxes and arrows that reveal in order, with flowing packets and state flashes."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"nodes", "edges", "flash", "title", "node_size"})
    NODE_FIELDS: ClassVar[frozenset[str]] = frozenset({"id", "label", "x", "y", "vx", "vy", "at", "kind", "sub"})
    EDGE_FIELDS: ClassVar[frozenset[str]] = frozenset({"from", "to", "label", "at", "color", "bad", "dashed", "flow"})
    FLASH_FIELDS: ClassVar[frozenset[str]] = frozenset({"node", "at", "text", "color"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u = ctx.u
        th = ctx.theme
        W, H = ctx.W, ctx.H
        size = spec.get("node_size", 54 if ctx.vertical else 44) * u
        self.nodes: dict[str, dict[str, Any]] = {}
        t_auto = 0.35
        for n in spec["nodes"]:
            x = n.get("vx", n["x"]) if ctx.vertical else n["x"]
            y = n.get("vy", n["y"]) if ctx.vertical else n["y"]
            blk = ctx.block(str(n["label"]), "sans_bold", size, W * 0.34, align="center", spacing=1.2, min_size=size * 0.7)
            w, h = blk.width + 56 * u, blk.height + 36 * u
            px = min(max(x * W, w / 2 + ctx.margin * 0.5), W - w / 2 - ctx.margin * 0.5)
            py = y * H
            self.nodes[n["id"]] = dict(blk=blk, w=w, h=h, x=px, y=py, kind=n.get("kind", "service"),
                                       appear=float(n.get("at", t_auto)), sub=n.get("sub"))
            t_auto += 0.45
        self.edges = []
        prev = 0.0
        for e in spec.get("edges", []):
            a, b = self.nodes[e["from"]], self.nodes[e["to"]]
            at = float(e.get("at", max(prev + 0.7, max(a["appear"], b["appear"]) + 0.3)))
            prev = at
            color = e.get("color", "accent" if e.get("bad") else "accent2")
            if color not in _COLOR_NAMES:
                raise ValueError(f"edge color must be one of {_COLOR_NAMES}")
            label = ctx.block(str(e["label"]), "sans_bold", (34 if ctx.vertical else 30) * u, W * 0.4, align="center") if e.get("label") else None
            ddx, ddy = b["x"] - a["x"], b["y"] - a["y"]
            dd = math.hypot(ddx, ddy) or 1.0
            self.edges.append(dict(ux=ddx / dd, uy=ddy / dd, a=a, b=b, at=at, color=color, dashed=bool(e.get("dashed") or e.get("bad")),
                                   flow=e.get("flow", True), label=label))
        self.flash = []
        for f in spec.get("flash", []):
            color = f.get("color", "accent")
            tag = ctx.block(str(f["text"]), "sans_bold", (36 if ctx.vertical else 30) * u, W * 0.4, align="center") if f.get("text") else None
            self.flash.append(dict(node=self.nodes[f["node"]], at=float(f["at"]), color=color, tag=tag))
        self.title = ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * ctx.margin, align="center") if spec.get("title") else None

    def _col(self, name: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
        r, g, b = getattr(self.ctx.theme, name)
        return (r, g, b, int(255 * alpha))

    def frame(self, t: float) -> Image.Image:
        c = self.ctx
        th = c.theme
        k = c.u * SS
        layer = Image.new("RGBA", (c.W * SS, c.H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)

        # edges
        for e in self.edges:
            a, b = e["a"], e["b"]
            p = ease_in_out((t - e["at"]) / 0.55)
            if p <= 0:
                continue
            dx, dy = b["x"] - a["x"], b["y"] - a["y"]
            dist = math.hypot(dx, dy) or 1.0
            ux, uy = dx / dist, dy / dist

            def exit_t(n: dict[str, Any], sx: float) -> float:
                tx = (n["w"] / 2) / abs(ux) if abs(ux) > 1e-6 else 1e9
                ty = (n["h"] / 2) / abs(uy) if abs(uy) > 1e-6 else 1e9
                return min(tx, ty) + 10 * c.u * sx

            s = exit_t(a, 1)
            f = dist - exit_t(b, 1)
            if f <= s:
                continue
            x1, y1 = (a["x"] + ux * s) * SS, (a["y"] + uy * s) * SS
            x2, y2 = (a["x"] + ux * f) * SS, (a["y"] + uy * f) * SS
            ex, ey = x1 + (x2 - x1) * p, y1 + (y2 - y1) * p
            col = self._col(e["color"], 0.95)
            width = int(5 * k)
            if e["dashed"]:
                seg, gap = 22 * k, 14 * k
                full = math.hypot(x2 - x1, y2 - y1) or 1.0
                vx, vy = (x2 - x1) / full, (y2 - y1) / full
                length = math.hypot(ex - x1, ey - y1)
                pos = 0.0
                while pos < length:
                    q = min(pos + seg, length)
                    d.line([(x1 + vx * pos, y1 + vy * pos), (x1 + vx * q, y1 + vy * q)], fill=col, width=width)
                    pos += seg + gap
            else:
                d.line([(x1, y1), (ex, ey)], fill=col, width=width)
            if p >= 0.98:
                ah = 22 * k
                px, py = -uy, ux
                d.polygon([(x2, y2), (x2 - ux * ah + px * ah * 0.55, y2 - uy * ah + py * ah * 0.55),
                           (x2 - ux * ah - px * ah * 0.55, y2 - uy * ah - py * ah * 0.55)], fill=col)
                if e["flow"]:
                    for off in (0.0, 0.5):
                        q = (((t - e["at"] - 0.55) / 1.3) + off) % 1.0
                        if t - e["at"] > 0.55:
                            qx, qy = x1 + (x2 - x1) * q, y1 + (y2 - y1) * q
                            r = 9 * k
                            d.ellipse([qx - r, qy - r, qx + r, qy + r], fill=self._col("fg", 0.95))

        # nodes
        fills = {"client": th.card, "service": th.card,
                 "cache": lerp_color(th.card, th.accent2, 0.22), "db": lerp_color(th.card, th.accent, 0.20)}
        outlines = {"client": "fg", "service": "accent2", "cache": "accent2", "db": "accent"}
        for n in self.nodes.values():
            a = ease_out((t - n["appear"]) / 0.4)
            if a <= 0:
                continue
            w, h = n["w"] * (0.92 + 0.08 * a), n["h"] * (0.92 + 0.08 * a)
            box = [(n["x"] - w / 2) * SS, (n["y"] - h / 2) * SS, (n["x"] + w / 2) * SS, (n["y"] + h / 2) * SS]
            kind = n["kind"] if n["kind"] in fills else "service"
            out_name = outlines[kind]
            flashed = [f for f in self.flash if f["node"] is n and t >= f["at"]]
            width = int(4 * k)
            if flashed:
                f = flashed[-1]
                out_name = f["color"]
                pulse = max(0.0, 1 - (t - f["at"]) / 0.7)
                width = int((4 + 6 * pulse) * k)
            rad = int(18 * k) if kind != "client" else int(h / 2 * SS)
            fr, fg_, fb = fills[kind]
            d.rounded_rectangle(box, rad, fill=(fr, fg_, fb, int(255 * a)), outline=self._col(out_name, a), width=width)
            if kind == "db":
                d.rounded_rectangle([box[0] + 10 * k, box[1] + 10 * k, box[2] - 10 * k, box[3] - 10 * k], int(12 * k),
                                    outline=self._col("accent", a * 0.45), width=max(1, int(2 * k)))
        d_small = layer.resize((c.W, c.H), Image.LANCZOS)
        canvas = c.background()
        canvas.paste(d_small, (0, 0), d_small)

        # text (drawn at 1x on the composite for crisp glyphs)
        if self.title:
            blit(canvas, self.title, (c.W / 2, c.H * 0.09), th.fg, _appear(t, 0.05), "tc")
        for n in self.nodes.values():
            a = ease_out((t - n["appear"]) / 0.4)
            blit(canvas, n["blk"], (n["x"], n["y"]), th.fg, a, "mc")
        for e in self.edges:
            if e["label"] and t >= e["at"] + 0.3:
                a = _appear(t, e["at"] + 0.3, 0.3)
                mx, my = (e["a"]["x"] + e["b"]["x"]) / 2, (e["a"]["y"] + e["b"]["y"]) / 2
                lb = e["label"]
                # keep the label beside the arrow, never on top of it
                if abs(e["uy"]) > abs(e["ux"]):
                    mx += lb.width / 2 + 30 * c.u
                else:
                    my -= lb.height / 2 + 24 * c.u
                blit(canvas, lb, (mx, my), getattr(th, e["color"]), a, "mc")
        for f in self.flash:
            if f["tag"] and t >= f["at"]:
                n = f["node"]
                a = _appear(t, f["at"], 0.3)
                if c.vertical:  # arrows run vertically, so put the tag beside the node
                    blit(canvas, f["tag"], (n["x"] + n["w"] / 2 + 22 * c.u, n["y"]), getattr(th, f["color"]), a, "ml")
                else:
                    blit(canvas, f["tag"], (n["x"], n["y"] + n["h"] / 2 + 20 * c.u), getattr(th, f["color"]), a, "tc")
        return canvas


SCENE_TYPES = {"title": TitleScene, "type": TypeScene, "shot": ShotScene, "diagram": DiagramScene,
               "bullets": BulletsScene, "code": CodeScene, "stat": StatScene}


def build_scene(spec: dict[str, Any], ctx: Ctx) -> Scene:
    return SCENE_TYPES[spec["type"]](spec, ctx)


def make_caption(ctx: Ctx, text: str) -> tuple[Image.Image, Image.Image]:
    """Return (pill_rgb, pill_mask) for a caption lower-third."""
    th, u = ctx.theme, ctx.u
    blk = ctx.block(text, ctx.body, 46 * u * ctx.fs, ctx.W * 0.84, align="center", spacing=1.2)
    padx, pady = int(40 * u), int(22 * u)
    w, h = blk.width + 2 * padx, blk.height + 2 * pady
    rgb = Image.new("RGB", (w, h), th.card)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], int(24 * u), fill=240)
    d = ImageDraw.Draw(rgb)
    d.rounded_rectangle([0, 0, w - 1, h - 1], int(24 * u), outline=th.accent2, width=max(1, int(2 * u)))
    rgb.paste(th.fg, (padx, pady), blk.mask)
    return rgb, mask
