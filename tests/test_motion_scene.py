"""The `motion` scene: determinism, sharp text, no jitter, sprite parity, memory, audio and Chrome independence."""
from __future__ import annotations

import hashlib
import random
import sys
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageDraw

from yamlreel import anim
from yamlreel import lineart_scene
from yamlreel import motion_scene as motion
from yamlreel.render import Runtime, Timeline, _prepare_audio, energy_sections, plan_durations
from yamlreel.scenebase import Ctx, _appear, downsample
from yamlreel.scenes import SCENE_TYPES, build_scene
from yamlreel.specs import validate
from yamlreel.textkit import blit
from yamlreel.theme import get_theme


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def motion_spec(*elements: dict[str, Any], seconds: float = 6, **extra: Any) -> dict[str, Any]:
    return validate({"scenes": [{"type": "motion", "elements": list(elements), "seconds": seconds, **extra}]})["scenes"][0]


def make(tmp_path, *elements, W=270, H=480, seconds=6, **extra) -> tuple[motion.MotionScene, Ctx]:
    ctx = Ctx(W=W, H=H, theme=get_theme("midnight"), workdir=tmp_path, base_dir=tmp_path)
    return build_scene(motion_spec(*elements, seconds=seconds, **extra), ctx), ctx   # type: ignore[return-value]


def digest(img: Image.Image) -> str:
    return hashlib.sha256(img.tobytes()).hexdigest()


def same(a: Image.Image, b: Image.Image) -> bool:
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


BEAT = [
    {"id": "card", "kind": "rect", "w": 600, "h": 160, "y": 0.3, "enter": "slide-up", "emphasis": {"type": "pulse", "at": 2.0}},
    {"kind": "text", "text": "GET /user/42", "font": "mono", "size": 50, "y": 0.3, "at": 0.35, "enter": "slide-up"},
    {"id": "dot", "kind": "circle", "r": 40, "y": 0.5, "at": 1.0, "enter": "pop", "end": 4.5,
     "moves": [{"at": 1.6, "seconds": 0.7, "y": 0.6, "scale": 1.4}]},
    {"kind": "text", "text": "Stop guessing now", "size": 70, "y": 0.75, "split": "words", "stagger": 0.1, "enter": "pop"},
    {"kind": "circle", "r": 30, "fill": "none", "stroke": "accent", "x": 0.2, "y": 0.9, "enter": "draw"},
]
TIMES = [0.0, 0.37, 1.0, 1.9, 2.5, 5.95]


# --- registration -----------------------------------------------------------------------------------------

def test_motion_is_a_registered_scene_type_with_all_kinds():
    assert SCENE_TYPES["motion"] is motion.MotionScene
    assert set(motion.KINDS) == {"text", "rect", "circle", "group", "arrow", "path", "image"}


# --- AC-1: determinism --------------------------------------------------------------------------------------

def test_frames_depend_only_on_t_not_on_order_or_instance(tmp_path):
    forward, _ = make(tmp_path, *BEAT)
    want = {t: digest(forward.frame(t)) for t in TIMES}
    fresh, _ = make(tmp_path, *BEAT)
    assert {t: digest(fresh.frame(t)) for t in TIMES} == want                          # two fresh instances
    rev, _ = make(tmp_path, *BEAT)
    assert {t: digest(rev.frame(t)) for t in reversed(TIMES)} == want                  # reverse order
    shuffled, _ = make(tmp_path, *BEAT)
    order = list(TIMES)
    random.Random(3).shuffle(order)
    assert {t: digest(shuffled.frame(t)) for t in order} == want                       # shuffled order
    off, _ = make(tmp_path, *BEAT)
    off._frame_cache = False
    assert {t: digest(off.frame(t)) for t in TIMES} == want                            # frame cache off


def test_frames_from_a_timeline_match_a_lone_scene_and_the_only_path(tmp_path):
    raw = {"transition": "cut", "scenes": [
        {"type": "title", "title": "Intro", "seconds": 2},
        {"type": "motion", "elements": BEAT, "seconds": 6, "transition": "cut"}]}
    both = Timeline(validate(raw), "vertical", tmp_path, scale=0.25)
    only = Timeline(validate({"transition": "cut", "scenes": [raw["scenes"][1]]}), "vertical", tmp_path, scale=0.25)
    alone = build_scene(motion_spec(*BEAT, transition="cut"), both.ctx)
    for t in TIMES:
        want = digest(alone.frame(t))
        assert digest(both._scene_frame(1, t)) == want
        assert digest(only._scene_frame(0, t)) == want
        assert digest(both.frame_at(both.starts[1] + t)) == digest(only.frame_at(only.starts[0] + t))   # with the blend path


