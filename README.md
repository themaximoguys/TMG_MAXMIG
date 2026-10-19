# MaxMig — Data Migration and Dataload Tool

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-A31F34)](LICENSE)
[![Tests: 371 passing](https://img.shields.io/badge/tests-371%20passing-0A9396?logo=pytest&logoColor=white)](tests/unit)
[![Status: pilot](https://img.shields.io/badge/status-pilot-CA6702)](#roadmap)

An API-based data migration CLI for IBM Maximo / IBM Maximo Application Suite (MAS). One tool, driven entirely by which environment config you point it at — the same commands work whether you're migrating Maximo 7.6.1.2 → MAS 9, or moving data between two MAS environments (dev → test, client cutover, etc.).

Pilot scope: **Inventory Management** and **Purchasing** modules. The design is config-driven (YAML), so adding the rest of the standard Maximo modules later is a config change, not a code change.

![MaxMig architecture: a YAML config layer drives both an export path (source Maximo/MAS environment through cli.py, api/client.py and extract/writer.py out to timestamped CSV and manifest files on disk) and an import path (CSV back through load/reader.py and load/importer.py, then out through the same api/client.py to a target environment). Source and target are the same kind of environment reached by the same code — only the .env file selected by --env differs.](docs/images/architecture.svg)

**Current status:** `export` works end-to-end against live environments. `import` works in two modes, both dry-run by default: per-row create-or-update by natural key, and `--bulk`, which uses the MxLoader engine (child relations re-nested, OSLC BULK batches, typed values). A MxLoader-compatible sheet runner (`migrate mxl`) with FillData formulas, RunBatch, and Python hook scripts in place of MxLoader's VBA macros, custom-attribute support, checkpoint/resume for large exports, `discover`, `check`, `status`, and `list-*` are built too. All write paths are unit-tested against mocked HTTP; **none has yet been exercised with a live `--execute` run**, so treat your first real `--execute` on any object as the validation. See [Roadmap](#roadmap) for what's still open.

## Documentation

| Doc | What it covers |
|---|---|
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Step-by-step end-user process for running an export, start to finish |
| [`docs/export-commands.md`](docs/export-commands.md) | Flag/object/filter reference, plus object → Maximo table mapping |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Original implementation plan, and where the shipped tool diverged from it |

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

This installs `requests`, `PyYAML`, `pydantic`, `click`, plus `pytest`/`requests-mock` for the test suite.

Run the tests:

```bash
./.venv/bin/python -m pytest tests/unit -v
```

## Credentials

MaxMig reads credentials from a `.env`-style file, never from Vaultwarden or any other secret store at runtime. Copy the template and fill in real values:

```bash
cp env/.env.example env/.env.<prefix>
```

`<prefix>` is an uppercase environment name (e.g. `MAXDEMO`) passed to `--env`. Two auth modes are supported, both keyed off that same prefix:

**API key** (required for SSO/OIDC-fronted instances that reject a bare basic-auth header — generate one by hand via Manage's **Security → API Keys** app, or the MAS Suite console's API keys page; there's no headless way to create one):
```
MAXDEMO_MANAGE_URL=https://maxdemo.manage.example.com/maximo/
MAXDEMO_MANAGE_APIKEY=<the generated key>
```

If both `_APIKEY` and `_USERNAME`/`_PASSWORD` are set, the API key wins. You can also point `--env-file` at an existing credentials file elsewhere (e.g. one already maintained under `/path/to/credentials/`) instead of duplicating it under `env/`.

`.env.*` files are gitignored — only `env/.env.example` is meant to be committed.

## Configuring what to migrate

Each Maximo module is one YAML file under `config/modules/`. `config/modules/_template.yaml` is a documented skeleton for adding a new module; `config/modules/inventory.yaml` and `config/modules/purchasing.yaml` are the pilot definitions.

```bash
# See what's configured
grep -A1 '^  - name:' config/modules/purchasing.yaml
```

Objects currently defined:

| Module | Objects |
|---|---|
| `inventory` | `item` (+ `item_spec`, `item_structure`), `storeroom`, `inventory_balance` (+ `invbalances`, `invcost`) |
| `purchasing` | `company` (vendors), `purchase_requisition` (+ `pr_line`), `rfq` (+ `rfq_line`), `purchase_order` (+ `po_line`), `receipt` |

Adding a new object or module means editing/adding YAML here — no Python changes required, as long as it's a standard REST MBO/OSLC object structure. Custom object structures can go in `config/custom_objects/` using the same schema.

## Exporting data

```bash
./.venv/bin/migrate export \
  --module purchasing \
  --object company \
  --env MAXDEMO \
  --out exports/
```

This writes:

```
exports/purchasing/company/company_<timestamp>.csv
exports/purchasing/company/company_<timestamp>.manifest.json
```

Every object (and every child relation — see below) gets its own stable folder, `{out}/{module}/{object_name}/`, reused across runs — each run adds a new timestamped file rather than overwriting or scattering across a new run directory, so history for a given extract accumulates in one place. The manifest records module, object, env, row count, CSV path, and start/finish timestamps. (Note: the timestamp is second-resolution — two runs of the same object within the same second will collide and overwrite each other.)

### Exporting a whole module at once

Omit `--object` to export every object in the module, one after another, in the module YAML's `extract_order` (declaration order if `extract_order` is empty):

```bash
migrate export --module inventory --env MAXDEMO --flat --skip-errors
```

Every object in the run gets its own CSV/manifest under `{out}/{module}/{object_name}/`, all sharing one run timestamp, plus a final summary line reporting how many objects were exported and their combined row count. Any flag (`--flat`, `--skip-errors`, `--site`, `--filter`, `--where`) applies uniformly to every object in the module. If one object fails, the export stops immediately — objects already exported before the failure keep their files; nothing already written is rolled back, but nothing after the failure runs either. (If an environment has an object that consistently can't be read — e.g. an unpublished object structure, or one that needs `--skip-errors` — a whole-module run will always stop there; export that module's objects individually with `--object` instead until it's resolved.)

### Objects with child relations

An object with children configured in its YAML (e.g. `inventory_balance` → `invbalances`, `invcost`) gets one CSV per relation automatically, however deep — no separate command needed, each in its own same-named folder alongside the parent's (`{out}/{module}/{child_name}/`).

Each child CSV carries its declared `foreign_key` fields as its own leading columns, so it's traceable back to its parent without needing that file open alongside it. The manifest's `children` map reports each relation's own row count and CSV path.

Pass `--flat` to skip every child relation and export only the object's own fields — e.g. for a huge parent table where you want the flat rows without paying for the nested fetch:

```bash
migrate export --module inventory --object inventory_balance --env MAXDEMO --flat
```

### Skipping unreadable records instead of aborting

Some live Maximo data can throw a server-side error on a plain read that has nothing to do with your query — e.g. a rotating-item business rule firing during a GET. `--skip-errors` (OSLC objects only) retries a failing page one row at a time, skips just the offending record(s), and logs them instead of aborting the whole export:

```bash
migrate export --module inventory --object inventory_balance --env MAXDEMO --skip-errors
```

This writes `{object}_{timestamp}.skipped.jsonl` (one JSON line per skipped record — `resource`, `pageno`, `reason_code`, `message`) in that object's own folder alongside its CSV, and the manifest's `skipped_count`/`skipped_log` fields report it. Only a small, known allowlist of Maximo reason codes is treated as skippable (currently just `BMXAA1825E`) — anything else still aborts the export as before.

### Filtering what gets exported

```bash
# Site-scope
migrate export --module purchasing --object purchase_order --env MAXDEMO --site BEDFORD

# Arbitrary field=value conditions (repeatable, ANDed together)
migrate export --module inventory --object item --env MAXDEMO \
  --filter status=ACTIVE --filter commoditygroup=ELEC

# Raw OSLC where clause — full manual control, overrides --site/--filter entirely.
# --where is passed to Maximo completely unmodified: any valid OSLC where
# expression works, not just what --site/--filter can express (AND, ranges,
# IN-lists, etc.)
migrate export --module purchasing --object purchase_order --env MAXDEMO \
  --where 'status="APPR" and totalcost>5000'

# "Value is one of several" -- prefer an IN-list over OR (see note below)
migrate export --module purchasing --object purchase_order --env MAXDEMO \
  --where 'status in ["APPR","WAPPR"]'
```

`--site` and a `--filter siteid=...` condition can't both be set (ambiguous — pick one). Filter values can't contain a literal `"` (rejected rather than guessed at escaping, since that's exactly the kind of value that could otherwise turn a filter into an always-true query).

**`OR` can be unreliable depending on whitespace and parentheses**, on top of Maximo's own OSLC query parser — behavior here varies enough (some environments parse it fine, others reject `field="A" or field="B"` depending on the exact spacing/nesting) that it's not worth memorizing rules for. Prefer `field in ["A","B"]` for "one of several values" — it's unambiguous and consistently supported — and reserve `or` for cases `in` genuinely can't express.

### Resuming a very large export (`--checkpoint` / `--resume`)

For a table big enough that a dropped connection hours in would hurt (e.g. `inventory_balance`), add `--checkpoint`:

```bash
migrate export --module inventory --object inventory_balance --env MAXDEMO --checkpoint
# ... fails on page 3,812 ...
migrate export --module inventory --object inventory_balance --env MAXDEMO --resume
```

With `--checkpoint`, pages are fetched by explicit cursor (OSLC `pageno`, REST MBO `_rsStart`) and, after every fully written page, `{object}_{timestamp}.checkpoint.json` records the cursor plus each output file's size and row count (parent and every child relation). If the export fails, its `.partial` files and the checkpoint are kept, and the error says how many pages were saved.

`--resume` picks up the newest checkpoint for that object: it truncates each partial file back to its last checkpointed size (dropping a half-written page), continues from the next page with the same API and the same run timestamp, then renames the files and writes the manifest exactly like an uninterrupted run (plus `"resumed": true` and `"resume_count"`). The checkpoint is deleted on success.

- The resume must use the same object definition, filter (`--site`/`--filter`/`--where`), and env; anything else is rejected rather than splicing two different queries into one file.
- `--skip-errors` and `--all-attributes` both survive a resume (skipped rows and discovered custom columns are part of the checkpoint).
- With `--object` omitted, `--resume` reuses the interrupted module run's timestamp and skips objects that already have a manifest for it.
- `--resume` with no checkpoint simply starts a fresh checkpointed export.
- A resume assumes the source rows don't move between pages in the meantime: Maximo pages by position, so rows inserted or deleted in the source during the outage can shift page boundaries. For a cutover extract, freeze the source first, as you would anyway.

### Other flags

| Flag | Purpose |
|---|---|
| `--env-file PATH` | Use a specific credentials file instead of the `env/.env.<prefix>` default |
| `--config-dir PATH` | Use a different module-YAML directory (repeatable); defaults to `config/modules` + `config/custom_objects` |
| `--out PATH` | Base output directory (default `exports/`) |

Run `./.venv/bin/migrate export --help` for the full list.

### Reading the output

Every CSV is RFC 4180, UTF-8 without a BOM. Maximo field values are written verbatim (no reformatting of dates/numbers), and missing/null values are empty strings, never the literal text `"None"`.

A failed export never leaves a truncated file at the final path — every CSV (object and child alike) is written to a `.partial` sibling and only renamed in once every file in the run has succeeded, so an incomplete run is never mistaken for a complete one.

## Inventory module: every export combination

A single consolidated reference for the `inventory` module specifically — every object (`item`, `storeroom`, `inventory_balance`), combined with every applicable flag. All commands below assume `./.venv/bin/migrate` and `--env MAXDEMO`; swap in your own env prefix.

### Whole module (all three objects in one command)

```bash
# Recommended default: flat + skip-errors, so inventory_balance's known
# rotating-item bug on this instance can't abort the other two objects
migrate export --module inventory --env MAXDEMO --flat --skip-errors

# Nested (adds item_spec/item_structure, invbalances/invcost) -- still needs
# --skip-errors, since inventory_balance's nested fetch hits the same bug
migrate export --module inventory --env MAXDEMO --skip-errors

# Without --skip-errors: item and storeroom still succeed, but the whole
# command exits non-zero the moment inventory_balance's turn fails -- not
# recommended against this environment, shown here only for completeness
migrate export --module inventory --env MAXDEMO --flat
```

### `item` (item master)

```bash
# Flat -- item's own fields only
migrate export --module inventory --object item --env MAXDEMO --flat

# Nested -- adds item_spec.csv and item_structure.csv
migrate export --module inventory --object item --env MAXDEMO

# Filtered by one field
migrate export --module inventory --object item --env MAXDEMO --flat --filter status=ACTIVE

# Filtered by multiple fields (ANDed)
migrate export --module inventory --object item --env MAXDEMO --flat \
  --filter status=ACTIVE --filter commoditygroup=FITTINGS

# Raw where clause -- anything --filter can't express (ranges, "or", etc.)
migrate export --module inventory --object item --env MAXDEMO --flat \
  --where 'status="ACTIVE" and rotating=0'
```
`item` has no `siteid` field (Maximo's ITEM master is itemset-scoped, not site-scoped) — `--site` isn't meaningful here and will error if used.

### `storeroom`

```bash
# storeroom has no children, so this is the only "shape" -- flat vs nested don't differ
migrate export --module inventory --object storeroom --env MAXDEMO

# Scoped to one site
migrate export --module inventory --object storeroom --env MAXDEMO --site BEDFORD

# Filtered by a field
migrate export --module inventory --object storeroom --env MAXDEMO --filter disabled=0

# Site + field filter together
migrate export --module inventory --object storeroom --env MAXDEMO --site FLEET --filter status=OPERATING
```

### `inventory_balance` (+ `invbalances`, `invcost`)

```bash
# Flat, safe default -- parent rows only, skip-errors required on this environment
migrate export --module inventory --object inventory_balance --env MAXDEMO --flat --skip-errors

# Nested -- adds invbalances.csv and invcost.csv
migrate export --module inventory --object inventory_balance --env MAXDEMO --skip-errors

# Scoped to one site, flat
migrate export --module inventory --object inventory_balance --env MAXDEMO --flat --skip-errors --site FLEET

# Scoped to one site, nested
migrate export --module inventory --object inventory_balance --env MAXDEMO --skip-errors --site BEDFORD

# Field filter (e.g. only ABC-class-A balances)
migrate export --module inventory --object inventory_balance --env MAXDEMO --flat --skip-errors \
  --filter abctype=A

# Combined site + custom where (raw --where overrides --site/--filter, so fold the site into it)
migrate export --module inventory --object inventory_balance --env MAXDEMO --skip-errors \
  --where 'siteid="FLEET" and abctype="A"'

# Without --skip-errors -- will abort partway through on environments where
# rotating-item inventory records trigger the business-rule error --skip-errors
# exists to work around (see "Skipping unreadable records" above)
migrate export --module inventory --object inventory_balance --env MAXDEMO --flat
```

### Other flags, combinable with any of the above

```bash
# Different output directory
migrate export --module inventory --object item --env MAXDEMO --flat --out /tmp/mig-exports

# Explicit credentials file instead of env/.env.maxdemo
migrate export --module inventory --object item --env MAXDEMO --flat --env-file /path/to/.env.maxdemo

# Different module-YAML directory (e.g. testing a custom_objects override)
migrate export --module inventory --object item --env MAXDEMO --flat --config-dir /path/to/config/modules
```

`--site` and a `--filter siteid=...` can't both be set (ambiguous, rejected). `--where` overrides `--site`/`--filter` entirely when given — it doesn't combine with them.

## Purchasing module: every export combination

The same kind of consolidated reference as inventory's, for `purchasing`'s five objects: `company` (vendors), `purchase_requisition` (PR), `rfq` (RFQ), `purchase_order` (PO), `receipt` (receiving).

Not every object structure is published/enabled on every Maximo instance — `rfq` in particular uses IBM's standard `mxapirfq` object structure name, but whether that's actually enabled varies by environment and installed features. If an object in this module fails against your environment with "Unknown Object" or a 404, that object structure isn't published there; check with whoever administers that Maximo instance, or try `--config-dir` with a corrected `oslc_resource` once you know the right name for it.

### Whole module

```bash
migrate export --module purchasing --env MAXDEMO --flat
```
If any one object in the module isn't available on the target environment, the whole-module run stops there (see "Exporting a whole module at once" above) — export the working objects individually with `--object` instead until that's resolved.

### `company` (vendors)

```bash
# All vendors (no children -- flat vs nested don't differ here either)
migrate export --module purchasing --object company --env MAXDEMO

# Filtered by type (V = vendor)
migrate export --module purchasing --object company --env MAXDEMO --filter type=V

# Active (non-disabled) vendors only
migrate export --module purchasing --object company --env MAXDEMO --filter disabled=0

# Filtered by org
migrate export --module purchasing --object company --env MAXDEMO --filter orgid=EAGLENA

# Combined filters (ANDed)
migrate export --module purchasing --object company --env MAXDEMO \
  --filter type=V --filter disabled=0

# One of several types, via IN (see the OR note above)
migrate export --module purchasing --object company --env MAXDEMO \
  --where 'type in ["V","M"]'

# Raw where clause -- e.g. org + currency together
migrate export --module purchasing --object company --env MAXDEMO \
  --where 'orgid="EAGLENA" and currencycode="USD"'
```
`company` has no `siteid` field (vendors are org-scoped in Maximo, not site-scoped) — `--site` isn't meaningful here and will error if used, same as `item` above.

### `purchase_requisition` (+ `pr_line`)

```bash
# Flat -- PR header fields only
migrate export --module purchasing --object purchase_requisition --env MAXDEMO --flat

# Nested -- adds pr_line.csv
migrate export --module purchasing --object purchase_requisition --env MAXDEMO

# Scoped to one site
migrate export --module purchasing --object purchase_requisition --env MAXDEMO --site BEDFORD

# Filtered by status
migrate export --module purchasing --object purchase_requisition --env MAXDEMO --flat --filter status=CLOSE

# Raw where clause
migrate export --module purchasing --object purchase_requisition --env MAXDEMO --flat \
  --where 'status="CLOSE" and priority>5'
```

### `purchase_order` (+ `po_line`)

```bash
# Flat -- PO header fields only
migrate export --module purchasing --object purchase_order --env MAXDEMO --flat

# Nested -- adds po_line.csv
migrate export --module purchasing --object purchase_order --env MAXDEMO

# Scoped to one site
migrate export --module purchasing --object purchase_order --env MAXDEMO --site BEDFORD

# Filtered by status
migrate export --module purchasing --object purchase_order --env MAXDEMO --flat --filter status=APPR

# Filtered by vendor
migrate export --module purchasing --object purchase_order --env MAXDEMO --flat --filter vendor=ATI

# Combined site + status
migrate export --module purchasing --object purchase_order --env MAXDEMO --flat \
  --site BEDFORD --filter status=APPR

# Raw where clause -- e.g. a cost threshold
migrate export --module purchasing --object purchase_order --env MAXDEMO --flat \
  --where 'status="APPR" and totalcost>1000'
```

### `receipt` (receiving)

```bash
migrate export --module purchasing --object receipt --env MAXDEMO

# Filtered by PO
migrate export --module purchasing --object receipt --env MAXDEMO --filter ponum=1000
```
`receipt` has no children (`children: []`), so there's no flat/nested distinction.

### `rfq` (Request for Quotation)

```bash
# Flat -- RFQ header fields only
migrate export --module purchasing --object rfq --env MAXDEMO --flat

# Nested -- adds rfq_line.csv
migrate export --module purchasing --object rfq --env MAXDEMO

# Filtered by status
migrate export --module purchasing --object rfq --env MAXDEMO --flat --filter status=APPR
```
Configured using IBM's standard `mxapirfq` object structure name. Confirm it's actually published on your target environment before relying on it (`migrate discover` would automate this once built — see Roadmap — or a manual check against `/maximo/api/os/mxapirfq`).

## Importing data

`migrate import` reads a CSV back and creates or updates matching records in Maximo, matched by the object's declared natural key (`key_fields`). **Dry-run by default** — nothing is written unless you pass `--execute`:

```bash
# Preview only -- no writes. Auto-locates the newest exports/inventory/item/item_*.csv
migrate export --module inventory --object item --env MAXDEMOSRC --flat
migrate import --module inventory --object item --env MAXDEMOTGT

# Actually write
migrate import --module inventory --object item --env MAXDEMOTGT --execute
```

By default it auto-locates the newest export for that module/object under `--out` (same folder `export` writes to); pass `--csv-file PATH` to import a specific file instead (e.g. one you hand-edited, or one that didn't come from this tool's own `export`):

```bash
migrate import --module inventory --object item --env MAXDEMOTGT --csv-file exports/inventory/item/item_<timestamp>.csv --execute
```

Every row writes a `{key, action, href, error}` line to `{object}_{timestamp}.import-log.jsonl`, and a per-run summary (`counts`, `dry_run`, `csv_file`) to `{object}_{timestamp}.import-result.json`, both in that object's own `{out}/{module}/{object_name}/` folder alongside its exports. If any row fails, the command exits non-zero after processing every row (a bad row doesn't stop the rest from being attempted).

**Constraints:**
- OSLC objects only (the object needs an `oslc_resource` configured) — no REST MBO write path exists.
- Omit `--object` to import every object in the module's `load_order`; the run stops at the first object with failures.
- The default per-row mode writes only an object's own fields. Use `--bulk` (below) to load child relations nested in their parent.
- CSV values are always strings; whether Maximo's write path coerces them to an attribute's real type (NUMBER, BOOLEAN) hasn't been confirmed with a live `--execute` run as of this writing — treat your first real `--execute` on any given object as validating that, not as already-settled.
- A create that fails after possibly reaching the server (timeout, 5xx) is automatically re-checked by natural key before being reported as failed, so re-running an import is safe from double-creates — but is **not** wrapped in this tool's usual retry/backoff (only the natural-key lookup GET is retried); a failed create or update is reported once, not retried in-process.

### Bulk import with child relations (`--bulk`, MxLoader engine)

```bash
# Dry run: builds and type-checks every nested payload, sends nothing
migrate import --module purchasing --object purchase_order --env MAXDEMOTGT --bulk

# Write: PO headers + their po_line rows in one BULK record per PO, 50 POs per request
migrate import --module purchasing --object purchase_order --env MAXDEMOTGT --bulk --execute

# Every object in the module, in load_order
migrate import --module inventory --env MAXDEMOTGT --bulk --execute
```

`--bulk` reads the parent CSV plus every relation CSV **from the same export run** (matching timestamp). It nests the child rows under their parent by foreign key and POSTs batches with `x-method-override: BULK`. Each record carries `_action`, and Maximo returns a per-record status, so one bad record fails alone while the rest of the batch commits. Before sending, the CSVs go through the referential-integrity check.

| Flag | Meaning (MxLoader equivalent) |
|---|---|
| `--action AddChange\|Add\|Change\|Delete` | Sheet action (cell C1). Default `AddChange`. `Sync` is refused over REST, same as MxLoader |
| `--batch-size N` | Objects per request; `0` = all in one (*Batch size*) |
| `--continue-on-error` | Keep going after a failed batch (*Stop on error = False*) |
| `--start-row N` | Resume from parent CSV data row N (*resume from last row in error*) |
| `--datadict FILE` | Attribute types; defaults to `config/datadict/<env>.csv` if refreshed |
| `--timezone +00:00` | Appended to DATETIME values that don't carry an offset |

`--start-row` also works without `--bulk`. A stopped run prints the row to resume from, and the import-result JSON records it as `resume_row`.

## MxLoader-compatible sheets (`migrate mxl`)

`src/maxmig/mxl/` is a port of MxLoader's VBA architecture, module by module: MxSheet, ConvSheetToNode/ConvNodeToSheet, MxNode, AdjustVal, MxAttrCfg, MxMifService (REST), and MxRunSync/MxRunQuery. It reproduces MxLoader's behaviour; none of its code or data is copied, because MxLoader is licensed for non-commercial use only. A sheet that works in MxLoader works here too. The layout:

```
row 1:  A=object structure  B=main object  C=action  D=where  E=order by  F=OSLC directives
row 2:  PONUM | SITEID | EX_BUDGET | POLINE.POLINENUM | POLINE.ORDERQTY | [POID] | [RESULT]
row 3+: data. Consecutive rows with identical parent values are one parent; REL.ATTR columns are its children
```

```bash
migrate mxl run assets.xlsx --sheet Assets --env MAXDEMO              # dry run: validates every payload
migrate mxl run assets.xlsx --sheet Assets --env MAXDEMO --execute    # writes [RESULT] and [ID] columns back
migrate mxl run query.csv --env MAXDEMO                                # action Query: fills the sheet
migrate mxl template --module inventory --object item --output item.xlsx
migrate mxl from-export --module purchasing --object purchase_order --output po.xlsx  # export -> editable sheet
```

Sheets can be `.csv` files or `.xlsx`/`.xlsm` workbooks; workbooks need the `excel` extra (`pip install -e ".[excel]"`). The port has a few deliberate differences from MxLoader:
- Sibling child relations (e.g. `POLINE.*` next to `POTERM.*`) nest correctly. MxLoader nests the second relation under the first.
- Writes are dry-run unless you pass `--execute`.
- Results go to every sheet row of a multi-row object, not just its first row.
- Only the REST service is supported, not the legacy MIF OS/XML service.
- A `[ERROR]` column (MxLoader's error-only virtual attribute) is filled for failed rows and blanked for successful ones.

### Formulas instead of Excel (`migrate mxl fill`)

MxLoader leaves data transformation to Excel: reference data sits in another worksheet, row 3 of the MxLoader sheet holds formulas, and the *FillData* macro drags that row down once per source row. `migrate mxl fill` does the same thing headless, with the same formula syntax:

```bash
# an MxLoader workbook: row 3 holds formulas, D1 names the source worksheet, other sheets are VLOOKUP tables
migrate mxl fill assets.xlsx --sheet Assets --output assets_filled.xlsx

# or a reviewable mapping file
migrate mxl fill mappings/assets.yaml --output assets.csv
migrate mxl run assets.csv --env MAXDEMO              # then load it as usual
```

```yaml
# mappings/assets.yaml
object_service: MXAPIASSET
object: ASSET
action: AddChange
source: legacy/assets.csv               # or legacy.xlsx:Assets
lookups:
  LOCMAP: legacy/location_map.csv       # first column is the key
filter: '=[Status] <> "SCRAPPED"'       # optional: drop rows where this is FALSE
script: transforms.py                   # optional: see "Hook scripts" below
columns:
  ASSETNUM: '=CONCATENATE("PUMP", RIGHT([Asset], 3))'
  SITEID: BEDFORD                       # constants are copied to every row
  DESCRIPTION: '=PROPER([Description])'
  LOCATION: '=VLOOKUP([Location], "LOCMAP", "NEWLOC", FALSE)'
  "[ASSETUID]": ""
```

Source columns are `[Any Header]` or a bare `Name`, case-insensitive. Operators are `&`, `+ - * /`, and `= <> < > <= >=`, with Excel precedence. Functions: `CONCATENATE`/`CONCAT`, `TEXTJOIN`, `LEFT`, `RIGHT`, `MID`, `LEN`, `UPPER`, `LOWER`, `PROPER`, `TRIM`, `REPLACE`, `SUBSTITUTE`, `REPT`, `FIND`, `SEARCH`, `EXACT`, `TEXT` (zero-padding, decimals, dates), `VALUE`, `ROUND`, `INT`, `ABS`, `MIN`, `MAX`, `SUM`, `TODAY`, `NOW`, `DATEVALUE`, `IF`, `IFS`, `SWITCH`, `IFERROR`, `AND`, `OR`, `NOT`, `ISBLANK`, `ISNUMBER`, `COALESCE`, `NULL()` (MxLoader's `~NULL~`), and `VLOOKUP(value, "TABLE", column-number-or-header[, FALSE])`. `VLOOKUP` is exact-match only, because approximate matching on unsorted migration data loads wrong values silently. Formulas are parsed, never handed to Python `eval`. A bad row doesn't stop the fill: every failing source row is listed with its column and error, and nothing is written until they're all fixed.

### Hook scripts (`--script`): the VBA-macro layer

Anything a sheet or a formula can't express is done in MxLoader with VBA. MaxMig has the same extension point in Python: a `.py` file passed with `--script` to `mxl run`, `mxl batch`, or `mxl fill` (or named in a mapping's `script:`). Every hook is optional:

```python
from maxmig.mxl.scripting import formula

@formula("PADNUM")                       # a UDF, usable in fill formulas: =PADNUM([Asset], 8)
def padnum(value, width):
    return str(value).zfill(int(width))

def transform_row(row, ctx):             # fill: each source row; return None to drop it
    return row

def before_run(ctx): ...                 # load: once, before the first batch
def before_record(payload, ctx):         # load: each top-level object's JSON; None skips it
    payload.setdefault("siteid", "BEDFORD")
    return payload
def after_record(outcome, ctx): ...      # load: each outcome (status, message, ids, rows)
def after_run(summary, ctx): ...         # load: once, with the counts
```

`ctx.state` is a dict that lasts for the whole run (VBA's module-level variables), and `ctx.dry_run`, `ctx.env`, and `ctx.sheet` say what's running. An exception in `transform_row`, `before_record`, or a UDF fails that row or object and is reported in `[RESULT]`; an exception in `before_run`/`after_run` aborts the run. As with enabling macros in MxLoader, a script runs with your permissions and is only ever loaded through an explicit `--script`.

### Batches (`migrate mxl batch`)

MxLoader's *RunBatch* sheet runs several sheets in dependency order: companies, then items, then storerooms, then balances. `migrate mxl batch` reads the same layout. Row 1 is `RunBatch`, row 2 is `Sheet, Action, Status, Query, Sync, Error, Execution Date, Execution Duration`, and each following row names a sheet: a worksheet of the same workbook, a `.csv`/`.xlsx` path relative to the batch file, or `book.xlsx:Worksheet`.

```bash
migrate mxl batch cutover.xlsx --env MAXTEST              # dry run every load sheet
migrate mxl batch cutover.xlsx --env MAXTEST --execute    # run them; status columns written back
```

A blank `Action` uses the sheet's own C1 action. `Skip` and `ExportCsv` (writes the sheet to `Export/<name>.csv`, no Maximo call) work as in MxLoader; `ExportXml`, which is MIF-only, is rejected. A sheet with failed objects stops the batch, so later sheets never load against half-loaded prerequisites. `--continue-on-error` runs everything and reports every failure.

Typed values work the same way as in MxLoader, driven by the data dictionary:
- Numeric types are sent as JSON numbers.
- `YORN` is sent as a boolean (anything other than 0/1/true/false is rejected).
- `DATE`/`DATETIME`/`TIME` are normalized to ISO.
- `~NULL~` explicitly blanks an attribute.
- `FileTxt:/` and `FileEnc:/` inline a file's contents.

## Custom attributes

Custom (user-defined) attributes are supported everywhere, whether declared in config or added as needed:

| Need | How |
|---|---|
| Permanently part of an object | `custom_fields: [ex_criticality]` in the module YAML (objects and child relations) |
| One-off on an export | `--extra-field ex_attr` or `--extra-field po_line.ex_attr` |
| "Give me everything, including attributes nobody listed" | `--all-attributes`, or `capture_all_attributes: true` |
| Load a column you added to the CSV by hand | Nothing to do: every non-empty CSV column is sent on import |
| Typed correctly on load | `migrate datadict refresh --env X` (reads MAXATTRIBUTE, custom ones included) or `field_types: {ex_cost: AMOUNT}` in YAML |
| MxLoader sheet | Just add the column header, e.g. `EX_CRITICALITY` or `POLINE.EX_REF` |
| Draft config for a custom object structure | `migrate discover --object CLIENT_ASSET --env X` (uses the dictionary's USERDEFINED flags to split out `custom_fields`) |

`migrate datadict refresh` reads MAXATTRIBUTE through an object structure; the default is MxLoader's own `MXL_MAXATTRIBUTECFG`, and you can choose another with `--resource`. See `config/custom_objects/_example_custom_asset.yaml` for a complete example.

## Inspection and checks

```bash
migrate list-modules
migrate list-objects --module purchasing
migrate status [--module inventory]              # newest export/import per object
migrate check --module purchasing --object purchase_order   # orphans, duplicate/blank keys
migrate discover --object asset --env MAXDEMO    # probe and draft config/custom_objects/asset.discovered.yaml
```

## Roadmap

Not built yet:

- Concurrency (parallel BULK requests).
- A live `--execute` round-trip validation against a real dev→test pair.

## Project layout

```
src/maxmig/
├── cli.py                # `migrate export` / `migrate import` (click)
├── config.py              # .env loading, EnvConfig
├── api/
│   ├── client.py           # MaximoClient — auth, REST-MBO/OSLC fallback
│   ├── rest_mbo.py          # REST MBO pagination
│   ├── oslc.py              # OSLC pagination + child-relation select
│   ├── filter.py            # site/field/raw-where query filters
│   └── http.py              # shared GET-with-retry-and-parse
├── model/
│   ├── schema.py            # ObjectDef/RelationDef pydantic models
│   └── registry.py          # loads config/modules/*.yaml
├── extract/
│   ├── writer.py            # streaming, atomic CSV writer(s) — object + child relations
│   └── checkpoint.py        # export checkpoints for --checkpoint / --resume
├── load/
│   ├── reader.py             # CSV -> row dicts, for import
│   └── importer.py           # natural-key lookup + create-or-update (dry-run by default)
├── mxl/                     # MxLoader engine port (sheet, node, values, datadict, convert, service, runner, bridge)
│                            #   + formula/fill (FillData), scripting (VBA-macro hooks), batch (RunBatch)
├── discover.py              # probe an object structure, draft its YAML
├── integrity.py             # CSV referential-integrity check
├── runs.py                  # list-modules / list-objects / status helpers
└── retry.py                 # shared retry/backoff policy

config/modules/              # module YAML — the config-driven object definitions
env/                          # credentials (.env.example committed, real files gitignored)
tests/unit/                   # all against mocked HTTP — no live Maximo needed
```

## License

MIT — see [LICENSE](LICENSE).
