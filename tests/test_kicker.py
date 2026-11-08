"""The `kicker` tag above a data scene's title: validation, placement in every format, the warnings, and the proof
that a scene without a kicker renders the frames it did before."""
from __future__ import annotations

import hashlib

import pytest
from PIL import Image, ImageChops

from yamlreel.render import Timeline
from yamlreel.scenebase import KICKER_GAP, kicker_shift, make_kicker
from yamlreel.scenes import SCENE_TYPES, make_caption
from yamlreel.specs import MAX_KICKER_CHARS, SpecError, validate

from test_data_scenes import ARRAY, TABLE
from test_grid import GRID
from test_meter import METER
from test_motion import HASH_FINGERPRINT, raster_fingerprint
from test_timeline import TIMELINE
from test_tree import TREE
from test_validation import _scene_reads

KICKER = "01 / THE BURST"
FORMATS = ["landscape", "vertical", "square"]
WITH_KICKER = ["bullets", "code", "diagram", "table", "array", "meter", "grid", "tree", "timeline"]

# One scene per type, big enough to fill the space (so a kicker has to be made room for) and with a title.
CODE = "\n".join(f"line_{i} = compute({i})" for i in range(13))
SCENES: dict[str, dict] = {
    "bullets": {"type": "bullets", "title": "Steps", "items": [f"step number {i} of the plan" for i in range(7)],
                "seconds": 8},
    "code": {"type": "code", "title": "Code", "code": CODE, "seconds": 8},
    "diagram": {"type": "diagram", "title": "Flow", "seconds": 8,
                "nodes": [{"id": "a", "label": "A", "x": 0.25, "y": 0.5}, {"id": "b", "label": "B", "x": 0.75, "y": 0.5}],
                "edges": [{"from": "a", "to": "b"}]},
    "table": {**TABLE, "title": "Users", "rows": [[str(i), f"user{i}", f"u{i}@example.com"] for i in range(10)],
              "scan": {"at": 2.0, "stop": 3}},
    "array": {**ARRAY, "title": "List"},
    "meter": {**METER, "title": "Tokens drain"},
    "grid": {**GRID, "title": "Cells"},
    "tree": {**TREE},
    "timeline": {**TIMELINE},
}
assert set(SCENES) == set(WITH_KICKER)


def timeline(tmp_path, scene: dict, fmt: str = "landscape", **extra) -> Timeline:
    return Timeline(validate({"scenes": [scene], **extra}), fmt, tmp_path, scale=0.5)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def title_top(sc) -> float:
    """Where the scene puts the top of its title (where the title would start if it had none)."""
    u = sc.ctx.u
    kind = type(sc).__name__
    if kind == "BulletsScene":
        return sc.y0 - 70 * u
    if kind == "CodeScene":
        return sc.top - 54 * u
    if kind == "TableScene":
        return sc.head_xy[1]
    if kind == "DiagramScene":
        return sc.title_top
    return sc.title_xy[1]


# --- validation -----------------------------------------------------------------------------------------

def kick(**kw):
    return validate({"scenes": [{**SCENES["bullets"], **kw}]})["scenes"][0]


@pytest.mark.parametrize("value,msg", [
    (["a"], "scenes[0].kicker must be text"),
    ({"a": 1}, "scenes[0].kicker must be text"),
    (True, "scenes[0].kicker must be text"),
    ("x" * 61, f"scenes[0].kicker is 61 characters; the limit is {MAX_KICKER_CHARS} (MAX_KICKER_CHARS)"),
    ("one\ntwo", "scenes[0].kicker must be one line of text, without a line break"),
    ("one\rtwo", "scenes[0].kicker must be one line of text, without a line break"),
    ("", "scenes[0].kicker must not be empty"),
    ("   ", "scenes[0].kicker must not be empty"),
])
def test_kicker_validation(value, msg):
    with pytest.raises(SpecError) as e:
        kick(kicker=value)
    assert msg in str(e.value)


def test_a_kicker_of_the_limit_passes_and_one_over_does_not():
    assert MAX_KICKER_CHARS == 48
    assert kick(kicker="W" * 48)["kicker"] == "W" * 48
    assert kick(kicker=KICKER)["kicker"] == KICKER
    with pytest.raises(SpecError, match="is 49 characters"):
        kick(kicker="W" * 49)


@pytest.mark.parametrize("scene", [
    {"type": "title", "title": "T"},
    {"type": "shot", "source": "https://example.com"},
    {"type": "stat", "value": "10", "label": "rows"},
    {"type": "type", "text": "hello"},
    {"type": "cta"},
])
def test_kicker_is_rejected_on_the_other_scene_types(scene):
    with pytest.raises(SpecError) as e:
        validate({"scenes": [{**scene, "kicker": KICKER}]})
    assert f"scenes[0].kicker: unknown field for a {scene['type']} scene; allowed: " in str(e.value)


