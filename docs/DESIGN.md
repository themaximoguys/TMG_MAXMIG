# IBM Maximo Data Migration CLI — Implementation Plan

> **Status: original design document, not a description of the shipped tool.**
> This is the plan the build started from. Parts of it shipped as written; parts
> diverged or were deferred. Where this document and the [README](../README.md)
> disagree, the README describes what actually exists.
>
> Known divergences as of 2026-09-11:
> - **Not built:** `discover`, `list-modules`, `list-objects`, `status`, `report`
>   commands; checkpoint/resume; concurrency/thread pool; OSLC BULK batching.
> - **Export layout** shipped as `exports/{module}/{object_name}/{object}_{ts}.csv`,
>   not the `exports/{run_id}/{module}/{object}.csv` described below.
> - **Dry-run inverted:** import is dry-run *by default*; `--execute` performs writes.
>   There is no `--dry-run` flag.
> - **Module layout** is flatter than the tree below — no `pipeline.py`,
>   `paginator.py`, `idempotency.py`, or `logging_setup.py`.
>
> Update 2026-10-19: export checkpoint/resume shipped as an opt-in
> `migrate export --checkpoint` / `--resume` (no run-id argument). The checkpoint
> lives next to the object's CSV as `{object}_{ts}.checkpoint.json`, not under
> `state/`; it records byte offsets per partial file (truncated back to on
> resume) rather than row counts, and pages by explicit OSLC `pageno` rather
> than `nextPage` links. See `src/maxmig/extract/checkpoint.py`. The MxLoader
> engine (`src/maxmig/mxl/`) now also covers FillData formulas, RunBatch, and
> Python hook scripts as the equivalent of MxLoader's VBA macros.

## Context

Maximo teams need a repeatable, API-based way to move data between IBM Maximo environments — either as part of a Maximo 7.6.1.2 → IBM MAS 9 Manage upgrade, or for MAS9-to-MAS9 environment moves (dev→test, client cutovers, etc.). Today this kind of migration has no tooling: everything found on the system (the `MaximoData` skill, credential conventions) supports ad hoc small-scale pulls, but there is no CSV-based bulk extract/load pipeline, no relationship-aware export, no support for custom (non-OOB) object structures, and no checkpoint/resume for large tables.

The pilot targets **Inventory Management and Purchasing** (item master, inventory balances, storerooms, companies, POs/PO lines, PRs/PR lines, receipts) because those are the modules where the largest tables live (item master, inventory balances can approach ~1M rows) and where getting the export/import mechanics right first proves the design before it's rolled out to the rest of the standard Maximo data model (assets, work orders, service requests, contracts, etc.).

Working directory `TMG_MAXMIG` is currently empty — this is a from-scratch build (confirmed via exploration, no existing migration code anywhere on the system).

**Decisions already confirmed with the user:**
- One generic tool, not two separate codepaths — source and target are just two environment configs. Same tool handles both the 7.6.1.2→MAS9 upgrade and any MAS9→MAS9 move.
- Python.
- Rollout after the pilot = all remaining standard Maximo modules (not a separate internal app).
- Standalone CLI, run manually per migration event/environment pair (no scheduler for v1).

## Approach

### Grounding (reuse, don't reinvent)

Field-verified in `~/.claude/skills/MaximoData/ApiEndpoints.md`, `Workflows/Pull.md`/`Sync.md`, and this org's real `.env.*` files under `/path/to/credentials/`:

- **Auth**: header `maxauth: base64(username:password)`.
- **REST MBO** (flat bulk reads): `GET {url}/rest/mbo/{table}?_format=json&_maxItems=N&_rsStart=M&{FIELD}={VALUE}` → `{TABLE}MboSet.{TABLE}[].Attributes.{FIELD}.content`. No auto-pagination; default page 40 if `_maxItems` unset — always set explicitly.
- **OSLC** (nested child data, custom objects, writes): `GET {url}/oslc/os/{resource}?lean=1&oslc.pageSize=N&oslc.where={f}="{v}"&oslc.select=*,child{*}` → `member[]` + `responseInfo.nextPage`. Create=POST; Update/Delete=POST + `x-method-override: PATCH`/`DELETE`. Resource naming convention `mxapi{object}`, with known exceptions (e.g. `mxapipersongroup`, not `mxpersongroup`) — needs a discovery/fallback probe for unknown/custom objects.
- **Credential convention**: `{PREFIX}_MANAGE_URL` / `{PREFIX}_MANAGE_USERNAME` / `{PREFIX}_MANAGE_PASSWORD` per environment (verified against real files like `credentials/.env.maxdemo.example`). The tool reuses this scheme directly — `.env` files stay the runtime source of truth; Vaultwarden is never called at runtime (per `VaultCredentials/SKILL.md`).

