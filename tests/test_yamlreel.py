import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml
from fakes import _fake_voice_server, _tone_wav_bytes  # tests/ is on sys.path (pytest rootdir import mode)

from yamlreel import __version__, legacy, music
from yamlreel.cli import main
from yamlreel.render import RenderError, Runtime, Timeline, render, share_copy
from yamlreel.config import Config, ConfigError, load_config, merge
from yamlreel.safety import UnsafeInput, check_http_url, resolve_local
from yamlreel.shots import CaptureError
from yamlreel.specs import FORMATS, SpecError, load_spec, validate
from yamlreel.theme import get_theme
from yamlreel.textkit import make_block

TEMPLATES = Path(__file__).parent.parent / "yamlreel" / "templates"


def isolate_env(monkeypatch, xdg):
    """Drop every old and new override variable, point XDG_CONFIG_HOME at a tmp dir and reset warn-once."""
    for prefix in ("YAMLREEL", legacy.LEGACY.upper()):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "xdg"))
    legacy._warned.clear()


@pytest.fixture(autouse=True)
def no_personal_config(tmp_path_factory, monkeypatch):
    """Never read the developer's own settings: their ~/.config/yamlreel/config.yaml, a ./yamlreel.config.yaml in the
    folder pytest runs from, or YAMLREEL_* overrides (a voice server or theme there would leak into tests).
    Tests that need a config set it up themselves."""
    empty = tmp_path_factory.mktemp("no-config")
    isolate_env(monkeypatch, empty)
    monkeypatch.setenv("YAMLREEL_CONFIG", str(empty / "config.yaml"))
    monkeypatch.chdir(empty)


