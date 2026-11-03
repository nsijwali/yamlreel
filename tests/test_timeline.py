"""The `timeline` scene: validation, the window and count logic (pure) and smoke renders in both orientations."""
from __future__ import annotations

import pytest

from yamlreel.datascenes import (MAX_TIMELINE_EVENTS, WINDOW_MOVE, TimelineScene, count_in_window, event_times,
                                 timeline_count, window_range, window_segments)
from yamlreel.render import Timeline
from yamlreel.specs import SpecError, validate

from test_validation import _literal_keys

TIMELINE = {"type": "timeline", "title": "Requests", "seconds": 10, "count": True,
            "axis": {"from": 0, "to": 120, "unit": "s", "ticks": 30},
            "events": [{"x": 5, "at": 0.4}, {"x": 22, "at": 0.6, "label": "req"}, {"x": 71, "at": 0.8, "color": "accent"},
                       {"x": 100, "at": 6.0}],
            "window": [{"at": 1.0, "range": [0, 60]}, {"at": 3.0, "range": [40, 100], "text": "slides"}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def check(**kw):
    return validate({"scenes": [{**TIMELINE, **kw}]})["scenes"][0]


@pytest.mark.parametrize("kw,msg", [
    ({"axis": {"from": 10, "to": 10}}, "scenes[0].axis.from must be less than scenes[0].axis.to"),
    ({"axis": {"from": 20, "to": 10}}, "scenes[0].axis.from must be less than"),
    ({"axis": {"from": 0}}, "scenes[0].axis: needs to"),
    ({"axis": {"to": 5}}, "scenes[0].axis: needs from"),
    ({"axis": {"from": 0, "to": 10, "ticks": 0}}, "scenes[0].axis.ticks must be a number greater than 0"),
    ({"axis": {"from": 0, "to": 10, "ticks": -2}}, "scenes[0].axis.ticks must be a number"),
    ({"axis": {"from": 0, "to": 100, "ticks": 1}}, "scenes[0].axis.ticks 1 would draw more than 24 ticks"),
    ({"axis": {"from": 0, "to": 10, "step": 2}}, "scenes[0].axis"),
    ({"axis": {"from": "a", "to": 10}}, "scenes[0].axis.from must be a number"),
    ({"axis": 5}, "scenes[0].axis must be a mapping"),
    ({"events": []}, "scenes[0].events must have at least 1 entry"),
    ({"events": [{"at": 1}]}, "scenes[0].events[0]: needs x"),
    ({"events": [{"x": 121}]}, "scenes[0].events[0].x must be a number between 0 and 120"),
    ({"events": [{"x": -1}]}, "scenes[0].events[0].x must be a number between 0 and 120"),
    ({"events": [{"x": 5, "color": "pink"}]}, "scenes[0].events[0].color must be one of"),
    ({"events": [{"x": 5, "size": 3}]}, "scenes[0].events[0]"),
    ({"events": [{"x": 5, "at": -1}]}, "scenes[0].events[0].at must be a number"),
    ({"events": [{"x": 5, "label": ["a"]}]}, "scenes[0].events[0].label must be text"),
    ({"events": [{"x": i} for i in range(MAX_TIMELINE_EVENTS + 1)]}, "scenes[0].events has 61 entries; the limit is 60"),
    ({"window": [{"at": 1, "range": [0, 121]}]}, "scenes[0].window[0].range[1] must be a number between 0 and 120"),
    ({"window": [{"at": 1, "range": [-5, 50]}]}, "scenes[0].window[0].range[0] must be a number between 0 and 120"),
    ({"window": [{"at": 1, "range": [50, 50]}]}, "scenes[0].window[0].range must be [low, high] with low < high"),
    ({"window": [{"at": 1, "range": [60, 10]}]}, "scenes[0].window[0].range must be [low, high] with low < high"),
    ({"window": [{"at": 1, "range": [1, 2, 3]}]}, "scenes[0].window[0].range must be [low, high]"),
    ({"window": [{"at": 1}]}, "scenes[0].window[0]: needs range"),
    ({"window": [{"range": [0, 5]}]}, "scenes[0].window[0]: needs at"),
    ({"window": [{"at": 3, "range": [0, 5]}, {"at": 2, "range": [0, 5]}]}, "scenes[0].window[1].at must not be earlier"),
    ({"window": [{"at": 1, "range": [0, 5], "color": "red"}]}, "scenes[0].window[0]"),
    ({"count": "yes"}, "scenes[0].count must be true or false"),
])
def test_bad_timelines_are_rejected(kw, msg):
    with pytest.raises(SpecError) as e:
        check(**kw)
    assert msg in str(e.value)


def test_required_fields_and_defaults():
    with pytest.raises(SpecError, match="missing axis, events"):
        validate({"scenes": [{"type": "timeline"}]})
    sc = validate({"scenes": [{"type": "timeline", "axis": {"from": 0, "to": 4}, "events": [{"x": 4}]}]})["scenes"][0]
    assert sc["seconds"] == 10.0 and "window" not in sc and "count" not in sc
    assert len(check(events=[{"x": 0}] * MAX_TIMELINE_EVENTS)["events"]) == MAX_TIMELINE_EVENTS
    assert check(window=[{"at": 1, "range": [0, 120]}])["window"][0]["range"] == [0, 120]


def test_events_without_at_are_staggered_in_order():
    assert event_times([{"x": 1}, {"x": 2, "at": 5}, {"x": 3}]) == pytest.approx([0.3, 5.0, 0.45])


def test_window_is_absent_then_eased_between_steps():
    w = check()["window"]
    assert window_range(w, 0.9) is None
    assert window_range(w, 1.0) == (0, 60) and window_range(w, 2.9) == (0, 60)
    mid = window_range(w, 3.0 + WINDOW_MOVE / 2)
    assert mid == pytest.approx((20, 80))   # in_out passes through the midpoint
    lo = [window_range(w, 3.0 + i / 20 * WINDOW_MOVE)[0] for i in range(21)]
    assert lo == sorted(lo) and 0 < lo[5] < lo[10] < lo[15] < 40
    assert window_range(w, 4.0) == (40, 100) and window_range(w, 99) == (40, 100)


def test_a_step_during_a_move_starts_from_where_the_window_is():
    w = [{"at": 0, "range": [0, 10]}, {"at": 1, "range": [20, 30]}, {"at": 1 + WINDOW_MOVE / 2, "range": [0, 10]}]
    segs = window_segments(w)
    assert (segs[2].start_lo, segs[2].start_hi) == pytest.approx((10, 20))
    assert window_range(w, 1 + WINDOW_MOVE / 2, segs) == pytest.approx((10, 20))   # no jump


def test_window_segments_reject_unordered_steps():
    with pytest.raises(ValueError, match="window\\[1\\].at"):
        window_segments([{"at": 2, "range": [0, 1]}, {"at": 1, "range": [0, 1]}])


def test_count_counts_only_appeared_events_inside_the_range():
    ev = check()["events"]
    assert count_in_window(ev, 5, None) == 0
    assert count_in_window(ev, 0.5, (0, 60)) == 1            # only x=5 has appeared
    assert count_in_window(ev, 1.0, (0, 60)) == 2            # x=5 and x=22
    assert count_in_window(ev, 1.0, (0, 120)) == 3           # x=100 has not appeared yet
    assert count_in_window(ev, 7.0, (0, 120)) == 4
    assert count_in_window(ev, 7.0, (22, 71)) == 2            # both ends are inclusive
    assert count_in_window(ev, 7.0, (23, 70)) == 0


def test_count_follows_the_window_as_it_slides():
    sc = check()
    assert timeline_count(sc, 0.9) == 0                       # no window yet
    assert timeline_count(sc, 2.0) == 2                       # x=5, 22 in [0, 60]
    assert timeline_count(sc, 3.0 + WINDOW_MOVE) == 1         # [40, 100]: x=71 (x=100 appears at 6.0 and x=22 has left)
    assert timeline_count(sc, 6.0) == 2                       # x=100 appears, still inside
    counts = [timeline_count(sc, 3.0 + i * 0.05) for i in range(17)]
    assert counts[0] == 2 and counts[-1] == 1 and set(counts) <= {1, 2}


def test_timeline_reads_only_declared_fields():
    read = _literal_keys(TimelineScene.__init__, {"spec"})
    assert read and read <= TimelineScene.FIELDS
    assert _literal_keys(TimelineScene.__init__, {"axis"}) <= TimelineScene.AXIS_FIELDS
    assert _literal_keys(TimelineScene.__init__, {"ev"}) <= TimelineScene.EVENT_FIELDS
    assert _literal_keys(window_segments, {"w"}) <= TimelineScene.WINDOW_FIELDS
    assert _literal_keys(count_in_window, {"ev"}) <= TimelineScene.EVENT_FIELDS


def scene(tmp_path, fmt="landscape", scale=0.5, **kw) -> TimelineScene:
    tl = Timeline(validate({"scenes": [{**TIMELINE, **kw}]}), fmt, tmp_path, scale=scale)
    sc = tl.scenes[0]
    assert isinstance(sc, TimelineScene)
    return sc


@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
def test_renders_events_window_and_count(tmp_path, fmt):
    sc = scene(tmp_path, fmt)
    frames = [sc.frame(t) for t in (0.2, 2.0, 3.4, 8.0)]   # empty, first window, sliding, settled
    assert all(f.size == (sc.ctx.W, sc.ctx.H) for f in frames)
    assert len({f.tobytes() for f in frames}) == 4
    assert sc.cached_layers() == 1 and sc.ctx.warnings == []


def test_dense_events_stack_into_lanes_inside_the_chart(tmp_path):
    events = [{"x": 50 + i * 0.1, "at": 0.1 * i, "label": "r"} for i in range(30)]
    sc = scene(tmp_path, "vertical", scale=1.0, events=events)
    assert all(sc.band_top <= y <= sc.axis_y for _, y in sc.dots)
    assert len({round(y) for _, y in sc.dots}) > 1             # they did not all land on one row
    assert sc.frame(5.0).size == (1080, 1920)


def test_events_far_apart_share_one_row(tmp_path):
    sc = scene(tmp_path, events=[{"x": 10}, {"x": 60}, {"x": 110}])
    assert len({y for _, y in sc.dots}) == 1


def test_warns_when_a_step_ends_after_the_scene(tmp_path):
    tl = Timeline(validate({"scenes": [{**TIMELINE, "seconds": 3.5}]}), "landscape", tmp_path, scale=0.5)
    assert any("timeline event or window step ends after the scene" in w for w in tl.ctx.warnings)


def test_a_timeline_without_a_window_or_count_renders(tmp_path):
    spec = {k: v for k, v in TIMELINE.items() if k not in ("window", "count")}
    tl = Timeline(validate({"scenes": [spec]}), "vertical", tmp_path, scale=0.5)
    assert tl.scenes[0].frame(2.0).size == (tl.ctx.W, tl.ctx.H)
