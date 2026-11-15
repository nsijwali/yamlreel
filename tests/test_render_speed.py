"""The faster render paths draw the same pixels as the plain ones, and `render --profile` reports where time went."""
from __future__ import annotations

import io
import os
import random
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageChops, ImageDraw

from yamlreel import lineart_scene
from yamlreel.cli import main
from yamlreel.render import FRAMES_AHEAD, ffmpeg_exe
from yamlreel.scenes import SS, downsample

needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def no_personal_config(tmp_path_factory, monkeypatch):
    """Never read the developer's own config or YAMLREEL_* overrides."""
    empty = tmp_path_factory.mktemp("no-config")
    for name in ("CONFIG", "FONT_DIR"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))


def _layer(w: int, h: int, rng: random.Random, shapes: int) -> Image.Image:
    layer = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(shapes):
        x, y = rng.randint(0, w * SS - 120), rng.randint(0, h * SS - 120)
        bw, bh = rng.randint(40, w), rng.randint(30, h)
        d.rounded_rectangle([x, y, x + bw, y + bh], rng.randint(4, 40), fill=(30, 40, 60, rng.choice([255, 200, 90])),
                            outline=(79, 157, 255, 255), width=rng.randint(2, 10))
        d.line([x, y, rng.randint(0, w * SS), rng.randint(0, h * SS)], fill=(255, 200, 0, 240), width=8)
    return layer


@pytest.mark.parametrize("size", [(540, 960), (960, 540), (540, 540), (270, 480)])
def test_downsample_gives_the_pixels_of_a_whole_layer_resize(size):
    rng = random.Random(size[0] * 7 + size[1])
    for shapes in (1, 1, 3, 6):
        layer = _layer(*size, rng, shapes)
        whole = layer.resize(size, Image.LANCZOS)
        part, at = downsample(layer)
        mine = Image.new("RGBA", size, (0, 0, 0, 0))
        mine.paste(part, at)
        assert ImageChops.difference(whole, mine).getbbox() is None
        assert part.width * part.height <= size[0] * size[1]


def test_downsample_of_a_tall_narrow_shape_matches_too():
    """Pillow resizes a tall, narrow image down-then-across, which rounds differently from a full frame."""
    layer = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([271, 302, 594, 1199], 18, fill=(30, 40, 60, 255),
                                            outline=(79, 157, 255, 255), width=4)
    part, at = downsample(layer)
    mine = Image.new("RGBA", (540, 960), (0, 0, 0, 0))
    mine.paste(part, at)
    assert ImageChops.difference(layer.resize((540, 960), Image.LANCZOS), mine).getbbox() is None


def test_downsample_of_an_empty_layer_is_none():
    assert downsample(Image.new("RGBA", (200, 200), (0, 0, 0, 0))) is None


@needs_ffmpeg
def test_profile_prints_stages_and_scene_types(tmp_path, capsys):
    spec = {"formats": ["vertical"], "scenes": [
        {"type": "title", "title": "Hello", "seconds": 1.5},
        {"type": "diagram", "seconds": 1.5, "nodes": [{"id": "a", "label": "A", "x": 0.3, "y": 0.5},
                                                      {"id": "b", "label": "B", "x": 0.7, "y": 0.5}],
         "edges": [{"from": "a", "to": "b"}]}]}
    path = tmp_path / "v.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["render", str(path), "--out", str(out), "--preview", "--no-voice", "--music", "none", "--profile"]) == 0
    text = capsys.readouterr().out
    assert "profile vertical:" in text and "x real time" in text
    for stage in ("draw frames", "send frames to ffmpeg", "finish encoding"):
        assert stage in text
    assert "title" in text and "diagram" in text and "ms/frame" in text
    assert main(["render", str(path), "--out", str(out), "--preview", "--no-voice", "--music", "none"]) == 0
    assert "profile vertical:" not in capsys.readouterr().out


def test_the_direct_capture_gives_the_pixels_of_a_standard_screenshot(tmp_path):
    pytest.importorskip("playwright")
    from test_lineart import ctx
    from yamlreel.scenes import build_scene
    exe = os.environ.get("YAMLREEL_CHROMIUM")
    c = ctx(tmp_path)
    c.chromium = exe if exe and Path(exe).exists() else None
    try:
        holder = lineart_scene._Page.get(c)
    except Exception:
        lineart_scene._Page.close_all()
        pytest.skip("no Chromium: set YAMLREEL_CHROMIUM or run `playwright install chromium`")
    try:
        scene = build_scene({"type": "lineart", "text": "Hello there", "seconds": 5.0}, c)
        for t in (0.4, 2.0, 4.0):
            scene.frame(t)
            if holder._cdp is None:
                pytest.skip("this browser has no direct capture; the standard screenshot is used")
            direct = Image.open(io.BytesIO(holder.png())).convert("RGB")
            assert holder._cdp is not None, "the direct capture failed and fell back"
            standard = Image.open(io.BytesIO(holder.standard_png())).convert("RGB")
            assert direct.size == (c.W, c.H) == standard.size
            assert ImageChops.difference(direct, standard).getbbox() is None
    finally:
        lineart_scene._Page.close_all()


