"""Image elements: local files only, guarded, pre-scaled once, counted against a byte budget."""
from __future__ import annotations

import pytest
from PIL import Image, ImageChops

from yamlreel import motion_image as mi
from yamlreel import motion_scene as motion
from yamlreel import shots
from yamlreel.safety import UnsafeInput
from yamlreel.scenebase import Ctx
from yamlreel.scenes import build_scene
from yamlreel.shots import CaptureError
from yamlreel.specs import SpecError, validate
from yamlreel.theme import get_theme


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def picture(path, size=(200, 100), colour=(200, 40, 40, 255)):
    Image.new("RGBA", size, colour).save(path)
    return path


def make(tmp_path, *elements, W=270, H=480, roots=None, **extra):
    spec = validate({"scenes": [{"type": "motion", "elements": list(elements), "seconds": 6, **extra}]})["scenes"][0]
    ctx = Ctx(W=W, H=H, theme=get_theme("midnight"), workdir=tmp_path, base_dir=tmp_path,
              roots=[tmp_path] if roots is None else roots)
    return build_scene(spec, ctx), ctx


def img(**kw):
    return {"kind": "image", "source": "pic.png", "at": 0, "enter": "none", **kw}


def test_a_local_image_loads_scales_and_draws(tmp_path):
    picture(tmp_path / "pic.png", (200, 100))
    sc, ctx = make(tmp_path, img(w=800))
    frame = sc.frame(1.0)
    assert frame.getpixel((135, 240)) == (200, 40, 40)
    ink = ImageChops.difference(frame.convert("RGB"), ctx.background()).getbbox()
    assert ink == (35, 190, 235, 290)                                             # 800u x 400u at u = 0.25, centred
    assert sc.retained_bytes() >= sc._image_bytes > 0


def test_w_or_h_alone_keeps_the_aspect_ratio_and_both_stretch():
    assert mi.display_size((200, 100), 400, None) == (400, 200)
    assert mi.display_size((200, 100), None, 50) == (100, 50)
    assert mi.display_size((200, 100), 30, 30) == (30, 30)


def test_prescale_never_enlarges_and_caps_the_pixels():
    big = Image.new("RGBA", (3000, 2000), (1, 2, 3, 255))
    capped = mi.prescale(big, 6000, 4000)
    assert capped.width * capped.height <= mi.MAX_PRESCALE_PIXELS and capped.width <= 3000
    out = mi.prescale(big, 1500, 1000)
    assert out.size == (1500, 1000)
    small = Image.new("RGBA", (10, 10))
    assert mi.prescale(small, 100, 100) is small


def test_rounded_corners_and_opacity_and_rotation(tmp_path):
    picture(tmp_path / "pic.png", (200, 200))
    sq, ctx = make(tmp_path, img(w=800, h=800))
    rd, _ = make(tmp_path, img(w=800, h=800, radius=300))
    a, b = sq.frame(1.0), rd.frame(1.0)
    assert a.getpixel((35, 140)) == (200, 40, 40) and b.getpixel((35, 140)) == ctx.background().getpixel((35, 140))
    half, _ = make(tmp_path, img(w=800, h=800, opacity=0.5))
    mid = half.frame(1.0).getpixel((135, 240))
    bg = ctx.background().getpixel((135, 240))
    assert all(abs(m - (c + g) / 2) <= 2 for m, c, g in zip(mid, (200, 40, 40), bg))
    turned, _ = make(tmp_path, img(w=800, h=400, rotate=90))
    flat, _ = make(tmp_path, img(w=800, h=400))
    assert turned.frame(1.0).getbbox() and turned.frame(1.0).getpixel((135, 150)) == (200, 40, 40) != flat.frame(1.0).getpixel((135, 150))


def test_a_url_is_rejected_in_the_spec_with_a_hint():
    with pytest.raises(SpecError, match="elements\\[0\\].source: must be a local file; use a shot scene for web pages"):
        validate({"scenes": [{"type": "motion", "elements": [{"kind": "image", "source": "https://example.com/a.png", "w": 10}]}]})
    with pytest.raises(CaptureError, match="elements\\[3\\].source: must be a local file"):
        mi.load("http://example.com/a.png", None, [], "elements[3].source")


def test_a_path_outside_the_allowed_folders_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    picture(outside / "pic.png")
    with pytest.raises(UnsafeInput, match="outside the folders this run may read"):
        make(tmp_path, img(w=100), roots=[tmp_path / "inside"])


def test_a_missing_or_unreadable_file_names_the_element(tmp_path):
    with pytest.raises(CaptureError, match="elements\\[0\\].source: image not found"):
        make(tmp_path, img(w=100))
    (tmp_path / "pic.png").write_bytes(b"not an image")
    with pytest.raises(CaptureError, match="elements\\[1\\].source: cannot read pic.png"):
        make(tmp_path, {"kind": "rect", "w": 10, "h": 10}, img(w=100))


