# MaxMig — End-User Runbook

The complete process for exporting data out of a Maximo / MAS environment with MaxMig, start to finish. Every command below is copy-paste ready — just swap in your own `<ENV>` prefix.

> Looking for every flag/object/filter option instead of the step-by-step process? See [`docs/export-commands.md`](export-commands.md).

## What this does today

MaxMig **exports** data from a Maximo 7.6.1.2 or MAS 9 environment to CSV, and **imports** a CSV back into a target environment (create-or-update, matched on each object's declared natural key). Import is **dry-run by default** — nothing is written unless you pass `--execute`.

This runbook walks through the export process. For the import side, see [Importing data](../README.md#importing-data) in the README, and [Limitations](#limitations-what-this-cant-do-yet) below for what's still missing.

Pilot scope: the `inventory` and `purchasing` modules only.

---

## Step 1 — One-time setup

Run these once, from inside the `/maxdata_mig` project folder.

```bash
# Create a virtual environment
python3 -m venv .venv

# Install maxmig and its dependencies into it
./.venv/bin/pip install -e ".[dev]"
```

You now have a `migrate` command available at `./.venv/bin/migrate`. Confirm it works:

```bash
./.venv/bin/migrate --help
```

## Step 2 — Set up credentials for your environment

Credentials live in a small text file under `env/`, named after the environment you're connecting to. Copy the template to start:

```bash
cp env/.env.example env/.env.<prefix>
```

Replace `<prefix>` with a short uppercase name for your environment, lowercased in the filename — e.g. for `MAXDEMO` you'd create `env/.env.maxdemo`.

Open that file and fill in **one** of the two auth modes below (not both).

**Option A — username/password** (works on most Maximo/MAS instances):

```
MAXDEMO_MANAGE_URL=https://maxdemo.manage.example.com/maximo/
MAXDEMO_MANAGE_USERNAME=maxadmin
MAXDEMO_MANAGE_PASSWORD=changeme
```

**Option B — API key** (required when the instance is SSO/OIDC-fronted and rejects a plain username/password login). Generate the key by hand first, in the Maximo/MAS UI — there's no command-line way to create one:
- Manage → **Security → API Keys** app, or
- the MAS Suite console's API Keys page

```
MAXDEMO_MANAGE_URL=https://maxdemo.manage.example.com/maximo/
MAXDEMO_MANAGE_APIKEY=<the key you generated>
```

If a file has both an API key and a username/password, the API key is used and the password is ignored.

> Already have a credentials file for this environment elsewhere (e.g. under `/path/to/credentials/`)? You don't need to duplicate it — pass `--env-file /path/to/that/file` on every command below instead of relying on `env/.env.<prefix>`.

`env/.env.*` files never get committed to git — only the example template does.

## Step 3 — Run the export

Every export follows this shape:

```bash
./.venv/bin/migrate export --module <module> --object <object> --env <ENV> --out exports/
```

`<ENV>` is the same prefix you used in Step 2 (e.g. `MAXDEMO`), in uppercase.

### Recommended run — every object, in the right order

Parent objects need to be exported before the children/dependents that reference them. Run these in this order:

```bash
# --- inventory module ---
./.venv/bin/migrate export --module inventory --object item              --env <ENV> --out exports/
./.venv/bin/migrate export --module inventory --object storeroom         --env <ENV> --out exports/
./.venv/bin/migrate export --module inventory --object inventory_balance --env <ENV> --out exports/

# --- purchasing module ---
./.venv/bin/migrate export --module purchasing --object company              --env <ENV> --out exports/
./.venv/bin/migrate export --module purchasing --object purchase_requisition --env <ENV> --out exports/
./.venv/bin/migrate export --module purchasing --object purchase_order       --env <ENV> --out exports/
./.venv/bin/migrate export --module purchasing --object receipt              --env <ENV> --out exports/
```

Each object gets its own stable folder, `exports/{module}/{object_name}/`, reused across runs — every run adds a new `{object}_{timestamp}.csv` plus a matching `{object}_{timestamp}.manifest.json` alongside it, so history for a given object accumulates in one place instead of scattering across a new run folder each time. An object with child relations also gets one CSV per child, each in its own same-named folder.

### Narrowing what gets exported (optional)

Add one of these to any command above if you don't want the whole table:

```bash
# Only one site
./.venv/bin/migrate export --module purchasing --object purchase_order --env <ENV> --site BEDFORD

# One or more field=value conditions (repeatable — combined with AND)
./.venv/bin/migrate export --module inventory --object item --env <ENV> \
  --filter status=ACTIVE --filter commoditygroup=ELEC

# Full manual control — a raw query clause (overrides --site/--filter entirely)
./.venv/bin/migrate export --module purchasing --object purchase_order --env <ENV> \
  --where 'status="APPR" and totalcost>5000'
```

Rules: don't combine `--site` with a `--filter siteid=...` (they'd conflict). Filter values can't contain a literal `"` character.

## Step 4 — Verify the output

For each export, check:

1. A new CSV appeared under `exports/{module}/{object_name}/` (e.g. `exports/inventory/item/item_<timestamp>.csv`).
2. A matching `{object}_{timestamp}.manifest.json` sits beside it — open it and confirm the row count looks right and there's a `finished` timestamp (its presence means the export completed; a missing manifest, or a leftover `.partial` file, means it didn't).
3. The CSV file is non-empty (for anything but a genuinely empty table).

A run that fails partway never leaves a broken CSV at the final filename — you'll either get a complete file or none at all, so a present file is always trustworthy.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `command not found: migrate` | You didn't run Step 1, or you're not using the `./.venv/bin/` prefix |
| 401 / login failure | Wrong password, or this instance needs an API key instead (Option B in Step 2) |
| "No file env/.env.\<prefix\>" | The `<prefix>` in `--env` doesn't match your filename, or you need `--env-file` to point at a file that lives elsewhere |
| `--site` and `--filter siteid=` both set | Pick one — remove the other |
| Command hangs / very slow on `inventory_balance` | Expected — this table can be ~1M rows; let it finish |

## Limitations (what this can't do yet)

- **No whole-module import.** `export` can run every object in a module at once (omit `--object`); `import` handles one object per run.
- **No child-relation import.** Child CSVs (`item_spec`/`item_structure`, `pr_line`, `po_line`, `invbalances`) *are* written on export, but only top-level objects can be imported back.
- **No `discover` command.** Adding a brand-new object type still means someone hand-writes its YAML under `config/modules/`.
- **No checkpoint/resume.** A very large export that fails partway has to be re-run from the start.
- **`--execute` not yet exercised live.** The import create/update paths are unit-tested against mocked HTTP, but haven't been run against a real environment — treat your first real `--execute` as validating that.

See [`docs/DESIGN.md`](DESIGN.md) for the full design of what's planned next.
