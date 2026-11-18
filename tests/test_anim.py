"""The motion timeline: a pure function of the element tree and the time (closed forms, defaults, composition)."""
from __future__ import annotations

import math

import pytest

from yamlreel import anim
from yamlreel.anim import Move, Node, Pulse, Timing, preset_timing
from yamlreel.textkit import ease

D = anim.SLIDE_DISTANCE
APPROX = dict(abs=1e-9)


def node(index=0, at=1.0, enter="none", x=100.0, y=200.0, **kw) -> Node:
    t = enter if isinstance(enter, Timing) else preset_timing("enter", enter)
    return Node(index, "rect", at, t, x, y, **kw)


def one(n: Node, t: float, slide: float = D) -> anim.State:
    states = anim.resolve([n], t, slide)
    assert len(states) == 1, f"expected one visible state at t={t}"
    return states[0]


# --- the presets, in closed form -----------------------------------------------------------------------

@pytest.mark.parametrize("name", [n for n in anim.ENTER_PRESETS if n != "none"])
@pytest.mark.parametrize("p_in", [0.0, 0.5, 1.0])
def test_every_enter_preset_matches_its_closed_form(name, p_in):
    secs, easing = anim.ENTER_PRESETS[name]
    n = node(enter=name, scale=1.0)
    t = 1.0 + p_in * secs
    st = one(n, t)
    lin = p_in
    p = ease(easing, lin)
    x, y, s, o, d = 100.0, 200.0, 1.0, 1.0, 1.0
    if name == "fade":
        o = p
    elif name == "slide-up":
        y += (1 - p) * D
        o = p
    elif name == "slide-down":
        y -= (1 - p) * D
        o = p
    elif name == "slide-left":
        x += (1 - p) * D
        o = p
    elif name == "slide-right":
        x -= (1 - p) * D
        o = p
    elif name == "pop":
        s = 0.6 + 0.4 * p
        o = min(1.0, 2 * lin)
    elif name == "draw":
        d = p
    assert (st.x, st.y) == (pytest.approx(x, **APPROX), pytest.approx(y, **APPROX))
    assert st.scale == pytest.approx(s, **APPROX) and st.opacity == pytest.approx(min(max(o, 0), 1), **APPROX)
    assert st.draw == pytest.approx(d, **APPROX)


@pytest.mark.parametrize("name", list(anim.EXIT_PRESETS))
@pytest.mark.parametrize("q_in", [0.0, 0.5, 1.0])
def test_every_exit_preset_matches_its_closed_form(name, q_in):
    secs, easing = anim.EXIT_PRESETS[name]
    n = node(at=0.0, end=3.0, exit=preset_timing("exit", name))
    t = 3.0 + q_in * secs
    states = anim.resolve([n], t, D)
    q = ease(easing, q_in)
    if q_in == 1.0:
        assert states == []     # hidden from end + exit.seconds on
        return
    st = states[0]
    assert st.opacity == pytest.approx(1 - q, **APPROX)
    assert st.y == pytest.approx(200 + (q * D if name == "slide-out" else 0), **APPROX)
    assert st.scale == pytest.approx(1 - 0.4 * q if name == "pop-out" else 1.0, **APPROX)


def test_pulse_scales_by_a_sine_per_cycle_and_stops_after_its_repeats():
    n = node(at=0.0, emphasis=Pulse(2.0, 0.6, 2))
    assert one(n, 1.99).scale == 1.0 and one(n, 2.0).scale == pytest.approx(1.0)
    assert one(n, 2.15).scale == pytest.approx(1 + 0.08 * math.sin(math.pi * 0.25))
    assert one(n, 2.3).scale == pytest.approx(1.08)
    assert one(n, 2.6 + 0.3).scale == pytest.approx(1.08)     # the second cycle
    assert one(n, 3.2).scale == 1.0 and one(n, 9).scale == 1.0   # over: back to rest exactly


def test_a_slide_distance_is_passed_in_pixels():
    assert one(node(enter="slide-up"), 1.0, slide=30.0).y == pytest.approx(230.0)


def test_pop_and_back_overshoot_stay_clamped_in_opacity_and_peak_where_documented():
    n = node(enter="pop")
    peak = max(one(n, 1.0 + k * 0.45 / 200).scale for k in range(201))
    assert 1.03 < peak <= 1.0401           # 0.6 + 0.4 x 1.1
    fade_back = node(enter=Timing("fade", 0.4, "back"))
    ops = [one(fade_back, 1.0 + k * 0.4 / 100).opacity for k in range(1, 101)]
    assert max(ops) == 1.0 and min(ops) >= 0.0   # p reaches 1.1; opacity is clamped
    assert one(node(enter="slide-up"), 1.0).opacity == 0.0


# --- hidden before at, hidden after the exit ------------------------------------------------------------

