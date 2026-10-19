"""`migrate` CLI — `export` streams one object structure (or, with `--object`
omitted, every object in a module's `extract_order`) out of a Maximo
environment into a CSV plus a run manifest; `import` reads a CSV back and
creates/updates matching records in a (usually different) Maximo environment.

Every expected failure (bad env file, unknown module/object, malformed filter,
API error after retries) is surfaced as a one-line click.ClickException on
stderr with exit status 1 — a raw traceback is never the only output.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable

import click

from maxmig.api.client import MaximoClient
from maxmig.api.filter import Filter
from maxmig.api.oslc import SkippedRow
from maxmig.config import EnvConfig, load_env
from maxmig.extract.checkpoint import CheckpointError, ExportCheckpoint, find_latest, fingerprint, timestamp_of
from maxmig.extract.writer import ObjectExportResult, write_object_tree, write_object_tree_resumable
from maxmig.load.importer import ImportRowResult, import_row
from maxmig.load.reader import read_csv_rows
from maxmig.model.registry import ModuleLoadError, ObjectRegistry
from maxmig.discover import draft_module_yaml, probe_object, write_draft
from maxmig.integrity import check_tree
from maxmig.model.schema import ObjectDef, with_capture_all, with_extra_fields
from maxmig.mxl.bridge import (
    TreeReader,
    batched,
    export_tree_to_sheet,
    make_resolver,
    node_key,
    path_index,
    sheet_resolver,
    template_sheet,
)
from maxmig.mxl.batch import EntryReport, entry_counts_from_summary, load_batch, run_batch
from maxmig.mxl.fill import (
    fill, load_lookup, load_mapping, parse_source_spec, read_table, table_rows, template_row, workbook_lookups,
)
from maxmig.mxl.formula import Formula, FormulaError
from maxmig.mxl.scripting import NO_SCRIPT, ScriptContext, ScriptError, load_script
from maxmig.mxl.datadict import DEFAULT_DICTIONARY_OS, OOB_DICTIONARY_OS, DataDictionary, fetch_from_maximo
from maxmig.mxl.runner import RunOptions, run_nodes, run_query, run_sheet
from maxmig.mxl.service import MxRestService
from maxmig.mxl.sheet import LOAD_ACTIONS, SheetFormatError, load_sheet, normalize_action
from maxmig.runs import describe_modules, describe_objects, latest_runs

_RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# Sync is MIF-OS-only (it deletes unlisted children); REST BULK loads offer the rest.
_REST_LOAD_ACTIONS = tuple(a for a in LOAD_ACTIONS if a != "Sync")


@click.group()
@click.version_option(package_name="maxmig")
def main() -> None:
    """IBM Maximo / MAS data migration CLI."""


def _resolve_env_file(env_prefix: str, env_file: Path | None) -> Path:
    if env_file is not None:
        return env_file
    path = Path.cwd() / "env" / f".env.{env_prefix.lower()}"
    if not path.is_file():
        raise click.ClickException(
            f"no env file at {path}; pass --env-file to point at one explicitly"
        )
    return path


def _load_env_config(env_prefix: str, env_file: Path | None) -> EnvConfig:
    path = _resolve_env_file(env_prefix, env_file)
    try:
        return load_env(env_prefix, path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


def _load_registry(config_dirs: tuple[Path, ...]) -> ObjectRegistry:
    try:
        return ObjectRegistry(config_dirs or None)
    except (ModuleLoadError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def _lookup_object(registry: ObjectRegistry, module: str, object_name: str) -> ObjectDef:
    try:
        return registry.get_object(module, object_name)
    except KeyError as exc:
        raise click.ClickException(str(exc.args[0])) from exc


def _resolve_object_names(
    registry: ObjectRegistry, module: str, object_name: str | None, order: str = "extract_order"
) -> list[str]:
    """A single object if `--object` was given; otherwise every object in the
    module's `extract_order` (declared object order, if `extract_order` is
    empty — e.g. a hand-written custom-object module that never set it)."""
    if object_name is not None:
        return [object_name]
    try:
        module_def = registry.get_module(module)
    except KeyError as exc:
        raise click.ClickException(str(exc.args[0])) from exc
    return getattr(module_def, order) or module_def.object_names()


def _parse_filter_conditions(filter_kv: tuple[str, ...]) -> dict[str, str]:
    conditions: dict[str, str] = {}
    for entry in filter_kv:
        field, sep, value = entry.partition("=")
        if not sep or not field.strip():
            raise click.ClickException(
                f"malformed --filter {entry!r}; expected field=value"
            )
        conditions[field.strip().lower()] = value
    return conditions


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_skip_log(object_dir: Path, object_name: str, timestamp: str, skipped: list[SkippedRow]) -> Path:
    path = object_dir / f"{object_name}_{timestamp}.skipped.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "resource": s.resource,
                    "pageno": s.pageno,
                    "reason_code": s.reason_code,
                    "message": s.message,
                }
            )
            + "\n"
            for s in skipped
        ),
        encoding="utf-8",
    )
    return path


def _open_checkpoint(
    module: str,
    object_name: str,
    obj_def: ObjectDef,
    env_config: EnvConfig,
    row_filter: Filter,
    out: Path,
    timestamp: str,
    page_size: int,
    resume: bool,
) -> ExportCheckpoint:
    """The checkpoint to write to: with `resume`, the newest existing one for
    this object (verified to describe the same query), else a fresh one."""
    object_dir = out / module / object_name
    fp = fingerprint(
        definition=obj_def.model_dump(mode="json"),
        filter=asdict(row_filter),
        page_size=page_size,
    )
    if resume:
        found = find_latest(object_dir, object_name)
        if found is not None:
            checkpoint = ExportCheckpoint.load(found)
            checkpoint.check_matches(module=module, object_name=object_name, env_prefix=env_config.prefix, fp=fp)
            return checkpoint
        click.echo(f"no checkpoint for {object_name} under {object_dir}; starting a fresh checkpointed export")
    object_dir.mkdir(parents=True, exist_ok=True)
    return ExportCheckpoint(
        path=ExportCheckpoint.path_for(object_dir, object_name, timestamp),
        module=module,
        object=object_name,
        env_prefix=env_config.prefix,
        timestamp=timestamp,
        fingerprint=fp,
    )


def _export_one(
    module: str,
    object_name: str,
    obj_def: ObjectDef,
    env_config: EnvConfig,
    row_filter: Filter,
    out: Path,
    timestamp: str,
    flat: bool,
    skip_errors: bool,
    checkpoint_mode: bool = False,
    resume: bool = False,
    limit: int = 0,
) -> ObjectExportResult:
    """Fetch and write one object structure (its own CSV plus one per child
    relation), its manifest, and its skip log if anything was skipped. Prints
    progress as it goes. Returns the top-level object's result, for a
    whole-module run's final summary line."""
    if flat:
        obj_def = obj_def.model_copy(update={"children": []})

    started_at = datetime.now(timezone.utc)
    skipped: list[SkippedRow] = []
    checkpoint: ExportCheckpoint | None = None

    try:
        client = MaximoClient(env_config, page_size=min(limit, 250)) if limit else MaximoClient(env_config)
        if checkpoint_mode:
            checkpoint = _open_checkpoint(
                module, object_name, obj_def, env_config, row_filter, out, timestamp, client.page_size, resume
            )
            started_at = datetime.fromisoformat(checkpoint.started_at)
            skipped = [SkippedRow(**s) for s in checkpoint.skipped]

            def on_skip(row: SkippedRow) -> None:
                skipped.append(row)
                checkpoint.skipped.append(asdict(row))

            if checkpoint.resuming:
                click.echo(
                    f"resuming {object_name} from {checkpoint.api} cursor {checkpoint.cursor} "
                    f"({checkpoint.nodes[object_name]['count']} rows already written)"
                )
                checkpoint.resume_count += 1
            if checkpoint.resuming and checkpoint.cursor is None:
                pages: Iterable = iter(())  # every page was written; only the final rename was missing
            else:
                pages = client.fetch_pages(
                    obj_def, filter=row_filter, skip_errors=skip_errors, on_skip=on_skip,
                    api=checkpoint.api, cursor=checkpoint.cursor,
                )
            results = write_object_tree_resumable(pages, obj_def, out, module, checkpoint.timestamp, checkpoint)
            timestamp = checkpoint.timestamp
        else:
            rows = client.fetch_object(
                obj_def, filter=row_filter, skip_errors=skip_errors, on_skip=skipped.append
            )
            if limit:
                rows = islice(rows, limit)  # stops paging once `limit` top-level rows are in
            results = write_object_tree(rows, obj_def, out, module, timestamp)
    except CheckpointError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        hint = ""
        if checkpoint is not None and checkpoint.path.exists():
            hint = (
                f"\n  progress saved after {checkpoint.pages} page(s) in {checkpoint.path}"
                "\n  re-run the same command with --resume to continue from there"
            )
        raise click.ClickException(f"export of '{object_name}' failed: {exc}{hint}") from exc

    top = results[object_name]
    object_dir = top.path.parent

    skipped_log_path = _write_skip_log(object_dir, object_name, timestamp, skipped) if skipped else None

    _write_manifest(
        object_dir / f"{object_name}_{timestamp}.manifest.json",
        {
            "module": module,
            "object": object_name,
            "env_prefix": env_config.prefix,
            "row_count": top.row_count,
            "csv_path": str(top.path),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "children": {
                name: {"row_count": result.row_count, "csv_path": str(result.path)}
                for name, result in results.items()
                if name != object_name
            },
            **({"limit": limit} if limit else {}),
            "skipped_count": len(skipped),
            "skipped_log": str(skipped_log_path) if skipped_log_path else None,
            **(
                {"checkpointed": True, "resumed": checkpoint.resume_count > 0, "resume_count": checkpoint.resume_count}
                if checkpoint is not None
                else {}
            ),
        },
    )
    if checkpoint is not None:
        checkpoint.delete()
    click.echo(f"exported {top.row_count} rows to {top.path}")
    for name, result in results.items():
        if name != object_name:
            click.echo(f"  + {result.row_count} rows to {result.path}")
    if skipped:
        click.echo(f"  ! skipped {len(skipped)} rows (see {skipped_log_path})")

    return top


