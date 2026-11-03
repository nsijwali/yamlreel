"""Word-by-word subtitles (S3): word timing, keywords, chunks, drawing, caption deferral, SRT."""
from __future__ import annotations

import json
import re
import wave

import numpy as np
import pytest
import yaml
from PIL import Image, ImageChops

from fakes import _fake_voice_server
from yamlreel import legacy
from yamlreel import subtitles as subs
from yamlreel import voice as voice_mod
from yamlreel.cli import main
from yamlreel.config import Config
from yamlreel.render import RenderError, Runtime, Timeline, ffmpeg_exe, render
from yamlreel.safety import UnsafeInput, resolve_local
from yamlreel.specs import SpecError, validate
from yamlreel.subtitles import SceneWords, Word
from yamlreel.textkit import layout_words
from yamlreel.theme import font
from yamlreel.voice import NARRATION_LEAD, SAMPLE_RATE


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for prefix in ("YAMLREEL", legacy.LEGACY.upper()):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)
    legacy._warned.clear()


def bursts(*seconds: float, gap: float = 0.3, lead: float = 0.1, tail: float = 0.1) -> np.ndarray:
    """Tone bursts of the given lengths separated by `gap` seconds of exact silence."""
    def tone(d: float) -> np.ndarray:
        t = np.arange(int(d * SAMPLE_RATE)) / SAMPLE_RATE
        return (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def silence(d: float) -> np.ndarray:
        return np.zeros(int(d * SAMPLE_RATE), dtype=np.float32)

    parts = [silence(lead)]
    for n, d in enumerate(seconds):
        parts += [tone(d)] + ([silence(gap)] if n < len(seconds) - 1 else [])
    return np.concatenate([*parts, silence(tail)])


TEXT = "First part goes here. Second part goes here. Third part goes here"   # two periods, three bursts


def write_wav(path, samples, rate=SAMPLE_RATE):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return path


def same(a: Image.Image, b: Image.Image) -> bool:
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


# --- estimating word times ------------------------------------------------------------------------------

def test_sentence_ends_fall_inside_the_silences():
    samples = bursts(1.0, 1.2, 0.9)
    words = subs.estimate_words(TEXT, samples)
    assert [w.text for w in words] == TEXT.split()
    clip = len(samples) / SAMPLE_RATE
    gap1 = (0.1 + 1.0, 0.1 + 1.0 + 0.3)
    gap2 = (gap1[1] + 1.2, gap1[1] + 1.2 + 0.3)
    for k, (g0, g1) in ((3, gap1), (7, gap2)):  # "here." is the 4th and 8th word
        assert words[k].text.endswith(".")
        assert words[k].end == pytest.approx(g0, abs=0.05)
        assert words[k + 1].start == pytest.approx(g1, abs=0.05)
    assert words[0].start == pytest.approx(0.1, abs=0.05) and words[-1].end == pytest.approx(clip - 0.1, abs=0.05)


def test_words_are_monotonic_within_the_clip_and_deterministic():
    samples = bursts(1.0, 1.2, 0.9)
    a, b = subs.estimate_words(TEXT, samples), subs.estimate_words(TEXT, samples)
    assert a == b
    clip = len(samples) / SAMPLE_RATE
    for prev, cur in zip(a, a[1:]):
        assert prev.start <= prev.end <= cur.start + 1e-9
    assert all(0 <= w.start <= w.end <= clip for w in a)


def test_longer_words_take_longer_and_digits_count():
    est = subs.estimate_words("a extraordinarily", bursts(1.0))
    assert (est[1].end - est[1].start) > 3 * (est[0].end - est[0].start)
    nums = subs.estimate_words("go 2024", bursts(1.0))
    assert (nums[1].end - nums[1].start) > (nums[0].end - nums[0].start)


def test_without_gaps_the_time_is_shared_in_proportion():
    est = subs.estimate("One. Two. Three.", bursts(1.5))   # two boundaries, no silence to put them in
    assert est.boundaries == 2 and est.matched == 0
    assert est.words[0].start == pytest.approx(0.1, abs=0.03)
    assert est.words[-1].end == pytest.approx(1.6, abs=0.03)


def test_silent_audio_and_empty_text_do_not_crash():
    assert subs.estimate_words("", bursts(1.0)) == []
    silent = np.zeros(SAMPLE_RATE, dtype=np.float32)
    words = subs.estimate_words("hello there", silent)
    assert [w.text for w in words] == ["hello", "there"] and words[-1].end == pytest.approx(1.0, abs=0.05)
    assert subs.estimate_words("hi", np.zeros(0, dtype=np.float32))[0].start == 0.0


def test_a_lone_dash_joins_the_word_before_it():
    assert [w.text for w in subs.estimate_words("wait — what", bursts(1.0))] == ["wait —", "what"]


def audio(*parts: float) -> np.ndarray:
    """Alternating tone and silence lengths in seconds, starting with a tone: audio(1.0, 0.3, 1.0)."""
    out = []
    for n, d in enumerate(parts):
        k = int(round(d * SAMPLE_RATE))
        if n % 2 == 0:
            t = np.arange(k) / SAMPLE_RATE
            out.append((0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        else:
            out.append(np.zeros(k, dtype=np.float32))
    return np.concatenate([np.zeros(int(0.1 * SAMPLE_RATE), dtype=np.float32), *out, np.zeros(int(0.1 * SAMPLE_RATE), dtype=np.float32)])


SENTENCES = "Red cat sat. Big dog ran. Old man sat. Hot sun set."   # four sentences of three one-syllable words


def test_a_sentence_end_whose_pause_is_too_short_to_find_does_not_shift_the_others():
    # the pause after sentence 1 is 80 ms (below MIN_GAP_SECONDS); the other two are 300 ms
    est = subs.estimate(SENTENCES, audio(1.0, 0.08, 1.0, 0.3, 1.0, 0.3, 1.0))
    w = est.words
    assert est.boundaries == 3 and est.matched == 2
    second_end, third_end = 0.1 + 2.08, 0.1 + 2.08 + 0.3 + 1.0
    assert w[5].end == pytest.approx(second_end, abs=0.05) and w[6].start == pytest.approx(second_end + 0.3, abs=0.05)
    assert w[8].end == pytest.approx(third_end, abs=0.05) and w[9].start == pytest.approx(third_end + 0.3, abs=0.05)
    assert w[2].end == pytest.approx(0.1 + 1.04, abs=0.1)   # sentence 1 is placed by its share of the speech


def test_a_breath_inside_a_sentence_does_not_take_the_sentence_end():
    # sentence 1 is six words said in two halves with a 600 ms breath; every sentence end has a 300 ms pause
    text = "Red cat sat big dog ran. Old man sat. Hot sun set. Big cow lay."
    est = subs.estimate(text, audio(1.0, 0.6, 1.0, 0.3, 1.0, 0.3, 1.0, 0.3, 1.0))
    w = est.words
    assert est.boundaries == 3 and est.matched == 3
    first_end = 0.1 + 1.0 + 0.6 + 1.0
    assert w[5].end == pytest.approx(first_end, abs=0.05) and w[6].start == pytest.approx(first_end + 0.3, abs=0.05)
    assert [x.text for x in w] == text.split()
    assert w[2].end < w[3].start + 1e-9   # the breath is not a cut: the six words share the 2.6 s evenly
    assert w[3].start == pytest.approx(0.1 + 1.3, abs=0.05)


def test_the_text_and_the_audio_stay_in_order():
    est = subs.estimate("Red cat. Big dog. Old man. Hot sun.", audio(0.7, 0.4, 0.7, 0.2, 0.7, 0.3, 0.7))
    cuts = [b.start - a.end for a, b in zip(est.words, est.words[1:])]
    assert all(c >= -1e-9 for c in cuts) and sum(c > 0.15 for c in cuts) == est.matched == 3


def test_an_end_far_from_every_pause_is_not_counted_as_matched():
    text = "One. " + " ".join(["two"] * 11) + "."   # the first end is about 1/7 of the way along; the only pause is 2 s in
    est = subs.estimate(text, audio(2.0, 0.3, 3.0))
    assert est.boundaries == 1 and est.matched == 0
    assert est.words[0].end == pytest.approx(0.1 + 5.3 * 2 / 13, abs=0.05)   # shared out by weight, not put in the pause


def test_match_ends_prefers_the_sentence_over_a_clause_for_one_pause():
    assert subs.match_ends([1.0, 1.1], [1, 2], [1.05], [0.3], 0.6) == [(1, 0)]
    assert subs.match_ends([1.0, 1.1], [2, 1], [1.05], [0.3], 0.6) == [(0, 0)]
    assert subs.match_ends([], [], [1.0], [0.3], 0.6) == [] and subs.match_ends([1.0], [2], [], [], 0.6) == []


def test_match_ends_is_in_order_and_within_tolerance():
    rng = np.random.default_rng(3)
    for _ in range(40):
        n, m = int(rng.integers(1, 12)), int(rng.integers(1, 12))
        expected = sorted(rng.uniform(0, 10, n))
        places = sorted(rng.uniform(0, 10, m))
        pairs = subs.match_ends(expected, list(rng.integers(1, 3, n)), places, list(rng.uniform(0.1, 0.8, m)), 0.8)
        assert [j for j, _ in pairs] == sorted({j for j, _ in pairs}) and [i for _, i in pairs] == sorted({i for _, i in pairs})
        assert all(abs(places[i] - expected[j]) <= 0.8 + 1e-9 for j, i in pairs)


def test_fast_speech_with_short_pauses_stays_close_to_the_true_word_times():
    """Words with known times: tone bursts of uneven length, 40 ms between words, 140 ms between sentences."""
    text = "Quick brown foxes jump. Lazy dogs sleep all day. Rate limiting stops noisy clients. Tokens refill steadily."
    words = text.split()
    wts = [subs._weight(x) for x in words]
    scale = [1.0, 1.25, 0.8, 1.1, 0.9, 1.2, 0.85] * 4
    t, truth, parts = 0.1, [], []
    for k, (x, wt) in enumerate(zip(words, wts)):
        dur = 0.17 * wt * scale[k]
        truth.append(t)
        parts += [dur, 0.14 if x.endswith(".") else 0.04]
        t += dur + parts[-1]
    parts.pop()
    est = subs.estimate(text, audio(*parts))
    err = [abs(w.start - tr) for w, tr in zip(est.words, truth)]
    assert est.matched >= 2 and sum(err) / len(err) < 0.12 and max(err) < 0.3


def test_the_loudness_pass_gives_the_same_answer_in_any_precision_and_over_many_blocks():
    long = np.concatenate([audio(1.0, 0.5)] * 60)   # about 100 s: more than one FRAME_BLOCK of frames
    a = subs._speech_frames(long)
    assert len(a) > 2 * subs.FRAME_BLOCK
    assert np.array_equal(a, subs._speech_frames(long.astype(np.float64)))
    assert np.array_equal(a, subs._speech_frames((long * 32767).astype(np.int16).astype(np.float32) / 32767))
    assert not subs._speech_frames(np.full(SAMPLE_RATE, np.nan, dtype=np.float32)).any()


def test_a_vowel_is_a_vowel():
    for word in ("×", "÷", "ß", "Ñ", "ñ", "×÷"):
        assert subs._weight(word) == 1.0 and not subs._VOWELS.search(word)
    assert subs._VOWELS.search("é") and subs._VOWELS.search("Ö") and subs._VOWELS.search("Å")
    assert subs._weight("año") == subs._weight("ano") == 2.0


# --- narration_words files ------------------------------------------------------------------------------

def words_file(tmp_path, data, name="w.json"):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


GOOD = [{"word": "Hello", "start": 0.0, "end": 0.4}, {"word": "world", "start": 0.5, "end": 1.0, "prob": 0.9}]


def test_a_word_file_loads(tmp_path):
    got = subs.load_word_file(words_file(tmp_path, GOOD), 1.0)
    assert got == [Word("Hello", 0.0, 0.4), Word("world", 0.5, 1.0)]
    assert subs.load_word_file(words_file(tmp_path, [{"word": "x", "start": 0, "end": 1.15}]), 1.0)  # within the slack


@pytest.mark.parametrize("data,msg", [
    ("not json", "not readable JSON"),
    ({"word": "x"}, "non-empty list"),
    ([], "non-empty list"),
    ([{"word": "", "start": 0, "end": 1}], "non-empty text 'word'"),
    ([{"word": "x", "start": "0", "end": 1}], r"\.start must be a number"),
    ([{"word": "x", "start": 0, "end": True}], r"\.end must be a number"),
    ([{"word": "x", "start": 0.5, "end": 0.2}], "0 <= start <= end"),
    ([{"word": "x", "start": -1, "end": 0.2}], "0 <= start <= end"),
    (GOOD[::-1], "must not go backwards"),
    ([{"word": "x", "start": 0, "end": 1.5}], "clip is only"),
])
def test_bad_word_files_are_rejected(tmp_path, data, msg):
    with pytest.raises(subs.SubtitleError, match=msg):
        subs.load_word_file(words_file(tmp_path, data), 1.0)


def test_word_files_are_limited_in_size_and_entries(tmp_path):
    many = [{"word": "a", "start": 0, "end": 0} for _ in range(subs.MAX_WORDS_ENTRIES + 1)]
    with pytest.raises(subs.SubtitleError, match="MAX_WORDS_ENTRIES"):
        subs.load_word_file(words_file(tmp_path, many), 1.0)
    big = words_file(tmp_path, "[" + " " * (subs.MAX_WORDS_BYTES + 1) + "]", "big.json")
    with pytest.raises(subs.SubtitleError, match="MAX_WORDS_BYTES"):
        subs.load_word_file(big, 1.0)
    with pytest.raises(subs.SubtitleError, match="cannot read"):
        subs.load_word_file(tmp_path / "missing.json", 1.0)


def test_word_files_are_confined_to_the_allowed_folders(tmp_path):
    inside = tmp_path / "spec"
    inside.mkdir()
    words_file(inside, GOOD)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    words_file(outside, GOOD)
    spec = {"scenes": [{"type": "title", "title": "T", "seconds": 3, "narration_audio": "a.wav", "narration": "Hello world",
                        "narration_words": str(outside / "w.json")}], "subtitles": True}
    spec = validate(spec, Config())
    clips = {0: bursts(1.0)}
    resolver = lambda p: resolve_local(p, inside, [inside])  # noqa: E731
    with pytest.raises(UnsafeInput):
        subs.scene_words(spec, clips, resolver)
    spec["scenes"][0]["narration_words"] = "w.json"
    got, _ = subs.scene_words(spec, clips, resolver)
    assert got[0].source == "file" and [w.text for w in got[0].words] == ["Hello", "world"]


@pytest.mark.parametrize("raw,msg", [
    ('[{"word": "x", "start": NaN, "end": 1}]', r"\.start must be a number"),
    ('[{"word": "x", "start": 0, "end": Infinity}]', r"\.end must be a number"),
    ('[{"word": "x", "start": -Infinity, "end": 1}]', r"\.start must be a number"),
    ('[{"word": "x", "start": 0, "end": 1e999}]', r"\.end must be a number"),
    ('[{"word": "x", "start": 0, "end": %s}]' % ("9" * 400), r"\.end must be a number"),   # too large for a float
    ("[" * 100000 + "]" * 100000, "not readable JSON"),                                    # nested past the recursion limit
    ('[{"word": "bad\\u0000word", "start": 0, "end": 1}]', "control characters"),
    ('[{"word": "bell\\u0007", "start": 0, "end": 1}]', "control characters"),
])
def test_word_files_with_numbers_or_nesting_that_python_chokes_on_are_rejected_cleanly(tmp_path, raw, msg):
    with pytest.raises(subs.SubtitleError, match=msg):
        subs.load_word_file(words_file(tmp_path, raw), 1.0)


def test_whitespace_in_a_word_cannot_break_the_srt(tmp_path):
    got = subs.load_word_file(words_file(tmp_path, [
        {"word": "two\n\nlines", "start": 0.0, "end": 0.4}, {"word": "  tab\tseparated  ", "start": 0.5, "end": 1.0}]), 1.0)
    assert [w.text for w in got] == ["two lines", "tab separated"]
    cue = subs.Cue(0.0, 1.0, (Word("a\n\nb", 0, 0.5), Word("c\r\nd", 0.5, 1.0)), (False, False))
    block = subs.srt_text([cue, subs.Cue(1.0, 2.0, (Word("e", 1.0, 2.0),), (False,))])
    assert block.split("\n\n") == ["1\n00:00:00,000 --> 00:00:01,000\na b c d", "2\n00:00:01,000 --> 00:00:02,000\ne\n"]


def test_a_word_file_reached_through_a_symlink_cannot_leave_the_allowed_folders(tmp_path):
    inside, outside = tmp_path / "spec", tmp_path / "elsewhere"
    inside.mkdir()
    outside.mkdir()
    words_file(outside, GOOD)
    (inside / "link.json").symlink_to(outside / "w.json")
    spec = validate({"scenes": [{"type": "title", "title": "T", "seconds": 3, "narration": "Hello world",
                                 "narration_audio": "a.wav", "narration_words": "link.json"}], "subtitles": True}, Config())
    with pytest.raises(UnsafeInput):
        subs.scene_words(spec, {0: bursts(1.0)}, lambda p: resolve_local(p, inside, [inside]))


def test_a_word_file_that_is_not_utf8_is_rejected(tmp_path):
    p = tmp_path / "w.json"
    p.write_bytes(b'[{"word": "\xff\xfe", "start": 0, "end": 1}]')
    with pytest.raises(subs.SubtitleError, match="not readable JSON"):
        subs.load_word_file(p, 1.0)


def test_a_word_file_error_names_the_scene(tmp_path):
    words_file(tmp_path, [{"word": "x", "start": 0, "end": 9}])
    spec = validate({"scenes": [{"type": "title", "title": "T", "seconds": 3, "narration": "x", "narration_words": "w.json"}],
                     "subtitles": True}, Config())
    with pytest.raises(subs.SubtitleError, match=r"scenes\[0\]\.narration_words: w\.json\[0\]"):
        subs.scene_words(spec, {0: bursts(1.0)}, lambda p: tmp_path / p)


# --- the text is the text the voice speaks --------------------------------------------------------------

def test_narration_text_rules():
    nt = voice_mod.narration_text
    assert nt({"type": "title", "title": "T", "narration": "Say this"}, {}) == "Say this"
    assert nt({"type": "title", "title": "T"}, {}) is None
    assert nt({"type": "title", "title": "T", "narration": False}, {"read_screen": True}) is None
    assert nt({"type": "title", "title": "T", "subtitle": "S"}, {"read_screen": True}) == "T. S."
    assert nt({"type": "title", "title": "T", "narration": "own"}, {"read_screen": True}) == "own"
    assert nt({"type": "title", "title": "T", "narration_audio": "a.wav"}, {}) is None  # recording, no transcript
    assert nt({"type": "title", "title": "T", "narration_audio": "a.wav", "narration": "said"}, {}) == "said"
    assert nt({"type": "title", "title": "T", "narration_audio": "a.wav", "narration": False}, {}) is None


def test_synthesis_and_subtitles_use_the_same_string(tmp_path):
    spec = validate({"scenes": [{"type": "bullets", "title": "Steps", "items": ["one", "two"], "seconds": 3},
                                {"type": "title", "title": "End", "seconds": 3, "narration": "Own words here"}],
                     "voiceover": {"read_screen": True}, "subtitles": True, "music": {"style": "none"}}, Config())
    srv, calls, url = _fake_voice_server(seconds=1.0)
    try:
        clips = voice_mod.prepare_narration(spec, voice_mod.VoiceServer(url=url), tmp_path / "c", lambda p: tmp_path / p)
    finally:
        srv.shutdown()
    words, _ = subs.scene_words(spec, clips, lambda p: tmp_path / p)
    assert [c["text"] for c in calls] == [" ".join(w.text for w in words[i].words) for i in (0, 1)]
    assert calls[0]["text"] == "Steps. one. two."


def test_a_recording_without_a_transcript_warns(tmp_path):
    spec = validate({"scenes": [{"type": "title", "title": "T", "seconds": 3, "narration_audio": "a.wav"}],
                     "subtitles": True}, Config())
    got, warns = subs.scene_words(spec, {0: bursts(1.0)}, lambda p: tmp_path / p)
    assert got == {}
    assert warns == ["scene 0: narration_audio has no transcript; add narration: text for subtitles"]


def test_estimated_timing_is_reported_once_and_a_poor_match_is_called_out(tmp_path):
    spec = validate({"scenes": [{"type": "title", "title": "T", "seconds": 5, "narration": "One. Two. Three. Four. Five."}],
                     "subtitles": True}, Config())
    got, warns = subs.scene_words(spec, {0: bursts(2.0)}, lambda p: tmp_path / p)  # four boundaries, no pauses
    assert got[0].source == "estimate"
    assert any("only 0 of 4" in w for w in warns) and any("estimated from the audio in 1 scene" in w for w in warns)
    _, calm = subs.scene_words(spec, {0: bursts(0.4, 0.4, 0.4, 0.4, 0.4)}, lambda p: tmp_path / p)
    assert not any("only" in w for w in calm)


def test_a_word_file_that_cannot_be_used_is_reported(tmp_path):
    base = {"type": "title", "title": "T", "seconds": 3, "narration": "Hello world", "narration_words": "w.json"}
    words_file(tmp_path, GOOD)
    resolve = lambda p: tmp_path / p   # noqa: E731
    # off for this scene
    spec = validate({"scenes": [{**base, "subtitles": False, "narration_audio": "a.wav"}], "subtitles": True}, Config())
    got, warns = subs.scene_words(spec, {0: bursts(1.0)}, resolve)
    assert got == {} and warns == ["scene 0: narration_words is ignored because subtitles are off for this scene"]
    spec = validate({"scenes": [{**base, "narration_audio": "a.wav"}]}, Config())   # subtitles off at the top
    assert "subtitles are off" in subs.scene_words(spec, {0: bursts(1.0)}, resolve)[1][0]
    # no narration audio at all (for example --no-voice)
    spec = validate({"scenes": [base], "subtitles": True}, Config())
    got, warns = subs.scene_words(spec, {}, resolve)
    assert got == {} and warns == ["scene 0: narration_words is ignored because the scene has no narration audio"]
    # synthesised narration: the times must match audio that can change between runs
    got, warns = subs.scene_words(spec, {0: bursts(1.0)}, resolve)
    assert got[0].source == "file" and len(warns) == 1 and "synthesised narration" in warns[0]
    # a recording is the normal case: no warning
    spec = validate({"scenes": [{**base, "narration_audio": "a.wav"}], "subtitles": True}, Config())
    assert subs.scene_words(spec, {0: bursts(1.0)}, resolve)[1] == []


# --- keywords and numbers -------------------------------------------------------------------------------

def test_keywords_match_whole_words_and_phrases_case_insensitively():
    texts = "A Token bucket refills; the token-bucket limiter returns 429 or 5.5 errors.".split()
    flags = subs.keyword_flags(texts, ["token bucket", "LIMITER"], numbers=False)
    assert [t for t, f in zip(texts, flags) if f] == ["Token", "bucket", "limiter"]
    assert not any(subs.keyword_flags(["bucketing", "token"], ["bucket"], False))  # whole words only


def test_numbers_are_keywords_when_asked():
    texts = ["got", "429,", "twice", "in", "2024."]
    assert subs.keyword_flags(texts, [], numbers=True) == [False, True, False, False, True]
    assert subs.keyword_flags(texts, [], numbers=False) == [False] * 5
    assert subs.keyword_flags(["go"], ["", "  "], True) == [False]


def test_a_keyword_phrase_can_span_chunks():
    words = [Word(t, 0.2 * k, 0.2 * k + 0.15) for k, t in enumerate("the token bucket fills".split())]
    cues = subs.build_cues(words, 2, ["token bucket"], numbers=False, lead=0)
    assert [c.keywords for c in cues] == [(False, True), (True, False)]


# --- chunks and cues ------------------------------------------------------------------------------------

def spoken(text, step=0.3):
    return [Word(t, k * step, k * step + step * 0.8) for k, t in enumerate(text.split())]


def test_chunks_hold_at_most_n_words_and_never_cross_a_sentence():
    cues = subs.build_cues(spoken("One two three. Four five six seven eight"), 4, lead=0)
    assert [[w.text for w in c.words] for c in cues] == [["One", "two", "three."], ["Four", "five", "six", "seven"],
                                                         ["eight"]]


def test_a_long_silence_starts_a_new_chunk():
    words = [Word("a", 0, 0.2), Word("b", 0.3, 0.5), Word("c", 1.5, 1.7)]
    assert [len(c.words) for c in subs.build_cues(words, 4, lead=0)] == [2, 1]


def test_cues_do_not_overlap_start_after_the_lead_and_respect_the_limit():
    cues = subs.build_cues(spoken("a b c d e f g h i j k l"), 3, lead=0.3, limit=3.0)
    assert cues[0].start == pytest.approx(0.3)
    for a, b in zip(cues, cues[1:]):
        assert a.start < a.end <= b.start + 1e-9
    assert cues[-1].end <= 3.0 + 1e-9


def test_cues_never_overlap_even_when_word_times_do():
    words = [Word("a", 0.0, 1.0), Word("b", 0.4, 1.2), Word("c", 0.4, 0.9), Word("d", 3.0, 3.2)]   # a file may overlap words
    cues = subs.build_cues(words, 1, lead=0)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(cues, cues[1:]))
    assert all(c.end > c.start for c in cues)
    assert [c.words[0].text for c in cues] == ["a", "c", "d"]   # b starts with c, so it would never be seen
    assert (cues[0].start, cues[0].end) == (0.0, 0.4)


def test_a_cue_whose_next_one_starts_with_it_is_dropped():
    cues = subs.build_cues([Word("a", 1.0, 1.4), Word("b", 1.0, 1.5), Word("c", 2.0, 2.2)], 1, lead=0)
    assert [c.words[0].text for c in cues] == ["b", "c"]
    assert (cues[0].start, cues[0].end) == (1.0, 2.0) and cues[1].start == 2.0


def test_the_active_word_is_the_last_one_that_started():
    cue = subs.build_cues(spoken("one two three"), 4, lead=0)[0]
    assert [cue.active(t) for t in (0.0, 0.29, 0.31, 0.7, 5.0)] == [0, 0, 1, 2, 2]


def test_layout_words_wraps_and_centres_lines():
    fnt = font("sans_bold", 40)
    boxes, w, h = layout_words("alpha beta gamma delta epsilon".split(), fnt, 300)
    lines = {b.line for b in boxes}
    assert len(lines) >= 2 and h == pytest.approx(len(lines) * 40 * 1.28)
    for n in lines:
        row = [b for b in boxes if b.line == n]
        left, right = row[0].x, w - (row[-1].x + row[-1].w)
        assert left == pytest.approx(right, abs=1e-6)
        assert all(a.x + a.w < b.x for a, b in zip(row, row[1:]))
    assert layout_words(["x" * 100], fnt, 50)[0][0].line == 0  # a too-wide word still gets a line


# --- SRT ------------------------------------------------------------------------------------------------

def test_srt_time_format():
    assert subs.srt_time(0) == "00:00:00,000"
    assert subs.srt_time(3661.5) == "01:01:01,500"
    assert subs.srt_time(59.9996) == "00:01:00,000"


def test_srt_is_well_formed_and_matches_the_chunks():
    cues = subs.build_cues(spoken("One two three. Four five"), 3, lead=0.3)
    text = subs.srt_text(cues, uppercase=True)
    blocks = text.strip().split("\n\n")
    assert len(blocks) == len(cues)
    last_end = -1.0
    for n, (blk, cue) in enumerate(zip(blocks, cues), 1):
        idx, stamp, *body = blk.split("\n")
        assert idx == str(n)
        m = re.fullmatch(r"(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})", stamp)
        assert m
        a = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        b = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000
        assert a == pytest.approx(cue.start, abs=0.001) and b == pytest.approx(cue.end, abs=0.001)
        assert a < b and a >= last_end - 1e-9
        assert body == [cue.text(True)]
        last_end = b


# --- validation -----------------------------------------------------------------------------------------

SCENE = {"type": "title", "title": "T", "seconds": 3, "narration": "Hello there"}


def test_subtitles_default_to_off_and_true_means_the_defaults():
    assert validate({"scenes": [SCENE]}, Config())["subtitles"] is False
    assert validate({"subtitles": None, "scenes": [SCENE]}, Config())["subtitles"] is False
    on = validate({"subtitles": True, "scenes": [SCENE]}, Config())["subtitles"]
    assert on == subs.DEFAULT_SUBTITLES and on is not subs.DEFAULT_SUBTITLES
    full = validate({"subtitles": {"style": "pop", "words": 2, "position": "middle", "size": 1.4, "uppercase": True,
                                   "keywords": ["token bucket", 429], "numbers": False, "srt": True},
                     "scenes": [SCENE]}, Config())["subtitles"]
    assert full == {"style": "pop", "words": 2, "position": "middle", "size": 1.4, "uppercase": True,
                    "keywords": ["token bucket", "429"], "numbers": False, "srt": True}


@pytest.mark.parametrize("sub,msg", [
    ({"style": "glow"}, "subtitles.style must be one of karaoke, pop, plain"),
    ({"words": 0}, "subtitles.words must be an integer between 1 and 8"),
    ({"words": 9}, "subtitles.words must be an integer between 1 and 8"),
    ({"words": 2.5}, "subtitles.words must be an integer"),
    ({"position": "top"}, "subtitles.position must be one of lower, middle"),
    ({"size": 0.5}, "subtitles.size must be a number between 0.6 and 1.6"),
    ({"size": 2}, "subtitles.size must be a number between 0.6 and 1.6"),
    ({"uppercase": "yes"}, "subtitles.uppercase must be true or false"),
    ({"srt": 1}, "subtitles.srt must be true or false"),
    ({"numbers": None}, "subtitles.numbers must be true or false"),
    ({"keywords": "token"}, "subtitles.keywords must be a list"),
    ({"keywords": [" "]}, r"subtitles.keywords\[0\] must not be empty"),
    ({"keywords": ["x" * 61]}, "MAX_KEYWORD_CHARS"),
    ({"keywords": ["k"] * 51}, "MAX_KEYWORDS"),
    ({"colour": "red"}, "unknown"),
])
def test_bad_subtitle_settings_are_rejected(sub, msg):
    with pytest.raises(SpecError, match=msg):
        validate({"subtitles": sub, "scenes": [SCENE]}, Config())


def test_a_mapping_that_is_not_one_is_rejected():
    with pytest.raises(SpecError, match="subtitles must be a mapping"):
        validate({"subtitles": "karaoke", "scenes": [SCENE]}, Config())


def test_scene_fields():
    ok = validate({"scenes": [{**SCENE, "subtitles": False, "keywords": ["hi", 7], "narration_words": "w.json"}]}, Config())
    sc = ok["scenes"][0]
    assert sc["subtitles"] is False and sc["keywords"] == ["hi", "7"] and sc["narration_words"] == "w.json"
    for field, value, msg in (("subtitles", "no", r"scenes\[0\]\.subtitles must be true or false"),
                              ("keywords", "x", r"scenes\[0\]\.keywords must be a list"),
                              ("narration_words", "", r"scenes\[0\]\.narration_words must be a path"),
                              ("narration_words", 3, r"scenes\[0\]\.narration_words must be a path")):
        with pytest.raises(SpecError, match=msg):
            validate({"scenes": [{**SCENE, field: value}]}, Config())


def test_scene_settings_merge_keywords_and_honour_opt_outs():
    spec = validate({"subtitles": {"keywords": ["alpha"]}, "scenes": [SCENE]}, Config())
    cfg = subs.scene_settings(spec, {"keywords": ["beta", "alpha"]})
    assert cfg["keywords"] == ["alpha", "beta"]
    assert subs.scene_settings(spec, {"subtitles": False}) is None
    off = validate({"scenes": [SCENE]}, Config())
    assert subs.scene_settings(off, {}) is None
    assert subs.scene_settings(off, {"subtitles": True})["style"] == "karaoke"
    assert off["subtitles"] is False  # the spec itself is not touched


def test_subtitles_may_be_set_in_a_config_and_a_spec_can_turn_them_off():
    cfg = Config()
    cfg.data["subtitles"] = {"style": "pop"}
    assert validate({"scenes": [SCENE]}, cfg)["subtitles"]["style"] == "pop"
    assert validate({"subtitles": False, "scenes": [SCENE]}, cfg)["subtitles"] is False


def test_subtitles_true_in_a_spec_keeps_the_settings_of_a_config():
    cfg = Config()
    cfg.data["subtitles"] = {"style": "pop", "words": 2, "keywords": ["alpha"]}
    got = validate({"subtitles": True, "scenes": [SCENE]}, cfg)["subtitles"]
    assert got["style"] == "pop" and got["words"] == 2 and got["keywords"] == ["alpha"]
    mixed = validate({"subtitles": {"words": 5}, "scenes": [SCENE]}, cfg)["subtitles"]
    assert mixed["style"] == "pop" and mixed["words"] == 5
    assert validate({"subtitles": True, "scenes": [SCENE]}, Config())["subtitles"] == subs.DEFAULT_SUBTITLES


def test_a_decimal_keyword_must_be_quoted_so_it_is_kept_as_written():
    ok = validate({"subtitles": {"keywords": ["3.50", 429]}, "scenes": [{**SCENE, "keywords": ["0.10"]}]}, Config())
    assert ok["subtitles"]["keywords"] == ["3.50", "429"] and ok["scenes"][0]["keywords"] == ["0.10"]
    for where in ({"subtitles": {"keywords": [3.50]}, "scenes": [SCENE]},
                  {"scenes": [{**SCENE, "keywords": [2.0]}]}):
        with pytest.raises(SpecError, match=r"keywords\[0\] must be text; put a decimal number in quotes"):
            validate(where, Config())
    loaded = yaml.safe_load("keywords: [3.50, '3.50']")   # what YAML itself makes of it
    assert loaded["keywords"] == [3.5, "3.50"]


# --- the overlay ----------------------------------------------------------------------------------------

def make_timeline(tmp_path, scene, fmt="landscape", sub_cfg=None, words=None, extra=None, scale=0.5):
    spec = validate({"scenes": [scene], "subtitles": sub_cfg if sub_cfg is not None else True,
                     "music": {"style": "none"}, **(extra or {})}, Config())
    clip = 5.0
    sw = {0: SceneWords(tuple(words or spoken("Hello there. This is a test of the subtitles drawn on the frame", 0.4)),
                        clip, "file")}
    return Timeline(spec, fmt, tmp_path, scale=scale, words=sw), Timeline(spec, fmt, tmp_path, scale=scale)


BODY = {"type": "title", "title": "T", "seconds": 8, "narration": "x"}


@pytest.mark.parametrize("style", ["karaoke", "pop", "plain"])
def test_subtitles_draw_on_top_of_the_scene_and_only_while_a_cue_is_active(tmp_path, style):
    with_s, without = make_timeline(tmp_path, BODY, sub_cfg={"style": style})
    t = NARRATION_LEAD + 0.5
    assert not same(with_s.frame_at(t), without.frame_at(t))
    assert same(with_s.frame_at(7.5), without.frame_at(7.5))  # after the last cue
    assert same(with_s.frame_at(0.1), without.frame_at(0.1))  # before the first


def test_the_active_word_changes_the_frame_in_karaoke_and_pop_but_not_in_plain(tmp_path):
    for style, changes in (("karaoke", True), ("pop", True), ("plain", False)):
        tl, _ = make_timeline(tmp_path, BODY, sub_cfg={"style": style, "words": 4})
        box = tl._subs[0].bbox(0)   # the scene behind it is animating, so compare the subtitle area only
        a = tl.frame_at(NARRATION_LEAD + 0.1).crop(box)   # first word active
        b = tl.frame_at(NARRATION_LEAD + 0.5).crop(box)   # second word active, same chunk
        assert (not same(a, b)) == changes, style


def test_keywords_are_drawn_in_accent2(tmp_path):
    words = spoken("plain token bucket plain", 0.4)
    tl, _ = make_timeline(tmp_path, BODY, sub_cfg={"style": "plain", "keywords": ["token"], "numbers": False}, words=words)
    img = tl.frame_at(NARRATION_LEAD + 0.1).convert("RGB")
    accent2 = tl.ctx.theme.accent2
    track = tl._subs[0]
    x0, y0, x1, y1 = track.bbox(0)
    pixels = [img.getpixel((x, y)) for x in range(x0, x1) for y in range(y0, y1)]
    assert accent2 in pixels and tl.ctx.theme.fg in pixels


def test_uppercase_and_size_change_what_is_drawn(tmp_path):
    base, _ = make_timeline(tmp_path, BODY, sub_cfg={"style": "plain"})
    up, _ = make_timeline(tmp_path, BODY, sub_cfg={"style": "plain", "uppercase": True})
    big, _ = make_timeline(tmp_path, BODY, sub_cfg={"style": "plain", "size": 1.5})
    t = NARRATION_LEAD + 0.1
    assert not same(base.frame_at(t), up.frame_at(t))
    bb, bg = base._subs[0].bbox(0), big._subs[0].bbox(0)
    assert (bg[3] - bg[1]) > (bb[3] - bb[1]) and (bg[2] - bg[0]) > (bb[2] - bb[0])


def test_the_middle_position_is_centred_vertically(tmp_path):
    tl, _ = make_timeline(tmp_path, BODY, sub_cfg={"position": "middle"})
    x0, y0, x1, y1 = tl._subs[0].bbox(0)
    assert abs((y0 + y1) / 2 - tl.ctx.H / 2) < tl.ctx.H * 0.03
    low, _ = make_timeline(tmp_path, BODY)
    assert low._subs[0].bbox(0)[1] > y1


def test_a_scene_can_opt_out(tmp_path):
    tl, _ = make_timeline(tmp_path, {**BODY, "subtitles": False})
    assert tl._subs == {}


def test_a_scene_can_opt_in_when_the_top_level_is_off(tmp_path):
    tl, _ = make_timeline(tmp_path, {**BODY, "subtitles": True}, sub_cfg=False)
    assert 0 in tl._subs


def test_words_that_cannot_fit_two_lines_are_reported(tmp_path):
    words = [Word("supercalifragilisticexpialidocious" + "x" * k, 0.4 * k, 0.4 * k + 0.3) for k in range(8)]
    tl, _ = make_timeline(tmp_path, BODY, sub_cfg={"words": 8, "size": 1.6}, words=words, fmt="vertical")
    tl.frame_at(NARRATION_LEAD + 0.1)
    assert any("subtitle words are too wide" in w for w in tl.warnings)


# Content never sits under the subtitles: for each scene type, in the formats that matter, the lowest pixel the
# scene draws (once it is fully shown) is above the top of the subtitle block.
CONTENT_SCENES = [
    {"type": "title", "eyebrow": "Eyebrow", "title": "A fairly long title for the frame", "subtitle": "and a subtitle line"},
    {"type": "bullets", "title": "Steps", "items": ["first step of the plan", "second step of the plan",
                                                    "third step of the plan", "fourth step of the plan"]},
    {"type": "stat", "value": "10,000,000 rows", "label": "scanned without an index", "eyebrow": "Cost"},
    {"type": "code", "code": "def f(x):\n    y = x + 1\n    z = y * 2\n    return z", "title": "code"},
    {"type": "diagram", "title": "Flow", "nodes": [{"id": "a", "label": "Client", "x": 0.2, "y": 0.3},
                                                    {"id": "b", "label": "Server", "x": 0.8, "y": 0.7}],
     "edges": [{"from": "a", "to": "b"}]},
    {"type": "table", "title": "users", "columns": ["id", "name", "email"],
     "rows": [[str(i), f"user{i}", f"user{i}@example.com"] for i in range(1, 9)],
     "counter": {"label": "rows scanned", "to": 1000000}},
    {"type": "array", "title": "Queue", "items": ["a", "b", "c"], "steps": [{"at": 1, "push": "d"}, {"at": 2, "mark": [0, 1], "text": "hit"},
                                                                       {"at": 3, "window": [0, 2], "text": "window"}]},
    {"type": "type", "text": "Typed text appears here and goes on for a while so it wraps onto a few lines"},
]


@pytest.mark.parametrize("fmt", ["vertical", "landscape", "square"])
@pytest.mark.parametrize("scene", CONTENT_SCENES, ids=lambda s: s["type"])
def test_subtitles_never_collide_with_scene_content(tmp_path, fmt, scene):
    words = spoken("These are words to read aloud on screen while the scene shows its content to the viewer", 0.3)
    tl, without = make_timeline(tmp_path, {**scene, "seconds": 12, "narration": "x"}, fmt=fmt,
                                sub_cfg={"words": 6, "size": 1.3}, words=words)
    t = NARRATION_LEAD + 4.0   # the scene is fully shown, a cue is active
    k = tl._subs[0].cue_at(t)
    assert k is not None
    sx0, sy0, sx1, sy1 = tl._subs[0].bbox(k)
    bg = tl.ctx.background().convert("RGB")
    img = without.frame_at(t).convert("RGB")
    W, H = tl.ctx.W, tl.ctx.H
    diff = ImageChops.difference(img, bg).convert("L").point(lambda v: 255 if v > 12 else 0)
    # leave out the window frame line at the very edge of the picture
    diff = diff.crop((int(W * 0.06), 0, int(W * 0.94), int(H * 0.95)))
    box = diff.getbbox()
    assert box is not None
    assert box[3] <= sy0, f"{scene['type']}/{fmt}: content reaches y={box[3]} but subtitles start at y={sy0}"
    assert sy1 <= H and sx0 >= 0 and sx1 <= W


def test_content_check_would_catch_an_overlap(tmp_path):
    """Control for the check above: with the subtitles moved to the middle they do overlap a full bullets scene."""
    scene = CONTENT_SCENES[1]
    tl, without = make_timeline(tmp_path, {**scene, "seconds": 12, "narration": "x"}, sub_cfg={"position": "middle"})
    sy0 = tl._subs[0].bbox(0)[1]
    diff = ImageChops.difference(without.frame_at(5.0).convert("RGB"), tl.ctx.background().convert("RGB")).convert("L")
    assert diff.point(lambda v: 255 if v > 12 else 0).crop((int(tl.ctx.W * 0.06), 0, int(tl.ctx.W * 0.94),
                                                              int(tl.ctx.H * 0.95))).getbbox()[3] > sy0


# --- the caption comes after the speech -----------------------------------------------------------------

CAPTIONED = {"type": "title", "title": "T", "seconds": 8, "narration": "x", "caption": "The takeaway"}


def test_a_caption_waits_for_the_end_of_the_speech_when_subtitles_are_on(tmp_path):
    tl, plain = make_timeline(tmp_path, CAPTIONED, sub_cfg={"style": "plain"})
    speech_end = NARRATION_LEAD + 5.0
    cap = tl._cap_from[0]
    just_before = tl._scene_frame(0, cap - 0.01)
    after = tl._scene_frame(0, cap + 0.5)
    pill, _ = tl._caps[0]
    cx, cy = int(tl.ctx.W / 2), int(tl.ctx.caption_y)
    bg_px = tl.ctx.background().getpixel((cx, cy))
    top = int(cy - pill.height * 0.45)
    assert plain._scene_frame(0, 1.0).getpixel((cx, top)) != bg_px  # without subtitles the pill is up at 1 s
    assert tl._scene_frame(0, 1.0).getpixel((cx, top)) == bg_px  # with them it is not
    assert after.getpixel((cx, int(cy - pill.height * 0.45))) != bg_px
    assert just_before.getpixel((cx, int(cy - pill.height * 0.45))) == bg_px
    assert cap >= speech_end + subs.CAPTION_GAP
    assert any("scene 0: the caption appears after the narration ends" in w for w in tl.warnings)


def test_the_caption_comes_after_the_last_cue_as_well_as_the_speech(tmp_path):
    late = [Word("Hello", 0.2, 0.5), Word("there", 5.0, 5.2)]    # the last word ends 0.2 s after the 5 s clip
    tl, _ = make_timeline(tmp_path, CAPTIONED, sub_cfg={"style": "plain"}, words=late)
    last_end = tl._subs[0].cues[-1].end
    assert last_end == pytest.approx(NARRATION_LEAD + 5.2 + subs.HOLD_AFTER_LAST)
    assert tl._cap_from[0] == pytest.approx(last_end + subs.CAPTION_GAP)   # not speech end + 0.2 s
    assert tl._cap_from[0] > NARRATION_LEAD + 5.0 + subs.CAPTION_GAP
    early = [Word("Hi", 0.0, 0.2)]                                          # a cue that is gone long before the speech ends
    tl2, _ = make_timeline(tmp_path, CAPTIONED, sub_cfg={"style": "plain"}, words=early)
    assert tl2._cap_from[0] == pytest.approx(NARRATION_LEAD + 5.0 + subs.CAPTION_GAP)
    sw = SceneWords(tuple(early), 5.0, "file")
    assert subs.caption_start(sw) == pytest.approx(NARRATION_LEAD + 5.0 + subs.CAPTION_GAP)


def test_a_scene_without_subtitles_keeps_its_caption_timing(tmp_path):
    tl, _ = make_timeline(tmp_path, {**CAPTIONED, "subtitles": False})
    assert tl._cap_from == {} and not any("caption appears after" in w for w in tl.warnings)


def test_captioned_subtitled_scenes_are_made_long_enough_to_show_the_caption():
    spec = validate({"scenes": [CAPTIONED, {k: v for k, v in CAPTIONED.items() if k != "caption"}],
                     "subtitles": True}, Config())
    words = {i: SceneWords((Word("a", 0, 1),), 1.0, "estimate") for i in (0, 1)}
    floors = {0: 2.0, 1: 2.0}
    out = subs.raise_floors(spec, words, floors)
    assert out[0] == pytest.approx(2.0 + subs.CAPTION_GAP + subs.CAPTION_HOLD) and out[1] == 2.0
    assert floors == {0: 2.0, 1: 2.0}  # the input is not changed


# --- end to end -----------------------------------------------------------------------------------------

pytestmark_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="no ffmpeg")


@pytestmark_ffmpeg
def test_a_render_writes_the_srt_next_to_the_video(tmp_path):
    srv, calls, url = _fake_voice_server(seconds=2.0)
    spec = validate({"scenes": [{"type": "title", "title": "One", "seconds": 3, "narration": "First scene. It has two sentences."},
                                {"type": "title", "title": "Two", "seconds": 3, "narration": "Second scene is here",
                                 "subtitles": False},
                                {"type": "title", "title": "Three", "seconds": 3, "narration": "Third scene, same again."}],
                     "subtitles": {"srt": True, "words": 3}, "music": {"style": "none"}}, Config())
    try:
        res = render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=True,
                     runtime=Runtime(roots=[tmp_path], voice=voice_mod.VoiceServer(url=url)))
    finally:
        srv.shutdown()
    assert res["srt"] == tmp_path / "o" / "square" / "subtitles.srt" and res["srt"].exists()
    blocks = res["srt"].read_text(encoding="utf-8").strip().split("\n\n")
    texts = [b.split("\n")[2:] for b in blocks]
    flat = " ".join(" ".join(t) for t in texts)
    assert "First scene." in flat and "Third scene," in flat and "Second" not in flat  # scene 1 opted out
    starts = []
    for b in blocks:
        m = re.match(r"\d+\n(\d\d):(\d\d):(\d\d),(\d{3}) --> ", b)
        starts.append(int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000)
    assert starts == sorted(starts) and starts[0] >= NARRATION_LEAD - 0.01
    assert starts[-1] > 6.0  # the third scene starts at 6 s or later
    assert any("estimated from the audio" in w for w in res["warnings"])


