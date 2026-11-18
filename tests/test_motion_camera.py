"""The motion camera: zoom and pan keyframes applied to coordinates before drawing."""
from __future__ import annotations

import hashlib

import pytest
from PIL import Image, ImageChops

from yamlreel import motion_camera as mc
from yamlreel import motion_scene as motion
from yamlreel.motion_camera import CameraKey, Viewport, camera_at, to_screen
from yamlreel.scenebase import Ctx
from yamlreel.scenes import build_scene
from yamlreel.specs import SpecError, validate
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


def digest(img: Image.Image) -> str:
    return hashlib.sha256(img.tobytes()).hexdigest()


def same(a: Image.Image, b: Image.Image) -> bool:
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


CARD = {"id": "card", "kind": "group", "x": 0.3, "y": 0.4, "at": 0.0, "enter": "none",
        "children": [{"kind": "rect", "w": 400, "h": 200}, {"kind": "text", "text": "Card", "size": 60}]}
OTHER = {"kind": "circle", "r": 80, "x": 0.8, "y": 0.8, "at": 0.0, "enter": "none"}


# --- the maths ---------------------------------------------------------------------------------------------

def test_to_screen_maps_the_focus_to_the_middle_and_scales_around_it():
    W, H = 1000.0, 2000.0
    assert to_screen(Viewport(), 123.0, 456.0, W, H) == (123.0, 456.0)               # the identity
    vp = Viewport(2.0, 0.25, 0.5)
    assert to_screen(vp, 250.0, 1000.0, W, H) == (500.0, 1000.0)                      # the focus lands mid-frame
    assert to_screen(vp, 350.0, 1100.0, W, H) == (700.0, 1200.0)                      # 100 px away is 200 px away
    assert Viewport().identity and not Viewport(1.0, 0.4, 0.5).identity and not Viewport(1.5).identity


def test_camera_at_eases_each_key_from_the_view_before_it():
    keys = [CameraKey(1.0, 2.0, 2.0, 0.2, 0.4, "linear"), CameraKey(4.0, 1.0, 1.0, 0.5, 0.5, "linear"),
            CameraKey(6.0, 0.0, 3.0, 0.6, 0.6, "linear")]
    assert camera_at(keys, 0.5) == Viewport()
    mid = camera_at(keys, 2.0)
    assert (mid.zoom, mid.fx, mid.fy) == pytest.approx((1.5, 0.35, 0.45))
    assert camera_at(keys, 3.5) == Viewport(2.0, 0.2, 0.4)                            # held between keys
    back = camera_at(keys, 4.5)
    assert back.zoom == pytest.approx(1.5) and back.fx == pytest.approx(0.35)
    assert camera_at(keys, 5.5) == Viewport(1.0, 0.5, 0.5)
    assert camera_at(keys, 6.0) == Viewport(3.0, 0.6, 0.6) and camera_at(keys, 99) == Viewport(3.0, 0.6, 0.6)   # seconds 0: a cut
    assert camera_at([], 3.0) == Viewport()
    assert mc.max_zoom(keys) == 3.0 and mc.max_zoom([]) == 1.0


def test_camera_keys_are_validated():
    def bad(match, keys):
        with pytest.raises(SpecError, match=match):
            validate({"scenes": [{"type": "motion", "elements": [{"kind": "circle", "r": 5}], "camera": keys}]})
    bad("scenes\\[0\\].camera\\[1\\].at: starts at 1.2s, before the camera move before it ends \\(1.4s\\)",
        [{"at": 0.2, "seconds": 1.2, "zoom": 2}, {"at": 1.2, "zoom": 1}])
    bad("camera must have at least 1 entry", [])
    bad("camera must be a list", {})
    bad("camera has 17 entries; the limit is 16 \\(MAX_CAMERA_KEYS\\)", [{"at": k * 0.1, "seconds": 0} for k in range(17)])
    bad("camera\\[0\\].zoom must be a number between 0.25 and 4", [{"at": 0, "zoom": 5}])
    bad("camera\\[0\\].zoom must be a number between 0.25 and 4", [{"at": 0, "zoom": 0.1}])
    bad("camera\\[0\\].focus must be \\[x, y\\]", [{"at": 0, "focus": 0.5}])
    bad("camera\\[0\\].focus\\[1\\] must be a number between 0 and 1", [{"at": 0, "focus": [0.5, 2]}])
    bad("camera\\[0\\].seconds must be a number between 0 and 10", [{"at": 0, "seconds": 11}])
    bad("camera\\[0\\].q: unknown field for a camera key", [{"at": 0, "q": 1}])
    bad("camera\\[0\\]: needs at", [{"zoom": 2}])
    ok = validate({"scenes": [{"type": "motion", "elements": [{"kind": "circle", "r": 5}],
                               "camera": [{"at": 1, "zoom": 2, "focus": [0.3, 0.4], "vfocus": [0.5, 0.2]}]}]})
    key = ok["scenes"][0]["camera"][0]
    assert key["seconds"] == 0.8 and key["easing"] == "in_out" and key["vfocus"] == [0.5, 0.2]
    assert validate({"scenes": ok["scenes"]})["scenes"] == ok["scenes"]