def test_an_element_is_hidden_before_at_and_after_its_exit():
    n = node(at=1.0, enter="fade", end=2.0, exit=preset_timing("exit", "fade-out"))
    assert anim.resolve([n], 0.999) == []
    assert anim.resolve([n], 1.0)[0].opacity == 0.0
    assert anim.resolve([n], 1.5)[0].opacity == 1.0
    assert anim.resolve([n], 2.35) == [] and anim.resolve([n], 99) == []
    assert 0.0 < anim.resolve([n], 2.2)[0].opacity < 1.0


def test_end_without_an_exit_uses_fade_out():
    n = node(at=0.0, end=1.0)
    assert anim.resolve([n], 1.2)[0].opacity < 1.0 and anim.resolve([n], 1.35) == []


# --- moves ----------------------------------------------------------------------------------------------

def test_moves_interpolate_with_their_easing_and_carry_unset_fields_over():
    moves = (Move(2.0, 1.0, "linear", x=300.0), Move(3.5, 1.0, "in_out", y=50.0, scale=2.0),
             Move(5.0, 1.0, "linear", x=0.0))
    n = node(at=0.0, moves=moves)
    assert (one(n, 1.9).x, one(n, 1.9).y) == (100.0, 200.0)
    assert one(n, 2.5).x == pytest.approx(200.0) and one(n, 2.5).y == 200.0       # halfway along move 1
    assert one(n, 3.0).x == 300.0                                                  # between moves: R[0] holds
    st = one(n, 4.0)
    assert st.x == 300.0                                                           # x carries over
    assert st.y == pytest.approx(200 + (50 - 200) * ease("in_out", 0.5))
    assert st.scale == pytest.approx(1 + (2 - 1) * ease("in_out", 0.5))
    assert (one(n, 4.6).y, one(n, 4.6).scale) == (50.0, 2.0)
    assert one(n, 5.5).x == pytest.approx(150.0) and one(n, 5.5).scale == 2.0
    assert one(n, 99).x == 0.0


def test_a_move_with_back_easing_overshoots_and_lands_exactly():
    n = node(at=0.0, moves=(Move(1.0, 1.0, "back", x=400.0),))
    assert max(one(n, 1 + k / 100).x for k in range(101)) > 400.0
    assert one(n, 2.0).x == 400.0


def test_moves_start_after_the_enter_and_apply_to_the_rest_state_the_enter_changes():
    n = node(at=0.0, enter="slide-up", moves=(Move(1.0, 1.0, "linear", y=0.0),))
    assert one(n, 0.5).y == pytest.approx(200 + (1 - ease("out", 1.0)) * D)   # enter done: at rest
    assert one(n, 1.5).y == pytest.approx(100.0)


# --- composition: group children and split words --------------------------------------------------------

def test_a_child_composes_onto_its_parent_with_scale_rotation_and_opacity():
    kid = node(index=1, at=0.0, x=10.0, y=0.0, scale=2.0, rotate=10.0, opacity=0.5)
    parent = Node(0, "group", 0.0, preset_timing("enter", "none"), 100.0, 100.0, scale=3.0, rotate=90.0, opacity=0.5,
                  children=(kid,))
    states = anim.resolve([parent], 1.0)
    assert [s.node.index for s in states] == [0, 1]
    c = states[1]
    # offset (10, 0) x scale 3 = (30, 0), turned 90 degrees clockwise on screen (y down) = (0, 30)
    assert c.x == pytest.approx(100.0, abs=1e-9) and c.y == pytest.approx(130.0, abs=1e-9)
    assert c.scale == pytest.approx(6.0) and c.rotate == pytest.approx(100.0) and c.opacity == pytest.approx(0.25)
    assert c.root == 0


def test_a_hidden_parent_hides_its_children_and_a_child_waits_for_its_own_at():
    kid = node(index=1, at=2.0, x=0.0, y=0.0)
    parent = Node(0, "group", 1.0, preset_timing("enter", "none"), 50.0, 50.0, children=(kid,))
    assert anim.resolve([parent], 0.5) == []
    assert [s.node.index for s in anim.resolve([parent], 1.5)] == [0]
    assert [s.node.index for s in anim.resolve([parent], 2.0)] == [0, 1]
    ended = Node(0, "group", 0.0, preset_timing("enter", "none"), 50.0, 50.0, end=3.0, children=(kid,))
    assert anim.resolve([ended], 5.0) == []


def test_split_word_times_follow_the_stagger():
    assert anim.word_times(1.0, 4, 0.1) == pytest.approx([1.0, 1.1, 1.2, 1.3])
    assert anim.word_times(1.0, 0, 0.1) == []


def test_a_group_fading_out_multiplies_into_its_children():
    kid = node(index=1, at=0.0, x=0.0, y=0.0, opacity=0.8)
    parent = Node(0, "group", 0.0, preset_timing("enter", "none"), 0.0, 0.0, end=1.0,
                  exit=Timing("fade-out", 1.0, "linear"), children=(kid,))
    assert anim.resolve([parent], 1.5)[1].opacity == pytest.approx(0.8 * 0.5)


# --- auto times and timing rules ------------------------------------------------------------------------

