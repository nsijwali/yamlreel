"""Voiceover: turn each scene's `narration` into speech and mix it into the video's audio.

Speech comes from a local text-to-speech server you run yourself (VoiceStudio's MCP endpoint, by default
http://127.0.0.1:3900/mcp/), or from audio files you recorded (`narration_audio`). The server address is a
trusted setting: only the user config or the command line can set it, so a spec cannot make yamlreel send its
text to another host.

The client speaks plain MCP "Streamable HTTP" (JSON-RPC over POST) with the standard library, so it needs no
extra dependency.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np

SAMPLE_RATE = 44100
MAX_NARRATION_SECONDS = 600
MAX_RESPONSE_BYTES = 200 * 1024 * 1024
DEFAULT_URL = "http://127.0.0.1:3900/mcp/"
LOADING_TRIES = 5  # a 503 means VoiceStudio is up but its model is still loading
LOADING_BACKOFF = 3.0  # seconds before the second try; each wait is 1.5x the one before
NARRATION_LEAD = 0.3  # seconds of silence between a scene's start and its narration
MIN_SPEED, MAX_SPEED = 0.8, 1.3  # voiceover.speed
TRIM_FRAME = 0.02  # seconds per frame when looking for silence
TRIM_DB = 40.0  # quiet means this far under the clip's own loudest frame
TRIM_PAD = 0.03  # seconds of quiet kept next to speech so word edges are not clipped
TRIM_MAX_GAP = 0.3  # an inner quiet gap longer than this is shortened ...
TRIM_KEEP_GAP = 0.15  # ... to this
DEBREATH_HOP = 0.01  # seconds between analysis frames when looking for breaths
DEBREATH_WIN = 0.03  # seconds per analysis frame
DEBREATH_FLOOR_DB = -58.0  # quieter than this (full scale) is silence, which trim handles, not a breath
DEBREATH_CEIL_DB = 17.0  # a breath is at least this far under the clip's 95th-percentile frame level
DEBREATH_VOICED = 0.45  # normalised autocorrelation at or above this over 60-400 Hz means voiced (speech)
DEBREATH_PITCH_HZ = (60, 400)  # lag range searched for voicing
DEBREATH_MIN_MS = 90  # a quiet unvoiced run shorter than this is kept (consonants, word edges)
DEBREATH_FADE_MS = 12  # fade in/out around a silenced run
DEBREATH_CHUNK = 1024  # frames per FFT batch, to bound memory
CACHE_KEY_VERSION = 2  # bump when what a cached clip depends on changes; old files are then never read


class VoiceError(RuntimeError):
    pass


@dataclass
class VoiceServer:
    url: str = DEFAULT_URL
    client_id: str = "yamlreel"
    timeout: float = 600.0

    def __post_init__(self) -> None:
        try:
            u = urlsplit(self.url)
            u.port  # noqa: B018 (raises ValueError for a bad port)
        except ValueError as e:
            raise VoiceError(f"voice_server.url is not a valid URL ({e}): {self.url!r}") from e
        if u.scheme not in ("http", "https") or not u.hostname:
            raise VoiceError(f"voice_server.url must be an http(s) URL, got {self.url!r}")

    def identity(self) -> str:
        """`scheme://host:port/path`: which server speaks. Never includes userinfo, query or fragment, so a
        credential in the URL cannot end up in a cache key. `;params` stay in the path."""
        u = urlsplit(self.url)
        host = u.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        try:
            explicit = u.port
        except ValueError as e:  # url reassigned to a bad value after construction
            raise VoiceError(f"voice_server.url is not a valid URL ({e}): {self.url!r}") from e
        port = explicit if explicit is not None else (443 if u.scheme == "https" else 80)
        return f"{u.scheme}://{host}:{port}{u.path or '/'}"


class _Session:
    def __init__(self, server: VoiceServer):
        self.server = server
        self.sid: str | None = None
        self._id = 0

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "X-OmniVoice-Client-Id": self.server.client_id}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = Request(self.server.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        loading = 0  # how many tries came back 503: the model may still be loading
        for attempt in range(LOADING_TRIES):
            try:
                with urlopen(req, timeout=self.server.timeout) as r:  # noqa: S310 (scheme checked in VoiceServer)
                    self.sid = r.headers.get("Mcp-Session-Id") or self.sid
                    body = r.read(MAX_RESPONSE_BYTES + 1)
                    ctype = r.headers.get("Content-Type", "")
                break
            except HTTPError as e:  # before URLError, which it subclasses
                if e.code == 503:
                    loading += 1
                    if attempt + 1 < LOADING_TRIES:
                        time.sleep(LOADING_BACKOFF * 1.5 ** attempt)
                        continue
                raise VoiceError(self._unreachable(e.reason, loading == LOADING_TRIES)) from e
            except URLError as e:
                raise VoiceError(self._unreachable(e.reason, False)) from e
            except OSError as e:
                raise VoiceError(f"voice server request failed: {e}") from e
        if len(body) > MAX_RESPONSE_BYTES:
            raise VoiceError("voice server response is too large")
        if "id" not in payload or not body.strip():
            return None
        text = body.decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                if line.startswith("data:") and line[5:].strip():
                    msg = json.loads(line[5:])
                    if msg.get("id") == payload["id"]:
                        return msg
            raise VoiceError("voice server sent no reply")
        return json.loads(text)

    def _unreachable(self, reason: Any, loading: bool) -> str:
        hint = " It answered 503 every time, so it may still be loading the model; try again shortly." if loading else ""
        return f"cannot reach the voice server at {self.server.url}: {reason}. Is VoiceStudio running?{hint}"

    def rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if msg is None or "error" in msg:
            raise VoiceError(f"voice server error on {method}: {(msg or {}).get('error', 'empty reply')}")
        return msg["result"]

    def start(self) -> None:
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "yamlreel", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _find_wav_b64(result: dict[str, Any]) -> str | None:
    """The tool reply carries `wav_base64` as JSON text (or as an audio/resource block)."""
    for block in result.get("content", []):
        if block.get("type") == "audio" and block.get("data"):
            return block["data"]
        text = block.get("text")
        if isinstance(text, str):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                if data.get("error"):
                    raise VoiceError(f"voice server: {data['error']}")
                if data.get("wav_base64"):
                    return data["wav_base64"]
    return None


def synthesize(session: _Session, text: str, voice: str | None, language: str | None) -> bytes:
    args: dict[str, Any] = {"text": text}
    if voice:
        args["profile_id"] = voice
    if language:
        args["language"] = language
    result = session.rpc("tools/call", {"name": "generate_speech", "arguments": args})
    if result.get("isError"):
        raise VoiceError(f"voice server could not speak the text: {json.dumps(result.get('content'))[:300]}")
    b64 = _find_wav_b64(result)
    if not b64:
        raise VoiceError("voice server reply had no audio (is OMNIVOICE_MCP_OUTPUT_MODE set to 'files'? "
                         "yamlreel needs the default inline mode)")
    return base64.b64decode(b64, validate=False)


def read_wav(path: Path) -> np.ndarray:
    """PCM WAV -> mono float32 at SAMPLE_RATE."""
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if n / max(rate, 1) > MAX_NARRATION_SECONDS:
                raise VoiceError(f"{path.name}: narration clip is longer than {MAX_NARRATION_SECONDS}s")
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise VoiceError(f"{path.name}: not a readable PCM WAV file ({e})") from e
    if width == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise VoiceError(f"{path.name}: unsupported sample width {width}")
    a = a.reshape(-1, ch).mean(axis=1) if ch > 1 else a
    if rate != SAMPLE_RATE and len(a):
        x = np.linspace(0, len(a) - 1, int(len(a) * SAMPLE_RATE / rate))
        a = np.interp(x, np.arange(len(a)), a).astype(np.float32)
    return a


def write_wav(path: Path, samples: np.ndarray) -> Path:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return path


def trim_silence(samples: np.ndarray, speed: float = 1.0) -> np.ndarray:
    """Cut leading and trailing silence and shorten inner gaps longer than TRIM_MAX_GAP to TRIM_KEEP_GAP.

    `speed` is the speed-up applied afterwards: gaps are kept `speed` times longer, so that they are still
    TRIM_KEEP_GAP long once the clip has been sped up (the subtitles need that much pause to see a sentence end).

    Quiet is measured against the clip's own loudest 20 ms frame, so quiet and loud voices both work. A clip
    with no speech-level frame is returned as it came."""
    frame = int(TRIM_FRAME * SAMPLE_RATE)
    n = -(-len(samples) // frame)
    if n == 0:
        return samples
    padded = np.zeros(n * frame, dtype=np.float32)
    padded[: len(samples)] = samples
    rms = np.sqrt((padded.reshape(n, frame) ** 2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 0:
        return samples
    loud = np.flatnonzero(rms > peak * 10 ** (-TRIM_DB / 20))
    pad = int(TRIM_PAD * SAMPLE_RATE)
    start = max(0, int(loud[0]) * frame - pad)
    end = min(len(samples), (int(loud[-1]) + 1) * frame + pad)
    keep = int(TRIM_KEEP_GAP * max(speed, 1.0) / 2 * SAMPLE_RATE)
    pieces: list[np.ndarray] = []
    at = start
    for a, b in zip(loud[:-1], loud[1:]):  # the quiet run lies between two consecutive loud frames
        gap_a, gap_b = (int(a) + 1) * frame, int(b) * frame
        if gap_b - gap_a > TRIM_MAX_GAP * SAMPLE_RATE:
            pieces.append(samples[at:gap_a + keep])
            at = gap_b - keep
    pieces.append(samples[at:end])
    return np.concatenate(pieces)


def debreath(samples: np.ndarray) -> np.ndarray:
    """Replace breaths with silence, in place: the clip keeps its length (shortening is trim's job).

    A breath is a run of at least DEBREATH_MIN_MS of frames that are unvoiced (low autocorrelation over the
    pitch range) and quiet, but not silent: above DEBREATH_FLOOR_DB and DEBREATH_CEIL_DB under the clip's own
    95th-percentile level, so quiet and loud voices both work. Runs get DEBREATH_FADE_MS fades on each side.
    A clip with no breath (or no frames) is returned as it came."""
    hop, win = int(DEBREATH_HOP * SAMPLE_RATE), int(DEBREATH_WIN * SAMPLE_RATE)
    n = (len(samples) - win) // hop if len(samples) >= win else 0
    if n <= 0:
        return samples
    x = np.asarray(samples, dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop][:n]
    energy = np.einsum("ij,ij->i", frames, frames, dtype=np.float64)
    db = 10 * np.log10(energy / win + 1e-12)
    peak = float(np.percentile(db, 95))
    cand = (db > DEBREATH_FLOOR_DB) & (db < peak - DEBREATH_CEIL_DB)
    idx = np.flatnonzero(cand)  # only these need the (costly) voicing check
    if not len(idx):
        return samples
    lo, hi = SAMPLE_RATE // DEBREATH_PITCH_HZ[1], SAMPLE_RATE // DEBREATH_PITCH_HZ[0]
    nfft = 1 << (2 * win - 1).bit_length()
    voiced = np.zeros(n, dtype=bool)
    for a in range(0, len(idx), DEBREATH_CHUNK):
        sel = idx[a:a + DEBREATH_CHUNK]
        f = frames[sel].astype(np.float64)
        f -= f.mean(axis=1, keepdims=True)
        spec = np.fft.rfft(f, nfft, axis=1)
        r = np.fft.irfft(spec.real ** 2 + spec.imag ** 2, nfft, axis=1)[:, :hi]
        voiced[sel] = r[:, lo:hi].max(axis=1) / (r[:, 0] + 1e-12) >= DEBREATH_VOICED
    cand &= ~voiced
    edges = np.diff(np.concatenate(([0], cand.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    keep = (ends - starts) * DEBREATH_HOP * 1000 >= DEBREATH_MIN_MS
    if not keep.any():
        return samples
    out = x.copy()
    fade = int(DEBREATH_FADE_MS / 1000 * SAMPLE_RATE)
    for i, j in zip(starts[keep], ends[keep]):
        a, b = int(i) * hop, min((int(j) - 1) * hop + win, len(out))   # j is the first frame after the run
        out[a:b] = 0.0
        if a - fade >= 0:
            out[a - fade:a] *= np.linspace(1, 0, fade, dtype=np.float32)
        if b + fade <= len(out):
            out[b:b + fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    return out


def change_speed(samples: np.ndarray, speed: float) -> np.ndarray:
    """Play the clip `speed` times as fast without changing its pitch (ffmpeg `atempo`)."""
    from . import render  # late: render imports this module
    ffmpeg = render.ffmpeg_exe()
    if ffmpeg is None:
        raise VoiceError("voiceover.speed needs ffmpeg and none was found. Install it (macOS: brew install ffmpeg; "
                         "Debian/Ubuntu: sudo apt install ffmpeg) or run: pip install imageio-ffmpeg")
    with tempfile.TemporaryDirectory(prefix="yamlreel-speed-") as td:
        src, dst = Path(td) / "in.wav", Path(td) / "out.wav"
        write_wav(src, samples)
        cmd = [ffmpeg, "-loglevel", "error", "-y", "-i", str(src), "-filter:a", f"atempo={speed:.4f}",
               "-ar", str(SAMPLE_RATE), "-ac", "1", str(dst)]
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise VoiceError(f"cannot run ffmpeg at {ffmpeg} for voiceover.speed: {e}") from e
        if done.returncode != 0:
            raise VoiceError("ffmpeg failed on voiceover.speed:\n" + done.stderr.decode(errors="replace")[-500:])
        return read_wav(dst)


def process_clip(samples: np.ndarray, vo: dict[str, Any]) -> np.ndarray:
    """Apply voiceover.debreath, then voiceover.trim, then voiceover.speed. Debreath silences breaths in place
    so that trim can then cut them. A 1.0 speed leaves the audio alone and needs no ffmpeg."""
    speed = float(vo.get("speed", 1.0))
    if vo.get("debreath") and len(samples):
        samples = debreath(samples)
    if vo.get("trim") and len(samples):
        samples = trim_silence(samples, speed)
    if speed != 1.0 and len(samples):
        samples = change_speed(samples, speed)
    return samples


def _sentence(t: str) -> str:
    t = " ".join(str(t).split())
    return t if not t or t[-1] in ".!?:;" else t + "."


def screen_text(sc: dict[str, Any]) -> str:
    """The words a viewer reads in this scene, in reading order, so the voice says what the screen shows."""
    kind = sc["type"]
    if kind == "title":
        parts = [sc.get("title"), sc.get("subtitle")]
    elif kind == "type":
        parts = [sc.get("text")]
    elif kind == "bullets":
        parts = [sc.get("title"), *sc.get("items", [])]
    elif kind == "stat":
        parts = [sc.get("value"), sc.get("label")]
    elif kind == "cta":
        parts = [sc.get("question"), *(f"{chr(65 + n)}: {a}" for n, a in enumerate(sc.get("answers", []))),
                 sc.get("prompt")]   # validation fills in the default prompt
    else:  # shot, code, diagram, table, array, meter, tree, timeline: the title is the only prose; the data is read off the screen
        parts = [sc.get("title")]
    parts.append(sc.get("caption"))
    seen: list[str] = []
    for p in parts:
        p = _sentence(p) if p else ""
        if p and p not in seen:
            seen.append(p)
    return " ".join(seen)


def narration_text(sc: dict[str, Any], vo: dict[str, Any]) -> str | None:
    """The words spoken in this scene, or None when it is silent or has only a recording with no transcript.

    One source of truth: speech synthesis and subtitles both read it, so a caption never differs from the voice
    (`read_screen` included). With `narration_audio`, `narration` is the transcript of that recording."""
    if sc.get("narration_audio"):
        text = sc.get("narration")
        return text if isinstance(text, str) and text.strip() else None
    narration = sc.get("narration")
    if narration is False or not (narration or vo.get("read_screen")):
        return None
    return narration or screen_text(sc) or None


def narration_cache_key(server: VoiceServer, text: str, voice: str | None, language: str | None) -> str:
    """What a cached clip depends on: the server address, the voice, the language and the text. The client id
    only picks the voice when no `voice` is set (VoiceStudio binds it as a default profile)."""
    blob = json.dumps({"v": CACHE_KEY_VERSION, "server": server.identity(),
                       "client": None if voice else server.client_id,
                       "voice": voice, "language": language, "text": text}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def cached_clip_path(cache: Path, server: VoiceServer, text: str, vo: dict[str, Any]) -> Path:
    """Where the raw synthesised clip for this text lives (or would live): render and check-voice both use it."""
    key = narration_cache_key(server, text, vo.get("voice"), vo.get("language"))
    return cache / f"narration-v{CACHE_KEY_VERSION}-{key}.wav"


def min_scene_seconds(samples: np.ndarray, gap: float) -> float:
    """The shortest scene that holds this clip: the speech, then the larger of the gap and the lead."""
    return len(samples) / SAMPLE_RATE + max(gap, NARRATION_LEAD)


def scene_floors(spec: dict[str, Any], clips: dict[int, np.ndarray]) -> dict[int, float]:
    """{scene index: shortest allowed length} for every narrated scene."""
    gap = float((spec.get("voiceover") or {}).get("gap", 0.5))
    return {i: min_scene_seconds(c, gap) for i, c in clips.items()}


def prepare_narration(spec: dict[str, Any], server: VoiceServer | None, cache: Path,
                      resolve_audio) -> dict[int, np.ndarray]:
    """Return {scene index: samples} for every scene with narration, and stretch scene lengths to fit.

    `resolve_audio(path)` must confine `narration_audio` files to the allowed folders."""
    vo = spec.get("voiceover") or {}
    gap = float(vo.get("gap", 0.5))
    clips: dict[int, np.ndarray] = {}
    session: _Session | None = None
    cache.mkdir(parents=True, exist_ok=True)
    for i, sc in enumerate(spec["scenes"]):
        if sc.get("narration_audio"):
            samples = read_wav(resolve_audio(sc["narration_audio"]))
        elif (text := narration_text(sc, vo)) is not None:
            if server is None:
                raise VoiceError(
                    f"scene {i} has narration but no voice server is configured. Set voice_server.url in your "
                    "user config (yamlreel config init --user) or pass --voice-url, use narration_audio files, or "
                    "render with --no-voice for music only.")
            cached = cached_clip_path(cache, server, text, vo)
            if not cached.exists():
                if session is None:
                    session = _Session(server)
                    session.start()
                audio = synthesize(session, text, vo.get("voice"), vo.get("language"))
                tmp = cached.with_suffix(".part")
                tmp.write_bytes(audio)
                try:
                    read_wav(tmp)  # reject anything that is not a sane WAV before it is cached
                except VoiceError:
                    tmp.unlink(missing_ok=True)
                    raise
                tmp.replace(cached)
            samples = read_wav(cached)
        else:
            continue
        samples = process_clip(samples, vo)  # after the cache, so the cached audio stays raw
        clips[i] = samples
        sc["seconds"] = max(sc["seconds"], min_scene_seconds(samples, gap))
    return clips


def build_track(clips: dict[int, np.ndarray], starts: list[float], total: float,
                lead: float = NARRATION_LEAD) -> np.ndarray:
    """Place each clip `lead` seconds after its scene starts. `starts` are the scene start times of the final
    timeline, so the speech lines up with what is on screen."""
    track = np.zeros(int(total * SAMPLE_RATE) + SAMPLE_RATE, dtype=np.float32)
    for i, c in clips.items():
        a = int((starts[i] + lead) * SAMPLE_RATE)
        track[a:a + len(c)] += c[: max(0, len(track) - a)]
    peak = float(np.abs(track).max())
    return track / peak * 0.95 if peak > 0.95 else track
