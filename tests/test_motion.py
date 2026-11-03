"""Motion core (S1): transitions, easing, stagger, count-up numbers and contact frames."""
from __future__ import annotations

import pytest
import yaml
from PIL import Image, ImageChops

from yamlreel import legacy, render as render_mod
from yamlreel.cli import main
from yamlreel.render import RenderError, Timeline, contact_fracs, contact_sheet, ffmpeg_exe
from yamlreel.scenes import Ctx, Scene, StatScene
from yamlreel.scenebase import Ctx as BaseCtx, Scene as BaseScene
from yamlreel.specs import SpecError, validate
from yamlreel.textkit import EASINGS, ease, ease_in_out, ease_out, split_count


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never read the developer's own config; run from a scratch folder."""
    for prefix in ("YAMLREEL", legacy.LEGACY.upper()):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)
    legacy._warned.clear()


TITLE = {"type": "title", "title": "First", "seconds": 2}
BULLETS = {"type": "bullets", "title": "Steps", "items": ["one", "two", "three"], "seconds": 5}
STAT = {"type": "stat", "value": "10,000,000 rows", "label": "scanned", "seconds": 4, "count": True}


def timeline(tmp_path, *scenes, fmt="landscape", **extra) -> Timeline:
    return Timeline(validate({"scenes": list(scenes), **extra}), fmt, tmp_path, scale=0.25)


def same(a: Image.Image, b: Image.Image) -> bool:
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


# --- the move to scenebase ------------------------------------------------------------------------------

def test_scenes_still_reexports_the_base_classes():
    assert Ctx is BaseCtx and Scene is BaseScene


# --- easing ---------------------------------------------------------------------------------------------

def test_easings_are_anchored_and_clamped():
    assert set(EASINGS) == {"linear", "out", "in_out", "back"}
    for name in EASINGS:
        assert ease(name, 0) == pytest.approx(0)
        assert ease(name, 1) == pytest.approx(1)
        assert ease(name, -3) == pytest.approx(0) and ease(name, 9) == pytest.approx(1)
    assert ease("linear", 0.3) == pytest.approx(0.3)
    assert ease("out", 0.4) == ease_out(0.4) and ease("in_out", 0.4) == ease_in_out(0.4)
    assert max(ease("back", k / 100) for k in range(101)) > 1.0  # it overshoots, then settles
    with pytest.raises(KeyError):
        ease("bounce", 0.5)


# --- transitions ----------------------------------------------------------------------------------------

def test_default_fade_equals_image_blend_pixel_for_pixel(tmp_path):
    tl = timeline(tmp_path, TITLE, BULLETS)
    lt = 0.1
    old = tl._scene_frame(0, tl.scenes[0].duration - 1 / 30)
    new = tl._scene_frame(1, lt)
    assert same(tl.frame_at(tl.starts[1] + lt), Image.blend(old, new, lt / 0.25))
    assert same(tl.frame_at(tl.starts[1] + 0.3), tl._scene_frame(1, 0.3))  # fade over: just the scene


def test_cut_returns_the_current_frame(tmp_path):
    tl = timeline(tmp_path, TITLE, {**BULLETS, "transition": "cut"})
    assert same(tl.frame_at(tl.starts[1] + 0.05), tl._scene_frame(1, 0.05))


def test_slide_pushes_the_old_frame_out_to_the_left():
    old = Image.new("RGB", (100, 40), (255, 0, 0))
    old.paste((0, 255, 0), (50, 0, 100, 40))   # left red, right green
    new = Image.new("RGB", (100, 40), (0, 0, 255))
    new.paste((255, 255, 0), (50, 0, 100, 40))  # left blue, right yellow
    out = Timeline._transition("slide", old, new, 0.5)  # in_out(0.5) = 0.5: shifted by half the width
    assert out.getpixel((10, 20)) == (0, 255, 0)    # the old frame's right half is now in the left half
    assert out.getpixel((90, 20)) == (0, 0, 255)    # the new frame's left half has entered on the right
    assert Timeline._transition("slide", old, new, 0.0).getpixel((10, 20)) == (255, 0, 0)
    assert Timeline._transition("slide", old, new, 1.0).getpixel((90, 20)) == (255, 255, 0)


