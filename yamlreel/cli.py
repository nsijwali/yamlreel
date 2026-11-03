"""Command line interface: yamlreel render|validate|init|config|music."""
from __future__ import annotations

import argparse
import shutil
import sys
from importlib import resources
from pathlib import Path

import yaml

from . import __version__, legacy
from .config import CONFIG_FILENAME, config_base, Config, ConfigError, load_config, user_config_path
from .voice import VoiceError, VoiceServer
from .render import MAX_CONTACT_FRAMES, RenderError, Runtime, render, share_copy
from .safety import UnsafeInput
from .shots import CaptureError
from .specs import FORMATS, SpecError, check_frame_budget, load_spec
from .theme import FontError, set_font_dirs


def _templates() -> list[str]:
    root = resources.files("yamlreel") / "templates"
    return sorted(p.name[:-5] for p in root.iterdir() if p.name.endswith(".yaml") and not p.name.startswith("config"))


def _config(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config, cli_allow_paths=args.allow_path, cli_allow_private_hosts=args.allow_private_hosts,
                      cli_voice_url=getattr(args, "voice_url", None))
    set_font_dirs(cfg.font_dirs)
    return cfg


def _runtime(spec: dict, cfg: Config, music_override: str | None = None, no_voice: bool = False) -> Runtime:
    roots = [Path(spec["_dir"]), *[p.parent for p in cfg.sources], *cfg.allow_paths]
    if music_override and music_override not in ("koto", "none"):
        roots.append(Path(music_override).expanduser().resolve().parent)  # named on the command line: trusted
    voice = VoiceServer(**cfg.voice_server) if cfg.voice_server and not no_voice else None
    return Runtime(roots=roots, allow_private_hosts=cfg.allow_private_hosts, chromium=cfg.chromium_path, voice=voice)


