"""The `meter` scene: validation, the pure level function and a smoke render of each shape."""
from __future__ import annotations

import pytest

from yamlreel.datascenes import MeterScene, meter_level, meter_segments
from yamlreel.render import Timeline
from yamlreel.specs import SpecError, validate

from test_validation import _literal_keys

METER = {"type": "meter", "capacity": 10, "start": 10, "seconds": 8,
         "steps": [{"at": 1, "level": 4, "seconds": 1}, {"at": 4, "level": 0, "seconds": 1}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def check(**kw):
    return validate({"scenes": [{**METER, **kw}]})["scenes"][0]


@pytest.mark.parametrize("kw,msg", [
    ({"shape": "tank"}, "scenes[0].shape must be one of bucket, bar, slots"),
    ({"capacity": 0}, "scenes[0].capacity must be a number greater than 0"),
    ({"capacity": -3}, "scenes[0].capacity must be a number between 0"),
    ({"capacity": "ten"}, "scenes[0].capacity must be a number"),
    ({"shape": "slots", "capacity": 21}, "scenes[0].capacity must be an integer between 1 and 20"),
    ({"shape": "slots", "capacity": 2.5}, "scenes[0].capacity must be an integer between 1 and 20"),
    ({"start": 11}, "scenes[0].start must be a number between 0 and 10"),
    ({"start": -1}, "scenes[0].start must be a number between 0 and 10"),
    ({"steps": [{"at": 1, "level": 11}]}, "scenes[0].steps[0].level must be a number between 0 and 10"),
    ({"steps": [{"at": 1, "level": -1}]}, "scenes[0].steps[0].level must be a number between 0 and 10"),
    ({"steps": [{"at": -1, "level": 1}]}, "scenes[0].steps[0].at must be a number between 0 and"),
    ({"steps": [{"level": 1}]}, "scenes[0].steps[0]: needs at"),
    ({"steps": [{"at": 1}]}, "scenes[0].steps[0]: needs level"),
    ({"steps": [{"at": 3, "level": 1}, {"at": 2, "level": 2}]}, "scenes[0].steps[1].at must not be earlier"),
    ({"steps": [{"at": 1, "level": 1, "speed": 2}]}, "scenes[0].steps[0]"),
    ({"steps": [{"at": 1, "level": 1, "color": "pink"}]}, "scenes[0].steps[0].color must be one of"),
    ({"steps": [{"at": 1, "level": 1, "seconds": 0}]}, "scenes[0].steps[0].seconds must be a number"),
    ({"steps": "none"}, "scenes[0].steps must be a list"),
    ({"unit": ["x"]}, "scenes[0].unit must be text"),
    ({"bogus": 1}, "scenes[0]"),
])
def test_bad_meters_are_rejected(kw, msg):
    with pytest.raises(SpecError) as e:
        check(**kw)
    assert msg in str(e.value)


def test_capacity_is_required_and_defaults_apply():
    with pytest.raises(SpecError, match="missing capacity"):
        validate({"scenes": [{"type": "meter"}]})
    sc = validate({"scenes": [{"type": "meter", "capacity": 5}]})["scenes"][0]
    assert sc["seconds"] == 10.0


def test_slots_may_use_the_full_twenty():
    assert check(shape="slots", capacity=20, start=20, steps=[])["capacity"] == 20


def test_level_is_start_then_eased_between_steps():
    sc = check()
    assert meter_level(sc, 0) == 10 and meter_level(sc, 0.99) == 10
    assert meter_level(sc, 1.5) == pytest.approx(7)  # halfway: in_out passes through the midpoint
    assert 7 < meter_level(sc, 1.25) < 10 and 4 < meter_level(sc, 1.75) < 7
    assert meter_level(sc, 2.0) == pytest.approx(4) and meter_level(sc, 3.9) == 4
    assert meter_level(sc, 4.5) == pytest.approx(2) and meter_level(sc, 6) == 0
    levels = [meter_level(sc, 1 + i / 20) for i in range(21)]
    assert levels == sorted(levels, reverse=True)  # drains monotonically


def test_default_seconds_and_mid_move_start():
    spec = {"capacity": 10, "start": 0, "steps": [{"at": 0, "level": 10}, {"at": 0.4, "level": 0}]}
    first, second = meter_segments(spec)
    assert first.t1 == pytest.approx(0.8)  # default 0.8 s
    assert second.start_level == pytest.approx(5)  # begins from where the first move had got to, no jump
    assert meter_level(spec, 0.4) == pytest.approx(5)


def test_a_meter_with_no_steps_holds_start():
    assert meter_level({"capacity": 4, "start": 3}, 99) == 3


def test_meter_reads_only_declared_fields():
    read = _literal_keys(MeterScene.__init__, {"spec"})
    assert read and read <= MeterScene.FIELDS
    assert _literal_keys(meter_segments, {"step"}) <= MeterScene.STEP_FIELDS
    assert {"at", "level", "seconds", "text", "color"} == set(MeterScene.STEP_FIELDS)


def scene(tmp_path, fmt="landscape", **kw) -> MeterScene:
    tl = Timeline(validate({"scenes": [{**METER, "title": "Tokens", "unit": "tokens", "in_label": "refill",
                                        "out_label": "requests", **kw}]}), fmt, tmp_path, scale=0.5)
    sc = tl.scenes[0]
    assert isinstance(sc, MeterScene)
    return sc


@pytest.mark.parametrize("shape,cap", [("bucket", 10), ("bar", 10), ("slots", 8)])
@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
def test_each_shape_renders(tmp_path, shape, cap, fmt):
    sc = scene(tmp_path, fmt, shape=shape, capacity=cap, start=cap,
               steps=[{"at": 1, "level": 2, "seconds": 1, "text": "burst"}, {"at": 4, "level": cap, "seconds": 2}])
    frames = [sc.frame(t) for t in (0.5, 1.5, 5.0)]  # idle, draining, refilling
    assert all(f.size == (sc.ctx.W, sc.ctx.H) for f in frames)
    assert frames[0].tobytes() != frames[1].tobytes() != frames[2].tobytes()
    assert sc.cached_layers() == 1  # the chrome is drawn once


def test_drops_follow_the_direction_of_the_move(tmp_path):
    sc = scene(tmp_path)
    assert sc.motion_at(0.5) == (0, 0.0)
    assert sc.motion_at(1.5)[0] == -1 and sc.motion_at(1.5)[1] == 1.0
    assert sc.motion_at(3.0) == (0, 0.0)
    assert scene(tmp_path, steps=[{"at": 1, "level": 10, "seconds": 2}], start=2).motion_at(2)[0] == 1


def test_warns_when_a_step_ends_after_the_scene(tmp_path):
    tl = Timeline(validate({"scenes": [{**METER, "steps": [{"at": 7, "level": 1, "seconds": 3}]}]}),
                  "landscape", tmp_path, scale=0.5)
    assert any("meter step ends after the scene" in w for w in tl.ctx.warnings)
