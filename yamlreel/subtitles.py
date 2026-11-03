"""Word-by-word subtitles, timed from the narration audio.

Two ways to know when each word is spoken:

1. `narration_words`: a JSON file of `{"word", "start", "end"}` entries (seconds from the start of the clip) made
   by any aligner outside yamlreel. Exact, and confined to the allowed folders like `narration_audio`.
2. `estimate`: a pure-numpy guess from the audio itself. Speech is found by its loudness, silent gaps mark where
   sentences and clauses end, and the time inside each stretch is shared out by how long each word is to say.
   It is typically within about 150 ms per word, which showing 3 to 5 words at a time with the active one
   highlighted hides well.

The text comes from `voice.narration_text`, the same string the voice speaks. Nothing here needs more than numpy
and Pillow. Times in a `Cue` are scene-local (they include the narration lead), so the timeline can draw a cue
without knowing where the scene starts.
"""
from __future__ import annotations

import bisect
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw

from . import voice
from .scenebase import Ctx
from .textkit import layout_words
from .theme import font

MAX_WORDS_ENTRIES = 2000
MAX_WORDS_BYTES = 256 * 1024
MAX_KEYWORDS = 50
MAX_KEYWORD_CHARS = 60
WORD_FILE_SLACK = 0.2         # a word may end this long after the clip does
STYLES = ("karaoke", "pop", "plain")
POSITIONS = ("lower", "middle")
DEFAULT_SUBTITLES: dict[str, Any] = {"style": "karaoke", "words": 4, "position": "lower", "size": 1.0,
                                     "uppercase": False, "keywords": [], "numbers": True, "srt": False}
CAPTION_GAP = 0.2             # a scene's caption comes this long after its speech ends ...
CAPTION_HOLD = 1.2            # ... and a subtitled scene is made this much longer so the caption can be read

FRAME_SECONDS = 0.02          # loudness is measured over 20 ms frames
MIN_GAP_SECONDS = 0.12        # a silence at least this long can separate sentences and clauses
PEAK_SHARE = 0.05             # a frame is speech when louder than this share of the loudest frame ...
FLOOR_SHARE = 0.10            # ... and than this share of the 90th-percentile frame
DIGIT_WEIGHT = 1.5            # a digit takes this many syllables to say
BREAK_AFTER_SILENCE = 0.6     # a new chunk starts after a silence this long
HOLD_AFTER_LAST = 0.3         # the last chunk before a silence stays this long after its last word
JOIN_WITHIN = 0.5             # chunks closer together than this follow each other without a blank

STRONG_END = ".!?…"
WEAK_END = ",;:–—"
_CLOSERS = "\"')]}»”’"


class SubtitleError(ValueError):
    pass


@dataclass(frozen=True)
class Word:
    text: str
    start: float   # seconds from the start of the clip
    end: float


@dataclass(frozen=True)
class Estimate:
    words: list[Word]
    boundaries: int   # sentence and clause ends in the text
    matched: int      # how many of them were placed in a silence of the audio


@dataclass(frozen=True)
class SceneWords:
    """The words of one narrated scene, relative to the start of its clip."""
    words: tuple[Word, ...]
    clip_seconds: float
    source: str       # "file" or "estimate"


@dataclass(frozen=True)
class Cue:
    """A few words shown together. Times are scene-local seconds."""
    start: float
    end: float
    words: tuple[Word, ...]      # times are scene-local too
    keywords: tuple[bool, ...]

    def text(self, uppercase: bool = False) -> str:
        t = " ".join(w.text for w in self.words)
        return t.upper() if uppercase else t

    def active(self, t: float) -> int:
        """Index of the word being spoken at scene time t: the last one that has started."""
        n = 0
        for k, w in enumerate(self.words):
            if w.start <= t + 1e-9:
                n = k
        return n


# --- estimating word times from audio -------------------------------------------------------------------

def _weight(word: str) -> float:
    """How long a word takes to say, in rough syllables: vowel groups (at least one), and a digit counts more."""
    digits = sum(ch.isdigit() for ch in word)
    letters = re.sub(r"\d+", " ", word)
    groups = len(re.findall(r"[aeiouyAEIOUYÀ-ÿ]+", letters))
    if not groups and any(ch.isalpha() for ch in letters):
        groups = max(1, round(sum(ch.isalpha() for ch in letters) / 3))  # no Latin vowels: about 3 letters a syllable
    return max(1.0, groups + DIGIT_WEIGHT * digits)


