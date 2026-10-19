"""Bridge between MaxMig's YAML object definitions / export CSVs and the
MxLoader engine. MxLoader has no equivalent: there, the sheet header *is*
the definition. Here the same object can be described once in YAML and then:

- turned into an MxLoader sheet header/template (`template_sheet`) so it can
  be edited and loaded from Excel like any MxLoader sheet;
- streamed straight from an export's per-relation CSVs into nested MxNodes
  (`TreeReader`) — the "re-nest children into one payload per parent" step
  that lets `migrate import --bulk` load `po_line`, `invbalances`, etc. with
  their parent in one BULK record instead of not at all.

Custom attributes: every non-empty CSV column is sent, declared or not, so a
column added to the CSV by hand (or captured with `capture_all_attributes`)
loads without a YAML change. Types come from `field_types` first, then the
data dictionary under the node's `maximo_object`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from maxmig.mxl.datadict import DataDictionary
from maxmig.mxl.node import MxNode
from maxmig.mxl.sheet import MxSheet, normalize_action, parse_header


def _fk_list(foreign_key: str | list[str]) -> list[str]:
    return foreign_key if isinstance(foreign_key, list) else [foreign_key]


def _declared(node: Any) -> list[str]:
    all_fields = getattr(node, "all_fields", None)
    return all_fields() if callable(all_fields) else list(node.fields)


def object_service_of(obj_def: Any) -> str:
    if not obj_def.access.oslc_resource:
        raise ValueError(f"'{obj_def.name}' has no oslc_resource configured; MxLoader loads go through OSLC/REST")
    return obj_def.access.oslc_resource.upper()


def _maximo_object(node: Any) -> str:
    return (getattr(node, "maximo_object", None) or getattr(node, "relation_attribute", None) or node.name).upper()


@dataclass
class _PathInfo:
    node: Any
    maximo_object: str
    field_types: dict[str, str]


def path_index(obj_def: Any) -> dict[tuple[str, ...], _PathInfo]:
    """Object path (tuple of OSLC relation attributes) -> its definition."""
    index: dict[tuple[str, ...], _PathInfo] = {}

    def walk(node: Any, path: tuple[str, ...]) -> None:
        index[path] = _PathInfo(node, _maximo_object(node), dict(getattr(node, "field_types", {}) or {}))
        for child in node.children:
            walk(child, path + (child.relation_attribute.lower(),))

    walk(obj_def, ())
    return index


def make_resolver(obj_def: Any, datadict: DataDictionary | None = None):
    """TypeResolver for `obj_def`'s tree: YAML `field_types` override, then the
    dictionary entry for the node's Maximo object; unknown paths (a sheet
    header naming a relation the YAML doesn't) fall back to the relation name
    as object name, MxLoader's own convention."""
    index = path_index(obj_def)
    datadict = datadict or DataDictionary()

    def resolve(path: tuple[str, ...], attr: str) -> str | None:
        info = index.get(path)
        if info is not None:
            if attr in info.field_types:
                return info.field_types[attr]
            return datadict.type_of(info.maximo_object, attr)
        return datadict.type_of(path[-1] if path else None, attr)

    return resolve


def sheet_resolver(sheet: MxSheet, datadict: DataDictionary):
    """TypeResolver for a free-standing MxLoader sheet: main object from B1,
    child objects named by the relation segment — exactly MxSheet.GetAttrType."""

    def resolve(path: tuple[str, ...], attr: str) -> str | None:
        return datadict.type_of(path[-1] if path else sheet.object_name, attr)

    return resolve


# One encoding policy for every CSV this engine reads: utf-8-sig accepts files
# with or without a BOM (Excel adds one on save), so a hand-edited export still
# matches keys byte-for-byte.
CSV_ENCODING = "utf-8-sig"