def test_auto_times_count_only_the_elements_without_an_at():
    assert anim.auto_times([None, 2.0, None, None], 0.4) == pytest.approx([0.35, 2.0, 0.75, 1.15])
    assert anim.auto_times([], 0.4) == []
    assert anim.auto_times([None], 3.0) == [anim.AUTO_START]


def test_preset_timing_defaults_and_overrides():
    assert preset_timing("enter", "pop") == Timing("pop", 0.45, "back")
    assert preset_timing("enter", "pop", 1.0, "linear") == Timing("pop", 1.0, "linear")
    assert preset_timing("enter", "none", 5.0).seconds == 0.0
    assert preset_timing("exit", "slide-out") == Timing("slide-out", 0.4, "in_out")
    assert set(anim.EASING_NAMES) == {"linear", "out", "in_out", "back"}


def test_timing_problem_reports_the_first_broken_rule():
    assert anim.timing_problem(1.0, 0.25, 2.0, [], None) is None
    assert anim.timing_problem(1.0, 0.25, 1.0, [], None) == ("end", "1.0s is before the element has entered (1.25s)")
    assert anim.timing_problem(0.0, 0.4, None, [(0.2, 0.5)], None) == (
        "moves[0].at", "starts at 0.2s, before the element has entered (0.4s)")
    assert anim.timing_problem(0.0, 0.4, None, [(0.5, 0.9), (1.2, 0.3)], None) == (
        "moves[1].at", "starts at 1.2s, before the move before it ends (1.4s)")
    assert anim.timing_problem(0.0, 0.4, None, [(0.5, 0.5), (1.0, 0.3)], None) is None    # back to back is fine
    assert anim.timing_problem(0.0, 0.4, None, [], (0.1, 0.6, 1))[0] == "emphasis.at"
    assert anim.timing_problem(0.0, 0.4, 2.0, [], (1.0, 0.6, 3))[0] == "emphasis"
    assert anim.timing_problem(0.0, 0.4, 5.0, [], (None, 0.6, 2)) is None     # defaults to enter + 0.3


# --- quantisation and keys ------------------------------------------------------------------------------

def test_quantize_snaps_roots_to_pixels_and_members_to_their_root():
    kid = node(index=1, at=0.0, x=10.4, y=-3.6)
    parent = Node(0, "group", 0.0, preset_timing("enter", "none"), 100.4, 200.5, children=(kid,))
    q = anim.quantize(anim.resolve([parent], 1.0))
    assert (q[0].ix, q[0].iy) == (100, 201)
    assert (q[1].ix, q[1].iy) == (100 + 10, 201 - 4)       # the root's pixel plus the rounded offset


def test_quantised_values_use_the_documented_steps():
    n = node(at=0.0, scale=1.00123, rotate=12.34, opacity=0.5)
    q = anim.quantize(anim.resolve([n], 1.0))[0]
    assert q.sq == round(1.00123 * 512) and q.scale == q.sq / 512
    assert q.rq == round(12.34 * 20) and q.rotate == q.rq / 20          # 0.05 degree
    assert q.oq == round(0.5 * 255) and q.opacity == q.oq / 255
    assert q.dq == 1024 and q.draw == 1.0


def test_state_key_is_equal_for_equal_pictures_and_differs_when_anything_moves():
    n = node(at=0.0, enter="slide-up")
    a = anim.state_key(anim.quantize(anim.resolve([n], 5.0)))
    b = anim.state_key(anim.quantize(anim.resolve([n], 6.0)))
    assert a == b                                                         # at rest: the same picture
    c = anim.state_key(anim.quantize(anim.resolve([n], 0.25)))
    assert c != a
    # a sub-quantum change gives the same key
    s0 = anim.state_key(anim.quantize(anim.resolve([node(at=0.0, scale=1.0)], 1.0)))
    s1 = anim.state_key(anim.quantize(anim.resolve([node(at=0.0, scale=1.0 + 1 / 2048)], 1.0)))
    assert s0 == s1


def test_type_on_progress_is_whole_characters():
    n = node(at=0.0, enter=Timing("draw", 1.0, "linear"), draw_steps=10)
    assert [anim.quantize(anim.resolve([n], t))[0].dq for t in (0.0, 0.09, 0.1, 0.55, 1.0, 2.0)] == [0, 0, 1, 5, 10, 10]
    assert anim.quantize(anim.resolve([n], 0.55))[0].draw == 0.5


def test_resolve_is_pure_and_order_independent():
    kid = node(index=1, at=0.5, x=5.0, y=5.0, enter="pop")
    tree = [Node(0, "group", 0.0, preset_timing("enter", "slide-up"), 10.0, 10.0, children=(kid,)), node(index=2, at=1.0, enter="fade")]
    times = [0.0, 0.3, 0.7, 1.0, 1.2, 2.5]
    forward = [anim.resolve(tree, t) for t in times]
    for t in reversed(times):
        assert anim.resolve(tree, t) == forward[times.index(t)]