def test_slide_runs_through_the_timeline(tmp_path):
    tl = timeline(tmp_path, TITLE, {**BULLETS, "transition": {"type": "slide", "seconds": 0.4}})
    mid = tl.frame_at(tl.starts[1] + 0.2)
    assert mid.size == (tl.ctx.W, tl.ctx.H)
    assert not same(mid, tl._scene_frame(1, 0.2))
    assert same(tl.frame_at(tl.starts[1] + 0.4), tl._scene_frame(1, 0.4))  # done after `seconds`


def test_zoom_stays_in_frame_and_ends_on_the_new_frame():
    old = Image.new("RGB", (160, 90), (10, 10, 10))
    new = Image.new("RGB", (160, 90), (200, 100, 50))
    new.paste((0, 0, 0), (0, 0, 80, 45))
    for a in (0.0, 0.2, 0.5, 0.99, 1.0):
        out = Timeline._transition("zoom", old, new, a)
        assert out.size == new.size
    assert same(Timeline._transition("zoom", old, new, 0.0), old)
    assert same(Timeline._transition("zoom", old, new, 1.0), new)
    # mid-way the far corner still shows the new frame's plain colour, blended half with the old one
    mid = Timeline._transition("zoom", old, new, 0.5)
    assert all(abs(got - want) <= 1 for got, want in zip(mid.getpixel((159, 89)), (105, 55, 30)))
    assert not same(mid, Image.blend(old, new, 0.5))  # and the black quadrant has been scaled up, so it is not a plain fade


def test_zoom_enlarges_the_new_frame_around_its_centre():
    """With old == new a plain blend returns the image unchanged, so any difference is the zoom itself."""
    img = Image.new("RGB", (160, 90), (0, 0, 0))
    for x in range(0, 160, 16):
        img.paste((255, 255, 255), (x, 0, x + 4, 90))  # vertical stripes
    assert same(Timeline._transition("fade", img, img, 0.3), img)  # the control: no zoom, no change
    assert same(Timeline._transition("zoom", img, img, 1.0), img)  # settled
    zoomed = Timeline._transition("zoom", img, img, 0.05)
    assert not same(zoomed, img)


def test_last_holds_at_most_one_frame(tmp_path):
    tl = timeline(tmp_path, TITLE, BULLETS, STAT, TITLE)
    t = 0.0
    while t < tl.total:
        tl.frame_at(t)
        assert len(tl._last) <= 1
        t += 0.1
    assert max(tl._last) == len(tl.scenes) - 2


def test_cut_never_renders_the_previous_scene(tmp_path):
    tl = timeline(tmp_path, TITLE, BULLETS, transition="cut")
    tl.frame_at(tl.starts[1] + 0.05)
    assert tl._last == {}


def test_top_level_default_and_scene_override(tmp_path):
    spec = validate({"transition": {"type": "zoom", "seconds": 0.5},
                     "scenes": [TITLE, BULLETS, {**STAT, "transition": "cut"}]})
    assert spec["transition"] == {"type": "zoom", "seconds": 0.5}
    assert spec["scenes"][2]["transition"] == {"type": "cut", "seconds": 0.5}  # seconds come from the default
    tl = Timeline(spec, "landscape", tmp_path, scale=0.25)
    assert [t["type"] for t in tl._transitions] == ["zoom", "zoom", "cut"]


def test_transition_defaults_to_a_quarter_second_fade():
    spec = validate({"scenes": [TITLE]})
    assert spec["transition"] == {"type": "fade", "seconds": 0.25}
    assert validate({"transition": "slide", "scenes": [TITLE]})["transition"] == {"type": "slide", "seconds": 0.25}


@pytest.mark.parametrize("bad", [
    {"type": "fade", "seconds": 0.05}, {"type": "slide", "seconds": 1.5}, {"type": "zoom", "seconds": "slow"},
    {"type": "wipe"}, "wipe", {}, {"type": "fade", "speed": 1}, 3, ["fade"],
])
def test_bad_transitions_are_rejected(bad):
    if bad != {}:  # a top-level {} merges into the default; on a scene it is an error
        with pytest.raises(SpecError, match="transition"):
            validate({"transition": bad, "scenes": [TITLE]})
    with pytest.raises(SpecError, match=r"scenes\[1\]\.transition"):
        validate({"scenes": [TITLE, {**TITLE, "transition": bad}]})


