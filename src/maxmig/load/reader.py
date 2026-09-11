"""CSV reader for `migrate import` — the read-side mirror of extract/writer.py.

A row's values all arrive as strings (csv.DictReader never infers types) —
`importer.py` decides what, if anything, to omit or convert before sending a
row on to Maximo.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator


def read_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """Yield each data row of `path` as a dict keyed by its header column names."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)
