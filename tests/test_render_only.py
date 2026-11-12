"""`yamlreel render --only`: render some scenes, cut out of the full video."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml
from fakes import _fake_voice_server, _tone_wav_bytes

from yamlreel import render as render_mod
from yamlreel import voice as voice_mod
from yamlreel.cli import main, parse_only
from yamlreel.config import Config
from yamlreel.render import Runtime, Timeline, ffmpeg_exe, only_label, plan_durations, render, scene_windows
from yamlreel.specs import SpecError, validate

needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def no_personal_config(tmp_path_factory, monkeypatch):
    """Never read the developer's own config or YAMLREEL_* overrides."""
    empty = tmp_path_factory.mktemp("no-config")
    for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(empty / "config.yaml"))
    monkeypatch.chdir(empty)


# --- helpers ----------------------------------------------------------------------------------------------

class _Sink:
    def __init__(self):
        self.frames: list[bytes] = []
        self.count = 0

    def write(self, data: bytes) -> None:
        self.count += 1
        if len(self.frames) < 4000:
            self.frames.append(data)

    def close(self) -> None:
        pass


class FakePopen:
    """Records the ffmpeg argv and every frame written to it; encodes nothing."""
    runs: list["FakePopen"] = []

    def __init__(self, cmd, stdin=None, stderr=None, **kw):
        self.argv = [str(c) for c in cmd]
        self.stdin = _Sink()
        FakePopen.runs.append(self)

    def wait(self) -> int:
        return 0


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    FakePopen.runs = []
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(render_mod.subprocess, "Popen", FakePopen)
    return FakePopen


def titles(*seconds, **extra):
    scenes = [{"type": "title", "title": f"T{n}", "seconds": s} for n, s in enumerate(seconds)]
    return validate({"scenes": scenes, "music": {"style": "none", "snap": False}, **extra}, Config())


def run_time(video: Path, fps: int = 10) -> float:
    """The video stream's length (frames / fps), from ffmpeg itself (ffprobe may be absent)."""
    # decode (no `-c copy`): the stats line of a stream copy has no `frame=` on every ffmpeg version
    done = subprocess.run([ffmpeg_exe(), "-i", str(video), "-map", "0:v", "-f", "null", "-"],
                          capture_output=True, text=True)
    return int(re.findall(r"frame=\s*(\d+)", done.stderr)[-1]) / fps


def audio_8k(video: Path) -> np.ndarray:
    out = subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0


def rt_of(tmp_path, url=None) -> Runtime:
    return Runtime(roots=[tmp_path], voice=voice_mod.VoiceServer(url=url) if url else None)


# --- 1, 2: parse_only -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [("3", [3]), ("2,4", [2, 4]), ("2-4", [2, 3, 4]), ("4,2", [2, 4]),
                                       ("2-4,3", [2, 3, 4]), (" 1 , 3", [1, 3]), ("1-2,2-3", [1, 2, 3])])
def test_parse_only_normalises(text, want):
    assert parse_only(text, 8) == want


SHAPE = "use 3, 2,4 or 2-4 (scenes are numbered 0 to 7)"


@pytest.mark.parametrize("text,msg", [
    ("x", f"--only: 'x' is not a scene number or range; {SHAPE}"),
    ("", f"--only: '' is not a scene number or range; {SHAPE}"),
    ("2,,3", f"--only: '' is not a scene number or range; {SHAPE}"),
    ("-1", f"--only: '-1' is not a scene number or range; {SHAPE}"),
    ("2.5", f"--only: '2.5' is not a scene number or range; {SHAPE}"),
    ("\u0663", f"--only: '\u0663' is not a scene number or range; {SHAPE}"),   # an Arabic-Indic 3 is not scene 3
    ("9", "--only: scene 9 does not exist; scenes are numbered 0 to 7"),
    ("6-9", "--only: scene 9 does not exist; scenes are numbered 0 to 7"),
    ("4-2", "--only: 4-2 is backwards; write 2-4"),
])
def test_parse_only_errors(text, msg):
    with pytest.raises(SpecError) as e:
        parse_only(text, 8)
    assert str(e.value) == msg


def test_windows_and_labels():
    assert scene_windows([1, 2, 3, 5]) == [(1, 3), (5, 5)]
    assert only_label([(2, 4)]) == "2-4" and only_label([(1, 1), (5, 5)]) == "1_5" and only_label([(0, 1), (3, 3)]) == "0-1_3"