@main.command("export")
@click.option("--module", required=True, help="Module name, e.g. purchasing")
@click.option(
    "--object",
    "object_name",
    default=None,
    help=(
        "Object name within the module, e.g. company. Omit to export every object in the "
        "module's extract_order (declaration order if extract_order is empty), one after another."
    ),
)
@click.option("--env", "env_prefix", required=True, help="Env prefix, e.g. MASDEV")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to the .env file; defaults to env/.env.<prefix> (lowercased) under the cwd",
)
@click.option(
    "--config-dir",
    "config_dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Module YAML directory; repeatable. Defaults to the project's config/ dirs.",
)
@click.option("--filter", "filter_kv", multiple=True, help="field=value, repeatable, ANDed together")
@click.option("--site", default=None, help="Convenience site-scoping filter")
@click.option(
    "--where",
    "raw_where",
    default=None,
    help="Raw OSLC where clause override — ignores --filter/--site",
)
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("exports"),
    help=(
        "Base output directory. Each object/relation gets its own stable "
        "{out}/{module}/{object_name}/ folder, reused across runs, holding one "
        "timestamped CSV per run — e.g. exports/inventory/item/item_<timestamp>.csv"
    ),
)
@click.option(
    "--flat",
    is_flag=True,
    default=False,
    help="Export only the object's own fields — skip child relations (e.g. invbalances) entirely",
)
@click.option(
    "--skip-errors",
    is_flag=True,
    default=False,
    help=(
        "OSLC only: skip and log individual records that fail with a known-recoverable "
        "Maximo error (e.g. the rotating-item BMXAA1825E bug) instead of aborting the export"
    ),
)
@click.option(
    "--extra-field",
    "extra_fields",
    multiple=True,
    help=(
        "Custom attribute to export in addition to the YAML's fields, repeatable: 'attr' for the "
        "object itself, 'relation.attr' for a child (e.g. po_line.ex_contractref)"
    ),
)
@click.option(
    "--all-attributes",
    is_flag=True,
    default=False,
    help=(
        "Also export every attribute the API returns that the YAML doesn't declare (e.g. custom "
        "attributes nobody listed yet), as extra columns after the declared ones"
    ),
)
@click.option(
    "--checkpoint",
    "checkpoint_mode",
    is_flag=True,
    default=False,
    help=(
        "Record progress after every page ({object}_<timestamp>.checkpoint.json) and keep the "
        ".partial files if the export fails, so it can be continued with --resume"
    ),
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help=(
        "Continue the newest checkpointed export of this object from its last completed page "
        "(implies --checkpoint; starts a fresh checkpointed export if there's none). With --object "
        "omitted, objects whose export already finished are skipped"
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Export at most N top-level records per object (with all their child rows) — "
        "a quick smoke test of an object/environment, not a migration extract"
    ),
)
def export_cmd(
    module: str,
    object_name: str | None,
    env_prefix: str,
    env_file: Path | None,
    config_dirs: tuple[Path, ...],
    filter_kv: tuple[str, ...],
    site: str | None,
    raw_where: str | None,
    out: Path,
    flat: bool,
    skip_errors: bool,
    extra_fields: tuple[str, ...] = (),
    all_attributes: bool = False,
    checkpoint_mode: bool = False,
    resume: bool = False,
    limit: int | None = None,
) -> None:
    """Extract one object structure to CSV — its own CSV, plus one per child
    relation (e.g. item_spec, item_structure) unless --flat is passed. With
    --object omitted, extracts every object in the module in turn instead."""
    conditions = _parse_filter_conditions(filter_kv)
    env_config = _load_env_config(env_prefix, env_file)
    registry = _load_registry(config_dirs)
    object_names = _resolve_object_names(registry, module, object_name)
    try:
        row_filter = Filter(site=site, conditions=conditions, raw_where=raw_where)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if limit and (checkpoint_mode or resume):
        raise click.ClickException("--limit can't be combined with --checkpoint/--resume")
    timestamp = datetime.now(timezone.utc).strftime(_RUN_TIMESTAMP_FORMAT)
    if resume and len(object_names) > 1:
        timestamp = _module_resume_timestamp(out, module, object_names) or timestamp
    results = []
    for name in object_names:
        if resume and len(object_names) > 1:
            done = _finished_export(out, module, name, timestamp)
            if done is not None:
                click.echo(f"{name}: already exported in run {timestamp} ({done.row_count} rows), skipping")
                results.append(done)
                continue
        obj_def = _customize(_lookup_object(registry, module, name), extra_fields, all_attributes)
        results.append(
            _export_one(
                module, name, obj_def, env_config, row_filter, out, timestamp, flat, skip_errors,
                checkpoint_mode=checkpoint_mode or resume, resume=resume, limit=limit or 0,
            )
        )

    if len(results) > 1:
        total_rows = sum(result.row_count for result in results)
        click.echo(f"module '{module}': exported {len(results)} objects, {total_rows} total rows")