# --- drift guard ----------------------------------------------------------------------------------------

def test_exactly_the_nine_scene_types_list_kicker_and_each_reads_it():
    listed = {name for name, cls in SCENE_TYPES.items() if "kicker" in cls.FIELDS}
    assert listed == set(WITH_KICKER)
    for name in WITH_KICKER:
        # make_kicker reads spec["kicker"]; the AST walk finds it through the call in each constructor
        assert "kicker" in _scene_reads(SCENE_TYPES[name]), name
    for name in set(SCENE_TYPES) - set(WITH_KICKER):
        assert "kicker" not in _scene_reads(SCENE_TYPES[name]), name


# --- rendering ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("name", WITH_KICKER)
def test_kicker_renders(tmp_path, name, fmt):
    tl = timeline(tmp_path, {**SCENES[name], "kicker": KICKER}, fmt)
    plain = timeline(tmp_path, SCENES[name], fmt)
    sc, ref, ctx = tl.scenes[0], plain.scenes[0], tl.ctx
    box = sc.kicker_box
    assert box is not None and box[1] >= 0
    g = KICKER_GAP * ctx.u
    assert box[3] + g <= title_top(sc) + 1e-6
    t = sc.duration * 0.9
    frame = sc.frame(t).convert("RGB")
    x0, y0, x1, y1 = (int(v) for v in box)
    assert ctx.theme.accent in {c for _, c in frame.crop((x0, y0, x1 + 1, y1 + 1)).getcolors(1 << 20)}
    bg = ctx.background()
    bottom = ImageChops.difference(frame, bg).getbbox()[3]
    plain_bottom = ImageChops.difference(ref.frame(t).convert("RGB"), bg).getbbox()[3]
    pill_h = make_caption(ctx, "A caption")[0].height
    caption_top = ctx.caption_y - pill_h / 2
    assert bottom <= max(caption_top, plain_bottom), (bottom, caption_top, plain_bottom)
    assert tl.warnings == plain.warnings   # a kicker that fits adds no warning


# Where the title starts is above where a kicker needs it only for these (bullets, code and table, and every scene in a tall
# frame, start low enough already: the shift is 0 and nothing moves).
@pytest.mark.parametrize("name,fmt", [("diagram", "landscape"), ("meter", "landscape"),
                                      ("grid", "landscape"), ("tree", "landscape"), ("timeline", "landscape"),
                                      ("tree", "vertical"), ("timeline", "vertical")])
def test_a_full_scene_moves_its_title_down_to_make_room(tmp_path, name, fmt):
    tl = timeline(tmp_path, {**SCENES[name], "kicker": KICKER}, fmt)
    plain = timeline(tmp_path, SCENES[name], fmt)
    sc, ref = tl.scenes[0], plain.scenes[0]
    assert title_top(sc) > title_top(ref)
    assert sc.kicker_box[1] >= 0.05 * tl.ctx.H - 1


def test_a_roomy_scene_does_not_move_when_it_gets_a_kicker(tmp_path):
    for name in ("bullets", "code", "table", "array"):
        tl = timeline(tmp_path, {**SCENES[name], "kicker": KICKER}, "landscape")
        plain = timeline(tmp_path, SCENES[name], "landscape")
        assert title_top(tl.scenes[0]) == title_top(plain.scenes[0]), name


@pytest.mark.parametrize("name", WITH_KICKER)
def test_a_kicker_without_a_title_still_renders(tmp_path, name):
    scene = {k: v for k, v in SCENES[name].items() if k != "title"}
    tl = timeline(tmp_path, {**scene, "kicker": KICKER})
    sc = tl.scenes[0]
    assert sc.kicker_box is not None
    assert ImageChops.difference(sc.frame(sc.duration * 0.9).convert("RGB"), sc.ctx.background()).getbbox() is not None


def test_kicker_shrinks_and_warns(tmp_path):
    tl = timeline(tmp_path, {**SCENES["meter"], "kicker": "W" * 48}, "vertical")
    sc = tl.scenes[0]
    assert sc.kicker.shrunk
    assert any(w == f"scenes[0]: kicker shrunk to {sc.kicker.size}px to fit; shorten it" for w in tl.warnings)
    assert sc.kicker.size < 28 * tl.ctx.u
    assert not any("kicker" in w for w in timeline(tmp_path, {**SCENES["meter"], "kicker": KICKER}, "vertical").warnings)


