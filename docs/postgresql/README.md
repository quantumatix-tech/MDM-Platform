# PostgreSQL Documentation

PostgreSQL-specific documentation for the Migration Platform.

## Documents

| Document | Description |
|---|---|
| [POSTGRESQL_MIGRATION_FLOW.md](POSTGRESQL_MIGRATION_FLOW.md) | Complete E2E migration flow (conceptual), three verified E2E directions (Localâ†’Local, Localâ†’Cloud, Cloudâ†’Local), and E2E issues found and fixed |
| [POSTGRESQL_E2E_RUNBOOK.md](POSTGRESQL_E2E_RUNBOOK.md) | Reusable E2E runbook/demo guide: 10-step demo flow, verification queries, and demo scenario |
| [POSTGRESQL_OBJECT_SUPPORT_MATRIX.md](POSTGRESQL_OBJECT_SUPPORT_MATRIX.md) | Object-by-object classification: support level, E2E validation status, and known limitations |
| [POSTGRESQL_LOCAL_AUDIT_PHASE_1.md](POSTGRESQL_LOCAL_AUDIT_PHASE_1.md) | Localâ†’Local audit report â€” 100 unit tests, E2E evidence, cross-schema FK fix history, environment blockers, reproduction instructions |
| [POSTGRESQL_CLOUD_AUDIT.md](POSTGRESQL_CLOUD_AUDIT.md) | **Local â†’ Azure E2E evidence** â€” role creation, sequence USAGE+SELECT, grants, RLS, all objects verified on Azure PostgreSQL |
| [POSTGRESQL_TEST_GUIDE.md](POSTGRESQL_TEST_GUIDE.md) | Reusable E2E test guide: reset fixture, run migration, verify results (with schema placeholders) |
| [POSTGRESQL_LIMITATIONS.md](POSTGRESQL_LIMITATIONS.md) | Documented limitations: implementation, audit scope, and environment blockers |

## Quick links

- Fixture source: `tests/fixtures/postgresql_e2e/`
- Local E2E config: `config/postgresql_object_e2e.yaml`
- Cloud E2E config: `config/postgresql_cloud_test.yaml`
- Cloudâ†’Local config: `config/postgresql_cloud_to_local.yaml`
- PostgreSQL connector: `core/connectors/postgresql.py`
- Integration tests: `tests/integration/test_connectors.py`

## Running a local E2E test (Local â†’ Local)

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

## Running the Cloud E2E test (Local â†’ Azure)

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

## Running a Cloud â†’ Local E2E test

```bash
# 1. Set secrets
export SECRET_source_db_pass=<cloud_password>
export SECRET_target_db_pass=<local_password>

# 2. Configure config/postgresql_cloud_to_local.yaml with your Azure source and local target details

# 3. Reset target database if needed

# 4. Run migration
python -m migration_platform --config config/postgresql_cloud_to_local.yaml --mode full --no-live-ui

# 5. Verify on local target (see POSTGRESQL_MIGRATION_FLOW.md Section 2C)
```

## E2E Test Guide

See [POSTGRESQL_TEST_GUIDE.md](POSTGRESQL_TEST_GUIDE.md) for the complete reusable test sequence with schema placeholders, including source preparation, migration execution, and manual verification queries.

## E2E Runbook / Demo Guide

See [POSTGRESQL_E2E_RUNBOOK.md](POSTGRESQL_E2E_RUNBOOK.md) for the 10-step demo flow: prepare databases, verify source, run migration, read report, verify target, and clean up. Includes a practical demo scenario for demonstrating E2E migration to stakeholders.

## E2E Migration Flow

See [POSTGRESQL_MIGRATION_FLOW.md](POSTGRESQL_MIGRATION_FLOW.md) for:

- The complete conceptual migration flow (source connection â†’ object discovery â†’ object creation â†’ data migration â†’ dependencies â†’ validation â†’ success)
- Object and dependency ordering
- All three verified E2E directions (Localâ†’Local, Localâ†’Cloud, Cloudâ†’Local) with results
- E2E issues discovered and fixed during testing

## Object Support

See [POSTGRESQL_OBJECT_SUPPORT_MATRIX.md](POSTGRESQL_OBJECT_SUPPORT_MATRIX.md) for object-by-object classification showing:

- Implementation support level (Supported/Partial/Out of Scope/Environment Blocked)
- E2E validation status (which E2E runs verified each object type)
- Known limitations per object type

## Cloud-Specific Audit

- **Local â†’ Azure:** See [POSTGRESQL_CLOUD_AUDIT.md](POSTGRESQL_CLOUD_AUDIT.md)

## Limitations

See [POSTGRESQL_LIMITATIONS.md](POSTGRESQL_LIMITATIONS.md) for documented implementation gaps, audit scope limitations, and environment blockers (CDC/WAL level, custom types in non-public schemas, partition support, trigger state, etc.).