def test_render_checks_only_again(tmp_path):
    for bad in ([9], [2, 1], [1, 1], []):
        with pytest.raises(render_mod.RenderError, match="only:"):
            render(titles(1, 1, 1), "square", tmp_path / "o", fps=5, scale=0.1, contact=False, only=bad)


# --- 3-5: the video and audio of a slice -------------------------------------------------------------------

@needs_ffmpeg
def test_only_duration_is_sum_of_chosen_scenes(tmp_path):
    srv, _, url = _fake_voice_server(seconds=2.0)
    spec = validate({"scenes": [{"type": "title", "title": "A", "seconds": 1},
                                {"type": "title", "title": "B", "seconds": 1, "narration": "A narrated scene."},
                                {"type": "title", "title": "C", "seconds": 1.5},
                                {"type": "title", "title": "D", "seconds": 1}],
                     "music": {"style": "none", "snap": False}}, Config())
    try:
        res = render(spec, "square", tmp_path / "o", fps=10, scale=0.1, contact=False, runtime=rt_of(tmp_path, url), only=[1, 2])
    finally:
        srv.shutdown()
    (first, last, s0, s1), = res["windows"]
    assert (first, last) == (1, 2) and s1 - s0 > 2.5   # scene 1 was stretched past its 1 s to fit the speech
    assert res["video"].name == "video-scenes-1-2.mp4"
    assert abs(run_time(res["video"]) - (s1 - s0)) <= 1 / 10 + 0.05
    assert res["full_seconds"] == pytest.approx(s1 - s0 + 2.0)   # scenes 0 and 3 are 1 s each


def test_only_last_window_has_the_full_videos_frame_count(tmp_path, fake_ffmpeg):
    """--only 0-N must not draw a frame the full video does not have: ceil(total*fps) can be one more than round()."""
    spec = titles(1.37, 1.13, 1.21)   # 3.71 s at 30 fps: round() gives 111 frames, ceil() would give 112
    render(spec, "square", tmp_path / "full", fps=30, scale=0.1, contact=False)
    render(spec, "square", tmp_path / "all", fps=30, scale=0.1, contact=False, only=[0, 1, 2])
    full, whole = fake_ffmpeg.runs
    assert whole.stdin.count == full.stdin.count == 111


@needs_ffmpeg
@pytest.mark.parametrize("music", ["koto", "file"])
def test_only_audio_matches_video_length(tmp_path, music):
    style = music
    if music == "file":   # a music file much shorter than the window
        (tmp_path / "short.wav").write_bytes(_tone_wav_bytes(0.7))
        style = "short.wav"
    srv, _, url = _fake_voice_server(seconds=1.0)
    spec = validate({"scenes": [{"type": "title", "title": f"T{n}", "seconds": 2, "narration": f"Line {n}"} for n in range(3)],
                     "music": {"style": style, "snap": False}}, Config(), base_dir=tmp_path)
    try:
        res = render(spec, "square", tmp_path / "o", fps=10, scale=0.1, contact=False, runtime=rt_of(tmp_path, url), only=[0, 2])
    finally:
        srv.shutdown()
    video = run_time(res["video"])
    assert abs(len(audio_8k(res["video"])) / 8000 - video) <= 0.05


@needs_ffmpeg
def test_only_audio_without_music_is_cut_too(tmp_path):
    srv, _, url = _fake_voice_server(seconds=1.0)
    spec = titles(2, 2, 2)
    spec["scenes"][1]["narration"] = "Only the middle."
    try:
        res = render(spec, "square", tmp_path / "o", fps=10, scale=0.1, contact=False, runtime=rt_of(tmp_path, url), only=[1])
    finally:
        srv.shutdown()
    audio = audio_8k(res["video"])
    assert abs(len(audio) / 8000 - run_time(res["video"])) <= 0.05
    assert float(np.abs(audio[int(0.3 * 8000):int(1.0 * 8000)]).max()) > 0.05   # the speech is in the slice, after the lead


