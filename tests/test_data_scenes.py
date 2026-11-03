"""Data scenes (S2): table and array."""
from __future__ import annotations

import pytest
from PIL import Image

from yamlreel import legacy
from yamlreel import datascenes as ds
from yamlreel.datascenes import ArrayScene, ArrayStepError, TableScene, array_states
from yamlreel.render import Timeline
from yamlreel.specs import FORMATS, SpecError, load_spec, validate


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for prefix in ("YAMLREEL", legacy.LEGACY.upper()):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)
    legacy._warned.clear()


ROWS = [[str(i), f"user{i}", f"user{i}@example.com"] for i in range(1, 9)]
TABLE = {"type": "table", "columns": ["id", "name", "email"], "rows": ROWS, "stagger": 0.25, "seconds": 8,
         "scan": {"at": 2.0, "per_row": 0.3, "stop": 4}}
ARRAY = {"type": "array", "items": ["ada", "kim"], "seconds": 8}


def table(tmp_path, fmt="landscape", **kw) -> tuple[Timeline, TableScene]:
    tl = Timeline(validate({"scenes": [{**TABLE, **kw}]}), fmt, tmp_path, scale=0.5)
    sc = tl.scenes[0]
    assert isinstance(sc, TableScene)
    return tl, sc


def states(items, steps, **kw):
    return array_states({"items": items, "steps": steps, **kw})


def texts(state):
    return [c.text for c in state.cells]


# --- table: timing and geometry -------------------------------------------------------------------------

def test_visible_rows_is_monotonic_and_reaches_every_row(tmp_path):
    _, sc = table(tmp_path)
    counts = [sc.visible_rows(k * 0.05) for k in range(200)]
    assert counts == sorted(counts) and counts[0] == 0 and counts[-1] == len(ROWS)
    assert sc.visible_rows(0.35) == 1 and sc.visible_rows(0.6) == 2


def test_reveal_all_shows_every_row_at_once(tmp_path):
    _, sc = table(tmp_path, reveal="all")
    assert sc.visible_rows(0.3) == 0 and sc.visible_rows(0.35) == len(ROWS)


def test_scan_row_walks_then_holds(tmp_path):
    _, sc = table(tmp_path)
    assert sc.scan_row(1.9) is None
    assert sc.scan_row(2.0) == 0.0
    assert sc.scan_row(2.3) == pytest.approx(1.0)
    walk = [sc.scan_row(2.0 + k * 0.05) for k in range(60)]
    assert walk == sorted(walk) and max(walk) == 4.0
    assert sc.scan_row(7.9) == 4.0  # stays on `stop`


def test_row_boxes_stack_inside_the_card(tmp_path):
    _, sc = table(tmp_path)
    boxes = [sc.row_box(i) for i in range(len(ROWS))]
    for a, b in zip(boxes, boxes[1:]):
        assert b[1] == pytest.approx(a[3], abs=1) and a[0] == b[0] and a[2] == b[2]
    x0, y0, x1, y1 = sc.card
    assert boxes[0][1] > y0 and boxes[-1][3] <= y1 and boxes[0][0] >= x0 and boxes[0][2] <= x1
    with pytest.raises(IndexError):
        sc.row_box(len(ROWS))