# --- AC-3: sharp text at rest ---------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", [(270, 480), (480, 270)])
def test_text_at_rest_is_pasted_exactly_as_blit_pastes_it(tmp_path, fmt):
    sc, ctx = make(tmp_path, {"kind": "text", "text": "Hello motion", "size": 90, "color": "accent", "y": 0.4},
                   W=fmt[0], H=fmt[1])
    blk = ctx.block("Hello motion", ctx.display, int(round(90 * ctx.u * ctx.fs)), ctx.W * 0.84, align="center")
    (place,) = sc.placements(3.0)
    assert (place.x, place.y) == (round(ctx.W * 0.5), round(ctx.H * 0.4))
    want = ctx.background()
    blit(want, blk, (place.x, place.y), ctx.theme.accent, 1.0, "mc")
    assert same(sc.frame(3.0), want)


def test_a_typed_prefix_keeps_the_line_layout_and_the_full_text_matches_the_block(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "text", "text": "Typed text that wraps over lines", "size": 80, "max_w": 0.5, "enter": "draw"})
    leaf = next(iter(sc.leaves.values()))
    prep = leaf.prep
    assert len(prep.lines) >= 2
    full = sc._draw_lines(prep, prep.blk.size, prep.blk.width, prep.lines, prep.chars)
    assert same(full.convert("RGB"), prep.blk.mask.convert("RGB"))           # my layout is make_block's layout
    for k in (1, 4, prep.chars // 2, prep.chars - 1):
        typed = sc._typed(leaf, k)
        assert typed.size == prep.blk.mask.size and ImageChops.subtract(typed, prep.blk.mask).getbbox() is None
        assert typed.getbbox() is not None
    last_frame = sc.frame(5.0)
    assert same(last_frame, sc.frame(5.5))
    done = ctx.background()
    blit(done, prep.blk, (round(ctx.W / 2), round(ctx.H / 2)), ctx.theme.fg, 1.0, "mc")
    assert same(last_frame, done)                                             # the finished type-on is the plain text


def test_scaled_and_turned_text_stays_centred_on_its_anchor(tmp_path):
    base = {"kind": "text", "text": "Pop", "size": 120, "y": 0.5}
    rest, ctx = make(tmp_path, base)
    ink = lambda img: ImageChops.difference(img.convert("RGB"), ctx.background()).getbbox()  # noqa: E731
    x0, y0, x1, y1 = ink(rest.frame(3.0))
    for extra in ({"enter": "pop"}, {"scale": 1.5}, {"rotate": 20}, {"opacity": 0.5, "scale": 0.7}):
        sc, _ = make(tmp_path, {**base, **extra})
        t = 0.35 + 0.2 if "enter" in extra else 3.0
        a, b, c, d = ink(sc.frame(t))
        assert abs((a + c) / 2 - (x0 + x1) / 2) <= 3, extra
        assert abs((b + d) / 2 - (y0 + y1) / 2) <= 12, extra               # descenders: not exactly the block centre
    sc, _ = make(tmp_path, {**base, "scale": 1.5})
    assert sc.frame(3.0).size == (270, 480) and sc.retained_bytes() > 0


# --- AC-4: no jitter ------------------------------------------------------------------------------------------

def test_a_slide_moves_by_whole_pixels_that_never_reverse_and_differ_by_at_most_one(tmp_path):
    sc, _ = make(tmp_path, {"id": "b", "kind": "rect", "w": 400, "h": 100, "at": 1.0,
                            "enter": {"type": "slide-up", "seconds": 1.0, "easing": "linear"}}, W=1080, H=1920)
    ys = [sc.placements(1.0 + k / 30)[0].y for k in range(0, 31)]
    assert all(b <= a for a, b in zip(ys, ys[1:])), "a slide up never goes down"
    steps = [a - b for a, b in zip(ys, ys[1:])]
    assert max(steps) - min(steps) <= 1 and sum(steps) == anim.SLIDE_DISTANCE


def test_a_card_and_its_label_keep_their_offset_on_every_frame(tmp_path):
    sc, _ = make(tmp_path,
                 {"id": "r", "kind": "rect", "w": 600, "h": 160, "y": 0.3, "at": 0.4, "enter": "slide-up"},
                 {"id": "l", "kind": "text", "text": "label", "y": 0.3, "at": 0.4, "enter": "slide-up"}, W=1080, H=1920)
    offsets = set()
    for k in range(0, 60):
        p = {pl.name: pl for pl in sc.placements(0.4 + k / 30)}
        if "r" in p and "l" in p:
            offsets.add((p["l"].x - p["r"].x, p["l"].y - p["r"].y))
    assert len(offsets) == 1


def test_split_words_keep_their_offsets_while_the_line_moves(tmp_path):
    sc, _ = make(tmp_path, {"kind": "text", "text": "three small words", "at": 0.0, "enter": "none", "split": "words",
                            "moves": [{"at": 0.0, "seconds": 2.0, "y": 0.7, "easing": "linear"}]}, W=1080, H=1920)
    seen = set()
    for k in range(0, 60):
        ps = sc.placements(k / 30)
        if len(ps) == 3:                                                              # every word has appeared
            seen.add(tuple((p.x - ps[0].x, p.y - ps[0].y) for p in ps))               # and keeps its offset
    assert len(seen) == 1
    assert sc.placements(0.5)[0].y != sc.placements(1.5)[0].y


def test_split_words_use_the_size_the_line_shrank_to_and_do_not_overlap(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "text", "text": "Precomputed is ok", "size": 150, "max_w": 0.5, "enter": "none",
                              "at": 0, "split": "words"}, W=540, H=960)
    assert any(w.startswith("text shrunk to fit") for w in ctx.warnings)
    words = [lf for lf in sc.leaves.values()]
    parent_px = int(round(150 * ctx.u * ctx.fs))
    assert all(lf.prep.blk.size < parent_px for lf in words)                  # shrunk, and the words follow it
    assert len({lf.prep.blk.size for lf in words}) == 1
    placed = sc.placements(1.0)
    same_line = [p for p in placed if p.y == placed[0].y]
    checked = 0
    for a, b, la, lb in zip(placed, placed[1:], words, words[1:]):
        if a.y == b.y:
            checked += 1
            assert b.x - a.x >= (la.prep.blk.width + lb.prep.blk.width) / 2 - 2
    assert checked >= 1 and same_line


