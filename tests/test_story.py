"""S4: the step badge, the cta end card, the share-copy question and `yamlreel story`."""
from __future__ import annotations

import pytest
import yaml
from PIL import Image, ImageChops

from yamlreel import badge as badge_mod
from yamlreel import story
from yamlreel.cli import main
from yamlreel.config import Config
from yamlreel.datascenes import CtaScene
from yamlreel.render import Timeline, share_copy
from yamlreel.scenes import SCENE_TYPES
from yamlreel.specs import SpecError, load_spec, validate
from yamlreel.voice import screen_text


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for prefix in ("YAMLREEL",):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def same(a, b):
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


def timeline(tmp_path, scenes, fmt="landscape", **top):
    spec = validate({"scenes": scenes, "music": {"style": "none"}, **top}, Config())
    return Timeline(spec, fmt, tmp_path, scale=0.5)


T = {"type": "title", "title": "x", "seconds": 3}


# --- step badge -------------------------------------------------------------------------------------------

def test_step_plan_numbers_distinct_labels_in_order():
    plan = badge_mod.step_plan(["A", "A", None, "B", "A", "C"])
    assert plan == [(1, 3, "A"), (1, 3, "A"), None, (2, 3, "B"), (1, 3, "A"), (3, 3, "C")]
    assert badge_mod.step_plan([None, None]) == [None, None] and badge_mod.step_plan([]) == []
    assert badge_mod.badge_text(2, 3, "Token bucket") == "STEP 2/3 · TOKEN BUCKET"


def test_the_badge_is_drawn_on_steps_only(tmp_path):
    tl = timeline(tmp_path, [T, {**T, "step": "One"}, {**T, "step": "One"}, {**T, "step": "Two"}])
    assert sorted(tl._badges) == [1, 2, 3]
    assert tl._badges[1] is tl._badges[2] and tl._badges[3] is not tl._badges[1]
    assert tl._badges[1].text == "STEP 1/2 · ONE" and tl._badges[3].text == "STEP 2/2 · TWO"
    x0, y0, x1, y1 = tl._badges[1].box
    assert abs((x0 + x1) / 2 - tl.ctx.W / 2) <= 1 and y0 == int(tl.ctx.H * badge_mod.TOP)
    plain = timeline(tmp_path, [T, T])
    assert same(tl._scene_frame(0, 1.0), plain._scene_frame(0, 1.0))        # no step, no badge
    with_badge, without = tl._scene_frame(1, 1.0), plain._scene_frame(1, 1.0)
    assert not same(with_badge, without)
    assert ImageChops.difference(with_badge.convert("RGB"), without.convert("RGB")).getbbox() is not None
    box = ImageChops.difference(with_badge.convert("RGB"), without.convert("RGB")).getbbox()
    assert box[0] >= x0 and box[2] <= x1 and box[1] >= y0 and box[3] <= y1    # only inside the badge box


def test_progress_bar_has_one_segment_per_step_and_fills_up_to_this_one(tmp_path):
    tl = timeline(tmp_path, [{**T, "step": s} for s in "ABC"])
    th = tl.ctx.theme
    for i, filled in ((0, 1), (1, 2), (2, 3)):
        b = tl._badges[i]
        y = b.rgb.height - 3
        row = [b.rgb.getpixel((x, y)) for x in range(b.rgb.width) if b.mask.getpixel((x, y))]
        assert sum(p == th.accent for p in row) > 0 and sum(p == th.line for p in row) > 0 or filled == 3
        segs, prev = [], None
        for x in range(b.rgb.width):
            px = b.rgb.getpixel((x, y)) if b.mask.getpixel((x, y)) else None
            if px != prev:
                segs.append(px)
                prev = px
        coloured = [p for p in segs if p is not None]
        assert len(coloured) == 3 and sum(p == th.accent for p in coloured) == filled


def test_a_wide_top_left_watermark_warns_about_the_badge(tmp_path):
    scenes = [{**T, "step": "A"}]
    big = timeline(tmp_path, scenes, fmt="vertical", watermark={"text": "A watermark with many many words in it", "size": 60})
    assert any("watermark overlaps the step badge" in w for w in big.warnings)
    small = timeline(tmp_path, scenes, fmt="vertical", watermark="Me")
    assert not any("step badge" in w for w in small.warnings)
    none = timeline(tmp_path, [T], fmt="vertical", watermark={"text": "A watermark with many many words in it", "size": 60})
    assert not any("step badge" in w for w in none.warnings)


def test_step_validation():
    assert validate({"scenes": [{**T, "step": "  Token   bucket "}]}, Config())["scenes"][0]["step"] == "Token bucket"
    for bad, msg in (("", "must not be empty"), ("x" * 61, "MAX_STEP_LABEL"), ([1], "must be text")):
        with pytest.raises(SpecError, match=rf"scenes\[0\]\.step.*{msg}"):
            validate({"scenes": [{**T, "step": bad}]}, Config())


def test_the_badge_stays_clear_of_the_diagram_title(tmp_path):
    d = {"type": "diagram", "title": "A diagram title", "seconds": 5, "step": "Step",
         "nodes": [{"id": "a", "label": "A", "x": .3, "y": .5}, {"id": "b", "label": "B", "x": .7, "y": .5}],
         "edges": [{"from": "a", "to": "b"}]}
    for fmt in ("landscape", "vertical", "square"):
        tl = timeline(tmp_path, [d], fmt=fmt)
        bx0, by0, bx1, by1 = tl._badges[0].box
        title_top = tl.ctx.H * 0.09
        assert by1 <= title_top, (fmt, by1, title_top)


# --- cta ------------------------------------------------------------------------------------------------

CTA = {"type": "cta", "question": "Which limiter does your API use?", "answers": ["Token bucket", "Sliding window"],
       "seconds": 6}


def test_cta_validates_zero_to_four_answers_and_fills_the_prompt():
    sc = validate({"scenes": [{"type": "cta", "question": "Q?"}]}, Config())["scenes"][0]
    assert sc["answers"] == [] and sc["prompt"] == "Answer in the comments" and sc["seconds"] == 6.0
    for n in range(5):
        assert len(validate({"scenes": [{**CTA, "answers": list("abcd"[:n])}]}, Config())["scenes"][0]["answers"]) == n
    with pytest.raises(SpecError, match=r"scenes\[0\]\.answers has 5 entries; the limit is 4 \(MAX_ANSWERS\)"):
        validate({"scenes": [{**CTA, "answers": list("abcde")}]}, Config())
    for field, bad, msg in (("question", "", "must not be empty"), ("answers", "A", "must be a list"),
                            ("answers", ["ok", " "], r"answers\[1\] must not be empty"),
                            ("prompt", "", "must not be empty"), ("question", ["x"], "must be text")):
        with pytest.raises(SpecError, match=msg):
            validate({"scenes": [{**CTA, field: bad}]}, Config())
    with pytest.raises(SpecError, match="missing question"):
        validate({"scenes": [{"type": "cta"}]}, Config())
    assert "cta" in SCENE_TYPES and CtaScene.FIELDS == {"question", "answers", "prompt"}


def test_screen_text_of_a_cta_reads_question_answers_and_prompt():
    sc = validate({"scenes": [{**CTA, "prompt": "Comment A or B"}]}, Config())["scenes"][0]
    assert screen_text(sc) == "Which limiter does your API use? A: Token bucket. B: Sliding window. Comment A or B."
    assert screen_text(validate({"scenes": [{"type": "cta", "question": "Q?"}]}, Config())["scenes"][0]) == \
        "Q? Answer in the comments."


@pytest.mark.parametrize("fmt", ["landscape", "vertical", "square"])
@pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
def test_cta_renders_and_nothing_overlaps_or_leaves_the_frame(tmp_path, fmt, n):
    long = "A rather long answer that has to wrap onto a second line"
    tl = timeline(tmp_path, [{**CTA, "answers": [long, "B", long, "D"][:n], "question": "Which of these " * 6 + "?"}], fmt=fmt)
    sc = tl.scenes[0]
    W, H = tl.ctx.W, tl.ctx.H
    boxes = [sc.bubble_box(), *(sc.chip_box(i) for i in range(n)), sc.prompt_box()]
    for x0, y0, x1, y1 in boxes:
        assert 0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            A, B = boxes[a], boxes[b]
            assert A[2] <= B[0] or B[2] <= A[0] or A[3] <= B[1] or B[3] <= A[1], (a, b)
    assert boxes[-1][3] < H * (0.74 if tl.ctx.vertical else 0.88)       # above the caption zone
    for t in (0.0, 0.3, 1.2, 3.0, 5.9):
        assert tl.frame_at(t).size == (W, H)


