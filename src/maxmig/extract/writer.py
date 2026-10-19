"""Streaming CSV writer — one row in, one row out, nothing buffered in memory.

RFC 4180 dialect via the stdlib csv module (QUOTE_MINIMAL), UTF-8 with no BOM.
Values are written verbatim: nothing that looks like a date or a number is
reformatted, because the CSV is a faithful transport of what the API returned.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Protocol

from maxmig.api.oslc import is_plumbing_key

if TYPE_CHECKING:
    from maxmig.extract.checkpoint import ExportCheckpoint


def write_csv(rows: Iterable[dict[str, str]], path: Path, columns: list[str]) -> int:
    """Write `rows` to `path` as CSV with a `columns` header. Returns rows written.

    A field absent from a row dict, or present as None, is written as an empty
    string — never the literal text "None"/"null".

    Written to a sibling `.partial` file and only renamed to `path` on full
    success (an atomic `os.replace`, since both are on the same filesystem) —
    a mid-stream failure (e.g. the row source raises after page 1) then never
    leaves a truncated file sitting at the final path looking complete.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".partial")

    count = 0
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if (value := row.get(column)) is None else value for column in columns])
                count += 1
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(path)
    return count


class _StructureLike(Protocol):
    """Structural type covering both ObjectDef and RelationDef — all this
    module needs is name/key_fields/fields/children (+ foreign_key on a
    relation, accessed separately since the root object has none)."""

    name: str
    key_fields: list[str]
    fields: list[str]
    children: list["_StructureLike"]


def _declared_fields(node: _StructureLike) -> list[str]:
    """`fields` followed by any declared `custom_fields` (duck-typed: a node
    without custom-attribute support just has `fields`)."""
    custom = getattr(node, "custom_fields", None) or []
    return node.fields + [f for f in custom if f not in node.fields]


def _column_order(node: _StructureLike) -> list[str]:
    """Key fields first, then the remaining declared fields (standard, then
    custom) in their declared order."""
    seen = set(node.key_fields)
    return node.key_fields + [f for f in _declared_fields(node) if f not in seen]


def _is_capturable(key: str, value: Any) -> bool:
    """Whether an undeclared key in an API row is a real scalar attribute worth
    its own column under `capture_all_attributes` — not OSLC plumbing and not
    a nested relation/object."""
    return not is_plumbing_key(key) and not isinstance(value, (dict, list))


def _merge_foreign_key(parent_row: dict[str, Any], child_row: dict[str, Any], foreign_key: str | list[str]) -> dict[str, Any]:
    """Copy the parent's foreign-key field values into a child row.

    Maximo's nested child payloads (e.g. an item's embedded `itemspec` rows)
    don't repeat the parent's key fields — merging them in here is what makes
    each child CSV traceable back to its parent on its own, per-file, without
    needing the parent CSV open alongside it.
    """
    fks = foreign_key if isinstance(foreign_key, list) else [foreign_key]
    merged = dict(child_row)
    for fk in fks:
        if fk not in merged:
            merged[fk] = parent_row.get(fk)
    return merged


@dataclass(frozen=True)
class ObjectExportResult:
    """Where one object/relation's CSV ended up, and how many rows it got."""

    path: Path
    row_count: int


@dataclass
class _CsvTarget:
    node: _StructureLike
    path: Path
    tmp_path: Path
    file: Any = field(default=None)
    writer: Any = field(default=None)
    count: int = 0
    # capture_all_attributes only: rows are spooled as JSON lines first, since
    # the full column set (every custom attribute any row carries) is only
    # known once the last row has been seen — on disk, never buffered in memory.
    spool_path: Path | None = None
    extras: set[str] = field(default_factory=set)


@dataclass
class _Node:
    obj: Any  # ObjectDef (root) or RelationDef (descendant) — duck-typed
    target: _CsvTarget
    children: list["_Node"]
    # Fixed per node — computed once here, not on every row of the hot loop.
    columns: list[str] = field(default_factory=list)
    skip_keys: frozenset[str] = frozenset()  # declared columns + relation keys (capture-all only)