def test_transition_seconds_message_names_the_range():
    with pytest.raises(SpecError, match=r"scenes\[0\]\.transition\.seconds must be a number between 0\.1 and 1"):
        validate({"scenes": [{**TITLE, "transition": {"type": "fade", "seconds": 2}}]})


# --- stagger --------------------------------------------------------------------------------------------

def test_stagger_changes_when_bullets_appear(tmp_path):
    fast = timeline(tmp_path, {**BULLETS, "stagger": 0.1})
    slow = timeline(tmp_path, {**BULLETS, "stagger": 3.0})
    default = timeline(tmp_path, BULLETS)
    t = 1.3  # the third bullet is up with a 0.1 s stagger and not started with a 3 s one
    assert not same(fast.frame_at(t), slow.frame_at(t))
    assert not same(fast.frame_at(t), default.frame_at(t))
    assert same(fast.frame_at(5.0 - 1 / 30), default.frame_at(5.0 - 1 / 30))  # all bullets in by the end
    assert default.scenes[0].stagger is None and fast.scenes[0].stagger == 0.1


def test_unset_stagger_keeps_the_computed_step(tmp_path):
    a = timeline(tmp_path, BULLETS)
    b = timeline(tmp_path, {**BULLETS, "stagger": 1.1})  # min(1.1, (5 - 1.2) / 3) = 1.1 is the old step here
    assert same(a.frame_at(2.0), b.frame_at(2.0))


@pytest.mark.parametrize("bad", [0.05, 3.5, "x", True, {}])
def test_bad_stagger_is_rejected(bad):
    with pytest.raises(SpecError, match=r"scenes\[0\]\.stagger"):
        validate({"scenes": [{**BULLETS, "stagger": bad}]})


def test_stagger_is_only_for_scenes_that_have_it():
    with pytest.raises(SpecError, match="unknown field"):
        validate({"scenes": [{**TITLE, "stagger": 0.5}]})


# --- count-up -------------------------------------------------------------------------------------------

def stat_scene(tmp_path, **kw) -> StatScene:
    tl = timeline(tmp_path, {**STAT, **kw})
    sc = tl.scenes[0]
    assert isinstance(sc, StatScene)
    return sc


def test_count_starts_at_zero_passes_through_formatted_values_and_ends_exact(tmp_path):
    sc = stat_scene(tmp_path)
    assert sc.shown_text(0.0) == "0 rows"
    mid = sc.shown_text(1.2)
    number = mid.removesuffix(" rows")
    assert 0 < int(number.replace(",", "")) < 10_000_000 and "," in number
    assert sc.shown_text(3.9) == "10,000,000 rows"
    assert sc.shown_text(0.0) != sc.shown_text(1.2) != sc.shown_text(3.9)
    # monotonic
    vals = [int(sc.shown_text(k * 0.05).removesuffix(" rows").replace(",", "")) for k in range(80)]
    assert vals == sorted(vals)


def test_count_frames_differ_and_the_last_one_equals_a_plain_stat(tmp_path):
    counting = timeline(tmp_path, STAT)
    plain = timeline(tmp_path, {k: v for k, v in STAT.items() if k != "count"})
    assert not same(counting.frame_at(1.0), plain.frame_at(1.0))
    assert same(counting.frame_at(3.9), plain.frame_at(3.9))


def test_count_from_and_seconds_and_formats(tmp_path):
    sc = stat_scene(tmp_path, value="$12.50 saved", count={"from": 10, "seconds": 2})
    assert sc.shown_text(0.0) == "$10.00 saved"
    assert sc.shown_text(3.0) == "$12.50 saved"
    down = stat_scene(tmp_path, value="-5%", count={"from": 20, "seconds": 2})
    assert down.shown_text(0.0) == "20%" and down.shown_text(3.9) == "-5%"
    signed = stat_scene(tmp_path, value="+40 pts", count={"from": 0, "seconds": 2})
    assert signed.shown_text(0.0) == "+0 pts"
    assert signed.shown_text(3.9) == "+40 pts"


def test_count_never_shows_a_negative_zero(tmp_path):
    sc = stat_scene(tmp_path, value="5 left", count={"from": -0.4, "seconds": 2})
    assert not sc.shown_text(0.0).startswith("-")


