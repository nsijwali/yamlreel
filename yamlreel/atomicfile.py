"""Write a file so that readers see the old file or the complete new one, never a half-written or unchecked one."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

PART_SUFFIX = ".part"


def write_validated(dest: Path, data: bytes, validate: Callable[[Path], object]) -> Path:
    """Write `data` to `dest` through a temporary file of its own in the same folder, call `validate(tmp)` on it (it
    raises to reject), then move it into place with one `os.replace`.

    Every writer gets a unique temporary name, so two processes writing the same `dest` at once never mix bytes;
    the last complete, valid file wins. On any failure, interruption included, the temporary file is removed and
    `dest` is left as it was."""
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=PART_SUFFIX, dir=dest.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        validate(tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest
