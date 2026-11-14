"""The `lineart` scene (vendored line-art-animation engine in headless Chromium) and the story default."""
from pathlib import Path

import pytest

from yamlreel import lineart_scene, story
from yamlreel.config import Config
from yamlreel.scenebase import Ctx
from yamlreel.scenes import SCENE_TYPES, build_scene
from yamlreel.specs import SpecError, validate
from yamlreel.theme import get_theme

from test_story import OUTLINE


def spec_of(*scenes):
    return validate({"meta": {"title": "t"}, "scenes": list(scenes)}, Config())


def ctx(tmp_path):
    return Ctx(W=270, H=480, theme=get_theme("sumi"), workdir=tmp_path, base_dir=tmp_path)


def test_a_presenter_scene_needs_text_and_a_carry_scene_needs_items():
    ok = spec_of({"type": "lineart", "text": "Hello"})["scenes"][0]
    assert ok["kind"] == "presenter" and ok["seconds"] == 6.0
    with pytest.raises(SpecError, match="missing text"):
        spec_of({"type": "lineart"})
    with pytest.raises(SpecError, match="missing items"):
        spec_of({"type": "lineart", "kind": "carry"})
    with pytest.raises(SpecError, match="kind"):
        spec_of({"type": "lineart", "kind": "juggle", "text": "x"})
    with pytest.raises(SpecError, match="at most 6"):
        spec_of({"type": "lineart", "kind": "carry", "items": list("abcdefg")})
    carry = spec_of({"type": "lineart", "kind": "carry", "items": ["A", "B"]})["scenes"][0]
    assert carry["items"] == ["A", "B"]


def test_story_opens_with_the_line_art_presenter_unless_told_not_to():
    spec = story.build_spec(story.parse_outline(OUTLINE))
    assert spec["scenes"][0]["type"] == "lineart" and spec["scenes"][0]["seconds"] >= 4
    spec = story.build_spec(story.parse_outline(OUTLINE), lineart=False)
    assert spec["scenes"][0]["type"] == "type"


def test_the_scene_falls_back_to_typed_text_when_chromium_is_unavailable(tmp_path, monkeypatch):
    def boom(_ctx):
        raise RuntimeError("no chromium")
    monkeypatch.setattr(lineart_scene._Page, "get", classmethod(lambda cls, c: boom(c)))
    c = ctx(tmp_path)
    scene = build_scene({"type": "lineart", "text": "Hello there", "seconds": 4.0}, c)
    img = scene.frame(2.0)
    assert img.size == (270, 480)
    assert any("drawn as typed text" in w for w in c.warnings)
    assert scene.frame(3.0).size == (270, 480)   # keeps working after the fallback


def test_the_scene_is_registered_and_the_engine_ships():
    assert SCENE_TYPES["lineart"] is lineart_scene.LineArtScene
    from importlib import resources
    root = resources.files("yamlreel").joinpath("lineart")
    assert "line-art-animation" in root.joinpath("engine.js").read_text("utf-8")[:300]
    assert "MIT License" in root.joinpath("LICENSE-line-art-animation").read_text("utf-8")


def test_screen_text_reads_the_lineart_hook_or_items():
    from yamlreel.voice import screen_text
    assert "stampede" in screen_text({"type": "lineart", "text": "What is a stampede?"})
    assert "Shard 2" in screen_text({"type": "lineart", "kind": "carry", "items": ["Shard 1", "Shard 2"]})


def test_a_carry_scene_gets_enough_seconds_for_every_figure():
    sc = spec_of({"type": "lineart", "kind": "carry", "items": list("abcdef")})["scenes"][0]
    assert sc["seconds"] >= 7.5
    sc = spec_of({"type": "lineart", "kind": "carry", "items": list("abcdef"), "seconds": 5})["scenes"][0]
    assert sc["seconds"] == 5


def test_the_engine_parses():
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    from importlib import resources
    path = resources.files("yamlreel").joinpath("lineart/engine.js")
    assert subprocess.run([node, "--check", str(path)], capture_output=True).returncode == 0


def test_a_real_frame_is_drawn_when_chromium_is_available(tmp_path):
    import os
    pytest.importorskip("playwright")
    exe = os.environ.get("YAMLREEL_CHROMIUM")
    if exe and not Path(exe).exists():
        exe = None
    c = ctx(tmp_path)
    c.chromium = exe     # without one, playwright's own Chromium is tried; skip if it is not installed
    try:
        lineart_scene._Page.get(c)
    except Exception:
        lineart_scene._Page.close_all()
        pytest.skip("no Chromium: set YAMLREEL_CHROMIUM or run `playwright install chromium`")
    try:
        for spec in ({"type": "lineart", "text": "Hello there", "seconds": 5.0},
                     {"type": "lineart", "kind": "carry", "items": ["A", "B"], "seconds": 8.0}):
            img = build_scene(spec, c).frame(4.0)
            assert img.size == (270, 480) and not c.warnings
            assert len(set(img.getdata())) > 20    # something was drawn, not one flat colour
        build_scene({"type": "lineart", "text": "word " * 60, "seconds": 5.0}, c).frame(4.0)
        assert any("shrunk" in w for w in c.warnings)
    finally:
        lineart_scene._Page.close_all()
