"""Validation of `motion` scenes: exact messages, limits, timing rules, preset support and re-validation."""
from __future__ import annotations

import copy

import pytest

from yamlreel import anim
from yamlreel import motion_scene as motion
from yamlreel.specs import SpecError, validate

RECT = {"kind": "rect", "w": 200, "h": 100}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def scene(*elements, **extra):
    return {"type": "motion", "elements": list(elements), **extra}


def check(*elements, **extra):
    return validate({"scenes": [scene(*elements, **extra)]})["scenes"][0]


def bad(match, *elements, **extra):
    with pytest.raises(SpecError, match=match) as exc:
        check(*elements, **extra)
    assert "\n" not in str(exc.value)
    return str(exc.value)


def bad_at(index, message, element, **extra):
    """The error for `element` placed at scenes[2].elements[index]; it must start with the message."""
    filler = [dict(RECT) for _ in range(index)]
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{"type": "title", "title": "a"}, {"type": "title", "title": "b"},
                             scene(*filler, element, **extra)]})
    assert str(exc.value).startswith(message), str(exc.value)


# --- the messages of the spec, section 6.4 --------------------------------------------------------------

def test_kind_must_be_one_of_the_registered_kinds():
    bad_at(3, "scenes[2].elements[3].kind must be one of text, rect, circle, group, arrow, path, image", {"kind": "blob"})
    bad_at(0, "scenes[2].elements[0]: needs kind", {})
    assert list(motion.KINDS) == ["text", "rect", "circle", "group", "arrow", "path", "image"]


def test_unknown_field_names_the_kind_and_the_allowed_fields():
    bad_at(3, "scenes[2].elements[3].foo: unknown field for a rect element; allowed: ", {**RECT, "foo": 1})
    msg = bad("unknown field for a rect element", {**RECT, "radiuss": 4})
    assert "radius" in msg and "stroke_width" in msg and "text" not in msg.split("allowed:")[1].split(", ")[0]


def test_too_many_elements_and_too_many_drawable_leaves():
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{"type": "title", "title": "a"}, {"type": "title", "title": "b"},
                             scene(*[dict(RECT) for _ in range(61)])]})
    assert str(exc.value) == "scenes[2].elements has 61 entries; the limit is 60 (MAX_ELEMENTS)"
    words = {"kind": "text", "text": " ".join(["w"] * 34), "split": "words"}
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{"type": "title", "title": "a"}, {"type": "title", "title": "b"},
                             scene(words, words)]})
    assert str(exc.value) == ("scenes[2].elements: 68 drawable elements after groups and split words; "
                              "the limit is 60 (MAX_ELEMENTS)")
    check(*[dict(RECT) for _ in range(60)])
    assert anim.MAX_ELEMENTS == 60


def test_duplicate_ids():
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{"type": "title", "title": "a"}, {"type": "title", "title": "b"},
                             scene({**RECT, "id": "a"}, {**RECT, "id": "a"})]})
    assert str(exc.value).startswith("scenes[2].elements[1].id: duplicate element id 'a'")
    with pytest.raises(SpecError, match="duplicate element id '1'"):
        check({**RECT, "id": 1}, {**RECT, "id": "1"})    # integer ids are text


def test_end_before_the_element_has_entered():
    bad_at(3, "scenes[2].elements[3].end: 1.0s is before the element has entered (1.25s)",
           {**RECT, "at": 0.8, "enter": {"type": "fade", "seconds": 0.45}, "end": 1.0})
    check({**RECT, "at": 0.8, "enter": {"type": "fade", "seconds": 0.2}, "end": 1.0})     # entered by then
    check({**RECT, "at": 1.0, "enter": "none", "end": 1.0})                                  # `none` takes no time


