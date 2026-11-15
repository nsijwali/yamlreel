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
from yamlreel.render import ffmpeg_exe
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
            standard = Image.open(io.BytesIO(holder.page.screenshot(type="png"))).convert("RGB")
            assert direct.size == (c.W, c.H) == standard.size
            assert ImageChops.difference(direct, standard).getbbox() is None
    finally:
        lineart_scene._Page.close_all()