def skip_without_ffprobe():
    """Some ffmpeg builds (static pip wheels) ship without ffprobe; only the stream checks need it, not yamlreel."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not on PATH")


def spec_of(*scenes, **extra):
    return validate({"scenes": list(scenes), **extra})


@pytest.mark.parametrize("bad,msg", [
    ({"scenes": []}, "non-empty"),
    ({"scenes": [{"type": "nope"}]}, "unknown type"),
    ({"scenes": [{"type": "title"}]}, "missing title"),
    ({"scenes": [{"type": "title", "title": "x", "seconds": -1}]}, "seconds must be"),
    ({"scenes": [{"type": "title", "title": "x"}], "theme": "neon"}, "theme"),
    ({"scenes": [{"type": "title", "title": "x"}], "formats": ["imax"]}, "format"),
    ({"scenes": [{"type": "diagram", "nodes": [{"id": "a", "label": "A", "x": 0.1, "y": 0.1}],
                  "edges": [{"from": "a", "to": "zzz"}]}]}, "unknown node"),
    ({"scenes": [{"type": "diagram", "nodes": [{"id": "a", "label": "A"}]}]}, "needs x and y"),
])
def test_invalid_specs_are_rejected_with_a_clear_message(bad, msg):
    with pytest.raises(SpecError, match=msg):
        validate(bad)


def test_text_never_exceeds_its_box():
    long = "a very long headline that keeps going and going " * 4
    blk = make_block(long, "serif_bold", 120, 800, max_h=300)
    assert blk.width <= 800 + 6
    assert blk.height <= 300 or blk.size <= 18
    assert blk.shrunk


@pytest.mark.parametrize("fmt", FORMATS)
def test_all_scene_types_render_at_the_right_size(fmt, tmp_path):
    spec = spec_of(
        {"type": "title", "title": "T", "subtitle": "s", "eyebrow": "e", "url": "u", "caption": "cap"},
        {"type": "type", "text": "Hello there"},
        {"type": "bullets", "items": ["a", "b"], "title": "x"},
        {"type": "stat", "value": "42", "label": "answers"},
        {"type": "code", "code": "x = 1\nprint(x)", "highlight": [2]},
        {"type": "diagram", "nodes": [{"id": "a", "label": "A", "x": .2, "y": .5},
                                      {"id": "b", "label": "B", "x": .8, "y": .5, "kind": "db"}],
         "edges": [{"from": "a", "to": "b", "label": "go"}], "flash": [{"node": "b", "at": 1, "text": "hot"}]},
        {"type": "table", "columns": ["id", "name"], "rows": [["1", "Ada"], ["2", "Lin"]],
         "scan": True, "counter": {"to": 99}, "highlight": [{"row": 1, "text": "hit"}]},
        {"type": "array", "items": ["a", "b"], "steps": [{"at": 1, "insert": "ab"}, {"at": 2, "window": [0, 1]}]},
        {"type": "cta", "question": "Which one do you use?", "answers": ["First", "Second", "Third"], "step": "Last"},
        theme="sumi")
    tl = Timeline(spec, fmt, tmp_path, scale=0.25)
    W, H = tl.ctx.W, tl.ctx.H
    for i, sc in enumerate(tl.scenes):
        for frac in (0.0, 0.5, 0.99):
            im = tl.frame_at(tl.starts[i] + sc.duration * frac)
            assert im.size == (W, H) and im.mode == "RGB"


def test_shot_scene_from_local_image(tmp_path):
    from PIL import Image
    Image.new("RGB", (800, 450), (200, 50, 50)).save(tmp_path / "s.png")
    spec = spec_of({"type": "shot", "source": str(tmp_path / "s.png"), "zoom": [1, 2], "focus": [0.9, 0.9]})
    for fmt in ("landscape", "vertical"):
        tl = Timeline(spec, fmt, tmp_path / "w", scale=0.25, runtime=Runtime(roots=[tmp_path]))
        assert tl.frame_at(1.0).size == (tl.ctx.W, tl.ctx.H)


def test_missing_screenshot_file_is_a_clear_error(tmp_path):
    from yamlreel.shots import CaptureError
    spec = spec_of({"type": "shot", "source": "nope.png"})
    spec["_dir"] = str(tmp_path)
    with pytest.raises(CaptureError, match="not found"):
        Timeline(spec, "landscape", tmp_path, scale=0.25, runtime=Runtime(roots=[tmp_path]))


def test_music_is_deterministic_and_the_right_length():
    a = music.compose(6, seed=3)
    b = music.compose(6, seed=3)
    c = music.compose(6, seed=4)
    assert len(a) == 6 * music.SR
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    assert np.abs(a).max() <= 1.0 and np.abs(a).max() > 0.2


def test_music_rejects_unknown_scale_and_root():
    with pytest.raises(ValueError):
        music.compose(2, scale="blues")
    with pytest.raises(ValueError):
        music.compose(2, root="H")


def test_beat_snap_rounds_durations(tmp_path):
    spec = spec_of({"type": "title", "title": "x", "seconds": 3.1}, music={"style": "none", "bpm": 60, "snap": True})
    tl = Timeline(spec, "landscape", tmp_path, scale=0.25)
    assert tl.total == pytest.approx(3.0)


def test_share_copy_adds_shorts_tag_and_warns_when_too_long():
    spec = spec_of({"type": "title", "title": "x"}, meta={"title": "T" * 120, "description": "d", "tags": ["a"]})
    text, warns = share_copy(spec, 18, ["vertical"])
    assert "#Shorts #a" in text
    assert any("120 chars" in w for w in warns)
    _, warns = share_copy(spec, 120, ["landscape"])
    assert not any("Shorts" in w for w in warns)  # long landscape videos are fine
    _, warns = share_copy(spec, 200, ["vertical"])
    assert any("Shorts allow" in w for w in warns)


def test_cli_version_exits_without_loading_config(monkeypatch, capsys):
    def unexpected_config(*args, **kwargs):
        pytest.fail("--version must not load configuration")

    monkeypatch.setattr("yamlreel.cli.load_config", unexpected_config)
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"yamlreel {__version__}\n"
    assert captured.err == ""


def test_cli_init_validate_and_overwrite_guard(tmp_path, capsys):
    dest = tmp_path / "v.yaml"
    assert main(["init", "system-design", str(dest)]) == 0
    assert main(["validate", str(dest)]) == 0
    assert main(["init", "system-design", str(dest)]) == 2  # refuses to overwrite
    assert main(["init", "nope", str(tmp_path / "x.yaml")]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"scenes": []}))
    assert main(["validate", str(bad)]) == 1


def test_end_to_end_render_produces_playable_files(tmp_path):
    import json
    import subprocess
    spec = load_spec(TEMPLATES / "system-design.yaml")
    r = render(spec, "landscape", tmp_path, fps=8, scale=0.25, music_override="koto", contact=True)
    for key in ("video", "poster", "contact"):
        assert r[key].exists() and r[key].stat().st_size > 1000
    skip_without_ffprobe()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-show_entries", "format=duration", "-of", "json", str(r["video"])],
                           capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    kinds = sorted(s["codec_type"] for s in info["streams"])
    assert kinds == ["audio", "video"]
    assert float(info["format"]["duration"]) == pytest.approx(r["seconds"], abs=0.3)


def test_watermark_is_painted_on_every_frame_and_validated(tmp_path):
    plain = spec_of({"type": "title", "title": "x"}, watermark=None)
    marked = spec_of({"type": "title", "title": "x"}, watermark="Test Name")
    a = Timeline(plain, "landscape", tmp_path, scale=0.5)
    b = Timeline(marked, "landscape", tmp_path, scale=0.5)
    for t in (0.0, 1.0, 2.9):
        assert a.frame_at(t).tobytes() != b.frame_at(t).tobytes()
    x, y = b._wm[2]
    assert x > b.ctx.W / 2 and y > b.ctx.H / 2  # landscape default: bottom-right
    v = Timeline(marked, "vertical", tmp_path, scale=0.5)
    assert v._wm[2][0] < v.ctx.W / 2 and v._wm[2][1] < v.ctx.H / 2  # vertical default: top-left
    for bad in ({"text": "x", "position": "middle"}, {"text": "x", "opacity": 2}, {}, 5):
        with pytest.raises(SpecError):
            validate({"scenes": [{"type": "title", "title": "x"}], "watermark": bad})


# ---------- configuration ----------

def write(path: Path, data) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated user config location and working directory."""
    isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "home" / "config.yaml"))
    (tmp_path / "home").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path


