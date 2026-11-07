"""Data scenes: a table whose rows appear one by one with a scan bar, an array of cells that moves, and the `cta`
end card (a question bubble with lettered answers).

The table and the array animate the data itself instead of boxes around text. The step logic of the array is a pure function
(`array_states`), so a spec is replayed when it is loaded and a bad step is reported with its path and time.
All text is rendered to blocks once in `__init__`; `frame(t)` only composites.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from PIL import Image, ImageDraw

from .scenebase import Ctx, Scene, _appear
from .textkit import MAX_COUNT_DECIMALS, TextBlock, blit, ease, ease_out, lerp_color, make_block
from .theme import font

MAX_TABLE_ROWS = 12
MAX_TABLE_COLS = 6
MAX_CELLS = 12            # cells in an array at any moment
MAX_STEPS = 40
MAX_CELL_CHARS = 40       # characters in one table cell or array cell
MIN_READABLE = 22         # a cell font smaller than this (in 1080-wide units) is reported
COLOR_NAMES = ("accent", "accent2", "fg", "muted")

FIRST_ROW_AT = 0.35       # the first table row appears here
TABLE_STAGGER = 0.4       # seconds between rows when `stagger` is not set
SCAN_PER_ROW = 0.3
COUNTER_SECONDS = 2.0
ARRAY_STAGGER = 0.8       # seconds between array steps that have no `at`; the first one is at ARRAY_STAGGER
MOVE_SECONDS = 0.4        # how long cells take to move into their new places
GHOST_SECONDS = MOVE_SECONDS / 1.3   # a cell that left has faded out by then
ARRAY_ACTIONS = ("insert", "push", "pop", "mark", "unmark", "window")
POP_SIDES = ("front", "back")
MAX_ANSWERS = 4           # A to D
MAX_QUESTION_CHARS = 200
MAX_ANSWER_CHARS = 60
MAX_PROMPT_CHARS = 80
DEFAULT_CTA_PROMPT = "Answer in the comments"
CTA_LETTERS = "ABCD"


# --- array: pure step logic -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Cell:
    id: int      # stable for the life of the cell, so a renderer can follow it as the cells move
    text: str


@dataclass(frozen=True)
class ArrayState:
    """The array after one step (or at the start)."""
    t: float                                  # when the step happens
    cells: tuple[Cell, ...]
    marks: tuple[int, ...] = ()               # ids of the marked cells
    mark_text: str = ""
    mark_color: str = "accent"
    window: tuple[int, int] | None = None     # first and last cell index, inclusive
    window_text: str = ""
    window_color: str = "accent2"
    step: int | None = None                   # index into the spec's `steps` (None for the start)
    action: str | None = None                 # insert | push | pop | mark | unmark | window
    added: int | None = None                  # id of the cell that came in with this step
    removed: Cell | None = None               # the cell that left with this step
    side: str | None = None                   # which end it left from: front | back


class ArrayStepError(ValueError):
    """A step that cannot be applied. `str()` reads like `steps[3].pop: array is empty at 2.4s`."""

    def __init__(self, step: int, key: str, problem: str, at: float):
        self.step, self.key, self.problem, self.at = step, key, problem, at
        super().__init__(f"steps[{step}]{'.' + key if key else ''}: {problem} at {at:g}s")


_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?\Z")


def sort_key(text: str) -> tuple[int, float, str]:
    """How `insert` orders cells: numbers by value and before text, text without regard to case."""
    return (0, float(text), "") if _NUMBER.match(text.strip()) else (1, 0.0, text.casefold())


def step_times(steps: list[dict[str, Any]], stagger: float = ARRAY_STAGGER) -> list[float]:
    """The time of every step. A step without `at` comes `stagger` after the one before it (the first at
    `stagger`)."""
    out: list[float] = []
    prev = 0.0
    for s in steps:
        at = float(s["at"]) if s.get("at") is not None else prev + stagger
        out.append(at)
        prev = at
    return out


def _state_after(prev: ArrayState, **changes: Any) -> ArrayState:
    return ArrayState(**{**{f: getattr(prev, f) for f in ArrayState.__dataclass_fields__}, "added": None,
                         "removed": None, "side": None, **changes})


def array_states(spec: dict[str, Any]) -> list[ArrayState]:
    """Replay the array's steps in time order (ties keep the order of the list). Returns the start state and one
    state per step. Raises ArrayStepError for a step that cannot be applied: pop on an empty array, a mark or
    window outside the cells, or more than MAX_CELLS cells."""
    steps = list(spec.get("steps") or [])
    stagger = float(spec.get("stagger") or ARRAY_STAGGER)
    times = step_times(steps, stagger)
    cells = tuple(Cell(i, str(x)) for i, x in enumerate(spec.get("items") or []))
    if len(cells) > MAX_CELLS:
        raise ArrayStepError(0, "", f"array starts with {len(cells)} cells; the limit is {MAX_CELLS} (MAX_CELLS)", 0.0)
    next_id = len(cells)
    state = ArrayState(0.0, cells)
    states = [state]
    for k in sorted(range(len(steps)), key=lambda k: (times[k], k)):
        step, at = steps[k], times[k]
        given = [a for a in ARRAY_ACTIONS if a in step]
        if len(given) != 1:
            found = f", found {' and '.join(given)}" if given else ""
            raise ArrayStepError(k, "", f"needs exactly one of {', '.join(ARRAY_ACTIONS)}{found}", at)
        action = given[0]
        cells = state.cells
        n = len(cells)
        if action in ("insert", "push"):
            if n + 1 > MAX_CELLS:
                raise ArrayStepError(k, action, f"array would have {n + 1} cells; the limit is {MAX_CELLS} (MAX_CELLS)", at)
            new = Cell(next_id, str(step[action]))
            next_id += 1
            pos = n
            if action == "insert":
                key = sort_key(new.text)
                pos = next((i for i, c in enumerate(cells) if sort_key(c.text) > key), n)
            state = _state_after(state, t=at, cells=cells[:pos] + (new,) + cells[pos:], step=k, action=action,
                                 added=new.id, window=_clamp_window(state.window, n + 1))
        elif action == "pop":
            if n == 0:
                raise ArrayStepError(k, "pop", "array is empty", at)
            side = str(step["pop"])
            gone = cells[0] if side == "front" else cells[-1]
            left = tuple(c for c in cells if c.id != gone.id)
            marks = tuple(m for m in state.marks if m != gone.id)
            state = _state_after(state, t=at, cells=left, marks=marks, mark_text=state.mark_text if marks else "",
                                 step=k, action="pop", removed=gone, side=side, window=_clamp_window(state.window, n - 1))
        elif action == "mark":
            idx = list(step["mark"])
            bad = [i for i in idx if not 0 <= i < n]
            if bad:
                raise ArrayStepError(k, "mark", f"index {bad[0]} is outside the {n} cells (0..{n - 1})" if n
                                     else f"index {bad[0]} is outside the array, which is empty", at)
            ids = tuple(dict.fromkeys(cells[i].id for i in idx))
            state = _state_after(state, t=at, marks=ids, mark_text=str(step.get("text", "")),
                                 mark_color=str(step.get("color", "accent")), step=k, action="mark")
        elif action == "unmark":
            state = _state_after(state, t=at, marks=(), mark_text="", window=None, window_text="", step=k, action="unmark")
        else:  # window
            lo, hi = step["window"]
            if not (0 <= lo <= hi < n):
                raise ArrayStepError(k, "window", f"[{lo}, {hi}] is outside the {n} cells (0..{n - 1})" if n
                                     else f"[{lo}, {hi}] is outside the array, which is empty", at)
            state = _state_after(state, t=at, window=(lo, hi), window_text=str(step.get("text", "")),
                                 window_color=str(step.get("color", "accent2")), step=k, action="window")
        states.append(state)
    return states


def _clamp_window(window: tuple[int, int] | None, n: int) -> tuple[int, int] | None:
    """A window keeps its indices when cells come and go, but never reaches past the last cell."""
    if window is None or n == 0:
        return None
    lo, hi = window
    hi = min(hi, n - 1)
    return (lo, hi) if lo <= hi else None


# --- shared drawing helpers -----------------------------------------------------------------------------

def fill_margin(ctx: Ctx) -> int:
    """Side margin of a data scene. Vertical frames are read on phones, so the content takes 90% of the width."""
    return int(ctx.W * (0.05 if ctx.vertical else 0.08))


def centre_y(ctx: Ctx) -> float:
    """Where a data scene centres itself vertically: the middle of the frame above the caption and subtitle zone
    (which starts at about 0.73 of the height in vertical frames)."""
    return ctx.H * (0.42 if ctx.vertical else 0.45)


def _color(ctx: Ctx, name: str) -> tuple[int, int, int]:
    return getattr(ctx.theme, name)


def decimals_of(value: float) -> int:
    """Decimals needed to write `value` exactly (at most MAX_COUNT_DECIMALS): 12 -> 0, 0.5 -> 1, 3.141 -> 3."""
    text = f"{value:.{MAX_COUNT_DECIMALS}f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def _number_text(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


class _BlockCache:
    """A small least-recently-used cache of rendered text blocks (the counter changes its text every frame)."""

    def __init__(self, size: int = 64):
        self.size, self._d = size, {}

    def get(self, text: str, build: Any) -> TextBlock:
        blk = self._d.pop(text, None)
        if blk is None:
            blk = build(text)
        self._d[text] = blk  # most recently used last
        while len(self._d) > self.size:
            self._d.pop(next(iter(self._d)))
        return blk

    def __len__(self) -> int:
        return len(self._d)


# --- table ----------------------------------------------------------------------------------------------

class TableScene(Scene):
    """Rows appear one by one, a scan bar walks down them, rows can be highlighted, a counter can tick up."""

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"title", "columns", "rows", "stagger", "reveal", "scan", "highlight", "counter"})
    SCAN_FIELDS: ClassVar[frozenset[str]] = frozenset({"at", "per_row", "stop", "color"})
    HIGHLIGHT_FIELDS: ClassVar[frozenset[str]] = frozenset({"row", "at", "text", "color"})
    COUNTER_FIELDS: ClassVar[frozenset[str]] = frozenset({"label", "to", "at", "seconds"})
    REVEALS: ClassVar[tuple[str, ...]] = ("rows", "all")
    COUNTER_CACHE = 64

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, th = ctx.u, ctx.theme
        W, H = ctx.W, ctx.H
        margin = fill_margin(ctx)
        self.columns = [str(x) for x in spec["columns"]]
        self.rows = [[str(x) for x in r] for r in spec["rows"]]
        n, ncols = len(self.rows), len(self.columns)
        stagger = float(spec.get("stagger", TABLE_STAGGER))
        self.appear = [FIRST_ROW_AT if spec.get("reveal") == "all" else FIRST_ROW_AT + i * stagger for i in range(n)]

        scan = spec.get("scan")
        self.scan: dict[str, Any] | None = None
        if scan is not None:
            self.scan = {"at": float(scan.get("at", self.appear[0] + 0.6)),
                         "per_row": float(scan.get("per_row", SCAN_PER_ROW)),
                         "stop": int(scan.get("stop", n - 1)),
                         "color": str(scan.get("color", "accent"))}
        self.highlights: list[dict[str, Any]] = []
        for h in spec.get("highlight") or []:
            self.highlights.append({"row": int(h["row"]), "at": float(h.get("at", self.appear[int(h["row"])] + 0.4)),
                                    "text": str(h["text"]) if h.get("text") else "", "color": str(h.get("color", "accent"))})
        counter = spec.get("counter")
        self.counter: dict[str, Any] | None = None
        if counter is not None:
            to = float(counter["to"])
            self.counter = {"to": to, "decimals": decimals_of(to), "label": str(counter.get("label", "")),
                            "at": float(counter.get("at", self.scan["at"] if self.scan else FIRST_ROW_AT)),
                            "seconds": float(counter.get("seconds", COUNTER_SECONDS))}

        # --- layout: pick the largest text size at which the table fits its area
        avail_w = W - 2 * margin
        self.head = (ctx.block(str(spec["title"]).upper(), "sans_bold", 30 * u, avail_w, tracking=6 * u)
                     if spec.get("title") else None)
        head_h = self.head.height + 34 * u if self.head else 0.0
        side_counter = self.counter is not None and not ctx.vertical
        gap = 44 * u
        counter_w = avail_w * 0.29 if side_counter else 0.0
        table_w = avail_w - (counter_w + gap if side_counter else 0.0)
        counter_h = 0.0
        if self.counter is not None:
            self.cnt_size = (110 if ctx.vertical else 84) * u
            final = make_block(_number_text(self.counter["to"], self.counter["decimals"]), ctx.display, self.cnt_size,
                               avail_w if ctx.vertical else counter_w, max_h=self.cnt_size * 1.4, align="center")
            self.cnt_size = final.size
            self._cnt_w = avail_w if ctx.vertical else counter_w
            self.cnt_final = final
            self.cnt_label = (ctx.block(self.counter["label"], ctx.body, 38 * u, self._cnt_w, align="center",
                                        max_h=120 * u) if self.counter["label"] else None)
            counter_h = final.height + (self.cnt_label.height + 10 * u if self.cnt_label else 0)
            self._cnt_cache = _BlockCache(self.COUNTER_CACHE)
        area_h = H * (0.57 if ctx.vertical else 0.66) - head_h - (counter_h + 34 * u if ctx.vertical and self.counter else 0)
        tag_size_k = 0.72

        def tag_block(text: str, base: float) -> TextBlock:
            # one line, shrunk when it is wider than a third of the table
            return make_block(text, "sans_bold", base * tag_size_k, table_w * 0.3, max_h=base * tag_size_k * 1.3,
                              min_size=10)
        kinds = ("sans_bold", ctx.body)
        size = (60 if ctx.vertical else 40) * u * ctx.fs  # the loop shrinks it until the table fits
        floor = max(12.0, 14 * u)
        while True:
            pad_x = size * 0.55
            row_h = size * (2.2 if ctx.vertical else 1.75)
            tags = [tag_block(h["text"], size) for h in self.highlights if h["text"]]
            tag_w = (max(b.width for b in tags) + 2 * pad_x) if tags else 0.0
            natural = []
            for j in range(ncols):
                wj = font(kinds[0], size, self.columns[j]).getlength(self.columns[j])
                for r in self.rows:
                    wj = max(wj, font(kinds[1], size, r[j]).getlength(r[j]))
                natural.append(wj + 2 * pad_x)
            card_pad = row_h * 0.3
            need_w = sum(natural) + tag_w
            need_h = row_h * (n + 1) + 2 * card_pad
            tags_fit = all(b.height <= row_h * 0.9 for b in tags)  # a tag sits inside its row
            if (need_w <= table_w and need_h <= area_h and tags_fit) or size <= floor:
                break
            size -= 2
        self.size = size
        if size < MIN_READABLE * u:
            ctx.warn(f"table text is small ({int(size)}px); use fewer rows or columns, or shorter cells")
        scale = (table_w - tag_w) / sum(natural)  # columns share the width in proportion to their longest cell
        self.col_w = [w * scale for w in natural]
        self.pad_x, self.row_h, self.tag_w, self.card_pad = pad_x, row_h, tag_w, card_pad
        card_h = need_h
        total_h = head_h + card_h + (counter_h + 34 * u if ctx.vertical and self.counter else 0.0)
        top = max(H * 0.05, centre_y(ctx) - total_h / 2)
        self.head_xy = (margin, top)
        cy0 = top + head_h
        self.card = (margin, cy0, margin + table_w, cy0 + card_h)
        self.header_y = cy0 + card_pad
        self.rows_y0 = self.header_y + row_h
        self.col_x = [margin]
        for w in self.col_w[:-1]:
            self.col_x.append(self.col_x[-1] + w)
        if side_counter:
            self.cnt_xy = (margin + table_w + gap + counter_w / 2, cy0 + card_h / 2 - counter_h / 2)
        elif self.counter is not None:
            self.cnt_xy = (W / 2, cy0 + card_h + 34 * u)

        shrunk: list[int] = []

        def cell(text: str, kind: str, j: int) -> TextBlock:
            # one line, shrunk (and reported) rather than wrapped when it does not fit its column
            blk = make_block(text, kind, size, max(1.0, self.col_w[j] - 2 * pad_x), max_h=size * 1.3,
                             min_size=min(size, 10))
            if blk.shrunk:
                shrunk.append(blk.size)
            return blk

        self.head_cells = [cell(t, kinds[0], j) for j, t in enumerate(self.columns)]
        self.cells = [[cell(t, kinds[1], j) for j, t in enumerate(r)] for r in self.rows]
        self.tags = {id(h): tag_block(h["text"], size) for h in self.highlights if h["text"]}
        shrunk += [b.size for b in self.tags.values() if b.shrunk]
        if shrunk:
            ctx.warn(f"table text shrunk to fit in {len(shrunk)} cells (down to {min(shrunk)}px)")

        # --- cues that cannot be met
        if self.scan is not None:
            for k in range(self.scan["stop"] + 1):
                if self.scan["at"] + k * self.scan["per_row"] < self.appear[k] - 1e-9:
                    ctx.warn(f"table scan reaches row {k} before it appears; start the scan later or lower stagger")
                    break
            end = self.scan["at"] + self.scan["stop"] * self.scan["per_row"]
            if end > self.duration:
                ctx.warn(f"table scan does not reach row {self.scan['stop']} before the scene ends")
        if self.counter is not None and self.counter["at"] + self.counter["seconds"] > self.duration:
            ctx.warn("table counter does not finish before the scene ends")
        if self.appear[-1] + 0.4 > self.duration:
            ctx.warn("the last table row is not fully shown before the scene ends")

    # --- geometry and timing (also used by tests) -------------------------------------------------------

    def row_box(self, i: int) -> tuple[int, int, int, int]:
        """The pixel box of body row `i` inside the card: (x0, y0, x1, y1)."""
        if not 0 <= i < len(self.rows):
            raise IndexError(f"row {i} is outside 0..{len(self.rows) - 1}")
        x0, _, x1, _ = self.card
        inset = 8 * self.ctx.u
        y = self.rows_y0 + i * self.row_h
        return int(x0 + inset), int(y), int(x1 - inset), int(y + self.row_h)

    def visible_rows(self, t: float) -> int:
        """How many rows have started to appear at scene time t."""
        return sum(1 for a in self.appear if t >= a)

    def scan_row(self, t: float) -> float | None:
        """Where the scan bar is, in rows from the top of the first row (None before the scan starts). It walks
        down to `stop` and stays there."""
        if self.scan is None or t < self.scan["at"]:
            return None
        return min(float(self.scan["stop"]), (t - self.scan["at"]) / self.scan["per_row"])

    def counter_value(self, t: float) -> float | None:
        """The counter's number at scene time t (None without a counter); exactly `to` once it is done."""
        if self.counter is None:
            return None
        p = ease_out((t - self.counter["at"]) / self.counter["seconds"])
        return self.counter["to"] if p >= 1.0 else self.counter["to"] * p

    # --- drawing ----------------------------------------------------------------------------------------

    def _draw_static(self, img: Image.Image) -> None:
        c, th = self.ctx, self.ctx.theme
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = self.card
        d.rounded_rectangle([x0, y0, x1, y1], int(20 * c.u), fill=th.card, outline=th.line, width=max(1, int(2 * c.u)))
        d.rounded_rectangle([x0 + 8 * c.u, self.header_y, x1 - 8 * c.u, self.rows_y0], int(10 * c.u),
                            fill=lerp_color(th.card, th.accent2, 0.10))

    def frame(self, t: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        img = self.static_layer("table", self._draw_static)
        d = ImageDraw.Draw(img)
        x0, _, x1, _ = self.card
        if self.head:
            blit(img, self.head, self.head_xy, th.accent2, _appear(t, 0.05), "tl")
        ha = _appear(t, 0.1)
        for j, b in enumerate(self.head_cells):
            blit(img, b, (self.col_x[j] + self.pad_x, self.header_y + (self.row_h - b.height) / 2), th.accent2, ha)
        # row highlights, then the scan bar, so the bar sits over a highlighted row
        for h in self.highlights:
            a = _appear(t, h["at"], 0.3)
            if a <= 0:
                continue
            bx0, by0, bx1, by1 = self.row_box(h["row"])
            col = _color(c, h["color"])
            d.rounded_rectangle([bx0, by0, bx1, by1 - 2], int(8 * u), fill=lerp_color(th.card, col, 0.22 * a))
            d.rectangle([bx0, by0 + 6 * u, bx0 + 6 * u, by1 - 8 * u], fill=lerp_color(th.card, col, a))
        pos = self.scan_row(t)
        if pos is not None and self.scan is not None:
            col = _color(c, self.scan["color"])
            arrive = self.scan["at"] + self.scan["stop"] * self.scan["per_row"]
            pulse = max(0.0, 1.0 - (t - arrive) / 0.8) if t >= arrive else 0.0
            by0 = self.rows_y0 + pos * self.row_h
            bx0 = x0 + 8 * u
            bx1 = x1 - 8 * u
            d.rounded_rectangle([bx0, by0, bx1, by0 + self.row_h - 2], int(8 * u),
                                fill=lerp_color(th.card, col, 0.22 + 0.14 * pulse))
            d.rectangle([bx0, by0 + 6 * u, bx0 + 6 * u, by0 + self.row_h - 8 * u], fill=col)
        for i in range(len(self.rows)):
            a = _appear(t, self.appear[i], 0.4)
            if a > 0 and i < len(self.rows) - 1:
                y = int(self.rows_y0 + (i + 1) * self.row_h)
                d.line([(x0 + self.pad_x, y), (x1 - self.pad_x, y)], fill=lerp_color(th.card, th.line, a), width=max(1, int(u)))
        for i, row in enumerate(self.cells):
            a = _appear(t, self.appear[i], 0.4)
            if a <= 0:
                continue
            for j, b in enumerate(row):
                y = self.rows_y0 + i * self.row_h + (self.row_h - b.height) / 2 + (1 - a) * 12 * u
                blit(img, b, (self.col_x[j] + self.pad_x, y), th.fg, a)
        for h in self.highlights:
            tag = self.tags.get(id(h))
            a = _appear(t, h["at"], 0.3)
            if tag is not None and a > 0:
                _, by0, bx1, _ = self.row_box(h["row"])
                blit(img, tag, (bx1 - self.pad_x * 0.5, by0 + (self.row_h - tag.height) / 2), _color(c, h["color"]), a, "tr")
        if self.counter is not None:
            self._draw_counter(img, t)
        return img

    def _draw_counter(self, img: Image.Image, t: float) -> None:
        c, th = self.ctx, self.ctx.theme
        cnt = self.counter
        a = _appear(t, max(0.0, cnt["at"] - 0.1), 0.4)
        value = self.counter_value(t) or 0.0
        text = _number_text(value if cnt["decimals"] else round(value), cnt["decimals"])
        blk = self.cnt_final if text == _number_text(cnt["to"], cnt["decimals"]) else self._cnt_cache.get(
            text, lambda s: make_block(s, c.display, self.cnt_size, self._cnt_w, align="center", min_size=self.cnt_size))
        x, y = self.cnt_xy
        blit(img, blk, (x, y), th.accent, a, "tc")
        if self.cnt_label:
            blit(img, self.cnt_label, (x, y + blk.height + 10 * c.u), th.muted, a, "tc")


# --- array ----------------------------------------------------------------------------------------------

class ArrayScene(Scene):
    """One row of cells that insert, push, pop, get marked and bracketed, following `array_states`."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"title", "items", "steps", "stagger"})
    STEP_FIELDS: ClassVar[frozenset[str]] = frozenset({"at", "text", "color", *ARRAY_ACTIONS})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, th = ctx.u, ctx.theme
        W, H = ctx.W, ctx.H
        margin = fill_margin(ctx)
        self.states = array_states(spec)
        self.times = [s.t for s in self.states]
        self.n_start = len(spec.get("items") or [])
        self.born = {s.added: s.t for s in self.states if s.added is not None}  # when each new cell was added
        self._starts: dict[int, dict[int, tuple[float, float]]] = {}
        self._ghost_memo: dict[int, list[tuple[int, float, float, str]]] = {}
        self.maxn = max(1, max(len(s.cells) for s in self.states))
        pad = 28 * u
        inner_w = W - 2 * margin - 2 * pad
        self.pitch = min(inner_w / self.maxn, (330 if ctx.vertical else 300) * u)
        self.cell_w = self.pitch * 0.84
        self.cell_h = min(self.cell_w * (1.35 if ctx.vertical else 0.8), (420 if ctx.vertical else 200) * u)
        size = min(self.cell_h * (0.3 if ctx.vertical else 0.38), (110 if ctx.vertical else 64) * u * ctx.fs)
        shrunk: list[int] = []
        self.blocks: dict[int, TextBlock] = {}
        for s in self.states:
            for cell in (*s.cells, *([s.removed] if s.removed else [])):
                if cell.id not in self.blocks:
                    b = make_block(cell.text, "sans_bold", size, self.cell_w - 12 * u, max_h=self.cell_h * 0.8,
                                   min_size=10, spacing=1.2, align="center")
                    self.blocks[cell.id] = b
                    if b.shrunk:
                        shrunk.append(b.size)
        if shrunk:
            ctx.warn(f"array text shrunk to fit in {len(shrunk)} cells (down to {min(shrunk)}px)")
        smallest = min([b.size for b in self.blocks.values()], default=int(size))
        if smallest < MIN_READABLE * u:
            ctx.warn(f"array text is small ({smallest}px); use fewer cells or shorter text")
        self.idx_blocks = [make_block(str(i), "mono", 28 * u * ctx.fs, 120 * u, min_size=10) for i in range(self.maxn)]
        self.title = (ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * margin, align="center")
                      if spec.get("title") else None)
        self.tag_blocks: dict[tuple[str, str], TextBlock] = {}
        for s in self.states:
            for kind, text in (("mark", s.mark_text), ("window", s.window_text)):
                if text and (kind, text) not in self.tag_blocks:
                    self.tag_blocks[(kind, text)] = make_block(text, "sans_bold", 34 * u * ctx.fs, W * 0.8, align="center")
        # vertical stack: title, bracket zone, cells in a card, index row, mark label
        title_h = self.title.height + 36 * u if self.title else 0.0
        zone_top, zone_bot = 96 * u * ctx.fs, 110 * u * ctx.fs
        total = title_h + zone_top + self.cell_h + 2 * pad + zone_bot
        top = max(H * 0.05, centre_y(ctx) - total / 2)
        self.title_xy = (W / 2, top)
        self.card = (margin, top + title_h + zone_top, W - margin, top + title_h + zone_top + self.cell_h + 2 * pad)
        self.cell_cy = self.card[1] + pad + self.cell_h / 2
        self.pad = pad
        if self.states[-1].t + MOVE_SECONDS > self.duration:
            ctx.warn("an array step ends after the scene does; lengthen the scene or move the steps earlier")

    def layout_warnings(self, caption: tuple[float, float, float, float] | None) -> list[str]:
        """Step labels (mark and window text) that run past the left or right edge of the frame. Each label is
        centred on its cells, so one near either end of a wide array can be cut off; this only reports it."""
        out: list[str] = []
        for s in self.states:
            n = len(s.cells)
            labels: list[tuple[str, float, float]] = []  # (text, centre x, width)
            ids = [i for i, c in enumerate(s.cells) if c.id in s.marks]
            if ids and s.mark_text:
                cx = sum(self.slot_x(i, n) for i in ids) / len(ids)
                labels.append((s.mark_text, cx, self.tag_blocks[("mark", s.mark_text)].width))
            if s.window is not None and s.window_text:
                ext = self._window_extent(*s.window, n)
                labels.append((s.window_text, (ext[0] + ext[1]) / 2, self.tag_blocks[("window", s.window_text)].width))
            for text, cx, width in labels:
                sides = [name for name, over in (("left", width / 2 - cx), ("right", cx + width / 2 - self.ctx.W))
                         if over > 0.5]
                msg = (f"array label {text!r} is clipped at the {' and '.join(sides)} edge of the frame; "
                       "put it on a cell nearer the middle or shorten it")
                if sides and msg not in out:
                    out.append(msg)
        return out

    # --- geometry (also used by tests) ------------------------------------------------------------------

    def slot_x(self, index: float, n: float) -> float:
        """Centre x of slot `index` when the array holds `n` cells (both may be fractional while cells move)."""
        return self.ctx.W / 2 + (index - (n - 1) / 2) * self.pitch

    def state_at(self, t: float) -> int:
        """Index of the state in force at scene time t."""
        return max(0, bisect.bisect_right(self.times, t) - 1)

    def _start(self, si: int) -> dict[int, tuple[float, float]]:
        """{cell id: (centre x, mark alpha)} where each cell is drawn at the moment state `si` begins. When steps
        come closer together than MOVE_SECONDS the cells are still moving then, and the next move starts from
        there, so nothing jumps. Memoised per state."""
        got = self._starts.get(si)
        if got is not None:
            return got
        out: dict[int, tuple[float, float]] = {}
        if si > 0:
            cur, prev = self.states[si], self.states[si - 1]
            drawn = self._drawn(si - 1, cur.t)
            n_prev = len(prev.cells)
            for i, cell in enumerate(cur.cells):
                known = drawn.get(cell.id)
                out[cell.id] = (known[0], known[3]) if known else (self.slot_x(i, n_prev), 0.0)
            if cur.removed is not None and cur.removed.id in drawn:
                out[cur.removed.id] = (drawn[cur.removed.id][0], 0.0)
        self._starts[si] = out
        return out

    def _ghosts(self, si: int) -> list[tuple[int, float, float, str]]:
        """Cells that left, still fading when state `si` begins: (id, x where it left, time it left, side)."""
        got = self._ghost_memo.get(si)
        if got is not None:
            return got
        out: list[tuple[int, float, float, str]] = []
        if si > 0:
            cur = self.states[si]
            out = [g for g in self._ghosts(si - 1) if cur.t - g[2] < GHOST_SECONDS]
            if cur.removed is not None:
                out.append((cur.removed.id, self._start(si)[cur.removed.id][0], cur.t, str(cur.side)))
        self._ghost_memo[si] = out
        return out

    def _drawn(self, si: int, t: float) -> dict[int, tuple[float, float, float, float]]:
        """{cell id: (centre x, y offset, opacity, mark opacity)} for the cells of state `si` at time t."""
        cur = self.states[si]
        start = self._start(si)
        p = (t - cur.t) / MOVE_SECONDS if si > 0 else 1.0
        e, eo = ease("in_out", p), ease_out(p)
        n = len(cur.cells)
        out: dict[int, tuple[float, float, float, float]] = {}
        for i, cell in enumerate(cur.cells):
            tx = self.slot_x(i, n)
            marked = 1.0 if cell.id in cur.marks else 0.0
            sx, sm = start.get(cell.id, (tx, marked))
            x, ma = sx + (tx - sx) * e, sm + (marked - sm) * eo
            if cell.id < self.n_start:
                out[cell.id] = (x, 0.0, _appear(t, 0.1 + cell.id * 0.06, 0.4), ma)
            else:  # a new cell drops in from above and settles, whichever later steps come meanwhile
                pb = (t - self.born[cell.id]) / MOVE_SECONDS
                out[cell.id] = (x, -(1 - ease("back", pb)) * self.cell_h, min(1.0, max(0.0, pb) * 2.5), ma)
        return out

    def cell_positions(self, t: float) -> dict[int, tuple[float, float, float]]:
        """{cell id: (centre x, y offset, opacity)} for every cell drawn at t, including cells that are leaving."""
        si = self.state_at(t)
        out = {cid: v[:3] for cid, v in self._drawn(si, t).items()}
        for cid, gx, t0, side in self._ghosts(si):
            pp = (t - t0) / MOVE_SECONDS
            alpha = 1.0 - pp * 1.3
            if alpha > 0:
                sign = -1.0 if side == "front" else 1.0
                out[cid] = (gx + sign * self.pitch * 0.9 * ease("out", pp), -self.cell_h * 0.35 * ease("out", pp), alpha)
        return out

    def _draw_static(self, img: Image.Image) -> None:
        c, th = self.ctx, self.ctx.theme
        x0, y0, x1, y1 = self.card
        ImageDraw.Draw(img).rounded_rectangle([x0, y0, x1, y1], int(22 * c.u), fill=th.card, outline=th.line,
                                              width=max(1, int(2 * c.u)))

    def frame(self, t: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        img = self.static_layer("array", self._draw_static)
        d = ImageDraw.Draw(img)
        if self.title:
            blit(img, self.title, self.title_xy, th.fg, _appear(t, 0.05), "tc")
        si = self.state_at(t)
        cur = self.states[si]
        prev = self.states[si - 1] if si > 0 else None
        p = (t - cur.t) / MOVE_SECONDS if si > 0 else 1.0
        e = ease("in_out", p)
        n_prev = len(prev.cells) if prev else len(cur.cells)
        n_eff = n_prev + (len(cur.cells) - n_prev) * e
        pos = self.cell_positions(t)
        mark_a = {cid: v[3] for cid, v in self._drawn(si, t).items()}  # per cell: only cells whose mark changed fade
        card_c = th.card
        base_fill = lerp_color(th.card, th.fg, 0.07)
        cw, ch = self.cell_w, self.cell_h
        for cid, (x, dy, a) in pos.items():
            if a <= 0:
                continue
            y = self.cell_cy + dy
            m = mark_a.get(cid, 0.0)
            mc = _color(c, cur.mark_color if cid in cur.marks or prev is None else prev.mark_color)
            fill = lerp_color(base_fill, mc, 0.25 * m)
            outline = lerp_color(th.line, mc, m)
            d.rounded_rectangle([x - cw / 2, y - ch / 2, x + cw / 2, y + ch / 2], int(14 * u),
                                fill=lerp_color(card_c, fill, a), outline=lerp_color(card_c, outline, a),
                                width=max(2, int((3 + 2 * m) * u)))
            blk = self.blocks[cid]
            blit(img, blk, (x, y), th.fg, a, "mc")
        # index numbers under the slots
        n_show = max(len(cur.cells), n_prev)
        for i in range(n_show):
            a = 1.0 if i < min(len(cur.cells), n_prev) else (e if i < len(cur.cells) else 1.0 - e)
            if a > 0:
                blit(img, self.idx_blocks[i], (self.slot_x(i, n_eff), self.card[3] + 8 * u), th.muted, a, "tc")
        # mark label under the index row; after an unmark it fades out with the marks it belonged to
        label = cur if cur.marks else (prev if prev is not None and prev.marks else None)
        if label is not None and label.mark_text:
            ids = [m for m in label.marks if m in pos]
            if ids:
                alpha = max(mark_a.get(m, 0.0) for m in ids)
                blit(img, self.tag_blocks[("mark", label.mark_text)], (sum(pos[m][0] for m in ids) / len(ids),
                     self.card[3] + 50 * u * c.fs), _color(c, label.mark_color), alpha, "tc")
        # window bracket over the cells
        if cur.window is not None:
            lo, hi = cur.window
            ext = self._window_extent(lo, hi, n_eff)
            a = 1.0
            if prev is not None and prev.window is not None and prev.window != cur.window:
                pl, ph = prev.window
                before = self._window_extent(pl, ph, n_prev)
                ext = tuple(b + (x - b) * e for b, x in zip(before, ext))
            elif prev is not None and prev.window is None:
                a = ease_out(p)
            col = lerp_color(th.card, _color(c, cur.window_color), a)
            yb = self.card[1] - 14 * u
            wdt = max(2, int(5 * u))
            d.line([(ext[0], yb + 12 * u), (ext[0], yb), (ext[1], yb), (ext[1], yb + 12 * u)], fill=col, width=wdt)
            if cur.window_text:
                blit(img, self.tag_blocks[("window", cur.window_text)], ((ext[0] + ext[1]) / 2, yb - 12 * u),
                     _color(c, cur.window_color), a, "bc")
        return img

    def _window_extent(self, lo: int, hi: int, n: float) -> tuple[float, float]:
        return (self.slot_x(lo, n) - self.cell_w / 2 - 6 * self.ctx.u, self.slot_x(hi, n) + self.cell_w / 2 + 6 * self.ctx.u)



# --- meter ----------------------------------------------------------------------------------------------

METER_SHAPES = ("bucket", "bar", "slots")
MAX_SLOTS = 20
MAX_METER_CAPACITY = 1e9
METER_STEP_SECONDS = 0.8   # how long a level change takes when the step has no `seconds`
METER_FLASH_HOLD = 2.0     # how long a step's `text` stays up
METER_DROPS = 4


@dataclass(frozen=True)
class MeterSegment:
    """One level change: the level eases from `start_level` to `level` between t0 and t1."""
    step: int            # index into the spec's `steps`
    t0: float
    t1: float
    start_level: float   # where the level was when this step began (a step may begin mid-move)
    level: float
    text: str = ""
    color: str = "accent"


def meter_segments(spec: dict[str, Any]) -> list[MeterSegment]:
    """The level changes of a meter spec, in time order. Pure: no rendering. A step that begins while the previous
    one is still moving starts from the level reached by then, so the level never jumps."""
    out: list[MeterSegment] = []
    level = float(spec.get("start", 0))
    for n, step in enumerate(spec.get("steps") or []):
        t0 = float(step["at"])
        if out and t0 < out[-1].t0:
            raise ValueError(f"steps[{n}].at: {t0:g}s is earlier than the previous step ({out[-1].t0:g}s)")
        if out:
            level = _eased_level(out[-1], t0)
        seg = MeterSegment(n, t0, t0 + float(step.get("seconds", METER_STEP_SECONDS)), level, float(step["level"]),
                           str(step.get("text", "")), str(step.get("color", "accent")))
        out.append(seg)
    return out


def _eased_level(seg: MeterSegment, t: float) -> float:
    span = seg.t1 - seg.t0
    p = 1.0 if span <= 0 else (t - seg.t0) / span
    return seg.start_level + (seg.level - seg.start_level) * ease("in_out", p)


def meter_level(spec: dict[str, Any], t: float, segments: list[MeterSegment] | None = None) -> float:
    """The level at scene time t: `start` until the first step, then eased (in_out) between steps."""
    segs = meter_segments(spec) if segments is None else segments
    i = bisect.bisect_right([s.t0 for s in segs], t) - 1
    return float(spec.get("start", 0)) if i < 0 else _eased_level(segs[i], t)


class MeterScene(Scene):
    """A level that fills and drains: a bucket with liquid, a horizontal bar or a row of slots, following `steps`."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"title", "shape", "capacity", "start", "unit", "in_label",
                                                  "out_label", "steps"})
    STEP_FIELDS: ClassVar[frozenset[str]] = frozenset({"at", "level", "seconds", "text", "color"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, th = ctx.u, ctx.theme
        W, H = ctx.W, ctx.H
        margin = fill_margin(ctx)
        self.shape = str(spec.get("shape") or "bucket")
        self.capacity = float(spec["capacity"])
        self.start = float(spec.get("start", 0))
        self.unit = str(spec.get("unit") or "")
        steps = spec.get("steps") or []
        self.segments = meter_segments(spec)
        self.t0s = [s.t0 for s in self.segments]
        self.decimals = max([decimals_of(self.start), *(decimals_of(float(st["level"])) for st in steps)])
        self.title = (ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * margin, align="center")
                      if spec.get("title") else None)
        self.readouts = _BlockCache()
        self.in_block = self._label(spec.get("in_label"))
        self.out_block = self._label(spec.get("out_label"))
        self.flashes = {s.step: make_block(s.text, "sans_bold", 40 * u * ctx.fs, W * 0.8, align="center")
                        for s in self.segments if s.text}
        # vertical stack: title, readout, flash text, drop zone in, the meter, drop zone out
        self.readout_h = 100 * u * ctx.fs
        self.zone = 80 * u * ctx.fs
        self.flash_h = 84 * u * ctx.fs
        cx = W / 2
        if self.shape == "bucket":
            self.body_h = (640 if ctx.vertical else 380) * u
            self.top_w = min((600 if ctx.vertical else 400) * u, W - 2 * margin)
            self.bottom_w = self.top_w * 0.72
        elif self.shape == "bar":
            self.body_h = (110 if ctx.vertical else 100) * u
            self.top_w = self.bottom_w = W - 2 * margin
        else:
            n = int(self.capacity)
            self.pitch = (W - 2 * margin) / n
            self.slot_w = min(self.pitch * 0.8, 150 * u)
            self.body_h = min(self.slot_w * 1.7, (300 if ctx.vertical else 200) * u)
            self.top_w = self.bottom_w = self.pitch * n
        title_h = self.title.height + 36 * u if self.title else 0.0
        total = title_h + self.readout_h + 2 * self.zone + self.body_h + self.flash_h
        top = max(H * 0.05, centre_y(ctx) - total / 2)
        self.title_xy = (cx, top)
        self.readout_y = top + title_h
        self.flash_y = self.readout_y + self.readout_h
        self.body_top = self.flash_y + self.flash_h + self.zone
        self.body_bot = self.body_top + self.body_h
        self.cx = cx
        self.readout_size = 90 * u * ctx.fs
        end = max([s.t1 for s in self.segments], default=0.0)
        if end > self.duration:
            ctx.warn("a meter step ends after the scene does; lengthen the scene or move the steps earlier")

    def _label(self, text: Any) -> TextBlock | None:
        c = self.ctx
        return make_block(str(text), "sans_bold", 34 * c.u * c.fs, c.W * 0.8, align="center") if text else None

    # --- geometry (also used by tests) ------------------------------------------------------------------

    def level_at(self, t: float) -> float:
        i = bisect.bisect_right(self.t0s, t) - 1
        return self.start if i < 0 else _eased_level(self.segments[i], t)

    def motion_at(self, t: float) -> tuple[int, float]:
        """(+1 rising, -1 draining or 0, how strongly the drops show 0..1) at scene time t."""
        i = bisect.bisect_right(self.t0s, t) - 1
        if i < 0:
            return 0, 0.0
        seg = self.segments[i]
        if t >= seg.t1 or seg.level == seg.start_level:
            return 0, 0.0
        p = (t - seg.t0) / (seg.t1 - seg.t0)
        return (1 if seg.level > seg.start_level else -1), min(1.0, p * 6, (1 - p) * 6)

    def surface_y(self, level: float) -> float:
        """Top of the liquid (bucket) at this level."""
        return self.body_bot - self.body_h * max(0.0, min(1.0, level / self.capacity))

    def _half_width(self, y: float) -> float:
        """Half the inside width of the bucket at height y."""
        f = (y - self.body_top) / self.body_h
        return (self.bottom_w + (self.top_w - self.bottom_w) * (1 - f)) / 2

    def drop_x(self, level: float) -> float:
        """Where drops fall in or out: the middle of a bucket, the fill front of a bar, the active slot."""
        if self.shape == "bar":
            x0 = self.cx - self.top_w / 2
            return x0 + self.top_w * max(0.0, min(1.0, level / self.capacity))
        if self.shape == "slots":
            n = int(self.capacity)
            idx = min(n - 1, max(0, int(math.ceil(level)) - 1)) if level > 0 else 0
            return self.cx - self.top_w / 2 + (idx + 0.5) * self.pitch
        return self.cx

    # --- drawing ------------------------------------------------------------------------------------------

    def _draw_static(self, img: Image.Image) -> None:
        c, th = self.ctx, self.ctx.theme
        d = ImageDraw.Draw(img)
        w = max(3, int(5 * c.u))
        x0, x1 = self.cx - self.top_w / 2, self.cx + self.top_w / 2
        if self.shape == "bucket":
            b = (self.bottom_w) / 2
            pts = [(x0, self.body_top), (self.cx - b, self.body_bot), (self.cx + b, self.body_bot), (x1, self.body_top)]
            d.polygon(pts, fill=th.card)
            d.line([pts[0], pts[1], pts[2], pts[3]], fill=th.line, width=w, joint="curve")
        elif self.shape == "bar":
            d.rounded_rectangle([x0, self.body_top, x1, self.body_bot], int(self.body_h / 2), fill=th.card,
                                outline=th.line, width=w)
        else:
            for i in range(int(self.capacity)):
                sx = x0 + (i + 0.5) * self.pitch
                d.rounded_rectangle([sx - self.slot_w / 2, self.body_top, sx + self.slot_w / 2, self.body_bot],
                                    int(10 * c.u), fill=th.card, outline=th.line, width=max(2, int(3 * c.u)))

    def _draw_fill(self, img: Image.Image, level: float, a: float) -> None:
        c, th = self.ctx, self.ctx.theme
        d = ImageDraw.Draw(img)
        frac = max(0.0, min(1.0, level / self.capacity))
        liquid = lerp_color(th.card, th.accent, a)
        inset = max(3, int(5 * c.u))
        if self.shape == "bucket":
            if frac <= 0:
                return
            ys = self.surface_y(level)
            yb = self.body_bot - inset
            half_s, half_b = self._half_width(ys) - inset, self._half_width(yb) - inset
            d.polygon([(self.cx - half_s, ys), (self.cx + half_s, ys), (self.cx + half_b, yb), (self.cx - half_b, yb)],
                      fill=liquid)
            d.line([(self.cx - half_s, ys), (self.cx + half_s, ys)], fill=lerp_color(liquid, th.fg, 0.35),
                   width=max(2, int(4 * c.u)))
        elif self.shape == "bar":
            x0 = self.cx - self.top_w / 2 + inset
            x1 = x0 + (self.top_w - 2 * inset) * frac
            if x1 - x0 >= 1:
                r = int((self.body_h - 2 * inset) / 2)
                d.rounded_rectangle([x0, self.body_top + inset, x1, self.body_bot - inset],
                                    min(r, int((x1 - x0) / 2)), fill=liquid)
        else:
            x0 = self.cx - self.top_w / 2
            for i in range(int(self.capacity)):
                f = max(0.0, min(1.0, level - i))
                if f <= 0:
                    continue
                sx = x0 + (i + 0.5) * self.pitch
                top = self.body_bot - inset - (self.body_h - 2 * inset) * f
                d.rounded_rectangle([sx - self.slot_w / 2 + inset, top, sx + self.slot_w / 2 - inset, self.body_bot - inset],
                                    int(6 * c.u), fill=liquid)

    def _draw_drops(self, img: Image.Image, t: float, level: float, direction: int, strength: float) -> None:
        if direction == 0 or strength <= 0:
            return
        c, th = self.ctx, self.ctx.theme
        d = ImageDraw.Draw(img)
        r = max(4.0, 9 * c.u)
        base = th.accent if direction > 0 else th.accent2
        x_mid = self.drop_x(level)
        spread = (self.top_w * 0.1) if self.shape == "bucket" else (self.pitch * 0.12 if self.shape == "slots" else 14 * c.u)
        for k in range(METER_DROPS):
            ph = (t * 1.5 + k / METER_DROPS) % 1.0
            x = x_mid + (k - (METER_DROPS - 1) / 2) * spread
            if direction > 0:   # fall in from above into the liquid
                y0 = self.body_top - self.zone * 0.7
                y1 = self.surface_y(level) if self.shape == "bucket" else self.body_top + self.body_h * 0.4
                y, a = y0 + (y1 - y0) * ph * ph, min(1.0, ph * 5)
            else:               # leave through the bottom
                y0 = self.body_bot - r * 2
                y, a = y0 + (self.zone * 0.6) * ph * ph, min(1.0, (1 - ph) * 3)
            a *= strength
            if a > 0.02:
                d.ellipse([x - r, y - r * 1.3, x + r, y + r * 1.3], fill=lerp_color(th.bg, base, a))

    def frame(self, t: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        img = self.static_layer("meter", self._draw_static)
        if self.title:
            blit(img, self.title, self.title_xy, th.fg, _appear(t, 0.05), "tc")
        level = self.level_at(t)
        appear = _appear(t, 0.1, 0.5)
        self._draw_fill(img, level, appear)
        direction, strength = self.motion_at(t)
        self._draw_drops(img, t, level, direction, strength * appear)
        value = round(level, self.decimals)
        unit = self.unit[:-1] if value == 1 and len(self.unit) > 1 and self.unit.endswith("s") else self.unit
        text = _number_text(value, self.decimals) + (f" {unit}" if unit else "")
        size = self.readout_size
        blk = self.readouts.get(text, lambda s: make_block(s, "sans_bold", size, c.W * 0.9, min_size=10, align="center"))
        blit(img, blk, (self.cx, self.readout_y + self.readout_h / 2), th.fg, appear, "mc")
        if direction > 0 and self.in_block:
            blit(img, self.in_block, (self.cx, self.body_top - self.zone * 0.88), th.accent, strength, "mc")
        elif direction < 0 and self.out_block:
            blit(img, self.out_block, (self.cx, self.body_bot + self.zone * 0.88), th.accent2, strength, "mc")
        i = bisect.bisect_right(self.t0s, t) - 1
        while i >= 0 and i not in self.flashes:   # the latest step that has text
            i -= 1
        if i >= 0:
            seg = self.segments[i]
            a = _appear(t, seg.t0, 0.25) * (1.0 - ease("in_out", (t - seg.t0 - METER_FLASH_HOLD) / 0.4))
            if a > 0:
                blit(img, self.flashes[i], (self.cx, self.flash_y), _color(c, seg.color), a, "tc")
        return img


# --- tree ------------------------------------------------------------------------------------------------

MAX_TREE_NODES = 31
MAX_TREE_LEVELS = 5       # the root is level 1
MAX_TREE_CHILDREN = 6
MAX_TREE_PATHS = 12
TREE_FIRST_AT = 0.3       # the first node without `at` appears here
TREE_STAGGER = 0.12       # seconds between nodes that have no `at`
HOP_SECONDS = 0.35        # a path lights one edge and the node it leads to per hop
NODE_FADE = 0.3
PATH_FADE_OUT = 0.3       # a path dims this fast when the next one starts


def tree_layout(nodes: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    """Top-down tidy layout, in fractions (0..1) of the box the tree is drawn in. The first node is the root and
    every other node names an earlier `parent`. Leaves take consecutive slots left to right and each parent is
    centred over its first and last child, so a node is never closer than one slot to its neighbour on the same
    level. y is depth over the deepest level (0 for the root). Pure: validation guarantees at most
    MAX_TREE_LEVELS levels, so the recursion is shallow."""
    if not nodes:
        return {}
    kids: dict[str, list[str]] = {str(n["id"]): [] for n in nodes}
    for n in nodes[1:]:
        kids[str(n["parent"])].append(str(n["id"]))
    xs: dict[str, float] = {}
    depth: dict[str, int] = {}
    slot = 0

    def walk(i: str, d: int) -> None:
        nonlocal slot
        depth[i] = d
        if not kids[i]:
            xs[i] = float(slot)
            slot += 1
            return
        for k in kids[i]:
            walk(k, d + 1)
        xs[i] = (xs[kids[i][0]] + xs[kids[i][-1]]) / 2

    walk(str(nodes[0]["id"]), 0)
    deepest = max(depth.values())
    return {i: ((xs[i] + 0.5) / slot, depth[i] / deepest if deepest else 0.0) for i in xs}


def tree_levels(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Level of each node, the root being 1. Parents come first in the list, so one pass is enough."""
    level: dict[str, int] = {}
    for n in nodes:
        level[str(n["id"])] = 1 if n.get("parent") is None else level[str(n["parent"])] + 1
    return level


class TreeScene(Scene):
    """A top-down tree with an auto-layout. Nodes appear one by one; a `path` lights edges and nodes in sequence."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"title", "nodes", "path"})
    NODE_FIELDS: ClassVar[frozenset[str]] = frozenset({"id", "label", "parent", "at"})
    PATH_FIELDS: ClassVar[frozenset[str]] = frozenset({"at", "nodes", "text"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, W, H = ctx.u, ctx.W, ctx.H
        margin = fill_margin(ctx)
        nodes = spec["nodes"]
        self.ids = [str(n["id"]) for n in nodes]
        index = {i: k for k, i in enumerate(self.ids)}
        self.parent = [-1] + [index[str(n["parent"])] for n in nodes[1:]]
        self.appear_at: list[float] = []
        auto = 0
        for n in nodes:
            if n.get("at") is not None:
                self.appear_at.append(float(n["at"]))
            else:
                self.appear_at.append(TREE_FIRST_AT + auto * TREE_STAGGER)
                auto += 1
        self.title = (ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * margin, align="center")
                      if spec.get("title") else None)
        top = H * 0.06
        title_h = self.title.height + 30 * u if self.title else 0.0
        self.title_xy = (W / 2, top)
        self.text_y = top + title_h + 36 * u * ctx.fs       # the path text sits between the title and the tree
        tree_top = self.text_y + 56 * u * ctx.fs
        tree_bottom = H * (0.70 if ctx.vertical else 0.84)
        self.box_h = (128 if ctx.vertical else 92) * u
        levels = tree_levels(nodes)
        n_levels = max(levels.values())
        span = max(1.0, tree_bottom - tree_top - self.box_h)
        step_y = min(span / max(1, n_levels - 1), 360 * u)
        tree_top += (span - step_y * (n_levels - 1)) / 2   # centred in the space it has
        self.layout = tree_layout(nodes)
        x0, x1 = margin, W - margin
        self.pos = [(x0 + (x1 - x0) * self.layout[i][0], tree_top + self.box_h / 2 + (levels[i] - 1) * step_y)
                    for i in self.ids]
        # box width per level: as wide as the longest label wants, but never wider than the tightest gap there
        base = (46 if ctx.vertical else 40) * u
        pad = 14 * u
        self.box_w: list[float] = [0.0] * len(self.ids)
        self.labels: list[TextBlock] = []
        for lv in range(1, n_levels + 1):
            members = sorted((k for k in range(len(self.ids)) if levels[self.ids[k]] == lv), key=lambda k: self.pos[k][0])
            gap = min((self.pos[b][0] - self.pos[a][0] for a, b in zip(members, members[1:])), default=x1 - x0)
            want = max(make_block(str(nodes[k]["label"]), "sans_bold", base, 10 * W).width for k in members) + 2 * pad
            w = min(gap * 0.9, want, 460 * u)
            for k in members:
                self.box_w[k] = w
        sizes: dict[int, float] = {}
        for k, n in enumerate(nodes):
            b = make_block(str(n["label"]), "sans_bold", base, self.box_w[k] - 2 * self._pad(k), max_h=self.box_h - pad,
                           min_size=10, align="center")
            lv = levels[self.ids[k]]
            sizes[lv] = min(sizes.get(lv, base), b.size)
        for k, n in enumerate(nodes):   # one type size per level, so siblings look alike
            self.labels.append(make_block(str(n["label"]), "sans_bold", sizes[levels[self.ids[k]]],
                                          self.box_w[k] - 2 * self._pad(k), max_h=self.box_h - pad, min_size=10,
                                          align="center"))
        smallest = min(sizes.values())
        if smallest < 22 * u:
            ctx.warn(f"tree labels shrunk to {smallest:.0f}px to fit; use fewer nodes or shorter labels")
        # paths: node light times and the edge each hop draws
        self.paths: list[dict[str, Any]] = []
        for p in spec.get("path") or []:
            seq = [index[str(i)] for i in p["nodes"]]
            at = float(p["at"])
            self.paths.append({
                "at": at, "text": str(p.get("text") or ""), "seq": seq,
                "hops": [(seq[j - 1], seq[j], at + (j - 1) * HOP_SECONDS) for j in range(1, len(seq))],
                "lit": [at + j * HOP_SECONDS for j in range(len(seq))],
                "end": at + max(0, len(seq) - 1) * HOP_SECONDS + 0.2,
            })
        self.path_at = [p["at"] for p in self.paths]
        self.texts = {k: make_block(p["text"], "sans_bold", 40 * u * ctx.fs, W * 0.86, align="center")
                      for k, p in enumerate(self.paths) if p["text"]}
        end = max([q["end"] for q in self.paths] + self.appear_at, default=0.0)
        if end > self.duration:
            ctx.warn("a tree node or path step ends after the scene does; lengthen the scene or move them earlier")

    # --- geometry (also used by tests) ------------------------------------------------------------------

    def _pad(self, k: int) -> float:
        """Space between a label and the side of its box: 14 units, less in a box squeezed by a crowded level."""
        return min(14 * self.ctx.u, self.box_w[k] * 0.1)

    def box(self, k: int) -> tuple[float, float, float, float]:
        x, y = self.pos[k]
        w = self.box_w[k]
        return x - w / 2, y - self.box_h / 2, x + w / 2, y + self.box_h / 2

    def _edge_ends(self, child: int) -> tuple[tuple[float, float], tuple[float, float]]:
        p = self.parent[child]
        return (self.pos[p][0], self.pos[p][1] + self.box_h / 2), (self.pos[child][0], self.pos[child][1] - self.box_h / 2)

    def lights(self, t: float) -> tuple[list[float], dict[int, tuple[float, int, float]]]:
        """(how lit each node is 0..1, {child index: (hop progress 0..1, node the hop starts from, strength)} for
        the edges being lit). A path dims when the next one starts."""
        lit_n = [0.0] * len(self.ids)
        lit_e: dict[int, tuple[float, int, float]] = {}
        for s, p in enumerate(self.paths):
            if t < p["at"]:
                break
            fade = 1.0
            if s + 1 < len(self.paths):
                fade = 1.0 - ease("in_out", (t - self.paths[s + 1]["at"]) / PATH_FADE_OUT)
            if fade <= 0:
                continue
            for k, tl in zip(p["seq"], p["lit"]):
                lit_n[k] = max(lit_n[k], _appear(t, tl, 0.2) * fade)
            for a, b, t0 in p["hops"]:
                prog = max(0.0, min(1.0, (t - t0) / HOP_SECONDS))
                if prog > 0:
                    child = b if self.parent[b] == a else a
                    lit_e[child] = (prog, a, fade)
        return lit_n, lit_e

    # --- drawing ------------------------------------------------------------------------------------------

    def frame(self, t: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        img = self.static_layer("tree", lambda im: None)
        d = ImageDraw.Draw(img)
        if self.title:
            blit(img, self.title, self.title_xy, th.fg, _appear(t, 0.05), "tc")
        lit_n, lit_e = self.lights(t)
        alpha = [max(_appear(t, at, NODE_FADE), lit_n[k]) for k, at in enumerate(self.appear_at)]
        lw = max(2, int(4 * u))
        for k in range(1, len(self.ids)):
            if alpha[k] <= 0.01:
                continue
            a, b = self._edge_ends(k)
            d.line([a, b], fill=lerp_color(th.bg, th.line, alpha[k]), width=lw)
            if k in lit_e:
                prog, frm, strength = lit_e[k]
                s, e = (a, b) if frm == self.parent[k] else (b, a)
                end = (s[0] + (e[0] - s[0]) * prog, s[1] + (e[1] - s[1]) * prog)
                d.line([s, end], fill=lerp_color(th.line, th.accent, strength), width=lw + max(2, int(3 * u)))
        radius = int(16 * u)
        for k in range(len(self.ids)):
            a = alpha[k]
            if a <= 0.01:
                continue
            lit = lit_n[k]
            d.rounded_rectangle(self.box(k), radius,
                                fill=lerp_color(th.bg, lerp_color(th.card, th.accent, 0.35 * lit), a),
                                outline=lerp_color(th.bg, lerp_color(th.line, th.accent, lit), a),
                                width=max(2, int((3 + 2 * lit) * u)))
            blit(img, self.labels[k], self.pos[k], th.fg, a, "mc")
        i = bisect.bisect_right(self.path_at, t) - 1
        if i >= 0 and i in self.texts:
            blit(img, self.texts[i], (c.W / 2, self.text_y), th.accent, _appear(t, self.paths[i]["at"], 0.25), "mc")
        return img


# --- timeline ---------------------------------------------------------------------------------------------

MAX_TIMELINE_EVENTS = 60
MAX_TIMELINE_WINDOWS = MAX_STEPS
MAX_AXIS_TICKS = 24
TIMELINE_FIRST_AT = 0.3
TIMELINE_STAGGER = 0.15   # seconds between events that have no `at`
WINDOW_MOVE = 0.8         # how long the window takes to slide to its next range
WINDOW_TEXT_FADE = 0.25


def event_times(events: list[dict[str, Any]]) -> list[float]:
    """When each event appears: its `at`, or in declaration order TIMELINE_STAGGER apart."""
    out: list[float] = []
    auto = 0
    for ev in events:
        if ev.get("at") is not None:
            out.append(float(ev["at"]))
        else:
            out.append(TIMELINE_FIRST_AT + auto * TIMELINE_STAGGER)
            auto += 1
    return out


@dataclass(frozen=True)
class WindowSegment:
    """One window step: the range eases from (start_lo, start_hi) to (lo, hi) between at and at + WINDOW_MOVE."""
    step: int
    at: float
    start_lo: float
    start_hi: float
    lo: float
    hi: float
    text: str = ""


def _window_at(seg: WindowSegment, t: float) -> tuple[float, float]:
    p = ease("in_out", (t - seg.at) / WINDOW_MOVE)
    return seg.start_lo + (seg.lo - seg.start_lo) * p, seg.start_hi + (seg.hi - seg.start_hi) * p


def window_segments(window: list[dict[str, Any]]) -> list[WindowSegment]:
    """The window steps in time order. The first step shows the window where it is; each later one slides it from
    wherever the previous move had got to, so the window never jumps. Pure: no rendering."""
    out: list[WindowSegment] = []
    for n, w in enumerate(window):
        at, (lo, hi) = float(w["at"]), (float(w["range"][0]), float(w["range"][1]))
        if out and at < out[-1].at:
            raise ValueError(f"window[{n}].at: {at:g}s is earlier than the previous step ({out[-1].at:g}s)")
        s_lo, s_hi = _window_at(out[-1], at) if out else (lo, hi)
        out.append(WindowSegment(n, at, s_lo, s_hi, lo, hi, str(w.get("text") or "")))
    return out


def window_range(window: list[dict[str, Any]], t: float,
                 segments: list[WindowSegment] | None = None) -> tuple[float, float] | None:
    """The window's (lo, hi) at scene time t, or None before the first window step."""
    segs = window_segments(window) if segments is None else segments
    i = bisect.bisect_right([s.at for s in segs], t) - 1
    return None if i < 0 else _window_at(segs[i], t)


def count_in_window(events: list[dict[str, Any]], t: float, rng: tuple[float, float] | None) -> int:
    """How many events have appeared by t (at <= t) and sit inside the range (lo <= x <= hi). 0 without a window."""
    if rng is None:
        return 0
    lo, hi = rng
    return sum(1 for ev, at in zip(events, event_times(events)) if at <= t and lo <= float(ev["x"]) <= hi)


def timeline_count(spec: dict[str, Any], t: float) -> int:
    """The "N in window" number at scene time t for a validated timeline spec."""
    return count_in_window(spec["events"], t, window_range(spec.get("window") or [], t))


class TimelineScene(Scene):
    """Events on an axis and a window that slides over them, with a live count of the events inside it."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"title", "axis", "events", "window", "count"})
    AXIS_FIELDS: ClassVar[frozenset[str]] = frozenset({"from", "to", "unit", "ticks"})
    EVENT_FIELDS: ClassVar[frozenset[str]] = frozenset({"x", "at", "label", "color"})
    WINDOW_FIELDS: ClassVar[frozenset[str]] = frozenset({"at", "range", "text"})

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, W, H = ctx.u, ctx.W, ctx.H
        margin = fill_margin(ctx)
        axis = spec["axis"]
        self.lo_x, self.hi_x = float(axis["from"]), float(axis["to"])
        self.unit = str(axis.get("unit") or "")
        self.ticks = float(axis.get("ticks") or (self.hi_x - self.lo_x) / 4)
        self.events = spec["events"]
        self.times = event_times(self.events)
        self.window = spec.get("window") or []
        self.segments = window_segments(self.window)
        self.seg_at = [s.at for s in self.segments]
        self.show_count = bool(spec.get("count"))
        self.title = (ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * margin, align="center")
                      if spec.get("title") else None)
        # vertical stack: title, count, window text, then the event lanes above the axis
        top = H * 0.06
        title_h = self.title.height + 30 * u if self.title else 0.0
        self.title_xy = (W / 2, top)
        self.count_y = top + title_h + 60 * u * ctx.fs
        self.count_size = 96 * u * ctx.fs
        self.text_y = self.count_y + 80 * u * ctx.fs
        self.chart_top = self.text_y + 50 * u * ctx.fs
        chart_bottom = H * (0.70 if ctx.vertical else 0.84)
        self.band_h = (400 if ctx.vertical else 300) * u   # the window band and the lanes under it
        self.axis_y = (self.chart_top + chart_bottom) / 2 + self.band_h / 2 - 20 * u
        self.band_top = self.axis_y - self.band_h
        self.x0, self.x1 = margin + 20 * u, W - margin - 20 * u
        self.dot_r = max(5.0, (17 if ctx.vertical else 12) * u)
        self.label_size = 26 * u * ctx.fs
        self.labels = [make_block(str(ev["label"]), "sans_bold", self.label_size, 260 * u, align="center")
                       if ev.get("label") else None for ev in self.events]
        self.dots = self._lanes()
        end = max([s.at + WINDOW_MOVE for s in self.segments] + self.times, default=0.0)
        if end > self.duration:
            ctx.warn("a timeline event or window step ends after the scene does; lengthen the scene or move them earlier")
        self.texts = {s.step: make_block(s.text, "sans_bold", 40 * u * ctx.fs, W * 0.86, align="center")
                      for s in self.segments if s.text}
        self.counts = _BlockCache()

    def px(self, value: float) -> float:
        return self.x0 + (value - self.lo_x) / (self.hi_x - self.lo_x) * (self.x1 - self.x0)

    def _lanes(self) -> list[tuple[float, float]]:
        """(x, y) of every event dot. Events that would overlap (dot or label) stack into lanes above the axis."""
        r, u = self.dot_r, self.ctx.u
        has_label = any(self.labels)
        pitch = 2 * r + 10 * u + (self.label_size * 1.3 if has_label else 0)
        room = self.axis_y - 22 * u - self.band_top
        max_lanes = max(1, int(room // pitch))
        lane_end: list[float] = []   # right edge of the last item in each lane
        out: list[tuple[float, float]] = [(0.0, 0.0)] * len(self.events)
        for k in sorted(range(len(self.events)), key=lambda k: float(self.events[k]["x"])):
            x = self.px(float(self.events[k]["x"]))
            half = max(r, (self.labels[k].width / 2) if self.labels[k] else 0.0) + 4 * u
            lane = next((i for i, e in enumerate(lane_end) if x - half >= e), len(lane_end))
            if lane >= max_lanes:
                lane = max_lanes - 1
                self.ctx.warn("timeline events overlap; use fewer events or spread them along the axis")
            if lane == len(lane_end):
                lane_end.append(x + half)
            else:
                lane_end[lane] = max(lane_end[lane], x + half)
            out[k] = (x, self.axis_y - 22 * u - r - lane * pitch)
        return out

    def window_px(self, t: float) -> tuple[float, float] | None:
        rng = window_range(self.window, t, self.segments)
        return None if rng is None else (self.px(rng[0]), self.px(rng[1]))

    # --- drawing ------------------------------------------------------------------------------------------

    def _draw_static(self, img: Image.Image) -> None:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        d = ImageDraw.Draw(img)
        d.line([(self.x0, self.axis_y), (self.x1, self.axis_y)], fill=th.line, width=max(3, int(5 * u)))
        n = int(round((self.hi_x - self.lo_x) / self.ticks))
        decimals = max(decimals_of(self.lo_x), decimals_of(self.ticks))
        for i in range(n + 1):
            v = min(self.hi_x, self.lo_x + i * self.ticks)
            x = self.px(v)
            d.line([(x, self.axis_y), (x, self.axis_y + 14 * u)], fill=th.line, width=max(2, int(3 * u)))
            text = _number_text(v, decimals) + (f" {self.unit}" if self.unit and i == n else "")
            blit(img, make_block(text, "sans_bold", 28 * u * c.fs, 220 * u, align="center"),
                 (x, self.axis_y + 24 * u), th.muted, 1.0, "tc")

    def frame(self, t: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        img = self.static_layer("timeline", self._draw_static)
        if self.title:
            blit(img, self.title, self.title_xy, th.fg, _appear(t, 0.05), "tc")
        wpx = self.window_px(t)
        w_alpha = _appear(t, self.seg_at[0], 0.4) if self.segments else 0.0
        if wpx and w_alpha > 0.01:
            box = (int(wpx[0]), int(self.band_top), int(math.ceil(wpx[1])), int(self.axis_y + 12 * u))
            if box[2] > box[0]:
                region = img.crop(box)
                img.paste(Image.blend(region, Image.new("RGB", region.size, th.accent), 0.16 * w_alpha), box[:2])
                d = ImageDraw.Draw(img)
                edge = lerp_color(th.bg, th.accent, w_alpha)
                for x in (wpx[0], wpx[1]):
                    d.line([(x, self.band_top), (x, self.axis_y + 12 * u)], fill=edge, width=max(2, int(4 * u)))
                d.line([(wpx[0], self.axis_y), (wpx[1], self.axis_y)], fill=edge,
                       width=max(5, int(10 * u)))
        d = ImageDraw.Draw(img)
        rng = window_range(self.window, t, self.segments)
        r = self.dot_r
        for k, ev in enumerate(self.events):
            a = _appear(t, self.times[k], 0.3)
            if a <= 0.01:
                continue
            x, y = self.dots[k]
            base = getattr(th, str(ev.get("color") or "accent2"))
            inside = rng is not None and rng[0] <= float(ev["x"]) <= rng[1]
            if rng is not None and not inside:
                base = lerp_color(base, th.bg, 0.6)
            rr = r * (0.6 + 0.4 * a)
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=lerp_color(th.bg, base, a),
                      outline=lerp_color(th.bg, th.fg, a) if inside else None, width=max(2, int(3 * u)))
            if self.labels[k]:
                blit(img, self.labels[k], (x, y - r - 6 * u), base if not inside else th.fg, a, "bc")
        if self.show_count and self.segments:
            n = count_in_window(self.events, t, rng)
            blk = self.counts.get(f"{n} in window", lambda s: make_block(s, "sans_bold", self.count_size, c.W * 0.9,
                                                                         min_size=10, align="center"))
            blit(img, blk, (c.W / 2, self.count_y), th.accent, w_alpha, "mc")
        i = bisect.bisect_right(self.seg_at, t) - 1
        if i >= 0 and i in self.texts:
            blit(img, self.texts[i], (c.W / 2, self.text_y), th.accent2, _appear(t, self.segments[i].at, WINDOW_TEXT_FADE),
                 "mc")
        return img


# --- cta end card ---------------------------------------------------------------------------------------

class CtaScene(Scene):
    """An end card that asks a question: a speech bubble that pops in, lettered answer chips that follow one
    by one, and a prompt that pulses once a second."""

    FIELDS: ClassVar[frozenset[str]] = frozenset({"question", "answers", "prompt"})
    BUBBLE_AT = 0.1          # the bubble starts to pop here
    POP_SECONDS = 0.5
    CHIP_AT = 0.9            # the first answer comes in here, the others CHIP_STAGGER apart
    CHIP_STAGGER = 0.35
    CHIP_SECONDS = 0.4
    PULSE_SECONDS = 1.0

    def __init__(self, spec: dict[str, Any], ctx: Ctx):
        super().__init__(spec, ctx)
        u, th, W, H = ctx.u, ctx.theme, ctx.W, ctx.H
        margin = fill_margin(ctx)
        avail = W - 2 * margin
        self.answers: list[str] = list(spec.get("answers") or [])
        n = len(self.answers)
        # the bubble: a card with the question and a small tail pointing down at the answers
        pad = int(44 * u)
        qblk = ctx.block(str(spec["question"]), ctx.display, 64 * u * ctx.fs, avail - 2 * pad,
                         max_h=H * (0.2 if ctx.vertical else 0.24), align="center", spacing=1.2)
        tail = int(26 * u)
        bw, bh = int(avail), qblk.height + 2 * pad
        self.bubble = Image.new("RGBA", (bw, bh + tail), (0, 0, 0, 0))
        bd = ImageDraw.Draw(self.bubble)
        line_w = max(2, int(4 * u))
        bd.rounded_rectangle([0, 0, bw - 1, bh - 1], int(30 * u), fill=th.card, outline=th.accent, width=line_w)
        tx = int(bw * 0.12)
        bd.polygon([(tx, bh - 2), (tx + int(46 * u), bh - 2), (tx + int(6 * u), bh + tail - 1)], fill=th.card,
                   outline=th.accent)
        bd.line([(tx + line_w, bh - 2), (tx + int(46 * u) - line_w, bh - 2)], fill=th.card, width=line_w + 2)
        self.bubble.paste(th.fg, ((bw - qblk.width) // 2, pad), qblk.mask)
        # answer chips: one column in vertical, two columns in landscape (side by side for two answers)
        cols = 1 if (ctx.vertical or n <= 1) else 2
        gap = int(18 * u)
        chip_w = int((avail - (cols - 1) * gap) / cols)
        text_size = (44 if ctx.vertical else 40) * u * ctx.fs
        chip_h = int(text_size * 2.1)
        self.chip_size = (chip_w, chip_h)
        self.chips: list[Image.Image] = []
        for i, answer in enumerate(self.answers):
            self.chips.append(self._make_chip(CTA_LETTERS[i], answer, chip_w, chip_h, text_size))
        rows = (n + cols - 1) // cols
        grid_h = rows * chip_h + max(0, rows - 1) * gap
        # the prompt
        pblk = ctx.block(str(spec.get("prompt") or DEFAULT_CTA_PROMPT), "sans_bold", 40 * u * ctx.fs, avail - 90 * u,
                         max_h=120 * u, align="center")
        self.prompt_blk = pblk
        ppx, ppy = int(40 * u), int(18 * u)
        self.prompt_size = (pblk.width + 2 * ppx, pblk.height + 2 * ppy)
        # vertical stack, centred in the area above the caption zone
        gap1, gap2 = 34 * u, 44 * u
        total = self.bubble.height + (gap1 + grid_h if n else 0.0) + gap2 + self.prompt_size[1]
        top = max(H * 0.07, centre_y(ctx) - total / 2)
        if top + total > H * (0.74 if ctx.vertical else 0.86):
            ctx.warn("cta card is tall; use fewer or shorter answers")
        self.bubble_xy = (margin, top)
        y = top + self.bubble.height + gap1
        self.chip_xy: list[tuple[float, float]] = []
        for i in range(n):
            r, c = divmod(i, cols)
            self.chip_xy.append((margin + c * (chip_w + gap), y + r * (chip_h + gap)))
        y += grid_h + (gap2 if n else gap2 - gap1)
        self.prompt_xy = ((W - self.prompt_size[0]) / 2, y)
        self.prompt_at = self.CHIP_AT + n * self.CHIP_STAGGER + 0.1
        self.prompt_pad = (ppx, ppy)
        if self.prompt_at + 0.5 > self.duration:
            ctx.warn(f"the cta card needs {self.prompt_at + 0.5:.1f}s to finish; the scene is {self.duration:.1f}s")

    def _make_chip(self, letter: str, answer: str, w: int, h: int, size: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        chip = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(chip)
        d.rounded_rectangle([0, 0, w - 1, h - 1], int(h * 0.3), fill=th.card, outline=th.line, width=max(2, int(3 * u)))
        diam = int(h * 0.62)
        cx0, cy0 = int(h * 0.19), (h - diam) // 2
        d.ellipse([cx0, cy0, cx0 + diam, cy0 + diam], fill=th.accent)
        lb = make_block(letter, "sans_bold", diam * 0.58, diam, min_size=10, align="center")
        chip.paste(th.bg, (cx0 + (diam - lb.width) // 2, cy0 + (diam - lb.mask.height) // 2 + int(diam * 0.04)), lb.mask)
        tx = cx0 + diam + int(h * 0.26)
        blk = c.block(answer, "sans_bold", size, w - tx - h * 0.3, max_h=h * 0.84, align="left", spacing=1.15)
        chip.paste(th.fg, (tx, (h - blk.height) // 2), blk.mask)
        return chip

    # --- timing and geometry (also used by tests) -------------------------------------------------------

    def pop_scale(self, t: float) -> float:
        """Size of the bubble at scene time t: it grows from 0.7 with an overshoot and settles at 1.0."""
        return 0.7 + 0.3 * ease("back", (t - self.BUBBLE_AT) / self.POP_SECONDS)

    def chip_alpha(self, i: int, t: float) -> float:
        return _appear(t, self.CHIP_AT + i * self.CHIP_STAGGER, self.CHIP_SECONDS)

    def pulse(self, t: float) -> float:
        """0..1 once a second, starting when the prompt appears (0 at the start of each beat, 1 half a beat later)."""
        if t < self.prompt_at:
            return 0.0
        return 0.5 - 0.5 * math.cos(2 * math.pi * (t - self.prompt_at) / self.PULSE_SECONDS)

    def bubble_box(self) -> tuple[float, float, float, float]:
        x, y = self.bubble_xy
        return x, y, x + self.bubble.width, y + self.bubble.height

    def chip_box(self, i: int) -> tuple[float, float, float, float]:
        x, y = self.chip_xy[i]
        return x, y, x + self.chip_size[0], y + self.chip_size[1]

    def prompt_box(self) -> tuple[float, float, float, float]:
        x, y = self.prompt_xy
        return x, y, x + self.prompt_size[0], y + self.prompt_size[1]

    @staticmethod
    def _put(img: Image.Image, layer: Image.Image, xy: tuple[float, float], alpha: float) -> None:
        if alpha <= 0:
            return
        mask = layer.getchannel("A")
        if alpha < 1:
            mask = mask.point(lambda v: int(v * alpha))
        img.paste(layer.convert("RGB"), (int(round(xy[0])), int(round(xy[1]))), mask)

    def frame(self, t: float) -> Image.Image:
        c, th, u = self.ctx, self.ctx.theme, self.ctx.u
        img = c.background()
        a = ease_out((t - self.BUBBLE_AT) / 0.25)
        k = self.pop_scale(t)
        if (t - self.BUBBLE_AT) / self.POP_SECONDS >= 1.0:
            self._put(img, self.bubble, self.bubble_xy, a)
        elif a > 0:
            bw, bh = self.bubble.size
            w, h = max(1, int(bw * k)), max(1, int(bh * k))
            pop = self.bubble.resize((w, h), Image.BILINEAR)
            self._put(img, pop, (self.bubble_xy[0] + (bw - w) / 2, self.bubble_xy[1] + (bh - h) / 2), a)
        for i, chip in enumerate(self.chips):
            ca = self.chip_alpha(i, t)
            x, y = self.chip_xy[i]
            self._put(img, chip, (x, y + (1 - ca) * 24 * u), ca)
        pa = _appear(t, self.prompt_at, 0.4)
        if pa > 0:
            p = self.pulse(t)
            x0, y0, x1, y1 = self.prompt_box()
            d = ImageDraw.Draw(img)
            outline = lerp_color(th.card, lerp_color(th.line, th.accent, p), pa)
            d.rounded_rectangle([x0, y0, x1, y1], int((y1 - y0) / 2), fill=lerp_color(th.bg, th.card, pa),
                                outline=outline, width=max(2, int((3 + 3 * p) * u)))
            blit(img, self.prompt_blk, ((x0 + x1) / 2, y0 + self.prompt_pad[1]), lerp_color(th.accent, th.fg, 0.4 * (1 - p)),
                 pa, "tc")
        return img
