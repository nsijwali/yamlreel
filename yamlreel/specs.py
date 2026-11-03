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
from .music import NOTE_OFFSETS, SCALES
from .scenes import SCENE_TYPES, DiagramScene, Scene
from .textkit import MAX_COUNT_MAGNITUDE, split_count
from .theme import get_theme

FORMATS = {"landscape": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}

DEFAULT_SECONDS = {"title": 3.0, "type": 3.0, "shot": 4.0, "diagram": 7.0,
                   "bullets": 5.0, "code": 6.0, "stat": 3.5}
REQUIRED = {
    "title": ["title"],
    "type": ["text"],
    "shot": ["source"],
    "diagram": ["nodes"],
    "bullets": ["items"],
    "code": ["code"],
    "stat": ["value"],
}
MUSIC_STYLES = {"koto", "none"}
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
MAX_ID_CHARS = 100       # a node id
MAX_CUE_SECONDS = 600    # latest `at` of a node, edge or flash
MAX_META_ENTRIES = 50    # tags, links or credits
MAX_THEMES = 100         # custom themes in one spec or config
MAX_THEME_NAME = 60
MAX_FORMATS = 3
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


def _bool_field(path: str, v: Any, sc: dict[str, Any], env: _Env) -> bool:
    return _bool(path, v)


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


# One checker per scene field (common and per type). Each returns the normalised value.
FIELD_CHECKS: dict[str, Checker] = {
    "type": _scene_type,
    "seconds": _seconds,
    "caption": _text_field,
    "narration": _narration,
    "narration_audio": _narration_audio,
    "transition": _transition,
    "stagger": _stagger,
    "count": _count,
    "title": _text_field,
    "eyebrow": _text_field,
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
}
# Fields whose checker reads another field of the same scene, so that one is checked first.
_CHECKED_FIRST = ("code", "nodes")


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
    sc.setdefault("seconds", DEFAULT_SECONDS[t])
    for k in sorted(sc, key=lambda k: k not in _CHECKED_FIRST):
        if k != "type":
            sc[k] = FIELD_CHECKS[k](f"{path}.{k}", sc[k], sc, env)
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


def _music(path: str, v: Any, spec: dict[str, Any], env: _Env) -> dict[str, Any]:
    if isinstance(v, str):  # a caller that skipped validate(): the shorthand was normalised per layer otherwise
        v = {"style": v}
    music = dict(_mapping(path, v)) if isinstance(v, dict) else None
    if music is None:
        raise SpecError(f"{path} must be a mapping or a string")
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
    return music


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
    _no_unknown(path, vo, ("voice", "language", "gap", "music_level", "read_screen"), "voiceover")
    for k in ("voice", "language"):
        if k in vo and (not isinstance(vo[k], str) or not vo[k].strip() or len(vo[k]) > 200):
            raise SpecError(f"{path}.{k} must be a short string")
    if "read_screen" in vo:
        _bool(f"{path}.read_screen", vo["read_screen"])
    for k, hi in (("gap", 10), ("music_level", 1)):
        if k in vo:
            vo[k] = _number(f"{path}.{k}", vo[k], 0, hi)
    return vo


def _scenes(path: str, v: Any, spec: dict[str, Any], env: _Env) -> list[dict[str, Any]]:
    if not isinstance(v, list) or not v:
        raise SpecError("spec needs a non-empty 'scenes' list")
    if len(v) > env.max_scenes:
        raise SpecError(f"{len(v)} scenes; the limit is {env.max_scenes} ({env.note('max_scenes')})")
    out = [_scene(i, sc, env) for i, sc in enumerate(v)]
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
    spec: dict[str, Any] = merge(base, raw)
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