@pytestmark_ffmpeg
def test_a_stale_srt_from_an_earlier_run_is_removed_when_none_is_written(tmp_path):
    srv, _, url = _fake_voice_server(seconds=1.0)
    scene = {"type": "title", "title": "One", "seconds": 3, "narration": "Hello world"}
    out = tmp_path / "o"
    rt = Runtime(roots=[tmp_path], voice=voice_mod.VoiceServer(url=url))
    try:
        first = render(validate({"scenes": [scene], "subtitles": {"srt": True}, "music": {"style": "none"}}, Config()),
                       "square", out, fps=5, scale=0.1, contact=False, runtime=rt)
        assert first["srt"].exists()
        for sub in (True, False):   # srt not asked for / no subtitles at all
            first["srt"].write_text("old", encoding="utf-8")
            res = render(validate({"scenes": [scene], "subtitles": sub, "music": {"style": "none"}}, Config()),
                         "square", out, fps=5, scale=0.1, contact=False, runtime=rt)
            assert res["srt"] is None and not (out / "square" / "subtitles.srt").exists()
    finally:
        srv.shutdown()


@pytestmark_ffmpeg
def test_no_srt_unless_asked(tmp_path):
    srv, _, url = _fake_voice_server(seconds=1.0)
    spec = validate({"scenes": [{"type": "title", "title": "One", "seconds": 3, "narration": "Hello world"}],
                     "subtitles": True, "music": {"style": "none"}}, Config())
    try:
        res = render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
                     runtime=Runtime(roots=[tmp_path], voice=voice_mod.VoiceServer(url=url)))
    finally:
        srv.shutdown()
    assert res["srt"] is None and not (tmp_path / "o" / "square" / "subtitles.srt").exists()


