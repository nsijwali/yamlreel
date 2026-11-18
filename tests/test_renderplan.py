"""Hardening slice 5: the three dependency loops are gone and render planning is a typed RenderPlan that every
format of one run shares (docs/specs/hardening.md)."""
from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

import pytest

from fakes import _tone_wav_bytes
from yamlreel import config, errorfmt, ffmpegtools, polish, renderplan, specs, theme
from yamlreel import music as music_mod
from yamlreel import render as render_mod
from yamlreel import voice as voice_mod
from yamlreel.cli import main
from yamlreel.config import Config
from yamlreel.render import RenderError, Runtime, ffmpeg_exe, make_plan, render
from yamlreel.specs import validate

needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")
PKG = Path(render_mod.__file__).parent


def _imports() -> dict[str, set[str]]:
    """Package-internal imports of every module, late (function-level) imports included."""
    mods = {p.stem: p for p in PKG.glob("*.py")}
    graph: dict[str, set[str]] = {}
    for name, path in mods.items():
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                found |= {node.module.split(".")[0]} if node.module else {a.name for a in node.names}
        graph[name] = found & set(mods)
    return graph


def _reaches(graph: dict[str, set[str]], a: str, b: str) -> bool:
    todo, seen = [a], set()
    while todo:
        for n in graph[todo.pop()]:
            if n == b:
                return True
            if n not in seen:
                seen.add(n)
                todo.append(n)
    return False


@pytest.mark.parametrize("a,b", [("config", "specs"), ("voice", "render"), ("theme", "config"), ("theme", "specs")])
def test_the_reviewed_dependency_loops_are_gone(a, b):
    assert not _reaches(_imports(), a, b), f"{a} still imports {b} (directly or through other modules)"


def test_the_new_modules_stay_dependency_light():
    graph = _imports()
    assert graph["errorfmt"] == set() and graph["ffmpegtools"] == set()
    assert graph["proclife"] <= {"errorfmt"} and not _reaches(graph, "renderplan", "render")


def test_old_import_paths_still_work():
    assert render_mod.RenderError is renderplan.RenderError
    assert render_mod.plan_durations is renderplan.plan_durations
    assert render_mod.scene_windows is renderplan.scene_windows and render_mod.only_label is renderplan.only_label
    assert render_mod.ffmpeg_exe is ffmpegtools.ffmpeg_exe and render_mod.AUDIO_FORMATS == ffmpegtools.AUDIO_FORMATS
    assert config.describe_key is errorfmt.describe_key and config.unknown_key is errorfmt.unknown_key
    assert config.yaml_problem is errorfmt.yaml_problem and theme.unknown_key is errorfmt.unknown_key
    assert voice_mod.VoiceError.__module__ == "yamlreel.voicewire"
    assert polish._ffmpeg() == ffmpegtools.ffmpeg_exe() or ffmpegtools.ffmpeg_exe() is None


def test_voice_finds_ffmpeg_without_the_renderer(monkeypatch):
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: pytest.fail("voice must not ask render for ffmpeg"))
    monkeypatch.setattr(ffmpegtools, "ffmpeg_exe", lambda: None)
    with pytest.raises(voice_mod.VoiceError, match="voiceover.speed needs ffmpeg"):
        voice_mod.change_speed(__import__("numpy").zeros(100, dtype="float32"), 1.1)


# --- the plan ---------------------------------------------------------------------------------------------------

def _spec(tmp_path: Path, **extra) -> dict:
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(0.6))
    raw = {"music": {"style": "lofi", "seed": 3}, "voiceover": {"duck": True},
           "scenes": [{"type": "title", "title": "One", "seconds": 1.0, "narration_audio": "tone.wav", "narration": "hi"},
                      {"type": "bullets", "title": "Two", "items": ["a", "b"], "seconds": 1.2}], **extra}
    return {**validate(raw, Config(), base_dir=tmp_path), "_dir": str(tmp_path)}