def test_cta_reveals_in_order_and_the_prompt_pulses_once_a_second(tmp_path):
    tl = timeline(tmp_path, [{**CTA, "answers": ["A1", "B1", "C1"]}])
    sc = tl.scenes[0]
    alphas = [[sc.chip_alpha(i, t / 10) for t in range(0, 60)] for i in range(3)]
    assert all(a == sorted(a) for a in alphas) and all(a[0] == 0 for a in alphas) and all(a[-1] == 1 for a in alphas)
    starts = [next(t for t, v in enumerate(a) if v > 0) for a in alphas]
    assert starts == sorted(starts) and len(set(starts)) == 3
    p0 = sc.prompt_at
    assert sc.pulse(p0 - 0.1) == 0 and sc.pulse(p0) == pytest.approx(0, abs=1e-9)
    assert sc.pulse(p0 + 0.5) == pytest.approx(1.0) and sc.pulse(p0 + 1.0) == pytest.approx(0, abs=1e-9)
    assert sc.pulse(p0 + 3.5) == pytest.approx(1.0)
    # the bubble pops: it overshoots 1.0 on its way and settles at exactly 1.0
    scales = [sc.pop_scale(sc.BUBBLE_AT + k * 0.01) for k in range(0, 60)]
    assert scales[0] == pytest.approx(0.7) and max(scales) > 1.0 and scales[-1] == pytest.approx(1.0)
    assert not same(tl._scene_frame(0, sc.prompt_at + 0.5), tl._scene_frame(0, sc.prompt_at + 1.0))
    assert same(tl._scene_frame(0, sc.prompt_at + 1.5 + 1.0), tl._scene_frame(0, sc.prompt_at + 1.5 + 2.0))


def test_a_cta_that_does_not_fit_its_scene_warns(tmp_path):
    tl = timeline(tmp_path, [{**CTA, "seconds": 1.5}])
    assert any("the cta card needs" in w for w in tl.warnings)


# --- share copy -----------------------------------------------------------------------------------------

def spec_with(description, *scenes, tags=()):
    return validate({"meta": {"title": "T", "description": description, "tags": list(tags), "links": ["https://x.example"]},
                     "scenes": list(scenes)}, Config())


def test_the_cta_question_is_added_once_before_the_hashtags():
    text, _ = share_copy(spec_with("About it.", T, CTA, {**CTA, "question": "Another?"}, tags=["a"]), 30, ["landscape"])
    lines = text.split("Description:\n")[1].strip().split("\n")
    assert lines == ["About it.", "Which limiter does your API use?", "https://x.example", "#a"]
    assert text.count("Which limiter does your API use?") == 1 and "Another?" not in text
    again, _ = share_copy(spec_with("Ask: Which limiter does your API use?", T, CTA), 30, ["landscape"])
    assert again.count("Which limiter does your API use?") == 1
    none, _ = share_copy(spec_with("About it.", T), 30, ["landscape"])
    assert "?" not in none
    empty, _ = share_copy(spec_with("", CTA), 30, ["landscape"])
    assert empty.split("Description:\n")[1].startswith("Which limiter")


# --- the outline and the generated spec -------------------------------------------------------------------

OUTLINE = {"topic": "Rate limiting", "hook": "One noisy client can take your API down.",
           "problem": "One burst starves every other client.",
           "steps": [{"name": "Token bucket", "say": "Each request spends a token.", "show": "array"},
                     {"name": "Sliding window", "say": "Count the requests in the last minute."}],
           "when_to_use": ["Bursty traffic: token bucket", "Exact limits: sliding window"],
           "question": "Which one does your API use?", "answers": ["Token bucket", "Sliding window"]}


def gen(outline=None, **kw):
    text = story.generate(story.parse_outline(outline or OUTLINE), **kw)
    return text, yaml.safe_load(text)


def test_the_generated_spec_has_the_arc_and_is_a_plain_valid_spec():
    text, spec = gen()
    kinds = [s["type"] for s in spec["scenes"]]
    assert kinds == ["type", "title", "array", "bullets", "bullets", "cta"]
    assert spec["scenes"][0]["transition"] == "cut" and spec["scenes"][1]["eyebrow"] == "THE PROBLEM"
    assert [s.get("step") for s in spec["scenes"]] == [None, None, "Token bucket", "Sliding window", None, None]
    assert spec["scenes"][2]["narration"] == "Each request spends a token." and spec["scenes"][-1]["answers"] == OUTLINE["answers"]
    assert spec["meta"]["title"] == "Rate limiting" and spec["subtitles"] is True
    assert "story" not in spec and "story" not in text.split("\n", 3)[3]
    assert text.startswith("# Generated by `yamlreel story`") and "TODO" in text.split("meta:")[0]
    validate(spec, Config())


