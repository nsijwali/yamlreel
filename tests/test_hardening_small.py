"""Hardening slice 1: watermark bounds, build-once caches, the Shorts rule, DNS-blocked mapped addresses, the voice
error model with URL redaction, and race-free narration cache writes (docs/specs/hardening.md)."""
from __future__ import annotations

import io
import ipaddress
import json
import threading
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from fakes import _tone_wav_bytes
from yamlreel import atomicfile, errorfmt, polish, safety, shorts
from yamlreel import render as render_mod
from yamlreel import voice as voice_mod
from yamlreel.cli import main
from yamlreel.config import Config
from yamlreel.render import RenderError, Runtime, Timeline, share_copy
from yamlreel.specs import validate
from yamlreel.voice import VoiceError, VoiceServer
from yamlreel.watermark_fit import MAX_WIDTH_FRAC, WatermarkSizeError, fit_watermark


def _isolate(monkeypatch, tmp_path: Path) -> None:
    for name in ("CONFIG", "CHROMIUM", "FONT_DIR"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-user-config.yaml"))
    monkeypatch.chdir(tmp_path)


# --- 1. watermark size is bounded before any resize -----------------------------------------------------------

@pytest.mark.parametrize("src,height", [((300, 120), 56), ((64, 64), 40), ((1000, 333), 30), ((7, 3), 56)])
def test_an_ordinary_logo_keeps_the_old_size(src, height):
    w, h = src
    assert fit_watermark(w, h, height, 1920) == (max(1, int(w * height / h)), height)


def test_a_tall_logo_keeps_its_height_and_is_narrow():
    assert fit_watermark(10, 4000, 56, 1080) == (1, 56)


def test_a_wide_logo_is_shrunk_to_half_the_frame_keeping_its_aspect():
    w, h = fit_watermark(4000, 100, 56, 1080)
    assert w == int(1080 * MAX_WIDTH_FRAC) and h == int(100 * w / 4000)


def test_an_extreme_wide_logo_is_a_clear_error():
    with pytest.raises(WatermarkSizeError, match="less than 1 px tall"):
        fit_watermark(100_000, 1, 200, 1920)
    with pytest.raises(WatermarkSizeError, match="no pixels"):
        fit_watermark(0, 10, 56, 1920)


def _wm_spec(tmp_path: Path, logo: Image.Image, size: int = 56) -> dict:
    logo.save(tmp_path / "logo.png")
    spec = validate({"scenes": [{"type": "title", "title": "x", "seconds": 1}], "music": {"style": "none"},
                     "watermark": {"image": "logo.png", "size": size}}, Config())
    spec["_dir"] = str(tmp_path)
    return spec


def _record_resizes(monkeypatch) -> list[tuple[int, int]]:
    seen: list[tuple[int, int]] = []
    real = Image.Image.resize

    def spy(self, size, *a, **kw):
        seen.append(tuple(size))
        assert size[0] * size[1] <= 4_000_000, f"oversized resize to {size}"
        return real(self, size, *a, **kw)

    monkeypatch.setattr(Image.Image, "resize", spy)
    return seen


def test_a_100000x1_watermark_never_asks_for_a_huge_resize(tmp_path, monkeypatch):
    seen = _record_resizes(monkeypatch)
    spec = _wm_spec(tmp_path, Image.new("RGBA", (100_000, 1), (255, 0, 0, 255)), size=200)
    with pytest.raises(RenderError, match="less than 1 px tall"):
        Timeline(spec, "landscape", tmp_path, scale=1.0, runtime=Runtime(roots=[tmp_path]))
    assert seen == []   # refused from the header: nothing was resized


def test_a_wide_watermark_is_shrunk_inside_the_frame(tmp_path, monkeypatch):
    seen = _record_resizes(monkeypatch)
    spec = _wm_spec(tmp_path, Image.new("RGBA", (20_000, 100), (255, 0, 0, 255)), size=56)
    tl = Timeline(spec, "landscape", tmp_path, scale=0.5, runtime=Runtime(roots=[tmp_path]))
    rgb, mask, _ = tl._wm
    assert rgb.width == int(tl.ctx.W * MAX_WIDTH_FRAC) and rgb.height == 2 and mask.size == rgb.size
    assert (rgb.width, rgb.height) in seen


def test_a_transparent_and_a_normal_logo_render_as_before(tmp_path):
    logo = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    logo.paste((240, 80, 40, 255), (0, 0, 120, 120))
    spec = _wm_spec(tmp_path, logo)
    tl = Timeline(spec, "landscape", tmp_path, scale=0.5, runtime=Runtime(roots=[tmp_path]))
    rgb, mask, _ = tl._wm
    h = int(56 * tl.ctx.u)
    assert rgb.size == (int(300 * h / 120), h)
    assert mask.getextrema()[0] == 0 and mask.getextrema()[1] > 0   # transparent half stays transparent
    expected = logo.resize(rgb.size, Image.LANCZOS)
    assert rgb.tobytes() == expected.convert("RGB").tobytes()


# --- 8. caches build each distinct value once -------------------------------------------------------------------

def test_one_step_badge_is_built_per_distinct_step(tmp_path, monkeypatch):
    built = []
    real = render_mod.make_badge
    monkeypatch.setattr(render_mod, "make_badge", lambda ctx, *plan: built.append(plan) or real(ctx, *plan))
    spec = validate({"scenes": [{"type": "title", "title": f"t{n}", "seconds": 1, "step": s}
                                for n, s in enumerate(["a", "a", "b", "a", "b"])], "music": {"style": "none"}}, Config())
    tl = Timeline(spec, "landscape", tmp_path, scale=0.25, runtime=Runtime(roots=[tmp_path]))
    assert len(built) == len(set(built)) == 2   # five stepped scenes, two distinct steps, two badges built
    assert tl._badges[0] is tl._badges[1] is tl._badges[3] and tl._badges[2] is tl._badges[4]


def test_a_recording_used_by_many_events_is_decoded_once(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(polish, "_decode", lambda f: calls.append(f) or np.full(100, 0.1))
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    events = [polish.Event(t, 1.0, file=f) for t, f in ((0.1, a), (0.2, a), (0.3, b), (0.4, a))]
    out = polish.render_events(events, 1.0)
    assert calls == [a, b]
    assert np.isclose(out.max(), 0.1)


# --- 10. one Shorts rule ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("w,h,s,ok", [
    (1080, 1920, 60, True), (1080, 1920, 60.01, True), (1080, 1920, 180, True), (1080, 1920, 180.01, False),
    (1080, 1080, 179, True), (1080, 1080, 181, False), (1920, 1080, 30, False),
])
def test_shorts_eligibility_boundaries(w, h, s, ok):
    assert shorts.shorts_eligible(w, h, s) is ok