@needs_ffmpeg
def test_only_music_is_the_full_slice(tmp_path):
    spec = titles(2, 2, 2, 2)
    spec["music"] = {**spec["music"], "style": "koto", "seed": 5}
    full = render(spec, "square", tmp_path / "o", fps=10, scale=0.1, contact=False)
    part = render(spec, "square", tmp_path / "o", fps=10, scale=0.1, contact=False, only=[2])
    a, b = audio_8k(full["video"]), audio_8k(part["video"])
    ref = a[int(4.0 * 8000):int(4.0 * 8000) + len(b)]
    n = min(len(ref), len(b))
    assert n > 1.8 * 8000 and np.corrcoef(ref[:n], b[:n])[0, 1] > 0.95


# --- 6: windows ------------------------------------------------------------------------------------------

def test_only_non_contiguous_skips_the_gap(tmp_path, fake_ffmpeg):
    spec = titles(1, 1, 1.5, 1)   # transitions are the default fade
    res = render(spec, "square", tmp_path / "o", fps=10, scale=0.1, contact=False, only=[0, 2])
    run, = fake_ffmpeg.runs
    assert res["seconds"] == pytest.approx(1 + 1.5) and run.stdin.count == 25
    assert run.argv[run.argv.index("-t") + 1] == "2.500"
    tl = Timeline(spec, "square", tmp_path / "w", 0.1, durations=[1, 1, 1.5, 1])
    plain = tl._scene_frame(2, 0.0).convert("RGB").tobytes()
    blended = tl.frame_at(tl.starts[2]).convert("RGB").tobytes()   # without cut_in: scene 1's last frame fading into 2
    assert blended != plain
    assert run.stdin.frames[10] == plain   # window 2 begins at frame 10 of the slice: a hard cut, no scene 1 in it
    assert run.stdin.frames[10] != blended


@needs_ffmpeg
def test_only_non_contiguous_video_length(tmp_path):
    res = render(titles(1, 1, 1.5, 1), "square", tmp_path / "o", fps=10, scale=0.1, contact=False, only=[0, 2])
    assert abs(run_time(res["video"]) - 2.5) <= 0.15


# --- 7: names ---------------------------------------------------------------------------------------------

def write_story(tmp_path) -> Path:
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(1.2))
    raw = {"meta": {"title": "Names", "tags": ["x"]}, "theme": "midnight", "formats": ["landscape"], "fps": 10,
           "music": {"style": "koto", "seed": 7}, "subtitles": {"words": 3, "srt": True},
           "scenes": [{"type": "title", "title": "Zero", "seconds": 2},
                      {"type": "title", "title": "One", "seconds": 2, "narration_audio": "tone.wav",
                       "narration": "The middle scene speaks here"},
                      {"type": "title", "title": "Two", "seconds": 2}]}
    p = tmp_path / "story.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def digests(out: Path) -> dict[str, str]:
    return {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out.rglob("*")) if p.is_file() and ".cache" not in p.parts}


@needs_ffmpeg
def test_only_names_do_not_collide(tmp_path, capsys):
    spec = write_story(tmp_path)
    out = tmp_path / "out"
    assert main(["render", str(spec), "--out", str(out), "--preview"]) == 0
    before = digests(out)
    assert {"landscape/video.mp4", "landscape/poster.jpg", "landscape/contact.png", "landscape/subtitles.srt",
            "share-copy.txt"} <= set(before)
    assert main(["render", str(spec), "--out", str(out), "--preview", "--only", "1"]) == 0
    after = digests(out)
    assert {k: v for k, v in after.items() if k in before} == before   # every full output is byte-unchanged
    assert set(after) - set(before) == {"landscape/video-scenes-1.mp4", "landscape/contact-scenes-1.png",
                                        "landscape/subtitles-scenes-1.srt"}   # and no poster, no share copy
    cues = (out / "landscape" / "subtitles-scenes-1.srt").read_text(encoding="utf-8")
    assert re.match(r"1\n00:00:00,\d{3} --> ", cues)   # shifted to slice time: scene 1 starts at 2 s in the full video


@needs_ffmpeg
def test_only_without_subtitles_leaves_the_full_srt_alone(tmp_path):
    spec = write_story(tmp_path)
    out = tmp_path / "out"
    assert main(["render", str(spec), "--out", str(out), "--preview"]) == 0
    full_srt = (out / "landscape" / "subtitles.srt").read_bytes()
    assert main(["render", str(spec), "--out", str(out), "--preview", "--only", "2"]) == 0
    assert (out / "landscape" / "subtitles.srt").read_bytes() == full_srt
    assert not (out / "landscape" / "subtitles-scenes-2.srt").exists()