def _boundary(word: str) -> int:
    """2 when the word ends a sentence, 1 when it ends a clause, else 0."""
    w = word.rstrip(_CLOSERS)
    if w and w[-1] in STRONG_END:
        return 2
    if w and w[-1] in WEAK_END:
        return 1
    return 0


def _split_text(text: str) -> list[str]:
    words: list[str] = []
    for tok in text.split():
        if words and not any(ch.isalnum() for ch in tok):
            words[-1] += " " + tok  # a lone dash or ellipsis belongs to the word before it
        else:
            words.append(tok)
    return words


def _speech_frames(samples: np.ndarray) -> np.ndarray:
    hop = int(voice.SAMPLE_RATE * FRAME_SECONDS)
    n = len(samples) // hop
    if n == 0:
        return np.zeros(0, dtype=bool)
    frames = samples[: n * hop].astype(np.float64).reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 0.0:
        return np.zeros(n, dtype=bool)
    threshold = max(PEAK_SHARE * peak, FLOOR_SHARE * float(np.percentile(rms, 90)))
    return rms > threshold


def estimate(text: str, samples: np.ndarray) -> Estimate:
    """Guess when each word of `text` is spoken in `samples` (mono, voice.SAMPLE_RATE). Deterministic, O(n)."""
    texts = _split_text(text)
    clip = len(samples) / voice.SAMPLE_RATE
    if not texts:
        return Estimate([], 0, 0)
    active = _speech_frames(samples)
    idx = np.flatnonzero(active)
    if len(idx):
        first, last = int(idx[0]), int(idx[-1])
        t0, t1 = first * FRAME_SECONDS, min(clip, (last + 1) * FRAME_SECONDS)
        gaps: list[tuple[float, float]] = []
        k = first
        while k <= last:
            if active[k]:
                k += 1
                continue
            j = k
            while j <= last and not active[j]:
                j += 1
            if (j - k) * FRAME_SECONDS >= MIN_GAP_SECONDS - 1e-9:
                gaps.append((k * FRAME_SECONDS, j * FRAME_SECONDS))
            k = j
    else:
        t0, t1, gaps = 0.0, clip, []

    levels = [_boundary(w) for w in texts]
    ends = [k for k in range(len(texts) - 1) if levels[k]]   # a boundary follows word k
    ordered = [k for k in ends if levels[k] == 2] + [k for k in ends if levels[k] == 1]  # sentences first
    chosen = sorted(ordered[: len(gaps)])
    biggest = sorted(sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[: len(chosen)])  # in time order
    cuts = list(zip(chosen, biggest))                        # (word before the cut, the silence it falls in)

    weights = [_weight(w) for w in texts]
    words: list[Word] = []
    lo, seg_start = 0, t0
    for k, (g0, g1) in [*cuts, (len(texts) - 1, (t1, t1))]:
        seg_end = g0 if k < len(texts) - 1 else t1
        seg = range(lo, k + 1)
        total = sum(weights[m] for m in seg)
        at = seg_start
        for m in seg:
            span = max(0.0, seg_end - seg_start) * weights[m] / total
            words.append(Word(texts[m], round(at, 4), round(at + span, 4)))
            at += span
        lo, seg_start = k + 1, g1
    return Estimate(words, len(ends), len(chosen))


def estimate_words(text: str, samples: np.ndarray) -> list[Word]:
    return estimate(text, samples).words


# --- exact word times from a file -----------------------------------------------------------------------