def _module_resume_timestamp(out: Path, module: str, object_names: list[str]) -> str | None:
    """The run timestamp of an interrupted whole-module export: the newest
    checkpoint left by any of its objects."""
    stamps = [
        timestamp_of(found, name)
        for name in object_names
        if (found := find_latest(out / module / name, name)) is not None
    ]
    return max(stamps) if stamps else None


def _finished_export(out: Path, module: str, object_name: str, timestamp: str) -> ObjectExportResult | None:
    """The completed result of `object_name` in run `timestamp`, if its manifest exists."""
    manifest = out / module / object_name / f"{object_name}_{timestamp}.manifest.json"
    if not manifest.exists():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return ObjectExportResult(path=Path(data["csv_path"]), row_count=data["row_count"])


def _default_datadict_path(env_config: EnvConfig) -> Path:
    """Where `migrate datadict refresh` caches an environment's dictionary."""
    return Path.cwd() / "config" / "datadict" / f"{env_config.prefix.lower()}.csv"


def _load_datadict(env_config: EnvConfig, files: tuple[Path, ...]) -> DataDictionary:
    """Explicit --datadict files (later wins), else the cached one for this
    env if `migrate datadict refresh` has been run; else empty (every value of
    unknown type is sent as a string, YAML field_types still apply)."""
    paths = list(files) or [p for p in [_default_datadict_path(env_config)] if p.is_file()]
    result = DataDictionary()
    for path in paths:
        try:
            result = result.merge(DataDictionary.from_csv(path))
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"cannot read data dictionary {path}: {exc}") from exc
    return result


def _customize(obj_def: ObjectDef, extra_fields: tuple[str, ...], all_attributes: bool) -> ObjectDef:
    """Apply run-time custom-attribute options to an object definition."""
    try:
        obj_def = with_extra_fields(obj_def, list(extra_fields))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    return with_capture_all(obj_def) if all_attributes else obj_def


def _resolve_csv_file(csv_file: Path | None, out: Path, module: str, object_name: str) -> Path:
    """An explicit --csv-file always wins; otherwise auto-locate the newest
    export for this module/object under `out` (same {out}/{module}/{object}/
    folder `export` writes to) — the timestamp-suffixed filenames sort
    chronologically, so the lexicographically-last match is the newest."""
    if csv_file is not None:
        return csv_file
    directory = out / module / object_name
    candidates = sorted(directory.glob(f"{object_name}_*.csv")) if directory.is_dir() else []
    if not candidates:
        raise click.ClickException(
            f"no exported CSV found under {directory}; run `migrate export` first, "
            "or pass --csv-file to point at one explicitly"
        )
    return candidates[-1]


def _write_import_log(path: Path, results: list[ImportRowResult]) -> None:
    path.write_text(
        "".join(
            json.dumps({"key": r.key, "action": r.action, "href": r.href, "error": r.error}) + "\n"
            for r in results
        ),
        encoding="utf-8",
    )


_IMPORT_OPTIONS_HELP = (
    "Create-or-update records in Maximo from exported CSVs, matched by each object's natural key "
    "(key_fields). Dry-run by default -- pass --execute to actually write. OSLC only."
)


