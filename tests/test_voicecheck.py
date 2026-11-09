"""`yamlreel check-voice`: compare cached narration clips with the narration text. No model, no download."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml

from fakes import _tone_wav_bytes
from yamlreel import voice as voice_mod
from yamlreel import voicecheck
from yamlreel.cli import main
from yamlreel.config import Config
from yamlreel.specs import validate
from yamlreel.voice import VoiceServer, cached_clip_path, narration_cache_key

URL = "http://127.0.0.1:9/mcp/"     # never contacted: check-voice does not synthesise
QUEUE = "But a queue isn't extra capacity."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def fake_recogniser(monkeypatch, heard: dict | str = "", seen: list | None = None):
    """Make `load_recogniser` return a recogniser that answers from `heard` ({clip name: text} or one text)."""
    def recognise(path: Path) -> str:
        if seen is not None:
            seen.append(path)
        return heard if isinstance(heard, str) else heard[path.name]
    monkeypatch.setattr(voicecheck, "load_recogniser", lambda model: recognise)


def write_spec(tmp_path, scenes, **top) -> Path:
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump({"scenes": scenes, "music": {"style": "none"}, **top}), encoding="utf-8")
    return p


def scene(narration, **extra):
    return {"type": "title", "title": "T", "seconds": 3, "narration": narration, **extra}


def cache_clip(tmp_path, text, seconds=1.5, vo=None) -> Path:
    cache = tmp_path / "out" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cached_clip_path(cache, VoiceServer(url=URL), text, vo or {})
    path.write_bytes(_tone_wav_bytes(seconds))
    return path


def run(tmp_path, spec, *extra) -> int:
    return main(["check-voice", str(spec), "--out", str(tmp_path / "out"), "--voice-url", URL, *extra])


# --- normalisation and comparison -----------------------------------------------------------------------

def test_normalise_matches_contractions_numbers_and_punctuation():
    n = voicecheck.normalise
    assert n("It isn't") == n("it is not") == ["it", "is", "not"]
    assert n("We can't, won't or shan't; cannot.") == ["we", "can", "not", "will", "not", "or", "shall", "not", "can", "not"]
    assert n("That’s it. Queue's full.") == ["that", "is", "it", "queues", "full"]
    assert n("They're sure, we'll go, I've, I'm, he'd") == ["they", "are", "sure", "we", "will", "go", "i", "have", "i", "am", "he", "would"]
    assert n("1,000 requests") == n("one thousand requests") == ["1000", "requests"]
    assert n("twenty-five") == n("twenty five") == n("25") == ["25"]
    assert n("a hundred and five") == ["105"]
    assert n("two thousand twenty five") == ["2025"]
    assert n("50%") == n("fifty percent") == ["50", "percent"]
    assert n("10k users") == ["10000", "users"]
    assert n("3x faster, 2.5 times") == ["3", "x", "faster", "2.5", "times"]
    assert n("read-only") == ["read", "only"]
    assert n("one two") == ["1", "2"]    # separate numbers are not merged


def test_digit_scale_words_normalise_equal():
    n = voicecheck.normalise
    assert n("5 million") == n("five million") == n("5,000,000") == ["5000000"]
    assert n("1.5 million") == n("1,500,000") == ["1500000"]
    assert n("3 thousand") == n("three thousand") == ["3000"]
    assert n("2 billion") == n("two billion") == ["2000000000"]


def test_scale_word_difference_is_still_an_error():
    found = voicecheck.compare("We served five million users", "We served six million users")
    assert "ERROR" in [level for level, _ in found]
    assert voicecheck.compare("We served five million users", "We served 5 million users") == []


def test_quoted_words_are_not_read_as_contractions():
    n = voicecheck.normalise
    assert n("The 'data' layer") == ["the", "data", "layer"]
    assert n("Say 'mutex' twice.") == ["say", "mutex", "twice"]
    assert voicecheck.compare("The 'data' layer", "The data layer") == []
    assert voicecheck.compare("Say 'mutex' twice.", "Say mutex twice.") == []
    assert n("isn't") == n("is not") and n("we'll") == n("we will")
    assert "ERROR" in [lv for lv, _ in voicecheck.compare("It isn't ready", "It is ready")]


def test_dropped_negation_is_an_error():
    found = voicecheck.compare(QUEUE, "But a queue is extra capacity.")
    assert found[0] == ("ERROR", "negation missing (not)")
    assert voicecheck.compare(QUEUE, "But a queue is not extra capacity.") == []


def test_added_negation_is_an_error():
    found = voicecheck.compare("A queue is extra capacity.", "A queue is not extra capacity.")
    assert ("ERROR", "negation added (not)") in found


def test_changed_number_is_an_error_but_three_equals_3():
    assert voicecheck.compare("We keep 3 replicas.", "We keep three replicas.") == []
    assert voicecheck.compare("Send 50 requests.", "Send fifty requests.") == []
    found = voicecheck.compare("Keep 3 of 50 replicas.", "Keep 3 of 15 replicas.")
    assert found == [("ERROR", "numbers differ (expected 3, 50; heard 3, 15)")]


def test_overlap_threshold():
    assert voicecheck.compare("red blue green pink gold", "red blue green pink") == []   # 4/5 heard: clean
    found = voicecheck.compare("alpha beta gamma delta epsilon", "alpha beta gamma")     # 3/5 heard
    assert found == [("WARNING", "40% of the narration words not heard (limit 20%)")]
    assert voicecheck.compare("anything at all", "")[0][0] == "WARNING"


# --- reading the cache ------------------------------------------------------------------------------------

def test_reads_the_raw_clip_by_render_key_without_the_server(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("check-voice must not synthesise or change speed")
    monkeypatch.setattr(voice_mod, "_Session", boom)
    monkeypatch.setattr(voice_mod, "change_speed", boom)
    vo = {"trim": True, "speed": 1.25}
    clip = cache_clip(tmp_path, QUEUE, vo=vo)
    seen: list[Path] = []
    fake_recogniser(monkeypatch, QUEUE, seen)
    spec = write_spec(tmp_path, [scene(QUEUE)], voiceover=vo)
    assert run(tmp_path, spec) == 0
    key = narration_cache_key(VoiceServer(url=URL), QUEUE, None, None)
    assert seen == [clip] and clip.name == f"narration-v2-{key}.wav"


def test_uncached_scene_is_a_note_and_nothing_is_written(tmp_path, monkeypatch, capsys):
    fake_recogniser(monkeypatch, "never called")
    spec = write_spec(tmp_path, [scene(QUEUE)])
    assert run(tmp_path, spec) == 0
    out = capsys.readouterr().out
    assert "scene 0 note: not cached, run render first" in out
    assert "checked 0 of 1 narrated scenes: 0 errors, 0 warnings" in out
    assert not (tmp_path / "out").exists()


def test_narration_audio_uses_its_transcript_and_no_transcript_is_a_note(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.wav").write_bytes(_tone_wav_bytes(1.5))
    (tmp_path / "b.wav").write_bytes(_tone_wav_bytes(1.5))
    seen: list[Path] = []
    fake_recogniser(monkeypatch, "We keep three replicas.", seen)
    spec = write_spec(tmp_path, [
        {"type": "title", "title": "A", "seconds": 3, "narration_audio": "a.wav", "narration": "We keep 3 replicas."},
        {"type": "title", "title": "B", "seconds": 3, "narration_audio": "b.wav"}])
    assert run(tmp_path, spec) == 0
    out = capsys.readouterr().out
    assert [p.name for p in seen] == ["a.wav"]
    assert "scene 1 note: no transcript to compare" in out
    assert "checked 1 of 2 narrated scenes" in out


def test_short_clip_is_a_note_and_not_transcribed(tmp_path, monkeypatch, capsys):
    cache_clip(tmp_path, QUEUE, seconds=0.5)
    seen: list[Path] = []
    fake_recogniser(monkeypatch, "Thank you.", seen)
    assert run(tmp_path, write_spec(tmp_path, [scene(QUEUE)])) == 0
    assert seen == []
    assert "too short to check reliably (minimum 1.0 s); listen to it" in capsys.readouterr().out


def test_unreadable_clip_is_an_error_on_that_scene(tmp_path, monkeypatch, capsys):
    cache_clip(tmp_path, QUEUE).write_bytes(b"not a wav")
    fake_recogniser(monkeypatch, "x")
    assert run(tmp_path, write_spec(tmp_path, [scene(QUEUE)])) == 1
    assert "scene 0 ERROR cannot read the clip" in capsys.readouterr().out


def test_silent_scenes_and_narration_words_are_ignored(tmp_path, monkeypatch, capsys):
    fake_recogniser(monkeypatch, "never called")
    (tmp_path / "words.json").write_text("[]", encoding="utf-8")
    spec = write_spec(tmp_path, [
        {"type": "title", "title": "silent", "seconds": 3},
        {"type": "title", "title": "off", "seconds": 3, "narration": False},
        {"type": "title", "title": "words", "seconds": 3, "narration_words": "words.json"}])
    assert run(tmp_path, spec) == 0
    assert capsys.readouterr().out == "checked 0 of 0 narrated scenes: 0 errors, 0 warnings\n"


def test_read_screen_flag_finds_the_same_clip_as_render(tmp_path, monkeypatch):
    cfg_spec = validate({"scenes": [{"type": "title", "title": "Cache", "seconds": 3}], "music": {"style": "none"},
                         "voiceover": {"read_screen": True}}, Config())
    text = voice_mod.narration_text(cfg_spec["scenes"][0], cfg_spec["voiceover"])
    clip = cache_clip(tmp_path, text)
    seen: list[Path] = []
    fake_recogniser(monkeypatch, text, seen)
    assert run(tmp_path, write_spec(tmp_path, [{"type": "title", "title": "Cache", "seconds": 3}]), "--read-screen") == 0
    assert seen == [clip]


# --- exit codes -------------------------------------------------------------------------------------------

def test_exit_codes(tmp_path, monkeypatch, capsys):
    cache_clip(tmp_path, QUEUE)
    spec = write_spec(tmp_path, [scene(QUEUE)])
    fake_recogniser(monkeypatch, QUEUE)
    assert run(tmp_path, spec) == 0
    fake_recogniser(monkeypatch, "But a queue is extra capacity.")
    assert run(tmp_path, spec) == 1
    out = capsys.readouterr().out
    assert f'scene 0 ERROR negation missing (not): expected "{QUEUE}" heard "But a queue is extra capacity."' in out
    assert "checked 1 of 1 narrated scenes: 1 error, 0 warnings" in out
    long = "alpha beta gamma delta epsilon zeta"
    cache_clip(tmp_path, long)
    fake_recogniser(monkeypatch, "alpha beta gamma")
    assert run(tmp_path, write_spec(tmp_path, [scene(long)])) == 1
    assert "WARNING" in capsys.readouterr().out


def test_missing_faster_whisper_is_exit_2_with_one_stderr_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    assert run(tmp_path, write_spec(tmp_path, [scene(QUEUE)])) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err == 'error: check-voice needs faster-whisper: pip install "yamlreel[verify]"\n'


def test_setup_problems_are_exit_2(tmp_path, monkeypatch, capsys):
    fake_recogniser(monkeypatch, "x")
    spec = write_spec(tmp_path, [scene(QUEUE)])
    assert main(["check-voice", str(spec), "--out", str(tmp_path / "out")]) == 2     # no voice server
    assert "needs the voice_server.url (or --voice-url)" in capsys.readouterr().err
    assert run(tmp_path, write_spec(tmp_path, [scene(QUEUE)], voiceover={"language": "ja"})) == 2
    assert "English narration only" in capsys.readouterr().err
    assert run(tmp_path, write_spec(tmp_path, [{"type": "nope"}])) == 2
    assert capsys.readouterr().err.startswith("error:")
    assert main(["check-voice", str(spec), "--voice-url", URL]) == 2    # output_dir outside the spec folder
    monkeypatch.setattr(voicecheck, "load_recogniser", lambda m: (_ for _ in ()).throw(voice_mod.VoiceError("boom")))
    assert run(tmp_path, spec) == 2


def test_a_model_that_fails_is_exit_2(tmp_path, monkeypatch, capsys):
    cache_clip(tmp_path, QUEUE)
    def recognise(path):
        raise voice_mod.VoiceError("model failed")
    monkeypatch.setattr(voicecheck, "load_recogniser", lambda m: recognise)
    assert run(tmp_path, write_spec(tmp_path, [scene(QUEUE)])) == 2
    assert capsys.readouterr().out == ""


def test_load_recogniser_is_lazy_and_uses_cpu_int8_english(tmp_path, monkeypatch):
    log: dict = {}

    class Seg:
        def __init__(self, text): self.text = text

    class WhisperModel:
        def __init__(self, name, **kw):
            log["init"] = (name, kw)
        def transcribe(self, path, **kw):
            log["transcribe"] = (path, kw)
            return iter([Seg(" Hello "), Seg("world")]), object()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    recognise = voicecheck.load_recogniser("base.en")
    assert log == {}                                      # nothing built until the first clip
    assert recognise(tmp_path / "c.wav") == "Hello world"
    assert log["init"] == ("base.en", {"device": "cpu", "compute_type": "int8"})
    assert log["transcribe"] == (str(tmp_path / "c.wav"), {
        "language": "en", "beam_size": 5, "temperature": 0.0, "condition_on_previous_text": False, "vad_filter": False})
    assert "initial_prompt" not in log["transcribe"][1]