def _confined_output_dir(spec: dict, cfg: Config) -> Path:
    """output_dir can come from a spec or project config, which may not be yours. Without --out it must
    stay under the spec's folder, the current folder, or a folder you passed to --allow-path."""
    out = Path(spec["output_dir"]).expanduser()
    out = (out if out.is_absolute() else Path.cwd() / out).resolve()
    for root in [Path(spec["_dir"]), Path.cwd(), *cfg.allow_paths]:
        try:
            out.relative_to(root.expanduser().resolve())
            return out
        except ValueError:
            continue
    raise SystemExit(f"error: output_dir {out} is outside the spec folder and the current folder. "
                     "Pass --out DIR to choose it explicitly.")


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _config(args)
    spec = load_spec(args.spec, cfg)
    total = sum(s["seconds"] for s in spec["scenes"])
    print(f"ok: {len(spec['scenes'])} scenes, {total:.1f}s, theme={spec['theme']}, formats={spec['formats']}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    cfg = _config(args)
    spec = load_spec(args.spec, cfg)
    if args.no_voice and args.read_screen:
        raise ValueError("--no-voice and --read-screen cannot be used together")
    if args.no_voice:
        for sc in spec["scenes"]:
            sc.pop("narration", None)
            sc.pop("narration_audio", None)
        spec["voiceover"] = {**spec["voiceover"], "read_screen": False}
    elif args.read_screen:
        spec["voiceover"] = {**spec["voiceover"], "read_screen": True}
    formats = list(FORMATS) if args.format == "all" else [args.format] if args.format else spec["formats"]
    out_dir = Path(args.out) if args.out else _confined_output_dir(spec, cfg)
    scale = 0.5 if args.preview else 1.0
    if args.fps is not None and not 1 <= args.fps <= 60:
        raise SpecError("--fps must be an integer between 1 and 60")
    fps = 12 if args.preview else (args.fps or int(spec["fps"]))
    check_frame_budget(sum(s["seconds"] for s in spec["scenes"]), fps, len(formats))
    if args.contact_frames is not None and not 1 <= args.contact_frames <= MAX_CONTACT_FRAMES:
        raise SpecError(f"--contact-frames must be an integer between 1 and {MAX_CONTACT_FRAMES}")
    rt = _runtime(spec, cfg, args.music, no_voice=args.no_voice)
    results = {}
    for fmt in formats:
        print(f"rendering {fmt} ...", flush=True)
        results[fmt] = render(spec, fmt, out_dir, fps=fps, scale=scale, music_override=args.music, contact=True, runtime=rt,
                               n_formats=len(formats), contact_frames=args.contact_frames)
    total = next(iter(results.values()))["seconds"]
    text, warns = share_copy(spec, total, formats)
    (out_dir / "share-copy.txt").write_text(text, encoding="utf-8")
    for fmt, r in results.items():
        print(f"  {fmt}: {r['video']} ({r['seconds']:.1f}s, {r['size'][0]}x{r['size'][1]})")
        warns += [f"{fmt}: {w}" for w in r["warnings"]]
    for w in warns:
        print(f"  warning: {w}", file=sys.stderr)
    print(f"share copy: {out_dir / 'share-copy.txt'}")
    return 0


def _copy_template(name: str, dest: Path, force: bool) -> int:
    if dest.exists() and not force:
        print(f"{dest} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(resources.files("yamlreel") / "templates" / f"{name}.yaml") as src:
        shutil.copyfile(src, dest)
    print(f"wrote {dest}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    names = _templates()
    if args.template not in names:
        print(f"unknown template {args.template!r}; choose from {', '.join(names)}", file=sys.stderr)
        return 2
    return _copy_template(args.template, Path(args.dest), args.force)


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "init":
        dest = user_config_path() if args.user else Path(CONFIG_FILENAME)
        if args.user and not args.force:
            conflict = legacy.user_config_conflict(Path(config_base()), dest)
            if conflict:
                print(conflict, file=sys.stderr)
                return 2
        return _copy_template("config.example", dest, args.force)
    cfg = _config(args)
    shown = dict(cfg.data)
    shown["_sources"] = [str(p) for p in cfg.sources] or ["(none: built-in defaults)"]
    shown["_allow_paths"] = [str(p) for p in cfg.allow_paths]
    shown["_allow_private_hosts"] = cfg.allow_private_hosts
    print(yaml.safe_dump(shown, sort_keys=False, allow_unicode=True))
    return 0


def cmd_music(args: argparse.Namespace) -> int:
    from . import music
    wav = music.write_wav(Path(args.out), music.compose(args.seconds, args.bpm, args.root, args.scale, args.seed))
    print(f"wrote {wav}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yamlreel", description="Render short videos from a YAML spec.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="project config file (default: ./yamlreel.config.yaml)")
    common.add_argument("--allow-path", action="append", default=[], metavar="DIR",
                        help="also let the spec read images, pages and music from DIR (repeatable)")
    common.add_argument("--allow-private-hosts", action="store_true",
                        help="let `shot` capture localhost and private-network URLs")
    common.add_argument("--voice-url", metavar="URL",
                        help="text-to-speech server for scene narration, e.g. VoiceStudio at http://127.0.0.1:3900/mcp/")
    common.add_argument("--read-screen", action="store_true",
                        help="voiceover reads the text shown on screen for every scene without its own narration")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", parents=[common], help="check a spec without rendering")
    v.add_argument("spec")
    v.set_defaults(fn=cmd_validate)
    r = sub.add_parser("render", parents=[common], help="render video, poster, contact sheet and share copy")
    r.add_argument("spec")
    r.add_argument("--out", help="output folder (default: output_dir from config)")
    r.add_argument("--format", choices=[*FORMATS, "all"], help="override the spec's formats")
    r.add_argument("--music", help="koto, none, or a path to an audio file (overrides the spec)")
    r.add_argument("--fps", type=int, help="frames per second, 1-60 (default: fps from config)")
    r.add_argument("--no-voice", action="store_true",
                   help="skip all narration (music only); no text-to-speech server needed")
    r.add_argument("--preview", action="store_true", help="half size, 12 fps: fast look at timing and layout")
    r.add_argument("--contact-frames", type=int, metavar="N",
                   help="stills per scene on the contact sheet, 1-8 (default: 2); one row per scene")
    r.set_defaults(fn=cmd_render)
    i = sub.add_parser("init", help="write a starter spec")
    i.add_argument("template", help=f"one of: {', '.join(_templates())}")
    i.add_argument("dest")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)
    c = sub.add_parser("config", parents=[common], help="show the effective config, or write a starter config file")
    c.add_argument("action", choices=["show", "init"])
    c.add_argument("--user", action="store_true", help="with init: write the user config (~/.config/yamlreel/config.yaml)")
    c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_config)
    m = sub.add_parser("music", help="generate the koto-style track on its own")
    m.add_argument("--seconds", type=float, default=20)
    m.add_argument("--bpm", type=float, default=72)
    m.add_argument("--root", default="D")
    m.add_argument("--scale", default="hirajoshi")
    m.add_argument("--seed", type=int, default=7)
    m.add_argument("--out", default="music.wav")
    m.set_defaults(fn=cmd_music)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (SpecError, ConfigError, RenderError, CaptureError, UnsafeInput, FontError, VoiceError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
