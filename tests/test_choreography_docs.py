"""The YAML snippets in docs/choreography/**/rules.md must keep validating, so the guide cannot rot."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from yamlreel.specs import validate

ROOT = Path(__file__).resolve().parent.parent / "docs" / "choreography"
RULES = sorted(ROOT.glob("*/rules.md"))
FENCE = re.compile(r"```yaml\n(.*?)```", re.S)
VERTICAL_BOTTOM = 0.69  # centre limit; element heights are checked by eye in the frame sheets
MAX_RULE_LINES = 65


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def snippets() -> list[tuple[str, int, str]]:
    found = []
    for path in RULES:
        for i, m in enumerate(FENCE.finditer(path.read_text(encoding="utf-8"))):
            found.append((f"{path.parent.name}#{i}", i, m.group(1)))
    return found


def as_spec(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    scenes = data if isinstance(data, list) else data["scenes"]
    return {"theme": "studio", "formats": ["vertical"], "mix": {}, "scenes": scenes}


def test_every_move_folder_has_rules_and_is_indexed():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    folders = sorted(p.name for p in ROOT.iterdir() if p.is_dir())
    assert 4 <= len(folders) <= 10   # a short guide: a handful of moves, not a catalogue
    for name in folders:
        assert (ROOT / name / "rules.md").is_file(), name
        assert f"{name}/rules.md" in readme, f"{name} missing from the README index"
    assert (ROOT / "failures.md").is_file()


@pytest.mark.parametrize("path", RULES, ids=lambda p: p.parent.name)
def test_rules_are_terse_and_carry_a_snippet(path):
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= MAX_RULE_LINES
    assert FENCE.search(text), "needs a yaml snippet"
    for heading in ("**Use when**", "**Safe zone.**", "**Timing.**", "**Mistakes to avoid**"):
        assert heading in text, heading


@pytest.mark.parametrize("name,_i,text", snippets(), ids=lambda v: v if isinstance(v, str) else None)
def test_snippet_validates_as_a_studio_vertical_spec(name, _i, text):
    spec = validate(as_spec(text))
    assert spec["scenes"], name


@pytest.mark.parametrize("name,_i,text", snippets(), ids=lambda v: v if isinstance(v, str) else None)
def test_snippet_keeps_motion_inside_the_vertical_safe_zone(name, _i, text):
    def ys(el: dict[str, Any]):
        if isinstance(el.get("y"), (int, float)):
            yield el["y"]
        for mv in el.get("moves", []):
            if isinstance(mv.get("y"), (int, float)):
                yield mv["y"]

    for scene in as_spec(text)["scenes"]:
        for el in scene.get("elements", []):
            if "children" in el:  # child y values are offsets inside the group
                assert max(ys(el), default=0) <= VERTICAL_BOTTOM, (name, el)
            else:
                assert max(ys(el), default=0) <= VERTICAL_BOTTOM, (name, el)


def test_a_broken_snippet_is_caught():
    broken = "- type: motion\n  seconds: 3\n  elements:\n    - {kind: circle, r: 10, at: 1, emphasis: {type: pulse, at: 0.5}}\n"
    from yamlreel.specs import SpecError

    with pytest.raises(SpecError):
        validate(as_spec(broken))