def test_frames_after_the_last_animation_are_byte_identical(tmp_path):
    sc, _ = make(tmp_path, *BEAT)
    sc._frame_cache = False
    frames = [digest(sc.frame(5.0 + k / 30)) for k in range(5)]
    assert len(set(frames)) == 1


# --- AC-5: a sprite equals the supersampled whole layer -------------------------------------------------------

def _expected(ctx: Ctx, f: int, draw) -> Image.Image:
    layer = Image.new("RGBA", (ctx.W * f, ctx.H * f), (0, 0, 0, 0))
    draw(ImageDraw.Draw(layer), f)
    out = ctx.background()
    small = downsample(layer, f)
    out.paste(small[0], small[1], small[0])
    return out


@pytest.mark.parametrize("quality,f", [("standard", 2), ("high", 3)])
def test_a_rect_sprite_matches_the_whole_layer_resize(tmp_path, quality, f):
    sc, ctx = make(tmp_path, {"kind": "rect", "w": 400, "h": 220, "radius": 30, "stroke_width": 8, "enter": "none", "at": 0},
                   W=540, H=960, quality=quality)
    th, u = ctx.theme, ctx.u
    cx, cy, w, h = 270, 480, 400 * u, 220 * u

    def draw(d, f):
        d.rounded_rectangle([(cx - w / 2) * f, (cy - h / 2) * f, (cx + w / 2) * f, (cy + h / 2) * f], int(30 * u * f),
                            fill=(*th.card, 255), outline=(*th.accent2, 255), width=int(8 * u * f))
    assert same(sc.frame(2.0), _expected(ctx, f, draw))


