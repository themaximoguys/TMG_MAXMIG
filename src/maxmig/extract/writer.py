"""Streaming CSV writer — one row in, one row out, nothing buffered in memory.

RFC 4180 dialect via the stdlib csv module (QUOTE_MINIMAL), UTF-8 with no BOM.
Values are written verbatim: nothing that looks like a date or a number is
reformatted, because the CSV is a faithful transport of what the API returned.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol


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


def _column_order(node: _StructureLike) -> list[str]:
    """Key fields first, then the remaining declared fields in their declared order."""
    seen = set(node.key_fields)
    return node.key_fields + [f for f in node.fields if f not in seen]


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


@dataclass
class _Node:
    obj: Any  # ObjectDef (root) or RelationDef (descendant) — duck-typed
    target: _CsvTarget
    children: list["_Node"]


def _build_tree(obj: _StructureLike, out_dir: Path, module: str, timestamp: str) -> _Node:
    """Every object/relation gets its own stable folder (`{module}/{name}/`),
    reused across runs, holding one timestamped CSV per run — so history for a
    given object accumulates in one place instead of being scattered across a
    new run directory each time."""
    path = out_dir / module / obj.name / f"{obj.name}_{timestamp}.csv"
    target = _CsvTarget(node=obj, path=path, tmp_path=path.with_name(path.name + ".partial"))
    children = [_build_tree(child, out_dir, module, timestamp) for child in obj.children]
    return _Node(obj=obj, target=target, children=children)


def _flatten(node: _Node) -> list[_Node]:
    result = [node]
    for child in node.children:
        result.extend(_flatten(child))
    return result


def _write_node(row: dict[str, Any], node: _Node) -> None:
    columns = _column_order(node.obj)
    node.target.writer.writerow(["" if (value := row.get(column)) is None else value for column in columns])
    node.target.count += 1
    for child in node.children:
        child_rows = row.get(child.obj.relation_attribute) or []
        if isinstance(child_rows, dict):
            child_rows = [child_rows]
        for child_row in child_rows:
            _write_node(_merge_foreign_key(row, child_row, child.obj.foreign_key), child)


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
            node.target.path.parent.mkdir(parents=True, exist_ok=True)
            node.target.file = node.target.tmp_path.open("w", newline="", encoding="utf-8")
            node.target.writer = csv.writer(node.target.file)
            node.target.writer.writerow(_column_order(node.obj))
        for row in rows:
            _write_node(row, root)
    except BaseException:
        for node in nodes:
            if node.target.file is not None:
                node.target.file.close()
            node.target.tmp_path.unlink(missing_ok=True)
        raise
    else:
        for node in nodes:
            node.target.file.close()
        for node in nodes:
            node.target.tmp_path.replace(node.target.path)

    return {
        node.obj.name: ObjectExportResult(path=node.target.path, row_count=node.target.count)
        for node in nodes
    }