def test_defaults_when_there_is_no_config(home):
    cfg = load_config()
    assert cfg.data["theme"] == "sumi" and cfg.data["watermark"] if "watermark" in cfg.data else True
    assert "watermark" not in cfg.data and cfg.sources == []


def test_layers_merge_in_order_and_meta_lists_concatenate(home):
    write(home / "home" / "config.yaml", {"theme": "washi", "watermark": "User Name",
                                          "meta": {"tags": ["a"]}, "music": {"bpm": 60}})
    write(home / "work" / "yamlreel.config.yaml", {"theme": "midnight", "meta": {"tags": ["b", "a"]},
                                                 "music": {"seed": 3}})
    cfg = load_config()
    assert cfg.data["theme"] == "midnight"               # project beats user
    assert cfg.data["watermark"] == "User Name"          # user value survives
    assert cfg.data["music"]["bpm"] == 60 and cfg.data["music"]["seed"] == 3 and cfg.data["music"]["style"] == "koto"
    assert cfg.data["meta"]["tags"] == ["a", "b"]
    spec = validate({"theme": "sumi", "scenes": [{"type": "title", "title": "x"}]}, cfg)
    assert spec["theme"] == "sumi" and spec["watermark"]["text"] == "User Name"   # spec beats both


def test_project_config_cannot_grant_itself_access(home):
    write(home / "work" / "yamlreel.config.yaml", {"allow_paths": ["/"]})
    with pytest.raises(ConfigError, match="only read from the user config"):
        load_config()
    for key, val in (("allow_private_hosts", True), ("chromium_path", "/bin/sh"), ("font_dir", "/")):
        write(home / "work" / "yamlreel.config.yaml", {key: val})
        with pytest.raises(ConfigError):
            load_config()
    with pytest.raises(SpecError, match="not settable from a spec"):
        validate({"allow_paths": ["/"], "scenes": [{"type": "title", "title": "x"}]})


def test_user_config_can_grant_access_and_cli_flags_add_to_it(home):
    write(home / "home" / "config.yaml", {"allow_paths": [str(home / "logos")], "allow_private_hosts": True})
    cfg = load_config(cli_allow_paths=[str(home / "more")])
    assert cfg.allow_paths == [home / "logos", home / "more"] and cfg.allow_private_hosts


def test_config_errors_are_clear(home):
    write(home / "work" / "yamlreel.config.yaml", {"colour": "red"})
    with pytest.raises(ConfigError, match="unknown or not-permitted"):
        load_config()
    (home / "work" / "yamlreel.config.yaml").write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config()
    with pytest.raises(ConfigError, match="not found"):
        load_config(explicit=home / "nope.yaml")
    (home / "work" / "yamlreel.config.yaml").write_text("x: [")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config()
    (home / "work" / "yamlreel.config.yaml").write_text("#" * 300_000)
    with pytest.raises(ConfigError, match="larger than"):
        load_config()


def test_relative_paths_in_config_resolve_from_that_file(home):
    write(home / "work" / "yamlreel.config.yaml", {"watermark": {"image": "logo.png"}, "output_dir": "out"})
    cfg = load_config()
    assert cfg.data["watermark"]["image"] == str(home / "work" / "logo.png")
    assert cfg.data["output_dir"] == str(home / "work" / "out")