def _browser(tmp_path):
    """A frame context and the shared line-art browser, or a skip when there is no Chromium."""
    pytest.importorskip("playwright")
    from test_lineart import ctx
    exe = os.environ.get("YAMLREEL_CHROMIUM")
    c = ctx(tmp_path)
    c.chromium = exe if exe and Path(exe).exists() else None
    try:
        return c, lineart_scene._Page.get(c)
    except Exception:
        lineart_scene._Page.close_all()
        pytest.skip("no Chromium: set YAMLREEL_CHROMIUM, install Chrome, or run `playwright install chromium`")


def test_frames_drawn_ahead_are_the_frames_drawn_on_demand(tmp_path):
    from yamlreel.scenes import build_scene
    c, holder = _browser(tmp_path)
    try:
        for spec in ({"type": "lineart", "text": "Hello there", "seconds": 5.0},
                     {"type": "lineart", "kind": "carry", "items": ["A", "B"], "seconds": 8.0}):
            times = [round(0.3 + i / 30, 6) for i in range(0, 120, 7)]
            plain = build_scene(spec, c)
            on_demand = [plain.frame(t) for t in times]       # one page, one frame at a time
            ahead = build_scene(spec, c)
            drawn = []
            for i, t in enumerate(times):                      # announced the way the render loop announces frames
                for m in (range(i + 1, i + FRAMES_AHEAD + 1) if i == 0 else (i + FRAMES_AHEAD,)):
                    if m < len(times):
                        ahead.prefetch(times[m])
                drawn.append(ahead.frame(t))
            assert holder.dropped == 0, "a frame that was about to be collected was dropped and drawn twice"
            assert len(holder._tabs) == lineart_scene.TABS and not holder._ahead
            for a, b in zip(on_demand, drawn):
                assert a.size == b.size == (c.W, c.H) and ImageChops.difference(a, b).getbbox() is None
        assert not c.warnings
    finally:
        lineart_scene._Page.close_all()


def test_frames_asked_for_ahead_and_never_collected_are_dropped(tmp_path):
    from yamlreel.scenes import build_scene
    c, holder = _browser(tmp_path)
    try:
        scene = build_scene({"type": "lineart", "text": "Hello there", "seconds": 5.0}, c)
        for i in range(3 * lineart_scene.AHEAD_MAX):
            scene.prefetch(i / 30)
        assert len(holder._ahead) == lineart_scene.AHEAD_MAX and holder.dropped == 2 * lineart_scene.AHEAD_MAX
        assert scene.frame(4.0).size == (c.W, c.H)             # a frame nobody announced is still drawn
    finally:
        lineart_scene._Page.close_all()


def test_an_installed_chrome_is_tried_when_playwrights_own_chromium_is_missing(monkeypatch):
    pytest.importorskip("playwright")
    import asyncio
    tried = []

    class Chromium:
        async def launch(self, **kw):
            tried.append({k: v for k, v in kw.items() if k != "args"})
            raise RuntimeError(f"cannot start {len(tried)}")

    class Playwright:
        chromium = Chromium()

    def page(chromium):
        p = object.__new__(lineart_scene._Page)
        p.browser = None
        return p._start(None, chromium)

    monkeypatch.setattr(lineart_scene._Page, "_pw", Playwright())
    with pytest.raises(RuntimeError, match="cannot start 1"):    # the first error says how to install Chromium
        asyncio.run(page(None))
    assert tried == [{}, {"channel": "chrome"}, {"channel": "msedge"}]
    tried.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(page("/opt/my/chrome"))
    assert tried == [{"executable_path": "/opt/my/chrome"}]      # a browser named in the config is the only one used


def test_a_scene_is_told_ahead_about_exactly_the_frames_it_then_draws(tmp_path, monkeypatch):
    """Scene.prefetch gets the same local times, bit for bit, that Scene.frame gets a few frames later."""
    from yamlreel import render as render_mod
    from yamlreel.scenes import TypeScene
    told, drawn = [], []
    frame = TypeScene.frame
    monkeypatch.setattr(TypeScene, "prefetch", lambda self, t: told.append((id(self), t)))
    monkeypatch.setattr(TypeScene, "frame", lambda self, t: (drawn.append((id(self), t)), frame(self, t))[1])
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump({"formats": ["vertical"], "scenes": [
        {"type": "type", "text": "one", "seconds": 1.0}, {"type": "type", "text": "two", "seconds": 1.0}]}))
    if ffmpeg_exe() is None:
        pytest.skip("needs ffmpeg")
    assert main(["render", str(path), "--out", str(tmp_path / "out"), "--preview", "--no-voice", "--music", "none"]) == 0
    assert told and set(told) <= set(drawn)
    # every frame of the video was announced, except the first ones; the poster and contact-sheet frames are not
    assert len(set(drawn) - set(told)) <= render_mod.FRAMES_AHEAD + render_mod.MAX_CONTACT_FRAMES + 2


def test_the_look_ahead_buffer_is_larger_than_the_renders_look_ahead():
    assert lineart_scene.AHEAD_MAX > FRAMES_AHEAD + 1
