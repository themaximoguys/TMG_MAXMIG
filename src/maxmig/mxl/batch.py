"""RunBatch — port of MxLoader's MxAddSheetBatch / MxRunBatch.

A batch sheet lists MxLoader sheets to run in order, one per row, and records
how each went — MxLoader's dependency-ordered cutover script (companies,
then items, then storerooms, then inventory...):

    row 1: RunBatch
    row 2: Sheet | Action | Status | Query | Sync | Error | Execution Date | Execution Duration
    row 3+: one sheet per row

`Sheet` is a worksheet name (when the batch lives in the same workbook, as in
MxLoader), a path to a .csv/.xlsx sheet (relative to the batch file), or
`book.xlsx:Worksheet`. `Action` overrides the sheet's own C1 action; blank
keeps it. Batch-only actions, as in MxLoader: `Skip` (record and move on)
and `ExportCsv` (write the sheet out as CSV under `Export/`, no Maximo call).

Like MxLoader, a sheet that stops on error stops the batch, so later sheets
that depend on it never run against half-loaded data; with
`continue_on_error` the batch carries on and reports every failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from maxmig.mxl.sheet import LOAD_ACTIONS, MxSheet, SheetFormatError, _cell_text, _openpyxl, load_sheet, normalize_action

BATCH_MARKER = "RunBatch"
BATCH_HEADER = ("Sheet", "Action", "Status", "Query", "Sync", "Error", "Execution Date", "Execution Duration")
BATCH_ONLY_ACTIONS = ("ExportCsv", "Skip")


@dataclass
class BatchEntry:
    row: int  # sheet row number (3-based, like MxLoader)
    sheet: str
    action: str = ""
    status: str = ""
    query: str = ""
    sync: str = ""
    error: str = ""
    executed_at: str = ""
    duration: str = ""

    def cells(self) -> list[str]:
        return [self.sheet, self.action, self.status, self.query, self.sync, self.error, self.executed_at, self.duration]


@dataclass
class BatchSheet:
    entries: list[BatchEntry]
    path: Path
    sheet_name: str | None = None

    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path)
        rows = [[BATCH_MARKER], list(BATCH_HEADER)] + [e.cells() for e in self.entries]
        if target.suffix.lower() in (".xlsx", ".xlsm"):
            openpyxl = _openpyxl()
            if target.exists():
                wb = openpyxl.load_workbook(target, keep_vba=target.suffix.lower() == ".xlsm")
                title = self.sheet_name or BATCH_MARKER
                ws = wb[title] if title in wb.sheetnames else wb.create_sheet(title)
                ws.delete_rows(1, ws.max_row)
            else:
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = self.sheet_name or BATCH_MARKER
            for r, row in enumerate(rows, start=1):
                for c, value in enumerate(row, start=1):
                    ws.cell(row=r, column=c, value=value or None)
            wb.save(target)
        else:
            import csv

            tmp = target.with_name(target.name + ".partial")
            with tmp.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows)
            tmp.replace(target)
        return target


def normalize_batch_action(action: str) -> str:
    text = (action or "").strip()
    if not text:
        return ""
    for special in BATCH_ONLY_ACTIONS:
        if text.lower() == special.lower():
            return special
    if text.lower() == "exportxml":
        raise SheetFormatError("ExportXml is MxLoader's MIF/XML path, which MaxMig doesn't implement; use ExportCsv")
    return normalize_action(text)


def load_batch(path: Path, sheet_name: str | None = None) -> BatchSheet:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        wb = _openpyxl().load_workbook(path, data_only=True)
        if sheet_name is None:
            sheet_name = next((ws.title for ws in wb.worksheets if _cell_text(ws.cell(1, 1).value).strip() == BATCH_MARKER), None)
            if sheet_name is None:
                raise SheetFormatError(f"{path}: no worksheet has {BATCH_MARKER!r} in A1")
        rows = [list(r) for r in wb[sheet_name].iter_rows(values_only=True)]
    else:
        import csv

        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))
    if not rows or _cell_text(rows[0][0] if rows[0] else "").strip() != BATCH_MARKER:
        raise SheetFormatError(f"{path}: cell A1 must be {BATCH_MARKER!r}")
    entries = []
    for offset, row in enumerate(rows[2:]):
        cells = [_cell_text(v).strip() for v in list(row) + [""] * len(BATCH_HEADER)]
        if not cells[0]:
            continue
        entries.append(BatchEntry(3 + offset, cells[0], normalize_batch_action(cells[1]), *cells[2:8]))
    return BatchSheet(entries, path, sheet_name)


def resolve_sheet(entry: BatchEntry, batch: BatchSheet) -> tuple[Path, str | None]:
    ref = entry.sheet
    head, sep, tail = ref.rpartition(":")
    if sep and head.lower().endswith((".xlsx", ".xlsm")):
        path, worksheet = Path(head), tail
    elif ref.lower().endswith((".csv", ".xlsx", ".xlsm")):
        path, worksheet = Path(ref), None
    elif batch.path.suffix.lower() in (".xlsx", ".xlsm"):
        return batch.path, ref  # a worksheet of the batch's own workbook
    else:
        raise SheetFormatError(f"row {entry.row}: {ref!r} is not a .csv/.xlsx path (a bare worksheet name needs an .xlsx batch)")
    return (path if path.is_absolute() else batch.path.parent / path), worksheet


@dataclass
class EntryReport:
    """What a sheet run returns to the batch: counts for the Query/Sync cells,
    an error text, and whether the batch must stop."""

    query: int | None = None
    sync: int | None = None
    error: str = ""
    stop: bool = False
    note: str = ""  # e.g. "(truncated)"


@dataclass
class BatchSummary:
    ran: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_at: int | None = None
    entries: list[BatchEntry] = field(default_factory=list)


def export_csv(sheet: MxSheet, batch: BatchSheet, name: str) -> Path:
    target = batch.path.parent / "Export" / f"{name}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target)
    return target


def run_batch(
    batch: BatchSheet,
    run_one: Callable[[MxSheet, str], EntryReport],
    continue_on_error: bool = False,
    save: Callable[[MxSheet], None] | None = None,
    clock: Callable[[], datetime] = datetime.now,
) -> BatchSummary:
    """Run every entry in order. `run_one(sheet, action)` does the Maximo work
    (query or load) and returns an EntryReport; `save(sheet)` persists the
    sheet's [RESULT]/[ID] write-back. The batch's own status columns are
    updated in place — the caller saves the batch."""
    summary = BatchSummary(entries=batch.entries)
    for entry in batch.entries:
        entry.status = entry.query = entry.sync = entry.error = entry.executed_at = entry.duration = ""
    for entry in batch.entries:
        if entry.action == "Skip":
            entry.status = "Skipped"
            summary.skipped += 1
            continue
        started = time.monotonic()
        entry.executed_at = clock().replace(microsecond=0).isoformat(sep=" ")
        try:
            path, worksheet = resolve_sheet(entry, batch)
            sheet = load_sheet(path, worksheet)
            if entry.action == "ExportCsv":
                target = export_csv(sheet, batch, worksheet or path.stem)
                report = EntryReport(note=f"-> {target.name}")
            else:
                if entry.action:
                    sheet.action = entry.action
                report = run_one(sheet, sheet.action)
                if save is not None and (sheet.action == "Query" or sheet.action in LOAD_ACTIONS):
                    save(sheet)
        except Exception as exc:  # a broken entry is reported in its row, like frmError
            report = EntryReport(error=str(exc), stop=True)
        entry.duration = f"{time.monotonic() - started:.1f}s"
        entry.query = "" if report.query is None else str(report.query)
        entry.sync = "" if report.sync is None else str(report.sync)
        entry.error = report.error
        entry.status = ("Error" if report.error else "OK") + (f" {report.note}" if report.note else "")
        summary.ran += 1
        if report.error:
            summary.failed += 1
            if report.stop and not continue_on_error:
                summary.stopped_at = entry.row
                break
    return summary


def new_batch(sheets: list[tuple[str, str]], path: Path) -> BatchSheet:
    """MxAddSheetBatch: a batch listing the given (sheet, action) pairs."""
    return BatchSheet([BatchEntry(3 + i, s, normalize_batch_action(a)) for i, (s, a) in enumerate(sheets)], Path(path))


def entry_counts_from_summary(summary: Any) -> tuple[int, str]:
    """(objects synced OK, error text) from a runner RunSummary."""
    ok = summary.created + summary.updated + summary.validated
    if summary.errors:
        return ok, f"{summary.errors} object(s) failed" + (f"; first at row {summary.first_error_row}" if summary.first_error_row else "")
    return ok, ""
