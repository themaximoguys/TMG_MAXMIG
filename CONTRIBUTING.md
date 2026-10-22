# Contributing to MaxMig

Thanks for your interest in MaxMig. Bug reports, fixes, new module configs, and docs improvements are all welcome.

## Reporting issues

Open an issue at https://github.com/themaximoguys/TMG_MAXMIG/issues and include:

- the `migrate` command you ran (with credentials removed)
- your Maximo / MAS version
- the full error output, or the relevant lines from `logs/`

**Never paste real hostnames, usernames, passwords, API keys, or exported client data** into an issue or PR.

## Development setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest tests/unit -v
```

All tests run against mocked HTTP — no live Maximo instance is needed.

## Making a change

1. Fork the repo and branch from `develop`.
2. Keep the change focused — one fix or feature per PR.
3. Add or update unit tests under `tests/unit/` for any behaviour change.
4. Make sure the full suite passes.
5. Update `README.md` or `docs/` if you change a command, flag, or module config.
6. Open a pull request against `develop` describing what changed and why.

## Adding a new object or module

Objects are defined in YAML under `config/modules/` (or `config/custom_objects/` for non-OOB object structures). Use `migrate discover --object <NAME> --env <PREFIX>` to draft a config from a live instance, then review it before committing. See [`docs/export-commands.md`](docs/export-commands.md) for the object → Maximo table mapping.

## MxLoader code

`src/maxmig/mxl/` reproduces MxLoader's behaviour, but MxLoader is licensed for non-commercial use only — **do not copy MxLoader VBA code or data into this repo**. Reimplement from behaviour instead.

## Credentials and data

- Real `.env` files are gitignored; only `env/.env.example` is committed. Keep it that way.
- `exports/`, `state/`, and `logs/` are run outputs and must not be committed.
- Use `example.com` hostnames and placeholder values in tests and docs.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
