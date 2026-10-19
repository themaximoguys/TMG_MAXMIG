"""Referential-integrity check over an exported CSV tree — pure CSV, no network.

An export writes one CSV per object plus one per child relation, linked only
by foreign-key columns copied from the parent. Before those files are trusted
for an import (or after an export, to catch a writer bug), every child row's
foreign key must point at a row that actually exists in its parent's CSV, and
every natural key must be present and unique — a duplicate key would make the
create-or-update loader PATCH the same target record twice with different data.

Memory is bounded by key tuples, not rows: each CSV is streamed once, keeping
only its own key set (for duplicate detection) and the projections its
children's foreign keys need.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Orphan:
    """A child row whose foreign key matches no row in its parent CSV."""

    relation: str
    path: Path
    row: int
    foreign_key: tuple[str, ...]
    values: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateKey:
    relation: str
    path: Path
    row: int
    first_row: int
    key_fields: tuple[str, ...]
    values: tuple[str, ...]


@dataclass(frozen=True)
class BlankKey:
    relation: str
    path: Path
    row: int
    blank_fields: tuple[str, ...]


@dataclass(frozen=True)
class MissingInput:
    """A CSV (or a required column in it) that couldn't be checked."""

    relation: str
    path: Path | None
    reason: str


@dataclass
class IntegrityReport:
    orphans: list[Orphan] = field(default_factory=list)
    duplicates: list[DuplicateKey] = field(default_factory=list)
    blank_keys: list[BlankKey] = field(default_factory=list)
    missing: list[MissingInput] = field(default_factory=list)
    rows_checked: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only if everything was checkable and nothing was wrong — a
        missing file is not a pass, since its rows were never verified."""
        return not (self.orphans or self.duplicates or self.blank_keys or self.missing)

    def summary_lines(self, limit: int = 20) -> list[str]:
        """Human-readable summary: one header line, then up to `limit` lines per problem kind."""
        lines = [
            f"integrity {'OK' if self.ok else 'FAILED'}: "
            f"{sum(self.rows_checked.values())} rows in {len(self.rows_checked)} file(s); "
            f"{len(self.orphans)} orphan(s), {len(self.duplicates)} duplicate key(s), "
            f"{len(self.blank_keys)} blank key(s), {len(self.missing)} missing input(s)"
        ]
        for m in self.missing[:limit]:
            lines.append(f"  missing  {m.relation}: {m.reason} ({m.path})")
        for o in self.orphans[:limit]:
            lines.append(f"  orphan   {o.relation} row {o.row}: {_pairs(o.foreign_key, o.values)} not in parent ({o.path})")
        for d in self.duplicates[:limit]:
            lines.append(
                f"  dup      {d.relation} row {d.row}: {_pairs(d.key_fields, d.values)} "
                f"(first seen row {d.first_row}) ({d.path})"
            )
        for b in self.blank_keys[:limit]:
            lines.append(f"  blank    {b.relation} row {b.row}: {', '.join(b.blank_fields)} ({b.path})")
        return lines


def _pairs(names: Iterable[str], values: Iterable[str]) -> str:
    return ", ".join(f"{n}={v!r}" for n, v in zip(names, values))


def _as_list(foreign_key: str | list[str]) -> list[str]:
    return [foreign_key] if isinstance(foreign_key, str) else list(foreign_key)


def check_tree(obj_def: Any, csv_paths: dict[str, Path]) -> IntegrityReport:
    """Check `obj_def`'s CSV tree. `csv_paths` maps object/relation name -> CSV path.

    `obj_def` is duck-typed (ObjectDef/RelationDef shape: name, key_fields,
    children[] with foreign_key) so this module doesn't depend on the model layer.
    """
    report = IntegrityReport()
    _check_node(obj_def, csv_paths, None, report)
    return report


def _check_node(
    node: Any,
    csv_paths: dict[str, Path],
    parent_keys: set[tuple[str, ...]] | None,
    report: IntegrityReport,
) -> None:
    """Check one CSV, then recurse into its children.

    `parent_keys` is the parent's rows projected onto THIS node's foreign_key
    columns; None means the parent couldn't be read, so the FK check is
    skipped (the parent's own `missing` entry already explains why).
    """
    children = list(getattr(node, "children", None) or [])
    child_fks = {child.name: _as_list(child.foreign_key) for child in children}
    child_sets: dict[str, set[tuple[str, ...]] | None] = {name: set() for name in child_fks}

    own_fk = _as_list(node.foreign_key) if parent_keys is not None else []
    readable = _scan_csv(node, csv_paths, own_fk, parent_keys, child_fks, child_sets, report)
    if not readable:
        child_sets = {name: None for name in child_fks}

    for child in children:
        _check_node(child, csv_paths, child_sets.get(child.name), report)


def _scan_csv(
    node: Any,
    csv_paths: dict[str, Path],
    own_fk: list[str],
    parent_keys: set[tuple[str, ...]] | None,
    child_fks: dict[str, list[str]],
    child_sets: dict[str, set[tuple[str, ...]] | None],
    report: IntegrityReport,
) -> bool:
    name = node.name
    raw_path = csv_paths.get(name)
    if raw_path is None:
        report.missing.append(MissingInput(name, None, "no CSV path given"))
        return False
    path = Path(raw_path)
    if not path.is_file():
        report.missing.append(MissingInput(name, path, "file not found"))
        return False

    key_fields = list(node.key_fields)
    optional_keys = set(getattr(node, "optional_key_fields", None) or ())
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = set(reader.fieldnames or [])

        needed = set(key_fields) | set(own_fk)
        for fks in child_fks.values():
            needed |= set(fks)
        absent = sorted(needed - header)
        if absent:
            report.missing.append(MissingInput(name, path, f"column(s) absent: {', '.join(absent)}"))
            return False

        seen: dict[tuple[str, ...], int] = {}
        count = 0
        for row_number, row in enumerate(reader, start=1):
            count = row_number
            key = tuple(row.get(f) or "" for f in key_fields)
            blank = tuple(f for f, v in zip(key_fields, key) if not v.strip() and f not in optional_keys)
            if blank:
                report.blank_keys.append(BlankKey(name, path, row_number, blank))
            elif key in seen:
                report.duplicates.append(
                    DuplicateKey(name, path, row_number, seen[key], tuple(key_fields), key)
                )
            else:
                seen[key] = row_number

            if parent_keys is not None:
                fk_values = tuple(row.get(f) or "" for f in own_fk)
                if fk_values not in parent_keys:
                    report.orphans.append(Orphan(name, path, row_number, tuple(own_fk), fk_values))

            for child_name, fks in child_fks.items():
                target = child_sets[child_name]
                if target is not None:
                    target.add(tuple(row.get(f) or "" for f in fks))
        report.rows_checked[name] = count
    return True
