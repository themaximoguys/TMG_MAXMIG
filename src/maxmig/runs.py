"""Read-only views over the module registry and past run outputs.

Backs `list-modules`, `list-objects` and `status`. Kept separate from cli.py so
the shaping logic is unit-testable without Click, and so the CLI layer only
decides formatting (table vs JSON), not what the data is.

Run history is read straight from the manifest / import-result JSON files the
CLI already writes next to each CSV — no separate run database to drift out of
sync with what's actually on disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

RunKind = Literal["export", "import"]

# Suffix -> kind. Filenames are `{object}_{YYYYmmddTHHMMSSZ}{suffix}`, so a
# plain string sort within one object folder is chronological.
_RUN_SUFFIXES: dict[str, RunKind] = {
    ".manifest.json": "export",
    ".import-result.json": "import",
}


def describe_modules(registry: Any) -> list[dict[str, Any]]:
    """One summary dict per loaded module, sorted by module name."""
    rows: list[dict[str, Any]] = []
    for name in registry.list_modules():
        module = registry.get_module(name)
        rows.append(
            {
                "module": module.module,
                "description": module.description,
                "object_count": len(module.objects),
                "extract_order": list(module.extract_order),
                "load_order": list(module.load_order),
            }
        )
    return rows


def _child_paths(children: list[Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for child in children:
        path = f"{prefix}{child.name}"
        paths.append(path)
        paths.extend(_child_paths(list(getattr(child, "children", None) or []), f"{path}."))
    return paths


def describe_objects(registry: Any, module: str) -> list[dict[str, Any]]:
    """One summary dict per object in `module`, in declaration order.

    `custom_fields` is read with getattr so this works against both the
    current schema and the one that adds user-defined attribute support.
    """
    rows: list[dict[str, Any]] = []
    for obj in registry.get_module(module).objects:
        rows.append(
            {
                "name": obj.name,
                "maximo_object": obj.maximo_object,
                "oslc_resource": obj.access.oslc_resource,
                "rest_mbo_table": obj.access.rest_mbo_table,
                "prefer": obj.access.prefer,
                "key_fields": list(obj.key_fields),
                "field_count": len(obj.fields),
                "custom_fields": list(getattr(obj, "custom_fields", None) or []),
                "children": _child_paths(list(obj.children)),
            }
        )
    return rows


@dataclass(frozen=True)
class RunInfo:
    """The newest export or import run recorded for one object."""

    kind: RunKind
    module: str
    object: str
    path: Path
    env_prefix: str | None = None
    row_count: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    dry_run: bool | None = None
    started_at: str | None = None
    finished_at: str | None = None


def _kind_of(path: Path) -> RunKind | None:
    for suffix, kind in _RUN_SUFFIXES.items():
        if path.name.endswith(suffix):
            return kind
    return None


def _load_run(path: Path, kind: RunKind, module: str, object_name: str) -> RunInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    return RunInfo(
        kind=kind,
        module=str(data.get("module") or module),
        object=str(data.get("object") or object_name),
        path=path,
        env_prefix=data.get("env_prefix"),
        row_count=data.get("row_count"),
        counts=dict(counts),
        dry_run=data.get("dry_run") if kind == "import" else None,
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
    )


def latest_runs(out_dir: Path, module: str | None = None) -> list[RunInfo]:
    """Newest export and newest import per object under `{out_dir}/{module}/{object}/`.

    "Newest" is by filename (the embedded UTC timestamp), not mtime — copying
    an exports folder between machines resets mtimes but not names. An
    unreadable/corrupt JSON file is skipped and the next-newest valid one used,
    so one bad file doesn't hide an object's history.
    """
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    module_dirs = [out_dir / module] if module else sorted(p for p in out_dir.iterdir() if p.is_dir())

    runs: list[RunInfo] = []
    for module_dir in module_dirs:
        if not module_dir.is_dir():
            continue
        for object_dir in sorted(p for p in module_dir.iterdir() if p.is_dir()):
            by_kind: dict[RunKind, list[Path]] = {}
            for path in object_dir.iterdir():
                kind = _kind_of(path)
                if kind is not None and path.is_file():
                    by_kind.setdefault(kind, []).append(path)
            for kind in ("export", "import"):
                for path in sorted(by_kind.get(kind, []), key=lambda p: p.name, reverse=True):
                    info = _load_run(path, kind, module_dir.name, object_dir.name)
                    if info is not None:
                        runs.append(info)
                        break
    return runs