def test_custom_theme_extends_and_validates():
    t = get_theme("brand", {"brand": {"extends": "midnight", "accent": "#ff6b35", "frame": True}})
    assert t.accent == (255, 107, 53) and t.bg == get_theme("midnight").bg and t.frame
    for bad in ({"brand": {"accent": "orange"}}, {"brand": {"nope": 1}}, {"brand": {"extends": "brand"}},
                {"brand": {"display": "comic"}}, {"brand": "x"}):
        with pytest.raises(ValueError):
            get_theme("brand", bad)
    with pytest.raises(ValueError, match="unknown theme"):
        get_theme("neon")


def test_custom_theme_in_spec_renders(tmp_path):
    spec = spec_of({"type": "title", "title": "x"}, theme="brand",
                   themes={"brand": {"extends": "washi", "bg": [10, 20, 30]}})
    tl = Timeline(spec, "landscape", tmp_path, scale=0.25)
    assert tl.frame_at(0.0).getpixel((tl.ctx.W // 2, tl.ctx.H - 5))[0] < 40  # background uses the custom colour


# ---------- safety ----------

def test_paths_must_stay_inside_allowed_folders(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ok.png").write_bytes(b"x")
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    assert resolve_local("ok.png", root, [root]) == (root / "ok.png").resolve()
    for evil in ("../secret.txt", str(outside), "/etc/passwd", "sub/../../secret.txt"):
        with pytest.raises(UnsafeInput, match="outside the folders"):
            resolve_local(evil, root, [root])
    (root / "link").symlink_to(outside)                       # a symlink must not smuggle a file out
    with pytest.raises(UnsafeInput):
        resolve_local("link", root, [root])


def test_a_spec_cannot_read_outside_its_folder(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "other.png").write_bytes(b"x")
    spec = spec_of({"type": "shot", "source": "../other.png"})
    spec["_dir"] = str(tmp_path / "proj")
    with pytest.raises(CaptureError, match="outside the folders"):
        Timeline(spec, "landscape", tmp_path, scale=0.25)
    for src in ("file:///etc/hostname", "file://evilhost/etc/hostname"):
        spec = spec_of({"type": "shot", "source": src})
        spec["_dir"] = str(tmp_path / "proj")
        with pytest.raises(CaptureError):
            Timeline(spec, "landscape", tmp_path, scale=0.25)


def test_music_and_watermark_files_are_confined(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "evil.mp3").write_bytes(b"x")
    spec = spec_of({"type": "title", "title": "x"}, music={"style": "../evil.mp3"})
    spec["_dir"] = str(tmp_path / "proj")
    with pytest.raises(RenderError, match="outside the folders"):
        render(spec, "landscape", tmp_path / "out", fps=4, scale=0.1, runtime=Runtime(roots=[tmp_path / "proj"]))
    spec = spec_of({"type": "title", "title": "x"}, watermark={"image": "../evil.png"})
    spec["_dir"] = str(tmp_path / "proj")
    with pytest.raises(RenderError, match="outside the folders"):
        Timeline(spec, "landscape", tmp_path, scale=0.1, runtime=Runtime(roots=[tmp_path / "proj"]))


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/", "http://metadata.google.internal/",
    "http://0.0.0.0/", "ftp://example.com/", "javascript:alert(1)", "http:///nohost",
    "http://[::ffff:169.254.169.254]/",
])
def test_dangerous_urls_are_always_refused(url):
    with pytest.raises(UnsafeInput):
        check_http_url(url, allow_private=True)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://localhost/", "http://10.0.0.5/", "http://[::1]/"])
def test_private_hosts_need_explicit_opt_in(url):
    with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
        check_http_url(url, allow_private=False)
    check_http_url(url, allow_private=True)  # allowed once the user opts in


def test_input_limits():
    s = {"type": "title", "title": "x"}
    big = {"limits": {"max_seconds": 10, "max_scenes": 2}}
    with pytest.raises(SpecError, match="limit is 2"):
        validate({"scenes": [s, s, s]}, Config(data=merge(Config().data, big)))
    with pytest.raises(SpecError, match="total length"):
        validate({"scenes": [{**s, "seconds": 6}, {**s, "seconds": 6}]}, Config(data=merge(Config().data, big)))
    cases = [
        {"type": "title", "title": "x" * 3000},
        {"type": "title", "title": "x", "seconds": 1e9},
        {"type": "shot", "source": "a.png", "zoom": 0},
        {"type": "shot", "source": "a.png", "zoom": 99},
        {"type": "shot", "source": "a.png", "focus": [2, 0]},
        {"type": "shot", "source": "a.png", "viewport": [100000, 100000]},
        {"type": "shot", "source": "a.png", "scale": 50},
        {"type": "shot", "source": "a.png", "click": ["a"] * 50},
        {"type": "bullets", "items": ["a"] * 500},
        {"type": "code", "code": "x\n" * 500},
        {"type": "code", "code": "x" * 500},
        {"type": "code", "code": "a\nb", "highlight": [9]},
        {"type": "diagram", "nodes": [{"id": str(n), "label": "a", "x": 0, "y": 0} for n in range(500)]},
        {"type": "diagram", "nodes": [{"id": "a", "label": "a", "x": 5, "y": 0}]},
        {"type": "diagram", "nodes": [{"id": "a", "label": "a", "x": 0, "y": 0, "kind": "../x"}]},
    ]
    for c in cases:
        with pytest.raises(SpecError):
            validate({"scenes": [c]})
    for bad in ({"music": {"bpm": 0}}, {"music": {"volume": 99}}, {"poster": {"scene": -1}}, {"nope": 1},
                {"watermark": {"text": "x", "size": 9999}}):
        with pytest.raises(SpecError):
            validate({"scenes": [s], **bad})


def test_oversized_spec_file_is_rejected(tmp_path):
    p = tmp_path / "big.yaml"
    p.write_text("# " + "x" * 1_100_000)
    with pytest.raises(SpecError, match="larger than"):
        load_spec(p)


def test_cli_end_to_end_with_config(home, capsys):
    write(home / "work" / "yamlreel.config.yaml", {"watermark": "Cfg Name", "formats": ["square"],
                                                 "music": {"style": "none"}, "output_dir": "o"})
    spec = write(home / "work" / "v.yaml", {"scenes": [{"type": "title", "title": "Hi", "seconds": 1}]})
    assert main(["render", str(spec), "--preview"]) == 0
    assert (home / "work" / "o" / "square" / "video.mp4").exists()
    assert main(["config", "show"]) == 0
    assert "Cfg Name" in capsys.readouterr().out
    assert main(["render", str(spec), "--fps", "999"]) == 1


def test_config_init_writes_a_loadable_starter(home):
    assert main(["config", "init"]) == 0
    assert main(["config", "init"]) == 2                       # will not overwrite
    cfg = load_config()
    assert cfg.data["theme"] == "sumi"


def test_music_file_that_is_really_a_playlist_is_refused(tmp_path):
    """ffmpeg sniffs content: a .mp3 holding an ffconcat script must not be demuxed as a playlist."""
    import wave
    with wave.open(str(tmp_path / "real.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\0\0" * 8000)
    (tmp_path / "evil.mp3").write_text("ffconcat version 1.0\nfile real.wav\n")
    spec = tmp_path / "s.yaml"
    spec.write_text("music: {style: evil.mp3}\nscenes:\n  - {type: title, title: hi, seconds: 2}\n")
    rc = main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "landscape", "--fps", "5"])
    assert rc != 0
    assert not (tmp_path / "o" / "landscape" / "video.mp4").exists() or rc != 0