def test_editorial_advice_is_separate_from_eligibility():
    assert shorts.editorial_note(60) is None and shorts.editorial_note(181) is None
    assert "60s or less" in shorts.editorial_note(60.01) and "valid Short" in shorts.editorial_note(180)


def _audit(monkeypatch, seconds: float, w: int = 1080, h: int = 1920) -> polish.Report:
    monkeypatch.setattr(polish, "probe", lambda v: polish.Probe(seconds, w, h, 30.0, True))
    monkeypatch.setattr(polish, "measure_audio", lambda v: polish.Audio(-14.0, 3.0, -2.0))
    monkeypatch.setattr(polish, "measure_loop", lambda v: polish.Loop(0.0, 0.01))
    return polish.audit_video(Path("x.mp4"))


@pytest.mark.parametrize("seconds,warnings,tip", [(60, 0, False), (61, 0, True), (180, 0, True), (180.5, 1, False)])
def test_strict_audit_fails_on_length_only_past_180s(monkeypatch, seconds, warnings, tip):
    rep = _audit(monkeypatch, seconds)
    assert rep.warnings == warnings
    assert any(line.startswith("info: tip") for line in rep.lines) is tip


def test_a_long_square_video_is_an_info_line_and_landscape_is_not_judged(monkeypatch):
    rep = _audit(monkeypatch, 200, 1080, 1080)
    assert rep.warnings == 0 and any(l.startswith("info: square, 200.0s") for l in rep.lines)
    assert _audit(monkeypatch, 180, 1080, 1080).warnings == 0
    rep = _audit(monkeypatch, 500, 1920, 1080)
    assert rep.warnings == 0 and not any("Shorts" in line for line in rep.lines)