# --- 8: no --only means no change ---------------------------------------------------------------------------

# The ffmpeg command lines `render` built on eda9f02 (before --only) for these two specs; tmp paths are normalised.
AUDIO_ARGS = ["-protocol_whitelist", "file", "-format_whitelist", "wav,mp3,ogg,flac,mov,aac,matroska,webm"]
GOLDEN_MUSIC_ONLY = [
    "FFMPEG", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "960x540", "-r", "12", "-i", "-",
    *AUDIO_ARGS, "-i", "TMP/music.wav",
    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
    "-af", "atrim=0:4.500,asetpts=PTS-STARTPTS,volume=0.9,afade=t=in:st=0:d=0.8,afade=t=out:st=2.500:d=2.0",
    "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", "-t", "4.500", "OUT/landscape/video.mp4"]
GOLDEN_MUSIC_AND_VOICE = [
    "FFMPEG", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "540x960", "-r", "12", "-i", "-",
    *AUDIO_ARGS, "-i", "TMP/music.wav", *AUDIO_ARGS, "-i", "TMP/narration.wav",
    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
    "-filter_complex", "[1:a]atrim=0:4.000,asetpts=PTS-STARTPTS,volume=0.9,afade=t=in:st=0:d=0.8,afade=t=out:st=2.000:d=2.0[m];"
                       "[2:a]atrim=0:4.000,asetpts=PTS-STARTPTS[n];[m][n]amix=inputs=2:duration=longest:normalize=0[a]",
    "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", "-t", "4.000",
    "OUT/vertical/video.mp4"]


def normalised(argv: list[str], out: Path) -> list[str]:
    return ["FFMPEG" if n == 0 else re.sub(r"\S*/yamlreel-[A-Za-z0-9_]+/", "TMP/", a.replace(str(out), "OUT"))
            for n, a in enumerate(argv)]


SPEC_MUSIC_ONLY = {
    "meta": {"title": "Spec A"}, "theme": "midnight", "formats": ["landscape"], "fps": 12,
    "music": {"style": "koto", "seed": 7}, "watermark": {"text": "yr"},
    "scenes": [{"type": "title", "eyebrow": "One", "title": "Hello", "seconds": 1.5},
               {"type": "stat", "value": "60 seconds", "label": "a ttl", "seconds": 1.5, "caption": "Short",
                "transition": {"type": "slide", "seconds": 0.4}},
               {"type": "title", "eyebrow": "Three", "title": "Bye", "seconds": 1.5, "transition": {"type": "cut"}}]}
SPEC_MUSIC_AND_VOICE = {
    "meta": {"title": "Spec B"}, "theme": "midnight", "formats": ["vertical"], "fps": 12,
    "music": {"style": "ambient", "seed": 3}, "subtitles": {"style": "karaoke", "words": 3, "srt": True},
    "voiceover": {"duck": True},
    "scenes": [{"type": "title", "eyebrow": "One", "title": "Hello there", "seconds": 2, "narration_audio": "tone.wav",
                "narration": "Hello there my friend and welcome"},
               {"type": "stat", "value": "10 seconds", "label": "a ttl", "seconds": 2, "step": "Setup",
                "transition": {"type": "zoom", "seconds": 0.4}}]}


@pytest.mark.parametrize("raw,fmt,golden", [(SPEC_MUSIC_ONLY, "landscape", GOLDEN_MUSIC_ONLY),
                                            (SPEC_MUSIC_AND_VOICE, "vertical", GOLDEN_MUSIC_AND_VOICE)])
def test_render_without_only_is_unchanged(tmp_path, fake_ffmpeg, raw, fmt, golden):
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(1.2))
    spec = {**validate(raw, Config(), base_dir=tmp_path), "_dir": str(tmp_path)}
    out = tmp_path / "out"
    res = render(spec, fmt, out, fps=12, scale=0.5, contact=True, runtime=rt_of(tmp_path))
    run, = fake_ffmpeg.runs
    assert normalised(run.argv, out) == golden
    total = res["seconds"]
    assert res["full_seconds"] == total and run.stdin.count == round(total * 12)
    tl = Timeline(spec, fmt, tmp_path / "w", 0.5, durations=plan_durations(spec, _floors(spec, tmp_path)))
    assert run.stdin.frames[0] == tl.frame_at(tl.poster_time()).convert("RGB").tobytes()   # frame 0 is the poster
    assert (out / fmt / "poster.jpg").exists() and (out / fmt / "contact.png").exists() and res["poster"] is not None
    assert not list((out / fmt).glob("*-scenes-*"))