@pytest.mark.parametrize("quality,f", [("standard", 2), ("high", 3)])
def test_a_circle_sprite_matches_the_whole_layer_resize(tmp_path, quality, f):
    sc, ctx = make(tmp_path, {"kind": "circle", "r": 120, "stroke": "accent", "stroke_width": 10, "enter": "none", "at": 0,
                              "x": 0.4, "y": 0.4}, W=540, H=960, quality=quality)
    th, u = ctx.theme, ctx.u
    cx, cy, r = round(540 * 0.4), round(960 * 0.4), 120 * u

    def draw(d, f):
        d.ellipse([(cx - r) * f, (cy - r) * f, (cx + r) * f, (cy + r) * f], fill=(*th.accent, 255),
                  outline=(*th.accent, 255), width=int(10 * u * f))
    assert same(sc.frame(2.0), _expected(ctx, f, draw))


def test_draft_draws_at_1x_without_a_downsample(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "rect", "w": 400, "h": 220, "radius": 30, "enter": "none", "at": 0}, W=540, H=960,
                   quality="draft")
    want = ctx.background()
    u, th = ctx.u, ctx.theme
    ImageDraw.Draw(want).rounded_rectangle([270 - 200 * u, 480 - 110 * u, 270 + 200 * u, 480 + 110 * u], int(30 * u),
                                           fill=th.card, outline=th.accent2, width=int(4 * u))
    assert same(sc.frame(2.0), want)
    assert sc.max_layer_pixels() < 540 * 960


def test_opacity_blends_the_sprite_and_rotation_draws_a_polygon(tmp_path):
    full, ctx = make(tmp_path, {"kind": "rect", "w": 400, "h": 220, "enter": "none", "at": 0})
    half, _ = make(tmp_path, {"kind": "rect", "w": 400, "h": 220, "enter": "none", "at": 0, "opacity": 0.5})
    turned, _ = make(tmp_path, {"kind": "rect", "w": 400, "h": 220, "enter": "none", "at": 0, "rotate": 45})
    bg = ctx.background()
    a, b, c = full.frame(1.0), half.frame(1.0), turned.frame(1.0)
    centre = (135, 240)
    assert a.getpixel(centre) == ctx.theme.card
    assert all(abs(x - (p + q) / 2) <= 2 for x, p, q in zip(b.getpixel(centre), a.getpixel(centre), bg.getpixel(centre)))
    assert not same(a, c) and c.getpixel(centre) == ctx.theme.card
    corner = (135 + 48, 240 + 25)                              # inside the upright card, outside the turned one
    assert a.getpixel(corner) != c.getpixel(corner)


# --- flash ----------------------------------------------------------------------------------------------------

def test_a_flash_recolours_the_stroke_and_its_tag_sits_where_diagram_puts_it(tmp_path):
    rect = {"id": "box", "kind": "rect", "w": 400, "h": 220, "y": 0.5, "x": 0.5, "enter": "none", "at": 0, "stroke_width": 12}
    for (W, H) in ((540, 960), (960, 540)):
        sc, ctx = make(tmp_path, rect, flash=[{"node": "box", "at": 1.0, "text": "found", "color": "accent"}], W=W, H=H)
        t = 2.0
        got = sc.frame(t)
        u = ctx.u
        top = round(H * 0.5) - 110 * u
        stroke = got.getpixel((round(W * 0.5), int(top) + 3))                              # the stroke took the flash colour
        assert all(abs(a - b) <= 4 for a, b in zip(stroke, ctx.theme.accent)), stroke
        plain, _ = make(tmp_path, rect, flash=[{"node": "box", "at": 1.0, "color": "accent"}], W=W, H=H)
        want = plain.frame(t)
        tag = ctx.block("found", "sans_bold", (36 if ctx.vertical else 30) * u, W * 0.4, align="center")
        cx, cy = round(W * 0.5), round(H * 0.5)
        if ctx.vertical:
            blit(want, tag, (cx + 200 * u + 22 * u, cy), ctx.theme.accent, 1.0, "ml")
        else:
            blit(want, tag, (cx, cy + 110 * u + 20 * u), ctx.theme.accent, 1.0, "tc")
        assert same(got, want)
    before, _ = make(tmp_path, rect, flash=[{"node": "box", "at": 1.0, "text": "found"}])
    assert same(before.frame(0.9), make(tmp_path, rect)[0].frame(0.9))                    # nothing before `at`


