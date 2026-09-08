# PostgreSQL Documentation

PostgreSQL-specific documentation for the Migration Platform.

## Documents

| Document | Description |
|---|---|
| [POSTGRESQL_LOCAL_AUDIT_PHASE_1.md](POSTGRESQL_LOCAL_AUDIT_PHASE_1.md) | Final local audit report — 94 unit tests, full E2E evidence, environment blockers, reproduction instructions |
| [POSTGRESQL_OBJECT_SUPPORT_MATRIX.md](POSTGRESQL_OBJECT_SUPPORT_MATRIX.md) | Object-by-object classification: Supported, Partial, Out of Scope, Environment Blocked |
| [POSTGRESQL_TEST_GUIDE.md](POSTGRESQL_TEST_GUIDE.md) | Step-by-step E2E test guide: reset fixture, run migration, verify results |
| [POSTGRESQL_LIMITATIONS.md](POSTGRESQL_LIMITATIONS.md) | Documented limitations: implementation, audit scope, and environment blockers |

## Quick links

- Fixture source: `tests/fixtures/postgresql_e2e/`
- E2E config: `config/postgresql_object_e2e.yaml`
- PostgreSQL connector: `core/connectors/postgresql.py`
- Integration tests: `tests/integration/test_connectors.py`

## Running the E2E test

```bash
# 1. Set secrets
export SECRET_source_db_pass=postgres
export SECRET_target_db_pass=postgres

# 2. Reset source fixture
bash tests/fixtures/postgresql_e2e/reset_source.sh

# 3. Run migration
python -m migration_platform --config config/postgresql_object_e2e.yaml --mode full --no-live-ui

# 4. Verify
python tests/fixtures/postgresql_e2e/verify_migration.py
```

See [POSTGRESQL_TEST_GUIDE.md](POSTGRESQL_TEST_GUIDE.md) for the complete sequence.