def sheet_headers(obj_def: Any) -> list[str]:
    """MxLoader header for an object tree: the main object's key and declared
    fields (custom ones included), then each relation's own fields as
    `REL.ATTR` — minus the foreign-key columns a child inherits from its parent,
    which MxLoader sheets never repeat."""
    headers: list[str] = []

    def walk(node: Any, prefix: str, inherited: set[str]) -> None:
        declared = _declared(node)
        for attr in dict.fromkeys(list(node.key_fields) + declared):
            if attr in inherited and attr not in declared:
                continue
            headers.append(f"{prefix}{attr}".upper())
        for child in node.children:
            walk(child, f"{prefix}{child.relation_attribute}.", set(_fk_list(child.foreign_key)))

    walk(obj_def, "", set())
    return headers


def template_sheet(
    obj_def: Any, action: str = "AddChange", path: Path | None = None, extra_headers: list[str] = ()
) -> MxSheet:
    headers = sheet_headers(obj_def) + list(extra_headers) + ["[RESULT]"]
    return MxSheet(
        object_service=object_service_of(obj_def),
        object_name=_maximo_object(obj_def),
        action=normalize_action(action),
        columns=[parse_header(i, h) for i, h in enumerate(headers)],
        where=obj_def.where or "",
        path=path,
    )


