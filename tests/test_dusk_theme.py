"""The `dusk` theme (yamlreel/dusk_theme.py): registered as a built-in, its gradient, faces and fallbacks."""
from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from yamlreel import theme
from yamlreel.dusk_theme import DUSK_FACES
from yamlreel.scenebase import Ctx, studio_background
from yamlreel.scenes import build_scene
from yamlreel.specs import SpecError, validate
from yamlreel.theme import FACE_SETS, THEMES, Theme, font, get_theme, use_faces

DUSK = THEMES["dusk"]


@pytest.fixture(autouse=True)
def no_personal_config(tmp_path_factory, monkeypatch):
    empty = tmp_path_factory.mktemp("no-config")
    for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(empty / "config.yaml"))
    monkeypatch.chdir(empty)
    yield
    use_faces(None)


def test_dusk_is_a_built_in_theme_with_the_lamp_accent():
    assert isinstance(DUSK, Theme) and DUSK.name == "dusk"
    assert get_theme("dusk") is DUSK
    assert FACE_SETS["dusk"] is DUSK_FACES
    assert DUSK.bg == (0x0B, 0x10, 0x20) and DUSK.bg2 == (0x1A, 0x21, 0x47)
    assert DUSK.accent == (0xFF, 0xB3, 0x47)
    assert set(DUSK_FACES) <= set(theme._FONT_FILES)   # every face replaces a font kind the renderer asks for


@pytest.mark.parametrize("first", ["yamlreel.dusk_theme", "yamlreel.theme"])
def test_the_dusk_module_imports_in_any_order(first):
    code = f"import {first}; from yamlreel.theme import THEMES; assert THEMES['dusk'].accent == (255, 179, 71)"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=60)


def test_registering_dusk_leaves_the_other_themes_alone():
    assert {"sumi", "washi", "midnight", "editorial", "studio", "dusk"} <= set(THEMES)
    assert THEMES["studio"].faces == "avenir"
    assert all(THEMES[n].bg2 is None and THEMES[n].faces is None for n in ("sumi", "washi", "midnight", "editorial"))


def test_dusk_background_is_a_gradient_lighter_towards_the_horizon():
    img = np.asarray(studio_background(DUSK, 108, 192)).astype(int)
    assert img[186, 54].sum() > img[5, 54].sum()            # the bottom (horizon) is lighter than the top
    assert img[:, :, 2].mean() > img[:, :, 0].mean()         # indigo: blue leads red everywhere on average


def test_a_dusk_context_uses_the_dusk_faces_and_a_classic_one_resets_them(tmp_path):
    Ctx(108, 192, DUSK, tmp_path, tmp_path)
    assert theme._active_faces == DUSK_FACES
    Ctx(108, 192, THEMES["sumi"], tmp_path, tmp_path)
    assert theme._active_faces == {}
    assert font("serif_bold", 20).getname()[0].startswith("DejaVu")


def test_dusk_falls_back_to_dejavu_when_its_faces_are_missing(monkeypatch):
    monkeypatch.setattr(theme, "_load_face", lambda *_a: None)   # as on a machine without Georgia or Avenir Next
    use_faces("dusk")
    for kind in DUSK_FACES:
        assert font(kind, 22).getname()[0].startswith("DejaVu"), kind


def test_a_custom_theme_can_extend_dusk_and_keeps_its_gradient_and_faces():
    t = get_theme("night", {"night": {"extends": "dusk", "accent": "#ff8040"}})
    assert t.accent == (255, 128, 64)
    assert (t.bg2, t.glow, t.faces) == (DUSK.bg2, DUSK.glow, DUSK.faces)
    assert t.bg == DUSK.bg


def test_a_spec_can_choose_dusk_and_an_unknown_theme_lists_it():
    spec = validate({"theme": "dusk", "formats": ["vertical"],
                     "scenes": [{"type": "motion", "seconds": 2, "elements": [{"kind": "text", "text": "hi"}]}]})
    assert spec["_theme_obj"] is DUSK
    with pytest.raises(SpecError, match="dusk"):
        validate({"theme": "dawn", "scenes": [{"type": "title", "title": "x"}]})


def test_a_motion_scene_renders_on_the_dusk_background(tmp_path):
    spec = validate({"theme": "dusk", "formats": ["vertical"],
                     "scenes": [{"type": "motion", "seconds": 2,
                                 "elements": [{"kind": "circle", "r": 200, "fill": "accent", "stroke": "none",
                                               "at": 0, "enter": "none"}]}]})
    ctx = Ctx(216, 384, spec["_theme_obj"], tmp_path, tmp_path)
    scene = build_scene(spec["scenes"][0], ctx)
    img = np.asarray(scene.frame(1.0)).astype(int)
    assert tuple(img[192, 108]) == pytest.approx(DUSK.accent, abs=2)   # the lamp-coloured circle at the centre
    assert img[5, 5].sum() < 120                                         # the dark indigo corner around it