def test_seconds_follow_the_words():
    assert story.narrated_seconds("type", "Short.") == 3.0
    long = " ".join(["word"] * 50)
    assert story.narrated_seconds("bullets", long) == pytest.approx(50 / 2.5 + 1)
    _, spec = gen({**OUTLINE, "hook": long})
    assert spec["scenes"][0]["seconds"] == 21.0


@pytest.mark.parametrize("kind", sorted(story.PLACEHOLDERS))
def test_every_placeholder_type_passes_load_spec(tmp_path, kind):
    text, _ = gen({**OUTLINE, "steps": [{"name": "Only step", "say": "Words.", "show": kind}]})
    p = tmp_path / f"{kind}.yaml"
    p.write_text(text, encoding="utf-8")
    sc = load_spec(p)["scenes"][2]
    assert sc["type"] == kind and sc["step"] == "Only step"


def test_every_scene_type_with_required_fields_is_a_placeholder_or_left_out_on_purpose():
    from yamlreel.specs import REQUIRED
    missing = set(REQUIRED) - set(story.PLACEHOLDERS)
    assert missing == {"shot", "cta"}


def test_optional_parts_can_be_left_out():
    text, spec = gen({"topic": "T", "hook": "H.", "steps": [{"name": "Only", "say": "Say it."}]})
    assert [s["type"] for s in spec["scenes"]] == ["type", "bullets", "title"]   # no problem, no when_to_use, closing card
    assert spec["scenes"][-1]["title"] == "T"
    _, spec = gen({**OUTLINE, "question": "Q?", "answers": []})
    assert spec["scenes"][-1]["answers"] == []


def test_theme_and_format_go_into_the_spec():
    _, spec = gen(theme="washi", fmt="vertical")
    assert spec["theme"] == "washi" and spec["formats"] == ["vertical"]
    with pytest.raises(story.StoryError, match="--theme: unknown theme"):
        gen(theme="nope")


def test_the_outline_is_strict():
    bad = [({**OUTLINE, "extra": 1}, "extra: unknown field for an outline"),
           ({k: v for k, v in OUTLINE.items() if k != "topic"}, "needs 'topic'"),
           ({k: v for k, v in OUTLINE.items() if k != "hook"}, "needs 'hook'"),
           ({k: v for k, v in OUTLINE.items() if k != "steps"}, "needs 'steps'"),
           ({**OUTLINE, "steps": []}, "0 entries; it needs 1 to 6"),
           ({**OUTLINE, "steps": [{"name": f"s{n}"} for n in range(7)]}, "7 entries; it needs 1 to 6"),
           ({**OUTLINE, "steps": [{"name": "a", "colour": 1}]}, r"steps\[0\]\.colour: unknown field for a step"),
           ({**OUTLINE, "steps": [{"say": "x"}]}, r"steps\[0\] needs a 'name'"),
           ({**OUTLINE, "steps": [{"name": "a", "show": "shot"}]}, r"steps\[0\]\.show must be one of"),
           ({**OUTLINE, "steps": [{"name": "a"}, {"name": "a"}]}, "names must be different"),
           ({**OUTLINE, "steps": "x"}, "steps must be a list"),
           ({**OUTLINE, "topic": ""}, "topic must not be empty"),
           ({**OUTLINE, "topic": "x" * 101}, "topic is 101 characters"),
           ({**OUTLINE, "hook": ["x"]}, "hook must be text"),
           ({**OUTLINE, "answers": list("abcde")}, "answers has 5 entries"),
           ({**OUTLINE, "question": None, "answers": ["a"]}, "answers needs a question"),
           ({**OUTLINE, "when_to_use": "x"}, "when_to_use must be a list"),
           ({**OUTLINE, "when_to_use": ["x"] * 7}, "when_to_use has 7 entries")]
    for outline, msg in bad:
        with pytest.raises(story.StoryError, match=msg):
            story.parse_outline(outline)
    with pytest.raises(story.StoryError, match="the outline must be a mapping"):
        story.parse_outline(["x"])