def test_a_plan_holds_the_format_independent_decisions(tmp_path):
    spec = _spec(tmp_path)
    with make_plan(spec, 10, tmp_path / "out", Runtime(roots=[tmp_path]), only=[1]) as plan:
        assert plan.durations == render_mod.plan_durations(plan.spec, {0: 0.6 + 0.5})
        assert plan.starts == [0.0, plan.durations[0]] and plan.total == sum(plan.durations)
        assert plan.windows == [(1, 1)] and plan.tag == "-scenes-1" and set(plan.clips) == {0}
        assert renderplan.frame_runs(plan, plan.total) == [(11, 23)]
        assert plan.workdir.is_dir()
        assert spec["scenes"][0]["seconds"] == 1.0   # the caller's spec is never changed
    assert not plan.workdir.exists()


def test_a_plan_made_for_other_settings_is_refused(tmp_path):
    spec = _spec(tmp_path)
    with make_plan(spec, 10, tmp_path / "out", Runtime(roots=[tmp_path])) as plan:
        for kw in ({"fps": 12}, {"only": [1]}, {"music_override": "none"}):
            with pytest.raises(RenderError, match="plan was made for another"):
                render(spec, "square", tmp_path / "out", **{"fps": 10, **kw}, plan=plan)


def _video_hash(path: Path) -> str:
    done = subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-f", "framemd5", "-"],
                          capture_output=True, check=True)
    return hashlib.md5(b"".join(l for l in done.stdout.splitlines(True) if not l.startswith(b"#"))).hexdigest()


@needs_ffmpeg
def test_formats_sharing_a_plan_render_exactly_what_separate_renders_do(tmp_path):
    spec = _spec(tmp_path, mix={})
    rt = Runtime(roots=[tmp_path])
    alone = {f: render(spec, f, tmp_path / "alone", fps=6, scale=0.2, runtime=rt, n_formats=2) for f in ("square", "vertical")}
    with make_plan(spec, 6, tmp_path / "shared", rt, n_formats=2) as plan:
        shared = {f: render(spec, f, tmp_path / "shared", fps=6, scale=0.2, runtime=rt, n_formats=2, plan=plan)
                  for f in ("square", "vertical")}
    for f in alone:
        assert _video_hash(alone[f]["video"]) == _video_hash(shared[f]["video"])
        assert alone[f]["mix"] == shared[f]["mix"] and alone[f]["warnings"] == shared[f]["warnings"]


@needs_ffmpeg
def test_the_cli_processes_narration_and_prepares_audio_once_for_all_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(0.6))
    (tmp_path / "s.yaml").write_text(
        "music: {style: lofi, seed: 3}\nscenes:\n"
        "  - {type: title, title: One, seconds: 1, narration_audio: tone.wav, narration: hi}\n"
        "  - {type: title, title: Two, seconds: 1}\n")
    narrations, musics = [], []
    real_prep, real_music = voice_mod.prepare_narration, music_mod.write_wav_stream
    monkeypatch.setattr(voice_mod, "prepare_narration", lambda *a, **kw: narrations.append(1) or real_prep(*a, **kw))
    monkeypatch.setattr(music_mod, "write_wav_stream", lambda *a, **kw: musics.append(1) or real_music(*a, **kw))
    assert main(["render", str(tmp_path / "s.yaml"), "--out", str(tmp_path / "o"), "--format", "all",
                 "--fps", "4", "--preview"]) == 0
    assert len(narrations) == 1 and len(musics) == 1
    assert all((tmp_path / "o" / f / "video.mp4").exists() for f in ("landscape", "vertical", "square"))


def test_the_unknown_key_hint_hook_still_reaches_config_errors(monkeypatch):
    monkeypatch.setattr(specs, "suggest", lambda key, allowed: "did you mean x?")
    with pytest.raises(ValueError, match=r"\(did you mean x\?\)$"):
        config.clamp_limits({"max_secs": 1})
