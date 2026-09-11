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
from datetime import datetime, timezone
from pathlib import Path

import click

from maxmig.api.client import MaximoClient
from maxmig.api.filter import Filter
from maxmig.api.oslc import SkippedRow
from maxmig.config import EnvConfig, load_env
from maxmig.extract.writer import ObjectExportResult, write_object_tree
from maxmig.load.importer import ImportRowResult, import_row
from maxmig.load.reader import read_csv_rows
from maxmig.model.registry import ModuleLoadError, ObjectRegistry
from maxmig.model.schema import ObjectDef

_RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


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


def _resolve_object_names(registry: ObjectRegistry, module: str, object_name: str | None) -> list[str]:
    """A single object if `--object` was given; otherwise every object in the
    module's `extract_order` (declared object order, if `extract_order` is
    empty — e.g. a hand-written custom-object module that never set it)."""
    if object_name is not None:
        return [object_name]
    try:
        module_def = registry.get_module(module)
    except KeyError as exc:
        raise click.ClickException(str(exc.args[0])) from exc
    return module_def.extract_order or module_def.object_names()


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
) -> ObjectExportResult:
    """Fetch and write one object structure (its own CSV plus one per child
    relation), its manifest, and its skip log if anything was skipped. Prints
    progress as it goes. Returns the top-level object's result, for a
    whole-module run's final summary line."""
    if flat:
        obj_def = obj_def.model_copy(update={"children": []})

    started_at = datetime.now(timezone.utc)
    skipped: list[SkippedRow] = []

    try:
        client = MaximoClient(env_config)
        rows = client.fetch_object(
            obj_def, filter=row_filter, skip_errors=skip_errors, on_skip=skipped.append
        )
        results = write_object_tree(rows, obj_def, out, module, timestamp)
    except Exception as exc:
        raise click.ClickException(f"export of '{object_name}' failed: {exc}") from exc

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
            "skipped_count": len(skipped),
            "skipped_log": str(skipped_log_path) if skipped_log_path else None,
        },
    )
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

    timestamp = datetime.now(timezone.utc).strftime(_RUN_TIMESTAMP_FORMAT)
    results = []
    for name in object_names:
        obj_def = _lookup_object(registry, module, name)
        results.append(
            _export_one(module, name, obj_def, env_config, row_filter, out, timestamp, flat, skip_errors)
        )

    if len(results) > 1:
        total_rows = sum(result.row_count for result in results)
        click.echo(f"module '{module}': exported {len(results)} objects, {total_rows} total rows")


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


@main.command("import")
@click.option("--module", required=True, help="Module name, e.g. inventory")
@click.option("--object", "object_name", required=True, help="Object name within the module, e.g. item")
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
    help="Explicit CSV file to import. Defaults to the newest export for this module/object under --out.",
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
        "reports what it WOULD do (would_create/would_update per row) -- nothing is written."
    ),
)
def import_cmd(
    module: str,
    object_name: str,
    env_prefix: str,
    env_file: Path | None,
    config_dirs: tuple[Path, ...],
    csv_file: Path | None,
    out: Path,
    execute: bool,
) -> None:
    """Create-or-update one object's records in Maximo from a CSV, matched by
    the object's declared natural key (its `key_fields`). Dry-run by default —
    pass --execute to actually write. OSLC only (the object must have an
    oslc_resource configured)."""
    env_config = _load_env_config(env_prefix, env_file)
    registry = _load_registry(config_dirs)
    obj_def = _lookup_object(registry, module, object_name)
    if not obj_def.access.oslc_resource:
        raise click.ClickException(
            f"'{object_name}' has no oslc_resource configured; migrate import only supports OSLC objects"
        )
    resolved_csv = _resolve_csv_file(csv_file, out, module, object_name)

    started_at = datetime.now(timezone.utc)
    timestamp = started_at.strftime(_RUN_TIMESTAMP_FORMAT)
    client = MaximoClient(env_config)

    results: list[ImportRowResult] = []
    try:
        for row in read_csv_rows(resolved_csv):
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

    if counts.get("failed"):
        raise click.ClickException(f"{counts['failed']} row(s) failed — see {log_path}")