def test_share_copy_tags_every_eligible_short_and_warns_past_180s():
    spec = validate({"scenes": [{"type": "title", "title": "x"}], "meta": {"title": "T", "tags": ["a"]}}, Config())
    for total in (61, 120, 180):
        assert "#Shorts #a" in share_copy(spec, total, ["vertical"])[0]
    text, warns = share_copy(spec, 181, ["vertical"])
    assert "#Shorts" not in text and any("up to 180s" in w for w in warns)
    assert "#Shorts" in share_copy(spec, 30, ["square"])[0]
    text, warns = share_copy(spec, 200, ["square"])   # same rule as audit: noted there, no share-copy warning
    assert "#Shorts" not in text and not any("Shorts" in w for w in warns)
    assert "#Shorts" not in share_copy(spec, 30, ["landscape"])[0]


@pytest.mark.parametrize("w,h,s,level", [(1080, 1920, 180, "ok"), (1080, 1920, 181, "warn"), (1080, 1080, 181, "info"),
                                         (1080, 1080, 60, "ok"), (1920, 1080, 999, None)])
def test_one_length_rule_for_audit_and_share_copy(w, h, s, level):
    assert shorts.length_level(w, h, s) == level


# --- 7. DNS-blocked mapped address ------------------------------------------------------------------------------

@pytest.fixture
def old_python_unspecified(monkeypatch):
    """Behave like the Python patch releases whose IPv6 `is_unspecified` says False for ::ffff:0.0.0.0."""
    real = ipaddress.IPv6Address.is_unspecified
    monkeypatch.setattr(ipaddress.IPv6Address, "is_unspecified",
                        property(lambda self: False if self.ipv4_mapped is not None else real.fget(self)))


@pytest.mark.parametrize("answer", [["::ffff:0.0.0.0"], ["::ffff:0.0.0.0", "0.0.0.0", "::"]])
def test_a_name_answered_with_mapped_zero_is_dns_blocked_on_every_python(monkeypatch, old_python_unspecified, answer):
    monkeypatch.setattr(safety, "_resolve", lambda host: list(answer))
    for allow_private in (False, True):
        with pytest.raises(safety.Unresolvable, match="blocked by DNS"):
            safety.pinned_ips("tracker.test", allow_private)


def test_mapped_zero_as_a_literal_or_beside_a_public_address_keeps_the_refusal(monkeypatch, old_python_unspecified):
    with pytest.raises(safety.UnsafeInput) as e:
        safety.pinned_ips("[::ffff:0.0.0.0]", True)
    assert not isinstance(e.value, safety.Unresolvable)
    monkeypatch.setattr(safety, "_resolve", lambda host: ["::ffff:0.0.0.0", "93.184.216.34"])
    with pytest.raises(safety.UnsafeInput, match="always refused") as e:
        safety.pinned_ips("mixed.test", True)
    assert not isinstance(e.value, safety.Unresolvable)


@pytest.mark.parametrize("ip", ["::ffff:169.254.169.254", "::ffff:10.0.0.1", "::ffff:127.0.0.1"])
def test_mapped_metadata_and_private_addresses_are_still_refused(monkeypatch, ip):
    monkeypatch.setattr(safety, "_resolve", lambda host: [ip])
    with pytest.raises(safety.UnsafeInput) as e:
        safety.pinned_ips("x.test", False)
    assert not isinstance(e.value, safety.Unresolvable)