def _build_tree(obj: _StructureLike, out_dir: Path, module: str, timestamp: str) -> _Node:
    """Every object/relation gets its own stable folder (`{module}/{name}/`),
    reused across runs, holding one timestamped CSV per run — so history for a
    given object accumulates in one place instead of being scattered across a
    new run directory each time."""
    path = out_dir / module / obj.name / f"{obj.name}_{timestamp}.csv"
    target = _CsvTarget(node=obj, path=path, tmp_path=path.with_name(path.name + ".partial"))
    if getattr(obj, "capture_all_attributes", False):
        target.spool_path = path.with_name(path.name + ".spool.jsonl")
    children = [_build_tree(child, out_dir, module, timestamp) for child in obj.children]
    columns = _column_order(obj)
    skip_keys = frozenset(columns) | {child.relation_attribute for child in obj.children}
    return _Node(obj=obj, target=target, children=children, columns=columns, skip_keys=skip_keys)


def _flatten(node: _Node) -> list[_Node]:
    result = [node]
    for child in node.children:
        result.extend(_flatten(child))
    return result


def _write_node(row: dict[str, Any], node: _Node) -> None:
    columns = node.columns
    target = node.target
    if target.spool_path is not None:
        record = {column: row.get(column) for column in columns}
        for key, value in row.items():
            if key not in node.skip_keys and _is_capturable(key, value):
                record[key] = value
                target.extras.add(key)
        target.file.write(json.dumps(record) + "\n")
    else:
        target.writer.writerow(["" if (value := row.get(column)) is None else value for column in columns])
    target.count += 1
    for child in node.children:
        child_rows = row.get(child.obj.relation_attribute) or []
        if isinstance(child_rows, dict):
            child_rows = [child_rows]
        for child_row in child_rows:
            _write_node(_merge_foreign_key(row, child_row, child.obj.foreign_key), child)


def _spool_to_csv(target: _CsvTarget, declared_columns: list[str]) -> None:
    """Second pass for a capture-all node: declared columns first (same order
    as a normal export), then every undeclared attribute seen, sorted."""
    columns = declared_columns + sorted(target.extras - set(declared_columns))
    with target.spool_path.open(encoding="utf-8") as src, target.tmp_path.open("w", newline="", encoding="utf-8") as dst:
        writer = csv.writer(dst)
        writer.writerow(columns)
        for line in src:
            record = json.loads(line)
            writer.writerow(["" if (value := record.get(column)) is None else value for column in columns])
    target.spool_path.unlink()


def write_object_tree(
    rows: Iterable[dict[str, Any]], obj_def: _StructureLike, out_dir: Path, module: str, timestamp: str
) -> dict[str, ObjectExportResult]:
    """Write `obj_def`'s own CSV plus one CSV per (possibly nested) child
    relation, all in a single pass over `rows`. Returns
    `{object_name: ObjectExportResult(path, row_count)}` covering the root
    object and every descendant relation, however deep — each under its own
    stable `{out_dir}/{module}/{object_name}/` folder, filename-stamped with
    `timestamp` (e.g. `20260911T151322Z`) so repeated runs accumulate history
    per object instead of overwriting or scattering across run directories.

    Every relation's CSV carries its declared `foreign_key` values copied in
    from its immediate parent row, so the relationship is traceable from the
    child file alone.

    All files are written to `.partial` siblings and only renamed in together
    on full success — a mid-stream failure never leaves some CSVs complete and
    others missing/truncated for the same run.
    """
    root = _build_tree(obj_def, out_dir, module, timestamp)
    nodes = _flatten(root)

    try:
        for node in nodes:
            target = node.target
            target.path.parent.mkdir(parents=True, exist_ok=True)
            if target.spool_path is not None:
                target.file = target.spool_path.open("w", encoding="utf-8")
            else:
                target.file = target.tmp_path.open("w", newline="", encoding="utf-8")
                target.writer = csv.writer(target.file)
                target.writer.writerow(node.columns)
        for row in rows:
            _write_node(row, root)
        for node in nodes:
            node.target.file.close()
            if node.target.spool_path is not None:
                _spool_to_csv(node.target, node.columns)
    except BaseException:
        for node in nodes:
            if node.target.file is not None:
                node.target.file.close()
            node.target.tmp_path.unlink(missing_ok=True)
            if node.target.spool_path is not None:
                node.target.spool_path.unlink(missing_ok=True)
        raise
    else:
        for node in nodes:
            node.target.tmp_path.replace(node.target.path)

    return {
        node.obj.name: ObjectExportResult(path=node.target.path, row_count=node.target.count)
        for node in nodes
    }