def test_a_flash_on_a_split_text_recolours_every_word(tmp_path):
    sc, ctx = make(tmp_path, {"id": "line", "kind": "text", "text": "two words", "size": 100, "split": "words",
                              "enter": "none", "at": 0}, flash=[{"node": "line", "at": 1.0, "color": "accent"}])
    plain, _ = make(tmp_path, {"id": "line", "kind": "text", "text": "two words", "size": 100, "split": "words",
                               "enter": "none", "at": 0})
    assert not same(sc.frame(0.5), sc.frame(2.0)) and same(sc.frame(0.5), plain.frame(0.5))
    img = sc.frame(2.0)
    colours = {c for _, c in img.getcolors(maxcolors=100000) if c == ctx.theme.accent}
    assert colours


# --- C3, release, caches, formats, warnings ------------------------------------------------------------------

def test_every_call_returns_a_new_image_and_a_cached_frame_is_not_shared(tmp_path):
    sc, _ = make(tmp_path, *BEAT)
    a = sc.frame(5.0)
    b = sc.frame(5.0)                       # the frame cache answers this one
    assert a is not b and same(a, b)
    ImageDraw.Draw(a).rectangle([0, 0, 50, 50], fill=(255, 0, 255))     # the timeline pastes a caption and subtitles
    c = sc.frame(5.0)
    assert same(b, c) and not same(a, c)


def test_release_empties_every_cache_and_the_scene_can_draw_again(tmp_path):
    sc, _ = make(tmp_path, *BEAT)
    before = [digest(sc.frame(t)) for t in TIMES]
    assert sc.retained_bytes() > 0 and sc.cached_layers() > 0
    sc.release()
    assert sc.retained_bytes() == 0 and sc.cached_layers() == 0
    assert [digest(sc.frame(t)) for t in TIMES] == before


@pytest.mark.parametrize("fmt", ["landscape", "vertical", "square"])
def test_all_three_formats_render(tmp_path, fmt):
    tl = Timeline(validate({"scenes": [{"type": "motion", "elements": BEAT, "title": "T", "seconds": 4}]}), fmt, tmp_path, scale=0.2)
    for t in (0.0, 0.5, 1.5, 3.9):
        assert tl.frame_at(t).size == (tl.ctx.W, tl.ctx.H)


def test_vertical_overrides_move_an_element_only_in_vertical_frames(tmp_path):
    el = {"kind": "rect", "w": 100, "h": 100, "x": 0.25, "vx": 0.75, "y": 0.5, "enter": "none", "at": 0}
    h, _ = make(tmp_path, el, W=480, H=270)
    v, _ = make(tmp_path, el, W=270, H=480)
    assert h.placements(1.0)[0].x == 120 and v.placements(1.0)[0].x == 203   # 202.5 rounds half up


def test_off_frame_elements_are_culled_and_the_scene_still_draws(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "rect", "w": 100, "h": 100, "x": 1.4, "enter": "none", "at": 0},
                   {"kind": "text", "text": "gone", "y": -0.4, "enter": "none", "at": 0})
    assert same(sc.frame(1.0), ctx.background())
    assert any("sits outside the frame at rest" in w for w in ctx.warnings)


def test_warnings_for_late_elements_slow_moves_and_overlaps(tmp_path):
    sc, ctx = make(tmp_path, {"id": "late", "kind": "rect", "w": 100, "h": 100, "at": 7.2},
                   {"id": "slow", "kind": "rect", "w": 100, "h": 100, "at": 0, "enter": "none",
                    "moves": [{"at": 0.5, "seconds": 3.0, "x": 0.52}]},
                   {"id": "under", "kind": "rect", "w": 300, "h": 100, "y": 0.1, "enter": "none", "at": 0},
                   title="A title", seconds=6)
    assert "motion element 'late' enters at 7.2s, after the 6s scene ends" in ctx.warnings
    assert any("'slow' moves less than half a pixel per frame" in w for w in ctx.warnings)
    assert any(w.startswith("the title overlaps motion element 'under'") and w.endswith("(vy)") for w in sc.layout_warnings(None))
    box = (0.0, 0.0, 270.0, 480.0)
    assert any(w.startswith("the caption overlaps") for w in sc.layout_warnings(box))


def test_the_flash_fields_are_the_diagrams(tmp_path):
    from yamlreel.scenes import DiagramScene
    assert motion.FLASH_FIELDS == DiagramScene.FLASH_FIELDS


