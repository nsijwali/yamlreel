"""Hardening slice 4: whole-job memory follows the working set, not the number of scenes (docs/specs/hardening.md)."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from fakes import _tone_wav_bytes
from yamlreel import contactpages
from yamlreel import render as render_mod
from yamlreel import voice as voice_mod
from yamlreel.config import Config
from yamlreel.mediabudget import MediaBudget, MediaBudgetError
from yamlreel.render import RenderError, Runtime, Timeline, contact_sheet, ffmpeg_exe, render
from yamlreel.shots import CaptureError
from yamlreel.specs import validate
from yamlreel.voice import VoiceError

needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="needs ffmpeg")


# --- the budget ---------------------------------------------------------------------------------------------------

class Owner:
    def __init__(self, budget: MediaBudget, name: str):
        self.budget, self.name, self.released = budget, name, 0

    def load(self, n: int) -> None:
        self.budget.charge(self, n, self.release)

    def release(self) -> None:
        self.released += 1


def test_the_least_recently_used_picture_is_released_to_make_room():
    b = MediaBudget(limit=100)
    a, c, d = Owner(b, "a"), Owner(b, "c"), Owner(b, "d")
    a.load(40)
    c.load(40)
    b.touch(a)              # a was drawn again: c is now the oldest
    d.load(40)
    assert (a.released, c.released, d.released) == (0, 1, 0)
    assert b.used == 80 and b.peak == 80 and b.holds(a) and not b.holds(c)


def test_charging_again_replaces_and_dropping_gives_back():
    b = MediaBudget(limit=100)
    a = Owner(b, "a")
    a.load(30)
    a.load(50)
    assert b.used == 50
    b.drop(a)
    b.drop(a)
    assert b.used == 0 and b.peak == 50


def test_one_picture_larger_than_the_budget_is_refused():
    b = MediaBudget(limit=100)
    with pytest.raises(MediaBudgetError, match="MAX_DECODED_MEDIA_BYTES"):
        Owner(b, "a").load(101)


# --- contact-sheet pages ----------------------------------------------------------------------------------------

def test_rows_per_page_and_page_names(tmp_path):
    assert contactpages.rows_per_page(1000, 100, 250_000) == 2
    assert contactpages.rows_per_page(10_000, 10_000, 10) == 1
    first = tmp_path / "contact.png"
    assert contactpages.page_path(first, 0) == first
    assert contactpages.page_path(first, 2) == tmp_path / "contact-p3.png"
    for name in ("contact-p2.png", "contact-p10.png", "contact-p3.png", "contact-scenes-1-p2.png", "contact-p1.png"):
        (tmp_path / name).touch()
    assert [p.name for p in contactpages.extra_pages(tmp_path, "contact.png")] == \
        ["contact-p2.png", "contact-p3.png", "contact-p10.png"]


# --- shot scenes decode on demand -------------------------------------------------------------------------------

def _shots(tmp_path: Path, n: int, size=(400, 300), **extra) -> dict:
    for k in range(n):
        Image.new("RGB", size, (k * 30 % 255, 90, 160)).save(tmp_path / f"s{k}.png")
    raw = {"scenes": [{"type": "shot", "source": f"s{k}.png", "seconds": 1.0, "zoom": [1.0, 1.2]} for k in range(n)],
           "music": {"style": "none"}, **extra}
    return {**validate(raw, Config(), base_dir=tmp_path), "_dir": str(tmp_path)}


def _timeline(tmp_path: Path, spec: dict) -> Timeline:
    return Timeline(spec, "landscape", tmp_path / "w", 0.25, Runtime(roots=[tmp_path]))


def test_building_a_timeline_decodes_no_shot_and_drawing_holds_at_most_two(tmp_path):
    tl = _timeline(tmp_path, _shots(tmp_path, 6))
    media = tl.ctx.media
    assert media.used == 0 and not any(sc._shot.loaded for sc in tl.scenes)
    for k in range(int(tl.total * 10)):
        tl.frame_at(k / 10)
        assert sum(sc._shot.loaded for sc in tl.scenes) <= 2      # the scene drawn and the one before it
    one = tl.scenes[0]._shot.nbytes
    assert media.peak <= 2 * one


def test_frames_are_identical_whether_a_shot_was_kept_or_decoded_again(tmp_path):
    tl = _timeline(tmp_path, _shots(tmp_path, 3, transition={"type": "fade", "seconds": 0.3}))
    times = [0.0, 0.5, 1.1, 2.2, 0.4, 2.9, 1.05, 0.2]   # random access: scenes are released and decoded again
    first = [tl.frame_at(t).tobytes() for t in times]
    for sc in tl.scenes:
        sc.release()
    again = [tl.frame_at(t).tobytes() for t in reversed(times)]
    assert first == list(reversed(again))


def test_a_full_budget_releases_the_least_recently_drawn_shot(tmp_path):
    tl = _timeline(tmp_path, _shots(tmp_path, 3))
    one = tl.scenes[0]._shot.nbytes
    tl.ctx.media.limit = one   # room for one decoded picture
    tl.scenes[0].frame(0.5)
    tl.scenes[2].frame(0.5)
    assert [sc._shot.loaded for sc in tl.scenes] == [False, False, True]
    assert tl.scenes[0].frame(0.5).size == (tl.ctx.W, tl.ctx.H)   # decoded again on demand


def test_capture_problems_are_still_reported_with_the_scene_before_rendering(tmp_path):
    spec = _shots(tmp_path, 2)
    spec["scenes"][1]["source"] = "missing.png"
    with pytest.raises(CaptureError, match=r"scenes\[1\]\.source: image not found"):
        _timeline(tmp_path, spec)
    (tmp_path / "bad.png").write_bytes(b"not an image")
    spec["scenes"][1]["source"] = "bad.png"
    with pytest.raises(CaptureError, match=r"scenes\[1\]\.source: cannot read image bad.png"):
        _timeline(tmp_path, spec)


def _motion_ctx(tmp_path: Path):
    from yamlreel.scenebase import Ctx
    from yamlreel.theme import get_theme
    return Ctx(W=270, H=480, theme=get_theme("midnight"), workdir=tmp_path, base_dir=tmp_path, roots=[tmp_path])


def _motion_spec(source: str = "pic.png") -> dict:
    return validate({"scenes": [{"type": "motion", "seconds": 6, "elements": [
        {"kind": "image", "source": source, "at": 0, "enter": "none", "w": 800}]}]})["scenes"][0]


def test_motion_images_are_decoded_only_while_drawn_and_charged_to_the_budget(tmp_path):
    from yamlreel.scenes import build_scene
    Image.new("RGBA", (200, 100), (200, 40, 40, 255)).save(tmp_path / "pic.png")
    ctx = _motion_ctx(tmp_path)
    scenes = [build_scene(_motion_spec(), ctx) for _ in range(3)]
    assert ctx.media.used == 0 and all(sc.retained_bytes() == 0 for sc in scenes)   # building decodes nothing
    one = scenes[0]._image_bytes
    ctx.media.limit = one                       # room for one decoded image
    frames = [sc.frame(1.0).tobytes() for sc in scenes]
    assert ctx.media.used == one and [sc._images[0].loaded for sc in scenes] == [False, False, True]
    assert scenes[0].frame(1.0).tobytes() == frames[0]   # decoded again, same pixels


def test_many_motion_image_scenes_no_longer_hit_a_whole_render_limit(tmp_path):
    from yamlreel.scenes import build_scene
    Image.new("RGBA", (200, 100), (200, 40, 40, 255)).save(tmp_path / "pic.png")
    ctx = _motion_ctx(tmp_path)
    one = build_scene(_motion_spec(), ctx)._image_bytes
    ctx.media.limit = 3 * one   # far fewer bytes than all scenes together: building and drawing still work
    for _ in range(10):
        build_scene(_motion_spec(), ctx).frame(1.0)
    assert ctx.media.peak <= 3 * one


def test_a_motion_image_problem_is_still_reported_at_build_time_with_its_path(tmp_path):
    from yamlreel.scenes import build_scene
    (tmp_path / "bad.png").write_bytes(b"not an image")
    with pytest.raises(CaptureError, match=r"elements\[0\]\.source: cannot read bad.png"):
        build_scene(_motion_spec("bad.png"), _motion_ctx(tmp_path))


# --- narration is checked as it grows ---------------------------------------------------------------------------

def test_narration_stops_loading_once_the_clips_pass_the_limit(tmp_path, monkeypatch):
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(1.0))
    processed = []
    real = voice_mod.process_clip
    monkeypatch.setattr(voice_mod, "process_clip", lambda s, vo: processed.append(1) or real(s, vo))
    spec = {"voiceover": {"gap": 0.5},
            "scenes": [{"type": "title", "title": f"t{k}", "seconds": 1, "narration_audio": "tone.wav"} for k in range(5)]}
    with pytest.raises(VoiceError, match=r"up to scene 1 already needs 3\.0s; the limit is 2\.5s"):
        voice_mod.prepare_narration(spec, None, tmp_path / "c", lambda p: tmp_path / p, max_seconds=2.5)
    assert len(processed) == 2   # scenes 2 to 4 were never loaded
    clips = voice_mod.prepare_narration(spec, None, tmp_path / "c", lambda p: tmp_path / p)
    assert len(clips) == 5      # no limit: unchanged behaviour


@needs_ffmpeg
def test_render_refuses_over_long_narration_before_loading_it_all(tmp_path, monkeypatch):
    (tmp_path / "tone.wav").write_bytes(_tone_wav_bytes(1.0))
    processed = []
    real = voice_mod.process_clip
    monkeypatch.setattr(voice_mod, "process_clip", lambda s, vo: processed.append(1) or real(s, vo))
    raw = {"music": {"style": "none"}, "limits": {"max_seconds": 4},
           "scenes": [{"type": "title", "title": f"t{k}", "seconds": 0.5, "narration_audio": "tone.wav"} for k in range(6)]}
    spec = {**validate(raw, Config(), base_dir=tmp_path), "_dir": str(tmp_path)}
    with pytest.raises(RenderError, match=r"voiceover: .*limits\.max_seconds"):
        render(spec, "square", tmp_path / "out", fps=5, scale=0.25, runtime=Runtime(roots=[tmp_path]))
    assert len(processed) < 6


# --- contact sheets are paginated -------------------------------------------------------------------------------

def test_an_oversized_contact_sheet_is_split_into_pages_of_whole_rows(tmp_path, monkeypatch):
    tl = _timeline(tmp_path, _shots(tmp_path, 5))
    allocated = []
    real_new = Image.new
    monkeypatch.setattr(render_mod.Image, "new", lambda mode, size, *a: allocated.append(size) or real_new(mode, size, *a))
    tile_w, tile_h = 100, int(100 * tl.ctx.H / tl.ctx.W)
    monkeypatch.setattr(contactpages, "MAX_SHEET_PIXELS", 2 * 4 * tile_w * (tile_h + 24))   # two rows a page
    first = contact_sheet(tl, tmp_path / "contact.png", tile_w=tile_w)
    pages = [first, *contactpages.extra_pages(tmp_path, "contact.png")]
    assert [p.name for p in pages] == ["contact.png", "contact-p2.png"]
    sizes = [Image.open(p).size for p in pages]
    assert sizes == [(400, 2 * (tile_h + 24)), (400, 1 * (tile_h + 24))]   # 10 tiles: 4 + 4, then 2
    assert max(w * h for w, h in allocated) <= contactpages.MAX_SHEET_PIXELS


def test_a_small_contact_sheet_is_one_page_as_before(tmp_path):
    tl = _timeline(tmp_path, _shots(tmp_path, 3))
    contact_sheet(tl, tmp_path / "contact.png")
    assert contactpages.extra_pages(tmp_path, "contact.png") == []
    assert Image.open(tmp_path / "contact.png").size == (4 * 480, 2 * (int(480 * tl.ctx.H / tl.ctx.W) + 24))


@needs_ffmpeg
def test_render_publishes_every_page_and_removes_pages_a_shorter_sheet_no_longer_has(tmp_path, monkeypatch):
    spec = _shots(tmp_path, 6)
    out = tmp_path / "out"
    monkeypatch.setattr(contactpages, "MAX_SHEET_PIXELS", 1)   # one row a page
    render(spec, "square", out, fps=5, scale=0.25, runtime=Runtime(roots=[tmp_path]))
    folder = out / "square"
    assert [p.name for p in contactpages.extra_pages(folder, "contact.png")] == ["contact-p2.png", "contact-p3.png"]
    monkeypatch.setattr(contactpages, "MAX_SHEET_PIXELS", contactpages.rows_per_page.__defaults__[0])
    render(spec, "square", out, fps=5, scale=0.25, runtime=Runtime(roots=[tmp_path]))
    assert contactpages.extra_pages(folder, "contact.png") == [] and (folder / "contact.png").exists()


# --- peak memory follows the working set ------------------------------------------------------------------------

RSS_PROBE = textwrap.dedent('''
    import resource, sys
    from pathlib import Path
    from PIL import Image
    from yamlreel.config import Config
    from yamlreel.render import Runtime, Timeline, contact_sheet
    from yamlreel.specs import validate
    n, work = int(sys.argv[1]), Path(sys.argv[2])
    spec = validate({"scenes": [{"type": "shot", "source": str(work / f"s{k}.png"), "seconds": 1.0} for k in range(n)],
                     "music": {"style": "none"}}, Config())
    spec["_dir"] = str(work)
    tl = Timeline(spec, "landscape", work / "cache", 0.25, Runtime(roots=[work]))
    for i in range(int(tl.total * 4)):
        tl.frame_at(i / 4)
    contact_sheet(tl, work / "contact.png")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(peak if sys.platform == "darwin" else peak * 1024)
''')


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs the resource module")
def test_peak_rss_tracks_the_working_set_not_the_scene_count(tmp_path):
    for k in range(10):
        Image.new("RGB", (4000, 3000), (k * 20, 90, 160)).save(tmp_path / f"s{k}.png", compress_level=1)
    probe = tmp_path / "probe.py"
    probe.write_text(RSS_PROBE)
    root = Path(__file__).resolve().parents[1]

    def peak(n: int) -> int:
        done = subprocess.run([sys.executable, str(probe), str(n), str(tmp_path)], capture_output=True, text=True,
                              cwd=root, timeout=300)
        assert done.returncode == 0, done.stderr
        return int(done.stdout.split()[-1])

    small, large = peak(2), peak(10)
    one = 4000 * 3000 * 3   # one decoded shot: 36 MB
    # Eager decoding holds all ten (8 more shots, about 290 MB more); on demand stays near two.
    assert large - small < 2 * one, f"peak RSS grew {(large - small) / 2**20:.0f} MB from 2 to 10 shot scenes"


MOTION_RSS_PROBE = textwrap.dedent('''
    import resource, sys
    from pathlib import Path
    from yamlreel.config import Config
    from yamlreel.specs import validate
    from yamlreel.render import Runtime, Timeline
    n, work = int(sys.argv[1]), Path(sys.argv[2])
    spec = validate({"scenes": [{"type": "motion", "seconds": 1.0, "elements": [
        {"kind": "image", "source": str(work / f"m{k}.png"), "at": 0, "enter": "none", "w": 2000}]} for k in range(n)],
        "music": {"style": "none"}}, Config())
    spec["_dir"] = str(work)
    tl = Timeline(spec, "landscape", work / "cache", 1.0, Runtime(roots=[work]))
    for i in range(int(tl.total * 4)):
        tl.frame_at(i / 4)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(peak if sys.platform == "darwin" else peak * 1024)
''')


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs the resource module")
def test_peak_rss_of_motion_image_scenes_tracks_the_working_set(tmp_path):
    for k in range(10):
        Image.new("RGBA", (3000, 3000), (k * 20, 90, 160, 255)).save(tmp_path / f"m{k}.png", compress_level=1)
    probe = tmp_path / "probe.py"
    probe.write_text(MOTION_RSS_PROBE)
    root = Path(__file__).resolve().parents[1]

    def peak(n: int) -> int:
        done = subprocess.run([sys.executable, str(probe), str(n), str(tmp_path)], capture_output=True, text=True,
                              cwd=root, timeout=300)
        assert done.returncode == 0, done.stderr
        return int(done.stdout.split()[-1])

    one = 2000 * 2000 * 4   # one pre-scaled image: 16 MB; pinning all ten held about 128 MB more than two
    small, large = peak(2), peak(10)
    assert large - small < 3 * one, f"peak RSS grew {(large - small) / 2**20:.0f} MB from 2 to 10 motion-image scenes"


def test_a_truncated_shot_image_is_reported_with_its_scene_before_rendering(tmp_path):
    spec = _shots(tmp_path, 3, size=(600, 400))
    good = (tmp_path / "s1.png").read_bytes()
    (tmp_path / "s1.png").write_bytes(good[: len(good) // 2])   # header intact, pixel data cut off
    with pytest.raises(CaptureError, match=r"scenes\[1\]\.source: cannot read image s1.png: .*truncated"):
        _timeline(tmp_path, spec)