def _floors(spec, tmp_path):
    """The scene floors a narrated spec gets (the same calls render() makes), for the expected timeline."""
    from yamlreel import subtitles as subs_mod
    clips = voice_mod.prepare_narration(spec, None, tmp_path / "c", lambda p: tmp_path / p)
    words, _ = subs_mod.scene_words(spec, clips, lambda p: tmp_path / p)
    return subs_mod.raise_floors(spec, words, voice_mod.scene_floors(spec, clips))


# --- 9: frame budget -------------------------------------------------------------------------------------

def test_only_frame_budget_counts_only_the_slice(tmp_path, fake_ffmpeg, capsys):
    raw = {"theme": "midnight", "formats": ["landscape"], "fps": 60, "music": {"style": "none"}, "limits": {"max_seconds": 3600},
           "scenes": [{"type": "title", "title": "Short", "seconds": 1},
                      *[{"type": "title", "title": f"Long {n}", "seconds": 590} for n in range(6)]]}
    spec = tmp_path / "long.yaml"
    spec.write_text(yaml.safe_dump(raw), encoding="utf-8")
    out = str(tmp_path / "out")
    base = ["render", str(spec), "--out", out, "--format", "all", "--no-voice"]
    assert main(base) == 1   # 3541 s x 60 fps x 3 formats is over MAX_FRAMES
    assert "the limit is 216000 (MAX_FRAMES)" in capsys.readouterr().err
    assert main([*base, "--only", "0"]) == 0
    assert len(fake_ffmpeg.runs) == 3 and all(r.stdin.count == 60 for r in fake_ffmpeg.runs)


# --- 10: contact sheet -----------------------------------------------------------------------------------

def test_only_contact_sheet_rows(tmp_path, fake_ffmpeg, monkeypatch):
    labels: list[str] = []
    real_draw = render_mod.ImageDraw.Draw

    class Spy:
        def __init__(self, im):
            self._d = real_draw(im)

        def text(self, xy, text, **kw):
            labels.append(text)
            return self._d.text(xy, text, **kw)

    monkeypatch.setattr(render_mod, "ImageDraw", SimpleNamespace(Draw=Spy))
    out = tmp_path / "o"
    res = render(titles(2, 2, 2, 2), "square", out, fps=10, scale=0.1, contact=True, contact_frames=1, only=[1, 2])
    assert [l.split()[0] for l in labels] == ["1:title", "2:title"]
    assert re.fullmatch(r"1:title 3\.8s", labels[0]) and re.fullmatch(r"2:title 5\.8s", labels[1])   # full-video times
    from PIL import Image
    with Image.open(res["contact"]) as sheet:
        tile_h = int(480 * 1.0)
        assert res["contact"].name == "contact-scenes-1-2.png" and sheet.size == (480, 2 * (tile_h + 24))


# --- 11: the note ----------------------------------------------------------------------------------------

def test_only_note_on_stderr(tmp_path, fake_ffmpeg, capsys):
    raw = {"theme": "midnight", "formats": ["square"], "fps": 10, "music": {"style": "none", "snap": False},
           "scenes": [{"type": "title", "title": f"T{n}", "seconds": 2} for n in range(5)]}
    spec = tmp_path / "five.yaml"
    spec.write_text(yaml.safe_dump(raw), encoding="utf-8")
    base = ["render", str(spec), "--out", str(tmp_path / "o"), "--preview", "--no-voice"]
    assert main([*base, "--only", "1-3"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines().count("only: scenes 1-3 of 0-4, 2.00-8.00s of the 10.00s video (6.00s)") == 1
    assert main([*base, "--only", "0,3-4"]) == 0
    assert "only: scenes 0 + 3-4 of 0-4, 0.00-2.00s + 6.00-10.00s of the 10.00s video (6.00s)" in capsys.readouterr().err
    assert not (tmp_path / "o" / "share-copy.txt").exists()
    assert main([*base, "--only", "4-2"]) == 1
    assert "error: --only: 4-2 is backwards; write 2-4" in capsys.readouterr().err