def load_word_file(path: Path, clip_seconds: float) -> list[Word]:
    """Read `[{"word": str, "start": s, "end": s}, ...]`. Raises SubtitleError for a file that is too large, not
    JSON, not that shape, not in time order, or with times past the end of the clip."""
    name = path.name
    try:
        size = path.stat().st_size
    except OSError as e:
        raise SubtitleError(f"{name}: cannot read the file ({e.strerror or e})") from e
    if size > MAX_WORDS_BYTES:
        raise SubtitleError(f"{name} is {size} bytes; the limit is {MAX_WORDS_BYTES} (MAX_WORDS_BYTES)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise SubtitleError(f"{name}: not readable JSON ({e})") from e
    if not isinstance(data, list) or not data:
        raise SubtitleError(f"{name} must be a non-empty list of {{word, start, end}} entries")
    if len(data) > MAX_WORDS_ENTRIES:
        raise SubtitleError(f"{name} has {len(data)} entries; the limit is {MAX_WORDS_ENTRIES} (MAX_WORDS_ENTRIES)")
    out: list[Word] = []
    prev_start = prev_end = 0.0
    for n, e in enumerate(data):
        where = f"{name}[{n}]"
        if not isinstance(e, dict) or not isinstance(e.get("word"), str) or not e["word"].strip():
            raise SubtitleError(f"{where} must have a non-empty text 'word'")
        times = []
        for key in ("start", "end"):
            v = e.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise SubtitleError(f"{where}.{key} must be a number of seconds")
            times.append(float(v))
        start, end = times
        if start < 0 or end < start:
            raise SubtitleError(f"{where}: needs 0 <= start <= end, got {start:g} and {end:g}")
        if start < prev_start or end < prev_end:
            raise SubtitleError(f"{where}: times must not go backwards (start {start:g} after {prev_start:g}, "
                                f"end {end:g} after {prev_end:g})")
        if end > clip_seconds + WORD_FILE_SLACK:
            raise SubtitleError(f"{where}: ends at {end:g}s but the clip is only {clip_seconds:.2f}s long")
        out.append(Word(e["word"].strip(), start, end))
        prev_start, prev_end = start, end
    return out


# --- keywords, chunks, cues -----------------------------------------------------------------------------

def _token(word: str) -> str:
    return re.sub(r"^\W+|\W+$", "", word).casefold()


def keyword_flags(texts: list[str], keywords: list[str], numbers: bool) -> list[bool]:
    """Which words are keywords: a whole word or phrase from `keywords` (case-insensitive, punctuation at the
    edges ignored), and, with `numbers`, any word with a digit in it."""
    toks = [_token(w) for w in texts]
    flags = [bool(numbers and any(ch.isdigit() for ch in t)) for t in toks]
    for phrase in keywords:
        parts = [_token(p) for p in phrase.split()]
        parts = [p for p in parts if p]
        if not parts:
            continue
        for s in range(len(toks) - len(parts) + 1):
            if toks[s:s + len(parts)] == parts:
                for m in range(s, s + len(parts)):
                    flags[m] = True
    return flags


def build_cues(words: list[Word], per_chunk: int, keywords: list[str] | None = None, numbers: bool = True,
               lead: float = voice.NARRATION_LEAD, limit: float | None = None) -> list[Cue]:
    """Group words into chunks of at most `per_chunk`, never across a sentence end or a long silence. Times in the
    result are scene-local (`lead` added) and cues never overlap. `limit` (the scene length) caps the last one."""
    flags = keyword_flags([w.text for w in words], keywords or [], numbers)
    groups: list[list[int]] = []
    for k, w in enumerate(words):
        if groups:
            cur = groups[-1]
            prev = words[cur[-1]]
            if (len(cur) < per_chunk and not _boundary(prev.text) == 2 and w.start - prev.end < BREAK_AFTER_SILENCE):
                cur.append(k)
                continue
        groups.append([k])
    cues: list[Cue] = []
    for gi, g in enumerate(groups):
        ws = tuple(Word(words[k].text, words[k].start + lead, words[k].end + lead) for k in g)
        start, last_end = ws[0].start, ws[-1].end
        end = last_end + HOLD_AFTER_LAST
        if gi + 1 < len(groups):
            nxt = words[groups[gi + 1][0]].start + lead
            end = nxt if nxt - last_end <= JOIN_WITHIN else end
            end = min(end, nxt)
        if limit is not None:
            if start >= limit - 0.05:
                break  # the scene ends before this chunk would be seen
            end = min(end, limit)
        cues.append(Cue(start, max(end, start + 0.05), ws, tuple(flags[k] for k in g)))
    return cues


def srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_text(cues: list[Cue], uppercase: bool = False) -> str:
    """SubRip text for cues with absolute times: numbered from 1, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, a blank line
    after each. Cues are in ascending order and none overlaps the next."""
    out: list[str] = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text(uppercase)}\n")
    return "\n".join(out)


# --- which scenes get subtitles, and the words of each --------------------------------------------------

def scene_settings(spec: dict[str, Any], sc: dict[str, Any]) -> dict[str, Any] | None:
    """The subtitle settings that apply to one scene, or None when it has none. `subtitles: false` on the scene
    always wins; `subtitles: true` on a scene turns subtitles on for it even when the top level is off."""
    flag = sc.get("subtitles")
    top = spec.get("subtitles")
    if flag is False or (not top and flag is not True):
        return None
    cfg = dict(top) if top else dict(DEFAULT_SUBTITLES)
    kw = list(cfg.get("keywords") or [])
    kw += [k for k in sc.get("keywords") or [] if k not in kw]
    cfg["keywords"] = kw
    return cfg