# --- 6. voice error model and URL redaction ---------------------------------------------------------------------

SECRET_URL = "http://alice:s3cret-pw@voice.example:3900/mcp/?token=abc123&x=1#frag"


def test_redact_url_hides_userinfo_query_values_and_fragment():
    shown = errorfmt.redact_url(SECRET_URL)
    assert shown == "http://***@voice.example:3900/mcp/?token=***&x=***"
    assert "s3cret" not in shown and "abc123" not in shown and "alice" not in shown
    assert errorfmt.redact_url("http://127.0.0.1:3900/mcp/") == "http://127.0.0.1:3900/mcp/"
    assert errorfmt.redact_url("http://[::1]:99999/") == "<invalid URL>"
    assert errorfmt.redact_text(f"failed for {SECRET_URL} (s3cret-pw)", SECRET_URL).count("s3cret") == 0


@pytest.mark.parametrize("url", ["ftp://alice:s3cret-pw@h/?token=abc123", "http://alice:s3cret-pw@h:bad/?token=abc123"])
def test_a_bad_voice_url_error_does_not_print_its_credentials(url):
    with pytest.raises(VoiceError) as e:
        VoiceServer(url=url)
    assert "s3cret" not in str(e.value) and "abc123" not in str(e.value)


def test_an_unreachable_server_error_does_not_print_its_credentials(monkeypatch):
    from urllib.error import URLError

    def refuse(req, timeout):
        raise URLError(f"refused by {SECRET_URL}")

    monkeypatch.setattr(voice_mod, "urlopen", refuse)
    with pytest.raises(VoiceError, match="cannot reach the voice server at http://\\*\\*\\*@voice.example") as e:
        voice_mod._Session(VoiceServer(url=SECRET_URL)).rpc("initialize", {})
    assert "s3cret" not in str(e.value) and "abc123" not in str(e.value)


