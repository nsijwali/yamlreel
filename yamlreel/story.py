"""`yamlreel story`: turn a short outline into a plain, valid spec with the arc that holds attention.

hook -> problem -> one scene per step (with a `step` badge) -> when to use -> end card with a question.

The generator is offline and deterministic. It writes an ordinary spec (no `story:` key is read at render time), so
every scene can be edited by hand afterwards; the placeholders it cannot know (a table's cells, a diagram's nodes)
are marked TODO and listed in a comment at the top of the file. The result is checked with the normal validator
before it is returned.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .datascenes import ARRAY_ACTIONS, MAX_CELL_CHARS
from .scenes import SCENE_TYPES
from .specs import DEFAULT_SECONDS, MAX_SPEC_BYTES, SpecError, validate
from .theme import get_theme

MIN_STEPS = 1
MAX_STEPS = 6
MAX_WHEN = 6
SECONDS_PER_WORD = 1 / 2.5     # a narrated scene is at least words / 2.5 + 1 seconds
PADDING_SECONDS = 1.0
MAX_TOPIC = 100                # the video title (YouTube allows 100)
MAX_LINE = 300                 # the hook, the problem, a step's `say`
MAX_NAME = 40                  # a step's `name`, which becomes its badge label
MAX_WHEN_ENTRY = 120
MAX_QUESTION = 200
MAX_ANSWER = 60
MAX_ANSWERS = 4
MAX_EVENTS = 8                 # timed cues in one step
MAX_EVENT_AT = 60              # seconds from the scene's start
EVENT_HOLD = 1.5               # seconds the scene lasts after its last event
EVENT_SHOWS = ("diagram", "meter", "array")
METER_LEVELS = (4, 10)         # the placeholder's drain and refill, which events alternate between
TODO = "TODO"

OUTLINE_KEYS = ("topic", "hook", "problem", "steps", "when_to_use", "question", "answers")
STEP_KEYS = ("name", "say", "show", "events")
EVENT_KEYS = ("at", "text")     # the placeholder form: the generator invents the cue
# The pass-through form: `at`, exactly one action key, and the optional fields of the scene's own step.
EVENT_ACTIONS: dict[str, tuple[str, ...]] = {"array": ARRAY_ACTIONS, "meter": ("level",), "diagram": ("flash",)}
EVENT_EXTRAS: dict[str, tuple[str, ...]] = {"array": ("text", "color"), "meter": ("seconds", "text", "color"),
                                            "diagram": ("text", "color")}
DEFAULT_SHOW = "bullets"


class StoryError(ValueError):
    pass


# Required fields of each scene type a step can `show`, filled with obvious placeholders. The step's name is
# added as the scene's `title` when the type has one. `shot` is left out on purpose: a placeholder would need a
# real URL or image. This table grows with the scene types.
PLACEHOLDERS: dict[str, dict[str, Any]] = {
    "title": {},
    "type": {"text": f"{TODO}: the line to type"},
    "bullets": {"items": [f"{TODO}: first point", f"{TODO}: second point", f"{TODO}: third point"]},
    "code": {"code": f"# {TODO}: paste the code here"},
    "stat": {"value": TODO},
    "diagram": {"nodes": [{"id": "a", "label": TODO, "x": 0.25, "y": 0.5},
                          {"id": "b", "label": TODO, "x": 0.75, "y": 0.5, "at": 0.8}],
                "edges": [{"from": "a", "to": "b", "at": 1.2}]},
    "table": {"columns": [f"{TODO} 1", f"{TODO} 2", f"{TODO} 3"], "rows": [[TODO, TODO, TODO] for _ in range(3)]},
    "array": {"items": [TODO, TODO, TODO]},
    "meter": {"capacity": 10, "start": 10, "steps": [{"at": 1.0, "level": 4}, {"at": 3.0, "level": 10}]},
    "tree": {"nodes": [{"id": "root", "label": TODO}, {"id": "a", "label": TODO, "parent": "root"},
                       {"id": "b", "label": TODO, "parent": "root"}]},
    "timeline": {"axis": {"from": 0, "to": 10, "ticks": 5}, "events": [{"x": 2, "at": 0.6}, {"x": 7, "at": 1.2}]},
    "grid": {"cells": ["#.#", ".#.", "#.#"]},
}


# --- the outline -----------------------------------------------------------------------------------------

def _text(path: str, v: Any, limit: int, required: bool = True) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise StoryError(f"{path} must be text")
    out = " ".join(str(v).split())
    if required and not out:
        raise StoryError(f"{path} must not be empty")
    if len(out) > limit:
        raise StoryError(f"{path} is {len(out)} characters; the limit is {limit}")
    return out


def _mapping(path: str, v: Any, allowed: tuple[str, ...], what: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise StoryError(f"{path or 'the outline'} must be a mapping")
    for key in v:
        if key not in allowed:
            raise StoryError(f"{path + '.' if path else ''}{key}: unknown field for {what}; allowed: "
                             f"{', '.join(sorted(allowed))}")
    return v


def _event_keys(show: str) -> tuple[str, ...]:
    return ("at", *EVENT_ACTIONS[show], *EVENT_EXTRAS[show])


def _events(path: str, raw: Any, show: str = "meter") -> list[dict[str, Any]]:
    """A step's `events`, or [] when there are none (`events: []` is the same as no events).

    Each event is `{at, text}` (the generator invents the cue) or `{at, <one action key>, ...}` for the scene it
    belongs to, which is passed through to the scene unchanged and checked by the scene validator."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise StoryError(f"{path} must be a list")
    if len(raw) > MAX_EVENTS:
        raise StoryError(f"{path} has {len(raw)} entries; the limit is {MAX_EVENTS}")
    actions = EVENT_ACTIONS.get(show, ())
    out: list[dict[str, Any]] = []
    for n, ev in enumerate(raw):
        where = f"{path}[{n}]"
        if not isinstance(ev, dict):
            raise StoryError(f"{where} must be a mapping")
        given = [a for a in actions if a in ev]
        allowed = _event_keys(show) if actions else EVENT_KEYS
        ev = _mapping(where, ev, allowed, f"{'an' if show == 'array' else 'a'} {show} event" if actions else "an event")
        if len(given) > 1:
            raise StoryError(f"{where}: an event takes at most one of {', '.join(actions)}, found {' and '.join(given)}")
        if not given and any(k not in EVENT_KEYS for k in ev):
            extra = next(k for k in ev if k not in EVENT_KEYS)
            raise StoryError(f"{where}.{extra}: needs one of {', '.join(actions)} (or only at and text); "
                             f"allowed: {', '.join(sorted(allowed))}")
        if "at" not in ev:
            raise StoryError(f"{where} needs 'at'")
        at = ev["at"]
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at <= MAX_EVENT_AT:
            raise StoryError(f"{where}.at must be a number from 0 to {MAX_EVENT_AT}")
        if out and at < out[-1]["at"]:
            raise StoryError(f"{where}.at must not be earlier than the event before it")
        if not given:
            if "text" not in ev:
                raise StoryError(f"{where} needs 'text'" + (f", or one of {', '.join(actions)}" if actions else ""))
            out.append({"at": at, "text": _text(f"{where}.text", ev["text"], MAX_CELL_CHARS)})
            continue
        if show == "diagram":
            appears = {n_["id"]: n_.get("at", 0) for n_ in PLACEHOLDERS["diagram"]["nodes"]}
            if not isinstance(ev["flash"], str) or ev["flash"] not in appears:
                raise StoryError(f"{where}.flash: unknown node {ev['flash']!r}; the generated diagram has: "
                                 f"{', '.join(appears)}")
            if at < appears[ev["flash"]]:
                raise StoryError(f"{where}.at: node {ev['flash']!r} of the generated diagram appears at "
                                 f"{appears[ev['flash']]}s, so it cannot flash at {at}s")
            if "text" not in ev:
                raise StoryError(f"{where} needs 'text'")
            _text(f"{where}.text", ev["text"], MAX_CELL_CHARS)
        out.append(dict(ev))
    if actions and out and 0 < sum(1 for e in out if any(a in e for a in actions)) < len(out):
        raise StoryError(f"{path}: use one form per step, either {{at, text}} events or events with an action key "
                         f"({', '.join(actions)}), not both")
    return out