def scene_words(spec: dict[str, Any], clips: dict[int, np.ndarray],
                resolve: Callable[[str], Path]) -> tuple[dict[int, SceneWords], list[str]]:
    """Word times for every narrated scene that has subtitles, and warnings. `resolve(path)` confines
    `narration_words` files to the allowed folders (and raises its own error otherwise)."""
    vo = spec.get("voiceover") or {}
    out: dict[int, SceneWords] = {}
    warns: list[str] = []
    estimated: list[int] = []
    for i, samples in clips.items():
        sc = spec["scenes"][i]
        if scene_settings(spec, sc) is None:
            continue
        clip = len(samples) / voice.SAMPLE_RATE
        if sc.get("narration_words"):
            try:
                words = load_word_file(resolve(sc["narration_words"]), clip)
            except SubtitleError as e:
                raise SubtitleError(f"scenes[{i}].narration_words: {e}") from e
            out[i] = SceneWords(tuple(words), clip, "file")
            continue
        text = voice.narration_text(sc, vo)
        if text is None:
            if sc.get("narration_audio"):
                warns.append(f"scene {i}: narration_audio has no transcript; add narration: text for subtitles")
            continue
        est = estimate(text, samples)
        if not est.words:
            continue
        estimated.append(i)
        if est.boundaries and est.matched * 2 < est.boundaries:
            warns.append(f"scene {i}: only {est.matched} of {est.boundaries} sentence or clause ends match a pause in "
                         "the audio, so subtitle timing there is rough; add narration_words for exact timing")
        out[i] = SceneWords(tuple(est.words), clip, "estimate")
    if estimated:
        warns.append(f"subtitle timing is estimated from the audio in {len(estimated)} scene"
                     f"{'s' if len(estimated) != 1 else ''} ({', '.join(map(str, estimated[:8]))}"
                     f"{', ...' if len(estimated) > 8 else ''}); narration_words gives exact timing")
    return out, warns


def caption_floor() -> float:
    """Extra seconds a subtitled scene with a caption needs after `voice.min_scene_seconds`, so the caption
    (shown once the speech has ended) is on screen long enough to read."""
    return CAPTION_GAP + CAPTION_HOLD


def raise_floors(spec: dict[str, Any], words: dict[int, SceneWords], floors: dict[int, float]) -> dict[int, float]:
    """`floors` with every subtitled scene that has a caption made longer by `caption_floor`."""
    out = dict(floors)
    for i, sw in words.items():
        if spec["scenes"][i].get("caption"):
            out[i] = out.get(i, 0.0) + caption_floor()
    return out


def caption_start(sw: SceneWords) -> float:
    """Scene-local time a subtitled scene's caption pill may appear: after its speech, not over the subtitles."""
    return voice.NARRATION_LEAD + sw.clip_seconds + CAPTION_GAP


# --- drawing --------------------------------------------------------------------------------------------

@dataclass
class _Glyph:
    outline: Image.Image   # 'L': the word grown by the stroke
    fill: Image.Image      # 'L': the word itself
    pad: int


@dataclass
class _Chunk:
    boxes: list[Any]
    x0: float
    y0: float
    size: float
    glyphs: list[_Glyph]
    big: dict[int, _Glyph] = field(default_factory=dict)


