"""Staging folders left behind by a render that was killed outright (SIGKILL, a power cut) are removed by the next
render into the same folder.

Each staging folder holds an owner file with the process id and host of the render that made it. Another render
removes the folder when that process is gone (same host), or when the folder is older than MAX_AGE_SECONDS in any
case. A folder without an owner file is left alone until it is that old (it may be one being created right now).
"""
from __future__ import annotations

import os
import shutil
import socket
import time
from pathlib import Path

OWNER_FILE = ".owner"
MAX_AGE_SECONDS = 24 * 3600


def mark(folder: Path) -> None:
    """Record this process as the owner of `folder`."""
    (folder / OWNER_FILE).write_text(f"{os.getpid()} {socket.gethostname()}\n", encoding="utf-8")


def _alive(pid: int) -> bool:
    if os.name == "nt":   # os.kill(pid, 0) would end the process on Windows: rely on the age rule there
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # exists, owned by someone else
        return True
    return True


def is_stale(folder: Path, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        age = now - folder.stat().st_mtime
    except OSError:
        return False
    if age > MAX_AGE_SECONDS:
        return True
    try:
        pid_text, _, host = (folder / OWNER_FILE).read_text(encoding="utf-8").strip().partition(" ")
        pid = int(pid_text)
    except (OSError, ValueError):
        return False
    return host == socket.gethostname() and pid != os.getpid() and not _alive(pid)


def sweep(dest: Path, prefix: str) -> list[Path]:
    """Remove the stale `prefix*` folders in `dest`; returns what was removed."""
    removed: list[Path] = []
    if not dest.is_dir():
        return removed
    for folder in dest.glob(prefix + "*"):
        if folder.is_dir() and is_stale(folder):
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder)
    return removed