@main.command("import", help=_IMPORT_OPTIONS_HELP)
@click.option("--module", required=True, help="Module name, e.g. inventory")
@click.option(
    "--object",
    "object_name",
    default=None,
    help="Object name within the module, e.g. item. Omit to import every object in the module's load_order.",
)
@click.option("--env", "env_prefix", required=True, help="Env prefix to import INTO, e.g. MAXDEMOTST")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to the .env file; defaults to env/.env.<prefix> (lowercased) under the cwd",
)
@click.option(
    "--config-dir",
    "config_dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Module YAML directory; repeatable. Defaults to the project's config/ dirs.",
)
@click.option(
    "--csv-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Explicit CSV file to import (single --object only). Defaults to the newest export under --out.",
)
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("exports"),
    help="Base directory to auto-locate the newest export from, when --csv-file is not given",
)
@click.option(
    "--execute",
    is_flag=True,
    default=False,
    help=(
        "Actually create/update records in Maximo. Without this flag, migrate import only "
        "reports what it WOULD do -- nothing is written."
    ),
)
@click.option(
    "--bulk",
    is_flag=True,
    default=False,
    help=(
        "Load through the MxLoader engine: child relation CSVs (po_line, invbalances, ...) are re-nested "
        "into their parent's payload, values typed via the data dictionary, and records sent in OSLC BULK "
        "batches with a per-record result. Without it, one natural-key lookup + one write per row, no children."
    ),
)
@click.option(
    "--action",
    type=click.Choice(_REST_LOAD_ACTIONS, case_sensitive=False),
    default="AddChange",
    show_default=True,
    help="--bulk only: MxLoader action sent as each record's _action",
)
@click.option("--batch-size", type=int, default=50, show_default=True, help="--bulk only: objects per BULK request (0 = all)")
@click.option(
    "--continue-on-error",
    is_flag=True,
    default=False,
    help="--bulk only: keep going after a failed batch (MxLoader 'Stop on error = False')",
)
@click.option(
    "--start-row",
    type=int,
    default=1,
    show_default=True,
    help="1-based data row of the (parent) CSV to start from -- resume a stopped import",
)
@click.option(
    "--datadict",
    "datadict_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="--bulk only: MxAttrCfg-style data dictionary CSV (see `migrate datadict`); repeatable, later wins",
)
@click.option("--timezone", "tz", default=None, help="--bulk only: offset appended to DATETIME values lacking one, e.g. +00:00")
def import_cmd(
    module: str,
    object_name: str | None,
    env_prefix: str,
    env_file: Path | None,
    config_dirs: tuple[Path, ...],
    csv_file: Path | None,
    out: Path,
    execute: bool,
    bulk: bool = False,
    action: str = "AddChange",
    batch_size: int = 50,
    continue_on_error: bool = False,
    start_row: int = 1,
    datadict_files: tuple[Path, ...] = (),
    tz: str | None = None,
) -> None:
    env_config = _load_env_config(env_prefix, env_file)
    registry = _load_registry(config_dirs)
    if object_name is None and csv_file is not None:
        raise click.ClickException("--csv-file needs --object (it names one object's CSV)")
    object_names = _resolve_object_names(registry, module, object_name, order="load_order")
    if start_row < 1:
        raise click.ClickException("--start-row must be 1 or more")

    datadict = _load_datadict(env_config, datadict_files) if bulk else None
    for index, name in enumerate(object_names):
        obj_def = _lookup_object(registry, module, name)
        if not obj_def.access.oslc_resource:
            raise click.ClickException(
                f"'{name}' has no oslc_resource configured; migrate import only supports OSLC objects"
            )
        resolved_csv = _resolve_csv_file(csv_file, out, module, name)
        first_row = start_row if index == 0 else 1
        if bulk:
            failed, log_path = _import_bulk(
                module, name, obj_def, env_config, resolved_csv, out, execute, datadict,
                action, batch_size, not continue_on_error, first_row, tz,
            )
        else:
            failed, log_path = _import_rows(module, name, obj_def, env_config, resolved_csv, out, execute, first_row)
        if failed:
            remaining = object_names[index + 1:]
            suffix = f"; not imported: {', '.join(remaining)}" if remaining else ""
            raise click.ClickException(f"{failed} row(s) failed — see {log_path}{suffix}")