# --- AC-6: memory ----------------------------------------------------------------------------------------------

def test_sixty_large_pulsing_elements_stay_inside_the_retained_budget(tmp_path):
    els = [{"kind": "rect", "w": 700, "h": 300, "x": 0.5, "y": 0.1 + 0.012 * k, "at": 0, "enter": "none",
            "fill": "card" if k % 2 else "none", "stroke": "accent2",
            "emphasis": {"type": "pulse", "at": 0.1, "seconds": 0.5, "repeat": 10}} for k in range(60)]
    sc, ctx = make(tmp_path, *els, W=540, H=960, seconds=6)
    for k in range(0, 50, 3):
        sc.frame(0.1 + k * 0.1)
    frame_bytes = ctx.W * ctx.H * 3
    assert sc.retained_bytes() <= motion.MOTION_CACHE_BYTES + motion.MAX_IMAGE_BYTES + frame_bytes
    assert 0 < sc.max_layer_pixels() <= motion.MAX_LAYER_PIXELS


def test_the_lru_evicts_to_its_budget_and_rebuilds_what_it_dropped(tmp_path):
    els = [{"kind": "rect", "w": 500, "h": 200, "y": 0.5, "at": 0, "enter": "none",
            "emphasis": {"type": "pulse", "at": 0.1, "seconds": 3.0, "repeat": 1}}]
    sc, _ = make(tmp_path, *els, W=540, H=960)
    sc._lru.budget = 250_000    # room for two or three 100 KB sprites
    want = {}
    for t in (0.5, 1.0, 1.6, 2.2, 2.8):
        want[t] = digest(sc.frame(t))
    assert sc._lru.bytes <= 250_000
    assert len(sc._lru) >= 1
    again, _ = make(tmp_path, *els, W=540, H=960)
    assert {t: digest(again.frame(t)) for t in want} == want


def test_an_oversized_element_stays_clipped_and_under_the_pixel_ceiling_at_high(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "rect", "w": 4000, "h": 4000, "scale": 8, "enter": "none", "at": 0},
                   W=1080, H=1920, quality="high")
    img = sc.frame(1.0)
    assert img.getpixel((540, 960)) == ctx.theme.card
    f = 2                                                      # high (3) did not fit, so it fell back to 2
    assert sc.max_layer_pixels() <= motion.MAX_LAYER_PIXELS
    assert sc.max_layer_pixels() <= (1080 + 2 * 16) * (1920 + 2 * 16) * f * f
    assert any("too large to supersample" in w for w in ctx.warnings)
    assert sc.retained_bytes() <= motion.MOTION_CACHE_BYTES + 1080 * 1920 * 3


def test_the_text_mask_for_scaled_drawing_is_capped(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "text", "text": "Big", "size": 100, "scale": 8, "enter": "none", "at": 0})
    leaf = next(iter(sc.leaves.values()))
    sc.frame(1.0)
    mask = sc._hi(leaf)
    assert mask.width * mask.height <= motion.MAX_HI_MASK_PIXELS and leaf.prep.super_s <= motion.TEXT_SUPER_MAX
    assert any("scales up to 8.0x" in w for w in ctx.warnings)


# --- AC-8: audio is untouched -----------------------------------------------------------------------------------

def test_a_motion_scene_changes_neither_the_scene_lengths_nor_the_music(tmp_path):
    common = {"seconds": 3, "energy": "high", "narration": "Hello there.", "caption": "x"}
    def spec(scene):
        return validate({"music": {"style": "koto", "seed": 5}, "scenes": [
            {"type": "title", "title": "A", "seconds": 2, "energy": "low"}, {**common, **scene}]})
    a = spec({"type": "motion", "elements": BEAT})
    b = spec({"type": "title", "title": "same"})
    da, db = plan_durations(a), plan_durations(b)
    assert da == db
    starts = [0.0, da[0]]
    assert energy_sections(a, starts, da) == energy_sections(b, starts, db)
    wa = _prepare_audio(a, sum(da), tmp_path / "a", None, Runtime(roots=[tmp_path]), energy_sections(a, starts, da))
    wb = _prepare_audio(b, sum(db), tmp_path / "b", None, Runtime(roots=[tmp_path]), energy_sections(b, starts, db))
    assert wa.read_bytes() == wb.read_bytes()


# --- AC-11: no Chrome ------------------------------------------------------------------------------------------