class SubtitleTrack:
    """Draws the cues of one scene. Word masks are made once per chunk (a few small images), so a frame costs a
    handful of pastes."""

    MAX_LINES = 2
    POP = 1.15
    CACHE = 8

    def __init__(self, ctx: Ctx, cues: list[Cue], cfg: dict[str, Any]):
        self.ctx, self.cues, self.cfg = ctx, cues, cfg
        self.starts = [c.start for c in cues]
        self._cache: dict[int, _Chunk] = {}
        self.base_size = 52 * ctx.u * ctx.fs * float(cfg["size"])
        self.max_w = ctx.W * 0.84

    def cue_at(self, t: float) -> int | None:
        k = bisect.bisect_right(self.starts, t) - 1
        return k if k >= 0 and t < self.cues[k].end else None

    def _text(self, w: Word) -> str:
        return w.text.upper() if self.cfg["uppercase"] else w.text

    def _glyph(self, text: str, size: float, stroke: int) -> _Glyph:
        fnt = font("sans_bold", size, text)
        w = int(math.ceil(fnt.getlength(text))) + 2 * (stroke + 2)
        h = int(math.ceil(size * 1.28)) + 2 * (stroke + 2)
        pad = stroke + 2
        out = Image.new("L", (w, h), 0)
        ImageDraw.Draw(out).text((pad, pad), text, font=fnt, fill=255, stroke_width=stroke)
        fill = Image.new("L", (w, h), 0)
        ImageDraw.Draw(fill).text((pad, pad), text, font=fnt, fill=255)
        return _Glyph(out, fill, pad)

    def _chunk(self, k: int) -> _Chunk:
        got = self._cache.get(k)
        if got is not None:
            return got
        c, cue = self.ctx, self.cues[k]
        texts = [self._text(w) for w in cue.words]
        size = self.base_size
        while True:
            fnt = font("sans_bold", size, " ".join(texts))
            boxes, bw, bh = layout_words(texts, fnt, self.max_w, space=size * 0.32)
            if bh <= self.MAX_LINES * size * 1.28 + 1 or size <= self.base_size * 0.7:
                break
            size *= 0.92
        if bh > self.MAX_LINES * size * 1.28 + 1:
            c.warn("subtitle words are too wide for two lines; use fewer words per chunk or a smaller size")
        stroke = max(1, int(round(size * 0.08)))
        line_h = size * 1.28
        top = (c.H * 0.5 - bh / 2 if self.cfg["position"] == "middle" else c.caption_y - line_h / 2)
        top = min(top, c.H * 0.95 - bh)  # never off the bottom of the frame
        chunk = _Chunk(boxes, (c.W - bw) / 2, top, size, [self._glyph(b.text, size, stroke) for b in boxes])
        if len(self._cache) >= self.CACHE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[k] = chunk
        return chunk

    def bbox(self, k: int) -> tuple[int, int, int, int]:
        """Where chunk `k` draws: (x0, y0, x1, y1) (tests use it to check nothing collides)."""
        ch = self._chunk(k)
        xs = [ch.x0 + b.x for b in ch.boxes] + [ch.x0 + b.x + b.w for b in ch.boxes]
        ys = [ch.y0 + b.y for b in ch.boxes] + [ch.y0 + b.y + b.h for b in ch.boxes]
        return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))

    def draw(self, img: Image.Image, t: float) -> bool:
        """Draw the cue in force at scene time t onto `img`; False when there is none."""
        k = self.cue_at(t)
        if k is None:
            return False
        th = self.ctx.theme
        cue, ch = self.cues[k], self._chunk(k)
        style = self.cfg["style"]
        active = cue.active(t) if style != "plain" else -1
        for n, (b, g) in enumerate(zip(ch.boxes, ch.glyphs)):
            x, y = ch.x0 + b.x, ch.y0 + b.y
            colour = th.accent2 if cue.keywords[n] else th.fg
            if n == active and style == "karaoke":
                padx, pady = ch.size * 0.12, ch.size * 0.06
                ImageDraw.Draw(img).rounded_rectangle(
                    [x - padx, y + pady, x + b.w + padx, y + b.h - pady], int(ch.size * 0.25), fill=th.accent)
                self._paste(img, g.fill, x - g.pad, y - g.pad + (b.h - ch.size * 1.28) / 2, th.bg)
                continue
            if n == active and style == "pop":
                big = ch.big.get(n)
                if big is None:
                    size = ch.size * self.POP
                    big = ch.big[n] = self._glyph(b.text, size, max(1, int(round(size * 0.08))))
                bx = x + b.w / 2 - (big.fill.width / 2)
                by = y + b.h / 2 - (big.fill.height / 2)
                self._paste(img, big.outline, bx, by, th.bg)
                self._paste(img, big.fill, bx, by, th.accent)
                continue
            gx, gy = x - g.pad, y - g.pad + (b.h - ch.size * 1.28) / 2
            self._paste(img, g.outline, gx, gy, th.bg)
            self._paste(img, g.fill, gx, gy, colour)
        return True

    @staticmethod
    def _paste(img: Image.Image, mask: Image.Image, x: float, y: float, colour: tuple[int, int, int]) -> None:
        xi, yi = int(round(x)), int(round(y))
        img.paste(colour, (xi, yi, xi + mask.width, yi + mask.height), mask)
