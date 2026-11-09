"""`file:line:` in spec and outline errors: where the line points, and that nothing costs anything when valid."""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
import yaml

from yamlreel import cli, story, yamlpos
from yamlreel.cli import main
from yamlreel.yamlpos import locate
from yamlreel.config import Config, load_config
from yamlreel.specs import SpecError, load_spec, validate


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def write(tmp_path: Path, body: str, name: str = "spec.yaml") -> Path:
    p = tmp_path / name
    p.write_bytes(body.encode("utf-8"))
    return p


def failure(path: Path, config: Config | None = None) -> SpecError:
    with pytest.raises(SpecError) as exc:
        load_spec(path, config)
    return exc.value


def cli_lines(capsys, *argv, code: int = 1) -> list[str]:
    assert main(list(argv)) == code
    return capsys.readouterr().err.splitlines()


BLOCK = """\
scenes:
  - type: title
    title: One
  - type: bullets
    items: [a]
    secnds: 3
"""


def test_library_messages_unchanged(tmp_path):
    f = write(tmp_path, BLOCK)
    with pytest.raises(SpecError) as plain:
        validate(yaml.safe_load(BLOCK), Config())
    assert str(failure(f)) == str(plain.value)


def test_block_unknown_field_line(tmp_path, capsys):
    f = write(tmp_path, BLOCK)
    e = failure(f)
    assert (e.line, e.column) == (6, 5)
    lines = cli_lines(capsys, "validate", str(f))
    assert len(lines) == 1 and lines[0].startswith(f"error: {f}:6: scenes[1].secnds: unknown field")


def test_flow_mapping_line_and_column(tmp_path):
    f = write(tmp_path, "scenes:\n  - {type: title,\n     title: x}\n  - {type: stat,\n     value: 42%,\n      seconds: abc}\n")
    e = failure(f)
    assert str(e).startswith("scenes[1].seconds must be a number")
    assert (e.line, e.column) == (6, 7)


def test_merged_key_reports_anchor_definition(tmp_path):
    f = write(tmp_path, "scenes:\n  - &look {type: title, title: Hi, eyebrow: INTRO}\n  - <<: *look\n    type: bullets\n    items: [a]\n")
    e = failure(f)
    assert str(e).startswith("scenes[1].eyebrow: unknown field for a bullets scene")
    assert e.line == 2
    # an explicit key beats a merged one
    g = write(tmp_path, "scenes:\n  - &a {type: title, title: x, seconds: bad}\n  - <<: *a\n    seconds: worse\n", "g.yaml")
    e = failure(g)
    assert str(e).startswith("scenes[0].seconds") and e.line == 2   # scene 0 is reported first
    g2 = write(tmp_path, "scenes:\n  - &a {type: title, title: x, seconds: 3}\n  - <<: *a\n    seconds: worse\n", "g2.yaml")
    e = failure(g2)
    assert str(e).startswith("scenes[1].seconds") and e.line == 4   # the explicit key, not the merged one
    # in <<: [*a, *b] the first alias wins, as in safe_load
    both = "a: &a {k: 1}\nb: &b {k: 2}\nm:\n  <<: [*a, *b]\n"
    assert yaml.safe_load(both)["m"]["k"] == 1
    assert locate(both, "m.k") == (1, 8)


def test_alias_value_reports_using_key(tmp_path):
    f = write(tmp_path, "scenes:\n  - &t {type: title, title: x}\n  - type: &u nope\n    title: x\n  - type: *u\n    title: x\n")
    e = failure(f)
    assert str(e).startswith("scenes[1].type") and e.line == 3
    f = write(tmp_path, "scenes:\n  - type: &u nope\n    title: x\n  - type: *u\n    title: y\n", "alias2.yaml")
    assert failure(f).line == 2


def test_missing_required_field_points_at_scene(tmp_path):
    f = write(tmp_path, "scenes:\n  - type: title\n    title: x\n  - type: bullets\n    seconds: 3\n")
    e = failure(f)
    assert str(e).startswith("scenes[1] (bullets): missing items") and e.line == 4