def test_a_motion_scene_builds_and_renders_without_playwright_or_a_browser(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    def no_page(*a, **k):
        raise AssertionError("a browser page was created for a motion scene")
    monkeypatch.setattr(lineart_scene._Page, "__init__", no_page)
    tl = Timeline(validate({"scenes": [{"type": "motion", "elements": BEAT, "seconds": 3}]}), "vertical", tmp_path, scale=0.2)
    for t in (0.2, 1.4, 2.9):
        assert tl.frame_at(t).size == (tl.ctx.W, tl.ctx.H)
    assert lineart_scene._Page._open == {}


# --- review fixes: masks, images and caches are clipped to the frame -----------------------------------------------

def test_the_lru_refuses_one_entry_bigger_than_its_budget():
    lru = motion._Lru(1000)
    lru.put("a", "A", 600)
    lru.put("huge", "H", 5000)
    assert lru.bytes == 600 and len(lru) == 1 and lru.get("huge") is motion._MISS and lru.get("a") == "A"
    lru.put("b", "B", 600)                       # normal eviction still works
    assert lru.bytes <= 1000 and lru.get("a") is motion._MISS


def test_a_clipped_text_mask_matches_the_whole_mask_where_they_overlap(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "text", "text": "Clipped", "size": 60, "scale": 2, "enter": "none", "at": 0},
                   W=540, H=960)
    leaf = next(iter(sc.leaves.values()))
    q = sc._plan(1.0)[0].q
    full, (fx, fy) = sc.text_mask(leaf, q, False)
    assert full.width < 540                                    # it fits: this is the whole mask
    shifted = motion.replace(q, ix=30)                         # half of it hangs off the left edge
    part, (px, py) = sc.text_mask(leaf, shifted, False)
    assert px == -16 and part.width < full.width
    # the same pixels at another x: compare the overlap away from the window edge
    ox = int(30 - round(leaf.prep.blk.width * q.scale) / 2)
    x0 = px + 4
    a = part.crop((x0 - px, 0, part.width, part.height))
    b = full.crop((x0 - ox, 0, part.width + px - ox, part.height))
    assert a.size == b.size and ImageChops.difference(a, b).getbbox() is None


def test_text_that_misses_the_frame_makes_no_mask_and_no_layer(tmp_path):
    sc, ctx = make(tmp_path, {"kind": "text", "text": "Away", "size": 30, "scale": 2, "x": 1.5, "y": 0.5, "enter": "none", "at": 0})
    assert sc.frame(1.0) is not None and sc.max_layer_pixels() == 0
    far, _ = make(tmp_path, {"kind": "text", "text": "Away", "size": 30, "scale": 2, "rotate": 30, "x": 1.5, "y": 0.5, "enter": "none", "at": 0})
    far.frame(1.0)
    assert far.max_layer_pixels() == 0


@pytest.mark.parametrize("rotate", [0, 33])
def test_a_huge_composed_scale_draws_only_the_window_the_frame_shows(tmp_path, rotate, monkeypatch):
    monkeypatch.setattr(motion, "MAX_HI_MASK_PIXELS", 40_000)          # force the window path for turned text at small size
    el = {"kind": "text", "text": "Hi", "size": 400, "scale": 8, "rotate": rotate, "enter": "none", "at": 0}
    sc, ctx = make(tmp_path, el, W=270, H=480, camera=[{"at": 0, "seconds": 0, "zoom": 4}])
    img = sc.frame(1.0)
    assert img.getbbox() and sc.max_layer_pixels() <= (270 + 2 * 16) * (480 + 2 * 16)
    assert sc.max_layer_pixels() <= motion.MAX_LAYER_PIXELS and sc.retained_bytes() <= motion.MOTION_CACHE_BYTES + 270 * 480 * 3