def parse_outline(raw: Any) -> dict[str, Any]:
    """The checked outline: every key present, text normalised. Unknown keys and out-of-range values are errors."""
    outline = _mapping("", raw, OUTLINE_KEYS, "an outline")
    for key in ("topic", "hook", "steps"):
        if key not in outline:
            raise StoryError(f"outline needs '{key}'")
    out: dict[str, Any] = {"topic": _text("topic", outline["topic"], MAX_TOPIC),
                           "hook": _text("hook", outline["hook"], MAX_LINE),
                           "problem": (_text("problem", outline["problem"], MAX_LINE)
                                       if outline.get("problem") is not None else ""),
                           "question": (_text("question", outline["question"], MAX_QUESTION)
                                        if outline.get("question") is not None else "")}
    steps = outline["steps"]
    if not isinstance(steps, list):
        raise StoryError("steps must be a list")
    if not MIN_STEPS <= len(steps) <= MAX_STEPS:
        raise StoryError(f"steps has {len(steps)} entries; it needs {MIN_STEPS} to {MAX_STEPS}")
    out["steps"] = []
    for n, st in enumerate(steps):
        path = f"steps[{n}]"
        st = _mapping(path, st, STEP_KEYS, "a step")
        if "name" not in st:
            raise StoryError(f"{path} needs a 'name'")
        show = st.get("show", DEFAULT_SHOW)
        if not isinstance(show, str) or show not in PLACEHOLDERS:
            raise StoryError(f"{path}.show must be one of {', '.join(PLACEHOLDERS)}")
        events = _events(f"{path}.events", st.get("events"), show if isinstance(show, str) else "")
        if events and show not in EVENT_SHOWS:
            raise StoryError(f"{path}.events: events work with show: diagram, meter or array; this step shows {show}")
        out["steps"].append({"name": _text(f"{path}.name", st["name"], MAX_NAME),
                             "say": _text(f"{path}.say", st["say"], MAX_LINE) if st.get("say") is not None else "",
                             "show": show, "events": events})
    names = [s["name"] for s in out["steps"]]
    if len(set(names)) != len(names):
        raise StoryError("steps: names must be different, because each one is a badge label")
    when = outline.get("when_to_use") or []
    if not isinstance(when, list):
        raise StoryError("when_to_use must be a list")
    if len(when) > MAX_WHEN:
        raise StoryError(f"when_to_use has {len(when)} entries; the limit is {MAX_WHEN}")
    out["when_to_use"] = [_text(f"when_to_use[{n}]", w, MAX_WHEN_ENTRY) for n, w in enumerate(when)]
    answers = outline.get("answers") or []
    if not isinstance(answers, list):
        raise StoryError("answers must be a list")
    if len(answers) > MAX_ANSWERS:
        raise StoryError(f"answers has {len(answers)} entries; the limit is {MAX_ANSWERS}")
    out["answers"] = [_text(f"answers[{n}]", a, MAX_ANSWER) for n, a in enumerate(answers)]
    if out["answers"] and not out["question"]:
        raise StoryError("answers needs a question")
    return out


