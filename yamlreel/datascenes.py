"""Data scenes: a table whose rows appear one by one with a scan bar, and an array of cells that moves.

Both animate the data itself instead of boxes around text. The step logic of the array is a pure function
(`array_states`), so a spec is replayed when it is loaded and a bad step is reported with its path and time.
All text is rendered to blocks once in `__init__`; `frame(t)` only composites.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from PIL import Image, ImageDraw

from .scenebase import Ctx, Scene, _appear
from .textkit import TextBlock, blit, ease, ease_out, lerp_color, make_block
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
ARRAY_ACTIONS = ("insert", "push", "pop", "mark", "unmark", "window")
POP_SIDES = ("front", "back")


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

def _color(ctx: Ctx, name: str) -> tuple[int, int, int]:
    return getattr(ctx.theme, name)


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
            self.counter = {"to": to, "decimals": 0 if to == int(to) else 2, "label": str(counter.get("label", "")),
                            "at": float(counter.get("at", self.scan["at"] if self.scan else FIRST_ROW_AT)),
                            "seconds": float(counter.get("seconds", COUNTER_SECONDS))}

        # --- layout: pick the largest text size at which the table fits its area
        avail_w = W - 2 * ctx.margin
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
        area_h = H * (0.60 if ctx.vertical else 0.66) - head_h - (counter_h + 34 * u if ctx.vertical and self.counter else 0)
        tag_size_k = 0.72
        kinds = ("sans_bold", ctx.body)
        size = 40 * u * ctx.fs
        floor = max(12.0, 14 * u)
        while True:
            pad_x = size * 0.55
            row_h = size * 1.75
            tags = [make_block(h["text"], "sans_bold", size * tag_size_k, table_w * 0.3, min_size=size * tag_size_k)
                    for h in self.highlights if h["text"]]
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
            if (need_w <= table_w and need_h <= area_h) or size <= floor:
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
        top = max(H * 0.05, ctx.cy - total_h / 2)
        self.head_xy = (ctx.margin, top)
        cy0 = top + head_h
        self.card = (ctx.margin, cy0, ctx.margin + table_w, cy0 + card_h)
        self.header_y = cy0 + card_pad
        self.rows_y0 = self.header_y + row_h
        self.col_x = [ctx.margin]
        for w in self.col_w[:-1]:
            self.col_x.append(self.col_x[-1] + w)
        if side_counter:
            self.cnt_xy = (ctx.margin + table_w + gap + counter_w / 2, cy0 + card_h / 2 - counter_h / 2)
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
        self.tags = {id(h): make_block(h["text"], "sans_bold", size * tag_size_k, table_w * 0.3, min_size=size * tag_size_k)
                     for h in self.highlights if h["text"]}
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
        self.states = array_states(spec)
        self.times = [s.t for s in self.states]
        self.n_start = len(spec.get("items") or [])
        self.maxn = max(1, max(len(s.cells) for s in self.states))
        pad = 28 * u
        inner_w = W - 2 * ctx.margin - 2 * pad
        self.pitch = min(inner_w / self.maxn, 300 * u)
        self.cell_w = self.pitch * 0.84
        self.cell_h = min(self.cell_w * 0.8, 200 * u)
        size = min(self.cell_h * 0.38, 64 * u * ctx.fs)
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
        self.title = (ctx.block(str(spec["title"]), ctx.display, 56 * u * ctx.fs, W - 2 * ctx.margin, align="center")
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
        top = max(H * 0.05, ctx.cy - total / 2)
        self.title_xy = (W / 2, top)
        self.card = (ctx.margin, top + title_h + zone_top, W - ctx.margin, top + title_h + zone_top + self.cell_h + 2 * pad)
        self.cell_cy = self.card[1] + pad + self.cell_h / 2
        self.pad = pad
        if self.states[-1].t + MOVE_SECONDS > self.duration:
            ctx.warn("an array step ends after the scene does; lengthen the scene or move the steps earlier")

    # --- geometry (also used by tests) ------------------------------------------------------------------

    def slot_x(self, index: float, n: float) -> float:
        """Centre x of slot `index` when the array holds `n` cells (both may be fractional while cells move)."""
        return self.ctx.W / 2 + (index - (n - 1) / 2) * self.pitch

    def state_at(self, t: float) -> int:
        """Index of the state in force at scene time t."""
        return max(0, bisect.bisect_right(self.times, t) - 1)

    def cell_positions(self, t: float) -> dict[int, tuple[float, float, float]]:
        """{cell id: (centre x, y offset, opacity)} for every cell drawn at t, including one that is leaving."""
        si = self.state_at(t)
        cur = self.states[si]
        prev = self.states[si - 1] if si > 0 else None
        p = (t - cur.t) / MOVE_SECONDS if si > 0 else 1.0
        e = ease("in_out", p)
        n_prev = len(prev.cells) if prev else len(cur.cells)
        n_eff = n_prev + (len(cur.cells) - n_prev) * e
        before = {c.id: i for i, c in enumerate(prev.cells)} if prev else {}
        out: dict[int, tuple[float, float, float]] = {}
        for i, cell in enumerate(cur.cells):
            intro = 1.0 if cell.id >= self.n_start else _appear(t, 0.1 + cell.id * 0.06, 0.4)
            if cell.id in before:
                idx = before[cell.id] + (i - before[cell.id]) * e
                out[cell.id] = (self.slot_x(idx, n_eff), 0.0, intro)
            elif prev is not None:  # a new cell drops in from above and settles
                drop = (1 - ease("back", p)) * self.cell_h * 1.0
                out[cell.id] = (self.slot_x(i, n_eff), -drop, min(1.0, p * 2.5))
            else:
                out[cell.id] = (self.slot_x(i, n_eff), 0.0, intro)
        if prev is not None and cur.removed is not None:  # the cell that left drifts off its end and fades
            gone = cur.removed
            idx = before[gone.id]
            sign = -1.0 if cur.side == "front" else 1.0
            alpha = max(0.0, 1.0 - p * 1.3)
            if alpha > 0:
                out[gone.id] = (self.slot_x(idx, n_eff) + sign * self.pitch * 0.9 * ease("out", p),
                                -self.cell_h * 0.35 * ease("out", p), alpha)
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
        marked = set(cur.marks)
        mark_a = ease_out(p) if prev is not None and prev.marks != cur.marks else 1.0
        card_c = th.card
        base_fill = lerp_color(th.card, th.fg, 0.07)
        cw, ch = self.cell_w, self.cell_h
        for cid, (x, dy, a) in pos.items():
            if a <= 0:
                continue
            y = self.cell_cy + dy
            is_marked = cid in marked
            mc = _color(c, cur.mark_color)
            fill = lerp_color(base_fill, mc, 0.25 * mark_a) if is_marked else base_fill
            outline = lerp_color(th.line, mc, mark_a) if is_marked else th.line
            d.rounded_rectangle([x - cw / 2, y - ch / 2, x + cw / 2, y + ch / 2], int(14 * u),
                                fill=lerp_color(card_c, fill, a), outline=lerp_color(card_c, outline, a),
                                width=max(2, int((5 if is_marked else 3) * u)))
            blk = self.blocks[cid]
            blit(img, blk, (x, y), th.fg, a, "mc")
        # index numbers under the slots
        n_show = max(len(cur.cells), n_prev)
        for i in range(n_show):
            a = 1.0 if i < min(len(cur.cells), n_prev) else (e if i < len(cur.cells) else 1.0 - e)
            if a > 0:
                blit(img, self.idx_blocks[i], (self.slot_x(i, n_eff), self.card[3] + 8 * u), th.muted, a, "tc")
        # mark label under the index row
        if marked and cur.mark_text:
            xs = [pos[m][0] for m in cur.marks if m in pos]
            if xs:
                blit(img, self.tag_blocks[("mark", cur.mark_text)], (sum(xs) / len(xs), self.card[3] + 50 * u * c.fs),
                     _color(c, cur.mark_color), mark_a, "tc")
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
