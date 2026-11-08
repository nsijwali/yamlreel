"""The `grid` scene: validation, the pure step replay, the stain, layout, and a render of the QR video's spec."""
from __future__ import annotations

from pathlib import Path

import pytest

from yamlreel import datascenes as ds
from yamlreel.datascenes import (GridScene, GridStepError, grid_states, recover_order, reveal_order, stain_cells)
from yamlreel.render import Timeline
from yamlreel.specs import SpecError, load_spec, validate

from test_validation import _literal_keys

ROOT = Path(__file__).parent.parent
CELLS = ["#.#.#", ".#.#.", "#.#.#", ".#.#.", "#.o.#"]
GRID = {"type": "grid", "cells": CELLS, "seconds": 10, "seed": 5,
        "steps": [{"at": 0.5, "reveal": "sweep", "seconds": 1}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def check(**kw):
    return validate({"scenes": [{**GRID, **kw}]})["scenes"][0]


def err(**kw) -> str:
    with pytest.raises(SpecError) as e:
        check(**kw)
    return str(e.value)


def steps(*more):
    return {"steps": [GRID["steps"][0], *more]}


# --- validation -----------------------------------------------------------------------------------------

def test_bad_cells_are_rejected():
    assert 'cells[1] must be text; quote rows that start with # ("#..#")' in err(cells=["#.#", None])
    assert "cells[0] must be text; quote rows" in err(cells=[101])
    assert "scenes[0].cells must be a list of rows; quote rows" in err(cells=None)
    assert "cells[2] has 2 cells; row 0 has 3" in err(cells=["#.#", "#.#", "#."])
    assert "cells[1]: 'x' at column 1 is not a cell; use # (foreground), . (card) or o (accent)" in err(cells=["#.#", ".x."])
    assert "cells[0] has 0 cells" in err(cells=[""])
    assert "cells[0] has 34 cells" in err(cells=["#" * 34])
    assert "scenes[0].cells has 34 entries; the limit is 33 (MAX_GRID)" in err(cells=["#"] * 34)
    assert "at least 1" in err(cells=[])
    assert len(check(cells=["#" * 33] * 33)["cells"]) == 33
    assert "seed must be an integer between 0 and 1000000" in err(seed=1_000_001)
    with pytest.raises(SpecError, match="missing cells"):
        validate({"scenes": [{"type": "grid"}]})


def test_unquoted_hash_row_gets_a_quote_hint(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("scenes:\n  - type: grid\n    cells:\n      - #.#\n      - .#.\n", encoding="utf-8")
    with pytest.raises(SpecError, match=r'scenes\[0\]\.cells\[0\] must be text; quote rows that start with #'):
        load_spec(p)


@pytest.mark.parametrize("step,msg", [
    ({"at": 1, "fail": True, "lose": [[0, 0]]}, "steps[1]: needs exactly one of"),
    ({"at": 1}, "steps[1]: needs exactly one of reveal, stain"),
    ({"at": 1, "lose": [[0, 0]], "speed": 2}, "steps[1].speed: unknown field for a grid step"),
    ({"fail": True}, "steps[1]: needs at"),
    ({"at": 11, "fail": True}, "steps[1].at must be a number between 0 and 10"),
    ({"at": 1, "reveal": "wipe"}, "steps[1].reveal must be one of sweep, pop"),
    ({"at": 1, "stain": [5, 0], "radius": 2}, "steps[1].stain must be [row, col] inside the 5 x 5 grid"),
    ({"at": 1, "stain": [1.5, 0], "radius": 2}, "steps[1].stain must be [row, col]"),
    ({"at": 1, "stain": [1, 1]}, "steps[1]: a stain needs radius"),
    ({"at": 1, "stain": [1, 1], "radius": 40}, "steps[1].radius must be a number between 0.5 and 33"),
    ({"at": 1, "fail": True, "radius": 2}, "steps[1].radius: only a stain step has radius"),
    ({"at": 1, "fail": True, "per_cell": 1}, "steps[1].per_cell: only a recover step has per_cell"),
    ({"at": 1, "fail": False}, "steps[1].fail must be true"),
    ({"at": 1, "lose": []}, "steps[1].lose must have at least 1 entry"),
    ({"at": 1, "lose": [[0, 9]]}, "steps[1].lose[0] must be [row, col] inside the 5 x 5 grid"),
    ({"at": 1, "recover": "some"}, "steps[1].recover must be a list"),
    ({"at": 1, "status": "x" * 41}, "steps[1].status is 41 characters"),
    ({"at": 1, "status": "a\nb"}, "steps[1].status must be one line"),
    ({"at": 1, "status": "ok", "color": "pink"}, "steps[1].color must be one of"),
    ({"at": 1, "outline": [3, 0, 1, 1]}, "steps[1].outline must be [r0, c0, r1, c1] inside the 5 x 5 grid with row0 <= row1"),
    ({"at": 1, "outline": [0, 0, 1]}, "steps[1].outline must be [r0, c0, r1, c1]"),
    ({"at": 1, "recover": "all", "per_cell": 5}, "steps[1].per_cell must be a number between 0.01 and 2"),
    ({"at": 1, "recover": "all", "order": "zigzag"}, "steps[1].order must be one of row, random"),
    ({"at": 1, "stain": [1, 1], "radius": 1, "seconds": 0}, "steps[1].seconds must be a number between 0.1 and 10"),
])
def test_bad_grid_steps_are_rejected(step, msg):
    assert msg in err(**steps(step))


def test_step_order_count_and_status_before_reveal():
    assert "steps[2].at must not be earlier than the previous step (2.4)" in err(
        **steps({"at": 2.4, "status": "a"}, {"at": 2, "status": "b"}))
    assert "the limit is 40 (MAX_STEPS)" in err(steps=[{"at": 1, "status": "s"}] * 41)
    assert len(check(steps=[{"at": 1, "status": "s"}] * 40)["steps"]) == 40
    # a status may come before the reveal; anything else may not
    check(steps=[{"at": 0, "status": "hi"}, {"at": 1, "reveal": "pop"}])


# --- replay ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("step_list,msg", [
    ([{"at": 3, "recover": "all"}], "steps[1].recover: no cells are lost at 3s"),
    ([{"at": 2, "lose": [[0, 0]]}, {"at": 6, "recover": [[3, 4]]}], "steps[2].recover: cell [3, 4] is not lost at 6s"),
    ([{"at": 2, "clear_stain": True}], "steps[1].clear_stain: there is no stain at 2s"),
    ([{"at": 2, "stain": [1, 1], "radius": 1}, {"at": 3, "stain": [2, 2], "radius": 1}],
     "steps[2].stain: the grid already has a stain at 3s"),
    ([{"at": 2, "stain": [1, 1], "radius": 1, "seconds": 2}, {"at": 3, "clear_stain": True}],
     "steps[2].clear_stain: the stain is still spreading at 3s"),
    ([{"at": 2, "reveal": "pop"}], "steps[1].reveal: the grid is already shown at 2s"),
])
def test_replay_errors_name_step_and_time(step_list, msg):
    assert f"scenes[0].{msg}" in err(**steps(*step_list))


# recover = all, per_cell 2: (0, 0) is back at 2 s and (4, 4) at 4 s
STAGGER = [{"at": 1, "lose": [[0, 0], [4, 4]]}, {"at": 2, "recover": "all", "per_cell": 2}]


@pytest.mark.parametrize("step,msg", [
    ({"at": 3, "lose": [[4, 4]]}, "steps[3].lose: cell [4, 4] is still coming back at 3s"),
    ({"at": 2.5, "recover": [[4, 4]]}, "steps[3].recover: cell [4, 4] is still coming back at 2.5s"),
    ({"at": 3, "stain": [4, 4], "radius": 1}, "steps[3].stain: cell [4, 4] is still coming back at 3s"),
])
def test_steps_during_a_recover_stagger_are_rejected(step, msg):
    assert f"scenes[0].{msg}" in err(**steps(*STAGGER, step))


def test_clear_stain_during_a_recover_stagger_is_rejected():
    # (0, 4) is back at 2.1 s, (1, 1) at 4.1 s; the stain over (1, 1) is cleared at 3 s
    msg = err(**steps({"at": 1, "lose": [[0, 4], [1, 1]]}, {"at": 1.2, "stain": [0, 0], "radius": 2, "seconds": 0.5},
                      {"at": 2.1, "recover": "all", "per_cell": 2}, {"at": 3, "clear_stain": True}))
    assert "scenes[0].steps[4].clear_stain: cell [1, 1] is still coming back at 3s" in msg


def test_steps_after_the_stagger_ends_work_as_before():
    st = grid_states({"cells": CELLS, "steps": [GRID["steps"][0], *STAGGER, {"at": 4, "lose": [[4, 4]]},
                                                 {"at": 5, "recover": [[4, 4]]}]})
    assert st[-1].lost == frozenset() and st[-1].recovered == ((4, 4),)
    assert (4, 4) in st[-2].lost


def test_replay_requires_a_reveal_first_when_there_is_one():
    with pytest.raises(SpecError, match=r"scenes\[0\]\.steps\[0\]\.lose: the grid is not shown yet at 0\.2s"):
        check(steps=[{"at": 0.2, "lose": [[0, 0]]}, {"at": 1, "reveal": "pop"}])
    check(steps=[{"at": 0.2, "lose": [[0, 0]]}])   # no reveal step: the grid is there from the start


def test_states_track_lost_cells():
    sc = check(steps=[{"at": 0.5, "reveal": "sweep"}, {"at": 1, "lose": [[0, 0], [4, 4]]},
                      {"at": 2, "stain": [2, 2], "radius": 1.5, "seconds": 1}, {"at": 4, "clear_stain": True},
                      {"at": 5, "recover": [[0, 0], [2, 2]], "order": "random"}, {"at": 6, "status": "ok", "color": "accent"},
                      {"at": 6.5, "outline": [0, 0, 1, 1], "text": "tl"}, {"at": 7, "unmark": True}])
    st = grid_states(sc)
    assert [s.action for s in st] == [None, "reveal", "lose", "stain", "clear_stain", "recover", "status", "outline", "unmark"]
    assert not st[0].shown and st[1].shown
    assert st[2].lost == {(0, 0), (4, 4)}
    assert st[3].stain is not None and (2, 2) in st[3].stain.cells and st[3].lost == st[2].lost
    assert st[4].stain is None and st[4].lost == st[2].lost | st[3].stain.cells
    assert st[5].lost == st[4].lost - {(0, 0), (2, 2)} and sorted(st[5].recovered) == [(0, 0), (2, 2)]
    assert st[6].recovered == () and (st[6].status, st[6].status_color) == ("ok", "accent")
    assert [o.text for o in st[7].outlines] == ["tl"] and st[8].outlines == ()


def test_stain_cells_are_seeded_and_format_free():
    a = stain_cells((10, 10), 4.5, 7, 3, 21, 21)
    assert a == stain_cells((10, 10), 4.5, 7, 3, 21, 21)
    assert a != stain_cells((10, 10), 4.5, 8, 3, 21, 21)
    assert a != stain_cells((10, 10), 4.5, 7, 4, 21, 21)
    assert (10, 10) in a
    assert all(((r - 10) ** 2 + (c - 10) ** 2) ** 0.5 <= 4.5 * 1.3 for r, c in a)
    assert stain_cells((0, 0), 0.5, 1, 0, 3, 3) == {(0, 0)}
    assert all(0 <= r < 3 and 0 <= c < 3 for r, c in stain_cells((0, 0), 9, 1, 0, 3, 3))
    cells = ["#" * 21] * 21
    spec = {"cells": cells, "seed": 7, "steps": [{"at": 1, "stain": [10, 10], "radius": 4.5}]}
    # the pure replay knows nothing about formats; both formats read the same state
    assert grid_states(spec)[1].stain.cells == grid_states(spec)[1].stain.cells
    for fmt in ("vertical", "landscape"):
        tl = Timeline(validate({"scenes": [{"type": "grid", "cells": cells, "seed": 7, "seconds": 5,
                                            "steps": [{"at": 1, "stain": [10, 10], "radius": 4.5}]}]}), fmt, Path("."), scale=0.25)
        assert tl.scenes[0].states[1].stain.cells == grid_states(spec)[1].stain.cells


def test_reveal_and_recover_orders():
    pop = reveal_order(5, 5, 5, 0, "pop")
    assert sorted(pop) == [(r, c) for r in range(5) for c in range(5)] and pop != sorted(pop)
    assert pop == reveal_order(5, 5, 5, 0, "pop") and pop != reveal_order(5, 5, 6, 0, "pop")
    assert reveal_order(5, 5, 5, 0, "sweep") == sorted(pop)
    cells = {(r, c) for r in range(4) for c in range(4)}
    rnd = recover_order(cells, 3, 2, "random")
    assert sorted(rnd) == sorted(cells) and rnd == recover_order(cells, 3, 2, "random") and rnd != sorted(cells)
    assert recover_order(cells, 3, 2, "row") == sorted(cells)
    sc = check(steps=[{"at": 0.5, "reveal": "sweep"}, {"at": 1, "lose": [[0, 0], [1, 1], [2, 2]]},
                      {"at": 3, "recover": "all", "per_cell": 0.2, "order": "random"}])
    scene = Timeline(validate({"scenes": [sc]}), "landscape", Path("."), scale=0.25).scenes[0]
    times = sorted(e[0] for cell in [(0, 0), (1, 1), (2, 2)] for e in scene.events[cell] if e[1] == "recover")
    assert times == pytest.approx([3.0, 3.2, 3.4])   # at + (n - 1) * per_cell is the last


# --- rendering ------------------------------------------------------------------------------------------

def build(tmp_path, fmt="landscape", scale=0.5, **kw) -> GridScene:
    tl = Timeline(validate({"scenes": [{**GRID, **kw}]}), fmt, tmp_path, scale=scale)
    sc = tl.scenes[0]
    assert isinstance(sc, GridScene)
    sc.tl = tl
    return sc


@pytest.mark.parametrize("fmt", ["landscape", "vertical", "square"])
def test_geometry_in_every_format(tmp_path, fmt):
    sc = build(tmp_path, fmt, title="T", steps=[{"at": 0.5, "reveal": "sweep"}, {"at": 1, "status": "s"}])
    ctx = sc.tl.ctx
    assert isinstance(sc.pitch, int) and sc.pitch >= 1
    for r in range(sc.rows):
        for c in range(sc.cols):
            x0, y0, x1, y1 = sc.rects[r][c]
            assert x1 - x0 == y1 - y0 == sc.cell
            assert (x0 - sc.rects[0][0][0], y0 - sc.rects[0][0][1]) == (c * sc.pitch, r * sc.pitch)
    assert sc.frame_box[3] < ctx.caption_y and sc.frame_box[1] > 0
    assert sc.frame_box[0] >= 0 and sc.frame_box[2] <= ctx.W


def test_a_33_by_33_grid_stays_readable_in_vertical(tmp_path):
    sc = build(tmp_path, "vertical", scale=1.0, cells=["#." * 16 + "#"] * 33, title="Big",
               steps=[{"at": 0.5, "reveal": "pop"}, {"at": 1, "status": "s"}])
    assert sc.pitch >= 16 and sc.layout_warnings(None) == []
    assert sc.frame_box[3] < sc.tl.ctx.caption_y


def test_lost_cell_pixels_follow_the_story(tmp_path):
    sc = build(tmp_path, scale=1.0, steps=[{"at": 0.5, "reveal": "sweep"}, {"at": 2, "lose": [[1, 1]]},
                                           {"at": 4, "recover": "all"}])
    x0, y0, x1, y1 = sc.rects[1][1]
    crop = lambda t: sc.frame(t).crop((x0, y0, x1, y1))   # noqa: E731
    before, lost, after = crop(1.9), crop(3.0), crop(5.0)
    assert before.tobytes() == after.tobytes() and before.tobytes() != lost.tobytes()
    assert sc.lost_fill in {px for _, px in lost.getcolors()}
    assert crop(4.1).tobytes() != before.tobytes()   # the accent flash


def test_frames_are_deterministic(tmp_path):
    kw = {"steps": [{"at": 0.2, "reveal": "pop"}, {"at": 1, "stain": [2, 2], "radius": 2}, {"at": 3, "clear_stain": True},
                    {"at": 4, "recover": "all", "order": "random"}, {"at": 5, "status": "x"}, {"at": 6, "fail": True, "text": "no"}]}
    a, b = build(tmp_path, **kw), build(tmp_path, **kw)
    for t in (0.1, 0.5, 1.5, 3.2, 4.2, 5.1, 6.2):
        assert a.frame(t).tobytes() == b.frame(t).tobytes()
    assert a.cached_layers() == 1


def test_frame_builds_no_text(tmp_path, monkeypatch):
    sc = build(tmp_path, steps=[{"at": 0.2, "reveal": "pop"}, {"at": 1, "outline": [0, 0, 1, 1], "text": "o"},
                                {"at": 2, "stain": [2, 2], "radius": 2}, {"at": 3.6, "clear_stain": True},
                                {"at": 4, "recover": "all"}, {"at": 5, "status": "x"}, {"at": 6, "fail": True, "text": "no"}],
               title="T")
    calls = []
    from yamlreel import textkit
    for mod, name in ((ds, "make_block"), (textkit, "make_block"), (ds, "font"), (textkit, "font")):
        real = getattr(mod, name)
        monkeypatch.setattr(mod, name, lambda *a, _r=real, _n=name, **k: (calls.append(_n), _r(*a, **k))[1])
    for t in (0.1, 0.6, 1.5, 2.5, 3.2, 4.3, 5.1, 6.1, 9):
        sc.frame(t)
    assert calls == []


def test_layout_warnings(tmp_path):
    big = build(tmp_path, cells=["#." * 16 + "#"] * 33, scale=0.5)
    assert any(m.startswith("grid cells are ") and "fewer rows or columns" in m for m in big.layout_warnings(None)) or big.pitch >= 8
    tiny = build(tmp_path, "vertical", scale=0.5, cells=["#" * 33] * 33, title="A long title " * 6)
    tiny.pitch = 3
    assert "grid cells are 3px; use fewer rows or columns, or a shorter title" in tiny.layout_warnings(None)
    sc = build(tmp_path)
    fx0, fy0, fx1, fy1 = sc.block
    assert "the caption overlaps the grid; shorten the caption" in sc.layout_warnings((fx0, fy0, fx1, fy1))
    assert sc.layout_warnings((0, sc.frame_box[3] + 50, 10, sc.frame_box[3] + 60)) == []
    long = build(tmp_path, "vertical", steps=[{"at": 0.5, "reveal": "sweep"}, {"at": 1, "status": "w" * 40}])
    assert any(m.startswith("grid status 'www") and "shrunk to" in m for m in long.layout_warnings(None))


def test_late_steps_warn(tmp_path):
    for step in ({"at": 9.8, "recover": "all", "per_cell": 0.5}, {"at": 9.9, "fail": True, "text": "late"}):
        tl = Timeline(validate({"scenes": [{**GRID, "steps": [{"at": 0.5, "reveal": "sweep"},
                                                              {"at": 1, "lose": [[0, 0], [0, 1], [0, 2]]}, step]}]}),
                      "landscape", tmp_path, scale=0.25)
        assert any("a grid step ends after the scene" in w for w in tl.ctx.warnings)
    assert not build(tmp_path).tl.ctx.warnings


def test_grid_reads_only_declared_fields():
    read = _literal_keys(GridScene.__init__, {"spec"})
    assert read and read <= GridScene.FIELDS
    assert _literal_keys(grid_states, {"step"}) <= GridScene.STEP_FIELDS
    assert _literal_keys(grid_states, {"spec"}) <= GridScene.FIELDS
    assert {"at", "seconds", "radius", "per_cell", "order", "text", "color", *ds.GRID_ACTIONS} == set(GridScene.STEP_FIELDS)


def test_example_validates_and_renders(tmp_path):
    spec = load_spec(ROOT / "examples" / "grid-demo.yaml")
    assert spec["scenes"][0]["type"] == "grid"
    tl = Timeline(spec, "vertical", tmp_path, scale=0.25)
    for t in (0.5, 2.5, 4.5, 6.5, 9.0, 13.0):
        assert tl.frame_at(t).size == (tl.ctx.W, tl.ctx.H)


def test_qr_video_25x25_uses_every_action_in_vertical(tmp_path):
    rows = []
    for r in range(25):
        row = ""
        for c in range(25):
            corner = (r < 7 and c < 7) or (r < 7 and c > 17) or (r > 17 and c < 7)
            row += "o" if corner and (r in (0, 6, 18, 24) or c in (0, 6, 18, 24)) else ("#" if (r * 7 + c * 3) % 5 < 2 else ".")
        rows.append(row)
    spec = validate({"scenes": [{
        "type": "grid", "title": "Stained QR", "caption": "Still scans", "seconds": 14, "seed": 11, "cells": rows,
        "steps": [{"at": 0.3, "reveal": "pop", "seconds": 1.2},
                  {"at": 2, "outline": [0, 0, 6, 6], "text": "finder"}, {"at": 3, "unmark": True},
                  {"at": 3.2, "stain": [12, 12], "radius": 5, "seconds": 1.5}, {"at": 5, "clear_stain": True},
                  {"at": 5.2, "lose": [[0, 24], [24, 0]]}, {"at": 5.4, "status": "Coffee 1 | Math 0", "color": "accent"},
                  {"at": 6, "fail": True, "text": "Can't read?"}, {"at": 7.6, "status": "Coffee 1 | Math 1"},
                  {"at": 7.8, "recover": "all", "per_cell": 0.02, "order": "random"}, {"at": 11, "status": "Scanned"}]}]})
    tl = Timeline(spec, "vertical", tmp_path, scale=0.5)
    assert tl.ctx.warnings == [] and tl.scenes[0].layout_warnings(None) == []
    for t in (0.2, 0.8, 2.5, 4.0, 5.3, 6.4, 8.5, 13.0):
        assert tl.frame_at(t).size == (tl.ctx.W, tl.ctx.H)
