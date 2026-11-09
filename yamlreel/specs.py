"""Load and validate a video spec (YAML), layered over the configuration.

One validator checks the type and range of every top-level key and every scene field after the spec has been
merged over the configuration. Unknown keys at any level are errors. Messages follow two shapes:
`<path> must be <expectation>` and `<path>: <problem>`, with paths such as `scenes[2].items[0]`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from . import shots
from .config import (DEFAULTS, HARD_LIMITS, SHARED_KEYS, Config, _absolutize, clamp_limits, describe_key,
                     describe_value, merge, normalize_music)
from .config import yaml_problem as _yaml_problem
from .datascenes import (ARRAY_ACTIONS, DEFAULT_CTA_PROMPT, GRID_ACTIONS, GRID_CHARS, GRID_ORDERS, GRID_REVEALS,
                         MAX_ANSWER_CHARS, MAX_ANSWERS, MAX_AXIS_TICKS, MAX_CELL_CHARS, MAX_CELLS, MAX_GRID,
                         MAX_METER_CAPACITY, MAX_PROMPT_CHARS, MAX_QUESTION_CHARS, MAX_SEED, MAX_SLOTS, MAX_STEPS,
                         MAX_TABLE_COLS, MAX_TABLE_ROWS, MAX_TIMELINE_EVENTS, MAX_TIMELINE_WINDOWS, MAX_TREE_CHILDREN,
                         MAX_TREE_LEVELS, MAX_TREE_NODES, MAX_TREE_PATHS, METER_SHAPES, POP_SIDES, ArrayScene,
                         ArrayStepError, GridScene, GridStepError, MeterScene, TableScene, TimelineScene, TreeScene,
                         array_states, grid_states)
from .music import ENERGY_LEVELS, NOTE_OFFSETS, SCALES, STYLE_DEFAULTS
from .subtitles import (DEFAULT_SUBTITLES, MAX_KEYWORD_CHARS, MAX_KEYWORDS, POSITIONS, STYLES)
from .scenes import SCENE_TYPES, DiagramScene, Scene
from .textkit import MAX_COUNT_MAGNITUDE, split_count
from .theme import get_theme
from .voice import MAX_SPEED, MIN_SPEED

FORMATS = {"landscape": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}

DEFAULT_SECONDS = {"title": 3.0, "type": 3.0, "shot": 4.0, "diagram": 7.0,
                   "bullets": 5.0, "code": 6.0, "stat": 3.5, "table": 8.0, "array": 8.0, "meter": 10.0, "tree": 8.0,
                   "timeline": 10.0, "grid": 10.0, "cta": 6.0}
REQUIRED = {
    "title": ["title"],
    "type": ["text"],
    "shot": ["source"],
    "diagram": ["nodes"],
    "bullets": ["items"],
    "code": ["code"],
    "stat": ["value"],
    "table": ["columns", "rows"],
    "array": ["items"],
    "meter": ["capacity"],
    "tree": ["nodes"],
    "timeline": ["axis", "events"],
    "grid": ["cells"],
    "cta": ["question"],
}
MUSIC_STYLES = {*STYLE_DEFAULTS, "none"}
TRANSITIONS = ("fade", "slide", "zoom", "cut")
TRANSITION_SECONDS = (0.1, 1.0)
DEFAULT_TRANSITION_SECONDS = 0.25
STAGGER_SECONDS = (0.1, 3.0)
WATERMARK_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right"}
NODE_KINDS = ("client", "service", "cache", "db")
COLOR_NAMES = ("accent", "accent2", "fg", "muted")

MAX_SPEC_BYTES = 1024 * 1024
MAX_TEXT = 2000          # characters in any one text field
MAX_CODE_CHARS = 8000    # characters in a code scene
MAX_CODE_LINES = 60
MAX_CODE_COLS = 120
MAX_ITEMS = 20
MAX_NODES = 40
MAX_EDGES = 80
MAX_FLASH = 40
MAX_CLICKS = 10
MAX_AXIS_VALUE = 1e9       # largest axis value
MAX_ID_CHARS = 100       # a node id
MAX_CUE_SECONDS = 600    # latest `at` of a node, edge or flash
MAX_META_ENTRIES = 50    # tags, links or credits
MAX_THEMES = 100         # custom themes in one spec or config
MAX_THEME_NAME = 60
MAX_FORMATS = 3
MAX_STEP_LABEL = 60      # characters in a scene's `step` label
MAX_STEP_LABELS = 12     # distinct `step` labels in one spec (the badge draws one progress segment per label)
MAX_KICKER_CHARS = 48    # characters in a scene's `kicker`
NODE_SIZE = (16, 120)
# Output frames over all formats: one hour at 60 fps, or two formats at 30 fps.
MAX_FRAMES = 216_000


class SpecError(ValueError):
    pass


def suggest(key: str, allowed: Iterable[str]) -> str | None:
    """Hook for "did you mean" hints on unknown keys (GitHub issue #13). Returns text to append, or None."""
    return None


def _unknown_key(path: str, key: object, allowed: Iterable[str], what: str, exc: type[Exception] = SpecError,
                 problem: str = "unknown field", note: str = "") -> Exception:
    """The error for a key that `what` does not have. `path` is the mapping that holds the key."""
    name = describe_key(key)
    full = f"{path}.{name}" if path else name
    msg = f"{full}: {problem} for {what}; allowed: {', '.join(sorted(allowed))}"
    if note:
        msg += f" ({note})"
    hint = suggest(key, allowed) if isinstance(key, str) else None
    return exc(f"{msg} ({hint})" if hint else msg)


def _no_unknown(path: str, raw: dict[Any, Any], allowed: Iterable[str], what: str) -> None:
    allowed = set(allowed)
    for key in raw:  # file order: the first mistake the author made is the one reported
        if key not in allowed:
            raise _unknown_key(path, key, allowed, what)


def load_spec(path: str | Path, config: Config | None = None) -> dict[str, Any]:
    p = Path(path)
    cfg = config or Config()
    try:
        too_big = p.stat().st_size > MAX_SPEC_BYTES
        text = "" if too_big else p.read_text(encoding="utf-8")
    except OSError as e:
        raise SpecError(f"cannot read spec {p}: {e}") from e
    except UnicodeDecodeError as e:
        raise SpecError(f"cannot read spec {p}: not valid UTF-8 ({e.reason})") from e
    if too_big:
        raise SpecError(f"spec {p} is larger than {MAX_SPEC_BYTES} bytes")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError(f"cannot parse spec {p}: {_yaml_problem(e)}") from e
    except (ValueError, RecursionError) as e:  # an impossible date, or nesting deeper than the parser allows
        raise SpecError(f"cannot parse spec {p}: {' '.join(str(e).split()) or 'nested too deeply'}") from e
    spec = validate(raw, cfg, base_dir=p.resolve().parent)
    spec["_dir"] = str(p.resolve().parent)
    return spec


def check_frame_budget(seconds: float, fps: int, n_formats: int) -> None:
    """Refuse a render that would write more than MAX_FRAMES frames over all its formats."""
    frames = round(seconds * fps) * n_formats
    if frames > MAX_FRAMES:
        plural = "s" if n_formats != 1 else ""
        raise SpecError(f"estimated {frames} frames ({seconds:g}s x {fps} fps x {n_formats} format{plural}); "
                        f"the limit is {MAX_FRAMES} (MAX_FRAMES)")


# --- value checkers -------------------------------------------------------------------------------------

def _fmt(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:g}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _number(path: str, v: Any, lo: float, hi: float, integer: bool = False) -> Any:
    whole = isinstance(v, int) or (isinstance(v, float) and v.is_integer())  # 30.0 is 30
    ok = _is_number(v) and (not integer or whole) and lo <= v <= hi
    if not ok:
        raise SpecError(f"{path} must be {'an integer' if integer else 'a number'} between {_fmt(lo)} and {_fmt(hi)}")
    return int(v) if integer else float(v)


def _text(path: str, v: Any, limit: int = MAX_TEXT, limit_name: str = "MAX_TEXT") -> str:
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise SpecError(f"{path} must be text")
    s = str(v)
    if len(s) > limit:
        raise SpecError(f"{path} is {len(s)} characters; the limit is {limit} ({limit_name})")
    return s


def _bool(path: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise SpecError(f"{path} must be true or false")
    return v


def _choice(path: str, v: Any, options: Iterable[str]) -> str:
    options = list(options)
    if not isinstance(v, str) or v not in options:
        raise SpecError(f"{path} must be one of {', '.join(options)}")
    return v


def _list(path: str, v: Any, lo: int, hi: int, limit_name: str | None = None) -> list[Any]:
    if not isinstance(v, list):
        raise SpecError(f"{path} must be a list")
    if len(v) > hi:  # checked before anything iterates it
        named = f" ({limit_name})" if limit_name else ""
        raise SpecError(f"{path} has {len(v)} entries; the limit is {hi}{named}")
    if len(v) < lo:
        raise SpecError(f"{path} must have at least {lo} entr{'y' if lo == 1 else 'ies'}")
    return v


def _mapping(path: str, v: Any) -> dict[Any, Any]:
    if not isinstance(v, dict):
        raise SpecError(f"{path} must be a mapping")
    return v


def _node_id(path: str, v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int)) or not 1 <= len(str(v)) <= MAX_ID_CHARS:
        raise SpecError(f"{path} must be text or an integer of 1 to {MAX_ID_CHARS} characters")
    return str(v)


@dataclass
class _Env:
    """What scene checkers need from the rest of the spec."""
    max_seconds: float = HARD_LIMITS["max_seconds"]
    max_scenes: int = HARD_LIMITS["max_scenes"]
    capped: frozenset[str] = frozenset()  # limits that were lowered to their absolute limit
    transition: dict[str, Any] = field(default_factory=lambda: {"type": "fade", "seconds": DEFAULT_TRANSITION_SECONDS})

    def note(self, key: str) -> str:
        suffix = f", capped at the absolute limit {HARD_LIMITS[key]}" if key in self.capped else ""
        return f"limits.{key}{suffix}"


Checker = Callable[[str, Any, dict[str, Any], _Env], Any]


# --- scene fields ---------------------------------------------------------------------------------------

def _scene_type(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    if not isinstance(v, str) or v not in SCENE_TYPES:
        raise SpecError(f"{path}: unknown type {describe_value(v)}; choose from {', '.join(SCENE_TYPES)}")
    return v


def _seconds(path: str, v: Any, sc: dict[str, Any], env: _Env) -> float:
    try:
        return _number(path, v, 0.1, env.max_seconds)
    except SpecError as e:
        if "max_seconds" in env.capped:
            raise SpecError(f"{e} ({env.note('max_seconds')})") from e
        raise


def _narration(path: str, v: Any, sc: dict[str, Any], env: _Env) -> Any:
    return False if v is False else _text(path, v)


def _narration_audio(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f"{path} must be a path to a .wav file")
    return _text(path, v)


def _narration_words(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f"{path} must be a path to a .json file of {{word, start, end}} entries")
    return _text(path, v)


def _step_label(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    label = " ".join(_text(path, v, MAX_STEP_LABEL, "MAX_STEP_LABEL").split())
    if not label:
        raise SpecError(f"{path} must not be empty")
    return label


def _question(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    q = " ".join(_text(path, v, MAX_QUESTION_CHARS, "MAX_QUESTION_CHARS").split())
    if not q:
        raise SpecError(f"{path} must not be empty")
    return q


def _answers(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    out = []
    for n, a in enumerate(_list(path, v, 0, MAX_ANSWERS, "MAX_ANSWERS")):
        text = " ".join(_text(f"{path}[{n}]", a, MAX_ANSWER_CHARS, "MAX_ANSWER_CHARS").split())
        if not text:
            raise SpecError(f"{path}[{n}] must not be empty")
        out.append(text)
    return out


def _prompt(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    text = " ".join(_text(path, v, MAX_PROMPT_CHARS, "MAX_PROMPT_CHARS").split())
    if not text:
        raise SpecError(f"{path} must not be empty")
    return text


def _keywords(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    out = []
    for n, k in enumerate(_list(path, v, 0, MAX_KEYWORDS, "MAX_KEYWORDS")):
        if isinstance(k, float):   # YAML reads 3.50 as 3.5, which would no longer match the word "3.50"
            raise SpecError(f"{path}[{n}] must be text; put a decimal number in quotes (\"{k:g}\") so it is kept as written")
        word = _text(f"{path}[{n}]", k, MAX_KEYWORD_CHARS, "MAX_KEYWORD_CHARS").strip()
        if not word:
            raise SpecError(f"{path}[{n}] must not be empty")
        out.append(word)
    return out


def _source(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f"{path} must be a URL or a file path")
    return _text(path, v)


def _zoom(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[float]:
    if isinstance(v, list):
        if len(v) != 2:
            raise SpecError(f"{path} must be a number or [from, to]")
        return [_number(f"{path}[{n}]", x, 1.0, 8.0) for n, x in enumerate(v)]
    return [1.0, _number(path, v, 1.0, 8.0)]


def _point(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[float]:
    if not isinstance(v, list) or len(v) != 2:
        raise SpecError(f"{path} must be [x, y] between 0 and 1")
    return [_number(f"{path}[{n}]", x, 0.0, 1.0) for n, x in enumerate(v)]


def _viewport(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[int]:
    if not isinstance(v, list) or len(v) != 2:
        raise SpecError(f"{path} must be [width, height]")
    return [int(_number(f"{path}[{n}]", x, 320, 3840)) for n, x in enumerate(v)]


def _click(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    clicks = _list(path, v, 0, MAX_CLICKS, "MAX_CLICKS")
    for n, c in enumerate(clicks):
        if not isinstance(c, str) or not 0 < len(c) < 300:
            raise SpecError(f"{path}[{n}] must be a CSS selector of under 300 characters")
    return list(clicks)


def _items(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    return [_text(f"{path}[{n}]", x) for n, x in enumerate(_list(path, v, 1, MAX_ITEMS, "MAX_ITEMS"))]


def _code_lines(code: str) -> list[str]:
    return code.rstrip("\n").split("\n")


def _code(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    code = _text(path, v, MAX_CODE_CHARS, "MAX_CODE_CHARS")
    lines = _code_lines(code)
    if len(lines) > MAX_CODE_LINES or max(len(l) for l in lines) > MAX_CODE_COLS:
        raise SpecError(f"{path}: at most {MAX_CODE_LINES} lines of {MAX_CODE_COLS} columns")
    return code


def _highlight(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[int]:
    n_lines = len(_code_lines(sc["code"]))  # `code` is checked first
    hl = _list(path, v, 0, MAX_CODE_LINES, "MAX_CODE_LINES")
    for n, line in enumerate(hl):
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= n_lines:
            raise SpecError(f"{path}[{n}] must be a line number between 1 and {n_lines}")
    return list(hl)


def _node(path: str, n: Any, seen: set[str]) -> dict[str, Any]:
    n = _mapping(path, n)
    _no_unknown(path, n, DiagramScene.NODE_FIELDS, "a diagram node")
    if "id" not in n or "label" not in n:
        raise SpecError(f"{path}: every node needs id and label")
    out = dict(n)
    out["id"] = _node_id(f"{path}.id", n["id"])
    if out["id"] in seen:
        raise SpecError(f"{path}.id: duplicate node id {out['id']!r}")
    seen.add(out["id"])
    if "x" not in n or "y" not in n:
        raise SpecError(f"{path}: node {out['id']!r} needs x and y (0..1)")
    out["label"] = _text(f"{path}.label", n["label"])
    for k in ("x", "y", "vx", "vy"):
        if k in n:
            out[k] = _number(f"{path}.{k}", n[k], 0.0, 1.0)
    if "at" in n:
        out["at"] = _number(f"{path}.at", n["at"], 0, MAX_CUE_SECONDS)
    if "kind" in n:
        out["kind"] = _choice(f"{path}.kind", n["kind"], NODE_KINDS)
    if "sub" in n:
        out["sub"] = _text(f"{path}.sub", n["sub"])
    return out


def _nodes(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    seen: set[str] = set()
    return [_node(f"{path}[{n}]", x, seen) for n, x in enumerate(_list(path, v, 1, MAX_NODES, "MAX_NODES"))]


def _node_ref(path: str, v: Any, ids: set[str]) -> str:
    ref = _node_id(path, v)
    if ref not in ids:
        raise SpecError(f"{path}: unknown node {ref!r}")
    return ref


def _edges(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    ids = {n["id"] for n in sc["nodes"]}  # `nodes` is checked first
    out = []
    for n, e in enumerate(_list(path, v, 0, MAX_EDGES, "MAX_EDGES")):
        p = f"{path}[{n}]"
        e = _mapping(p, e)
        _no_unknown(p, e, DiagramScene.EDGE_FIELDS, "a diagram edge")
        if "from" not in e or "to" not in e:
            raise SpecError(f"{p}: needs from and to")
        edge = dict(e)
        edge["from"] = _node_ref(f"{p}.from", e["from"], ids)
        edge["to"] = _node_ref(f"{p}.to", e["to"], ids)
        if "label" in e:
            edge["label"] = _text(f"{p}.label", e["label"])
        if "at" in e:
            edge["at"] = _number(f"{p}.at", e["at"], 0, MAX_CUE_SECONDS)
        if "color" in e:
            edge["color"] = _choice(f"{p}.color", e["color"], COLOR_NAMES)
        for k in ("bad", "dashed", "flow"):
            if k in e:
                edge[k] = _bool(f"{p}.{k}", e[k])
        out.append(edge)
    return out


def _flash(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    ids = {n["id"] for n in sc["nodes"]}
    out = []
    for n, f in enumerate(_list(path, v, 0, MAX_FLASH, "MAX_FLASH")):
        p = f"{path}[{n}]"
        f = _mapping(p, f)
        _no_unknown(p, f, DiagramScene.FLASH_FIELDS, "a diagram flash")
        if "node" not in f:
            raise SpecError(f"{p}: needs node")
        flash = dict(f)
        flash["node"] = _node_ref(f"{p}.node", f["node"], ids)
        flash["at"] = _number(f"{p}.at", f.get("at", 0), 0, MAX_CUE_SECONDS)
        if "text" in f:
            flash["text"] = _text(f"{p}.text", f["text"])
        if "color" in f:
            flash["color"] = _choice(f"{p}.color", f["color"], COLOR_NAMES)
        out.append(flash)
    return out


def _text_field(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    return _text(path, v)


def _kicker(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    out = _text(path, v, MAX_KICKER_CHARS, "MAX_KICKER_CHARS")
    if "\n" in out or "\r" in out:
        raise SpecError(f"{path} must be one line of text, without a line break")
    if not out.strip():
        raise SpecError(f"{path} must not be empty")
    return out


def _bool_field(path: str, v: Any, sc: dict[str, Any], env: _Env) -> bool:
    return _bool(path, v)


def _energy(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    return _choice(path, v, ENERGY_LEVELS)


def _transition_from(path: str, v: Any, base: dict[str, Any]) -> dict[str, Any]:
    """`fade`, or {type, seconds}. A field that is not set is taken from `base`. Normalised to both keys."""
    if isinstance(v, str):
        v = {"type": v}
    tr = dict(_mapping(path, v))
    if not tr:
        raise SpecError(f"{path} must be one of {', '.join(TRANSITIONS)} or a mapping with type and seconds")
    _no_unknown(path, tr, ("type", "seconds"), "transition")
    return {"type": _choice(f"{path}.type", tr.get("type", base["type"]), TRANSITIONS),
            "seconds": _number(f"{path}.seconds", tr.get("seconds", base["seconds"]), *TRANSITION_SECONDS)}


def _transition_top(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    """The default for every scene; the scenes are checked after it and inherit what they leave out."""
    env.transition = _transition_from(path, v, {"type": "fade", "seconds": DEFAULT_TRANSITION_SECONDS})
    return dict(env.transition)


def _transition(path: str, v: Any, sc: dict[str, Any], env: _Env) -> dict[str, Any]:
    """A scene's transition: what it sets, and the top-level transition for the rest."""
    return _transition_from(path, v, env.transition)


def _stagger(path: str, v: Any, sc: dict[str, Any], env: _Env) -> float:
    return _number(path, v, *STAGGER_SECONDS)


def _count(path: str, v: Any, sc: dict[str, Any], env: _Env) -> dict[str, Any] | bool:
    """`true`, `false` or {from, seconds}: count the one number in `value` up from `from`."""
    if v is False:
        return False
    count = {} if v is True else dict(_mapping(path, v))
    if v is not True and not count:
        raise SpecError(f"{path} must be true, false or a mapping with from and seconds")
    _no_unknown(path, count, ("from", "seconds"), "count")
    value = sc.get("value")
    try:
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError("value must contain exactly one number to count")
        split_count(str(value))
    except ValueError as e:
        raise SpecError(f"{path}: {e}") from e
    return {"from": _number(f"{path}.from", count.get("from", 0), -MAX_COUNT_MAGNITUDE, MAX_COUNT_MAGNITUDE),
            "seconds": (_number(f"{path}.seconds", count["seconds"], 0.1, env.max_seconds)
                        if "seconds" in count else None)}


def _cell_text(path: str, v: Any) -> str:
    """One table or array cell. Quote a number in YAML to keep its formatting (`"007"`, `"1.50"`)."""
    if v is None or isinstance(v, (list, dict)):
        raise SpecError(f"{path} must be text")
    out = _text(path, v, MAX_CELL_CHARS, "MAX_CELL_CHARS")
    if "\n" in out or "\r" in out:
        raise SpecError(f"{path} must be one line of text, without a line break")
    if not out.strip():
        raise SpecError(f"{path} must not be empty")
    return out


def _columns(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    return [_cell_text(f"{path}[{n}]", x) for n, x in enumerate(_list(path, v, 1, MAX_TABLE_COLS, "MAX_TABLE_COLS"))]


def _rows(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[list[str]]:
    width = len(sc["columns"])  # `columns` is checked first
    out = []
    for n, row in enumerate(_list(path, v, 1, MAX_TABLE_ROWS, "MAX_TABLE_ROWS")):
        p = f"{path}[{n}]"
        if not isinstance(row, list):
            raise SpecError(f"{p} must be a list of {width} cells, one per column")
        if len(row) != width:
            raise SpecError(f"{p} must have {width} cell{'s' if width != 1 else ''} (one per column), got {len(row)}")
        out.append([_cell_text(f"{p}[{j}]", x) for j, x in enumerate(row)])
    return out


def _reveal(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    return _choice(path, v, TableScene.REVEALS)


def _scan(path: str, v: Any, sc: dict[str, Any], env: _Env) -> dict[str, Any]:
    """`true` (a scan from the first row to the last), or {at, per_row, stop, color}."""
    scan = {} if v is True else dict(_mapping(path, v))
    if v is not True and not scan:
        raise SpecError(f"{path} must be true or a mapping with at, per_row, stop or color")
    _no_unknown(path, scan, TableScene.SCAN_FIELDS, "a table scan")
    rows = len(sc["rows"])
    out: dict[str, Any] = {"stop": rows - 1}
    if "at" in scan:
        out["at"] = _number(f"{path}.at", scan["at"], 0, MAX_CUE_SECONDS)
    if "per_row" in scan:
        out["per_row"] = _number(f"{path}.per_row", scan["per_row"], 0.05, 3)
    if "stop" in scan:
        out["stop"] = _number(f"{path}.stop", scan["stop"], 0, rows - 1, integer=True)
    if "color" in scan:
        out["color"] = _choice(f"{path}.color", scan["color"], COLOR_NAMES)
    return out


def _table_highlight(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    rows = len(sc["rows"])
    out = []
    for n, h in enumerate(_list(path, v, 0, MAX_TABLE_ROWS, "MAX_TABLE_ROWS")):
        p = f"{path}[{n}]"
        h = _mapping(p, h)
        _no_unknown(p, h, TableScene.HIGHLIGHT_FIELDS, "a table highlight")
        if "row" not in h:
            raise SpecError(f"{p}: needs row")
        item: dict[str, Any] = {"row": _number(f"{p}.row", h["row"], 0, rows - 1, integer=True)}
        if "at" in h:
            item["at"] = _number(f"{p}.at", h["at"], 0, MAX_CUE_SECONDS)
        if "text" in h:
            item["text"] = _text(f"{p}.text", h["text"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
        if "color" in h:
            item["color"] = _choice(f"{p}.color", h["color"], COLOR_NAMES)
        out.append(item)
    return out


def _counter(path: str, v: Any, sc: dict[str, Any], env: _Env) -> dict[str, Any]:
    counter = dict(_mapping(path, v))
    _no_unknown(path, counter, TableScene.COUNTER_FIELDS, "a table counter")
    if "to" not in counter:
        raise SpecError(f"{path}: needs to")
    out: dict[str, Any] = {"to": _number(f"{path}.to", counter["to"], -MAX_COUNT_MAGNITUDE, MAX_COUNT_MAGNITUDE)}
    if "label" in counter:
        out["label"] = _text(f"{path}.label", counter["label"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
    if "at" in counter:
        out["at"] = _number(f"{path}.at", counter["at"], 0, MAX_CUE_SECONDS)
    if "seconds" in counter:
        out["seconds"] = _number(f"{path}.seconds", counter["seconds"], 0.1, env.max_seconds)
    return out


def _cells(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    return [_cell_text(f"{path}[{n}]", x) for n, x in enumerate(_list(path, v, 0, MAX_CELLS, "MAX_CELLS"))]


def _step(path: str, st: Any) -> dict[str, Any]:
    st = _mapping(path, st)
    _no_unknown(path, st, ArrayScene.STEP_FIELDS, "an array step")
    given = [a for a in ARRAY_ACTIONS if a in st]
    if len(given) != 1:
        found = f", found {' and '.join(given)}" if given else ""
        raise SpecError(f"{path}: needs exactly one of {', '.join(ARRAY_ACTIONS)}{found}")
    action = given[0]
    out: dict[str, Any] = {}
    if "at" in st:
        out["at"] = _number(f"{path}.at", st["at"], 0, MAX_CUE_SECONDS)
    p = f"{path}.{action}"
    if action in ("insert", "push"):
        out[action] = _cell_text(p, st[action])
    elif action == "pop":
        out["pop"] = _choice(p, st["pop"], POP_SIDES)
    elif action == "mark":
        idx = _list(p, st["mark"], 1, MAX_CELLS, "MAX_CELLS")
        out["mark"] = [_number(f"{p}[{n}]", x, 0, MAX_CELLS - 1, integer=True) for n, x in enumerate(idx)]
    elif action == "unmark":
        if st["unmark"] is not True:
            raise SpecError(f"{p} must be true")
        out["unmark"] = True
    else:
        w = st["window"]
        if not isinstance(w, list) or len(w) != 2:
            raise SpecError(f"{p} must be [first, last] cell indices")
        lo, hi = (_number(f"{p}[{n}]", x, 0, MAX_CELLS - 1, integer=True) for n, x in enumerate(w))
        if lo > hi:
            raise SpecError(f"{p} must be [first, last] with first <= last")
        out["window"] = [lo, hi]
    for key in ("text", "color"):
        if key in st:
            if action not in ("mark", "window"):
                raise SpecError(f"{path}.{key}: only a mark or window step has {key}")
            out[key] = (_text(f"{path}.text", st[key], MAX_CELL_CHARS, "MAX_CELL_CHARS") if key == "text"
                        else _choice(f"{path}.color", st[key], COLOR_NAMES))
    return out


def _steps(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    steps = [_step(f"{path}[{n}]", st) for n, st in enumerate(_list(path, v, 0, MAX_STEPS, "MAX_STEPS"))]
    try:  # replay the steps now, so a pop on an empty array or a window past the cells fails at load time
        array_states({"items": sc["items"], "steps": steps, "stagger": sc.get("stagger")})
    except ArrayStepError as e:
        raise SpecError(f"{path.rsplit('.', 1)[0]}.{e}") from e
    return steps


def _meter_shape(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    return _choice(path, v, METER_SHAPES)


def _meter_capacity(path: str, v: Any, sc: dict[str, Any], env: _Env) -> float:
    if sc.get("shape", "bucket") == "slots":
        return _number(path, v, 1, MAX_SLOTS, integer=True)   # one slot per unit
    cap = _number(path, v, 0, MAX_METER_CAPACITY)
    if cap <= 0:
        raise SpecError(f"{path} must be a number greater than 0 (and at most {_fmt(MAX_METER_CAPACITY)})")
    return cap


def _meter_start(path: str, v: Any, sc: dict[str, Any], env: _Env) -> float:
    return _number(path, v, 0, sc["capacity"])


def _meter_label(path: str, v: Any, sc: dict[str, Any], env: _Env) -> str:
    return _text(path, v, MAX_CELL_CHARS, "MAX_CELL_CHARS")


def _meter_steps(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(_list(path, v, 0, MAX_STEPS, "MAX_STEPS")):
        p = f"{path}[{n}]"
        st = _mapping(p, raw)
        _no_unknown(p, st, MeterScene.STEP_FIELDS, "a meter step")
        for key in ("at", "level"):
            if key not in st:
                raise SpecError(f"{p}: needs {key}")
        item: dict[str, Any] = {"at": _number(f"{p}.at", st["at"], 0, MAX_CUE_SECONDS),
                                "level": _number(f"{p}.level", st["level"], 0, sc["capacity"])}
        if out and item["at"] < out[-1]["at"]:
            raise SpecError(f"{p}.at must not be earlier than the previous step ({_fmt(out[-1]['at'])})")
        if "seconds" in st:
            item["seconds"] = _number(f"{p}.seconds", st["seconds"], 0.1, env.max_seconds)
        if "text" in st:
            item["text"] = _text(f"{p}.text", st["text"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
        if "color" in st:
            item["color"] = _choice(f"{p}.color", st["color"], COLOR_NAMES)
        out.append(item)
    return out


def _grid_cells(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[str]:
    hint = 'quote rows that start with # ("#..#")'  # YAML reads an unquoted # as the start of a comment
    if v is None:
        raise SpecError(f"{path} must be a list of rows; {hint}")
    rows = _list(path, v, 1, MAX_GRID, "MAX_GRID")
    for n, row in enumerate(rows):
        p = f"{path}[{n}]"
        if not isinstance(row, str):
            raise SpecError(f"{p} must be text; {hint}")
        if not 1 <= len(row) <= MAX_GRID:
            raise SpecError(f"{p} has {len(row)} cells; use 1 to {MAX_GRID} (MAX_GRID)")
        if len(row) != len(rows[0]):
            raise SpecError(f"{p} has {len(row)} cells; row 0 has {len(rows[0])}")
        for j, ch in enumerate(row):
            if ch not in GRID_CHARS:
                raise SpecError(f"{p}: {ch!r} at column {j} is not a cell; use # (foreground), . (card) or o (accent)")
    return list(rows)


def _grid_seed(path: str, v: Any, sc: dict[str, Any], env: _Env) -> int:
    return _number(path, v, 0, MAX_SEED, integer=True)


def _grid_point(path: str, v: Any, rows: int, cols: int) -> list[int]:
    ok = (isinstance(v, list) and len(v) == 2 and all(_is_number(x) and float(x).is_integer() for x in v)
          and 0 <= v[0] < rows and 0 <= v[1] < cols)
    if not ok:
        raise SpecError(f"{path} must be [row, col] inside the {rows} x {cols} grid")
    return [int(v[0]), int(v[1])]


def _grid_points(path: str, v: Any, rows: int, cols: int) -> list[list[int]]:
    return [_grid_point(f"{path}[{n}]", x, rows, cols)
            for n, x in enumerate(_list(path, v, 1, rows * cols))]


# The keys a grid step takes besides `at` and its action, and which actions take them.
_GRID_EXTRAS = {"radius": ("stain",), "seconds": ("reveal", "stain"), "per_cell": ("recover",),
                "order": ("recover",), "text": ("outline", "fail"), "color": ("status", "outline")}
_GRID_OWNERS = {"radius": "a stain", "seconds": "a reveal or stain", "per_cell": "a recover", "order": "a recover",
                "text": "an outline or fail", "color": "a status or outline"}


def _grid_step(path: str, raw: Any, rows: int, cols: int, seconds: float) -> dict[str, Any]:
    st = _mapping(path, raw)
    _no_unknown(path, st, GridScene.STEP_FIELDS, "a grid step")
    given = [a for a in GRID_ACTIONS if a in st]
    if len(given) != 1:
        found = f", found {' and '.join(given)}" if given else ""
        raise SpecError(f"{path}: needs exactly one of {', '.join(GRID_ACTIONS)}{found}")
    action = given[0]
    if "at" not in st:
        raise SpecError(f"{path}: needs at")
    out: dict[str, Any] = {"at": _number(f"{path}.at", st["at"], 0, seconds)}
    p, v = f"{path}.{action}", st[action]
    if action == "reveal":
        out[action] = _choice(p, v, GRID_REVEALS)
    elif action == "stain":
        out[action] = _grid_point(p, v, rows, cols)
    elif action in ("clear_stain", "unmark", "fail"):
        if v is not True:
            raise SpecError(f"{p} must be true")
        out[action] = True
    elif action == "lose":
        out[action] = _grid_points(p, v, rows, cols)
    elif action == "recover":
        out[action] = "all" if v == "all" else _grid_points(p, v, rows, cols)
    elif action == "status":
        out[action] = _cell_text(p, v)
    else:  # outline
        ok = (isinstance(v, list) and len(v) == 4 and all(_is_number(x) and float(x).is_integer() for x in v)
              and 0 <= v[0] <= v[2] < rows and 0 <= v[1] <= v[3] < cols)
        if not ok:
            raise SpecError(f"{p} must be [r0, c0, r1, c1] inside the {rows} x {cols} grid with row0 <= row1 and col0 <= col1")
        out[action] = [int(x) for x in v]
    for key, owners in _GRID_EXTRAS.items():
        if key in st and action not in owners:
            raise SpecError(f"{path}.{key}: only {_GRID_OWNERS[key]} step has {key}")
    if action == "stain" and "radius" not in st:
        raise SpecError(f"{path}: a stain needs radius")
    kp = f"{path}."
    for key, check in (("radius", lambda x: _number(kp + "radius", x, 0.5, MAX_GRID)),
                       ("seconds", lambda x: _number(kp + "seconds", x, 0.1, 10)),
                       ("per_cell", lambda x: _number(kp + "per_cell", x, 0.01, 2)),
                       ("order", lambda x: _choice(kp + "order", x, GRID_ORDERS)),
                       ("text", lambda x: _cell_text(kp + "text", x)),
                       ("color", lambda x: _choice(kp + "color", x, COLOR_NAMES))):
        if key in st:
            out[key] = check(st[key])
    return out


def _grid_steps(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    rows, cols = len(sc["cells"]), len(sc["cells"][0])  # `cells` is checked first
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(_list(path, v, 0, MAX_STEPS, "MAX_STEPS")):
        step = _grid_step(f"{path}[{n}]", raw, rows, cols, sc["seconds"])
        if out and step["at"] < out[-1]["at"]:
            raise SpecError(f"{path}[{n}].at must not be earlier than the previous step ({_fmt(out[-1]['at'])})")
        out.append(step)
    try:  # replay the steps now, so a recover with nothing lost fails at load time
        grid_states({"cells": sc["cells"], "steps": out, "seed": sc.get("seed", 0)})
    except GridStepError as e:
        raise SpecError(f"{path.rsplit('.', 1)[0]}.{e}") from e
    return out


def _tree_nodes(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    level: dict[str, int] = {}
    children: dict[str, int] = {}
    for n, raw in enumerate(_list(path, v, 1, MAX_TREE_NODES, "MAX_TREE_NODES")):
        p = f"{path}[{n}]"
        node = _mapping(p, raw)
        _no_unknown(p, node, TreeScene.NODE_FIELDS, "a tree node")
        if "id" not in node or "label" not in node:
            raise SpecError(f"{p}: every node needs id and label")
        item: dict[str, Any] = {"id": _node_id(f"{p}.id", node["id"])}
        if item["id"] in level:
            raise SpecError(f"{p}.id: duplicate node id {item['id']!r}")
        item["label"] = _text(f"{p}.label", node["label"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
        if n == 0:
            if node.get("parent") is not None:
                raise SpecError(f"{p}.parent: the first node is the root and has no parent")
            level[item["id"]] = 1
        else:
            if "parent" not in node:
                raise SpecError(f"{p}: needs parent (only the first node is the root)")
            parent = _node_id(f"{p}.parent", node["parent"])
            if parent not in level:
                raise SpecError(f"{p}.parent: unknown node {parent!r} (a parent must be declared before its children)")
            if children.get(parent, 0) >= MAX_TREE_CHILDREN:
                raise SpecError(f"{p}.parent: {parent!r} already has {MAX_TREE_CHILDREN} children; the limit is "
                                f"{MAX_TREE_CHILDREN} (MAX_TREE_CHILDREN)")
            if level[parent] >= MAX_TREE_LEVELS:
                raise SpecError(f"{p}.parent: this would be level {level[parent] + 1}; the limit is "
                                f"{MAX_TREE_LEVELS} levels, the root being 1 (MAX_TREE_LEVELS)")
            children[parent] = children.get(parent, 0) + 1
            level[item["id"]] = level[parent] + 1
            item["parent"] = parent
        if "at" in node:
            item["at"] = _number(f"{p}.at", node["at"], 0, MAX_CUE_SECONDS)
        out.append(item)
    return out


def _tree_path(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    parent_of = {n["id"]: n.get("parent") for n in sc["nodes"]}   # `nodes` is checked first
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(_list(path, v, 0, MAX_TREE_PATHS, "MAX_TREE_PATHS")):
        p = f"{path}[{n}]"
        st = _mapping(p, raw)
        _no_unknown(p, st, TreeScene.PATH_FIELDS, "a tree path")
        for key in ("at", "nodes"):
            if key not in st:
                raise SpecError(f"{p}: needs {key}")
        item: dict[str, Any] = {"at": _number(f"{p}.at", st["at"], 0, MAX_CUE_SECONDS)}
        if out and item["at"] < out[-1]["at"]:
            raise SpecError(f"{p}.at must not be earlier than the previous path ({_fmt(out[-1]['at'])})")
        ids: list[str] = []
        for j, x in enumerate(_list(f"{p}.nodes", st["nodes"], 1, MAX_TREE_NODES, "MAX_TREE_NODES")):
            ref = _node_ref(f"{p}.nodes[{j}]", x, set(parent_of))
            if ids and parent_of[ref] != ids[-1] and parent_of[ids[-1]] != ref:
                raise SpecError(f"{p}.nodes[{j}]: {ref!r} is not next to {ids[-1]!r} (a path moves between a parent and its child)")
            ids.append(ref)
        item["nodes"] = ids
        if "text" in st:
            item["text"] = _text(f"{p}.text", st["text"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
        out.append(item)
    return out


def _axis(path: str, v: Any, sc: dict[str, Any], env: _Env) -> dict[str, Any]:
    axis = _mapping(path, v)
    _no_unknown(path, axis, TimelineScene.AXIS_FIELDS, "a timeline axis")
    for key in ("from", "to"):
        if key not in axis:
            raise SpecError(f"{path}: needs {key}")
    lo = _number(f"{path}.from", axis["from"], -MAX_AXIS_VALUE, MAX_AXIS_VALUE)
    hi = _number(f"{path}.to", axis["to"], -MAX_AXIS_VALUE, MAX_AXIS_VALUE)
    if not lo < hi:
        raise SpecError(f"{path}.from must be less than {path}.to")
    out: dict[str, Any] = {"from": lo, "to": hi}
    if "unit" in axis:
        out["unit"] = _text(f"{path}.unit", axis["unit"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
    if "ticks" in axis:
        ticks = _number(f"{path}.ticks", axis["ticks"], 0, MAX_AXIS_VALUE * 2)
        if ticks <= 0:
            raise SpecError(f"{path}.ticks must be a number greater than 0 (the spacing between tick labels)")
        if (hi - lo) / ticks > MAX_AXIS_TICKS:
            raise SpecError(f"{path}.ticks {_fmt(ticks)} would draw more than {MAX_AXIS_TICKS} ticks (MAX_AXIS_TICKS); "
                            "use a larger spacing")
        out["ticks"] = ticks
    return out


def _on_axis(path: str, v: Any, sc: dict[str, Any]) -> float:
    return _number(path, v, sc["axis"]["from"], sc["axis"]["to"])   # `axis` is checked first


def _timeline_events(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(_list(path, v, 1, MAX_TIMELINE_EVENTS, "MAX_TIMELINE_EVENTS")):
        p = f"{path}[{n}]"
        ev = _mapping(p, raw)
        _no_unknown(p, ev, TimelineScene.EVENT_FIELDS, "a timeline event")
        if "x" not in ev:
            raise SpecError(f"{p}: needs x")
        item: dict[str, Any] = {"x": _on_axis(f"{p}.x", ev["x"], sc)}
        if "at" in ev:
            item["at"] = _number(f"{p}.at", ev["at"], 0, MAX_CUE_SECONDS)
        if "label" in ev:
            item["label"] = _text(f"{p}.label", ev["label"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
        if "color" in ev:
            item["color"] = _choice(f"{p}.color", ev["color"], COLOR_NAMES)
        out.append(item)
    return out


def _timeline_window(path: str, v: Any, sc: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(_list(path, v, 0, MAX_TIMELINE_WINDOWS, "MAX_STEPS")):
        p = f"{path}[{n}]"
        st = _mapping(p, raw)
        _no_unknown(p, st, TimelineScene.WINDOW_FIELDS, "a timeline window step")
        for key in ("at", "range"):
            if key not in st:
                raise SpecError(f"{p}: needs {key}")
        item: dict[str, Any] = {"at": _number(f"{p}.at", st["at"], 0, MAX_CUE_SECONDS)}
        if out and item["at"] < out[-1]["at"]:
            raise SpecError(f"{p}.at must not be earlier than the previous step ({_fmt(out[-1]['at'])})")
        r = st["range"]
        if not isinstance(r, list) or len(r) != 2:
            raise SpecError(f"{p}.range must be [low, high] values on the axis")
        lo, hi = (_on_axis(f"{p}.range[{j}]", x, sc) for j, x in enumerate(r))
        if not lo < hi:
            raise SpecError(f"{p}.range must be [low, high] with low < high")
        item["range"] = [lo, hi]
        if "text" in st:
            item["text"] = _text(f"{p}.text", st["text"], MAX_CELL_CHARS, "MAX_CELL_CHARS")
        out.append(item)
    return out


# One checker per scene field (common and per type). Each returns the normalised value.
FIELD_CHECKS: dict[str, Checker] = {
    "type": _scene_type,
    "seconds": _seconds,
    "caption": _text_field,
    "narration": _narration,
    "narration_audio": _narration_audio,
    "narration_words": _narration_words,
    "subtitles": _bool_field,
    "keywords": _keywords,
    "step": _step_label,
    "energy": _energy,
    "question": _question,
    "answers": _answers,
    "prompt": _prompt,
    "transition": _transition,
    "stagger": _stagger,
    "count": _count,
    "title": _text_field,
    "eyebrow": _text_field,
    "kicker": _kicker,
    "subtitle": _text_field,
    "url": _text_field,
    "rule": _bool_field,
    "text": _text_field,
    "source": _source,
    "fit": lambda path, v, sc, env: _choice(path, v, ("card", "cover")),
    "zoom": _zoom,
    "focus": _point,
    "focus_to": _point,
    "viewport": _viewport,
    "scale": lambda path, v, sc, env: _number(path, v, 1, 3),
    "click": _click,
    "scroll_y": lambda path, v, sc, env: int(_number(path, v, 0, 100000)),
    "wait_ms": lambda path, v, sc, env: int(_number(path, v, 0, 30000)),
    "full_page": _bool_field,
    "items": _items,
    "value": _text_field,
    "label": _text_field,
    "code": _code,
    "highlight": _highlight,
    "nodes": _nodes,
    "edges": _edges,
    "flash": _flash,
    "node_size": lambda path, v, sc, env: _number(path, v, *NODE_SIZE),
    "columns": _columns,
    "rows": _rows,
    "reveal": _reveal,
    "scan": _scan,
    "counter": _counter,
    "steps": _steps,
    "shape": _meter_shape,
    "capacity": _meter_capacity,
    "start": _meter_start,
    "unit": _meter_label,
    "in_label": _meter_label,
    "out_label": _meter_label,
    "path": _tree_path,
    "axis": _axis,
    "events": _timeline_events,
    "window": _timeline_window,
    "cells": _grid_cells,
    "seed": _grid_seed,
}
# Fields whose meaning depends on the scene type get their own checker; it wins over FIELD_CHECKS.
TYPE_CHECKS: dict[str, dict[str, Checker]] = {
    "table": {"highlight": _table_highlight},
    "array": {"items": _cells},
    "meter": {"steps": _meter_steps},
    "tree": {"nodes": _tree_nodes},
    "timeline": {"count": _bool_field},
    "grid": {"steps": _grid_steps},
}
# Fields whose checker reads another field of the same scene, so that one is checked first.
_CHECKED_FIRST = ("code", "nodes")
_CHECKED_FIRST_BY_TYPE = {"table": ("columns", "rows"), "array": ("items", "stagger"),
                           "meter": ("shape", "capacity", "start"), "timeline": ("axis",),
                           "grid": ("cells", "seconds", "seed")}


def _scene(i: int, raw: Any, env: _Env) -> dict[str, Any]:
    path = f"scenes[{i}]"
    if not isinstance(raw, dict) or "type" not in raw:
        raise SpecError(f"{path}: needs a 'type'")
    t = _scene_type(f"{path}.type", raw["type"], raw, env)
    cls = SCENE_TYPES[t]
    _no_unknown(path, raw, cls.FIELDS | Scene.COMMON_FIELDS, f"a {t} scene")
    missing = [k for k in REQUIRED[t] if k not in raw]
    if missing:
        raise SpecError(f"{path} ({t}): missing {', '.join(missing)}")
    sc = dict(raw)
    if "transition" in sc and sc["transition"] is None:  # `transition: null` on a scene is the same as not set
        del sc["transition"]
    sc.setdefault("seconds", DEFAULT_SECONDS[t])
    first = _CHECKED_FIRST + _CHECKED_FIRST_BY_TYPE.get(t, ())
    for k in sorted(sc, key=lambda k: first.index(k) if k in first else len(first)):
        if k != "type":
            check = TYPE_CHECKS.get(t, {}).get(k) or FIELD_CHECKS[k]
            sc[k] = check(f"{path}.{k}", sc[k], sc, env)
    if t == "cta":
        sc.setdefault("answers", [])
        sc.setdefault("prompt", DEFAULT_CTA_PROMPT)
    if t == "shot":
        sc.setdefault("zoom", [1.0, 1.0])
        sc.setdefault("click", [])
        vw, vh = sc.get("viewport", shots.DEFAULT_VIEWPORT)
        problem = shots.capture_size_problem(vw, vh, sc.get("scale", shots.DEFAULT_SCALE))
        if problem:
            raise SpecError(f"{path}.viewport: {problem}")
    return sc


# --- top-level keys -------------------------------------------------------------------------------------

def _limits(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    try:
        limits = clamp_limits(v)
    except ValueError as e:
        raise SpecError(str(e)) from e
    env.max_seconds, env.max_scenes = limits["max_seconds"], limits["max_scenes"]
    env.capped = frozenset(k for k in HARD_LIMITS if v.get(k, 0) > HARD_LIMITS[k])
    return limits


def _theme_name(what: str, name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or not name.isprintable() or len(name) > MAX_THEME_NAME:
        raise SpecError(f"{what} must be printable text of 1 to {MAX_THEME_NAME} characters, got {describe_value(name)}")
    return name


def _themes(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    if v is None:  # like voiceover and watermark: nothing set
        return {}
    themes = _mapping(path, v)
    if len(themes) > MAX_THEMES:
        raise SpecError(f"{path} has {len(themes)} entries; the limit is {MAX_THEMES} (MAX_THEMES)")
    for name, d in themes.items():
        _theme_name(f"{path}: theme name", name)
        if isinstance(d, dict):
            for key in d:
                if not isinstance(key, str):
                    raise SpecError(f"{path}.{name}: keys must be text, got {describe_key(key)}")
            if "extends" in d:
                _theme_name(f"{path}.{name}.extends", d["extends"])
        try:
            get_theme(name, themes)  # validates the entry (and everything it extends)
        except ValueError as e:
            raise SpecError(str(e)) from e
    return themes


def _theme(path: str, v: Any, spec: dict[str, Any], env: _Env) -> str:
    _theme_name("theme", v)
    try:
        spec["_theme_obj"] = get_theme(v, spec.get("themes") or {})
    except ValueError as e:
        raise SpecError(str(e)) from e
    return v


def _formats(path: str, v: Any, spec: dict[str, Any], env: _Env) -> list[str]:
    fmts = _list(path, v, 1, MAX_FORMATS)
    for n, f in enumerate(fmts):
        _choice(f"{path}[{n}]", f, FORMATS)
        if f in fmts[:n]:
            raise SpecError(f"{path}[{n}]: duplicate format {f!r}")
    return list(fmts)


def _fps(path: str, v: Any, spec: dict[str, Any], env: _Env) -> int:
    return _number(path, v, 1, 60, integer=True)


def _output_dir(path: str, v: Any, spec: dict[str, Any], env: _Env) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f"{path} must be a non-empty folder path")
    return v


MUSIC_KEYS = ("style", "bpm", "root", "scale", "seed", "volume", "snap")
STYLE_KEYS = ("bpm", "root", "scale")   # filled from the style when the spec leaves them out


def _music(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    if isinstance(v, str):  # a caller that skipped validate(): the shorthand was normalised per layer otherwise
        v = {"style": v}
    music = dict(_mapping(path, v)) if isinstance(v, dict) else None
    if music is None:
        raise SpecError(f"{path} must be a mapping or a string")
    # `_filled` is ours: it lists the keys taken from the style's defaults, and survives a second validation
    filled = music.pop("_filled", [])
    if not isinstance(filled, list) or any(k not in STYLE_KEYS for k in filled):
        raise SpecError(f"{path}._filled is set by yamlreel and cannot be written in a spec")
    from_config = music.pop("_config", None)   # set by validate(): keys a config (not the spec) wrote, and its style
    if from_config is not None and not (
            isinstance(from_config, dict) and set(from_config) == {"style", "keys"} and isinstance(from_config["style"], str)
            and isinstance(from_config["keys"], list) and all(k in STYLE_KEYS for k in from_config["keys"])):
        raise SpecError(f"{path}._config is set by yamlreel and cannot be written in a spec")
    _no_unknown(path, music, MUSIC_KEYS, "music")
    style = music.get("style")
    if not isinstance(style, str) or (style not in MUSIC_STYLES and not Path(style).suffix):
        raise SpecError(f"{path}.style must be one of {', '.join(sorted(MUSIC_STYLES))} or a path to an audio file")
    for key, lo, hi, integer in (("bpm", 30, 240, False), ("volume", 0, 2, False), ("seed", 0, 2**31, True)):
        if key in music:
            music[key] = _number(f"{path}.{key}", music[key], lo, hi, integer)
    if "root" in music:
        music["root"] = _choice(f"{path}.root", music["root"], NOTE_OFFSETS)
    if "scale" in music:
        music["scale"] = _choice(f"{path}.scale", music["scale"], SCALES)
    if "snap" in music:
        music["snap"] = _bool(f"{path}.snap", music["snap"])
    defaults = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["koto"])   # none and audio files: koto's, so snap has a bpm
    for key in STYLE_KEYS:
        if key not in music:
            music[key] = defaults[key]
            filled.append(key)
    music["_filled"] = sorted(set(filled), key=STYLE_KEYS.index)
    if from_config and from_config["keys"]:
        music["_config"] = from_config
    return music


def _config_style_keys(base: dict[str, Any], raw: dict[str, Any], merged: dict[str, Any]) -> None:
    """bpm/root/scale count as written by the author only when the spec's own `music` has them. A config's values
    (the 0.2.x starter config wrote koto's) apply only under the style that config names; under any other style
    they are dropped, so the style's own defaults land in `_filled`. A matching one is remembered in `_config`,
    so `--music <other style>` can drop it later (music.with_style)."""
    music, cfg_music, own = merged.get("music"), base.get("music"), raw.get("music")
    if not (isinstance(music, dict) and isinstance(cfg_music, dict)) or "_config" in music:
        return
    own = own if isinstance(own, dict) else {}
    keys = [k for k in STYLE_KEYS if k in cfg_music and k not in own]
    if not keys:
        return
    cfg_style = cfg_music.get("style", DEFAULTS["music"]["style"])
    if music.get("style") == cfg_style:
        music["_config"] = {"style": cfg_style, "keys": keys}
        return
    for k in keys:
        music.pop(k, None)


def _meta(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    meta = dict(_mapping(path, v))
    _no_unknown(path, meta, ("title", "description", "tags", "links", "credits"), "meta")
    for k in ("title", "description"):
        if k in meta:
            meta[k] = _text(f"{path}.{k}", meta[k])
    for k in ("tags", "links", "credits"):
        entries = _list(f"{path}.{k}", meta.get(k, []), 0, MAX_META_ENTRIES, "MAX_META_ENTRIES")
        meta[k] = [_text(f"{path}.{k}[{n}]", x) for n, x in enumerate(entries)]
    return meta


def _poster(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any] | None:
    if v is None:
        return None
    poster = dict(_mapping(path, v))
    _no_unknown(path, poster, ("scene", "at"), "poster")
    if "scene" in poster:
        last = len(spec["scenes"]) - 1
        poster["scene"] = _number(f"{path}.scene", poster["scene"], 0, last, integer=True)
    if "at" in poster:
        poster["at"] = _number(f"{path}.at", poster["at"], 0, env.max_seconds)
    return poster


def _watermark(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any] | None:
    """Accepts a string (the text) or {text, image, position, opacity, size}; None disables it."""
    if v is None or v is False:
        return None
    if isinstance(v, str):
        wm: dict[str, Any] = {"text": v}
    elif isinstance(v, dict):
        wm = dict(v)
    else:
        raise SpecError(f"{path} must be a string or a mapping")
    _no_unknown(path, wm, ("text", "image", "position", "opacity", "size"), "watermark")
    if not wm.get("text") and not wm.get("image"):
        raise SpecError(f"{path} needs text or image")
    if "text" in wm:
        wm["text"] = _text(f"{path}.text", wm["text"])
    if "image" in wm:
        if not isinstance(wm["image"], str):
            raise SpecError(f"{path}.image must be a path to an image file")
        wm["image"] = _text(f"{path}.image", wm["image"])
    if wm.get("position") is not None:
        _choice(f"{path}.position", wm["position"], sorted(WATERMARK_POSITIONS))
    op = wm.get("opacity", 0.75)
    if not _is_number(op) or not 0 < op <= 1:
        raise SpecError(f"{path}.opacity must be a number above 0 and at most 1")
    wm["opacity"] = float(op)
    wm["size"] = float(_number(f"{path}.size", wm.get("size", 30), 8, 200))
    return wm


def _voiceover(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    v = v or {}
    vo = dict(_mapping(path, v))
    _no_unknown(path, vo, ("voice", "language", "gap", "music_level", "read_screen", "duck", "speed", "trim"), "voiceover")
    for k in ("voice", "language"):
        if k in vo and (not isinstance(vo[k], str) or not vo[k].strip() or len(vo[k]) > 200):
            raise SpecError(f"{path}.{k} must be a short string")
    for k in ("read_screen", "duck", "trim"):
        if k in vo:
            _bool(f"{path}.{k}", vo[k])
    if "speed" in vo:
        vo["speed"] = _number(f"{path}.speed", vo["speed"], MIN_SPEED, MAX_SPEED)
    for k, hi in (("gap", 10), ("music_level", 1)):
        if k in vo:
            vo[k] = _number(f"{path}.{k}", vo[k], 0, hi)
    return vo


SUBTITLE_KEYS = tuple(DEFAULT_SUBTITLES)


def _subtitles_top(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any] | bool:
    """`true` (all defaults), `false`, or a mapping. Returns False or the full settings."""
    if v is None or v is False:
        return False
    sub = {} if v is True else dict(_mapping(path, v))
    _no_unknown(path, sub, SUBTITLE_KEYS, "subtitles")
    out: dict[str, Any] = dict(DEFAULT_SUBTITLES)
    out["keywords"] = []
    if "style" in sub:
        out["style"] = _choice(f"{path}.style", sub["style"], STYLES)
    if "words" in sub:
        out["words"] = _number(f"{path}.words", sub["words"], 1, 8, integer=True)
    if "position" in sub:
        out["position"] = _choice(f"{path}.position", sub["position"], POSITIONS)
    if "size" in sub:
        out["size"] = _number(f"{path}.size", sub["size"], 0.6, 1.6)
    for k in ("uppercase", "numbers", "srt"):
        if k in sub:
            out[k] = _bool(f"{path}.{k}", sub[k])
    if "keywords" in sub:
        out["keywords"] = _keywords(f"{path}.keywords", sub["keywords"], spec, env)
    return out


def _scenes(path: str, v: Any, spec: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    if not isinstance(v, list) or not v:
        raise SpecError("spec needs a non-empty 'scenes' list")
    if len(v) > env.max_scenes:
        raise SpecError(f"{len(v)} scenes; the limit is {env.max_scenes} ({env.note('max_scenes')})")
    out = [_scene(i, sc, env) for i, sc in enumerate(v)]
    labels = {sc["step"] for sc in out if sc.get("step")}
    if len(labels) > MAX_STEP_LABELS:
        raise SpecError(f"{len(labels)} distinct step labels; the limit is {MAX_STEP_LABELS} (MAX_STEP_LABELS)")
    total = sum(sc["seconds"] for sc in out)
    if total > env.max_seconds:
        raise SpecError(f"total length {total:.0f}s; the limit is {_fmt(env.max_seconds)}s ({env.note('max_seconds')})")
    check_frame_budget(total, spec["fps"], len(spec["formats"]))
    return out


# One checker per top-level key. The order matters: later checks read earlier results.
TOP_CHECKS: dict[str, Checker] = {
    "limits": _limits,
    "themes": _themes,
    "theme": _theme,
    "formats": _formats,
    "fps": _fps,
    "output_dir": _output_dir,
    "music": _music,
    "watermark": _watermark,
    "meta": _meta,
    "voiceover": _voiceover,
    "transition": _transition_top,
    "subtitles": _subtitles_top,
    "scenes": _scenes,
    "poster": _poster,
}
_FORBIDDEN_IN_SPEC = {"allow_paths", "allow_private_hosts", "chromium_path", "font_dir", "voice_server"}


def validate(raw: Any, config: Config | None = None, base_dir: Path | None = None) -> dict[str, Any]:
    cfg = config or Config()
    if not isinstance(raw, dict):
        raise SpecError("spec must be a mapping")
    forbidden = _FORBIDDEN_IN_SPEC & set(raw)
    if forbidden:
        raise SpecError(f"{', '.join(sorted(forbidden))}: not settable from a spec (user config or CLI only)")
    _no_unknown("", raw, SHARED_KEYS | {"scenes"}, "a spec")
    raw = dict(raw)
    normalize_music(raw)
    if base_dir is not None:
        _absolutize(raw, base_dir)  # paths in a spec are relative to the spec
    base = dict(cfg.data)
    for scope in (base, raw):  # `transition: slide` is {type: slide} before the merge, so a later {seconds: ...} adds to it
        if isinstance(scope.get("transition"), str):
            scope["transition"] = {"type": scope["transition"]}
        elif "transition" in scope and scope["transition"] is None:  # `transition: null` is the same as not set
            del scope["transition"]
    if raw.get("subtitles") is True and isinstance(base.get("subtitles"), dict):
        del raw["subtitles"]  # `subtitles: true` means "on", so a config that already sets them keeps its settings
    spec: dict[str, Any] = merge(base, raw)
    _config_style_keys(base, raw, spec)
    for key, default in DEFAULTS.items():
        spec.setdefault(key, copy.deepcopy(default))
    spec.setdefault("scenes", None)  # reported by the scenes check
    spec.setdefault("voiceover", {})
    spec.setdefault("watermark", None)
    env = _Env()
    for key, check in TOP_CHECKS.items():
        if key in spec:
            spec[key] = check(key, spec[key], spec, env)
    return spec