def test_a_bad_outline_file_is_a_clear_error(tmp_path):
    for text, msg in (("a: [", "cannot parse outline"), ("- x", "the outline must be a mapping")):
        (tmp_path / "o.yaml").write_text(text)
        with pytest.raises(story.StoryError, match=msg):
            story.load_outline(tmp_path / "o.yaml")
    with pytest.raises(story.StoryError, match="cannot read outline"):
        story.load_outline(tmp_path / "missing.yaml")


# --- the command ----------------------------------------------------------------------------------------

def test_story_command_writes_a_spec_that_validates_and_refuses_to_overwrite(tmp_path, capsys):
    (tmp_path / "o.yaml").write_text(yaml.safe_dump(OUTLINE))
    assert main(["story", "o.yaml", "--out", "s.yaml", "--theme", "midnight", "--format", "vertical"]) == 0
    assert "wrote s.yaml (2 steps)" in capsys.readouterr().out
    assert main(["validate", "s.yaml"]) == 0
    before = (tmp_path / "s.yaml").read_text()
    assert main(["story", "o.yaml", "--out", "s.yaml"]) == 2
    assert "exists; pass --force" in capsys.readouterr().err and (tmp_path / "s.yaml").read_text() == before
    assert main(["story", "o.yaml", "--out", "s.yaml", "--force"]) == 0
    assert (tmp_path / "s.yaml").read_text() != before     # no theme or format this time
    assert main(["story", "o.yaml"]) == 0 and (tmp_path / "o.spec.yaml").exists()   # the default destination


def test_story_command_reports_outline_errors(tmp_path, capsys):
    for outline, msg in (({**OUTLINE, "oops": 1}, "oops: unknown field"), ({**OUTLINE, "steps": []}, "0 entries"),
                         ({**OUTLINE, "steps": [{"name": f"s{n}"} for n in range(7)]}, "7 entries")):
        (tmp_path / "o.yaml").write_text(yaml.safe_dump(outline))
        assert main(["story", "o.yaml", "--out", "x.yaml", "--force"]) == 1
        assert msg in capsys.readouterr().err
    assert not (tmp_path / "x.yaml").exists()


def test_the_template_and_the_shipped_example_match(tmp_path):
    from pathlib import Path
    root = Path(__file__).parent.parent
    outline = root / "examples" / "outlines" / "load-balancing.yaml"
    shipped = (root / "examples" / "load-balancing-story.yaml").read_text(encoding="utf-8")
    assert story.generate(story.load_outline(outline), outline.name) == shipped       # regenerate: nothing drifted
    assert main(["init", "explainer", str(tmp_path / "e.yaml")]) == 0
    spec = load_spec(tmp_path / "e.yaml")
    assert [s["type"] for s in spec["scenes"]][-1] == "cta" and {s.get("step") for s in spec["scenes"]} >= {"Token bucket"}


def test_story_without_energy_or_duck_support_still_validates(monkeypatch):
    monkeypatch.setattr(story, "_supports", lambda: (False, False))
    _, spec = gen()
    assert "voiceover" not in spec and all("energy" not in s for s in spec["scenes"])


def test_more_than_twelve_distinct_step_labels_is_a_spec_error():
    ok = [{**T, "step": f"S{i}"} for i in range(badge_mod_limit())]
    validate({"scenes": ok}, Config())
    with pytest.raises(SpecError, match=r"13 distinct step labels; the limit is 12 \(MAX_STEP_LABELS\)"):
        validate({"scenes": ok + [{**T, "step": "one too many"}]}, Config())
    validate({"scenes": ok + [{**T, "step": "S0"}]}, Config())     # repeats do not count


def badge_mod_limit():
    from yamlreel.specs import MAX_STEP_LABELS
    return MAX_STEP_LABELS


@pytest.mark.parametrize("fmt", ["vertical", "landscape"])
def test_make_badge_survives_more_steps_than_pixels(tmp_path, fmt):
    tl = timeline(tmp_path, [T], fmt)
    for total in (12, 60, 400):
        b = badge_mod.make_badge(tl.ctx, total, total, "Many")
        assert b.rgb.width > 0 and b.text == f"STEP {total}/{total} · MANY"
    th = tl.ctx.theme
    b = badge_mod.make_badge(tl.ctx, 200, 400, "Many")
    y = b.rgb.height - 2
    assert b.rgb.getpixel((2, y)) == th.accent and b.rgb.getpixel((b.rgb.width - 3, y)) == th.line