@pytestmark_ffmpeg
def test_a_recording_with_a_transcript_and_word_file_renders_through_the_cli(tmp_path, capsys):
    write_wav(tmp_path / "take.wav", bursts(0.5, 0.5))
    (tmp_path / "take.words.json").write_text(json.dumps(
        [{"word": "Exact", "start": 0.1, "end": 0.5}, {"word": "timing", "start": 0.7, "end": 1.1}]))
    spec = tmp_path / "s.yaml"
    spec.write_text(yaml.safe_dump({
        "music": {"style": "none"}, "subtitles": {"srt": True, "style": "pop", "words": 2},
        "scenes": [{"type": "title", "title": "T", "seconds": 3, "narration_audio": "take.wav", "narration": "Exact timing",
                    "narration_words": "take.words.json"}]}))
    out = tmp_path / "o"
    assert main(["render", str(spec), "--out", str(out), "--format", "square", "--preview"]) == 0
    srt = (out / "square" / "subtitles.srt").read_text(encoding="utf-8")
    assert "Exact timing" in srt and "00:00:00,400 -->" in srt  # word start 0.1 + the 0.3 s lead
    assert "estimated" not in capsys.readouterr().err


@pytestmark_ffmpeg
def test_a_word_file_outside_the_allowed_folders_stops_the_render(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "w.json").write_text(json.dumps(GOOD))
    write_wav(tmp_path / "take.wav", bursts(1.0))
    spec = validate({"scenes": [{"type": "title", "title": "T", "seconds": 3, "narration_audio": str(tmp_path / "take.wav"),
                                 "narration": "Hello world", "narration_words": str(other / "w.json")}],
                     "subtitles": True, "music": {"style": "none"}}, Config())
    spec["_dir"] = str(tmp_path)
    spec["scenes"][0]["narration_words"] = "../other/w.json"
    with pytest.raises(RenderError, match="subtitles:"):
        render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False, runtime=Runtime(roots=[tmp_path]))


def test_validate_reports_narrated_and_subtitled_counts(tmp_path, capsys):
    spec = tmp_path / "s.yaml"
    spec.write_text(yaml.safe_dump({"subtitles": True, "scenes": [
        {"type": "title", "title": "A", "seconds": 3, "narration": "Hello"},
        {"type": "title", "title": "B", "seconds": 3, "narration": "Hello", "subtitles": False},
        {"type": "title", "title": "C", "seconds": 3},
        {"type": "title", "title": "D", "seconds": 3, "narration_audio": "x.wav"}]}))
    assert main(["validate", str(spec)]) == 0
    assert "narrated=3, subtitled=1" in capsys.readouterr().out


def test_without_subtitles_the_timeline_is_unchanged(tmp_path):
    spec = validate({"scenes": [CAPTIONED], "music": {"style": "none"}}, Config())
    a = Timeline(spec, "landscape", tmp_path, scale=0.25)
    b = Timeline(spec, "landscape", tmp_path, scale=0.25, words={})
    assert a._subs == {} and a._cap_from == {}
    for t in (0.1, 1.0, 5.0):
        assert same(a.frame_at(t), b.frame_at(t))
