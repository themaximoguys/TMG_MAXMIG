"""Sheet <-> node conversion — port of MxLoader's ConvSheetToNode.bas,
ConvNodeToSheet.bas and MxUtils.BuildAttrList.

Load direction (`sheet_to_nodes`): each row is split into one candidate node
per object path named in the header (main object, then `REL`, `REL.SUB`...).
A candidate is merged into the previous node at the same place when its
values are identical (MxNode.Equals) — which is how several sheet rows become
one parent with several children, exactly as in MxLoader:

    PONUM  SITEID  POLINE.POLINENUM  POLINE.ITEMNUM
    1001   BEDFORD 1                 A-100           -> PO 1001 { poline: [1, 2] }
    1001   BEDFORD 2                 A-200

**Deliberate fix over MxLoader:** ConvSheetToNode tracks a single "previous
object" cursor, so a header with two *sibling* relations (e.g. `POLINE.*` and
`POTERM.*`) nests the second relation under the first. Here every path is
resolved against its own parent path, so siblings land side by side.

Query direction (`members_to_rows`): an OSLC member is flattened to one row
per leaf child, parent values repeated on every row (ConvNodeToSheet's
TraverseHierarchy).
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from maxmig.mxl.node import MxNode
from maxmig.mxl.sheet import Column, MxSheet, SheetFormatError
from maxmig.mxl.values import is_blank

# (object path, attribute) -> Maximo type, or None for "send as string".
TypeResolver = Callable[[tuple[str, ...], str], "str | None"]


def _no_types(path: tuple[str, ...], attr: str) -> None:
    return None


def _node_name(sheet: MxSheet, path: tuple[str, ...]) -> str:
    return path[-1].upper() if path else sheet.object_name.upper()


def _row_candidates(
    sheet: MxSheet, data_index: int, columns_by_path: dict[tuple[str, ...], list[Column]], resolve: TypeResolver
) -> dict[tuple[str, ...], MxNode | None]:
    candidates: dict[tuple[str, ...], MxNode | None] = {}
    for path, columns in columns_by_path.items():
        node = MxNode(_node_name(sheet, path))
        for column in columns:
            value = sheet.cell(data_index, column)
            if not is_blank(value):
                node.add_attr(column.attr, value, resolve(path, column.attr))
        candidates[path] = node if node.attrs else None
    return candidates


class SheetLayout:
    """Per-sheet parse state that doesn't change between batches — computed
    once so a 1M-row sheet isn't rescanned for every 50-object batch."""

    def __init__(self, sheet: MxSheet):
        self.paths = [()] + sheet.relation_paths
        self.columns_by_path = {p: [c for c in sheet.attr_columns if c.path == p] for p in self.paths}
        self.end = sheet.data_row_count()


def sheet_to_nodes(
    sheet: MxSheet,
    start_index: int = 0,
    batch_size: int = 0,
    resolve: TypeResolver = _no_types,
    layout: SheetLayout | None = None,
) -> tuple[list[MxNode], int]:
    """Parse data rows from `start_index` (0-based into `sheet.data`) into
    top-level nodes. Stops after `batch_size` top-level objects (0 = no limit)
    or at the first empty row. Returns (nodes, next_start_index).

    A parent never splits across batches: rows continuing the current parent
    are consumed before the batch limit is checked, the same guarantee
    MxLoader gets by parsing one row too many and dropping it.
    """
    layout = layout or SheetLayout(sheet)
    paths, columns_by_path, end = layout.paths, layout.columns_by_path, layout.end
    roots: list[MxNode] = []

    index = start_index
    while index < end:
        row_number = sheet.row_number(index)
        candidates = _row_candidates(sheet, index, columns_by_path, resolve)
        root = candidates[()]
        if root is None:
            raise SheetFormatError(f"row {row_number}: no value for any main-object ({sheet.object_name}) attribute")

        resolved: dict[tuple[str, ...], MxNode] = {}
        if roots and roots[-1].same_values(root):
            resolved[()] = roots[-1]
        else:
            if batch_size > 0 and len(roots) >= batch_size:
                break
            roots.append(root)
            resolved[()] = root
        resolved[()].rows.append(row_number)

        for path in paths[1:]:
            node, parent = candidates[path], resolved.get(path[:-1])
            if node is None or parent is None:
                continue
            siblings = parent.child_list(path[-1])
            if siblings and siblings[-1].same_values(node):
                node = siblings[-1]
            else:
                siblings.append(node)
            node.rows.append(row_number)
            resolved[path] = node
        index += 1
    return roots, index


def build_select(sheet: MxSheet) -> str:
    """oslc.select for a Query — MxUtils.BuildAttrList: own attributes plus
    `rel{attr,...,sub{...}}` per child path. `[ID]` columns are selected too,
    so a query fills them in."""
    def clause(path: tuple[str, ...]) -> str:
        attrs = [c.attr for c in sheet.columns if c.kind in ("attr", "id") and c.path == path]
        nested = [clause(p) for p in sheet.relation_paths if len(p) == len(path) + 1 and p[: len(path)] == path]
        parts = list(dict.fromkeys(attrs)) + nested
        return ",".join(parts) if not path else f"{path[-1]}{{{','.join(parts)}}}"

    return clause(())


def _children_of(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def members_to_rows(members: Iterator[dict] | list[dict], sheet: MxSheet) -> Iterator[list[Any]]:
    """Flatten OSLC members to sheet rows (ConvNodeToSheet.DoConv): one row
    per leaf child object, parent values repeated; a member with none of the
    header's child relations populated still yields its own single row."""
    width = len(sheet.columns)
    relation_paths = sheet.relation_paths

    def flatten(obj: dict, path: tuple[str, ...], inherited: list[Any]) -> Iterator[list[Any]]:
        lowered = {str(k).lower(): v for k, v in obj.items()}
        values = list(inherited)
        for column in sheet.columns:
            if column.kind in ("attr", "id") and column.path == path:
                values[column.index] = lowered.get(column.attr)
        produced = False
        for child_path in (p for p in relation_paths if len(p) == len(path) + 1 and p[: len(path)] == path):
            for child in _children_of(lowered.get(child_path[-1])):
                produced = True
                yield from flatten(child, child_path, values)
        if not produced:
            yield values

    for member in members:
        yield from flatten(member, (), [None] * width)