### Repo structure

```
TMG_MAXMIG/
├── pyproject.toml               # package "maxmig", console_script "migrate"
├── src/maxmig/
│   ├── cli.py                   # click-based CLI
│   ├── config.py                # .env loader → EnvConfig
│   ├── api/{client,rest_mbo,oslc,discover}.py
│   ├── model/{schema,registry}.py   # YAML → ObjectDef dataclasses
│   ├── extract/{paginator,writer,checkpoint,pipeline}.py
│   ├── load/{reader,idempotency,checkpoint,dryrun,pipeline}.py
│   ├── retry.py
│   └── logging_setup.py
├── config/modules/{inventory,purchasing,_template}.yaml
├── config/custom_objects/       # user-declared non-standard object structures
├── env/.env.example             # documents {PREFIX}_MANAGE_* scheme
├── exports/  state/  logs/       # gitignored run outputs, checkpoints, logs
├── tests/{unit,fixtures,integration}/
└── docs/{architecture,module-authoring,runbook}.md
```

### Object-definition layer (the part that makes this generalize)

One YAML file per module under `config/modules/`; each defines object structures with **no hardcoded logic per module** — the CLI only knows how to read this schema, so adding a future module (assets, work orders) is a config-only change.

```yaml
module: purchasing
objects:
  - name: purchase_order
    maximo_object: PO
    access: {oslc_resource: mxapipo, rest_mbo_table: po, prefer: oslc}   # oslc needed for child data
    key_fields: [ponum, siteid]              # natural key, used for idempotent lookup on load
    fields: [ponum, siteid, description, status, vendor, orderdate, totalcost]
    children:
      - name: po_line
        relation_attribute: poline           # OSLC child relation, oslc.select=*,poline{*}
        foreign_key: ponum                   # column injected into child CSV
        key_fields: [ponum, siteid, polinenum]
        fields: [polinenum, itemnum, description, orderqty, unitcost]
  - name: company
    access: {oslc_resource: mxapicompany, rest_mbo_table: company, prefer: oslc}
    key_fields: [company]
    fields: [company, name, type, status, address1, city, country]
    children: []
extract_order: [company, purchase_order, purchase_requisition, receipt]
load_order:    [company, purchase_order, purchase_requisition, receipt]
```

Pilot object list:

| Module | Objects (Maximo object) | Children |
|---|---|---|
| Inventory | item (ITEM), storeroom (LOCATIONS), inventory_balance (INVENTORY) | item_spec, item_structure; invbalances |
| Purchasing | company (COMPANY), purchase_order (PO), purchase_requisition (PR), receipt (MATRECTRANS) | po_line; pr_line |

`access.prefer` picks REST MBO for flat high-volume tables (e.g. `inventory_balance`, `receipt`) and OSLC where child relations or write support are needed (e.g. `purchase_order`, `item`). Registry (`model/registry.py`) globs `config/modules/*.yaml` + `config/custom_objects/*.yaml` at startup and validates with pydantic.

### Extract pipeline

- `MaximoClient` wraps `requests.Session` (connection pooling matters at 1M-row scale) with the auth header pre-set. `fetch_object()` picks REST MBO or OSLC per `access.prefer`, and **falls back to the other API on a non-2xx** before raising (covers the known REST-MBO-500 / OSLC-404 naming gotchas).
- **Pagination**: page size 250 (`--page-size` to tune), REST MBO via `_rsStart`/`_maxItems`, OSLC via `responseInfo.nextPage`. Everything is a generator — CSV writer consumes and streams row-by-row (never buffers the full dataset), which is what makes 1M-row tables safe.
- **CSV representation of parent/child: one flat CSV per object, with FK column** — not nested JSON-in-cell. This keeps every file human-inspectable/diffable/editable (an explicit requirement) and keeps streaming possible (a nested shape would force buffering a whole parent+children graph before writing a row). `purchasing/purchase_order.csv` and `purchasing/po_line.csv` are separate files; every `po_line.csv` row carries `ponum,siteid` as FK columns.
- **CSV conventions**: UTF-8 no BOM, RFC 4180 (`csv.QUOTE_MINIMAL`), header row with `key_fields` → `foreign_key` → remaining `fields`, nulls normalized to empty string, no reformatting of dates/numbers (round-trips the raw API string byte-identical).
- **File layout**: `exports/{run_id}/{module}/{object_name}.csv`, `run_id = {module}_{env_prefix}_{timestamp}`, plus a `manifest.json` (row counts, API mode used per object, timestamps — modeled on the existing `Sync.md` manifest pattern).
- **Checkpoint/resume**: `state/{run_id}.extract.checkpoint.json` written after every page (per-object `rsStart`/`nextPage`/`rows_written`, fsync'd). `migrate export --resume {run_id}` reopens CSVs in append mode, truncates back to the checkpointed row count first (so a crash mid-page can't leave duplicate rows), then continues from the last durable page.

