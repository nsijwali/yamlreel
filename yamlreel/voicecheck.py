"""`yamlreel check-voice`: transcribe the cached narration clips and compare them with the narration text.

It catches what a text-to-speech model gets wrong without anyone noticing: a dropped "n't", a changed number, a
skipped clause. It reads the clips `render` already cached and never synthesises. The speech recogniser is
injected (`Recognise`), so tests use a fake and the real one (faster-whisper) is imported only when asked for.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .voice import (SAMPLE_RATE, VoiceError, VoiceServer, cached_clip_path, narration_text, read_wav,
                    trim_silence)

Recognise = Callable[[Path], str]

DEFAULT_MODEL = "base.en"
MIN_CHECK_SECONDS = 1.0     # Whisper makes up phrases for shorter audio, so shorter clips are not transcribed
MAX_MISSED = 0.20           # a scene warns when more than this share of the narration words was not heard
QUOTE_CHARS = 200
NEGATIONS = ("not", "no", "never", "without")

_UNITS = {w: n for n, w in enumerate(
    "one two three four five six seven eight nine".split(), 1)}
_TEENS = {w: n for n, w in enumerate(
    "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), 10)}
_TENS = {w: n * 10 for n, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_IRREGULAR_NOT = {"can't": "can not", "won't": "will not", "shan't": "shall not"}
_IS_AFTER = "it|that|there|here|what|who|he|she"
_EXPAND = {"re": " are", "ll": " will", "ve": " have", "m": " am", "d": " would"}
_NUMERIC = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class Finding:
    scene: int
    level: str          # "ERROR", "WARNING" or "note"
    message: str
    expected: str = ""
    heard: str = ""

    def line(self) -> str:
        if self.level == "note":
            return f"scene {self.scene} note: {self.message}"
        return (f'scene {self.scene} {self.level} {self.message}: expected "{_clip(self.expected)}" '
                f'heard "{_clip(self.heard)}"')


class Findings(list):
    """The findings, plus how many scenes were narrated and how many of them were actually transcribed."""
    narrated: int = 0
    checked: int = 0


def _clip(text: str) -> str:
    return text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS - 1] + "…"


# --- normalisation ---------------------------------------------------------------------------------------

def _merge_digit_scales(tokens: list[str]) -> list[str]:
    """Merge a numeric token and a following scale word ("1.5 million") into one number ("1500000")."""
    scales = {"hundred": 100, **_SCALES}
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if _NUMERIC.fullmatch(tokens[i]) and i + 1 < len(tokens) and tokens[i + 1] in scales:
            value = round(float(tokens[i]) * scales[tokens[i + 1]], 6)
            out.append(str(int(value)) if value == int(value) else str(value))
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out


def _number_words(tokens: list[str]) -> list[str]:
    """Replace runs of number words ("twenty five", "a hundred") with digits."""
    tokens = _merge_digit_scales(tokens)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        total = current = 0
        last = None          # kind of the last number word taken: unit, teen, tens, hundred or scale
        j = i
        while j < len(tokens):
            w = tokens[j]
            kind = None
            if w == "a" and last is None and j + 1 < len(tokens) and tokens[j + 1] in ("hundred", *_SCALES):
                current, kind = 1, "unit"
            elif w in _UNITS and last in (None, "tens", "hundred", "scale"):
                current, kind = current + _UNITS[w], "unit"
            elif w in _TEENS and last in (None, "hundred", "scale"):
                current, kind = current + _TEENS[w], "teen"
            elif w in _TENS and last in (None, "hundred", "scale"):
                current, kind = current + _TENS[w], "tens"
            elif w == "hundred" and last in ("unit", "teen", "tens"):
                current, kind = current * 100, "hundred"
            elif w in _SCALES and last in ("unit", "teen", "tens", "hundred"):
                total, current, kind = total + current * _SCALES[w], 0, "scale"
            elif w == "zero" and last is None:
                kind = "unit"
            elif w == "and" and last in ("hundred", "scale") and j + 1 < len(tokens) \
                    and (tokens[j + 1] in _UNITS or tokens[j + 1] in _TEENS or tokens[j + 1] in _TENS):
                j += 1
                continue
            if kind is None:
                break
            last = kind
            j += 1
        if last is None:
            out.append(tokens[i])
            i += 1
        else:
            out.append(str(total + current))
            i = j
    return out


def normalise(text: str) -> list[str]:
    """Lowercase words, with contractions expanded, numbers as digits and punctuation removed, so that
    "isn't" matches "is not" and "twenty five" matches "25"."""
    t = text.lower().replace("’", "'")
    for short, full in _IRREGULAR_NOT.items():
        t = t.replace(short, full)
    t = t.replace("cannot", "can not")
    t = re.sub(r"(\w)n't\b", r"\1 not", t)
    t = re.sub(rf"\b({_IS_AFTER})'s\b", r"\1 is", t)
    t = re.sub(r"(?<=[a-z])'(re|ll|ve|m|d)\b", lambda m: _EXPAND[m.group(1)], t)
    t = t.replace("'", "")                       # quote marks and the 's left over: "queue's" -> "queues"
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    t = t.replace("%", " percent ")
    t = re.sub(r"(\d+(?:\.\d+)?)k\b", lambda m: f"{float(m.group(1)) * 1000:g}", t)
    t = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", t)
    t = re.sub(r"[^a-z0-9. ]", " ", t)           # hyphens and other punctuation
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)    # keep decimal points only
    return _number_words(t.split())


def _numbers(words: list[str]) -> list[float]:
    return sorted(float(w) for w in words if re.fullmatch(r"\d+(?:\.\d+)?", w))


def _fmt(values: list[float]) -> str:
    return ", ".join(str(int(v)) if v == int(v) else str(v) for v in values) or "none"


def compare(expected: str, heard: str) -> list[tuple[str, str]]:
    """[(level, message)] for one scene, in rule order: negations, numbers, overlap. Empty when it is clean."""
    exp, got = normalise(expected), normalise(heard)
    found: list[tuple[str, str]] = []
    ce, cg = Counter(exp), Counter(got)
    missing = [w for w in NEGATIONS if cg[w] < ce[w]]
    added = [w for w in NEGATIONS if cg[w] > ce[w]]   # both flip the meaning
    if missing or added:
        parts = ([f"negation missing ({', '.join(missing)})"] if missing else []) \
            + ([f"negation added ({', '.join(added)})"] if added else [])
        found.append(("ERROR", "; ".join(parts)))
    ne, ng = _numbers(exp), _numbers(got)
    if ne != ng:
        found.append(("ERROR", f"numbers differ (expected {_fmt(ne)}; heard {_fmt(ng)})"))
    if exp:
        matched = sum(b.size for b in difflib.SequenceMatcher(None, exp, got, autojunk=False).get_matching_blocks())
        missed = 1 - matched / len(exp)
        if round(missed, 6) > MAX_MISSED:
            found.append(("WARNING", f"{round(missed * 100)}% of the narration words not heard "
                                     f"(limit {round(MAX_MISSED * 100)}%)"))
    return found


# --- checking a spec -------------------------------------------------------------------------------------

def check_spec(spec: dict[str, Any], server: VoiceServer | None, cache: Path, resolve_audio: Callable[[str], Path],
               recognise: Recognise) -> Findings:
    """Compare every narrated scene's cached clip with its narration text. Read-only: nothing is synthesised and
    no folder is created. Setup problems raise VoiceError; problems with a scene are findings."""
    vo = spec.get("voiceover") or {}
    language = vo.get("language")
    if language and not str(language).lower().startswith("en"):
        raise VoiceError("check-voice checks English narration only")
    todo: list[tuple[int, str, str | None]] = []   # (scene, kind, text): kind is "audio" or "synth"
    for i, sc in enumerate(spec["scenes"]):
        if sc.get("narration_audio"):
            if sc.get("narration") is not False:
                todo.append((i, "audio", narration_text(sc, vo)))
        elif (text := narration_text(sc, vo)) is not None:
            todo.append((i, "synth", text))
    if server is None and any(kind == "synth" for _, kind, _ in todo):
        raise VoiceError("check-voice needs the voice_server.url (or --voice-url) used to render, "
                         "to find cached clips")
    out = Findings()
    out.narrated = len(todo)
    for i, kind, text in todo:
        if text is None:
            out.append(Finding(i, "note", "no transcript to compare"))
            continue
        if kind == "audio":
            path = resolve_audio(spec["scenes"][i]["narration_audio"])
        else:
            assert server is not None
            path = cached_clip_path(cache, server, text, vo)
            if not path.exists():
                out.append(Finding(i, "note", "not cached, run render first"))
                continue
        try:
            speech = len(trim_silence(read_wav(path))) / SAMPLE_RATE
        except (VoiceError, OSError) as e:
            out.append(Finding(i, "ERROR", f"cannot read the clip ({e})"))
            continue
        if speech < MIN_CHECK_SECONDS:
            out.append(Finding(i, "note", f"{speech:.1f} s of speech is too short to check reliably "
                                          f"(minimum {MIN_CHECK_SECONDS:.1f} s); listen to it"))
            continue
        heard = recognise(path)
        out.checked += 1
        out.extend(Finding(i, level, msg, text, heard) for level, msg in compare(text, heard))
    return out


def load_recogniser(model: str) -> Recognise:
    """A recogniser backed by faster-whisper (CPU, int8). The package is imported now, so a missing install is
    reported before any other work; the model itself is built, and downloaded if needed, on the first call."""
    try:
        import faster_whisper
    except ImportError as e:
        raise VoiceError('check-voice needs faster-whisper: pip install "yamlreel[verify]"') from e
    loaded: Any = None

    def recognise(path: Path) -> str:
        nonlocal loaded
        try:
            if loaded is None:
                loaded = faster_whisper.WhisperModel(model, device="cpu", compute_type="int8")  # verify against faster-whisper 1.x
            # No initial_prompt: the expected text would bias the result towards what we want to check.
            segments, _info = loaded.transcribe(  # verify against faster-whisper 1.x
                str(path), language="en", beam_size=5, temperature=0.0,  # verify against faster-whisper 1.x
                condition_on_previous_text=False, vad_filter=False)  # verify against faster-whisper 1.x
            return " ".join(s.text.strip() for s in segments).strip()  # segment.text: verify against faster-whisper 1.x
        except Exception as e:  # the model may fail to download, load or decode in many ways
            raise VoiceError(f"cannot transcribe with the speech model {model!r}: {e}") from e

    return recognise