def test_a_user_string_never_reaches_str_format(tmp_path):
    sc = stat_scene(tmp_path, value="{a.__class__} 7 {x}", count={"from": 0, "seconds": 2})
    assert sc.shown_text(0.0) == "{a.__class__} 0 {x}"
    assert sc.shown_text(3.9) == "{a.__class__} 7 {x}"
    sc.frame(0.5)  # renders without a format error


def test_count_value_block_cache_is_bounded_and_reused(tmp_path):
    sc = stat_scene(tmp_path)
    for k in range(200):
        sc.frame(0.4 + k * 0.01)
    assert len(sc._blocks) <= StatScene.COUNT_CACHE
    text = sc.shown_text(1.0)
    assert sc._value_block(text) is sc._value_block(text)


def test_count_false_is_a_plain_stat(tmp_path):
    sc = stat_scene(tmp_path, count=False)
    assert sc.count is None and sc.shown_text(0.0) == "10,000,000 rows"


@pytest.mark.parametrize("value", ["no digits here", "3 of 10", "v2 build 7", "1-2"])
def test_count_needs_exactly_one_number(value):
    with pytest.raises(SpecError) as e:
        validate({"scenes": [TITLE, TITLE, TITLE, {**STAT, "value": value}]})
    assert str(e.value) == "scenes[3].count: value must contain exactly one number to count"


@pytest.mark.parametrize("count,msg", [
    ({"from": "x"}, r"count\.from must be a number"), ({"seconds": 0}, r"count\.seconds must be a number"),
    ({"to": 5}, "unknown field"), ({}, "count must be"), ("yes", "count must be a mapping"),
])
def test_bad_count_is_rejected(count, msg):
    with pytest.raises(SpecError, match=msg):
        validate({"scenes": [{**STAT, "count": count}]})


def test_count_rejects_unbounded_numbers():
    with pytest.raises(SpecError, match="too large"):
        validate({"scenes": [{**STAT, "value": "9" * 40}]})
    with pytest.raises(SpecError, match="decimals"):
        validate({"scenes": [{**STAT, "value": "1.1234567"}]})


def test_count_is_only_for_stat_scenes():
    with pytest.raises(SpecError, match="unknown field"):
        validate({"scenes": [{**BULLETS, "count": True}]})


def test_count_that_cannot_finish_warns(tmp_path):
    tl = timeline(tmp_path, {**STAT, "seconds": 2, "count": {"seconds": 5}})
    assert any("does not finish" in w for w in tl.warnings)


def test_split_count_parts():
    p = split_count("About 1,250.5 ms")
    assert (p.prefix, p.suffix, p.target, p.decimals, p.grouped) == ("About ", " ms", 1250.5, 1, True)
    assert p.format(3.14159) == "About 3.1 ms"  # no separator below 1000
    assert p.format(1234567.0) == "About 1,234,567.0 ms"
    with pytest.raises(ValueError):
        split_count("a 1 b 2")


# --- contact frames -------------------------------------------------------------------------------------

def test_contact_fracs():
    assert contact_fracs(None) == (0.35, 0.92)
    assert len(contact_fracs(4)) == 4 and list(contact_fracs(6)) == sorted(contact_fracs(6))
    assert all(0 < f < 1 for f in contact_fracs(8)) and contact_fracs(1) == (0.92,)
    assert contact_fracs(2) == contact_fracs(None) == (0.35, 0.92)
    for bad in (0, 9, 13, -1):
        with pytest.raises(RenderError, match="contact frames"):
            contact_fracs(bad)


def test_contact_sheet_has_the_requested_tiles_per_scene(tmp_path):
    tl = timeline(tmp_path, TITLE, BULLETS, STAT)
    four = Image.open(contact_sheet(tl, tmp_path / "four.png", cols=4, fracs=contact_fracs(4)))
    assert four.width == 4 * 480
    tile_h = int(480 * tl.ctx.H / tl.ctx.W)
    assert four.height == 3 * (tile_h + 24)  # one row of 4 tiles per scene
    default = Image.open(contact_sheet(tl, tmp_path / "default.png"))
    assert default.width == 4 * 480 and default.height == 2 * (tile_h + 24)  # 6 tiles, as before
    two = Image.open(contact_sheet(tl, tmp_path / "two.png", fracs=contact_fracs(2)))
    assert two.size == default.size and same(two, default)  # N=2 is the default sheet
    eight = Image.open(contact_sheet(tl, tmp_path / "eight.png", fracs=contact_fracs(8)))
    assert eight.width == 8 * 320 and eight.height == 3 * (int(320 * tl.ctx.H / tl.ctx.W) + 24)