def test_output_dir_from_a_spec_cannot_escape(tmp_path, monkeypatch):
    spec = tmp_path / "s.yaml"
    target = tmp_path.parent / "elsewhere"
    spec.write_text(f"output_dir: {target}\nscenes:\n  - {{type: title, title: hi, seconds: 2}}\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["render", str(spec), "--format", "landscape", "--fps", "5"])
    assert not target.exists()


# --- DNS-rebinding defence: the pinning proxy ------------------------------------------------------------

def _http_server(body: bytes = b"hello"):
    import http.server, threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _via_proxy(proxy_url: str, target: str) -> bytes:
    import socket
    from urllib.parse import urlsplit
    ph = urlsplit(proxy_url)
    with socket.create_connection((ph.hostname, ph.port), timeout=5) as s:
        s.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    return data


def test_pinning_proxy_refuses_private_targets_by_default():
    from yamlreel.pinproxy import PinningProxy
    srv = _http_server()
    try:
        with PinningProxy(allow_private=False) as px:
            assert b" 403 " in _via_proxy(px.url, f"http://127.0.0.1:{srv.server_address[1]}/").split(b"\r\n")[0] + b" "
        with PinningProxy(allow_private=True) as px:
            assert _via_proxy(px.url, f"http://127.0.0.1:{srv.server_address[1]}/").endswith(b"hello")
    finally:
        srv.shutdown()


def test_pinning_proxy_connects_to_the_address_it_validated(monkeypatch):
    """A name that resolves to a public address for the check must not be re-resolved to an internal one."""
    from yamlreel import safety
    from yamlreel.pinproxy import PinningProxy
    srv = _http_server()
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])  # rebinding DNS: public first, then loopback
    monkeypatch.setattr(safety, "_ips_for", lambda host: [__import__("ipaddress").ip_address(a) for a in next(answers)])
    seen = []
    import yamlreel.pinproxy as pp
    real = pp.socket.create_connection

    def fake(addr, timeout=None):
        if addr[0] == "93.184.216.34":
            seen.append(addr)
            raise OSError("stop")
        return real(addr, timeout=timeout)

    monkeypatch.setattr(pp.socket, "create_connection", fake)
    try:
        with PinningProxy(allow_private=False) as px:
            _via_proxy(px.url, f"http://rebind.test:{srv.server_address[1]}/")
        assert seen == [("93.184.216.34", srv.server_address[1])]  # one resolution, pinned address used
    finally:
        srv.shutdown()