def test_a_move_cannot_start_before_the_move_before_it_ends():
    bad_at(0, "scenes[2].elements[0].moves[1].at: starts at 1.2s, before the move before it ends (1.4s)",
           {**RECT, "at": 0.0, "enter": "none", "moves": [{"at": 0.5, "seconds": 0.9, "x": 0.3}, {"at": 1.2, "y": 0.3}]})
    bad_at(0, "scenes[2].elements[0].moves[0].at: starts at 0.2s, before the element has entered (0.4s)",
           {**RECT, "at": 0.0, "moves": [{"at": 0.2, "x": 0.3}]})
    check({**RECT, "at": 0.0, "moves": [{"at": 0.4, "seconds": 0.5, "x": 0.3}, {"at": 0.9, "y": 0.3}]})


def test_exit_needs_end():
    bad_at(0, "scenes[2].elements[0].exit: needs end", {**RECT, "exit": "fade-out"})


def test_split_words_cannot_be_combined_with_draw():
    bad_at(0, "scenes[2].elements[0].split: words cannot be combined with enter: draw",
           {"kind": "text", "text": "a b", "split": "words", "enter": "draw"})
    check({"kind": "text", "text": "a b", "split": "none", "enter": "draw"})


def test_flash_references_and_times():
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{"type": "title", "title": "a"}, {"type": "title", "title": "b"},
                             scene({**RECT, "id": "a"}, flash=[{"node": "x", "at": 1}])]})
    assert str(exc.value) == "scenes[2].flash[0].node: unknown element 'x'" or \
        str(exc.value).startswith("scenes[2].flash[0].node: unknown element 'x'")
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{"type": "title", "title": "a"}, {"type": "title", "title": "b"},
                             scene({**RECT, "id": "a", "at": 1.5}, flash=[{"node": "a", "at": 1.0}])]})
    assert str(exc.value) == "scenes[2].flash[0].at: element 'a' appears at 1.5s, so it cannot flash at 1.0s"
    # an element timed by the scene (no `at`) has no explicit time to compare with
    assert check({**RECT, "id": "a"}, flash=[{"node": "a", "at": 0.0, "text": "hi", "color": "accent2"}])["flash"][0]["at"] == 0.0
    bad("flash\\[0\\].color must be one of", {**RECT, "id": "a"}, flash=[{"node": "a", "color": "purple"}])
    bad("flash\\[0\\].zzz: unknown field for a motion flash", {**RECT, "id": "a"}, flash=[{"node": "a", "zzz": 1}])
    bad("flash\\[0\\]: needs node", {**RECT, "id": "a"}, flash=[{"at": 1}])
    bad("flash has 41 entries; the limit is 40 \\(MAX_FLASH\\)", {**RECT, "id": "a"}, flash=[{"node": "a"}] * 41)


# --- limits ----------------------------------------------------------------------------------------------

def test_the_limits_have_names_and_values():
    assert (anim.MAX_MOVES, anim.MAX_SPLIT_WORDS, anim.MAX_MOTION_TEXT) == (8, 40, 300)
    moves = [{"at": 0.5 + k, "seconds": 0.5, "x": 0.5} for k in range(9)]
    still = {**RECT, "at": 0.0, "enter": "none"}
    bad("elements\\[0\\].moves has 9 entries; the limit is 8 \\(MAX_MOVES\\)", {**still, "moves": moves})
    check({**still, "moves": moves[:8]})
    bad("text is 301 characters; the limit is 300 \\(MAX_MOTION_TEXT\\)", {"kind": "text", "text": "x" * 301})
    check({"kind": "text", "text": "x" * 300})
    msg = bad("the text has 41 words; the limit is 40 \\(MAX_SPLIT_WORDS\\)",
              {"kind": "text", "text": " ".join(["w"] * 41), "split": "words"})
    assert msg.startswith("scenes[0].elements[0].split:")
    check({"kind": "text", "text": " ".join(["w"] * 40), "split": "words"})


