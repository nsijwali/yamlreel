"""Bounded lifetimes for the programs a render starts: the frame encoder and the one-shot ffmpeg calls.

Every wait has a deadline, and every error path stops what it started:

- `FramePipe` owns one encoder process, the thread that feeds it frames and the short queue between them. A frame
  that cannot be queued within `Deadlines.encoder_stall` (the encoder stopped reading) and an encoder that does not
  finish within `Deadlines.finish_for(video seconds)` are errors. On any error, KeyboardInterrupt included, leaving
  the `with` block stops the encoder (terminate, a bounded wait, then kill) before it waits for the feeder thread, so
  a write blocked on a full pipe returns and nothing is left running.
- `run()` is `subprocess.run` with a deadline (`Deadlines.subprocess`); subprocess kills the child when it passes.

The defaults are generous: they exist to end a hung program, never to cut a slow but working render short. They are
set with `timeouts:` in the user config.
"""
from __future__ import annotations

import queue
import subprocess
import threading
from dataclasses import dataclass, fields
from types import TracebackType
from typing import IO, Any, Mapping

MIN_SECONDS, MAX_SECONDS = 1.0, 86400.0
STOP_GRACE = 5.0      # seconds between terminate and kill, and after kill
ABORT_GRACE = 1.0     # the same for an aborted encode, whose output is thrown away (ffmpeg only sees SIGTERM
                      # once its blocked stdin read returns, so a long grace would only add waiting)
FRAME_QUEUE = 4       # frames waiting for the encoder at most (about 25 MB at 1080x1920)
JOIN_SECONDS = 10.0   # the feeder thread ends at most this long after the encoder has been stopped


class ProcessTimeout(RuntimeError):
    """A program did not answer within its deadline; it has been stopped."""


@dataclass(frozen=True)
class Deadlines:
    encoder_stall: float = 300.0     # the encoder takes no frame for this long: it is stuck
    encoder_finish: float = 600.0    # after the last frame, plus finish_per_second per second of video
    subprocess: float = 1800.0       # one mastering, decoding or polishing ffmpeg run
    browser_frame: float = 180.0     # one line-art frame, or the start of the browser that draws them
    finish_per_second: float = 2.0

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> "Deadlines":
        """The `timeouts:` setting: any of encoder_stall, encoder_finish, subprocess, browser_frame, in seconds
        (1 to 86400). Raises ValueError for anything else."""
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("timeouts must be a mapping of seconds")
        from .errorfmt import unknown_key
        allowed = [f.name for f in fields(cls) if f.name != "finish_per_second"]
        for key in raw:
            if key not in allowed:
                raise unknown_key("timeouts", key, allowed, "timeouts")
        values: dict[str, float] = {}
        for key, v in raw.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not MIN_SECONDS <= v <= MAX_SECONDS:
                raise ValueError(f"timeouts.{key} must be a number of seconds between {MIN_SECONDS:g} and {MAX_SECONDS:g}")
            values[key] = float(v)
        return cls(**values)

    def finish_for(self, video_seconds: float) -> float:
        """How long the encoder may take after the last frame of a `video_seconds` video."""
        return self.encoder_finish + self.finish_per_second * max(0.0, video_seconds)


def stop(proc: subprocess.Popen, grace: float = STOP_GRACE) -> None:
    """End `proc`: terminate, wait up to `grace`, then kill and wait again. Never waits without a bound."""
    if proc.poll() is not None:
        return
    for end in (proc.terminate, proc.kill):
        try:
            end()
        except OSError:   # it exited in between
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run(cmd: list[str], timeout: float, what: str) -> subprocess.CompletedProcess:
    """`cmd` with its output captured, ended after `timeout` seconds (ProcessTimeout). OSError is the caller's."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ProcessTimeout(f"ffmpeg did not finish {what} within {timeout:g}s (timeouts.subprocess); it was stopped") from e


class FramePipe:
    """The encoder process, the feeder thread and the frame queue of one render, as one owned resource."""

    def __init__(self, cmd: list[str], log: IO[bytes], deadlines: Deadlines, video_seconds: float):
        self.deadlines = deadlines
        self.video_seconds = video_seconds
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)   # OSError: the caller explains it
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=FRAME_QUEUE)
        self._broken = threading.Event()   # the encoder stopped reading (it exited early); its log says why
        self._closed = False
        self.feeder = threading.Thread(target=self._feed, name="yamlreel-frames", daemon=True)
        self.feeder.start()

    def _feed(self) -> None:
        while (data := self._queue.get()) is not None:
            if not self._broken.is_set():
                try:
                    self.proc.stdin.write(data)
                except Exception:   # keep draining so the drawing side never blocks on a full queue
                    self._broken.set()
        try:
            self.proc.stdin.close()
        except Exception:
            pass

    @property
    def broken(self) -> bool:
        """True once the encoder stopped accepting frames (it exited); stop drawing and call `finish()`."""
        return self._broken.is_set()

    def put(self, frame: bytes) -> None:
        """Queue one frame. Raises ProcessTimeout if the encoder took no frame for `encoder_stall` seconds."""
        try:
            self._queue.put(frame, timeout=self.deadlines.encoder_stall)
        except queue.Full:
            self.abort()
            raise ProcessTimeout(f"ffmpeg took no frame for {self.deadlines.encoder_stall:g}s "
                                 "(timeouts.encoder_stall); it was stopped") from None

    def finish(self) -> int:
        """No more frames: wait (bounded) for the encoder to write the file and exit. Returns its exit code."""
        limit = self.deadlines.finish_for(self.video_seconds)
        try:
            self._queue.put(None, timeout=self.deadlines.encoder_stall)
        except queue.Full:
            self.abort()
            raise ProcessTimeout(f"ffmpeg took no frame for {self.deadlines.encoder_stall:g}s "
                                 "(timeouts.encoder_stall); it was stopped") from None
        self._closed = True
        self.feeder.join(timeout=limit)
        try:
            if self.feeder.is_alive():
                raise subprocess.TimeoutExpired("ffmpeg", limit)
            return self.proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            self.abort()
            raise ProcessTimeout(f"ffmpeg did not finish encoding within {limit:g}s (timeouts.encoder_finish); "
                                 "it was stopped") from None

    def abort(self) -> None:
        """Stop the encoder first (a write blocked on its full pipe then fails), then end the feeder thread."""
        stop(self.proc, ABORT_GRACE)
        self._broken.set()
        if not self._closed:
            self._closed = True
            while True:   # make room for the end marker: the queued frames will never be written
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._queue.put_nowait(None)
            except queue.Full:   # the feeder put nothing back; it is draining, so it finds the queue empty soon
                pass
        self.feeder.join(timeout=JOIN_SECONDS)

    def __enter__(self) -> "FramePipe":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if exc_type is not None or self.proc.poll() is None and not self._closed:
            self.abort()
