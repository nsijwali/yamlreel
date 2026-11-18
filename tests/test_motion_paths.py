"""Path geometry, and the path, arrow, rect, circle and group elements of `motion` scenes."""
from __future__ import annotations

import math

import pytest
from PIL import Image, ImageChops

from yamlreel import motion_paths as mp
from yamlreel import motion_scene as motion
from yamlreel.scenebase import Ctx
from yamlreel.scenes import build_scene
from yamlreel.specs import validate
from yamlreel.theme import get_theme


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def make(tmp_path, *elements, W=270, H=480, seconds=6, **extra):
    spec = validate({"scenes": [{"type": "motion", "elements": list(elements), "seconds": seconds, **extra}]})["scenes"][0]
    ctx = Ctx(W=W, H=H, theme=get_theme("midnight"), workdir=tmp_path, base_dir=tmp_path)
    return build_scene(spec, ctx), ctx


def ink(img: Image.Image, ctx: Ctx) -> int:
    """How many pixels differ from the background."""
    diff = ImageChops.difference(img.convert("RGB"), ctx.background()).convert("L").point(lambda v: 255 if v else 0)
    return diff.histogram()[255]


# --- geometry -------------------------------------------------------------------------------------------------

def test_polyline_length_and_prefix():
    pts = [(0.0, 0.0), (3.0, 4.0), (3.0, 10.0)]
    assert mp.polyline_length(pts) == pytest.approx(11.0)
    assert mp.polyline_length(pts, closed=True) == pytest.approx(11.0 + math.dist((3, 10), (0, 0)))
    assert mp.polyline_prefix(pts, 0.0) == [(0.0, 0.0)]
    assert mp.polyline_prefix(pts, 1.0)[-1] == pytest.approx((3.0, 10.0))
    half = mp.polyline_prefix(pts, 5.5 / 11)
    assert half == [(0.0, 0.0), (3.0, 4.0), pytest.approx((3.0, 4.5))]
    quarter = mp.polyline_prefix(pts, 2.5 / 11)
    assert quarter == [(0.0, 0.0), pytest.approx((1.5, 2.0))]
    assert mp.polyline_prefix([], 0.5) == []
    assert mp.polyline_prefix(pts, 2.0)[-1] == pytest.approx((3.0, 10.0))     # clamped
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert mp.polyline_prefix(square, 1.0, closed=True)[-1] == (0.0, 0.0)      # the closing edge is drawn


def test_prefix_length_grows_with_progress():
    pts = [(0.0, 0.0), (50.0, 20.0), (80.0, 90.0), (10.0, 70.0)]
    lengths = [mp.polyline_length(mp.polyline_prefix(pts, k / 20)) for k in range(21)]
    assert all(b >= a for a, b in zip(lengths, lengths[1:])) and lengths[-1] == pytest.approx(mp.polyline_length(pts))


def test_catmull_rom_passes_through_its_points_and_respects_the_sample_cap():
    pts = [(0.0, 0.0), (10.0, 5.0), (20.0, 0.0), (30.0, 5.0)]
    curve = mp.catmull_rom(pts)
    for p in pts:
        assert any(math.dist(p, c) < 1e-9 for c in curve)
    assert curve[0] == pts[0] and curve[-1] == pts[-1]
    many = [(float(i), float(i % 7)) for i in range(mp.MAX_PATH_POINTS)]
    assert len(mp.catmull_rom(many)) <= mp.MAX_PATH_SAMPLES
    assert len(mp.catmull_rom(many, closed=True)) <= mp.MAX_PATH_SAMPLES
    assert mp.catmull_rom(pts[:2]) == pts[:2]


def test_dashes_follow_the_phase_and_the_length():
    a, b = (0.0, 0.0), (100.0, 0.0)
    d = mp.dash_segments(a, b, 100.0, 10.0, 5.0)
    assert d[0] == ((0.0, 0.0), (10.0, 0.0)) and d[1][0] == (15.0, 0.0) and d[-1][1][0] <= 100.0
    shifted = mp.dash_segments(a, b, 100.0, 10.0, 5.0, phase=5.0)
    assert shifted[0] == ((0.0, 0.0), (5.0, 0.0)) and shifted[1][0] == (10.0, 0.0)
    assert mp.dash_segments(a, b, 0.0, 10.0, 5.0) == [] and mp.dash_segments(a, a, 10.0, 10.0, 5.0) == []
    short = mp.dash_segments(a, b, 17.0, 10.0, 5.0)
    assert short == [((0.0, 0.0), (10.0, 0.0)), ((15.0, 0.0), (17.0, 0.0))]      # the last dash is cut at the length


