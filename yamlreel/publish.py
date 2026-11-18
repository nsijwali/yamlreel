"""Staged output: a render writes into a private folder and only a finished render replaces the previous files.

`Staging(dest)` makes `dest/.staging-<random>` (inside `dest`, so every move stays on one filesystem and is a plain
rename). The render asks it for the path of each output it writes. `publish()` then moves each staged file onto
its final name with `os.replace`, sidecars first and the video last, and deletes the stale names the render marked
with `remove()` (a subtitle file or extra contact-sheet page this run no longer makes). If the render fails, or is
interrupted, the staging folder is deleted and `dest` is exactly as it was.

Each replace is atomic on its own; the set of files as a whole is not: a crash in the middle of `publish()` can
leave some new files beside some old ones. The video goes last so that a new video always comes with its new
poster, subtitles and contact sheet.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import TracebackType

STAGING_PREFIX = ".staging-"


class PublishError(RuntimeError):
    """A staged output is missing at publish time (the step that should have written it did not)."""


class Staging:
    def __init__(self, dest: Path):
        self.dest = dest
        dest.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=dest))
        self._names: list[str] = []
        self._stale: list[str] = []
        self._done = False

    def path(self, name: str) -> Path:
        """Where to write the output that will be published as `dest/name`."""
        if Path(name).name != name:
            raise ValueError(f"staged output names are plain file names, got {name!r}")
        if name not in self._names:
            self._names.append(name)
        if name in self._stale:
            self._stale.remove(name)
        return self.dir / name

    def remove(self, name: str) -> None:
        """`dest/name` is left over from an earlier run and must go when this run publishes (never before)."""
        if Path(name).name != name:
            raise ValueError(f"staged output names are plain file names, got {name!r}")
        if name not in self._names and name not in self._stale:
            self._stale.append(name)

    def final(self, name: str) -> Path:
        return self.dest / name

    def publish(self, last: str | None = None) -> list[Path]:
        """Move every staged output into place (`last` after all the others) and delete the stale names. Returns
        the published paths. Raises PublishError, publishing nothing, if a staged output was never written."""
        order = [n for n in self._names if n != last] + ([last] if last in self._names else [])
        missing = [n for n in order if not (self.dir / n).is_file()]
        if missing:
            self.discard()
            raise PublishError(f"output not written: {', '.join(missing)}")
        out: list[Path] = []
        try:
            for name in order:
                os.replace(self.dir / name, self.dest / name)
                out.append(self.dest / name)
            for name in self._stale:
                (self.dest / name).unlink(missing_ok=True)
        finally:
            self.discard()
        return out

    def discard(self) -> None:
        """Delete the staging folder and everything in it; the destination is not touched."""
        if not self._done:
            self._done = True
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "Staging":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.discard()   # after publish() there is nothing left; after a failure nothing is published