def test_kicker_badge_overlap_warns(tmp_path):
    scene = {**SCENES["diagram"], "kicker": KICKER, "step": "Cause"}
    for fmt in ("landscape", "vertical"):
        tl = timeline(tmp_path, scene, fmt)
        assert "scenes[0]: the kicker overlaps the step badge; drop one of them" in tl.warnings
        no_step = timeline(tmp_path, {k: v for k, v in scene.items() if k != "step"}, fmt)
        assert not any("badge" in w for w in no_step.warnings)


def test_diagram_warns_when_the_kicker_covers_a_node(tmp_path):
    scene = {**SCENES["diagram"], "kicker": KICKER,
             "nodes": [{"id": "top", "label": "Top", "x": 0.5, "y": 0.06}, {"id": "b", "label": "B", "x": 0.5, "y": 0.6}],
             "edges": []}
    tl = timeline(tmp_path, scene)
    assert any(w.startswith("scenes[0]: the kicker overlaps diagram node 'top'") for w in tl.warnings)


@pytest.mark.parametrize("fmt", FORMATS)
def test_array_kicker_needs_no_shift(tmp_path, fmt):
    for title in ("List", None):
        scene = {"type": "array", "items": ["ada"], "seconds": 6, "kicker": KICKER, **({"title": title} if title else {})}
        sc = timeline(tmp_path, scene, fmt).scenes[0]
        assert kicker_shift(sc.ctx, sc.kicker, sc.title_xy[1]) == 0.0
        assert sc.kicker_box[1] >= 0.05 * sc.ctx.H


def test_make_kicker_and_shift_without_a_kicker(tmp_path):
    sc = timeline(tmp_path, SCENES["bullets"]).scenes[0]
    assert make_kicker(sc.ctx, {}, 500) is None and kicker_shift(sc.ctx, None, 0.0) == 0.0
    k = make_kicker(sc.ctx, {"kicker": "ab"}, 500)
    assert k is not None and kicker_shift(sc.ctx, k, 1e9) == 0.0
    assert kicker_shift(sc.ctx, k, 0.0) == pytest.approx(0.05 * sc.ctx.H + k.height + KICKER_GAP * sc.ctx.u)


# --- no kicker, no change -------------------------------------------------------------------------------

PIN_SCENES = {"table": TABLE, "array": ARRAY, "meter": METER, "grid": GRID, "tree": TREE, "timeline": TIMELINE}
PIN_TIMES = (0.5, 2.0, 4.0, 7.0)
# Frames of the six data scenes (the fixtures of their own tests) captured on 7d5b404, before the kicker existed.
# Bullets, code and diagram are pinned by the 0.2.2 test in test_motion. The hashes depend on the rasteriser and the
# fonts (HASH_FINGERPRINT); on any other the byte-exact check is skipped and the checks below still run.
PINNED = {
    ("table", "landscape"): ['86ea27e42350765f', '03d321f4d19cd994', '0e76674312d4e760', '0e76674312d4e760'],
    ("table", "vertical"): ['1fdfa28372e7708c', '585d89d6e93ddeae', '5075356d53b6298f', '5075356d53b6298f'],
    ("array", "landscape"): ['920aec04377966ac', '18738ebb7b6110d7', '18738ebb7b6110d7', '18738ebb7b6110d7'],
    ("array", "vertical"): ['e96130540d91cb98', 'ecc5592866a89eb0', 'ecc5592866a89eb0', 'ecc5592866a89eb0'],
    ("meter", "landscape"): ['a8da4dc4299cff22', 'a7cc7664e6171de5', 'a7cc7664e6171de5', '02cd37f4f048a8f0'],
    ("meter", "vertical"): ['96c6149565b39619', '6a8e63556b24b5fa', '6a8e63556b24b5fa', '4fe0cfba36ccae48'],
    ("grid", "landscape"): ['de7ec2d59e633da5', '761bb2bc3d29bae1', '761bb2bc3d29bae1', '761bb2bc3d29bae1'],
    ("grid", "vertical"): ['d6a6e767f8db3e32', 'e7705813228a9854', 'e7705813228a9854', 'e7705813228a9854'],
    ("tree", "landscape"): ['811342f887e197fb', '3f15c6f22a6972ac', '97a61d1071aed450', '97a61d1071aed450'],
    ("tree", "vertical"): ['9f1b1a2bfbccb8b3', '6c643c6002ed454a', '6f0f886d73878e13', '6f0f886d73878e13'],
    ("timeline", "landscape"): ['d5ccfcd4ae3364b8', '9d5857245c703634', '3a3f65297a365fe5', '876f9a7f27bb1fc1'],
    ("timeline", "vertical"): ['d33063737b214d3a', 'e69fe1963b70e67f', 'd8fb5dcfa73f0376', '6d423821662eed3d'],
}


