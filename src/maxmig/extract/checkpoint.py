"""Export checkpoints — resume a very large export where it stopped.

With `migrate export --checkpoint` (or `--resume`), the export pages by
explicit cursor (OSLC `pageno` / REST MBO `_rsStart`) and, after every page is
fully written and flushed, records in `{object}_{timestamp}.checkpoint.json`:

- the API in use and the cursor of the next page;
- for every output file (parent and each child relation): the byte size of its
  `.partial` (or capture-all `.spool.jsonl`) file, its row count, and, for
  capture-all nodes, the extra attribute names seen so far;
- a fingerprint of everything that decides *which* rows the export returns
  (env, object definition, filter, page size), so a resume can't silently
  splice together two different queries.

If the export dies, the partial files and the checkpoint are left in place.
`--resume` truncates every partial file back to its recorded size (dropping a
half-written page), reopens it for append and continues from the cursor; on
success the files are renamed and the manifest written exactly as for an
uninterrupted run, and the checkpoint is deleted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHECKPOINT_SUFFIX = ".checkpoint.json"
CHECKPOINT_VERSION = 1


class CheckpointError(ValueError):
    """A checkpoint that can't be used for the requested resume."""


def fingerprint(**parts: Any) -> str:
    text = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class ExportCheckpoint:
    path: Path
    module: str
    object: str
    env_prefix: str
    timestamp: str
    fingerprint: str
    api: str | None = None
    cursor: int | None = None  # next page to fetch; None = nothing fetched yet or finished
    pages: int = 0
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {offset, count, extras}
    skipped: list[dict[str, Any]] = field(default_factory=list)
    resume_count: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""
    version: int = CHECKPOINT_VERSION

    @property
    def resuming(self) -> bool:
        return bool(self.nodes)

    @staticmethod
    def path_for(object_dir: Path, object_name: str, timestamp: str) -> Path:
        return object_dir / f"{object_name}_{timestamp}{CHECKPOINT_SUFFIX}"

    def save(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        data = asdict(self)
        data["path"] = str(self.path)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)  # atomic: a crash mid-save leaves the previous checkpoint intact

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> "ExportCheckpoint":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckpointError(f"unreadable checkpoint {path}: {exc}") from exc
        if data.get("version") != CHECKPOINT_VERSION:
            raise CheckpointError(f"{path}: unsupported checkpoint version {data.get('version')!r}")
        data["path"] = Path(path)
        return cls(**data)

    def check_matches(self, *, module: str, object_name: str, env_prefix: str, fp: str) -> None:
        mismatches = [
            label
            for label, mine, theirs in (
                ("module", self.module, module),
                ("object", self.object, object_name),
                ("env", self.env_prefix, env_prefix),
                ("query (object definition, filter, page size)", self.fingerprint, fp),
            )
            if mine != theirs
        ]
        if mismatches:
            raise CheckpointError(
                f"checkpoint {self.path.name} was written for a different {', '.join(mismatches)}; "
                "re-run with the original options, or delete the checkpoint (and its .partial files) to start over"
            )


def find_latest(object_dir: Path, object_name: str) -> Path | None:
    """Newest checkpoint for this object (timestamps sort lexically)."""
    if not object_dir.is_dir():
        return None
    found = sorted(object_dir.glob(f"{object_name}_*{CHECKPOINT_SUFFIX}"))
    return found[-1] if found else None


def timestamp_of(path: Path, object_name: str) -> str:
    return path.name[len(object_name) + 1 : -len(CHECKPOINT_SUFFIX)]
