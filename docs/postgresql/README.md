# PostgreSQL Documentation

PostgreSQL-specific documentation for the Migration Platform.

## Documents

| Document | Description |
|---|---|
| [POSTGRESQL_LOCAL_AUDIT_PHASE_1.md](POSTGRESQL_LOCAL_AUDIT_PHASE_1.md) | Final local audit report — 100 unit tests, full E2E evidence, cross-schema FK fix history, environment blockers, reproduction instructions |
| [POSTGRESQL_CLOUD_AUDIT.md](POSTGRESQL_CLOUD_AUDIT.md) | **Local → Azure E2E evidence** — role creation, sequence USAGE+SELECT, grants, RLS, all objects verified on Azure PostgreSQL |
| [POSTGRESQL_OBJECT_SUPPORT_MATRIX.md](POSTGRESQL_OBJECT_SUPPORT_MATRIX.md) | Object-by-object classification: Supported, Partial, Out of Scope, Environment Blocked |
| [POSTGRESQL_TEST_GUIDE.md](POSTGRESQL_TEST_GUIDE.md) | Reusable E2E test guide: reset fixture, run migration, verify results (with schema placeholders) |
| [POSTGRESQL_LIMITATIONS.md](POSTGRESQL_LIMITATIONS.md) | Documented limitations: implementation, audit scope, and environment blockers |

## Quick links

- Fixture source: `tests/fixtures/postgresql_e2e/`
- Local E2E config: `config/postgresql_object_e2e.yaml`
- Cloud E2E config: `config/postgresql_cloud_test.yaml`
- PostgreSQL connector: `core/connectors/postgresql.py`
- Integration tests: `tests/integration/test_connectors.py`

## Running the Local E2E test

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

## Running the Cloud E2E test (Local → Azure)

```bash
# 1. Set secrets for both local and Azure
export SECRET_source_db_pass=<local_password>
export SECRET_target_db_pass=<azure_password>

# 2. Configure config/postgresql_cloud_test.yaml with your Azure server details

# 3. Reset local source fixture
bash tests/fixtures/postgresql_e2e/reset_source.sh

# 4. Run migration
python -m migration_platform --config config/postgresql_cloud_test.yaml --mode full --no-live-ui

# 5. Verify on Azure target (see POSTGRESQL_CLOUD_AUDIT.md Section 6)
```

See [POSTGRESQL_TEST_GUIDE.md](POSTGRESQL_TEST_GUIDE.md) for the complete
reusable test sequence with schema placeholders.