### Load pipeline

> **Addendum (2026-09-09, researched mid-Phase-1-build):** the user asked us to model the load pipeline on "MxLoader" as the standout Maximo bulk-push tool. Verified via WebSearch against IBM's own docs: MxLoader is an unsupported third-party Excel add-in that itself rides on the standard Maximo Integration Framework — not a distinct fast architecture, and its rumored DataSplice/InterOptics lineage doesn't check out. The real finding: Maximo's REST/OSLC API has a **native BULK mode** (`x-method-override: BULK`, JSON array body, per-record `_action`, optional `allornothing`) — see https://www.ibm.com/docs/en/masv-and-l/maximo-manage/cd?topic=postpatchdelete-bulk-operations. **The load pipeline (Phase 6-7) should batch 50-500 records per POST via this BULK mode** for the large flat tables (item master, inventory balances) instead of one-record-per-call, keeping natural-key idempotency and per-record `_responsemeta` error visibility while cutting HTTP round-trips by 1-2 orders of magnitude. Maximo's other OOB bulk path (Interface Tables + `IFACETABLECONSUMER`/`FLATFILECONSUMER` cron tasks, true bulk-SQL throughput but async/file-based error correction) is a real alternative but was not chosen as primary, since it can't preserve this tool's inline dry-run diffing. This changes the retry/concurrency design below: batch requests should still go through the same `call_with_retry` policy, but idempotency lookups and dry-run diffing now happen per-record *within* a batch response rather than via one lookup call per record.

- Follows `load_order` from the module YAML — parents before children.
- **Relationship reconstruction**: for a parent+children object, the loader re-nests the flat CSVs back into a single OSLC create/update payload per parent row (e.g. `{ponum, ..., poline: [{...}]}`) — Maximo's own OSLC create assigns internal IDs; the CSV's natural keys (`ponum`, `siteid`) are portable across environments, so **no cross-environment ID translation is needed**, only FK-based re-nesting. Child CSVs are pre-sorted by FK so parent/child streams are consumed in lockstep (bounded memory).
- **Idempotency**: before create, look up the target by `key_fields` (e.g. `oslc.where=ponum="1234" and siteid="BEDFORD"`). Found → field-diff and PATCH only changed fields; not found → create. This is what makes a re-run after a partial failure safe — no duplicates.
- **Dry-run**: `--dry-run` does the full lookup+diff but skips the write, producing `dryrun_report_{object}.csv` (`key`, `action` CREATE/UPDATE/NOOP/ERROR, `changed_fields`) — an auditable pre-cutover report.
- **Retry/backoff**: retryable = 429/500/502/503/504 + connection/timeout errors; exponential backoff (base 2s, ×2, max 5 attempts, 60s cap, ±20% jitter). Non-retryable (400/401/403/404/409/422) fail the row immediately and are logged.
- **Concurrency**: bounded `ThreadPoolExecutor`, default 4 workers (`--concurrency`), since Maximo calls are I/O-bound.
- **Per-row audit trail**: `load_report_{object}.csv` streamed as the run proceeds (SUCCESS/RETRIED_SUCCESS/FAILED + error body on failure).
- **Checkpoint/resume**: `state/{run_id}.load.checkpoint.json` every 100 rows (`--checkpoint-interval`); resuming a partial load is safe because of the idempotency check even if a row is reprocessed.

### Custom / non-standard object structures

- Declared in `config/custom_objects/*.yaml` using the **same schema** as a standard module object — no separate code path.
- `migrate discover --object {name}` runs the fallback probe: try OSLC `mxapi{name}`, try OSLC with the raw name (covers naming exceptions), try REST MBO `{name}`; classify 200/404/500 and write a **draft** YAML (`{name}.discovered.yaml`, not auto-activated) with a proposed field list from the sample response, for the user to review before it's wired in. The same probe is reused automatically inside the extract pipeline's runtime fallback.

### CLI surface (click)