def test_the_ranges_of_the_fields():
    for field, value in (("x", 1.6), ("y", -0.6), ("at", 601), ("end", -1), ("layer", 101), ("layer", 1.5),
                         ("opacity", 1.1), ("scale", 0.01), ("scale", 9), ("rotate", 721), ("w", 0), ("w", 4001),
                         ("radius", 1001), ("stroke_width", 41)):
        with pytest.raises(SpecError, match=f"elements\\[0\\].{field} must be"):
            check({**RECT, field: value})
    for field, value in (("size", 11), ("size", 401), ("max_w", 0.05), ("max_w", 1.1), ("stagger", 3.1)):
        with pytest.raises(SpecError, match=f"elements\\[0\\].{field} must be"):
            check({"kind": "text", "text": "a", field: value})
    with pytest.raises(SpecError, match="elements\\[0\\].r must be a number between 1 and 2000"):
        check({"kind": "circle", "r": 0})
    with pytest.raises(SpecError, match="elements\\[0\\]: a rect element needs w and h"):
        check({"kind": "rect", "w": 5})
    with pytest.raises(SpecError, match="elements\\[0\\]: a circle element needs r"):
        check({"kind": "circle"})
    with pytest.raises(SpecError, match="elements\\[0\\]: a text element needs text"):
        check({"kind": "text"})
    with pytest.raises(SpecError, match="elements\\[0\\].text must not be empty"):
        check({"kind": "text", "text": "  "})
    with pytest.raises(SpecError, match="needs a fill or a stroke"):
        check({**RECT, "fill": "none", "stroke": "none"})
    with pytest.raises(SpecError, match="elements has 0 entries|elements must have at least 1 entry"):
        check()


# --- timing rules and presets ---------------------------------------------------------------------------

def test_emphasis_rules():
    with pytest.raises(SpecError, match="emphasis.at: starts at 0.5s, before the element has entered \\(0.75s\\)"):
        check({**RECT, "at": 0.35, "emphasis": {"type": "pulse", "at": 0.5}})
    with pytest.raises(SpecError, match="emphasis: ends at 4.0s, after the element ends \\(3.0s\\)"):
        check({**RECT, "at": 0.0, "end": 3.0, "emphasis": {"type": "pulse", "at": 1.0, "repeat": 5}})
    check({**RECT, "at": 0.0, "end": 3.0, "emphasis": {"type": "pulse", "at": 1.0, "repeat": 3}})
    with pytest.raises(SpecError, match="emphasis.repeat must be an integer between 1 and 10"):
        check({**RECT, "emphasis": {"type": "pulse", "repeat": 11}})
    with pytest.raises(SpecError, match="emphasis.type must be one of pulse"):
        check({**RECT, "emphasis": "wobble"})


def test_timing_uses_the_times_the_scene_gives_elements_without_an_at():
    # the second element starts at 0.35 + 0.4 = 0.75 (scene stagger), so a move at 0.5 is too early for it
    with pytest.raises(SpecError, match="elements\\[1\\].moves\\[0\\].at: starts at 0.5s, before the element has entered \\(1.15s\\)"):
        check(dict(RECT), {**RECT, "moves": [{"at": 0.5, "x": 0.3}]})
    check(dict(RECT), {**RECT, "moves": [{"at": 1.2, "x": 0.3}]})
    with pytest.raises(SpecError, match="starts at 0.5s, before the element has entered \\(1.75s\\)"):
        check(dict(RECT), {**RECT, "moves": [{"at": 0.5, "x": 0.3}]}, stagger=1.0)


def test_draw_is_supported_only_where_a_kind_lists_it():
    supporting = [k for k, entry in motion.KINDS.items() if "draw" in entry.PRESETS]
    assert supporting == ["text", "rect", "circle", "arrow", "path"]
    check({"kind": "text", "text": "a", "enter": "draw"}, {"kind": "circle", "r": 20, "enter": "draw"},
          {**RECT, "enter": "draw"})
    bad_at(0, "scenes[2].elements[0].enter: draw works with text, rect, circle, arrow and path elements",
           {"kind": "image", "source": "a.png", "w": 10, "enter": "draw"})
    bad_at(0, "scenes[2].elements[0].enter: draw works with text, rect, circle, arrow and path elements",
           {"kind": "group", "children": [dict(RECT)], "enter": {"type": "draw", "seconds": 1}})
    for kind, entry in motion.KINDS.items():
        assert anim.COMMON_PRESETS <= entry.PRESETS, kind


