"""Text measuring, wrapping, fitting and drawing helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .theme import Color, font, has_cjk


@dataclass
class TextBlock:
    mask: Image.Image  # 'L' coverage mask
    width: int
    height: int
    size: int
    shrunk: bool


def wrap(text: str, fnt, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        tokens = list(para) if has_cjk(para) else para.split(" ")
        sep = "" if has_cjk(para) else " "
        cur = ""
        for tok in tokens:
            trial = tok if not cur else cur + sep + tok
            if fnt.getlength(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = tok
        lines.append(cur)
    return lines


def _tracked_length(s: str, fnt, tracking: float) -> float:
    return sum(fnt.getlength(c) + tracking for c in s) - (tracking if s else 0)


def make_block(text: str, kind: str, size: float, max_w: float, max_h: float | None = None,
               min_size: float = 18, spacing: float = 1.28, align: str = "left",
               tracking: float = 0.0) -> TextBlock:
    """Wrap text to max_w and shrink until it also fits max_h. Never overflows silently:
    `shrunk` reports when the requested size could not be kept."""
    requested = size
    while True:
        fnt = font(kind, size, text)
        lines = wrap(text, fnt, max_w) if not tracking else [text]
        lh = int(size * spacing)
        h = lh * len(lines)
        widest = max((_tracked_length(l, fnt, tracking) if tracking else fnt.getlength(l)) for l in lines)
        fits = (max_h is None or h <= max_h) and widest <= max_w + 1
        if fits or size <= min_size:
            break
        size -= 2
    w = int(widest) + 4
    mask = Image.new("L", (max(w, 1), max(h + int(size * 0.3), 1)), 0)
    d = ImageDraw.Draw(mask)
    for i, line in enumerate(lines):
        lw = _tracked_length(line, fnt, tracking) if tracking else fnt.getlength(line)
        x = {"left": 0, "center": (w - lw) / 2, "right": w - lw}[align]
        y = i * lh
        if tracking:
            for ch in line:
                d.text((x, y), ch, font=fnt, fill=255)
                x += fnt.getlength(ch) + tracking
        else:
            d.text((x, y), line, font=fnt, fill=255)
    return TextBlock(mask, w, h, int(size), size < requested - 1)


def blit(canvas: Image.Image, block: TextBlock, xy: tuple[float, float], color: Color,
         alpha: float = 1.0, anchor: str = "tl") -> None:
    x, y = xy
    if anchor[0] == "m":
        y -= block.height / 2
    elif anchor[0] == "b":
        y -= block.height
    if anchor[1] == "c":
        x -= block.width / 2
    elif anchor[1] == "r":
        x -= block.width
    # anchor[1] == "l" (or "tl"/"ml" style) keeps x as the left edge
    if alpha <= 0:
        return
    mask = block.mask if alpha >= 1 else block.mask.point(lambda v: int(v * alpha))
    canvas.paste(color, (int(x), int(y)), mask)


def lerp_color(a: Color, b: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 3 * t * t - 2 * t * t * t


def ease_back(t: float) -> float:
    """Ease out with a small overshoot (a drop that settles)."""
    t = max(0.0, min(1.0, t)) - 1.0
    c1 = 1.70158
    return 1 + (c1 + 1) * t ** 3 + c1 * t ** 2


def ease_linear(t: float) -> float:
    return max(0.0, min(1.0, t))


# Named easings. Easing is internal: no spec key picks one per element.
EASINGS = {"linear": ease_linear, "out": ease_out, "in_out": ease_in_out, "back": ease_back}


def ease(name: str, t: float) -> float:
    """Apply the easing `name` (one of EASINGS) to t; t is clamped to 0..1 first."""
    return EASINGS[name](t)


# --- count-up numbers -----------------------------------------------------------------------------------

# One number: optional sign (not a hyphen glued to a word or digit, as in "COVID-19" or "10-20"), digits with
# well-formed thousands groups or none, optional decimals. ASCII digits only; a trailing "," or "." is not part of it.
NUMBER_RE = re.compile(r"(?:(?<![A-Za-z0-9])[-+])?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", re.ASCII)
MAX_COUNT_MAGNITUDE = 1e15
MAX_COUNT_DECIMALS = 6


@dataclass(frozen=True)
class CountParts:
    """A text with exactly one number in it, taken apart so the number can be counted up."""
    prefix: str
    suffix: str
    target: float
    decimals: int
    grouped: bool    # the number is written with thousands separators
    signed: bool     # the number is written with an explicit "+"

    def format(self, value: float) -> str:
        """The text with `value` in place of the number. The format spec is built here from flags, never from
        user text, so nothing a spec says reaches str.format."""
        spec = ("+" if self.signed else "") + ("," if self.grouped else "") + f".{self.decimals}f"
        out = format(value, spec)
        if out.lstrip("+-").strip("0.,") == "":  # "-0" and "-0.0" while counting up from below zero
            out = ("+" if self.signed else "") + out.lstrip("+-")
        return self.prefix + out + self.suffix


def split_count(text: str) -> CountParts:
    """Parse the one number in `text`. Raises ValueError when there is not exactly one, or it cannot be counted."""
    found = list(NUMBER_RE.finditer(text))
    if len(found) != 1:
        raise ValueError("value must contain exactly one number to count")
    m = found[0]
    raw = m.group(0)
    digits = raw.replace(",", "")
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    if decimals > MAX_COUNT_DECIMALS:
        raise ValueError(f"the number in value has {decimals} decimals; the limit is {MAX_COUNT_DECIMALS}")
    target = float(digits)
    if abs(target) > MAX_COUNT_MAGNITUDE:
        raise ValueError("the number in value is too large to count")
    return CountParts(text[:m.start()], text[m.end():], target, decimals, "," in raw, raw.startswith("+"))


@dataclass(frozen=True)
class WordBox:
    """One word placed by `layout_words`: its text and its box inside the laid-out block."""
    text: str
    x: float
    y: float
    w: float
    h: float
    line: int


def layout_words(words: list[str], fnt, max_w: float, space: float | None = None,
                 line_h: float | None = None) -> tuple[list[WordBox], float, float]:
    """Place `words` on lines of at most `max_w` (greedy), each line centred. Returns (boxes, block width,
    block height). A word wider than `max_w` gets a line of its own. Every box is `line_h` tall (default 1.28 em)
    and the gap between words is `space` (default a quarter em)."""
    size = float(getattr(fnt, "size", 20))
    space = size * 0.25 if space is None else space
    line_h = size * 1.28 if line_h is None else line_h
    lines: list[list[tuple[str, float]]] = [[]]
    used = 0.0
    for w in words:
        wl = float(fnt.getlength(w))
        if lines[-1] and used + space + wl > max_w:
            lines.append([])
            used = 0.0
        used += (space if lines[-1] else 0.0) + wl
        lines[-1].append((w, wl))
    widths = [sum(wl for _, wl in ln) + space * (len(ln) - 1) for ln in lines]
    block_w = max(widths, default=0.0)
    boxes: list[WordBox] = []
    for n, (ln, lw) in enumerate(zip(lines, widths)):
        x = (block_w - lw) / 2
        for w, wl in ln:
            boxes.append(WordBox(w, x, n * line_h, wl, line_h, n))
            x += wl + space
    return boxes, block_w, line_h * len(lines)
