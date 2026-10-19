"""Run orchestration — port of MxLoader's Main.MxRunSync / MxRunQuery.

`run_nodes` is the batch loop shared by every load path: it sends batches of
MxNodes through `MxRestService.sync`, counts created/updated/errors from each
record's `_responsemeta.status` (201 created; 200/204 updated — MxLoader's
mapping), and honours *stop on error*. `run_sheet` wraps it for an MxLoader
sheet, writing each object's outcome to `[RESULT]` and returned IDs to
`[ID]` columns on every row the object came from.

**Dry-run by default** (unlike MxLoader): with `execute=False` every payload
is still built and type-checked — so a bad number/date/YORN cell is caught —
but nothing is POSTed and the sheet is left untouched.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from maxmig.mxl.convert import SheetLayout, TypeResolver, _no_types, build_select, members_to_rows, sheet_to_nodes
from maxmig.mxl.node import MxNode
from maxmig.mxl.scripting import NO_SCRIPT, Script, ScriptContext
from maxmig.mxl.service import MxRestService, RecordResult
from maxmig.mxl.sheet import START_ROW, MxSheet, SheetFormatError
from maxmig.mxl.values import ValueConversionError


@dataclass
class RunOptions:
    execute: bool = False
    batch_size: int = 50  # top-level objects per BULK request; 0 = everything in one request
    stop_on_error: bool = True
    start_row: int = START_ROW  # sheet row to start from — the "resume from row" prompt
    timezone: str | None = None  # appended to DATETIME values lacking one, e.g. "+00:00"
    max_rows: int = 0  # Query only: 0 = no limit
    page_size: int = 500
    # Keep every ObjectOutcome (payload + node) on the summary. Off for
    # million-row imports, which stream outcomes through `on_outcome` instead.
    keep_outcomes: bool = True


@dataclass
class ObjectOutcome:
    rows: list[int]
    status: str  # "created" | "updated" | "error" | "dry_run" | "skipped"
    message: str
    payload: dict[str, Any] | None = None
    ids: dict[str, Any] = field(default_factory=dict)
    node: MxNode | None = None


@dataclass
class RunSummary:
    action: str
    dry_run: bool
    counts: Counter = field(default_factory=Counter)  # outcome status -> objects
    batches: int = 0
    stopped: bool = False
    resume_row: int | None = None  # first row of the first failed object, when stopped
    first_error_row: int | None = None
    outcomes: list[ObjectOutcome] = field(default_factory=list)  # only with keep_outcomes

    created = property(lambda self: self.counts["created"])
    updated = property(lambda self: self.counts["updated"])
    errors = property(lambda self: self.counts["error"])
    validated = property(lambda self: self.counts["dry_run"])
    skipped = property(lambda self: self.counts["skipped"])


def check_action(action: str) -> None:
    if action == "Sync":
        # MxLoader blocks this too ("Sync action is supported only for OS
        # service"): Sync deletes child objects missing from the input, and the
        # REST BULK API has no equivalent; AddChange never deletes children.
        raise SheetFormatError("Sync action is supported only for the MIF OS service; use AddChange over REST")
    if action == "Query":
        raise SheetFormatError("Query is a read action; use run_query")


def run_nodes(
    batches: Iterable[list[MxNode]],
    service: MxRestService | None,
    object_service: str,
    action: str,
    options: RunOptions,
    id_attrs: list[str] | None = None,
    base_dir: Path | None = None,
    on_outcome: Callable[[ObjectOutcome], None] | None = None,
    script: Script = NO_SCRIPT,
    ctx: ScriptContext | None = None,
) -> RunSummary:
    check_action(action)
    summary = RunSummary(action=action, dry_run=not options.execute)
    if ctx is None:
        ctx = ScriptContext(action=action, dry_run=not options.execute)
    script.run_level("before_run", ctx)

    def record(outcome: ObjectOutcome) -> None:
        summary.counts[outcome.status] += 1
        if outcome.status == "error" and outcome.rows:
            row = outcome.rows[0]
            summary.first_error_row = row if summary.first_error_row is None else min(summary.first_error_row, row)
        if options.keep_outcomes:
            summary.outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        if script.has("after_record"):
            try:
                script.call("after_record", outcome, ctx)
            except Exception as exc:  # reporting hook: log, never change the outcome
                ctx.log.warning("after_record failed for row %s: %s", outcome.rows[:1], exc)

    for batch in batches:
        summary.batches += 1
        sendable: list[tuple[MxNode, dict[str, Any]]] = []
        batch_failed = False
        for node in batch:
            node.action = action
            try:
                payload = node.to_payload(base_dir, options.timezone)
            except ValueConversionError as exc:
                batch_failed = True
                record(ObjectOutcome(node.rows, "error", str(exc), node=node))
                continue
            if script.has("before_record"):
                try:
                    payload = script.call("before_record", payload, ctx)
                except Exception as exc:
                    batch_failed = True
                    record(ObjectOutcome(node.rows, "error", f"before_record: {exc}", node=node))
                    continue
                if payload is None:
                    record(ObjectOutcome(node.rows, "skipped", "skipped by script", node=node))
                    continue
            sendable.append((node, payload))

        if not options.execute:
            for node, payload in sendable:
                record(ObjectOutcome(node.rows, "dry_run", f"DRY RUN: would {action}", payload, node=node))
        elif sendable:
            assert service is not None, "execute=True needs a service"
            results = service.sync(object_service, action, [p for _, p in sendable], id_attrs)
            for (node, payload), result in zip(sendable, results):
                record(_outcome(node, payload, result))
                batch_failed = batch_failed or not result.ok

        if batch_failed and options.stop_on_error:
            summary.stopped = True
            summary.resume_row = summary.first_error_row
            break
    script.run_level("after_run", summary, ctx)
    return summary


def _outcome(node: MxNode, payload: dict[str, Any], result: RecordResult) -> ObjectOutcome:
    status = "error" if not result.ok else "created" if result.created else "updated"
    return ObjectOutcome(node.rows, status, result.message, payload, result.data if result.ok else {}, node=node)


def iter_sheet_batches(sheet: MxSheet, options: RunOptions, resolve: TypeResolver) -> Iterator[list[MxNode]]:
    index = max(options.start_row - START_ROW, 0)
    layout = SheetLayout(sheet)
    while True:
        nodes, index = sheet_to_nodes(sheet, index, options.batch_size, resolve, layout)
        if not nodes:
            return
        yield nodes


def run_sheet(
    sheet: MxSheet,
    service: MxRestService | None,
    options: RunOptions,
    resolve: TypeResolver = _no_types,
    script: Script = NO_SCRIPT,
    ctx: ScriptContext | None = None,
) -> RunSummary:
    """MxRunSync for one sheet. With `execute=True` a `[RESULT]` column is
    added when missing (as MxLoader does for REST batches) and every object's
    outcome and returned IDs are written back into the sheet in memory — the
    caller saves it."""
    check_action(sheet.action)
    if options.execute:
        sheet.ensure_result_column()
    id_attrs = [c.attr for c in sheet.id_columns if not c.path]

    def write_back(outcome: ObjectOutcome) -> None:
        if not options.execute:
            return
        for row in outcome.rows:
            sheet.set_result(row, outcome.message)
            sheet.set_error(row, outcome.message if outcome.status == "error" else "")
            for attr, value in outcome.ids.items():
                sheet.set_id(row, attr, value)

    base_dir = sheet.path.parent if sheet.path else None
    return run_nodes(
        iter_sheet_batches(sheet, options, resolve),
        service,
        sheet.object_service,
        sheet.action,
        options,
        id_attrs=id_attrs,
        base_dir=base_dir,
        on_outcome=write_back,
        script=script,
        ctx=ctx or ScriptContext(sheet=sheet, action=sheet.action, dry_run=not options.execute),
    )


def run_query(sheet: MxSheet, service: MxRestService, options: RunOptions) -> int:
    """MxRunQuery: replace the sheet's data rows with the query result.
    Returns the number of top-level objects fetched."""
    if sheet.action != "Query":
        raise SheetFormatError(f"sheet action is {sheet.action!r}, not Query")
    members = list(
        service.query(
            sheet.object_service,
            build_select(sheet),
            where=sheet.where,
            order_by=sheet.order_by,
            directives=sheet.oslc_directives,
            page_size=options.page_size,
            max_rows=options.max_rows,
        )
    )
    sheet.clear_data()
    sheet.data.extend(members_to_rows(members, sheet))
    return len(members)
