"""SIGTERM and SIGHUP end a command the way Ctrl-C does: as an exception, so every `finally`, `with` and ExitStack
on the way out runs. The encoder is stopped, the browser closed, the staging and work folders deleted, and the
previous outputs stay as they were. Python's default for these signals would end the process with no cleanup.

The exit status is the conventional 128 + signal number (143 for SIGTERM). Once the first signal has arrived,
further ones are ignored, so a second SIGTERM cannot interrupt the cleanup the first one started.
"""
from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


class Terminated(SystemExit):
    """Raised in the main thread when SIGTERM or SIGHUP arrives; its code is 128 + the signal number."""


def _raise(signum: int, frame: FrameType | None) -> None:
    for s in SIGNALS:   # the cleanup this starts must not be cut short by another signal
        signal.signal(s, signal.SIG_IGN)
    raise Terminated(128 + signum)


@contextmanager
def exit_on_signals() -> Iterator[None]:
    """Within the block, SIGTERM and SIGHUP raise Terminated. Only the main thread can install handlers; elsewhere
    (a library caller running yamlreel in a thread) this does nothing. The previous handlers come back afterwards."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {s: signal.getsignal(s) for s in SIGNALS}
    for s in SIGNALS:
        signal.signal(s, _raise)
    try:
        yield
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
