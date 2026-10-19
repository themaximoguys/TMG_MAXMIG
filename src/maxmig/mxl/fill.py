"""FillData — build an MxLoader sheet from source data plus per-column formulas.

MxLoader's FillData macro: the sheet's first data row (row 3) holds formulas
that reference a source worksheet (named in cell D1), and the macro drags
that row down once per source row, so Excel computes the load data. Here the
same thing runs headless, two ways:

1. **Template sheet** — an MxLoader sheet (.csv/.xlsx) whose row 3 holds
   formulas in MaxMig syntax (`=PROPER([Description])`, see
   `maxmig.mxl.formula`) and constants. The source comes from `--source`, or,
   in a workbook, from the worksheet named in D1 exactly like MxLoader. Every
   other worksheet in the workbook is available to VLOOKUP by its name.
2. **Mapping file** (YAML) — the same information as a reviewable,
   diffable file:

   ```yaml
   object_service: MXAPIASSET
   object: ASSET
   action: AddChange
   source: refdata/assets.csv          # or {file: legacy.xlsx, sheet: Assets}
   lookups:
     LOCMAP: refdata/locations.csv      # VLOOKUP([Location], "LOCMAP", "NEWLOC", FALSE)
   filter: '=[Status] <> "SCRAPPED"'    # optional: rows where FALSE are dropped
   script: transforms.py                # optional: UDFs + transform_row hook
   columns:
     ASSETNUM: '=CONCATENATE("PUMP", RIGHT([Asset], 3))'
     SITEID: BEDFORD                    # constants are copied to every row
     DESCRIPTION: '=PROPER([Description])'
     "[ASSETUID]": ""
   ```

Per-row formula errors don't stop the fill: every bad row is reported with
its source row number, so a whole source file can be fixed in one pass.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from maxmig.mxl.formula import FUNCTIONS, Formula, FormulaContext, FormulaError, LookupTable, evaluate_cell
from maxmig.mxl.scripting import NO_SCRIPT, Script, ScriptContext, load_script
from maxmig.mxl.sheet import START_ROW, MxSheet, SheetFormatError, _openpyxl, normalize_action, parse_header


@dataclass
class FillError:
    source_row: int  # 1-based data row of the source (header excluded)
    column: str
    message: str


@dataclass
class FillResult:
    sheet: MxSheet
    source_rows: int = 0
    dropped: int = 0  # by `filter` or transform_row returning None
    errors: list[FillError] = field(default_factory=list)


# --------------------------------------------------------------------------- tabular sources


def read_table(path: Path, sheet_name: str | None = None) -> tuple[list[str], list[list[Any]]]:
    """A plain table — header in row 1 — from a .csv or a worksheet of an .xlsx/.xlsm."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        workbook = _openpyxl().load_workbook(path, data_only=True, read_only=True)
        try:
            ws = workbook[sheet_name] if sheet_name else workbook.active
        except KeyError:
            raise SheetFormatError(f"{path}: no worksheet named {sheet_name!r} (have {', '.join(workbook.sheetnames)})") from None
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        workbook.close()
    else:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))
    if not rows:
        raise SheetFormatError(f"{path}{f'[{sheet_name}]' if sheet_name else ''}: empty — expected a header row")
    header = [("" if h is None else str(h)).strip() for h in rows[0]]
    while header and header[-1] == "":
        header.pop()
    data = [r for r in rows[1:] if any(v is not None and str(v).strip() != "" for v in r)]
    return header, data


def table_rows(header: list[str], data: Iterable[list[Any]]) -> Iterator[dict[str, Any]]:
    keys = [h.upper() for h in header]
    for row in data:
        yield {k: (row[i] if i < len(row) else None) for i, k in enumerate(keys) if k}


def load_lookup(name: str, path: Path, sheet_name: str | None = None) -> LookupTable:
    header, data = read_table(path, sheet_name)
    return LookupTable(name, header, data)


def parse_source_spec(spec: Any, base_dir: Path) -> tuple[Path, str | None]:
    """`file.csv`, `file.xlsx:Sheet`, or `{file: ..., sheet: ...}` — relative to `base_dir`."""
    if isinstance(spec, dict):
        file, sheet = spec.get("file"), spec.get("sheet")
    else:
        text = str(spec)
        head, sep, tail = text.rpartition(":")
        if sep and head.lower().endswith((".xlsx", ".xlsm")):
            file, sheet = head, tail
        else:
            file, sheet = text, None
    if not file:
        raise SheetFormatError(f"source/lookup spec {spec!r} has no file")
    path = Path(file)
    return (path if path.is_absolute() else base_dir / path), (sheet or None)


# --------------------------------------------------------------------------- core


def compile_cells(cells: list[Any]) -> list[Any]:
    """Text starting with `=` becomes a Formula; anything else stays a constant."""
    return [Formula.parse(c) if isinstance(c, str) and c.strip().startswith("=") else c for c in cells]