def test_config_only_value_has_no_line(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("fps: 999\n")
    f = write(tmp_path, "scenes:\n  - type: title\n    title: x\n")
    lines = cli_lines(capsys, "validate", "--config", str(cfg), str(f))
    assert len(lines) == 1 and lines[0].startswith("error: fps ") and str(f) not in lines[0]


def test_meta_tags_index_not_trusted(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("meta: {tags: [from-config, also-config]}\n")
    f = write(tmp_path, "meta:\n  title: x\n  tags: [a, [nested]]\nscenes:\n  - type: title\n    title: x\n")
    e = failure(f, load_config(cfg_file))
    assert str(e).startswith("meta.tags[3]") and e.line == 3   # index 3 of the merged list is index 1 in the file


SCENES = "scenes:\n  - type: title\n    title: x\n"


def test_nested_config_value_has_no_line_when_spec_sets_parent(tmp_path):
    cfg = tmp_path / "c2.yaml"
    cfg.write_text("voiceover: {speed: 99}\n")
    f = write(tmp_path, "voiceover:\n  voice: x\n" + SCENES)
    e = failure(f, load_config(cfg))
    assert str(e).startswith("voiceover.speed must be a number") and e.line is None


def test_meta_tags_from_config_have_no_line(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("meta: {tags: [ok, [bad]]}\n")
    f = write(tmp_path, "meta: {title: Hello}\n" + SCENES)
    e = failure(f, load_config(cfg))
    assert str(e).startswith("meta.tags[1] must be text") and e.line is None


def test_theme_value_from_config_has_no_line(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("themes:\n  mine: {bg: 12345678901}\n")
    f = write(tmp_path, "themes:\n  other: {bg: '#000000'}\n" + SCENES)
    e = failure(f, load_config(cfg))
    assert str(e).startswith("themes.mine") and e.line is None


def test_bad_value_in_spec_under_same_parent_keeps_exact_line(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("voiceover: {speed: 1.0}\n")
    f = write(tmp_path, "voiceover:\n  voice: x\n  speed: 99\n" + SCENES)
    e = failure(f, load_config(cfg))
    assert str(e).startswith("voiceover.speed") and e.line == 3


def test_crlf_and_bom_lines(tmp_path):
    lf = write(tmp_path, BLOCK, "lf.yaml")
    crlf = write(tmp_path, BLOCK.replace("\n", "\r\n"), "crlf.yaml")
    bom = write(tmp_path, "﻿" + BLOCK.replace("\n", "\r\n"), "bom.yaml")
    assert {failure(p).line for p in (lf, crlf, bom)} == {6}
    assert {failure(p).column for p in (lf, crlf, bom)} == {5}


def test_empty_and_pathless_messages_have_no_line(tmp_path):
    for body in ("", "- a\n- b\n", "# nothing\n"):
        e = failure(write(tmp_path, body))
        assert e.line is None and e.column is None
    big = write(tmp_path, "limits: {max_seconds: 30}\nscenes:\n" + "".join("  - {type: title, title: x, seconds: 20}\n" for _ in range(3)), "big.yaml")
    e = failure(big)
    assert str(e).startswith("total length") and e.line is None


def test_valid_spec_never_composes(tmp_path, monkeypatch):
    f = write(tmp_path, "scenes:\n  - type: title\n    title: x\n")
    monkeypatch.setattr(yamlpos, "locate", lambda *a, **k: pytest.fail("locate ran"))
    parses = []
    real = yaml.SafeLoader.get_single_node
    monkeypatch.setattr(yaml.SafeLoader, "get_single_node", lambda self: parses.append(1) or real(self))
    assert load_spec(f)["scenes"][0]["title"] == "x"
    assert len(parses) == 1   # safe_load's own parse; nothing is parsed a second time
    e = failure(write(tmp_path, BLOCK, "bad.yaml"))
    assert str(e).startswith("scenes[1].secnds")   # reading the message does not locate either
    monkeypatch.undo()


def test_alias_bomb_locates_fast(tmp_path):
    levels = ["&a0 [" + ", ".join(["x"] * 9) + "]"] + [f"&a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 9)]
    f = write(tmp_path, "poster: {at: [" + ", ".join(levels) + "]}\nscenes:\n  - {type: *a8, title: x}\n")
    start = time.monotonic()
    e = failure(f)
    assert e.line == 3
    assert time.monotonic() - start < 1.0


def test_locator_failure_falls_back(tmp_path, capsys, monkeypatch):
    f = write(tmp_path, BLOCK)

    def boom(*a, **k):
        raise RuntimeError("locator broke")
    monkeypatch.setattr(yamlpos, "locate", boom)
    lines = cli_lines(capsys, "validate", str(f))
    assert len(lines) == 1 and lines[0].startswith("error: scenes[1].secnds: unknown field")


def test_story_outline_line(tmp_path, capsys):
    f = write(tmp_path, "topic: t\nhook: h\nsteps:\n  - name: A\n    say: s\n    show: diagram\n  - name: B\n    say: s\n"
              "    show: diagram\n    events:\n      - at: soon\n        text: x\n", "outline.yaml")
    with pytest.raises(story.StoryError) as exc:
        story.load_outline(f)
    assert str(exc.value).startswith("steps[1].events[0].at") and exc.value.line == 11
    lines = cli_lines(capsys, "story", str(f))
    assert len(lines) == 1 and lines[0].startswith(f"error: {f}:11: steps[1].events[0].at")


def test_post_load_errors_print_plain(tmp_path, capsys):
    f = write(tmp_path, "scenes:\n  - type: title\n    title: x\n")
    lines = cli_lines(capsys, "render", str(f), "--fps", "99")
    assert lines == ["error: --fps must be an integer between 1 and 60"]
    assert cli._error_line(ValueError("x")) == "error: x"


def test_check_voice_prints_the_location_and_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.voicecheck, "load_recogniser", lambda model: object())
    f = write(tmp_path, BLOCK)
    lines = cli_lines(capsys, "check-voice", str(f), code=2)
    assert len(lines) == 1 and lines[0].startswith(f"error: {f}:6: scenes[1].secnds")