def test_scan_tints_the_stop_row_after_it_arrives(tmp_path):
    tl, sc = table(tmp_path)
    x0, y0, x1, y1 = sc.row_box(4)
    probe = (x0 + 3, (y0 + y1) // 2)  # inside the row, left of any text
    before = tl.scenes[0].frame(1.5).getpixel(probe)
    after = sc.frame(2.0 + 4 * 0.3 + 0.5).getpixel(probe)
    assert before != after
    th = tl.ctx.theme
    # moved towards the accent colour
    assert sum(abs(a - b) for a, b in zip(after, th.accent)) < sum(abs(a - b) for a, b in zip(before, th.accent))
    # another row, far from the scan, is untouched
    x0, y0, x1, y1 = sc.row_box(7)
    assert sc.frame(5.0).getpixel((x0 + 3, (y0 + y1) // 2)) == before_at(sc, 1.5, 7)


def before_at(sc, t, row):
    x0, y0, x1, y1 = sc.row_box(row)
    return sc.frame(t).getpixel((x0 + 3, (y0 + y1) // 2))


def test_highlight_tints_its_row_from_its_time(tmp_path):
    _, sc = table(tmp_path, highlight=[{"row": 6, "at": 4.0, "text": "hit", "color": "accent2"}])
    assert before_at(sc, 3.9, 6) != before_at(sc, 4.6, 6)


def test_counter_reaches_its_target_exactly(tmp_path):
    _, sc = table(tmp_path, counter={"label": "rows scanned", "to": 10_000_000, "at": 1.0, "seconds": 2})
    vals = [sc.counter_value(k * 0.1) for k in range(80)]
    assert vals == sorted(vals) and vals[0] == 0 and vals[-1] == 10_000_000
    assert sc.counter_value(3.0) == 10_000_000
    assert 0 < sc.counter_value(1.8) < 10_000_000
    assert table(tmp_path)[1].counter_value(1.0) is None


def test_counter_frames_differ_and_the_text_cache_is_bounded(tmp_path):
    _, sc = table(tmp_path, counter={"to": 5_000_000, "at": 0.5, "seconds": 3})
    assert sc.frame(1.0).tobytes() != sc.frame(2.0).tobytes()
    for k in range(300):
        sc.frame(0.5 + k * 0.01)
    assert len(sc._cnt_cache) <= TableScene.COUNTER_CACHE


def test_the_static_layer_is_built_once(tmp_path):
    _, sc = table(tmp_path)
    sc.frame(1.0)
    first = sc._static["table"]
    sc.frame(2.0)
    assert sc._static["table"] is first and len(sc._static) == 1


def test_table_warnings(tmp_path):
    tl, _ = table(tmp_path, seconds=1.5)
    assert any("scene ends" in w for w in tl.warnings)
    early, _ = table(tmp_path, scan={"at": 0.4, "per_row": 0.1, "stop": 4})
    assert any("before it appears" in w for w in early.warnings)
    many = {"type": "table", "columns": [f"column {i}" for i in range(6)],
            "rows": [[f"a fairly long value {i}-{j}" for j in range(6)] for i in range(12)]}
    tl2 = Timeline(validate({"scenes": [many]}), "vertical", tmp_path, scale=0.5)
    assert any("small" in w or "shrunk" in w for w in tl2.warnings)


# --- table: validation ----------------------------------------------------------------------------------

def bad_table(**kw):
    return validate({"scenes": [{"type": "table", "columns": ["a", "b"], "rows": [["1", "2"], ["3", "4"]], **kw}]})


@pytest.mark.parametrize("kw,msg", [
    ({"rows": [["1", "2"], ["3"]]}, r"scenes\[0\]\.rows\[1\] must have 2 cells \(one per column\), got 1"),
    ({"rows": [["1", "2", "3"]]}, r"rows\[0\] must have 2 cells"),
    ({"rows": []}, r"rows must have at least 1 entry"),
    ({"rows": [["1", "2"]] * 13}, r"rows has 13 entries; the limit is 12 \(MAX_TABLE_ROWS\)"),
    ({"columns": list("abcdefg"), "rows": [list("abcdefg")]}, r"columns has 7 entries; the limit is 6"),
    ({"columns": []}, r"columns must have at least 1 entry"),
    ({"rows": [["x" * 41, "2"]]}, r"rows\[0\]\[0\] is 41 characters; the limit is 40"),
    ({"rows": [[None, "2"]]}, r"rows\[0\]\[0\] must be text"),
    ({"rows": [["1", ""]]}, r"rows\[0\]\[1\] must not be empty"),
    ({"rows": ["12"]}, r"rows\[0\] must be a list of 2 cells"),
    ({"scan": {"stop": 2}}, r"scan\.stop must be an integer between 0 and 1"),
    ({"scan": {"per_row": 0.01}}, r"scan\.per_row must be a number between 0\.05 and 3"),
    ({"scan": {"color": "red"}}, r"scan\.color must be one of"),
    ({"scan": {"speed": 1}}, r"scan\.speed: unknown field for a table scan"),
    ({"scan": {}}, r"scan must be true or a mapping"),
    ({"highlight": [{"row": 5}]}, r"highlight\[0\]\.row must be an integer between 0 and 1"),
    ({"highlight": [{"at": 1}]}, r"highlight\[0\]: needs row"),
    ({"highlight": {"row": 1}}, r"highlight must be a list"),
    ({"counter": {"label": "x"}}, r"counter: needs to"),
    ({"counter": {"to": 5, "seconds": 0}}, r"counter\.seconds must be a number"),
    ({"counter": {"to": 5, "by": 1}}, r"counter\.by: unknown field"),
    ({"reveal": "slow"}, r"reveal must be one of rows, all"),
    ({"stagger": 9}, r"stagger must be a number between 0\.1 and 3"),
])
def test_bad_tables_are_rejected(kw, msg):
    with pytest.raises(SpecError, match=msg):
        bad_table(**kw)


def test_table_needs_columns_and_rows():
    with pytest.raises(SpecError, match=r"scenes\[0\] \(table\): missing columns, rows"):
        validate({"scenes": [{"type": "table"}]})


def test_a_table_spec_normalises_cells_to_text():
    sc = bad_table(rows=[[1, 2.5], [True and "x", "y"]], scan=True)["scenes"][0]
    assert sc["rows"][0] == ["1", "2.5"] and sc["scan"] == {"stop": 1}


def test_highlight_means_different_things_in_code_and_table():
    validate({"scenes": [{"type": "code", "code": "a\nb", "highlight": [2]}]})
    with pytest.raises(SpecError, match=r"highlight\[0\] must be a mapping"):
        bad_table(highlight=[1])


# --- array: pure step logic -----------------------------------------------------------------------------

def test_insert_keeps_order_and_numbers_sort_by_value():
    s = states(["ada", "kim"], [{"at": 1, "insert": "eve"}, {"at": 2, "insert": "zoe"}, {"at": 3, "insert": "Bob"}])
    assert [texts(x) for x in s] == [["ada", "kim"], ["ada", "eve", "kim"], ["ada", "eve", "kim", "zoe"],
                                      ["ada", "Bob", "eve", "kim", "zoe"]]
    n = states(["2", "10"], [{"at": 1, "insert": "5"}, {"at": 2, "insert": "-3"}, {"at": 3, "insert": "x"}])
    assert texts(n[-1]) == ["-3", "2", "5", "10", "x"]


def test_cells_keep_their_ids_as_others_come_and_go():
    s = states(["a", "b"], [{"at": 1, "push": "c"}, {"at": 2, "pop": "front"}, {"at": 3, "insert": "bb"}])
    ids = {c.text: c.id for st in s for c in st.cells}
    assert ids == {"a": 0, "b": 1, "c": 2, "bb": 3}
    assert [c.id for c in s[2].cells] == [1, 2] and s[2].removed.text == "a" and s[2].side == "front"
    assert s[1].added == 2 and s[3].added == 3 and s[3].removed is None


def test_push_and_pop_front_and_back():
    s = states(["a", "b", "c"], [{"at": 1, "push": "d"}, {"at": 2, "pop": "back"}, {"at": 3, "pop": "front"}])
    assert texts(s[1]) == ["a", "b", "c", "d"] and texts(s[2]) == ["a", "b", "c"] and texts(s[3]) == ["b", "c"]
    assert s[2].removed.text == "d" and s[2].side == "back"


def test_steps_are_replayed_in_time_order_and_unset_times_follow_the_previous_step():
    s = states(["a"], [{"at": 5, "push": "late"}, {"at": 1, "push": "early"}])
    assert [st.t for st in s] == [0.0, 1.0, 5.0] and texts(s[-1]) == ["a", "early", "late"]
    auto = states(["a"], [{"push": "x"}, {"push": "y"}, {"at": 4, "push": "z"}, {"push": "w"}], stagger=0.5)
    assert [st.t for st in auto][1:] == [0.5, 1.0, 4.0, 4.5]
    assert [st.t for st in states([], [{"push": "x"}])] == [0.0, ds.ARRAY_STAGGER]


def test_marks_follow_their_cells_and_unmark_clears():
    s = states(["a", "b", "c"], [{"at": 1, "mark": [1, 2], "text": "hit", "color": "accent2"}, {"at": 2, "insert": "aa"},
                                  {"at": 3, "pop": "back"}, {"at": 4, "unmark": True}])
    assert s[1].marks == (1, 2) and s[1].mark_text == "hit" and s[1].mark_color == "accent2"
    assert s[2].marks == (1, 2) and texts(s[2]) == ["a", "aa", "b", "c"]
    assert s[3].marks == (1,)  # the popped cell is no longer marked
    assert s[4].marks == () and s[4].mark_text == ""


def test_window_slides_and_clamps_when_the_array_shrinks():
    s = states(["a", "b", "c", "d"], [{"at": 1, "window": [0, 2], "text": "w"}, {"at": 2, "window": [1, 3]},
                                       {"at": 3, "pop": "back"}, {"at": 4, "pop": "back"}, {"at": 5, "pop": "back"}])
    assert s[1].window == (0, 2) and s[2].window == (1, 3) and s[2].window_text == ""
    assert s[3].window == (1, 2) and s[4].window == (1, 1) and s[5].window is None  # clamped, then gone


@pytest.mark.parametrize("steps,msg", [
    ([{"at": 1, "pop": "front"}, {"at": 1.5, "pop": "back"}, {"at": 2, "pop": "front"}],
     r"steps\[2\]\.pop: array is empty at 2s"),
    ([{"at": 1, "mark": [2]}], r"steps\[0\]\.mark: index 2 is outside the 2 cells \(0\.\.1\) at 1s"),
    ([{"at": 1, "window": [0, 5]}], r"steps\[0\]\.window: \[0, 5\] is outside the 2 cells \(0\.\.1\) at 1s"),
    ([{"at": 1, "pop": "front"}, {"at": 2, "pop": "front"}, {"at": 2.4, "pop": "back"}],
     r"steps\[2\]\.pop: array is empty at 2\.4s"),
    ([{"at": 1, "insert": "x", "push": "y"}], r"steps\[0\]: needs exactly one of insert, push, pop, mark, unmark, window, found insert and push"),
    ([{"at": 1}], r"steps\[0\]: needs exactly one of"),
])
def test_array_step_errors_name_the_step_and_time(steps, msg):
    with pytest.raises(ArrayStepError, match=msg):
        states(["a", "b"], steps)


def test_more_than_twelve_cells_is_an_error():
    with pytest.raises(ArrayStepError, match=r"steps\[0\]\.push: array would have 13 cells; the limit is 12 \(MAX_CELLS\) at 1s"):
        states([str(i) for i in range(12)], [{"at": 1, "push": "x"}])
    with pytest.raises(ArrayStepError, match="insert: array would have 13"):
        states([str(i) for i in range(12)], [{"at": 1, "insert": "x"}])
    assert len(states([str(i) for i in range(11)], [{"at": 1, "push": "x"}])[-1].cells) == 12


# --- array: validation through the spec -----------------------------------------------------------------

def bad_array(**kw):
    return validate({"scenes": [{**ARRAY, **kw}]})


@pytest.mark.parametrize("kw,msg", [
    ({"items": [str(i) for i in range(13)]}, r"scenes\[0\]\.items has 13 entries; the limit is 12 \(MAX_CELLS\)"),
    ({"items": "abc"}, r"items must be a list"),
    ({"items": [""]}, r"items\[0\] must not be empty"),
    ({"steps": [{"at": 1, "pop": "front"}] * 3}, r"scenes\[0\]\.steps\[2\]\.pop: array is empty at 1s"),
    ({"steps": [{"at": 1, "insert": "x", "push": "y"}]}, r"steps\[0\]: needs exactly one of"),
    ({"steps": [{"at": 1, "mark": [9]}]}, r"steps\[0\]\.mark: index 9 is outside the 2 cells"),
    ({"steps": [{"at": 1, "window": [0, 5]}]}, r"steps\[0\]\.window: \[0, 5\] is outside the 2 cells"),
    ({"steps": [{"at": 1, "window": [1, 0]}]}, r"steps\[0\]\.window must be \[first, last\] with first <= last"),
    ({"steps": [{"at": 1, "window": [1]}]}, r"steps\[0\]\.window must be \[first, last\]"),
    ({"steps": [{"at": 1, "unmark": False}]}, r"steps\[0\]\.unmark must be true"),
    ({"steps": [{"at": 1, "pop": "middle"}]}, r"steps\[0\]\.pop must be one of front, back"),
    ({"steps": [{"at": 1, "push": "x", "text": "t"}]}, r"steps\[0\]\.text: only a mark or window step has text"),
    ({"steps": [{"at": 1, "mark": [0], "color": "red"}]}, r"steps\[0\]\.color must be one of"),
    ({"steps": [{"at": -1, "push": "x"}]}, r"steps\[0\]\.at must be a number"),
    ({"steps": [{"at": 1, "push": "x", "extra": 1}]}, r"steps\[0\]\.extra: unknown field for an array step"),
    ({"steps": [{"push": "x"}] * 41}, r"steps has 41 entries; the limit is 40 \(MAX_STEPS\)"),
    ({"steps": {"at": 1}}, r"steps must be a list"),
    ({"stagger": 0.01}, r"stagger must be a number between 0\.1 and 3"),
])
def test_bad_arrays_are_rejected(kw, msg):
    with pytest.raises(SpecError, match=msg):
        bad_array(**kw)


def test_array_with_no_items_may_start_empty_and_needs_items():
    validate({"scenes": [{"type": "array", "items": [], "steps": [{"push": "x"}]}]})
    with pytest.raises(SpecError, match=r"\(array\): missing items"):
        validate({"scenes": [{"type": "array"}]})


def test_step_checks_run_after_stagger_whatever_the_key_order():
    sc = validate({"scenes": [{"type": "array", "items": ["a"], "steps": [{"push": "b"}, {"pop": "front"}],
                               "stagger": 1.5}]})["scenes"][0]
    assert sc["steps"][0] == {"push": "b"}  # times are resolved when rendering, not stored


# --- array: rendering -----------------------------------------------------------------------------------

def array_scene(tmp_path, fmt="landscape", **kw) -> ArrayScene:
    tl = Timeline(validate({"scenes": [{**ARRAY, **kw}]}), fmt, tmp_path, scale=0.5)
    sc = tl.scenes[0]
    assert isinstance(sc, ArrayScene)
    return sc


def test_cells_move_apart_when_one_is_inserted(tmp_path):
    sc = array_scene(tmp_path, steps=[{"at": 2, "insert": "eve"}])  # ada eve kim
    before = sc.cell_positions(1.9)
    done = sc.cell_positions(2.5)
    mid = sc.cell_positions(2.08)  # a fifth of the way: the drop has not overshot yet
    assert set(before) == {0, 1} and set(done) == {0, 1, 2}
    kim = 1
    assert before[kim][0] < mid[kim][0] < done[kim][0]  # kim shifts right, in order
    assert done[2][1] == pytest.approx(0, abs=1) and done[2][2] == 1.0  # the new cell has settled
    assert mid[2][1] < 0 and 0 < mid[2][2] <= 1  # and is still dropping in, fading up
    xs = [done[i][0] for i in (0, 2, 1)]
    assert xs == sorted(xs) and len(set(xs)) == 3


def test_a_popped_cell_leaves_to_its_side_and_fades(tmp_path):
    sc = array_scene(tmp_path, items=["a", "b", "c"], steps=[{"at": 1, "pop": "front"}])
    start = sc.cell_positions(1.0)[0]
    mid = sc.cell_positions(1.2)[0]
    assert mid[0] < start[0] and 0 < mid[2] < 1
    assert 0 not in sc.cell_positions(1.6)  # fully faded: no longer drawn


def test_array_frames_change_with_steps_and_marks(tmp_path):
    sc = array_scene(tmp_path, steps=[{"at": 1, "push": "zoe"}, {"at": 2, "mark": [0], "text": "hit"},
                                      {"at": 3, "window": [0, 1], "text": "span"}])
    imgs = [sc.frame(t).tobytes() for t in (0.9, 1.6, 2.6, 3.6)]
    assert len(set(imgs)) == 4


def test_array_warns_about_cramped_text_and_late_steps(tmp_path):
    sc = array_scene(tmp_path, "vertical", items=[f"item-{i:02d}-long" for i in range(12)])
    assert any("shrunk" in w or "small" in w for w in sc.ctx.warnings)
    late = array_scene(tmp_path, seconds=2, steps=[{"at": 1.9, "push": "x"}])
    assert any("ends after the scene" in w for w in late.ctx.warnings)


# --- all formats ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", FORMATS)
def test_table_and_array_render_in_every_format(fmt, tmp_path):
    spec = validate({"scenes": [
        {**TABLE, "counter": {"label": "scanned", "to": 12345, "at": 1}, "title": "users",
         "highlight": [{"row": 4, "at": 3, "text": "found"}]},
        {**ARRAY, "title": "idx", "steps": [{"at": 1, "insert": "bo"}, {"at": 2, "mark": [0, 1], "text": "m"},
                                           {"at": 3, "window": [0, 2], "text": "w"}, {"at": 4, "pop": "back"}]},
    ]})
    tl = Timeline(spec, fmt, tmp_path, scale=0.25)
    for i, sc in enumerate(tl.scenes):
        for frac in (0.0, 0.3, 0.6, 0.99):
            im = tl.frame_at(tl.starts[i] + sc.duration * frac)
            assert im.size == (tl.ctx.W, tl.ctx.H) and im.mode == "RGB"


def test_extreme_tables_and_arrays_still_render(tmp_path):
    wide = {"type": "table", "columns": ["c" * 40] * 6, "rows": [["v" * 40] * 6] * 12, "seconds": 8}
    full = {"type": "array", "items": ["x" * 40] * 12, "seconds": 8}
    tl = Timeline(validate({"scenes": [wide, full]}), "vertical", tmp_path, scale=0.25)
    for i in range(2):
        tl.frame_at(tl.starts[i] + 7.9)


def test_example_validates_and_all_its_scenes_render(tmp_path):
    from pathlib import Path
    spec = load_spec(Path(__file__).parent.parent / "examples" / "index-scan.yaml")
    assert {s["type"] for s in spec["scenes"]} >= {"table", "array"}
    tl = Timeline(spec, "vertical", tmp_path, scale=0.25)
    for t in (3.5, 8.0, 14.0, 20.0):
        assert tl.frame_at(t).size == (tl.ctx.W, tl.ctx.H)


def test_voice_reads_the_title_of_a_data_scene():
    from yamlreel.voice import screen_text
    assert "users" in screen_text({"type": "table", "title": "users"})
    assert "idx" in screen_text({"type": "array", "title": "idx"})