# --- drawing ---------------------------------------------------------------------------------------------------

def test_zoom_1_on_the_centre_is_byte_equal_to_no_camera(tmp_path):
    plain, _ = make(tmp_path, CARD, OTHER, title="Cam")
    cam, _ = make(tmp_path, CARD, OTHER, title="Cam", camera=[{"at": 0.5, "seconds": 1.0, "zoom": 1.0, "focus": [0.5, 0.5]}])
    for t in (0.0, 0.4, 1.0, 2.0, 5.0):
        assert digest(plain.frame(t)) == digest(cam.frame(t))


def test_a_zoom_moves_and_scales_elements_before_drawing(tmp_path):
    plain, ctx = make(tmp_path, CARD, OTHER)
    cam, _ = make(tmp_path, CARD, OTHER, camera=[{"at": 1.0, "seconds": 0.0, "zoom": 2.0, "focus": [0.3, 0.4]}])
    before = {p.kind: p for p in cam.placements(0.5)}
    after = {p.kind: p for p in cam.placements(2.0)}
    assert (before["rect"].x, before["rect"].y) == (round(270 * 0.3), round(480 * 0.4))
    assert (after["rect"].x, after["rect"].y) == (135, 240)                          # the focus is in the middle
    # the other element sat 0.5 W and 0.4 H from the focus: twice that now (rounded half up)
    assert after["circle"].x == round(135 + 2 * (round(270 * 0.8) - round(270 * 0.3)))
    # a card and its label keep their offset, scaled
    assert after["text"].x - after["rect"].x == 0
    z = cam.frame(2.0)
    box = ImageChops.difference(z.convert("RGB"), ctx.background()).getbbox()
    assert box is not None
    assert not same(z, plain.frame(2.0))
    rect_w = 400 * ctx.u * 2
    xs = [x for x in range(270) if z.getpixel((x, 240)) == ctx.theme.card]
    assert abs((max(xs) - min(xs)) - rect_w) <= 12      # drawn at twice the size (the fill, inside a 2 px stroke each side)


def test_stroke_widths_radii_and_text_scale_with_the_camera(tmp_path):
    el = {"kind": "rect", "w": 300, "h": 200, "stroke_width": 8, "radius": 40, "at": 0, "enter": "none", "y": 0.5}
    one, ctx = make(tmp_path, el, W=540, H=960)
    two, _ = make(tmp_path, el, W=540, H=960, camera=[{"at": 0, "seconds": 0, "zoom": 2}])
    def stroke_run(img, y):
        row = [img.getpixel((x, y)) for x in range(540)]
        left = next(i for i, p in enumerate(row) if p != ctx.background().getpixel((i, y)))
        run = 0
        while row[left + run] == ctx.theme.accent2 or sum(abs(a - b) for a, b in zip(row[left + run], ctx.theme.accent2)) < 40:
            run += 1
        return run
    assert stroke_run(two.frame(1.0), 480) >= 2 * stroke_run(one.frame(1.0), 480) - 2
    text, _ = make(tmp_path, {"kind": "text", "text": "Zoom", "size": 60, "at": 0, "enter": "none"}, W=540, H=960,
                   camera=[{"at": 0, "seconds": 0, "zoom": 3}])
    leaf = next(iter(text.leaves.values()))
    assert leaf.prep.s_max >= 3.0                                                      # the mask is sharp at the zoom
    assert text.frame(1.0).getbbox() is not None


def test_the_background_and_the_title_are_not_transformed(tmp_path):
    cam, ctx = make(tmp_path, OTHER, title="Fixed title", camera=[{"at": 0, "seconds": 0, "zoom": 3, "focus": [0.1, 0.1]}])
    none, _ = make(tmp_path, OTHER, title="Fixed title")
    a, b = cam.frame(1.0), none.frame(1.0)
    top = (0, 0, 270, int(480 * 0.09 + 60))
    assert same(a.crop(top), b.crop(top))                                              # the title strip is the same
    assert a.getpixel((2, 475)) == ctx.background().getpixel((2, 475))


def test_elements_outside_the_zoomed_frame_are_culled(tmp_path):
    cam, ctx = make(tmp_path, CARD, OTHER, camera=[{"at": 0, "seconds": 0, "zoom": 4, "focus": [0.3, 0.4]}])
    names = [p.kind for p in cam.placements(1.0)]
    assert "circle" in names                                                           # still planned, but off screen
    only_card, _ = make(tmp_path, CARD, camera=[{"at": 0, "seconds": 0, "zoom": 4, "focus": [0.3, 0.4]}])
    assert same(cam.frame(1.0), only_card.frame(1.0))                                  # the far circle left no pixel


