"""Contact sheets larger than MAX_SHEET_PIXELS are split into pages, so a long spec never needs one huge image.

The first page keeps the usual name (`contact.png`); later pages are `contact-p2.png`, `contact-p3.png`, ... Each page
holds whole rows. An ordinary sheet (a few dozen scenes) fits on one page and is exactly what it was before.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_SHEET_PIXELS = 64_000_000   # one page at most (about 190 MB as RGB), the same bound as one shot image


def rows_per_page(sheet_width: int, row_height: int, max_pixels: int = MAX_SHEET_PIXELS) -> int:
    """How many rows of `row_height` fit on a `sheet_width` page (always at least one)."""
    return max(1, max_pixels // max(1, sheet_width * row_height))


def page_path(first: Path, k: int) -> Path:
    """Page `k` (0-based) of the sheet whose first page is `first`."""
    return first if k == 0 else first.with_name(f"{first.stem}-p{k + 1}{first.suffix}")


def extra_pages(folder: Path, first_name: str) -> list[Path]:
    """The pages after the first that exist in `folder` for a sheet named `first_name`, in page order."""
    stem, suffix = Path(first_name).stem, Path(first_name).suffix
    pat = re.compile(rf"{re.escape(stem)}-p([2-9]|[1-9][0-9]+){re.escape(suffix)}")
    found = [(int(m[1]), p) for p in folder.iterdir() if (m := pat.fullmatch(p.name))]
    return [p for _, p in sorted(found)]