def test_arrow_clipping_matches_the_diagram_edge_rule():
    """The diagram's `exit_t`: min(w/2 / |ux|, h/2 / |uy|) + 10u, from each box centre toward the other."""
    a, b = (100.0, 100.0), (400.0, 300.0)
    sa, sb = (120.0, 60.0), (200.0, 80.0)
    dist = math.hypot(300, 200)
    ux, uy = 300 / dist, 200 / dist

    def exit_t(size):
        tx = size[0] / 2 / abs(ux)
        ty = size[1] / 2 / abs(uy)
        return min(tx, ty) + 10.0

    start, end, gx, gy = mp.arrow_span(a, sa, b, sb, 10.0, 10.0)
    assert (gx, gy) == pytest.approx((ux, uy))
    assert start == pytest.approx((a[0] + ux * exit_t(sa), a[1] + uy * exit_t(sa)))
    assert end == pytest.approx((a[0] + ux * (dist - exit_t(sb)), a[1] + uy * (dist - exit_t(sb))))
    assert mp.arrow_span((0, 0), (100, 100), (30, 0), (100, 100), 10, 10) is None       # no room between the boxes
    s2, e2, _, _ = mp.arrow_span((0, 0), (0, 0), (100, 0), (0, 0), 0, 0)               # point to point: no clipping
    assert s2 == (0.0, 0.0) and e2 == (100.0, 0.0)
    assert mp.exit_distance(1.0, 0.0, 80.0, 20.0, 5.0) == 45.0 and mp.exit_distance(0.0, 1.0, 80.0, 20.0, 5.0) == 15.0


# --- drawing on: the ink grows monotonically -------------------------------------------------------------------

@pytest.mark.parametrize("element", [
    {"kind": "path", "points": [[0.1, 0.5], [0.4, 0.3], [0.6, 0.7], [0.9, 0.5]], "stroke_width": 8},
    {"kind": "path", "points": [[0.2, 0.3], [0.8, 0.3], [0.5, 0.8]], "closed": True, "smooth": True, "stroke_width": 8},
    {"kind": "rect", "w": 600, "h": 300, "stroke_width": 10, "fill": "none"},
    {"kind": "circle", "r": 200, "fill": "none", "stroke": "accent", "stroke_width": 10},
    {"kind": "arrow", "from": [0.1, 0.2], "to": [0.9, 0.8], "stroke_width": 10},
    {"kind": "arrow", "from": [0.1, 0.2], "to": [0.9, 0.8], "stroke_width": 10, "dashed": True},
])
def test_draw_on_adds_ink_and_ends_as_the_finished_shape(tmp_path, element):
    sc, ctx = make(tmp_path, {**element, "at": 0.0, "enter": {"type": "draw", "seconds": 2.0, "easing": "linear"}}, W=540, H=960)
    amounts = [ink(sc.frame(k * 0.1), ctx) for k in range(0, 21)]
    assert amounts[0] == 0 or amounts[0] < amounts[-1] * 0.05
    assert all(b >= a - 2 for a, b in zip(amounts, amounts[1:])), amounts      # never loses ink (edge pixels aside)
    assert amounts[10] < amounts[-1] and amounts[-1] > 200
    plain, _ = make(tmp_path, {**element, "at": 0.0, "enter": "none"}, W=540, H=960)
    done = sc.frame(5.0)
    if not element.get("dashed"):
        assert ImageChops.difference(done.convert("RGB"), plain.frame(5.0).convert("RGB")).getbbox() is None


def test_a_path_fill_fades_in_at_the_end_of_the_draw(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "path", "points": [[0.2, 0.3], [0.8, 0.3], [0.5, 0.8]], "closed": True, "fill": "card",
                              "at": 0.0, "enter": {"type": "draw", "seconds": 2.0, "easing": "linear"}})
    assert sc.frame(1.0).getpixel((135, 190)) == ctx.background().getpixel((135, 190))      # not yet filled
    assert sc.frame(2.5).getpixel((135, 190)) == ctx.theme.card


# --- arrows follow their elements ------------------------------------------------------------------------------

def test_an_arrow_between_elements_stops_at_their_boxes_and_follows_a_move(tmp_path):
    els = [{"id": "a", "kind": "rect", "w": 200, "h": 100, "x": 0.5, "y": 0.2, "at": 0.0, "enter": "none"},
           {"id": "b", "kind": "rect", "w": 200, "h": 100, "x": 0.5, "y": 0.8, "at": 0.0, "enter": "none",
            "moves": [{"at": 1.0, "seconds": 1.0, "x": 0.8, "easing": "linear"}]},
           {"kind": "arrow", "from": "a", "to": "b", "at": 0.0, "enter": "none", "stroke_width": 8}]
    sc, ctx = make(tmp_path, *els, W=540, H=960)
    plan0 = {it.leaf.kind.__class__.__name__: it for it in sc._plan(0.5)}
    plan1 = {it.leaf.kind.__class__.__name__: it for it in sc._plan(2.5)}
    g0, g1 = plan0["ArrowKind"].geo, plan1["ArrowKind"].geo
    assert g0 is not None and g1 is not None and g0 != g1                         # it moved with the box
    ax0 = g0[0] / 16
    assert abs(g0[0] - g0[2]) < 16 and ax0 == pytest.approx(270, abs=1)         # straight down at first
    y_start, y_end = g0[1] / 16, g0[3] / 16
    top_box_bottom = 960 * 0.2 + 100 * ctx.u / 2                                 # u = 0.5: the box is 50 px tall
    assert y_start > top_box_bottom and y_end < 960 * 0.8 - 25 and y_start < y_end
    assert g1[2] / 16 > g0[2] / 16                                               # the far end slid right with box b
    assert sc.frame(0.5) is not None


def test_an_arrow_whose_element_is_hidden_is_not_drawn(tmp_path):
    sc, ctx = make(tmp_path, {"id": "a", "kind": "rect", "w": 200, "h": 100, "y": 0.2, "at": 3.0, "enter": "none"},
                   {"kind": "arrow", "from": "a", "to": [0.5, 0.9], "at": 0.0, "enter": "none"})
    assert ink(sc.frame(1.0), ctx) == 0 and ink(sc.frame(4.0), ctx) > 0


# --- groups -----------------------------------------------------------------------------------------------------

GROUP = {"id": "card", "kind": "group", "y": 0.4, "enter": "pop",
         "children": [{"kind": "rect", "w": 600, "h": 160}, {"kind": "text", "text": "label", "size": 50}]}


def test_group_children_appear_with_their_group_and_move_with_it(tmp_path):
    sc, _ = make(tmp_path, {**GROUP, "at": 1.0, "moves": [{"at": 2.0, "seconds": 1.0, "y": 0.6, "easing": "linear"}]},
                 W=1080, H=1920)
    assert sc.placements(0.9) == []
    kids = sc.placements(1.0)
    assert [p.kind for p in kids] == ["rect", "text"]
    offsets = set()
    for k in range(0, 100):
        ps = sc.placements(1.0 + k / 30)
        if len(ps) == 2:
            offsets.add((ps[1].x - ps[0].x, ps[1].y - ps[0].y))
    assert offsets == {(0, 0)}
    assert sc.placements(5.0)[0].y == round(1920 * 0.6)


def test_group_children_are_staggered_by_the_group_and_may_enter_on_their_own(tmp_path):
    sc, _ = make(tmp_path, {"kind": "group", "at": 1.0, "stagger": 0.5, "y": 0.5,
                            "children": [{"kind": "rect", "w": 100, "h": 50}, {"kind": "rect", "w": 100, "h": 50, "y": 0.1},
                                         {"kind": "rect", "w": 100, "h": 50, "y": 0.2, "at": 1.2, "enter": "fade"}]})
    assert [len(sc.placements(t)) for t in (0.9, 1.0, 1.4, 1.5, 1.2 + 0.0)] == [0, 1, 2, 3, 2]   # at 1.0, 1.2 (its own), 1.5
    third = next(n for n in sc.roots[0].children if n.index == sc.roots[0].children[2].index)
    assert third.at == 1.2 and third.enter.type == "fade" and sc.roots[0].children[0].enter.type == "none"