def test_an_image_over_the_pixel_limit_is_refused_at_its_path(tmp_path, monkeypatch):
    picture(tmp_path / "pic.png", (200, 100))
    monkeypatch.setattr(shots, "MAX_IMAGE_PIXELS", 10_000)
    with pytest.raises(CaptureError, match="elements\\[0\\].source: pic.png is 200x100; the limit is 10000 pixels"):
        make(tmp_path, img(w=100))


def test_nested_images_report_the_path_inside_the_group(tmp_path, monkeypatch):
    picture(tmp_path / "pic.png", (200, 100))
    monkeypatch.setattr(shots, "MAX_IMAGE_PIXELS", 10_000)
    with pytest.raises(CaptureError, match="elements\\[0\\].children\\[1\\].source"):
        make(tmp_path, {"kind": "group", "children": [{"kind": "rect", "w": 5, "h": 5}, {"kind": "image", "source": "pic.png", "w": 50}]})


def test_the_images_of_a_scene_share_a_byte_budget(tmp_path, monkeypatch):
    picture(tmp_path / "pic.png", (200, 100))
    monkeypatch.setattr(motion, "MAX_IMAGE_BYTES", 200 * 100 * 4 * 2)
    make(tmp_path, img(w=800), img(w=800, x=0.3))
    with pytest.raises(CaptureError, match="elements\\[2\\].source: the images of this scene need"):
        make(tmp_path, img(w=800), img(w=800, x=0.3), img(w=800, x=0.7))


def test_at_most_eight_images_per_scene():
    els = [{"kind": "image", "source": "a.png", "w": 10, "id": f"i{k}"} for k in range(9)]
    with pytest.raises(SpecError, match="scenes\\[0\\].elements: 9 images; the limit is 8 \\(MAX_IMAGES\\)"):
        validate({"scenes": [{"type": "motion", "elements": els}]})
    validate({"scenes": [{"type": "motion", "elements": els[:8]}]})
    assert mi.MAX_IMAGES == 8
    for bad in ({"kind": "image", "w": 10}, {"kind": "image", "source": "a.png"}, {"kind": "image", "source": " ", "w": 5}):
        with pytest.raises(SpecError):
            validate({"scenes": [{"type": "motion", "elements": [bad]}]})


def test_a_huge_zoomed_image_draws_only_what_the_frame_shows(tmp_path):
    picture(tmp_path / "pic.png", (400, 400), (10, 200, 10, 255))
    sc, ctx = make(tmp_path, img(w=4000, h=4000), W=1080, H=1920, camera=[{"at": 0, "seconds": 0, "zoom": 4}])
    frame = sc.frame(1.0)
    assert frame.getpixel((540, 960)) == (10, 200, 10) and frame.getpixel((2, 2)) == (10, 200, 10)
    assert sc.retained_bytes() <= motion.MOTION_CACHE_BYTES + motion.MAX_IMAGE_BYTES + 1080 * 1920 * 3
    assert sc._lru.bytes <= (1080 + 40) * (1920 + 40) * 4


def test_release_keeps_the_pinned_image_and_the_scene_draws_again(tmp_path):
    picture(tmp_path / "pic.png")
    sc, _ = make(tmp_path, img(w=300))
    first = sc.frame(1.0).tobytes()
    pinned = sc._image_bytes
    sc.release()
    assert sc.retained_bytes() == pinned > 0
    assert sc.frame(1.0).tobytes() == first


def test_a_crafted_decompression_bomb_header_is_a_capture_error_at_its_path(tmp_path):
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 20000, 20000, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b""))
    (tmp_path / "bomb.png").write_bytes(png)
    with pytest.raises(CaptureError, match="elements\\[2\\].source: cannot read bomb.png: Image size"):
        mi.load("bomb.png", tmp_path, [tmp_path], "elements[2].source")


@pytest.mark.parametrize("rotate", [0, 45])
def test_image_sprites_are_clipped_or_capped_and_recorded(tmp_path, rotate):
    Image.new("RGB", (1000, 1000), (30, 120, 200)).save(tmp_path / "pic.png")
    sc, ctx = make(tmp_path, img(w=4000, scale=8, rotate=rotate, radius=200), W=1080, H=1920)
    frame = sc.frame(1.0)
    assert frame.getpixel((540, 960)) == (30, 120, 200)
    assert 0 < sc.max_layer_pixels() <= motion.MAX_LAYER_PIXELS
    if rotate == 0:
        assert sc.max_layer_pixels() <= (1080 + 32) * (1920 + 32)      # only the visible part was resampled
    else:
        assert any("drawn smaller" in w for w in ctx.warnings)