def test_preset_names_seconds_and_easing():
    for name in anim.ENTER_PRESETS:
        if name != "draw":
            check({**RECT, "enter": name})
    for name in anim.EXIT_PRESETS:
        check({**RECT, "end": 5, "exit": name})
    with pytest.raises(SpecError, match="enter.type must be one of none, fade, slide-up"):
        check({**RECT, "enter": "zoom"})
    with pytest.raises(SpecError, match="exit.type must be one of fade-out, slide-out, pop-out"):
        check({**RECT, "end": 5, "exit": "fade"})
    with pytest.raises(SpecError, match="enter.seconds must be a number between 0.05 and 10"):
        check({**RECT, "enter": {"type": "fade", "seconds": 0.01}})
    with pytest.raises(SpecError, match="enter.easing must be one of linear, out, in_out, back"):
        check({**RECT, "enter": {"type": "fade", "easing": "bounce"}})
    with pytest.raises(SpecError, match="elements\\[0\\].easing must be one of linear, out, in_out, back"):
        check({**RECT, "easing": "bounce"})
    with pytest.raises(SpecError, match="elements\\[0\\].enter must be one of"):
        check({**RECT, "enter": {}})
    with pytest.raises(SpecError, match="enter: needs type"):
        check({**RECT, "enter": {"seconds": 1}})


def test_moves_need_a_target_and_an_at():
    with pytest.raises(SpecError, match="moves\\[0\\]: needs at least one of x, y, vx, vy, scale, rotate, opacity"):
        check({**RECT, "moves": [{"at": 1}]})
    with pytest.raises(SpecError, match="moves\\[0\\]: needs at"):
        check({**RECT, "moves": [{"x": 0.2}]})
    with pytest.raises(SpecError, match="moves\\[0\\].q: unknown field for a move"):
        check({**RECT, "moves": [{"at": 1, "x": 0.2, "q": 1}]})
    with pytest.raises(SpecError, match="moves\\[0\\].x must be a number between -0.5 and 1.5"):
        check({**RECT, "moves": [{"at": 1, "x": 2}]})


def test_quality_choices_and_scene_fields():
    for q in ("draft", "standard", "high"):
        assert check(dict(RECT), quality=q)["quality"] == q
    with pytest.raises(SpecError, match="scenes\\[0\\].quality must be one of draft, standard, high"):
        check(dict(RECT), quality="ultra")
    with pytest.raises(SpecError, match="scenes\\[0\\].camera must have at least 1 entry"):
        check(dict(RECT), camera=[])
    with pytest.raises(SpecError, match="scenes\\[0\\].camera must be a list"):
        check(dict(RECT), camera={})
    with pytest.raises(SpecError, match="scenes\\[0\\].kicker: unknown field for a motion scene"):
        check(dict(RECT), kicker="x")
    with pytest.raises(SpecError, match="scenes\\[0\\] \\(motion\\): missing elements"):
        validate({"scenes": [{"type": "motion"}]})
    assert check(dict(RECT))["seconds"] == 6.0
    with pytest.raises(SpecError, match="scenes\\[0\\].stagger must be a number between 0.1 and 3"):
        check(dict(RECT), stagger=5)


def test_normalised_output_validates_again_and_nothing_is_mutated():
    raw = scene({"id": 7, "kind": "text", "text": "Hello there", "split": "words", "enter": "pop", "emphasis": "pulse",
                 "end": 5, "exit": "slide-out", "moves": [{"at": 1.5, "vy": 0.3, "opacity": 0.5}]},
                {**RECT, "id": "box", "at": 1, "enter": {"type": "slide-up", "easing": "back"}, "easing": "linear"},
                {"kind": "circle", "r": 30, "enter": "draw", "layer": 2},
                flash=[{"node": "box", "at": 1.5, "text": "ok"}], stagger=0.5, title="T", quality="draft")
    before = copy.deepcopy(raw)
    once = validate({"scenes": [raw]})
    assert raw == before
    twice = validate({"scenes": once["scenes"]})
    assert twice["scenes"] == once["scenes"]
    el = once["scenes"][0]["elements"]
    assert el[0]["id"] == "7" and el[0]["enter"] == {"type": "pop", "seconds": 0.45, "easing": "back"}
    assert el[0]["exit"] == {"type": "slide-out", "seconds": 0.4, "easing": "in_out"}
    assert el[0]["emphasis"] == {"type": "pulse", "seconds": 0.6, "repeat": 1}
    assert el[1]["enter"] == {"type": "slide-up", "seconds": 0.5, "easing": "back"}    # the mapping's easing wins
    assert el[2]["enter"]["easing"] == "in_out" and "at" not in el[0] and "at" not in el[2]   # no auto times written back
    assert el[1]["easing"] == "linear" and check({**RECT, "easing": "linear"})["elements"][0]["enter"]["easing"] == "linear"