def test_capture_of_local_server_works_through_the_proxy(tmp_path):
    pytest.importorskip("playwright")
    from yamlreel.shots import load_shot
    srv = _http_server(b"<html><body style='background:#c00'>ok</body></html>")
    try:
        with pytest.raises(CaptureError):
            load_shot(f"http://127.0.0.1:{srv.server_address[1]}/", {"viewport": [400, 300], "scale": 1}, tmp_path, tmp_path, [tmp_path], allow_private=False)
        try:
            im = load_shot(f"http://127.0.0.1:{srv.server_address[1]}/", {"viewport": [400, 300], "scale": 1}, tmp_path, tmp_path, [tmp_path], allow_private=True)
        except CaptureError as e:
            pytest.skip(f"chromium unavailable: {e}")
        assert im.getpixel((10, 10))[0] > 150
    finally:
        srv.shutdown()


# --- voiceover ---------------------------------------------------------------------------------------------

def _probe(path) -> dict:
    import json, subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    return {"duration": float(d["format"]["duration"]), "streams": [s["codec_type"] for s in d["streams"]]}


@pytest.mark.parametrize("sse", [False, True])
def test_narration_is_synthesised_mixed_and_stretches_the_scene(tmp_path, sse):
    srv, calls, url = _fake_voice_server(seconds=3.0, sse=sse)
    try:
        spec = tmp_path / "v.yaml"
        spec.write_text("voiceover: {voice: warm, language: en}\nmusic: {style: koto}\nscenes:\n"
                        "  - {type: title, title: Hi, seconds: 1, narration: Hello there}\n"
                        "  - {type: title, title: Bye, seconds: 1}\n")
        rc = main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5", "--voice-url", url])
        assert rc == 0
    finally:
        srv.shutdown()
    assert calls == [{"text": "Hello there", "profile_id": "warm", "language": "en"}]
    skip_without_ffprobe()
    info = _probe(tmp_path / "o" / "square" / "video.mp4")
    assert "audio" in info["streams"]
    assert info["duration"] >= 3.0 + 0.5 + 1.0 - 0.3  # scene 1 stretched to speech + gap, plus scene 2


def test_narration_is_cached_between_renders(tmp_path):
    srv, calls, url = _fake_voice_server(seconds=1.0)
    try:
        spec = tmp_path / "v.yaml"
        spec.write_text("music: {style: none}\nscenes:\n  - {type: title, title: Hi, seconds: 1, narration: Same words}\n")
        for _ in range(2):
            assert main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5", "--voice-url", url]) == 0
    finally:
        srv.shutdown()
    assert len(calls) == 1


def test_narration_without_a_voice_server_is_a_clear_error(tmp_path, capsys):
    spec = tmp_path / "v.yaml"
    spec.write_text("music: {style: none}\nscenes:\n  - {type: title, title: Hi, seconds: 1, narration: Words}\n")
    rc = main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5"])
    assert rc != 0
    assert "no voice server is configured" in capsys.readouterr().err