@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
@pytest.mark.parametrize("name", sorted(PIN_SCENES))
def test_data_scenes_without_kicker_match_pinned_hashes(tmp_path, name, fmt):
    tl = timeline(tmp_path, PIN_SCENES[name], fmt)
    sc = tl.scenes[0]
    assert sc.kicker is None and sc.kicker_box is None   # on every platform
    if raster_fingerprint() != HASH_FINGERPRINT:
        pytest.skip(f"frame hashes were taken with {HASH_FINGERPRINT}, this is {raster_fingerprint()}")
    got = [hashlib.sha256(tl.frame_at(t).convert("RGB").tobytes()).hexdigest()[:16] for t in PIN_TIMES]
    assert got == PINNED[(name, fmt)]


# --- a kicker never moves content below its old lower limit -----------------------------------------------

def _bottom(tl) -> int:
    sc = tl.scenes[0]
    return ImageChops.difference(sc.frame(sc.duration * 0.95).convert("RGB"), tl.ctx.background()).getbbox()[3]


@pytest.mark.parametrize("fmt", FORMATS)
def test_overfull_bullets_do_not_slide_down_for_a_kicker(tmp_path, fmt):
    scene = {"type": "bullets", "title": "Steps", "seconds": 8, "caption": "A caption",
             "items": [f"step number {i} of a long plan that wraps onto a second line of text here" for i in range(20)]}
    plain = timeline(tmp_path, scene, fmt)
    tl = timeline(tmp_path, {**scene, "kicker": KICKER}, fmt)
    assert tl.scenes[0].shift <= plain.scenes[0].shift + 1e-6 and tl.scenes[0].shift == 0.0
    assert _bottom(tl) <= _bottom(plain)
    assert "bullets: no room for the kicker; shorten the content or the kicker" in tl.warnings
    assert "bullets: no room for the kicker; shorten the content or the kicker" not in plain.warnings


def test_a_long_table_gets_no_kicker_room_and_warns(tmp_path):
    # at a small render scale the table is at its text-size floor and still full: the kicker must not push it down
    scene = {**TABLE, "title": "Users", "seconds": 8, "rows": [[str(i), f"user{i}", f"u{i}@example.com"] for i in range(12)]}

    def build(spec):
        return Timeline(validate({"scenes": [spec]}), "landscape", tmp_path, scale=0.25)
    plain, tl = build(scene), build({**scene, "kicker": KICKER})
    assert "table: no room for the kicker; shorten the content or the kicker" in tl.warnings
    assert "table: no room for the kicker; shorten the content or the kicker" not in plain.warnings
    assert tl.scenes[0].card[3] <= plain.scenes[0].card[3] + 1e-6
    assert _bottom(tl) <= _bottom(plain)


# --- a long kicker at a small render scale stays inside the frame -----------------------------------------

@pytest.mark.parametrize("name", ["meter", "array", "grid", "bullets", "code", "table"])
@pytest.mark.parametrize("fmt", ["vertical", "square"])
def test_a_long_kicker_stays_inside_the_frame(tmp_path, name, fmt):
    tl = timeline(tmp_path, {**SCENES[name], "kicker": "W" * 48}, fmt)
    sc, W = tl.scenes[0], tl.ctx.W
    box = sc.kicker_box
    assert 0 <= box[0] and box[2] <= W, (box, W)
    assert sc.kicker.shrunk
    assert f"scenes[0]: kicker shrunk to {sc.kicker.size}px to fit; shorten it" in tl.warnings
    assert not any("wider than the frame" in w for w in tl.warnings)


def test_a_kicker_that_cannot_fit_says_so(tmp_path):
    tl = Timeline(validate({"scenes": [{**SCENES["meter"], "kicker": "W" * 48}]}), "vertical", tmp_path, scale=0.1)
    assert "the kicker is wider than the frame; shorten it" in tl.warnings


# --- the README example is not touched ---------------------------------------------------------------------

def test_an_inline_spec_with_kickers_validates_and_renders(tmp_path):
    spec = {"scenes": [{"type": "diagram", "kicker": "01 / THE BURST", "title": "Flow", "seconds": 4,
                        "nodes": [{"id": "a", "label": "A", "x": 0.25, "y": 0.5}, {"id": "b", "label": "B", "x": 0.75, "y": 0.5}],
                        "edges": [{"from": "a", "to": "b"}]},
                       {"type": "bullets", "kicker": "02 / THE FIX", "title": "Fix", "items": ["one", "two"], "seconds": 4},
                       {"type": "code", "kicker": "03 / THE CODE", "title": "Code", "code": "x = 1", "seconds": 4}]}
    tl = Timeline(validate(spec), "landscape", tmp_path, scale=0.5)
    assert all(sc.kicker_box is not None for sc in tl.scenes)
    assert not any("kicker" in w for w in tl.warnings)