def test_vfocus_overrides_focus_in_vertical_frames_only(tmp_path):
    keys = [{"at": 0, "seconds": 0, "zoom": 2, "focus": [0.3, 0.4], "vfocus": [0.8, 0.8]}]
    v, _ = make(tmp_path, OTHER, W=270, H=480, camera=keys)
    h, _ = make(tmp_path, OTHER, W=480, H=270, camera=keys)
    assert (v.camera[0].fx, v.camera[0].fy) == (0.8, 0.8) and (h.camera[0].fx, h.camera[0].fy) == (0.3, 0.4)


def test_a_camera_move_frame_is_deterministic_and_the_hold_is_cached(tmp_path):
    keys = [{"at": 1.0, "seconds": 1.0, "zoom": 2.0, "focus": [0.3, 0.4]}]
    a, _ = make(tmp_path, CARD, OTHER, camera=keys)
    b, _ = make(tmp_path, CARD, OTHER, camera=keys)
    b._frame_cache = False
    times = [0.0, 1.3, 1.7, 2.5, 4.0]
    assert [digest(a.frame(t)) for t in times] == [digest(b.frame(t)) for t in reversed(times[::-1])]
    assert [digest(a.frame(t)) for t in reversed(times)] == [digest(b.frame(t)) for t in reversed(times)]
    assert digest(a.frame(3.0)) == digest(a.frame(3.5))


def test_zoomed_layers_stay_clipped_to_the_frame_and_under_the_ceiling(tmp_path):
    els = [{"kind": "rect", "w": 900, "h": 500, "x": 0.5, "y": 0.3 + 0.1 * k, "at": 0, "enter": "none"} for k in range(4)]
    sc, ctx = make(tmp_path, *els, W=540, H=960,
                   camera=[{"at": 0, "seconds": 2.0, "zoom": 4.0, "focus": [0.5, 0.4], "easing": "linear"}])
    for k in range(0, 10):
        sc.frame(k * 0.2)
    f = 2
    assert 0 < sc.max_layer_pixels() <= min(motion.MAX_LAYER_PIXELS, (540 + 2 * 16) * (960 + 2 * 16) * f * f)


def test_an_oversized_element_at_zoom_4_and_high_quality_stays_inside_both_limits(tmp_path):
    """AC-6 with the camera: a 4000u rect at scale 8, zoomed 4x, at `high`, on a full 1080x1920 frame."""
    sc, ctx = make(tmp_path, {"kind": "rect", "w": 4000, "h": 4000, "scale": 8, "at": 0, "enter": "none"}, W=1080, H=1920,
                   quality="high", camera=[{"at": 0, "seconds": 0, "zoom": 4.0}])
    img = sc.frame(1.0)
    assert img.getpixel((540, 960)) == ctx.theme.card
    assert 0 < sc.max_layer_pixels() <= motion.MAX_LAYER_PIXELS
    assert sc.max_layer_pixels() <= (1080 + 2 * 16) * (1920 + 2 * 16) * 2 * 2          # clipped to the frame, at factor 2
    assert any("too large to supersample" in w for w in ctx.warnings)
    assert sc.retained_bytes() <= motion.MOTION_CACHE_BYTES + motion.MAX_IMAGE_BYTES + 1080 * 1920 * 3


def test_arrows_and_paths_follow_the_camera(tmp_path):
    els = [{"id": "a", "kind": "rect", "w": 200, "h": 100, "x": 0.3, "y": 0.2, "at": 0, "enter": "none"},
           {"id": "b", "kind": "rect", "w": 200, "h": 100, "x": 0.7, "y": 0.7, "at": 0, "enter": "none"},
           {"kind": "arrow", "from": "a", "to": "b", "at": 0, "enter": "none"},
           {"kind": "path", "points": [[0.1, 0.9], [0.5, 0.95], [0.9, 0.9]], "at": 0, "enter": "none"}]
    plain, ctx = make(tmp_path, *els, W=540, H=960)
    cam, _ = make(tmp_path, *els, W=540, H=960, camera=[{"at": 0, "seconds": 0, "zoom": 2.0, "focus": [0.3, 0.2]}])
    g = {it.leaf.kind.__class__.__name__: it for it in cam._plan(1.0)}
    p = {it.leaf.kind.__class__.__name__: it for it in plain._plan(1.0)}
    assert g["ArrowKind"].geo != p["ArrowKind"].geo and g["PathKind"].geo != p["PathKind"].geo
    assert g["ArrowKind"].geo[0] / 16 == pytest.approx(270 + 0.0, abs=40)               # starts near the zoomed box a
    assert not same(cam.frame(1.0), plain.frame(1.0))