def test_contact_frames_flag_on_the_command_line(tmp_path, capsys):
    if ffmpeg_exe() is None:
        pytest.skip("no ffmpeg")
    spec = tmp_path / "s.yaml"
    spec.write_text(yaml.safe_dump({"music": {"style": "none"}, "scenes": [{**TITLE, "seconds": 1}, {**STAT, "seconds": 1}]}))
    out = tmp_path / "o"
    assert main(["render", str(spec), "--out", str(out), "--format", "square", "--preview", "--no-voice",
                 "--contact-frames", "4"]) == 0
    sheet = Image.open(out / "square" / "contact.png")
    assert sheet.width == 4 * 480
    assert sheet.height == 2 * (480 + 24)  # two scenes, one row each
    assert main(["render", str(spec), "--out", str(out), "--contact-frames", "9"]) == 1
    assert "--contact-frames must be an integer between 1 and 8" in capsys.readouterr().err
    assert render_mod.MAX_CONTACT_FRAMES == 8


# --- review fixes ---------------------------------------------------------------------------------------

def test_a_scene_transition_that_sets_one_field_inherits_the_other():
    top = {"transition": {"type": "slide", "seconds": 0.6}}
    spec = validate({**top, "scenes": [TITLE, {**TITLE, "transition": {"seconds": 0.4}},
                                       {**TITLE, "transition": {"type": "zoom"}}, {**TITLE, "transition": "cut"}]})
    assert [s["transition"] for s in spec["scenes"][1:]] == [
        {"type": "slide", "seconds": 0.4}, {"type": "zoom", "seconds": 0.6}, {"type": "cut", "seconds": 0.6}]
    plain = validate({"scenes": [TITLE, {**TITLE, "transition": {"seconds": 0.4}}]})
    assert plain["scenes"][1]["transition"] == {"type": "fade", "seconds": 0.4}


def test_a_string_transition_in_config_and_spec_merges_with_a_mapping(tmp_path):
    from yamlreel.config import Config
    cfg = Config()
    cfg.data["transition"] = "slide"
    spec = validate({"transition": {"seconds": 0.4}, "scenes": [TITLE]}, cfg)
    assert spec["transition"] == {"type": "slide", "seconds": 0.4}
    assert validate({"scenes": [TITLE]}, cfg)["transition"] == {"type": "slide", "seconds": 0.25}
    assert validate({"transition": "zoom", "scenes": [TITLE]}, cfg)["transition"] == {"type": "zoom", "seconds": 0.25}


def test_a_null_top_level_transition_is_unset():
    spec = validate({"transition": None, "scenes": [TITLE, TITLE]})
    assert spec["transition"] == {"type": "fade", "seconds": 0.25}


def test_a_transition_longer_than_the_scene_is_shortened_with_a_warning(tmp_path):
    tl = timeline(tmp_path, TITLE, {**TITLE, "seconds": 0.2, "transition": {"seconds": 0.8}}, TITLE)
    assert tl._transitions[1]["seconds"] == pytest.approx(0.2)
    assert any("scenes[1]" in w and "shortened" in w for w in tl.warnings)
    tl.frame_at(tl.starts[1] + 0.19)  # the whole scene is the transition, and it still renders
    quiet = timeline(tmp_path, TITLE, {**TITLE, "seconds": 0.2, "transition": "cut"}, TITLE)
    assert quiet.warnings == []  # a cut has no length; the first scene has no transition into it


@pytest.mark.parametrize("text,suffix", [
    ("5, 6 left", None), ("about 5, then", ", then"), ("12,345 rows", " rows"), ("1,234,567.5 x", " x"),
    ("COVID-19 cases", " cases"), ("1000 and", " and"), ("v1,2", None),
])
def test_number_parsing_edges(text, suffix):
    if suffix is None:
        with pytest.raises(ValueError):
            split_count(text)  # "5" and "6": two numbers
        return
    p = split_count(text)
    assert p.suffix == suffix