def load_outline(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        too_big = p.stat().st_size > MAX_SPEC_BYTES
        text = "" if too_big else p.read_text(encoding="utf-8")
    except OSError as e:
        raise StoryError(f"cannot read outline {p}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise StoryError(f"cannot read outline {p}: not valid UTF-8 ({e.reason})") from e
    if too_big:
        raise StoryError(f"outline {p} is larger than {MAX_SPEC_BYTES} bytes")
    try:
        raw = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as e:
        raise StoryError(f"cannot parse outline {p}: {' '.join(str(e).split())[:200]}") from e
    return parse_outline(raw)


# --- the spec --------------------------------------------------------------------------------------------

def narrated_seconds(kind: str, text: str) -> float:
    """max(the scene type's default, words / 2.5 + 1): narration stretches a scene anyway, this is where it starts."""
    words = len(text.split())
    return round(max(DEFAULT_SECONDS[kind], words * SECONDS_PER_WORD + PADDING_SECONDS), 1)


def _sentence(text: str) -> str:
    return text if not text or text[-1] in ".!?:;" else text + "."


def _supports() -> tuple[bool, bool]:
    """(scene `energy`, voiceover `duck`), as far as the validator in this install accepts them. The story is
    written with them when they exist, and without them when they do not, so it always validates."""
    def ok(raw: dict[str, Any]) -> bool:
        try:
            validate({"scenes": [{"type": "title", "title": "x", **raw.get("scene", {})}], **raw.get("top", {})}, Config())
            return True
        except SpecError:
            return False
    return ok({"scene": {"energy": "mid"}}), ok({"top": {"voiceover": {"duck": True}}})


def _apply_events(sc: dict[str, Any], kind: str, events: list[dict[str, Any]]) -> None:
    """Turn a step's events into the cues of its scene (diagram flash, meter steps, array steps)."""
    cues: list[dict[str, Any]] = []
    for n, ev in enumerate(events):
        if any(a in ev for a in EVENT_ACTIONS[kind]):      # pass-through: the author's own step fields
            cues.append(dict(ev))
        elif kind == "diagram":
            cues.append({"node": "a", **ev})
        elif kind == "meter":
            cues.append({"at": ev["at"], "level": METER_LEVELS[n % 2], "text": ev["text"]})
        else:
            cues.append({"at": ev["at"], "mark": [n % 3], "text": ev["text"]})
    if kind == "diagram":
        sc["flash"] = [{"node": c.pop("flash"), **c} if "flash" in c else c for c in cues]
    else:
        sc["steps"] = cues


def _step_scene(step: dict[str, Any]) -> dict[str, Any]:
    kind = step["show"]
    sc: dict[str, Any] = {"type": kind}
    cls = SCENE_TYPES[kind]
    fields = {k: yaml.safe_load(yaml.safe_dump(v)) for k, v in PLACEHOLDERS[kind].items()}   # a fresh copy
    if "title" in cls.FIELDS:
        sc["title"] = step["name"]
    elif kind == "stat":
        sc["label"] = step["name"]
    sc.update(fields)
    events = step.get("events") or []
    if events:
        _apply_events(sc, kind, events)
    sc["step"] = step["name"]
    if step["say"]:
        sc["narration"] = step["say"]
    sc["seconds"] = narrated_seconds(kind, step["say"])
    if events:   # beat snapping can shorten a scene, so keep the last cue on screen for a moment
        # a meter ramp keeps draining for its own `seconds`, and an earlier long ramp can outlast a later cue
        sc["seconds"] = max(sc["seconds"], *(e["at"] + e.get("seconds", 0) + EVENT_HOLD for e in events))
    return sc


def build_spec(outline: dict[str, Any], theme: str | None = None, fmt: str | None = None) -> dict[str, Any]:
    """The spec dict for a checked outline."""
    energy, duck = _supports()

    def with_energy(sc: dict[str, Any], level: str) -> dict[str, Any]:
        if energy:
            sc["energy"] = level
        return sc

    scenes: list[dict[str, Any]] = [with_energy(
        {"type": "type", "text": outline["hook"], "narration": outline["hook"], "transition": "cut",
         "seconds": narrated_seconds("type", outline["hook"])}, "high")]
    if outline["problem"]:
        scenes.append(with_energy(
            {"type": "title", "eyebrow": "THE PROBLEM", "title": outline["problem"], "narration": outline["problem"],
             "seconds": narrated_seconds("title", outline["problem"])}, "mid"))
    for step in outline["steps"]:
        scenes.append(with_energy(_step_scene(step), "mid"))
    if outline["when_to_use"]:
        spoken = " ".join(["When to use which."] + [_sentence(w) for w in outline["when_to_use"]])
        scenes.append(with_energy(
            {"type": "bullets", "title": "When to use", "items": list(outline["when_to_use"]), "narration": spoken,
             "seconds": narrated_seconds("bullets", spoken)}, "mid"))
    if outline["question"]:
        scenes.append(with_energy(
            {"type": "cta", "question": outline["question"], "answers": list(outline["answers"]),
             "narration": outline["question"], "seconds": narrated_seconds("cta", outline["question"])}, "low"))
    else:
        scenes.append(with_energy({"type": "title", "title": outline["topic"], "seconds": DEFAULT_SECONDS["title"]}, "low"))
    spec: dict[str, Any] = {"meta": {"title": outline["topic"]}}
    if theme:
        spec["theme"] = theme
    if fmt:
        spec["formats"] = [fmt]
    spec["subtitles"] = True
    if duck:
        spec["voiceover"] = {"duck": True}
    spec["scenes"] = scenes
    return spec


def _todo_scenes(spec: dict[str, Any]) -> list[int]:
    return [i for i, sc in enumerate(spec["scenes"]) if TODO in yaml.safe_dump(sc)]


def render_spec(spec: dict[str, Any], source_name: str) -> str:
    """The YAML text: a comment that says where it came from and what to fill in, then the spec."""
    todo = _todo_scenes(spec)
    header = [f"# Generated by `yamlreel story` from {source_name}. It is an ordinary spec: edit anything.",
              "# Order: hook, problem, one scene per step (with a STEP n/N badge), when to use, end card."]
    if todo:
        header.append("# Replace the placeholders marked TODO in "
                      + ", ".join(f"scenes[{i}] ({spec['scenes'][i]['type']})" for i in todo)
                      + ", then render with `--preview --no-voice` to check the layout.")
    body = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100, default_flow_style=False)
    return "\n".join(header) + "\n" + body


_CUE_PATH = re.compile(r"scenes\[(\d+)\]\.(?:steps|flash)\[(\d+)\]")


def _rebase_event_error(message: str, spec: dict[str, Any], outline: dict[str, Any]) -> str:
    """The scene validator's message about a step or flash cue, named by the outline path instead:
    `scenes[3].steps[1].pop: ...` becomes `steps[1].events[1].pop: ...`. "" when the message is about anything else."""
    m = _CUE_PATH.search(message)
    if not m:
        return ""
    step_scenes = [i for i, sc in enumerate(spec["scenes"]) if "step" in sc]
    scene = int(m.group(1))
    if scene not in step_scenes:
        return ""
    j = step_scenes.index(scene)
    return message[:m.start()] + f"steps[{j}].events[{m.group(2)}]" + message[m.end():]


def generate(outline: dict[str, Any], source_name: str = "an outline", theme: str | None = None,
             fmt: str | None = None) -> str:
    """The spec text for a checked outline. It is loaded back through the normal validator before it is returned,
    so a generated spec that would not render is a StoryError here, not at render time."""
    if theme:
        try:
            get_theme(theme)
        except ValueError as e:
            raise StoryError(f"--theme: {e}") from e
    spec = build_spec(outline, theme, fmt)
    text = render_spec(spec, source_name)
    try:
        validate(yaml.safe_load(text), Config())
    except SpecError as e:
        rebased = _rebase_event_error(str(e), spec, outline)
        if rebased:
            raise StoryError(rebased) from e
        raise StoryError(f"the generated spec does not validate ({e}); shorten the outline text") from e
    return text
