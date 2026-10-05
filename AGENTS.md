# Migration Platform — Agent Knowledge Base

Concise repository rules for working on this codebase. Architecture detail lives in
`docs/architecture/platform_architecture.md`; engine/driver compatibility lives in
`docs/compatibility_matrix.md`; the release procedure lives in `docs/release_process.md`.
Do not duplicate those documents here.

## Layout

- `core/` — shared layer: `orchestrator.py`, `validator.py`, `secrets/`, `schema_mapping.py`, `alerting.py`, `retry.py`, `preflight.py`, `reporting/`
- `core/connectors/<engine>/` — per-engine packages (`mssql`, `mysql`, `postgresql`), each split into `source.py`, `target.py`, `cdc.py`, `_models.py`, and `objects/` for per-object modules
- `core/connectors/base.py` — `SourceConnector`/`TargetConnector` base classes and `quote_identifier`
- `migration_platform/` — CLI entry point (`__main__.py`, `__init__.py`); holds the version, read from installed metadata
- `tests/unit/`, `tests/integration/` — test suites
- `tests/e2e/<engine>/` — automated regression E2E (Mode 1): phased runners, setup, validation
- `tests/e2e/acceptance/` — shared acceptance E2E utilities (Mode 2): `E2EConfig`, config loader, report writer
- `config/` — YAML configs; `config/migration_config.schema.yaml` documents the shape
- `docs/`, `docker/`, `scripts/`, `bootstrap.py` — documentation, image, tooling, dev setup

## Rules

- **Dependencies** — `pyproject.toml` is the single source of truth. Do not add a
  requirements file or duplicate the dependency list.
- **Version** — declared once in `pyproject.toml` `[project].version`. Never hardcode a
  second copy; read it via `importlib.metadata`.
- **Secrets** — the resolver in `core/secrets/env.py` prepends `SECRET_`. Config must use
  bare names (e.g. `mssql_source_pass`), never prefixed. Never commit a password.
- **Identifier quoting** — per engine: MSSQL/postgres double quotes, MySQL backticks.
  Use `quote_identifier` in `core/connectors/base.py` rather than manual quoting.
- **Layering** — `core/*` must not import from `migration_platform/`. The reporting layer
  reads installed metadata directly for this reason.
- **Tests** — the 0.2.1 baseline is 755 passing, 4 known failures
  (`test_cross_engine_type_safety.py` ×2, `test_mysql_datatypes.py` ×2). Those four are
  pre-existing; do not delete or weaken them to force a green run. New failures are yours.
- **MSSQL** — the system ODBC driver must be "ODBC Driver 18 for SQL Server"; it is an OS
  component, not a Python dependency.

## Validation

```bash
pip install -e ".[dev]"
python scripts/release_validation.py   # 22 checks; must pass before tagging
pytest tests/unit/ -q
```

A real `docker build` is required before release; it cannot be substituted by a local
install simulation.