```
migrate list-modules
migrate list-objects   --module inventory
migrate discover       --object <name> [--module <name>] [--env <prefix>]
migrate export --module purchasing --env masdev [--object x] [--out ./exports/] [--page-size 250] [--resume <run-id>] [--where "..."]
migrate import --module purchasing --from ./exports/<run-id>/ --env mastest [--object x] [--dry-run] [--concurrency 4] [--resume <run-id>]
migrate status --run-id <run-id>
migrate report --run-id <run-id> [--object x]
```

`--env <prefix>` resolves to `.env.<prefix>` (repo `env/` dir first, `--env-file` override, or directly pointing at `/path/to/credentials/.env.<name>`). `export` reads one `--env` (source); `import` reads one `--env` (target) — same CSV directory, different env, which is the literal embodiment of "one generic tool."

### Testing/verification strategy

- **Unit-testable with mocked API responses** (no live Maximo): pagination termination, REST-MBO↔OSLC fallback, CSV writer (header order, null handling, quoting), FK population, checkpoint write/read/resume-truncation, idempotency decision logic, retry backoff timing (fake clock), dry-run report generation, discovery probe classification. Use `responses`/`requests-mock` against canned fixtures in `tests/fixtures/`.
- **Needs a real dev/test environment**: end-to-end export against a real environment verifying row counts match the API's `rsTotal`/`totalCount`; a spot-check script that re-fetches N random records and diffs against CSV; a referential-integrity check (every child FK exists among parent keys — this part is a pure CSV-only unit test, run automatically after every export and before every import); a full round-trip (export from env A → dry-run import to env B → real import → re-export B → diff counts/fields against A).

### Phased build plan

1. Env loader + API client (auth/retry) + YAML→dataclass registry — unit tests only, no network.
2. Single-page fetch for one simple object (`company`) — mocked + one manual smoke test against real dev.
3. Pagination/chunking for both APIs — mocked multi-page tests.
4. CSV writer + `migrate export` end-to-end for `company` — manual run against real dev, row count check.
5. Checkpoint/resume + parent/child flattening for `purchase_order`/`po_line` — simulated-crash resume test, referential-integrity check.
6. `migrate import`: reader, idempotency, dry-run, retry — unit tests + manual dry-run against real target.
7. Load checkpoint/resume, concurrency, FK re-nesting for OSLC payloads — full real dev→test dry-run then real import, verify created records.
8. `migrate discover` + custom-object YAML support — mocked probe branches + one real obscure-object discovery.
9. Full pilot: all 7 Inventory+Purchasing objects wired, bulk test on `inventory_balance` at meaningful scale (aim for a slice approaching the 1M-row profile) to validate memory/performance.
10. Docs (`module-authoring.md`, `runbook.md`) + test hardening + CLI polish (`list-modules`/`status`/`report`).

Phases 1–5 are read-only by design — all extract-side risk is retired before any create/update code (real data-mutation risk) is written. Phase 9 is the actual proof that the design generalizes across two modules at once; Phase 10 is what makes the *next* module (post-pilot rollout to the rest of the standard Maximo modules) a config-only change.

### Critical files

- `src/maxmig/model/schema.py` — ObjectDef/FieldDef/RelationDef dataclasses; the contract everything else depends on.
- `config/modules/purchasing.yaml`, `config/modules/inventory.yaml` — concrete pilot metadata, proves the schema before module-specific code exists.
- `src/maxmig/api/client.py` — auth, retry/backoff, REST-MBO/OSLC dual-path fallback used by both extract and load.
- `src/maxmig/extract/pipeline.py`, `src/maxmig/load/pipeline.py` — orchestration: pagination + CSV I/O + checkpointing + idempotency.
- `env/.env.example` — anchors the credential convention reused from `/path/to/credentials/.env.*`.

## Verification

- Run the unit suite (`pytest tests/unit`) after each phase — all mocked, no live Maximo dependency, should stay green throughout.
- At Phase 4 and again at Phase 9, run `migrate export` against a real non-prod Maximo/MAS environment and confirm `manifest.json` row counts match the API's own reported totals.
- At Phase 7 and Phase 9, run a full round trip (export from one real dev/test environment → `import --dry-run` into a second → review the dry-run report → real `import` → re-export the target → diff record counts and spot-checked field values against the source) to prove the create/update payloads Maximo actually accepts, since business-rule validation on write can't be fully mocked.
- Confirm referential integrity (every child FK value exists among its parent's exported keys) automatically after every export and before every import — pure CSV check, no live system needed.
- Confirm resumability: kill an export/import mid-run partway through a large object, resume, and verify no duplicate/missing rows in the final CSV or target system.
