# Export command reference

Every export follows the same shape:

```bash
./.venv/bin/migrate export --module <module> --object <object> --env <ENV_PREFIX> [options]
```

`<ENV_PREFIX>` must have a matching `env/.env.<prefix-lowercased>` file (see `env/.env.example`), or pass `--env-file` to point at one elsewhere (e.g. `/path/to/credentials/.env.<name>`).

## All objects, ready to run

Substitute your real `--env` prefix (e.g. `MASDEV`, `MAXDEMO`) for `<ENV>` below.

### `inventory` module

```bash
./.venv/bin/migrate export --module inventory --object item              --env <ENV>
./.venv/bin/migrate export --module inventory --object storeroom         --env <ENV>
./.venv/bin/migrate export --module inventory --object inventory_balance --env <ENV>
```

### `purchasing` module

```bash
./.venv/bin/migrate export --module purchasing --object company              --env <ENV>
./.venv/bin/migrate export --module purchasing --object purchase_requisition --env <ENV>
./.venv/bin/migrate export --module purchasing --object purchase_order       --env <ENV>
./.venv/bin/migrate export --module purchasing --object receipt              --env <ENV>
```

> **Children aren't separately exportable yet.** `item_spec`/`item_structure` (under `item`), `pr_line` (under `purchase_requisition`), `po_line` (under `purchase_order`), and `invbalances` (under `inventory_balance`) are declared in the module YAML and pulled via OSLC `oslc.select` alongside their parent, but the CLI currently writes only the parent object's flat CSV — there's no `--object pr_line` etc. (`registry.get_object` only resolves top-level objects). Not a config problem; it's a gap in `cli.py`/`extract` today.

## Recommended run order (referential integrity)

Matches each module's `extract_order` in the YAML — parents before the children/dependents that reference them:

```bash
# inventory
./.venv/bin/migrate export --module inventory --object item              --env <ENV>
./.venv/bin/migrate export --module inventory --object storeroom         --env <ENV>
./.venv/bin/migrate export --module inventory --object inventory_balance --env <ENV>

# purchasing
./.venv/bin/migrate export --module purchasing --object company              --env <ENV>
./.venv/bin/migrate export --module purchasing --object purchase_requisition --env <ENV>
./.venv/bin/migrate export --module purchasing --object purchase_order       --env <ENV>
./.venv/bin/migrate export --module purchasing --object receipt              --env <ENV>
```

## Filtering variants

```bash
# Site-scope
./.venv/bin/migrate export --module purchasing --object purchase_order --env <ENV> --site BEDFORD

# field=value conditions (repeatable, ANDed)
./.venv/bin/migrate export --module inventory --object item --env <ENV> \
  --filter status=ACTIVE --filter commoditygroup=ELEC

# Raw OSLC where clause (overrides --filter/--site entirely)
./.venv/bin/migrate export --module purchasing --object purchase_order --env <ENV> \
  --where 'status="APPR" and totalcost>5000'
```

`--site` and `--filter siteid=...` can't both be set. Filter values can't contain a literal `"`.

## Other flags

| Flag | Purpose |
|---|---|
| `--env-file PATH` | Use a specific credentials file instead of `env/.env.<prefix>` |
| `--config-dir PATH` | Use a different module-YAML directory (repeatable) |
| `--out PATH` | Base output directory (default `exports/`) |

## Object → source table quick reference

| Module | Object | Maximo object | Key fields | Access path |
|---|---|---|---|---|
| inventory | `item` | ITEM | itemnum, itemsetid | OSLC (`mxapiitem`) |
| inventory | `storeroom` | LOCATIONS (type=STOREROOM) | location, siteid | OSLC (`mxapilocations`) |
| inventory | `inventory_balance` | INVENTORY | itemnum, itemsetid, location, siteid | REST MBO (`inventory`) — ~1M-row table |
| purchasing | `company` | COMPANY | company, orgid | OSLC (`mxapicompany`) |
| purchasing | `purchase_requisition` | PR | prnum, siteid | OSLC (`mxapipr`) |
| purchasing | `purchase_order` | PO | ponum, siteid | OSLC (`mxapipo`) |
| purchasing | `receipt` | MATRECTRANS | matrectransid, siteid | REST MBO (`matrectrans`) — no OOB OSLC resource |
</content>