# --- S2: groups, arrows, paths ---------------------------------------------------------------------------------

def test_group_rules():
    kid = dict(RECT)
    with pytest.raises(SpecError, match="elements\\[0\\].children has 21 entries; the limit is 20 \\(MAX_GROUP_CHILDREN\\)"):
        check({"kind": "group", "children": [kid] * 21})
    with pytest.raises(SpecError, match="elements\\[0\\].children must have at least 1 entry"):
        check({"kind": "group", "children": []})
    with pytest.raises(SpecError, match="elements\\[0\\]: a group element needs children"):
        check({"kind": "group"})
    g3 = {"kind": "group", "children": [{"kind": "group", "children": [{"kind": "group", "children": [kid]}]}]}
    check(g3)
    with pytest.raises(SpecError, match="elements\\[0\\].children\\[0\\].children\\[0\\].children\\[0\\]: groups nest at most 3 levels"):
        check({"kind": "group", "children": [g3]})
    with pytest.raises(SpecError, match="elements\\[0\\].children\\[0\\].x must be a number between -1 and 1"):
        check({"kind": "group", "children": [{**kid, "x": 1.5}]})
    with pytest.raises(SpecError, match="elements\\[0\\].stagger must be a number between 0 and 3"):
        check({"kind": "group", "children": [kid], "stagger": 4})
    with pytest.raises(SpecError, match="elements\\[0\\].w: unknown field for a group element"):
        check({"kind": "group", "children": [kid], "w": 10})
    msg = bad("drawable elements after groups and split words", *[{"kind": "group", "children": [kid] * 20}] * 4)
    assert msg.startswith("scenes[0].elements: 61 drawable elements")      # counted as they come, not after expanding


def test_group_children_default_to_enter_none_and_ids_are_unique_across_groups():
    el = check({"kind": "group", "id": "g", "children": [dict(RECT), {**RECT, "enter": "pop"}]})["elements"][0]
    assert el["enter"]["type"] == "fade" and el["children"][0]["enter"]["type"] == "none"
    assert el["children"][1]["enter"]["type"] == "pop"
    with pytest.raises(SpecError, match="elements\\[1\\].id: duplicate element id 'g'"):
        check({"kind": "group", "id": "g", "children": [dict(RECT)]}, {**RECT, "id": "g"})
    with pytest.raises(SpecError, match="children\\[1\\].id: duplicate element id 'a'"):
        check({"kind": "group", "children": [{**RECT, "id": "a"}, {**RECT, "id": "a"}]})
    # a child's timing is checked against the group's start plus its stagger
    with pytest.raises(SpecError, match="children\\[1\\].moves\\[0\\].at: starts at 0.5s, before the element has entered \\(1.4s\\)"):
        check({"kind": "group", "at": 0.0, "stagger": 1.0,
               "children": [dict(RECT), {**RECT, "enter": "fade", "moves": [{"at": 0.5, "x": 0.1}]}]})


