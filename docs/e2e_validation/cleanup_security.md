# Cleanup & Security

## Overview

The E2E acceptance framework includes multiple layers of protection to prevent
accidental destruction of production databases and leakage of credentials.

## Protected Databases

Each engine maintains a `PROTECTED_DATABASES` frozenset in
`tests/e2e/<engine>/setup/target.py`. These databases are **never** dropped,
reset, or cleaned by E2E tooling.

### MSSQL — `tests/e2e/mssql/setup/target.py`

| Protected Database | Reason |
|--------------------|--------|
| master | System database |
| model | System database |
| msdb | System database |
| tempdb | System database |
| MigrationSource_MSSQL | Production migration source |
| MigrationTarget_MSSQL | Production migration target |

### MySQL — `tests/e2e/mysql/setup/target.py`

| Protected Database | Reason |
|--------------------|--------|
| mysql | System schema |
| information_schema | System schema |
| performance_schema | System schema |
| sys | System schema |
| migrationsource_mysql | Production migration source |
| migrationtarget_mysql | Production migration target |
| MigrationSource_MySQL | Production migration source (PascalCase) |
| MigrationTarget_MySQL | Production migration target (PascalCase) |

### PostgreSQL — `tests/e2e/postgresql/setup/target.py`

| Protected Database | Reason |
|--------------------|--------|
| postgres | System database |
| template0 | System database |
| template1 | System database |
| migration_source | Production migration source |
| migration_target | Production migration target |
| MigrationSource_PostgreSQL | Production migration source (PascalCase) |
| MigrationTarget_PostgreSQL | Production migration target (PascalCase) |
| migrationtarget_postgresql | Production migration target (lowercase) |

## Safe Database Pattern

In addition to the protected list, the framework enforces a safe-name pattern
to ensure only E2E databases can be reset:

| Engine | Pattern |
|--------|---------|
| MSSQL | `^MigrationE2E_MSSQL_.+$` |
| MySQL | `^MigrationE2E_MySQL_.+$` |
| PostgreSQL | `^MigrationE2E_PostgreSQL_.+$` |

Both checks must pass — a database must match the safe pattern AND not be in
the protected list. This is a defense-in-depth approach.

## Credential Handling

### Environment Variables

Passwords are resolved from environment variables via the `SECRET_` prefix
mechanism in `core/secrets/env.py`. Config files use bare names (e.g.
`mssql_source_pass`); the framework prepends `SECRET_` at runtime.

```python
# config.yaml (bare name, no password value)
source:
  connection:
    password_secret: mssql_source_pass
```

```bash
# Runtime (never committed)
export SECRET_mssql_source_pass="*****"
```

### No Password in Reports

Reports written to `tests/e2e/<engine>/reports/` contain:
- Database names
- Row counts
- Check results (PASS/FAIL)
- Object metadata

Reports **never** include:
- Passwords
- Connection strings
- Environment variable values

### Acceptance Clean (shared)

The shared cleanup runner `tests/e2e/acceptance_clean.py` validates both
protected-database and safe-pattern checks before destroying any database:

```python
# Refuses to clean protected databases
if target_mod.is_protected_database(db_name):
    raise ValueError(f"Refusing to reset protected database {db_name!r}")

# Refuses non-E2E databases
if not is_safe_e2e_database(db_name, pattern):
    raise ValueError(f"Refusing to reset {db_name!r}: does not match E2E pattern.")
```

## Cleanup Procedure

### Full Cleanup (Mode 2)

After completing all E2E phases, destroy the test databases:

```bash
python tests/e2e/acceptance_clean.py --engine mssql --config config/mssql_e2e_acceptance.yaml
```

### Per-Engine Cleanup

```bash
# MSSQL
python tests/e2e/mssql/run_setup.py --config config/mssql_e2e_acceptance.yaml --cleanup

# MySQL
python tests/e2e/mysql/run_setup.py --config config/mysql_e2e_acceptance.yaml --cleanup

# PostgreSQL
python tests/e2e/postgresql/run_setup.py --config config/postgresql_e2e_acceptance.yaml --cleanup
```

### Cleanup Scope

Cleanup destroys:
1. The E2E source database (`MigrationE2E_<Engine>_Source`)
2. The E2E target database (`MigrationE2E_<Engine>_Target`)

Cleanup does **not** touch:
- System databases / schemas
- Production migration databases
- Any database not matching the safe pattern

## Security Checklist for Developers

- [ ] Passwords set via environment variables only (`SECRET_*`)
- [ ] No passwords in config files, source code, or reports
- [ ] `docker-compose.e2e.yaml` not committed to git
- [ ] Protected databases verified before running setup
- [ ] Safe pattern verified before running cleanup
- [ ] Reports scanned for credential leakage after validation

## Known Issues

### `is_protected_database` Import Fix

A previous bug was fixed in `tests/e2e/acceptance_clean.py`: the
`is_protected_database` function was being called from the `target` module but
was originally imported from the wrong location. The fix ensures the per-engine
`target.py` module's `is_protected_database` is used, which contains the correct
engine-specific protected database list.

### MSSQL Trigger Data During Migration

The `trg_Orders_Insert` trigger on MSSQL `Orders` fires during migration and
creates `OrderAudit` records on the target. This is expected behavior — the
validation's `row_counts` phase compares source vs target row counts (not
expected.py counts), so both sides must have the trigger enabled.