def test_number_parsing_details():
    assert split_count("5, then").suffix == ", then" and split_count("5, then").target == 5
    assert split_count("COVID-19 cases").prefix == "COVID-" and split_count("COVID-19 cases").target == 19
    assert split_count("x -3 y").target == -3 and split_count("-3").target == -3 and split_count("+3").target == 3
    assert split_count("$-3").target == -3
    with pytest.raises(ValueError):
        split_count("1,2,3")  # three numbers, not 123
    with pytest.raises(ValueError):
        split_count("10-20")  # a range, not 10 and minus 20
    with pytest.raises(ValueError):
        split_count("\u0663\u0664")  # Arabic-Indic digits are not numbers here
    with pytest.raises(ValueError):
        split_count("1,2")


def test_signed_zero_keeps_its_plus():
    p = split_count("+40 pts")
    assert p.format(-0.2) == "+0 pts" and p.format(0.0) == "+0 pts" and p.format(12.0) == "+12 pts"
    assert split_count("40 pts").format(-0.2) == "0 pts"


def test_stagger_past_the_scene_end_warns(tmp_path):
    tl = timeline(tmp_path, {**BULLETS, "seconds": 3, "stagger": 1.5})  # 0.5 + 2 * 1.5 + 0.5 = 4 > 3
    assert any("stagger" in w for w in tl.warnings)
    ok = timeline(tmp_path, {**BULLETS, "seconds": 5, "stagger": 1.0})  # 0.5 + 2 + 0.5 = 3 <= 5
    assert not any("stagger" in w for w in ok.warnings)
    assert not any("stagger" in w for w in timeline(tmp_path, BULLETS).warnings)


def test_a_count_that_starts_wider_than_its_final_text_warns(tmp_path):
    tl = timeline(tmp_path, {**STAT, "value": "7 rows", "count": {"from": -123456789, "seconds": 2}})
    assert any("starts wider" in w for w in tl.warnings)
    assert not any("starts wider" in w for w in timeline(tmp_path, STAT).warnings)


# Frames of a spec that uses none of the new keys, pinned to what 0.2.2 renders (checked against the 0.2.2 tag).
# Hashes depend on the FreeType/Pillow rasteriser and the DejaVu fonts; if a library upgrade changes them, check
# the frames by eye (contact sheet) and print new values with frame_hashes().
NO_NEW_KEYS_SCENES = [
    {"type": "title", "title": "First", "subtitle": "sub", "seconds": 2},
    {"type": "bullets", "title": "Steps", "items": ["one", "two", "three"], "seconds": 4},
    {"type": "stat", "value": "10,000,000 rows", "label": "scanned", "seconds": 3},
    {"type": "code", "code": "def f(x):\n    return x + 1", "seconds": 3, "caption": "a caption"},
    {"type": "diagram", "nodes": [{"id": "a", "label": "A", "x": 0.2, "y": 0.5},
                                  {"id": "b", "label": "B", "x": 0.8, "y": 0.5, "at": 0.5}],
     "edges": [{"from": "a", "to": "b"}], "seconds": 3},
]
NO_NEW_KEYS_TIMES = (0.0, 0.7, 1.0, 2.1, 2.2, 3.0, 5.5, 6.1, 6.2, 8.0, 9.1, 10.0, 12.2, 14.0)
NO_NEW_KEYS_HASHES = {
    "landscape": ["769c116355a2bc1c", "b9c6158d60cff6ab", "0e936fde493711cb", "aa6a31ca7ca0fca9", "20c148f31d511276",
                  "7acc6fba867841b7", "28eb616de741ea0c", "70e641ab117f7f31", "9fbc3e0ef6d91f72", "6015fba2a433f4bc",
                  "8eb007ec74dd7cf7", "be3a9cc039690908", "08797bf700cfa713", "bd71e6b67b28d8ff"],
    "vertical": ["c4dedcb60db2fa83", "9e706b9069a351c9", "a9934b772ac350bc", "e14d01d3ecd1becd", "dc296a5b24c3e8bb",
                 "111633cae092ebe8", "6e1701a0b6eece7c", "c87ebfdfdd4112b2", "7adf69f02db3a392", "d68f34a51e3ff5ee",
                 "15a7d9c9dca8c61c", "619b44bc64950795", "121d3e8438b5a26e", "ba115ac39ddd5443"],
}