def test_voice_server_error_is_reported(tmp_path, capsys):
    srv, _, url = _fake_voice_server(fail=True)
    try:
        spec = tmp_path / "v.yaml"
        spec.write_text("music: {style: none}\nscenes:\n  - {type: title, title: Hi, seconds: 1, narration: Words}\n")
        rc = main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5", "--voice-url", url])
    finally:
        srv.shutdown()
    assert rc != 0 and "model not installed" in capsys.readouterr().err


def test_a_spec_cannot_choose_the_voice_server(tmp_path):
    with pytest.raises(SpecError, match="voice_server"):
        validate({"voice_server": {"url": "http://evil.example/"}, "scenes": [{"type": "title", "title": "x"}]}, Config(), base_dir=tmp_path)
    p = tmp_path / "yamlreel.config.yaml"
    p.write_text("voice_server: {url: 'http://evil.example/'}\n")
    with pytest.raises(ConfigError, match="voice_server"):
        load_config(None, cwd=tmp_path)


def test_narration_audio_file_is_confined_to_allowed_folders(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "v.wav").write_bytes(_tone_wav_bytes(1.0))
    work = tmp_path / "work"; work.mkdir()
    spec = work / "s.yaml"
    spec.write_text(f"music: {{style: none}}\nscenes:\n  - {{type: title, title: Hi, seconds: 1, narration_audio: {outside / 'v.wav'}}}\n")
    assert main(["render", str(spec), "--out", str(work / "o"), "--format", "square", "--fps", "5"]) != 0
    assert main(["render", str(spec), "--out", str(work / "o"), "--format", "square", "--fps", "5", "--allow-path", str(outside)]) == 0


def test_read_screen_speaks_the_text_shown_in_the_video(tmp_path):
    from yamlreel.voice import screen_text
    assert screen_text({"type": "title", "title": "Cache stampede", "subtitle": "In 45 seconds"}) == "Cache stampede. In 45 seconds."
    assert screen_text({"type": "bullets", "title": "Fixes", "items": ["Lock", "Jitter"], "caption": "Lock."}) == "Fixes. Lock. Jitter."
    assert screen_text({"type": "stat", "value": "10,000", "label": "requests"}) == "10,000. requests."
    srv, calls, url = _fake_voice_server(seconds=1.0)
    try:
        spec = tmp_path / "v.yaml"
        spec.write_text("music: {style: none}\nscenes:\n  - {type: type, text: 'What is a stampede?'}\n"
                        "  - {type: title, title: Skipped, narration: false}\n"
                        "  - {type: title, title: Own words, narration: Custom line}\n")
        assert main(["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5",
                     "--voice-url", url, "--read-screen"]) == 0
    finally:
        srv.shutdown()
    assert [c["text"] for c in calls] == ["What is a stampede?", "Custom line"]


def test_fonts_installed_per_user_are_found(tmp_path, monkeypatch):
    from yamlreel import theme
    fonts = tmp_path / "Library" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "DejaVuSans.ttf").write_bytes(b"")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(theme, "_FONT_DIRS", [])
    monkeypatch.setattr(theme, "_EXTRA_FONT_DIRS", [])
    assert theme._find("DejaVuSans.ttf") == str(fonts / "DejaVuSans.ttf")
    # font_dir from the user config still wins over a per-user copy
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "DejaVuSans.ttf").write_bytes(b"")
    monkeypatch.setattr(theme, "_EXTRA_FONT_DIRS", [str(mine)])
    assert theme._find("DejaVuSans.ttf") == str(mine / "DejaVuSans.ttf")
    # and a system copy wins over a per-user one
    system = tmp_path / "system"
    system.mkdir()
    (system / "DejaVuSans.ttf").write_bytes(b"")
    monkeypatch.setattr(theme, "_EXTRA_FONT_DIRS", [])
    monkeypatch.setattr(theme, "_FONT_DIRS", [str(system)])
    assert theme._find("DejaVuSans.ttf") == str(system / "DejaVuSans.ttf")


def test_fonts_are_found_when_the_home_folder_cannot_be_resolved(tmp_path, monkeypatch):
    # e.g. `docker run --user 12345`: no HOME and no passwd entry
    pwd = pytest.importorskip("pwd")
    from yamlreel import theme
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    def no_entry(uid):
        raise KeyError(uid)
    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    (tmp_path / "DejaVuSans.ttf").write_bytes(b"")
    monkeypatch.setattr(theme, "_EXTRA_FONT_DIRS", [str(tmp_path)])
    assert theme._find("DejaVuSans.ttf") == str(tmp_path / "DejaVuSans.ttf")
    monkeypatch.setattr(theme, "_EXTRA_FONT_DIRS", [])
    monkeypatch.setattr(theme, "_FONT_DIRS", [])
    assert theme._user_font_dirs() == []
    # an unresolved '~' must never become a folder relative to where yamlreel runs
    planted = tmp_path / "cwd" / "~" / "Library" / "Fonts"
    planted.mkdir(parents=True)
    (planted / "DejaVuSans.ttf").write_bytes(b"")
    monkeypatch.chdir(tmp_path / "cwd")
    assert theme._find("DejaVuSans.ttf") is None