def _import_rows(
    module: str,
    object_name: str,
    obj_def: ObjectDef,
    env_config: EnvConfig,
    resolved_csv: Path,
    out: Path,
    execute: bool,
    start_row: int,
) -> tuple[int, Path]:
    """Original per-row path: natural-key lookup + one POST/PATCH per row.
    Every CSV column with a value is sent — declared, custom or hand-added."""
    started_at = datetime.now(timezone.utc)
    timestamp = started_at.strftime(_RUN_TIMESTAMP_FORMAT)
    client = MaximoClient(env_config)

    results: list[ImportRowResult] = []
    try:
        for number, row in enumerate(read_csv_rows(resolved_csv), start=1):
            if number < start_row:
                continue
            results.append(
                import_row(
                    client.session,
                    client.env.manage_url,
                    client.headers,
                    obj_def.access.oslc_resource,
                    row,
                    obj_def.key_fields,
                    execute=execute,
                )
            )
    except Exception as exc:
        raise click.ClickException(f"import of '{object_name}' failed: {exc}") from exc

    counts = Counter(result.action for result in results)
    object_dir = out / module / object_name
    object_dir.mkdir(parents=True, exist_ok=True)

    log_path = object_dir / f"{object_name}_{timestamp}.import-log.jsonl"
    _write_import_log(log_path, results)

    _write_manifest(
        object_dir / f"{object_name}_{timestamp}.import-result.json",
        {
            "module": module,
            "object": object_name,
            "env_prefix": env_config.prefix,
            "csv_file": str(resolved_csv),
            "dry_run": not execute,
            "mode": "row",
            "start_row": start_row,
            "row_count": len(results),
            "counts": dict(counts),
            "log_path": str(log_path),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    mode = "EXECUTED" if execute else "DRY RUN (pass --execute to actually write)"
    click.echo(f"{mode}: {resolved_csv} -> {env_config.prefix}")
    for action, count in sorted(counts.items()):
        click.echo(f"  {action}: {count}")
    click.echo(f"log: {log_path}")
    return counts.get("failed", 0), log_path


def _relation_csvs(obj_def: ObjectDef, parent_csv: Path, out: Path, module: str) -> dict[str, Path]:
    """The parent CSV plus each relation's CSV from the *same* export run —
    `export` stamps every file of one run with the same timestamp, so the
    sibling `{out}/{module}/{child}/{child}_{ts}.csv` is the matching one."""
    paths = {obj_def.name: parent_csv}
    stem = parent_csv.stem
    prefix = f"{obj_def.name}_"
    timestamp = stem[len(prefix):] if stem.startswith(prefix) else None

    def walk(node) -> None:
        for child in node.children:
            if timestamp:
                candidate = out / module / child.name / f"{child.name}_{timestamp}.csv"
                if candidate.is_file():
                    paths[child.name] = candidate
            walk(child)

    walk(obj_def)
    return paths


def _import_bulk(
    module: str,
    object_name: str,
    obj_def: ObjectDef,
    env_config: EnvConfig,
    resolved_csv: Path,
    out: Path,
    execute: bool,
    datadict,
    action: str,
    batch_size: int,
    stop_on_error: bool,
    start_row: int,
    tz: str | None,
) -> tuple[int, Path]:
    """MxLoader-engine path (see maxmig.mxl): nested BULK writes with typed values."""
    started_at = datetime.now(timezone.utc)
    timestamp = started_at.strftime(_RUN_TIMESTAMP_FORMAT)
    csv_paths = _relation_csvs(obj_def, resolved_csv, out, module)

    report = check_tree(obj_def, csv_paths)
    if not report.ok:
        click.echo(f"warning: referential-integrity check found problems in {resolved_csv.parent}:", err=True)
        for line in report.summary_lines(limit=10):
            click.echo(f"  {line}", err=True)

    client = MaximoClient(env_config)
    service = MxRestService(client.session, client.env.manage_url, client.headers)
    reader = TreeReader(obj_def, csv_paths, make_resolver(obj_def, datadict), start_row=start_row)
    options = RunOptions(
        execute=execute, batch_size=batch_size, stop_on_error=stop_on_error, timezone=tz, keep_outcomes=False
    )
    object_dir = out / module / object_name
    object_dir.mkdir(parents=True, exist_ok=True)
    log_path = object_dir / f"{object_name}_{timestamp}.import-log.jsonl"

    # One JSONL line per object as it completes — bounded memory on a million-row
    # load, and the log survives a crash mid-run.
    with log_path.open("w", encoding="utf-8") as log:

        def write_log(o) -> None:
            log.write(
                json.dumps(
                    {
                        "rows": o.rows,
                        "key": node_key(o.node, obj_def.key_fields) if o.node else {},
                        "action": o.status,
                        "message": o.message,
                        "ids": o.ids,
                        "children": {rel: len(nodes) for rel, nodes in o.node.children.items()} if o.node else {},
                    }
                )
                + "\n"
            )
            if o.status == "error" and o.rows:
                click.echo(f"  row {o.rows[0]}: {o.message}", err=True)

        try:
            summary = run_nodes(
                batched(iter(reader), batch_size), service, obj_def.access.oslc_resource, normalize_action(action),
                options, base_dir=resolved_csv.parent, on_outcome=write_log,
            )
        except Exception as exc:
            raise click.ClickException(f"import of '{object_name}' failed: {exc}") from exc

    counts = summary.counts
    _write_manifest(
        object_dir / f"{object_name}_{timestamp}.import-result.json",
        {
            "module": module,
            "object": object_name,
            "env_prefix": env_config.prefix,
            "csv_file": str(resolved_csv),
            "child_csv_files": {k: str(v) for k, v in csv_paths.items() if k != object_name},
            "dry_run": not execute,
            "mode": "bulk",
            "action": action,
            "batch_size": batch_size,
            "batches": summary.batches,
            "start_row": start_row,
            "row_count": sum(counts.values()),
            "counts": dict(counts),
            "stopped": summary.stopped,
            "resume_row": summary.resume_row,
            "unmatched_child_rows": reader.unmatched,
            "integrity_ok": report.ok,
            "log_path": str(log_path),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    mode = "EXECUTED" if execute else "DRY RUN (pass --execute to actually write)"
    click.echo(f"{mode} [bulk {action}, batch {batch_size or 'all'}]: {resolved_csv} -> {env_config.prefix}")
    for status, count in sorted(counts.items()):
        click.echo(f"  {status}: {count}")
    for name, path in csv_paths.items():
        if name != object_name:
            click.echo(f"  + nested children from {path}")
    if reader.unmatched:
        click.echo(f"  ! child rows not matched to a parent: {reader.unmatched} (run `migrate check`)", err=True)
    if summary.stopped:
        click.echo(
            f"  ! stopped on error; fix and resume with --start-row {summary.resume_row}", err=True
        )
    click.echo(f"log: {log_path}")
    return summary.errors, log_path


# --------------------------------------------------------------------------- MxLoader engine


@main.group("mxl")
def mxl_group() -> None:
    """MxLoader-compatible sheets: row 1 = object structure / object / action / where,
    row 2 = attribute header (REL.ATTR for children, [ID] to return, [RESULT]), data from row 3.
    Works on .csv files and on .xlsx/.xlsm workbooks, including MxLoader's own."""


def _sheet_or_fail(path: Path, sheet_name: str | None):
    try:
        return load_sheet(path, sheet_name)
    except (SheetFormatError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@mxl_group.command("run")
@click.argument("sheet_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--sheet", "sheet_name", default=None, help="Worksheet name in an .xlsx/.xlsm (default: active sheet)")
@click.option("--env", "env_prefix", required=True, help="Env prefix to run against, e.g. MAXDEMO")
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--execute", is_flag=True, default=False, help="Actually write (load actions). Default: validate payloads only.")
@click.option("--batch-size", type=int, default=50, show_default=True, help="Objects per BULK request (0 = all)")
@click.option("--continue-on-error", is_flag=True, default=False, help="MxLoader 'Stop on error = False'")
@click.option("--start-row", type=int, default=3, show_default=True, help="Sheet row to start from (resume)")
@click.option(
    "--datadict",
    "datadict_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="MxAttrCfg-style data dictionary CSV; repeatable. Defaults to the env's cached one.",
)
@click.option("--timezone", "tz", default=None, help="Offset appended to DATETIME values lacking one, e.g. +00:00")
@click.option("--max-rows", type=int, default=0, help="Query only: stop after N objects (0 = all)")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the updated sheet here instead of back to SHEET_FILE")
@click.option("--script", "script_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Python hook script (before_run/before_record/after_record/after_run) — MxLoader's VBA macros")
def mxl_run_cmd(
    sheet_file: Path,
    sheet_name: str | None,
    env_prefix: str,
    env_file: Path | None,
    execute: bool,
    batch_size: int,
    continue_on_error: bool,
    start_row: int,
    datadict_files: tuple[Path, ...],
    tz: str | None,
    max_rows: int,
    output: Path | None,
    script_file: Path | None = None,
) -> None:
    """Run an MxLoader sheet: Query fills it from Maximo; AddChange/Add/Change/Delete
    load it through OSLC BULK, writing results to [RESULT] and returned IDs to [ID] columns."""
    sheet = _sheet_or_fail(sheet_file, sheet_name)
    env_config = _load_env_config(env_prefix, env_file)
    client = MaximoClient(env_config)
    service = MxRestService(client.session, client.env.manage_url, client.headers)
    options = RunOptions(
        execute=execute, batch_size=batch_size, stop_on_error=not continue_on_error,
        start_row=start_row, timezone=tz, max_rows=max_rows,
    )
    target = output or sheet_file

    if sheet.action == "Query":
        try:
            count = run_query(sheet, service, options)
        except Exception as exc:
            raise click.ClickException(f"query failed: {exc}") from exc
        sheet.save(target)
        click.echo(f"queried {count} {sheet.object_name} object(s) into {len(sheet.data)} row(s) -> {target}")
        return

    datadict = _load_datadict(env_config, datadict_files)
    script = _load_script_or_fail(script_file)
    ctx = ScriptContext(env=env_config.prefix, sheet=sheet, action=sheet.action, dry_run=not execute)
    try:
        summary = run_sheet(sheet, service, options, sheet_resolver(sheet, datadict), script=script, ctx=ctx)
    except (SheetFormatError, ScriptError) as exc:
        raise click.ClickException(str(exc)) from exc
    if execute:
        sheet.save(target)
        click.echo(f"EXECUTED {sheet.action} -> {env_config.prefix}; results written to {target}")
    else:
        click.echo(f"DRY RUN {sheet.action} (pass --execute to actually write): payloads built and type-checked")
    click.echo(
        f"  created: {summary.created}  updated: {summary.updated}  errors: {summary.errors}  "
        f"validated: {summary.validated}  batches: {summary.batches}"
        + (f"  skipped by script: {summary.skipped}" if summary.skipped else "")
    )
    for outcome in summary.outcomes:
        if outcome.status == "error":
            click.echo(f"  row {outcome.rows[0]}: {outcome.message}", err=True)
    if summary.stopped:
        raise click.ClickException(f"stopped on error; fix the sheet and resume with --start-row {summary.resume_row}")
    if summary.errors:
        raise click.ClickException(f"{summary.errors} object(s) failed — see [RESULT] in {target}")


def _load_script_or_fail(path: Path | None):
    if path is None:
        return NO_SCRIPT
    try:
        return load_script(path)
    except ScriptError as exc:
        raise click.ClickException(str(exc)) from exc


def _parse_lookup_options(specs: tuple[str, ...]) -> dict:
    tables = {}
    for spec in specs:
        name, sep, file = spec.partition("=")
        if not sep or not name.strip() or not file.strip():
            raise click.ClickException(f"--lookup expects NAME=file.csv or NAME=book.xlsx:Sheet, got {spec!r}")
        path, worksheet = parse_source_spec(file.strip(), Path.cwd())
        tables[name.strip().upper()] = load_lookup(name.strip(), path, worksheet)
    return tables


@mxl_group.command("fill")
@click.argument("template_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--sheet", "sheet_name", default=None, help="Worksheet of an .xlsx/.xlsm template (default: active)")
@click.option("--source", default=None,
              help="Source table: file.csv or book.xlsx:Sheet. Default: the mapping's source, or the worksheet named in D1")
@click.option("--lookup", "lookup_specs", multiple=True,
              help="VLOOKUP table NAME=file.csv or NAME=book.xlsx:Sheet; repeatable")
@click.option("--script", "script_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Python script with @formula UDFs and/or a transform_row hook")
@click.option("--filter", "filter_formula", default=None, help='Keep only source rows where this formula is TRUE, e.g. \'=[Status]<>"SCRAPPED"\'')
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path),
              help="MxLoader sheet to write (.csv or .xlsx), ready for `migrate mxl run`")
def mxl_fill_cmd(template_file, sheet_name, source, lookup_specs, script_file, filter_formula, output) -> None:
    """MxLoader FillData: compute an MxLoader sheet from a source table.

    TEMPLATE_FILE is either a mapping YAML (header -> formula/constant) or an
    MxLoader sheet whose row 3 holds Excel-style formulas such as
    =CONCATENATE("PUMP", RIGHT([Asset],3)) or =VLOOKUP([Loc], "LOCMAP", 2, FALSE);
    each source row produces one sheet row. No Maximo connection is used.
    """
    try:
        if template_file.suffix.lower() in (".yaml", ".yml"):
            mapping = load_mapping(template_file)
            sheet, template, lookups = mapping.sheet, mapping.template, mapping.lookups
            src_path, src_sheet = mapping.source
            script, row_filter = mapping.script, mapping.row_filter
        else:
            sheet = _sheet_or_fail(template_file, sheet_name)
            template = template_row(sheet)
            if not template:
                raise click.ClickException(f"{template_file}: row 3 must hold the formulas/constants to fill down")
            src_path, src_sheet = None, None
            if sheet.where and template_file.suffix.lower() in (".xlsx", ".xlsm"):
                src_path, src_sheet = template_file, sheet.where  # D1 names the source worksheet, as in MxLoader
            lookups = workbook_lookups(template_file, {sheet.sheet_name or ""})
            script, row_filter = NO_SCRIPT, None
            sheet.where = ""  # D1 was the source sheet, not an OSLC where clause
        if source:
            src_path, src_sheet = parse_source_spec(source, Path.cwd())
        if src_path is None:
            raise click.ClickException("no source table: pass --source (or put the source worksheet name in D1)")
        lookups.update(_parse_lookup_options(lookup_specs))
        if script_file:
            extra = _load_script_or_fail(script_file)
            if script is NO_SCRIPT:
                script = extra
            else:  # --script adds to (and overrides) the mapping's own script
                script.functions.update(extra.functions)
                script.hooks.update(extra.hooks)
        if filter_formula:
            row_filter = Formula.parse(filter_formula)
        header, data = read_table(src_path, src_sheet)
        result = fill(sheet, template, table_rows(header, data), lookups, script, row_filter)
    except (SheetFormatError, FormulaError, ScriptError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    for error in result.errors[:50]:
        click.echo(f"  source row {error.source_row} [{error.column}]: {error.message}", err=True)
    if len(result.errors) > 50:
        click.echo(f"  ... and {len(result.errors) - 50} more", err=True)
    if result.errors:
        raise click.ClickException(f"{len(result.errors)} source row(s) failed; nothing written — fix them and re-run")
    sheet.sheet_name = sheet.sheet_name or sheet.object_name
    result.sheet.save(output)
    click.echo(
        f"filled {len(result.sheet.data)} row(s) from {result.source_rows} source row(s)"
        + (f" ({result.dropped} dropped by filter/script)" if result.dropped else "")
        + f" -> {output}"
    )


@mxl_group.command("batch")
@click.argument("batch_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--sheet", "sheet_name", default=None, help="Batch worksheet (default: the one with RunBatch in A1)")
@click.option("--env", "env_prefix", required=True, help="Env prefix to run against, e.g. MAXDEMO")
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--execute", is_flag=True, default=False, help="Actually write. Default: validate every load sheet only.")
@click.option("--batch-size", type=int, default=50, show_default=True, help="Objects per BULK request (0 = all)")
@click.option("--continue-on-error", is_flag=True, default=False,
              help="Keep going after a failing sheet (and inside each sheet, MxLoader 'Stop on error = False')")
@click.option("--datadict", "datadict_files", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--timezone", "tz", default=None)
@click.option("--script", "script_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Hook script applied to every load sheet in the batch")
def mxl_batch_cmd(batch_file, sheet_name, env_prefix, env_file, execute, batch_size, continue_on_error,
                  datadict_files, tz, script_file) -> None:
    """MxLoader RunBatch: run the sheets listed in a batch sheet, in order.

    Row 1 is RunBatch, row 2 the header (Sheet, Action, Status, Query, Sync,
    Error, Execution Date, Execution Duration), one sheet per row after that.
    Status columns are written back to BATCH_FILE; each sheet's [RESULT]/[ID]
    columns are written back to the sheet (only with --execute or Query).
    """
    try:
        batch = load_batch(batch_file, sheet_name)
    except SheetFormatError as exc:
        raise click.ClickException(str(exc)) from exc
    env_config = _load_env_config(env_prefix, env_file)
    client = MaximoClient(env_config)
    service = MxRestService(client.session, client.env.manage_url, client.headers)
    datadict = _load_datadict(env_config, datadict_files)
    script = _load_script_or_fail(script_file)
    options = RunOptions(execute=execute, batch_size=batch_size, stop_on_error=not continue_on_error, timezone=tz)

    def run_one(sheet, action) -> EntryReport:
        if action == "Query":
            return EntryReport(query=run_query(sheet, service, options))
        ctx = ScriptContext(env=env_config.prefix, sheet=sheet, action=action, dry_run=not execute)
        summary = run_sheet(sheet, service, options, sheet_resolver(sheet, datadict), script=script, ctx=ctx)
        ok, error = entry_counts_from_summary(summary)
        return EntryReport(sync=ok, error=error, stop=bool(summary.errors))

    def save(sheet) -> None:
        if execute or sheet.action == "Query":
            sheet.save()

    summary = run_batch(batch, run_one, continue_on_error=continue_on_error, save=save)
    batch.save()
    for entry in batch.entries:
        line = f"  {entry.sheet:30} {entry.action or '(sheet)':10} {entry.status:12} q={entry.query or '-'} s={entry.sync or '-'} {entry.duration}"
        click.echo(line + (f"  {entry.error}" if entry.error else ""), err=bool(entry.error))
    click.echo(
        f"{'EXECUTED' if execute else 'DRY RUN'} batch: {summary.ran} ran, {summary.failed} failed, "
        f"{summary.skipped} skipped -> status written to {batch_file}"
    )
    if summary.stopped_at is not None:
        raise click.ClickException(f"batch stopped at row {summary.stopped_at}; later sheets were not run")
    if summary.failed:
        raise click.ClickException(f"{summary.failed} sheet(s) failed")


@mxl_group.command("template")
@click.option("--module", required=True)
@click.option("--object", "object_name", required=True)
@click.option("--action", type=click.Choice(("Query",) + _REST_LOAD_ACTIONS, case_sensitive=False),
              default="AddChange", show_default=True)
@click.option("--extra-field", "extra_fields", multiple=True, help="Custom attribute column to add (attr or relation.attr)")
@click.option("--config-dir", "config_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path), help=".csv or .xlsx path")
def mxl_template_cmd(module, object_name, action, extra_fields, config_dirs, output) -> None:
    """Write an empty MxLoader sheet for a module object (custom fields included)."""
    registry = _load_registry(config_dirs)
    obj_def = _customize(_lookup_object(registry, module, object_name), extra_fields, False)
    try:
        sheet = template_sheet(obj_def, action, output)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    sheet.save(output)
    click.echo(f"wrote {len(sheet.columns)}-column {sheet.action} template for {sheet.object_service} -> {output}")


@mxl_group.command("from-export")
@click.option("--module", required=True)
@click.option("--object", "object_name", required=True)
@click.option("--csv-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Parent CSV; defaults to the newest export. Child CSVs from the same run are joined in.")
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=Path("exports"))
@click.option("--action", type=click.Choice(_REST_LOAD_ACTIONS, case_sensitive=False),
              default="AddChange", show_default=True)
@click.option("--config-dir", "config_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path), help=".csv or .xlsx path")
def mxl_from_export_cmd(module, object_name, csv_file, out, action, config_dirs, output) -> None:
    """Join an export's parent + child CSVs into one MxLoader sheet (one row per leaf child)."""
    registry = _load_registry(config_dirs)
    obj_def = _lookup_object(registry, module, object_name)
    parent_csv = _resolve_csv_file(csv_file, out, module, object_name)
    try:
        sheet = export_tree_to_sheet(obj_def, _relation_csvs(obj_def, parent_csv, out, module), action, output)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    sheet.save(output)
    click.echo(f"wrote {len(sheet.data)} row(s), {len(sheet.columns)} column(s) -> {output}")


@main.group("datadict")
def datadict_group() -> None:
    """Maximo data dictionary (MxLoader's MxAttrCfg): attribute types used to send
    numbers, booleans and dates correctly — custom attributes included."""


@datadict_group.command("refresh")
@click.option("--env", "env_prefix", required=True)
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--resource", default=DEFAULT_DICTIONARY_OS, show_default=True,
              help="Object structure exposing MAXATTRIBUTE (MxLoader's own is MXL_MAXATTRIBUTECFG)")
@click.option("--module", "modules", multiple=True, help="Only objects used by these modules (default: all attributes)")
@click.option("--config-dir", "config_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Default: config/datadict/<env>.csv, picked up automatically by import --bulk and mxl run")
def datadict_refresh_cmd(env_prefix, env_file, resource, modules, config_dirs, output) -> None:
    """Pull MAXATTRIBUTE types (and USERDEFINED flags) from Maximo into a dictionary CSV."""
    env_config = _load_env_config(env_prefix, env_file)
    objects: set[str] = set()
    if modules:
        registry = _load_registry(config_dirs)
        for module in modules:
            try:
                module_def = registry.get_module(module)
            except KeyError as exc:
                raise click.ClickException(str(exc.args[0])) from exc
            for obj in module_def.objects:
                objects.update(path_info.maximo_object for path_info in path_index(obj).values())
    client = MaximoClient(env_config)
    try:
        datadict = fetch_from_maximo(client.session, client.env.manage_url, client.headers, objects or None, resource)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if resource != DEFAULT_DICTIONARY_OS or status != 404:
            raise click.ClickException(f"could not read MAXATTRIBUTE through {resource}: {exc}") from exc
        click.echo(f"{resource} not published here; falling back to out-of-the-box {OOB_DICTIONARY_OS}")
        try:
            datadict = fetch_from_maximo(
                client.session, client.env.manage_url, client.headers, objects or None, OOB_DICTIONARY_OS
            )
        except Exception as exc2:
            raise click.ClickException(
                f"could not read MAXATTRIBUTE through {resource} or {OOB_DICTIONARY_OS}: {exc2}. "
                "Pass --resource naming an object structure that exposes MAXATTRIBUTE"
            ) from exc2
    target = output or _default_datadict_path(env_config)
    count = datadict.to_csv(target)
    custom = sum(1 for t in datadict.entries.values() if t.user_defined)
    click.echo(f"wrote {count} attribute types ({custom} user-defined) -> {target}")


# --------------------------------------------------------------------------- discovery / inspection


@main.command("discover")
@click.option("--object", "object_name", required=True, help="Object structure / table name to probe, e.g. asset or MXL_FOO")
@click.option("--env", "env_prefix", required=True)
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--module", "module_name", default=None, help="Module name for the draft (default: discovered_<object>)")
@click.option("--datadict", "datadict_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Dictionary CSV whose USERDEFINED flags split custom attributes into custom_fields")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), default=Path("config/custom_objects"),
              show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
def discover_cmd(object_name, env_prefix, env_file, module_name, datadict_file, out_dir, overwrite) -> None:
    """Probe OSLC mxapi<object>, OSLC <object>, then REST MBO, and draft a module YAML
    (<object>.discovered.yaml — not loaded until you review and rename it)."""
    env_config = _load_env_config(env_prefix, env_file)
    client = MaximoClient(env_config)
    datadict = _load_datadict(env_config, (datadict_file,) if datadict_file else ())
    custom: set[str] = set()
    for (_, attr), info in datadict.entries.items():
        if info.user_defined:
            custom.add(attr.lower())
    result = probe_object(client.session, client.env.manage_url, client.headers, object_name, custom_attributes=custom)
    for attempt in result.attempts:
        click.echo(f"  {attempt.api:8} {attempt.resource:30} {attempt.status}{f' ({attempt.message})' if attempt.message else ''}")
    if not result.found:
        raise click.ClickException(f"no OSLC or REST MBO endpoint answered for '{object_name}'")
    text = draft_module_yaml(result, module_name or f"discovered_{object_name.lower()}", object_name.lower())
    try:
        path = write_draft(out_dir, object_name.lower(), text, overwrite=overwrite)
    except FileExistsError as exc:
        raise click.ClickException(f"{exc}; pass --overwrite to replace it") from exc
    click.echo(
        f"drafted {len(result.attributes)} attribute(s), {len(result.custom_attributes)} custom, "
        f"{len(result.children)} relation(s) -> {path}"
    )


@main.command("list-modules")
@click.option("--config-dir", "config_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def list_modules_cmd(config_dirs) -> None:
    """List configured modules."""
    for info in describe_modules(_load_registry(config_dirs)):
        click.echo(f"{info['module']:20} {info['object_count']:3} object(s)  {info.get('description') or ''}")


@main.command("list-objects")
@click.option("--module", required=True)
@click.option("--config-dir", "config_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def list_objects_cmd(module, config_dirs) -> None:
    """List a module's objects, their endpoints, keys, custom fields and child relations."""
    registry = _load_registry(config_dirs)
    try:
        rows = describe_objects(registry, module)
    except KeyError as exc:
        raise click.ClickException(str(exc.args[0])) from exc
    for info in rows:
        click.echo(f"{info['name']}  ({info.get('maximo_object') or '?'})  oslc={info.get('oslc_resource')}  prefer={info.get('prefer')}")
        click.echo(f"    key: {', '.join(info['key_fields'])}   fields: {info['field_count']}")
        if info.get("custom_fields"):
            click.echo(f"    custom: {', '.join(info['custom_fields'])}")
        if info.get("children"):
            click.echo(f"    children: {', '.join(info['children'])}")


@main.command("status")
@click.option("--module", default=None)
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=Path("exports"))
def status_cmd(module, out) -> None:
    """Show the newest export and import run per object under --out."""
    runs = latest_runs(out, module)
    if not runs:
        click.echo(f"no runs found under {out}")
        return
    for run in runs:
        detail = f"{run.row_count} row(s)" if run.row_count is not None else ""
        if run.kind == "import":
            detail += f" {'DRY RUN' if run.dry_run else 'EXECUTED'} {dict(run.counts)}"
        click.echo(f"{run.module}/{run.object:24} {run.kind:6} {run.env_prefix or '?':12} {run.finished_at or ''}  {detail}")


@main.command("check")
@click.option("--module", required=True)
@click.option("--object", "object_name", required=True)
@click.option("--csv-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=Path("exports"))
@click.option("--config-dir", "config_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def check_cmd(module, object_name, csv_file, out, config_dirs) -> None:
    """Referential-integrity check of an export: orphan child rows, duplicate and blank keys."""
    registry = _load_registry(config_dirs)
    obj_def = _lookup_object(registry, module, object_name)
    parent_csv = _resolve_csv_file(csv_file, out, module, object_name)
    report = check_tree(obj_def, _relation_csvs(obj_def, parent_csv, out, module))
    for line in report.summary_lines():
        click.echo(line)
    if not report.ok:
        raise click.ClickException("integrity check failed")
