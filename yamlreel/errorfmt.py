"""One-line error text shared by config, specs, theme and voice.

Dependency-light on purpose (only PyYAML and the standard library): config, specs and theme all format errors with
it, so none of them has to import another just for a message. The "did you mean" hint for unknown keys is a hook
that specs registers when it is imported (`set_hint`); without it no hint is added.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable
from urllib.parse import SplitResult, parse_qsl, urlsplit

import yaml

MAX_SHOWN_CHARS = 60
REDACTED = "***"

HintFn = Callable[[str, Iterable[str]], "str | None"]
_hint: HintFn | None = None


def set_hint(fn: HintFn | None) -> None:
    """Install the "did you mean" hook every unknown-key error goes through (specs installs one, late-bound)."""
    global _hint
    _hint = fn


def describe_key(key: object) -> str:
    """A mapping key as it can safely appear in a one-line error message (never walks a nested value)."""
    if isinstance(key, str):
        text = key
    elif key is None or isinstance(key, (bool, int, float)):
        text = repr(key)
    else:
        return type(key).__name__
    if text.isprintable() and len(text) <= MAX_SHOWN_CHARS:
        return text
    return repr(text[:MAX_SHOWN_CHARS]) + ("..." if len(text) > MAX_SHOWN_CHARS else "")


def describe_value(v: object) -> str:
    """Any value, quoted if it is text, short and on one line. Collections are named, never printed."""
    if isinstance(v, str):
        return repr(v[:MAX_SHOWN_CHARS]) + ("..." if len(v) > MAX_SHOWN_CHARS else "")
    return describe_key(v)


def yaml_problem(e: yaml.YAMLError) -> str:
    """A YAML error on one line: the problem and where it is."""
    if isinstance(e, yaml.MarkedYAMLError):
        mark = e.problem_mark or e.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return " ".join(f"{e.problem or 'invalid YAML'}{where}".split())
    return " ".join(str(e).split())


def unknown_key(path: str, key: object, allowed: Iterable[str], what: str, exc: type[Exception] = ValueError,
                problem: str = "unknown field", note: str = "") -> Exception:
    """The error for a key that `what` does not have. `path` is the mapping that holds the key. The one place the
    "did you mean" hook (issue #13) plugs in, for specs, configs, limits, voice_server and theme entries alike."""
    allowed = list(allowed)
    name = describe_key(key)
    full = f"{path}.{name}" if path else name
    msg = f"{full}: {problem} for {what}; allowed: {', '.join(sorted(allowed))}"
    if note:
        msg += f" ({note})"
    hint = _hint(key, allowed) if _hint is not None and isinstance(key, str) else None
    return exc(f"{msg} ({hint})" if hint else msg)


def redact_url(url: str) -> str:
    """`url` safe to print: user name and password become ***, every query value becomes ***, the fragment is
    dropped. Text that does not parse as a URL is replaced as a whole (it might hold a credential)."""
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port
    except ValueError:
        return "<invalid URL>"
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    if u.username is not None or u.password is not None:
        netloc = f"{REDACTED}@{netloc}"
    query = "&".join(f"{k}={REDACTED}" for k, _ in parse_qsl(u.query, keep_blank_values=True))
    if u.query and not query:
        query = REDACTED
    return SplitResult(u.scheme, netloc, u.path, query, "").geturl()


def redact_text(text: str, url: str) -> str:
    """`text` with every occurrence of `url` (and of its password, if any) replaced by the redacted form."""
    out = text.replace(url, redact_url(url))
    try:
        secret = urlsplit(url).password
    except ValueError:
        secret = None
    return out.replace(secret, REDACTED) if secret else out