def test_the_windowed_turned_text_agrees_with_the_whole_turned_mask(tmp_path, monkeypatch):
    el = {"kind": "text", "text": "Turn", "size": 100, "scale": 2, "rotate": 25, "enter": "none", "at": 0}
    whole, ctx = make(tmp_path, el, W=270, H=480)
    ref = whole.frame(1.0)
    monkeypatch.setattr(motion, "MAX_HI_MASK_PIXELS", 1_000)           # the window path for the same text
    windowed, _ = make(tmp_path, el, W=270, H=480)
    got = windowed.frame(1.0)
    bg = ctx.background()
    a, b = ImageChops.difference(ref, bg).getbbox(), ImageChops.difference(got, bg).getbbox()
    assert a and b and all(abs(x - y) <= 2 for x, y in zip(a, b))
    def ink_and_centre(img):
        m = ImageChops.difference(img, bg).convert("L")
        data = m.tobytes()
        total = sum(data)
        return (total, sum((i % m.width) * v for i, v in enumerate(data)) / total,
                sum((i // m.width) * v for i, v in enumerate(data)) / total)
    (ia, xa, ya), (ib, xb, yb) = ink_and_centre(ref), ink_and_centre(got)
    assert abs(ia - ib) < 0.08 * ia and abs(xa - xb) < 2.0 and abs(ya - yb) < 2.0   # the whole-mask path rounds its paste to a pixel


def test_every_raster_allocation_is_recorded(tmp_path):
    sc, _ = make(tmp_path, {"kind": "text", "text": "Scaled", "size": 80, "scale": 2, "enter": "none", "at": 0})
    assert sc.max_layer_pixels() == 0
    sc.frame(1.0)
    assert sc.max_layer_pixels() > 0                                   # a text mask counts, not only shape layers


def test_scales_that_multiply_past_the_limit_are_refused_by_the_validator():
    el = {"kind": "text", "text": "x", "size": 60, "scale": 8}
    for _ in range(3):
        el = {"kind": "group", "scale": 8, "children": [el]}
    with pytest.raises(Exception, match="scales multiply to 64.0x with the groups around it; the limit is 16x"):
        validate({"scenes": [{"type": "motion", "elements": [el]}]})
    ok = {"kind": "group", "scale": 4, "children": [{"kind": "text", "text": "x", "scale": 4, "moves": [{"at": 1, "scale": 2}]}]}
    validate({"scenes": [{"type": "motion", "elements": [ok]}]})


def test_a_huge_oversized_text_image_and_turned_image_stay_inside_every_limit(tmp_path):
    """The oversized-element test with every kind: scale 8, zoom 4, one image turned 45 degrees, on 1080x1920."""
    Image.new("RGB", (800, 800), (20, 150, 30)).save(tmp_path / "pic.png")
    els = [{"kind": "rect", "w": 4000, "h": 4000, "scale": 8, "enter": "none", "at": 0},
           {"kind": "text", "text": "Big", "size": 400, "scale": 8, "rotate": 20, "enter": "none", "at": 0},
           {"kind": "text", "text": "Big", "size": 400, "scale": 8, "enter": "none", "at": 0},
           {"kind": "image", "source": "pic.png", "w": 4000, "scale": 8, "enter": "none", "at": 0},
           {"kind": "image", "source": "pic.png", "w": 4000, "scale": 8, "rotate": 45, "radius": 400, "enter": "none", "at": 0}]
    ctx = Ctx(W=1080, H=1920, theme=get_theme("midnight"), workdir=tmp_path, base_dir=tmp_path, roots=[tmp_path])
    spec = validate({"scenes": [{"type": "motion", "elements": els, "seconds": 6, "quality": "high",
                                 "camera": [{"at": 0, "seconds": 0, "zoom": 4}]}]})["scenes"][0]
    sc = build_scene(spec, ctx)
    img = sc.frame(1.0)
    assert img.size == (1080, 1920)
    assert 0 < sc.max_layer_pixels() <= motion.MAX_LAYER_PIXELS
    assert sc.retained_bytes() <= motion.MOTION_CACHE_BYTES + motion.MAX_IMAGE_BYTES + 1080 * 1920 * 3
    assert sc._lru.bytes <= motion.MOTION_CACHE_BYTES
    assert any("drawn smaller" in w or "too large" in w for w in ctx.warnings)


def test_a_single_huge_entry_is_not_kept_and_the_budget_holds(tmp_path):
    sc, _ = make(tmp_path, {"kind": "rect", "w": 500, "h": 200, "enter": "none", "at": 0}, W=540, H=960)
    sc._lru.budget = 1_000                                              # smaller than any sprite
    for t in (1.0, 2.0):
        sc.frame(t)
    assert sc._lru.bytes <= 1_000 and sc.retained_bytes() <= 1_000 + 540 * 960 * 3