def test_arrow_rules():
    ok = check({**RECT, "id": "a"}, {"kind": "arrow", "from": "a", "to": [0.5, 0.5], "dashed": True, "head": False})
    assert ok["elements"][1]["to"] == [0.5, 0.5] and ok["elements"][1]["from"] == "a"
    with pytest.raises(SpecError, match="elements\\[0\\].to: unknown element 'zz'"):
        check({"kind": "arrow", "from": [0, 0], "to": "zz"})
    with pytest.raises(SpecError, match="elements\\[0\\]: an arrow element needs from and to"):
        check({"kind": "arrow", "from": [0, 0]})
    with pytest.raises(SpecError, match="elements\\[0\\].x: unknown field for an arrow element"):
        check({"kind": "arrow", "from": [0, 0], "to": [1, 1], "x": 0.5})
    with pytest.raises(SpecError, match="elements\\[0\\].from must be an element id or \\[x, y\\]"):
        check({"kind": "arrow", "from": [0, 0, 0], "to": [1, 1]})
    with pytest.raises(SpecError, match="moves\\[0\\]: needs at least one of opacity"):
        check({"kind": "arrow", "from": [0, 0], "to": [1, 1], "moves": [{"at": 1}]})
    with pytest.raises(SpecError, match="moves\\[0\\].x: unknown field for a move"):
        check({"kind": "arrow", "from": [0, 0], "to": [1, 1], "moves": [{"at": 1, "x": 0.2}]})
    check({"kind": "arrow", "from": [0, 0], "to": [1, 1], "moves": [{"at": 1, "opacity": 0.2}]})


def test_path_rules():
    pts = [[0.1, 0.1], [0.5, 0.5], [0.9, 0.1]]
    check({"kind": "path", "points": pts, "closed": True, "smooth": True, "fill": "card"})
    with pytest.raises(SpecError, match="elements\\[0\\].fill: needs closed: true"):
        check({"kind": "path", "points": pts, "fill": "card"})
    with pytest.raises(SpecError, match="elements\\[0\\].points must have at least 2 entries"):
        check({"kind": "path", "points": [[0, 0]]})
    with pytest.raises(SpecError, match="elements\\[0\\].points has 201 entries; the limit is 200 \\(MAX_PATH_POINTS\\)"):
        check({"kind": "path", "points": [[0.5, 0.5]] * 201})
    with pytest.raises(SpecError, match="elements\\[0\\].points\\[1\\] must be \\[x, y\\]"):
        check({"kind": "path", "points": [[0, 0], [1]]})
    with pytest.raises(SpecError, match="elements\\[0\\].points\\[1\\]\\[0\\] must be a number between -0.5 and 1.5"):
        check({"kind": "path", "points": [[0, 0], [3, 1]]})
    with pytest.raises(SpecError, match="elements\\[0\\].rotate: unknown field for a path element"):
        check({"kind": "path", "points": pts, "rotate": 4})


def test_flash_can_name_a_group_child_and_checks_its_explicit_time():
    ok = check({"kind": "group", "children": [{**RECT, "id": "c", "at": 1.0}]}, flash=[{"node": "c", "at": 2.0}])
    assert ok["flash"][0]["node"] == "c"
    with pytest.raises(SpecError, match="flash\\[0\\].at: element 'c' appears at 1.5s, so it cannot flash at 1.0s"):
        check({"kind": "group", "children": [{**RECT, "id": "c", "at": 1.5}]}, flash=[{"node": "c", "at": 1.0}])
    with pytest.raises(SpecError, match="flash\\[0\\].node: unknown element 'zz'"):
        check({"kind": "group", "children": [{**RECT, "id": "c"}]}, flash=[{"node": "zz", "at": 1.0}])


def test_aliased_groups_are_refused_as_the_leaves_are_counted_not_expanded():
    import time
    import yaml
    depth = 3
    lines = ["leaf: &l0 {kind: rect, w: 10, h: 10}"]
    for d in range(1, depth + 1):
        lines.append(f"g{d}: &l{d} {{kind: group, children: [" + ", ".join([f"*l{d - 1}"] * 20) + "]}")
    lines.append("scenes:\n  - type: motion\n    elements: [" + ", ".join(["*l%d" % (depth - 1)] * 60) + "]")
    raw = yaml.safe_load("\n".join(lines))
    start = time.perf_counter()
    with pytest.raises(SpecError, match="scenes\\[0\\].elements: 61 drawable elements after groups and split words"):
        validate({"scenes": raw["scenes"]})
    assert time.perf_counter() - start < 2.0