class _Cursor:
    """Forward-only reader over one relation's CSV with one-row lookahead."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = Path(path).open(newline="", encoding=CSV_ENCODING)
        self._reader = csv.DictReader(self._fh)
        self.line = 1
        self._next = self._read()

    def _read(self) -> dict[str, str] | None:
        row = next(self._reader, None)
        if row is not None:
            self.line += 1
        return row

    def peek(self) -> dict[str, str] | None:
        return self._next

    def take(self) -> dict[str, str]:
        row, self._next = self._next, self._read()
        return row

    def drain(self) -> int:
        count = 0
        while self._next is not None:
            self.take()
            count += 1
        return count

    def close(self) -> None:
        self._fh.close()


@dataclass
class TreeReader:
    """Stream an export's parent CSV plus every relation CSV into one MxNode
    per parent row, children attached by foreign key.

    Children are consumed in lockstep with their parent (bounded memory — no
    relation CSV is ever loaded whole), which relies on child rows being
    grouped in parent order. `migrate export` always writes them that way;
    rows that don't line up (hand-reordered files, orphans) are counted in
    `unmatched` once iteration finishes — run `migrate check` to locate them.
    """

    obj_def: Any
    csv_paths: dict[str, Path]
    resolve: Any
    start_row: int = 1  # 1-based data row of the parent CSV to start from
    unmatched: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Types depend only on (path, attribute): resolve each pair once, not per cell.
        self.resolve = lru_cache(maxsize=None)(self.resolve)
        self._relations = {}  # definition id -> [(child, fks, exclude, child_path)]

    def _relations_of(self, definition: Any, path: tuple[str, ...]):
        key = (id(definition), path)
        if key not in self._relations:
            self._relations[key] = [
                (
                    child,
                    _fk_list(child.foreign_key),
                    set(_fk_list(child.foreign_key)) - set(_declared(child)),
                    path + (child.relation_attribute.lower(),),
                )
                for child in definition.children
            ]
        return self._relations[key]

    def __iter__(self) -> Iterator[MxNode]:
        cursors: dict[str, _Cursor] = {}

        def open_cursors(node: Any) -> None:
            for child in node.children:
                path = self.csv_paths.get(child.name)
                if path is not None and Path(path).is_file():
                    cursors[child.name] = _Cursor(Path(path))
                open_cursors(child)

        open_cursors(self.obj_def)
        try:
            with Path(self.csv_paths[self.obj_def.name]).open(newline="", encoding=CSV_ENCODING) as fh:
                for number, row in enumerate(csv.DictReader(fh), start=1):
                    if number < self.start_row:
                        self._skip(self.obj_def, (), row, cursors)  # resume: advance cursors only
                        continue
                    node = self._node(self.obj_def, (), row, exclude=set())
                    self._attach(self.obj_def, (), row, node, cursors)
                    node.rows.append(number)
                    yield node
            for name, cursor in cursors.items():
                leftover = cursor.drain()
                if leftover:
                    self.unmatched[name] = leftover
        finally:
            for cursor in cursors.values():
                cursor.close()

    def _node(self, definition: Any, path: tuple[str, ...], row: dict[str, str], exclude: set[str]) -> MxNode:
        node = MxNode(_maximo_object(definition))
        for attr, value in row.items():
            if attr is None or attr in exclude or value is None or value == "":
                continue
            node.add_attr(attr, value, self.resolve(path, attr.lower()))
        return node

    def _skip(self, definition: Any, path: tuple[str, ...], row: dict[str, str], cursors) -> None:
        for child, fks, _, child_path in self._relations_of(definition, path):
            cursor = cursors.get(child.name)
            if cursor is None:
                continue
            parent_key = [row.get(fk, "") for fk in fks]
            while (peek := cursor.peek()) is not None and [peek.get(fk, "") for fk in fks] == parent_key:
                self._skip(child, child_path, cursor.take(), cursors)

    def _attach(self, definition: Any, path: tuple[str, ...], row: dict[str, str], node: MxNode, cursors) -> None:
        for child, fks, exclude, child_path in self._relations_of(definition, path):
            cursor = cursors.get(child.name)
            if cursor is None:
                continue
            parent_key = [row.get(fk, "") for fk in fks]
            while (peek := cursor.peek()) is not None and [peek.get(fk, "") for fk in fks] == parent_key:
                child_row = cursor.take()
                child_node = self._node(child, child_path, child_row, exclude)
                self._attach(child, child_path, child_row, child_node, cursors)
                if child_node.attrs:
                    node.child_list(child.relation_attribute).append(child_node)


def batched(nodes: Iterator[MxNode], size: int) -> Iterator[list[MxNode]]:
    batch: list[MxNode] = []
    for node in nodes:
        batch.append(node)
        if size > 0 and len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def node_key(node: MxNode, key_fields: list[str]) -> dict[str, str]:
    return {k: str(node.find_attr(k) or "") for k in key_fields}


def export_tree_to_sheet(obj_def: Any, csv_paths: dict[str, Path], action: str = "AddChange", path: Path | None = None) -> MxSheet:
    """Join an export's parent + relation CSVs into one MxLoader sheet — one
    row per leaf child, parent values repeated — so an export can be opened,
    reviewed and loaded from Excel exactly like an MxLoader query result.
    Columns present in the CSVs but not declared (custom attributes) are
    appended to the header."""
    known = set(sheet_headers(obj_def))
    extra: list[str] = []

    def add_extras(definition: Any, prefix: str, csv_path: Path | None, inherited: set[str]) -> None:
        if csv_path and Path(csv_path).is_file():
            with Path(csv_path).open(newline="", encoding=CSV_ENCODING) as fh:
                header = next(csv.reader(fh), [])
            for attr in header:
                name = f"{prefix}{attr}".upper()
                if attr and attr not in inherited and name not in known and name not in extra:
                    extra.append(name)
        for child in definition.children:
            add_extras(child, f"{prefix}{child.relation_attribute}.", csv_paths.get(child.name), set(_fk_list(child.foreign_key)))

    add_extras(obj_def, "", csv_paths.get(obj_def.name), set())
    sheet = template_sheet(obj_def, action, path, extra_headers=extra)

    reader = TreeReader(obj_def, csv_paths, resolve=lambda p, a: None)
    for node in reader:
        for values in _node_rows(node, (), {}):
            sheet.data.append([values.get(c.header) for c in sheet.columns])
    return sheet


def _node_rows(node: MxNode, path: tuple[str, ...], inherited: dict[str, Any]) -> Iterator[dict[str, Any]]:
    prefix = "".join(f"{p}." for p in path).upper()
    values = dict(inherited)
    for attr in node.attrs:
        values[f"{prefix}{attr.name.upper()}"] = attr.value
    produced = False
    for relation, children in node.children.items():
        for child in children:
            produced = True
            yield from _node_rows(child, path + (relation,), values)
    if not produced:
        yield values