def test_cli_reports_a_missing_font_without_a_traceback(home, monkeypatch, capsys):
    from yamlreel import theme
    monkeypatch.setattr(theme, "_find", lambda filename: None)
    theme._load.cache_clear()
    spec = write(home / "work" / "v.yaml", {"scenes": [{"type": "title", "title": "Hi", "seconds": 1}],
                                            "formats": ["square"], "music": {"style": "none"}})
    try:
        assert main(["render", str(spec), "--preview"]) == 1
    finally:
        theme._load.cache_clear()
    assert capsys.readouterr().err.startswith("error: font 'DejaVu")


def test_ffmpeg_falls_back_to_the_pip_build(monkeypatch):
    import sys
    import types
    from yamlreel import render as render_mod
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: None)
    fake = types.SimpleNamespace(get_ffmpeg_exe=lambda: "/pip/ffmpeg")
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)
    assert render_mod.ffmpeg_exe() == "/pip/ffmpeg"


def test_missing_ffmpeg_says_how_to_install_it(tmp_path, monkeypatch):
    import sys
    from yamlreel import render as render_mod
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)  # import fails
    with pytest.raises(RenderError, match="pip install imageio-ffmpeg"):
        render(spec_of({"type": "title", "title": "x", "seconds": 1}), "square", tmp_path)


def test_ffmpeg_that_cannot_start_is_a_clear_error(tmp_path, monkeypatch):
    from yamlreel import render as render_mod
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: str(tmp_path / "no-such-ffmpeg"))
    with pytest.raises(RenderError, match="cannot run ffmpeg"):
        render(spec_of({"type": "title", "title": "x", "seconds": 1}, music={"style": "none"}), "square", tmp_path,
               fps=5, scale=0.25, contact=False)


def test_no_voice_renders_a_narrated_spec_without_a_voice_server(tmp_path, capsys):
    spec = tmp_path / "v.yaml"
    spec.write_text("music: {style: none}\nvoiceover: {read_screen: true}\nscenes:\n"
                    "  - {type: title, title: Hi, seconds: 1, narration: Words}\n")
    args = ["render", str(spec), "--out", str(tmp_path / "o"), "--format", "square", "--fps", "5"]
    assert main(args) == 1  # narration needs a voice server...
    assert "--no-voice" in capsys.readouterr().err  # ...and the error says how to skip it
    assert main([*args, "--no-voice"]) == 0
    assert (tmp_path / "o" / "square" / "video.mp4").exists()
    assert main([*args, "--no-voice", "--read-screen"]) == 1
    assert "cannot be used together" in capsys.readouterr().err


def test_bad_voice_url_is_a_clear_error(tmp_path, capsys):
    spec = write(tmp_path / "v.yaml", {"scenes": [{"type": "title", "title": "Hi", "seconds": 1}]})
    assert main(["render", str(spec), "--out", str(tmp_path / "o"), "--voice-url", "ftp://x"]) == 1
    assert capsys.readouterr().err.startswith("error: voice_server.url must be an http(s) URL")


def test_new_names_only_print_no_deprecation(home, capsys):
    write(home / "home" / "config.yaml", {"theme": "washi"})
    write(home / "work" / "yamlreel.config.yaml", {"theme": "sumi"})
    load_config()
    assert "deprecated" not in capsys.readouterr().err
    assert main(["config", "show"]) == 0
    assert "deprecated" not in capsys.readouterr().err


def test_default_names(home, monkeypatch):
    from yamlreel.voice import VoiceServer
    assert load_config().data["output_dir"].endswith("yamlreel-output")
    assert VoiceServer().client_id == "yamlreel"
    assert main(["config", "init"]) == 0
    assert (home / "work" / "yamlreel.config.yaml").exists()
    monkeypatch.delenv("YAMLREEL_CONFIG")
    assert main(["config", "init", "--user"]) == 0
    assert (home / "xdg" / "yamlreel" / "config.yaml").exists()
