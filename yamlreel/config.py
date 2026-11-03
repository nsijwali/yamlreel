"""Layered configuration.

Precedence, lowest to highest: built-in defaults < user config < project config < the spec itself.
CLI flags override all of them.

  user config     $YAMLREEL_CONFIG, else $XDG_CONFIG_HOME/yamlreel/config.yaml (~/.config/yamlreel/config.yaml)
  project config  --config PATH, else ./yamlreel.config.yaml

One file is used per slot, never a merge.

Settings that widen what a spec may touch (`allow_paths`, `allow_private_hosts`, `chromium_path`) are only
honoured from the user config or the command line. A project config or spec that you downloaded cannot
grant itself access to your files or network.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


MAX_CONFIG_BYTES = 256 * 1024
CONFIG_FILENAME = "yamlreel.config.yaml"

# keys any config layer (and the spec) may set
SHARED_KEYS = {"theme", "themes", "formats", "watermark", "music", "meta", "poster", "fps", "output_dir", "limits", "voiceover", "transition", "subtitles"}
# keys only the user config / CLI may set (they widen access)
TRUSTED_KEYS = {"allow_paths", "allow_private_hosts", "chromium_path", "font_dir", "voice_server"}

DEFAULTS: dict[str, Any] = {
    "theme": "sumi",
    "formats": ["landscape"],
    "music": {"style": "koto", "seed": 7, "volume": 0.9, "snap": False},   # bpm, root and scale come from the style (music.STYLE_DEFAULTS)
    "meta": {"tags": [], "links": [], "credits": []},
    "fps": 30,
    "output_dir": "yamlreel-output",
    "limits": {"max_seconds": 600, "max_scenes": 200},
    "transition": {"type": "fade", "seconds": 0.25},
    "subtitles": False,
}
HARD_LIMITS = {"max_seconds": 3600, "max_scenes": 1000}
MAX_CLIENT_ID_CHARS = 200
VOICE_SERVER_KEYS = ("url", "client_id", "timeout")
PATH_KEYS = {("watermark", "image"), ("output_dir",), ("font_dir",), ("chromium_path",)}


class ConfigError(ValueError):
    pass


def describe_key(key: object) -> str:
    """A mapping key as it can safely appear in a one-line error message (never walks a nested value)."""
    if isinstance(key, str):
        text = key
    elif key is None or isinstance(key, (bool, int, float)):
        text = repr(key)
    else:
        return type(key).__name__
    return text if text.isprintable() and len(text) <= 60 else repr(text[:60]) + ("..." if len(text) > 60 else "")


def describe_value(v: object) -> str:
    """Any value, quoted if it is text, short and on one line. Collections are named, never printed."""
    if isinstance(v, str):
        return repr(v[:60]) + ("..." if len(v) > 60 else "")
    return describe_key(v)


def yaml_problem(e: yaml.YAMLError) -> str:
    """A YAML error on one line: the problem and where it is."""
    if isinstance(e, yaml.MarkedYAMLError):
        mark = e.problem_mark or e.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return " ".join(f"{e.problem or 'invalid YAML'}{where}".split())
    return " ".join(str(e).split())


def unknown_key(path: str, key: object, allowed: Any, what: str, exc: type[Exception] = ValueError,
                problem: str = "unknown field", note: str = "") -> Exception:
    """The error for a key that `what` does not have. The one place the "did you mean" hook (issue #13) plugs in,
    for specs, configs, limits, voice_server and theme entries alike."""
    from . import specs  # late: specs imports this module
    return specs._unknown_key(path, key, allowed, what, exc, problem, note)


def normalize_music(d: dict[str, Any]) -> None:
    """`music: koto` is shorthand for `music: {style: koto}`. Done per layer, before paths and merging, so a
    shorthand in one layer keeps the other settings from the layers below it."""
    if isinstance(d.get("music"), str):
        d["music"] = {"style": d["music"]}


def clamp_limits(raw: Any) -> dict[str, Any]:
    """Validate `limits` and cap each value at its absolute limit (HARD_LIMITS). Raises ValueError.

    Clamping, not an error, for a value above the cap: configs that set a larger number keep working."""
    if not isinstance(raw, dict):
        raise ValueError("limits must be a mapping")
    for key in raw:
        if key not in HARD_LIMITS:
            raise unknown_key("limits", key, HARD_LIMITS, "limits")
    out: dict[str, Any] = {}
    for key, hard in HARD_LIMITS.items():
        v = raw.get(key, DEFAULTS["limits"][key])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
            raise ValueError(f"limits.{key} must be a positive number")
        if key == "max_scenes" and not isinstance(v, int):
            raise ValueError("limits.max_scenes must be a positive integer")
        out[key] = min(v, hard)
    return out


def check_voice_server(vs: dict[str, Any]) -> None:
    """Type and range of the voice_server settings. Raises ValueError."""
    for key in vs:
        if key not in VOICE_SERVER_KEYS:
            raise unknown_key("voice_server", key, VOICE_SERVER_KEYS, "voice_server")
    if "url" in vs and (not isinstance(vs["url"], str) or not vs["url"].strip()):
        raise ValueError("voice_server.url must be a URL string")
    cid = vs.get("client_id", "x")
    if not isinstance(cid, str) or not cid.strip() or len(cid) > MAX_CLIENT_ID_CHARS:
        raise ValueError(f"voice_server.client_id must be a non-empty string of at most {MAX_CLIENT_ID_CHARS} characters")
    t = vs.get("timeout", 1)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 1 <= t <= 3600:
        raise ValueError("voice_server.timeout must be a number of seconds between 1 and 3600")


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    allow_paths: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium_path: str | None = None
    font_dirs: list[str] = field(default_factory=list)
    voice_server: dict[str, Any] | None = None  # {url, client_id}; user config / CLI only
    sources: list[Path] = field(default_factory=list)


def config_base() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME")  # per the XDG spec, a relative path is ignored
    return xdg if xdg and os.path.isabs(xdg) else str(Path.home() / ".config")


def _env(suffix: str) -> str | None:
    """$YAMLREEL_<suffix> if set and non-empty, else None."""
    return os.environ.get(f"YAMLREEL_{suffix}") or None


def user_config_path() -> Path:
    """Where the user config is written (config init --user)."""
    explicit = _env("CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(config_base()) / "yamlreel" / "config.yaml"


def find_user_config() -> Path:
    """Where the user config is read from (the same path it is written to)."""
    explicit = _env("CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(config_base())
    return base / "yamlreel" / "config.yaml"


def _read(path: Path) -> dict[str, Any]:
    try:
        too_big = path.stat().st_size > MAX_CONFIG_BYTES
        text = "" if too_big else path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"cannot read config {path}: not valid UTF-8 ({e.reason})") from e
    if too_big:
        raise ConfigError(f"{path}: config file is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {yaml_problem(e)}") from e
    except (ValueError, RecursionError) as e:  # a number or date the parser cannot hold, or nesting too deep
        raise ConfigError(f"{path}: invalid YAML: {' '.join(str(e).split()) or 'nested too deeply'}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: config must be a mapping")
    return raw


def _absolutize(d: dict[str, Any], base: Path) -> None:
    """Resolve path-valued keys relative to the file that set them."""
    for keys in PATH_KEYS:
        node: Any = d
        for k in keys[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get(keys[-1]), str):
            p = Path(node[keys[-1]]).expanduser()
            node[keys[-1]] = str(p if p.is_absolute() else (base / p))
    music = d.get("music")
    if isinstance(music, dict) and isinstance(music.get("style"), str) and Path(music["style"]).suffix:
        p = Path(music["style"]).expanduser()
        music["style"] = str(p if p.is_absolute() else (base / p))


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge mappings; replace everything else. meta.tags/links/credits concatenate."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "meta" and isinstance(v, dict) and isinstance(out.get("meta"), dict):
            meta = dict(out["meta"])
            for mk, mv in v.items():
                if mk in ("tags", "links", "credits") and isinstance(mv, list):
                    merged = list(meta.get(mk, []))
                    merged += [x for x in mv if x not in merged]
                    meta[mk] = merged
                else:
                    meta[mk] = copy.deepcopy(mv)
            out["meta"] = meta
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _check_keys(path: Path, raw: dict[str, Any], trusted: bool) -> None:
    allowed = SHARED_KEYS | (TRUSTED_KEYS if trusted else set())
    for key in raw:  # file order: the first mistake is the one reported
        if key not in allowed:
            note = ""
            if isinstance(key, str) and key in TRUSTED_KEYS:
                note = "allow_paths, allow_private_hosts, chromium_path, font_dir and voice_server are only read from the user config or the CLI"
            raise unknown_key("", key, allowed, f"config {path}", ConfigError, "unknown or not-permitted field", note)


def load_config(explicit: str | Path | None = None, cwd: Path | None = None,
                cli_allow_paths: list[str] | None = None, cli_allow_private_hosts: bool = False,
                cli_voice_url: str | None = None) -> Config:
    cfg = Config()
    user = find_user_config()
    layers: list[tuple[Path, bool]] = []
    if user.exists():
        layers.append((user, True))
    project = Path(explicit).expanduser() if explicit else (cwd or Path.cwd()) / CONFIG_FILENAME
    if explicit and not project.exists():
        raise ConfigError(f"config file not found: {project}")
    if project.exists() and (not user.exists() or project.resolve() != user.resolve()):
        layers.append((project, False))
    for path, trusted in layers:
        raw = _read(path)
        _check_keys(path, raw, trusted)
        normalize_music(raw)
        _absolutize(raw, path.resolve().parent)
        if trusted:
            for p in raw.pop("allow_paths", []) or []:
                cfg.allow_paths.append(Path(os.path.expanduser(str(p))))
            cfg.allow_private_hosts = bool(raw.pop("allow_private_hosts", cfg.allow_private_hosts))
            cfg.chromium_path = raw.pop("chromium_path", cfg.chromium_path)
            vs = raw.pop("voice_server", None)
            if vs is not None:
                if not isinstance(vs, dict) or set(vs) - {"url", "client_id", "timeout"}:
                    raise ConfigError(f"{path}: voice_server is {{url, client_id, timeout}}")
                cfg.voice_server = {**(cfg.voice_server or {}), **vs}
            fd = raw.pop("font_dir", None)
            if fd:
                cfg.font_dirs.append(str(fd))
        cfg.data = merge(cfg.data, raw)
        cfg.sources.append(path)
    cfg.allow_paths += [Path(os.path.expanduser(p)) for p in (cli_allow_paths or [])]
    cfg.allow_private_hosts = cfg.allow_private_hosts or cli_allow_private_hosts
    if cli_voice_url:
        cfg.voice_server = {**(cfg.voice_server or {}), "url": cli_voice_url}
    cfg.chromium_path = cfg.chromium_path or _env("CHROMIUM")
    font_dir = _env("FONT_DIR")
    if font_dir:
        cfg.font_dirs.append(font_dir)
    try:
        cfg.data["limits"] = clamp_limits(cfg.data.get("limits"))
        if cfg.voice_server is not None:
            check_voice_server(cfg.voice_server)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    return cfg
