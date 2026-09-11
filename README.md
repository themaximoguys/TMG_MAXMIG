# MaxMig — Data Migration and Dataload Tool

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-A31F34)](LICENSE)
[![Tests: 163 passing](https://img.shields.io/badge/tests-163%20passing-0A9396?logo=pytest&logoColor=white)](tests/unit)
[![Status: pilot](https://img.shields.io/badge/status-pilot-CA6702)](#roadmap)

An API-based data migration CLI for IBM Maximo / IBM Maximo Application Suite (MAS). One tool, driven entirely by which environment config you point it at — the same commands work whether you're migrating Maximo 7.6.1.2 → MAS 9, or moving data between two MAS environments (dev → test, client cutover, etc.).

Pilot scope: **Inventory Management** and **Purchasing** modules. The design is config-driven (YAML), so adding the rest of the standard Maximo modules later is a config change, not a code change.

![MaxMig architecture: a YAML config layer drives both an export path (source Maximo/MAS environment through cli.py, api/client.py and extract/writer.py out to timestamped CSV and manifest files on disk) and an import path (CSV back through load/reader.py and load/importer.py, then out through the same api/client.py to a target environment). Source and target are the same kind of environment reached by the same code — only the .env file selected by --env differs.](docs/images/architecture.svg)

**Current status:** the `export` side works end-to-end (extract from a Maximo/MAS environment → CSV + manifest). `import` (load a CSV into a target environment, create-or-update by natural key) also works, dry-run by default — see [Importing data](#importing-data) below; its create/update paths are unit-tested against mocked HTTP but haven't yet been exercised with a live `--execute` run. Checkpointing/resume for very large tables and custom-object discovery are not built yet — see [Roadmap](#roadmap) and [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

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
- One object per run — unlike `export`, there's no whole-module `import` yet.
- No nested-child writes — only an object's own fields import; `invbalances`/`item_spec`-style children aren't created/updated (their CSVs would need their own `import` runs against their own object structures, which this tool doesn't expose as importable objects yet).
- CSV values are always strings; whether Maximo's write path coerces them to an attribute's real type (NUMBER, BOOLEAN) hasn't been confirmed with a live `--execute` run as of this writing — treat your first real `--execute` on any given object as validating that, not as already-settled.
- A create that fails after possibly reaching the server (timeout, 5xx) is automatically re-checked by natural key before being reported as failed, so re-running an import is safe from double-creates — but is **not** wrapped in this tool's usual retry/backoff (only the natural-key lookup GET is retried); a failed create or update is reported once, not retried in-process.

## Roadmap

Not built yet (see [`docs/DESIGN.md`](docs/DESIGN.md) for the full design):

- Whole-module `import`, and importing child relations (`invbalances`, `item_spec`, etc.), not just top-level objects.
- Checkpointing/resume for very large tables (item master, inventory balances — up to ~1M rows).
- `migrate discover` — probe an unfamiliar/custom object structure and draft its YAML config.
- Batched OSLC BULK-mode requests for the load side, instead of one API call per record (verified against IBM's own Maximo Manage REST API Guide as the right mechanism for the ~1M-row tables).

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
│   └── writer.py            # streaming, atomic CSV writer(s) — object + child relations
├── load/
│   ├── reader.py             # CSV -> row dicts, for import
│   └── importer.py           # natural-key lookup + create-or-update (dry-run by default)
└── retry.py                 # shared retry/backoff policy

config/modules/              # module YAML — the config-driven object definitions
env/                          # credentials (.env.example committed, real files gitignored)
tests/unit/                   # all against mocked HTTP — no live Maximo needed
```

## License

MIT — see [LICENSE](LICENSE).
