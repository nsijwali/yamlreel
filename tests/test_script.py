"""`yamlreel script`: the script sheet for human review. No render, no voice server."""
from __future__ import annotations

import pytest
import yaml

from yamlreel.cli import main

SCENES = [
    {"type": "title", "eyebrow": "THE PROBLEM", "title": "Slow | pages", "seconds": 3, "narration": "Pages are slow. They | break.",
     "caption": "Real data"},
    {"type": "bullets", "title": "Fixes", "items": ["a", "b"], "kicker": "STEP 1", "seconds": 4.5,
     "narration": "Cache it\nand move on."},
    {"type": "stat", "value": "10x", "label": "faster", "seconds": 2},
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def spec_file(tmp_path, scenes=SCENES, name="spec.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump({"scenes": scenes, "music": {"style": "none"}}), encoding="utf-8")
    return p


def test_markdown_sheet_has_one_row_per_scene_and_escapes_pipes(tmp_path, capsys):
    assert main(["script", str(spec_file(tmp_path))]) == 0
    out, err = capsys.readouterr()
    rows = [line for line in out.splitlines() if line.startswith("| ")]
    assert rows[0].startswith("| # | Type | Kicker | Title | Caption | Narration | Seconds |")
    assert len(rows) == 2 + len(SCENES)    # header + separator + scenes
    assert "| 0 | title |  | Slow \\| pages | Real data | Pages are slow. They \\| break. | 3.0 |" in out
    assert "| 1 | bullets | STEP 1 | Fixes |  | Cache it and move on. | 4.5 |" in out
    assert "| 2 | stat |  | 10x |  |  | 2.0 |" in out      # no narration: an empty cell, not an error
    assert "Total: 9.5s" in out
    assert err.strip() == "3 scenes, 2 narrated, ~11 words"


def test_text_sheet_has_a_block_per_scene(tmp_path, capsys):
    assert main(["script", str(spec_file(tmp_path)), "--format", "text"]) == 0
    out, err = capsys.readouterr()
    assert "Scene 0 (title, 3.0s)\n  Kicker: \n  Title: Slow | pages\n  Caption: Real data\n  Narration: Pages are slow. They | break." in out
    assert "Scene 1 (bullets, 4.5s)\n  Kicker: STEP 1\n" in out
    assert "Scene 2 (stat, 2.0s)\n  Kicker: \n  Title: 10x\n  Caption: \n  Narration: \n" in out
    assert out.rstrip().endswith("Total: 9.5s")
    assert "|" not in out.split("Scene 1")[0].replace("Slow | pages", "").replace("They | break", "")


def test_out_writes_a_file_and_refuses_to_overwrite_without_force(tmp_path, capsys):
    spec = spec_file(tmp_path)
    dest = tmp_path / "sheet" / "script.md"
    assert main(["script", str(spec), "--out", str(dest)]) == 0
    first = dest.read_text(encoding="utf-8")
    assert first.startswith("| # |") and capsys.readouterr().out.strip() == f"wrote {dest}"
    dest.write_text("mine", encoding="utf-8")
    assert main(["script", str(spec), "--out", str(dest)]) == 2
    assert "pass --force" in capsys.readouterr().err and dest.read_text(encoding="utf-8") == "mine"
    assert main(["script", str(spec), "--out", str(dest), "--force"]) == 0
    assert dest.read_text(encoding="utf-8") == first


def test_an_invalid_spec_fails_like_validate_and_writes_nothing(tmp_path, capsys):
    bad = spec_file(tmp_path, [{"type": "title", "title": "x", "seconds": -1}], "bad.yaml")
    dest = tmp_path / "sheet.md"
    code = main(["script", str(bad), "--out", str(dest)])
    script_err = capsys.readouterr()
    assert code == main(["validate", str(bad)]) == 1
    validate_err = capsys.readouterr()
    assert script_err.err == validate_err.err and script_err.out == ""
    assert not dest.exists()
    assert main(["script", str(tmp_path / "missing.yaml")]) == main(["validate", str(tmp_path / "missing.yaml")]) == 1


def test_read_screen_shows_the_spoken_screen_text(tmp_path, capsys):
    assert main(["script", str(spec_file(tmp_path)), "--read-screen"]) == 0
    out, err = capsys.readouterr()
    assert "| 2 | stat |  | 10x |  | 10x. faster. | 2.0 |" in out       # the stat is read aloud now
    assert err.strip() == "3 scenes, 3 narrated, ~13 words"