def fill(
    target: MxSheet,
    template: list[Any],
    rows: Iterable[dict[str, Any]],
    lookups: dict[str, LookupTable] | None = None,
    script: Script = NO_SCRIPT,
    row_filter: Formula | None = None,
    ctx: ScriptContext | None = None,
) -> FillResult:
    """Replace `target`'s data with one row per source row, each cell computed
    from `template` (one entry per column of `target`; [RESULT]/[ID] columns
    are normally blank). Source keys are matched case-insensitively."""
    compiled = compile_cells(list(template) + [None] * (len(target.columns) - len(template)))
    lookups = {k.upper(): v for k, v in (lookups or {}).items()}
    functions = {**FUNCTIONS, **script.functions}
    ctx = ctx or ScriptContext(sheet=target, action=target.action)
    result = FillResult(sheet=target)
    target.clear_data()

    for number, raw in enumerate(rows, start=1):
        result.source_rows += 1
        row = {str(k).upper(): v for k, v in raw.items()}
        if script.has("transform_row"):
            try:
                row = script.call("transform_row", row, ctx)
            except Exception as exc:
                result.errors.append(FillError(number, "transform_row", str(exc)))
                continue
            if row is None:
                result.dropped += 1
                continue
            row = {str(k).upper(): v for k, v in row.items()}
        fctx = FormulaContext(functions, lookups, row)
        if row_filter is not None:
            try:
                keep = row_filter.evaluate(fctx)
            except FormulaError as exc:
                result.errors.append(FillError(number, "filter", str(exc)))
                continue
            if keep in (False, 0, "", "FALSE", None):
                result.dropped += 1
                continue
        out: list[Any] = []
        failed = False
        for column, cell in zip(target.columns, compiled):
            try:
                value = evaluate_cell(cell, fctx)
            except FormulaError as exc:
                result.errors.append(FillError(number, column.header, str(exc)))
                failed = True
                break
            out.append("" if value is None else _cell(value))
        if not failed:
            target.data.append(out)
    return result


def _cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "1" if value else "0"  # MxLoader/YORN convention; values.py normalizes again
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------- template sheets


def template_row(sheet: MxSheet) -> list[Any]:
    """Row 3 as written — formulas as text, not Excel's cached results."""
    if sheet.path and sheet.path.suffix.lower() in (".xlsx", ".xlsm"):
        workbook = _openpyxl().load_workbook(sheet.path, data_only=False)
        ws = workbook[sheet.sheet_name] if sheet.sheet_name else workbook.active
        return [c for c in next(ws.iter_rows(min_row=START_ROW, max_row=START_ROW, values_only=True), ())]
    return list(sheet.data[0]) if sheet.data else []


def workbook_lookups(path: Path, exclude: set[str]) -> dict[str, LookupTable]:
    """Every other worksheet of the workbook, by name — MxLoader's 'RefData' sheets."""
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return {}
    workbook = _openpyxl().load_workbook(path, data_only=True, read_only=True)
    names = [n for n in workbook.sheetnames if n not in exclude]
    workbook.close()
    tables = {}
    for name in names:
        try:
            tables[name.upper()] = load_lookup(name, path, name)
        except SheetFormatError:
            continue  # empty sheets (Config, notes) aren't lookup tables
    return tables


# --------------------------------------------------------------------------- mapping files


@dataclass
class Mapping:
    sheet: MxSheet
    template: list[Any]
    source: tuple[Path, str | None]
    lookups: dict[str, LookupTable]
    row_filter: Formula | None
    script: Script


def load_mapping(path: Path) -> Mapping:
    import yaml

    path = Path(path)
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    missing = [k for k in ("object_service", "object", "source", "columns") if not spec.get(k)]
    if missing:
        raise SheetFormatError(f"{path}: missing {', '.join(missing)}")
    columns = spec["columns"]
    if not isinstance(columns, dict):
        raise SheetFormatError(f"{path}: columns must map MxLoader header -> formula or constant")
    headers = [str(h) for h in columns]
    sheet = MxSheet(
        object_service=str(spec["object_service"]).upper(),
        object_name=str(spec["object"]).upper(),
        action=normalize_action(str(spec.get("action", "AddChange"))),
        columns=[parse_header(i, h) for i, h in enumerate(headers)],
        where=str(spec.get("where", "") or ""),
    )
    lookups = {}
    for name, lookup_spec in (spec.get("lookups") or {}).items():
        file, sheet_name = parse_source_spec(lookup_spec, base)
        lookups[str(name).upper()] = load_lookup(str(name), file, sheet_name)
    script = NO_SCRIPT
    if spec.get("script"):
        script_path = Path(spec["script"])
        script = load_script(script_path if script_path.is_absolute() else base / script_path)
    row_filter = Formula.parse(str(spec["filter"])) if spec.get("filter") else None
    return Mapping(
        sheet=sheet,
        template=[columns[h] for h in columns],
        source=parse_source_spec(spec["source"], base),
        lookups=lookups,
        row_filter=row_filter,
        script=script,
    )