def frame_hashes(tmp_path, fmt: str) -> list[str]:
    import hashlib
    tl = timeline(tmp_path, *NO_NEW_KEYS_SCENES, fmt=fmt)
    return [hashlib.sha256(tl.frame_at(t).convert("RGB").tobytes()).hexdigest()[:16] for t in NO_NEW_KEYS_TIMES]


def raster_fingerprint() -> tuple[str, str | None, bool]:
    import PIL
    import PIL.features
    return PIL.__version__, PIL.features.version("freetype2"), bool(PIL.features.check("raqm"))


# The text rasteriser the hashes above were taken with. On any other Pillow, FreeType or raqm the exact bytes of
# rendered text can differ, so the byte-exact check is skipped there; the platform-independent checks below still run.
HASH_FINGERPRINT = ("13.0.0", "2.14.3", False)


@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
def test_a_spec_without_new_keys_renders_the_same_frames_as_0_2_2(tmp_path, fmt):
    if raster_fingerprint() != HASH_FINGERPRINT:
        pytest.skip(f"frame hashes were taken with {HASH_FINGERPRINT}, this is {raster_fingerprint()}")
    assert frame_hashes(tmp_path, fmt) == NO_NEW_KEYS_HASHES[fmt]


@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
def test_new_keys_left_at_their_defaults_change_no_frame(tmp_path, fmt):
    """Spelling out the defaults (fade 0.25, no stagger of its own) must give the frames of a spec that has none."""
    plain = timeline(tmp_path, *NO_NEW_KEYS_SCENES, fmt=fmt)
    spelled = timeline(tmp_path, *[{**s, "transition": {"type": "fade", "seconds": 0.25}} for s in NO_NEW_KEYS_SCENES],
                       fmt=fmt, transition="fade")
    for t in NO_NEW_KEYS_TIMES:
        assert same(plain.frame_at(t), spelled.frame_at(t)), t


@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
def test_frames_without_new_keys_keep_their_geometry_and_colours(tmp_path, fmt):
    tl = timeline(tmp_path, *NO_NEW_KEYS_SCENES, fmt=fmt)
    W, H = tl.ctx.W, tl.ctx.H
    bg = tl.ctx.background()
    for t in NO_NEW_KEYS_TIMES:
        img = tl.frame_at(t)
        assert img.size == (W, H) and img.mode == "RGB"
        assert img.getpixel((W // 2, 2)) == bg.getpixel((W // 2, 2))  # the top edge is plain background
    # the bullets scene (starts at 2 s) draws its three items top to bottom, inside the side margins
    img = tl.frame_at(tl.starts[1] + 3.5)
    bgl = tl.ctx.background()
    rows = [y for y in range(H) if any(img.getpixel((x, y)) != bgl.getpixel((x, y)) for x in range(0, W, 3))]
    assert rows and rows[0] > H * 0.05 and rows[-1] < H * 0.95
    cols = [x for x in range(W) if any(img.getpixel((x, y)) != bgl.getpixel((x, y)) for y in range(0, H, 3))]
    assert cols[0] >= tl.ctx.margin * 0.9 and cols[-1] <= W - tl.ctx.margin * 0.9


@pytest.mark.parametrize("kind", ["slide", "zoom"])
def test_slide_and_zoom_are_clamped_to_a_short_scene(tmp_path, kind):
    tl = timeline(tmp_path, TITLE, {**TITLE, "seconds": 0.3, "transition": {"type": kind, "seconds": 1.0}}, TITLE)
    assert tl._transitions[1]["seconds"] == pytest.approx(0.3)
    assert any("shortened to fit" in w for w in tl.warnings)
    mid = tl.frame_at(tl.starts[1] + 0.15)
    assert mid.size == (tl.ctx.W, tl.ctx.H) and not same(mid, tl._scene_frame(1, 0.15))
    assert same(tl.frame_at(tl.starts[2] + 0.5), tl._scene_frame(2, 0.5))  # the next scene is unaffected


def test_a_scene_transition_of_null_inherits_the_top_level_one():
    spec = validate({"transition": {"type": "slide", "seconds": 0.5}, "scenes": [TITLE, {**TITLE, "transition": None}]})
    assert spec["scenes"][1].get("transition", spec["transition"]) == {"type": "slide", "seconds": 0.5}
    assert validate({"scenes": [{**TITLE, "transition": None}]})["scenes"][0].get("transition") is None