class _Reply:
    def __init__(self, body: bytes, ctype: str = "application/json"):
        self.body, self.headers = body, {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return self.body


def _session_replying(monkeypatch, body: bytes, ctype: str = "application/json") -> voice_mod._Session:
    monkeypatch.setattr(voice_mod, "urlopen", lambda req, timeout: _Reply(body, ctype))
    return voice_mod._Session(VoiceServer(url="http://voice.example/mcp/"))


@pytest.mark.parametrize("body,match", [
    (b"not json", "not valid JSON"),
    (b"[1, 2]", "not a JSON-RPC object"),
    (b'{"jsonrpc": "2.0", "id": 1}', "neither a result nor an error"),
    (b'{"jsonrpc": "2.0", "id": 7, "result": {}}', "has id 7, expected 1"),
    (b'{"jsonrpc": "2.0", "id": 1, "result": [1]}', "result that is not an object"),
    (b'{"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no model"}}', "error on initialize: no model"),
    (b'"just a string"', "not a JSON-RPC object"),
])
def test_malformed_json_rpc_envelopes_are_voice_errors(monkeypatch, body, match):
    with pytest.raises(VoiceError, match=match):
        _session_replying(monkeypatch, body).rpc("initialize", {})


def test_an_sse_reply_with_bad_json_is_a_voice_error(monkeypatch):
    s = _session_replying(monkeypatch, b"data: {oops\n\n", "text/event-stream")
    with pytest.raises(VoiceError, match="not valid JSON"):
        s.rpc("initialize", {})


@pytest.mark.parametrize("result,match", [
    ({"content": "nope"}, "not a list"),
    ({"content": ["nope"]}, "not an object"),
    ({"content": [{"type": "audio", "data": 5}]}, "not base64 text"),
    ({"content": [{"type": "audio", "data": "a"}]}, "not valid base64"),
])
def test_malformed_tool_results_are_voice_errors(monkeypatch, result, match):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
    with pytest.raises(VoiceError, match=match):
        voice_mod.synthesize(_session_replying(monkeypatch, body), "hi", None, None)


def test_a_missing_narration_file_is_a_voice_error(tmp_path):
    with pytest.raises(VoiceError, match="narration audio not found"):
        voice_mod.read_wav(tmp_path / "gone.wav")


def test_a_folder_given_as_narration_audio_is_a_voice_error(tmp_path):
    with pytest.raises(VoiceError, match="cannot read narration audio"):
        voice_mod.read_wav(tmp_path)


@pytest.mark.parametrize("cut", [1, 1001, 4000])
def test_a_truncated_wav_is_a_voice_error(tmp_path, cut):
    data = _tone_wav_bytes(0.5)
    p = tmp_path / "t.wav"
    p.write_bytes(data[: len(data) - cut])
    with pytest.raises(VoiceError, match="truncated WAV"):
        voice_mod.read_wav(p)


def test_cli_render_with_a_missing_narration_file_prints_one_error_line(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    spec = tmp_path / "s.yaml"
    spec.write_text("music: {style: none}\nscenes:\n  - {type: title, title: Hi, seconds: 1, narration_audio: gone.wav}\n")
    assert main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: voiceover: narration audio not found") and "Traceback" not in err


# --- 5. narration cache writes ----------------------------------------------------------------------------------

def _wav(seconds: float, freq: float) -> bytes:
    n = int(seconds * 8000)
    pcm = (np.sin(2 * np.pi * freq * np.arange(n) / 8000) * 12000).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(pcm)
    return buf.getvalue()


def test_two_simultaneous_writers_of_one_clip_never_mix_bytes(tmp_path):
    dest = tmp_path / "narration-v2-abc.wav"
    payloads = [_wav(0.5 + k * 0.01, 200 + 40 * k) for k in range(8)]
    start = threading.Barrier(len(payloads))
    errors: list[BaseException] = []

    def writer(data: bytes) -> None:
        try:
            start.wait()
            atomicfile.write_validated(dest, data, voice_mod.read_wav)
        except BaseException as e:   # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert dest.read_bytes() in payloads          # exactly one writer's complete file
    assert list(tmp_path.iterdir()) == [dest]     # no temporary files left behind


def test_an_interrupted_write_before_publish_leaves_the_old_clip_and_no_temp_file(tmp_path):
    dest = tmp_path / "clip.wav"
    dest.write_bytes(_wav(0.3, 300))
    old = dest.read_bytes()

    def interrupted(p: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        atomicfile.write_validated(dest, _wav(0.3, 500), interrupted)
    assert dest.read_bytes() == old and list(tmp_path.iterdir()) == [dest]
    with pytest.raises(VoiceError):
        atomicfile.write_validated(dest, b"RIFF junk", voice_mod.read_wav)
    assert dest.read_bytes() == old and list(tmp_path.iterdir()) == [dest]


def test_prepare_narration_publishes_through_unique_temp_files(tmp_path, monkeypatch):
    seen = []
    real = atomicfile.write_validated
    monkeypatch.setattr(voice_mod, "write_validated", lambda d, data, v: seen.append(d) or real(d, data, v))
    monkeypatch.setattr(voice_mod, "_Session", lambda server: type("S", (), {"start": lambda self: None})())
    monkeypatch.setattr(voice_mod, "synthesize", lambda s, text, v, lang: _tone_wav_bytes(0.4))
    spec = {"scenes": [{"type": "title", "title": "x", "seconds": 1, "narration": "hello"}], "voiceover": {}}
    server = VoiceServer(url="http://voice.example/mcp/")
    clips = voice_mod.prepare_narration(spec, server, tmp_path, lambda p: p)
    cached = voice_mod.cached_clip_path(tmp_path, server, "hello", {})
    assert seen == [cached] and cached.exists() and 0 in clips
    assert not list(tmp_path.glob("*.part"))