def test_group_children_are_drawn_in_layer_order(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "group", "at": 0.0, "enter": "none", "y": 0.5,
                              "children": [{"kind": "rect", "w": 300, "h": 300, "fill": "accent", "stroke": "none", "layer": 2},
                                           {"kind": "rect", "w": 300, "h": 300, "fill": "fg", "stroke": "none", "layer": 1}]},
                   W=540, H=960)
    assert sc.frame(1.0).getpixel((270, 480)) == ctx.theme.accent                 # layer 2 on top, though drawn second
    plain, _ = make(tmp_path, {"kind": "group", "at": 0.0, "enter": "none", "y": 0.5,
                               "children": [{"kind": "rect", "w": 300, "h": 300, "fill": "accent", "stroke": "none"},
                                            {"kind": "rect", "w": 300, "h": 300, "fill": "fg", "stroke": "none"}]},
                    W=540, H=960)
    assert plain.frame(1.0).getpixel((270, 480)) == ctx.theme.fg                  # document order otherwise


def test_a_group_inherits_its_layer_and_nested_groups_compose(tmp_path):
    sc, _ = make(tmp_path, {"kind": "group", "x": 0.5, "y": 0.5, "scale": 2.0, "at": 0, "enter": "none", "layer": 3,
                            "children": [{"kind": "group", "x": 0.1, "y": 0.0, "scale": 0.5,
                                          "children": [{"kind": "circle", "r": 40}]}]}, W=1080, H=1920)
    (p,) = sc.placements(1.0)
    assert p.x == round(540 + 0.1 * 1080 * 2.0) and sc.layers[p and next(iter(sc.leaves))] == 3
    assert sc.frame(1.0).size == (1080, 1920)


def test_a_flash_on_a_group_recolours_every_child_and_the_tag_sits_by_the_group(tmp_path):
    sc, ctx = make(tmp_path, {**GROUP, "at": 0.0, "enter": "none"}, flash=[{"node": "card", "at": 1.0, "text": "hit"}])
    assert sc._flash_leaves == [tuple(sc.leaves)]
    assert sc.frame(0.5) is not None and sc.frame(2.0).getbbox() is not None
    plain, _ = make(tmp_path, {**GROUP, "at": 0.0, "enter": "none"})
    assert ImageChops.difference(sc.frame(2.0).convert("RGB"), plain.frame(2.0).convert("RGB")).getbbox() is not None


def test_a_motion_scene_with_a_group_has_no_unwanted_warnings(tmp_path):
    sc, ctx = make(tmp_path, GROUP)
    assert ctx.warnings == []


def test_arrows_and_paths_carry_no_anchor():
    assert not motion.KINDS["arrow"].POSITIONAL and not motion.KINDS["path"].POSITIONAL
    assert motion.POSITION_FIELDS <= motion.KINDS["arrow"].REJECTS


def test_s2_frames_depend_only_on_t(tmp_path):
    import hashlib
    els = [{**GROUP, "at": 0.3}, {"kind": "circle", "r": 40, "y": 0.7, "at": 0.8, "enter": "draw"},
           {"kind": "arrow", "from": "card", "to": [0.5, 0.9], "at": 1.0, "enter": "draw", "dashed": True},
           {"kind": "path", "points": [[0.1, 0.9], [0.4, 0.75], [0.9, 0.9]], "smooth": True, "at": 1.2, "enter": "draw"},
           {"kind": "rect", "w": 200, "h": 80, "y": 0.6, "at": 1.5, "enter": "draw", "emphasis": "pulse"}]
    kw = dict(camera=[{"at": 2.0, "seconds": 1.0, "zoom": 1.6, "focus": [0.4, 0.5]}, {"at": 4.0, "seconds": 0.5}],
              flash=[{"node": "card", "at": 2.5, "text": "x"}])
    times = [0.0, 0.5, 1.1, 1.9, 2.5, 3.2, 4.2, 5.9]
    digest = lambda sc, t: hashlib.sha256(sc.frame(t).tobytes()).hexdigest()  # noqa: E731
    a, _ = make(tmp_path, *els, **kw)
    want = {t: digest(a, t) for t in times}
    b, _ = make(tmp_path, *els, **kw)
    assert {t: digest(b, t) for t in reversed(times)} == want
    c, _ = make(tmp_path, *els, **kw)
    c._frame_cache = False
    assert {t: digest(c, t) for t in times} == want
    d, _ = make(tmp_path, *els, **kw)
    d.frame(2.2)
    d.release()
    assert {t: digest(d, t) for t in times} == want