# --- events -----------------------------------------------------------------------------------------------

def _outline(show, events, say="Short."):
    return {"topic": "t", "hook": "h", "steps": [{"name": "Queue", "say": say, "show": show, "events": events}]}


def _step_scene(show, events, say="Short."):
    spec = yaml.safe_load(story.generate(story.parse_outline(_outline(show, events, say))))
    return next(sc for sc in spec["scenes"] if sc.get("step") == "Queue")


EVENTS = [{"at": 1.0, "text": "Burst arrives"}, {"at": 3.5, "text": "Queue full"}, {"at": 5, "text": "Drained"},
          {"at": 6, "text": "Again"}]


def test_events_become_cues_for_diagram_meter_and_array(tmp_path):
    d = _step_scene("diagram", EVENTS[:2])
    assert d["flash"] == [{"node": "a", "at": 1.0, "text": "Burst arrives"}, {"node": "a", "at": 3.5, "text": "Queue full"}]
    assert [n["id"] for n in d["nodes"]] == ["a", "b"] and len(d["edges"]) == 1
    m = _step_scene("meter", EVENTS)
    assert m["steps"] == [{"at": 1.0, "level": 4, "text": "Burst arrives"}, {"at": 3.5, "level": 10, "text": "Queue full"},
                          {"at": 5, "level": 4, "text": "Drained"}, {"at": 6, "level": 10, "text": "Again"}]
    a = _step_scene("array", EVENTS)
    assert a["steps"] == [{"at": 1.0, "mark": [0], "text": "Burst arrives"}, {"at": 3.5, "mark": [1], "text": "Queue full"},
                          {"at": 5, "mark": [2], "text": "Drained"}, {"at": 6, "mark": [0], "text": "Again"}]
    for show in ("diagram", "meter", "array"):
        outline = _outline(show, EVENTS[:2])
        (tmp_path / f"{show}.yaml").write_text(story.generate(story.parse_outline(outline)), encoding="utf-8")
        load_spec(tmp_path / f"{show}.yaml")


@pytest.mark.parametrize("show", ["bullets", "table", "timeline"])
def test_events_on_other_scene_types_are_an_error(show):
    with pytest.raises(story.StoryError, match=rf"steps\[0\]\.events: events work with show: diagram, meter or array; "
                                               rf"this step shows {show}"):
        story.parse_outline(_outline(show, EVENTS[:1]))
    story.parse_outline(_outline(show, []))      # events: [] is the same as none


def test_event_validation():
    def bad(events, match):
        with pytest.raises(story.StoryError, match=match):
            story.parse_outline(_outline("meter", events))
    bad("x", "must be a list")
    bad([1], "must be a mapping")
    bad([{"at": 1}], "needs 'text'")
    bad([{"text": "x"}], "needs 'at'")
    bad([{"at": 1, "text": "x", "level": 3}], "unknown field")
    bad([{"at": -1, "text": "x"}], "from 0 to 60")
    bad([{"at": 61, "text": "x"}], "from 0 to 60")
    bad([{"at": "1", "text": "x"}], "from 0 to 60")
    bad([{"at": True, "text": "x"}], "from 0 to 60")
    bad([{"at": 2, "text": "x"}, {"at": 1, "text": "y"}], "must not be earlier")
    bad([{"at": 1, "text": ""}], "must not be empty")
    bad([{"at": 1, "text": "y" * 41}], "limit is 40")
    bad([{"at": n, "text": "x"} for n in range(9)], "the limit is 8")
    ok = story.parse_outline(_outline("meter", [{"at": n, "text": "x"} for n in (0, 0, 60)] + [{"at": 60, "text": "y" * 40}]))
    assert len(ok["steps"][0]["events"]) == 4


def test_events_lengthen_the_scene_to_hold_the_last_cue():
    assert _step_scene("meter", [{"at": 9, "text": "x"}])["seconds"] == 10.5
    long_say = " ".join(["word"] * 40)
    assert _step_scene("meter", [{"at": 9, "text": "x"}], say=long_say)["seconds"] == story.narrated_seconds("meter", long_say)
    assert _step_scene("meter", [])["seconds"] == story.narrated_seconds("meter", "Short.")