def _live_path(target: _CsvTarget) -> Path:
    """The file rows are being appended to while the export runs."""
    return target.spool_path if target.spool_path is not None else target.tmp_path


def write_object_tree_resumable(
    pages: Iterable[tuple[str, int | None, list[dict[str, Any]]]],
    obj_def: _StructureLike,
    out_dir: Path,
    module: str,
    timestamp: str,
    checkpoint: "ExportCheckpoint",
) -> dict[str, ObjectExportResult]:
    """`write_object_tree`, page by page, checkpointing after every page.

    `pages` yields `(api, next_cursor, rows)` (see `MaximoClient.fetch_pages`).
    After each page every output file is flushed and its size recorded in
    `checkpoint` along with `next_cursor`. When `checkpoint` already holds node
    state (a resume), each partial file is first truncated to its recorded size
    — dropping whatever part of a page was written after the last checkpoint —
    and appended to rather than recreated.

    Unlike `write_object_tree`, a failure *keeps* the partial files: they and
    the checkpoint are what a later `--resume` continues from.
    """
    root = _build_tree(obj_def, out_dir, module, timestamp)
    nodes = _flatten(root)
    resuming = checkpoint.resuming

    try:
        for node in nodes:
            target = node.target
            target.path.parent.mkdir(parents=True, exist_ok=True)
            live = _live_path(target)
            if resuming:
                saved = checkpoint.nodes.get(node.obj.name)
                if saved is None or not live.exists():
                    raise FileNotFoundError(
                        f"cannot resume: {live.name} {'is missing' if saved else 'is not in the checkpoint'}"
                    )
                with live.open("rb+") as fh:
                    fh.truncate(saved["offset"])
                target.count = saved["count"]
                target.extras = set(saved.get("extras", []))
            if target.spool_path is not None:
                target.file = live.open("a" if resuming else "w", encoding="utf-8")
            else:
                target.file = live.open("a" if resuming else "w", newline="", encoding="utf-8")
                target.writer = csv.writer(target.file)
                if not resuming:
                    target.writer.writerow(node.columns)

        for api, next_cursor, rows in pages:
            for row in rows:
                _write_node(row, root)
            for node in nodes:
                node.target.file.flush()
            checkpoint.api = api
            checkpoint.cursor = next_cursor
            checkpoint.pages += 1
            checkpoint.nodes = {
                node.obj.name: {
                    "offset": _live_path(node.target).stat().st_size,
                    "count": node.target.count,
                    "extras": sorted(node.target.extras),
                }
                for node in nodes
            }
            checkpoint.save()

        for node in nodes:
            node.target.file.close()
            if node.target.spool_path is not None:
                _spool_to_csv(node.target, node.columns)
    except BaseException:
        if not checkpoint.resuming:  # failed before the first checkpoint: nothing to resume from
            for node in nodes:
                if node.target.file is not None:
                    node.target.file.close()
                _live_path(node.target).unlink(missing_ok=True)
                node.target.tmp_path.unlink(missing_ok=True)
        raise
    finally:
        for node in nodes:
            if node.target.file is not None and not node.target.file.closed:
                node.target.file.close()

    for node in nodes:
        node.target.tmp_path.replace(node.target.path)
    return {
        node.obj.name: ObjectExportResult(path=node.target.path, row_count=node.target.count)
        for node in nodes
    }

