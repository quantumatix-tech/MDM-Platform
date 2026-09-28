# MSSQL Documentation

MSSQL-specific documentation for the Migration Platform.

## Documents

| Document | Description |
|---|---|
| [MSSQL_E2E_RUNBOOK.md](MSSQL_E2E_RUNBOOK.md) | Reusable E2E runbook/demo guide: prerequisites, config, migration command, validation |
| [MSSQL_MIGRATION_FLOW.md](MSSQL_MIGRATION_FLOW.md) | Complete migration flow, three E2E directions, and issues found and fixed |
| [MSSQL_OBJECT_SUPPORT_MATRIX.md](MSSQL_OBJECT_SUPPORT_MATRIX.md) | Object-by-object classification: support level, E2E validation status, and known limitations |
| [MSSQL_LOCAL_AUDIT.md](MSSQL_LOCAL_AUDIT.md) | Local→Local audit report: environment, evidence, cross-schema FK, regression counts, commits |
| [MSSQL_LIMITATIONS.md](MSSQL_LIMITATIONS.md) | Documented limitations: implementation, audit scope, and environment blockers |
| [MSSQL_TEST_GUIDE.md](MSSQL_TEST_GUIDE.md) | Reusable E2E test guide: reset fixture, run migration, verify results (with schema placeholders) |

## Migration Direction Status

| Direction | Status |
|---|---|
| **Local → Local** | COMPLETED — See [MSSQL_LOCAL_AUDIT.md](MSSQL_LOCAL_AUDIT.md) |
| **Local → Cloud** | COMPLETED — See [MSSQL_CLOUD_AUDIT.md](MSSQL_CLOUD_AUDIT.md) |
| **Cloud → Local** | COMPLETED / VALIDATED — See [MSSQL_CLOUD_AUDIT.md](MSSQL_CLOUD_AUDIT.md) |

## Quick links

- Local E2E config: `config/mssql_local_test.yaml`
- Local → Cloud config: `config/mssql_local_cloud.yaml`
- Cloud → Local config: `config/mssql_cloud_local.yaml`
- MSSQL connector: `core/connectors/mssql.py`

## Running a local E2E test (Local → Local)

```powershell
# 1. Set secrets
$env:SECRET_mssql_source_pass = "<source_password>"
$env:SECRET_mssql_target_pass = "<target_password>"

# 2. Run migration
python -m migration_platform --config config/mssql_local_test.yaml --mode full --no-live-ui

# 3. Verify (see MSSQL_TEST_GUIDE.md for verification queries)
```

## Running other E2E directions

- **Local → Cloud**: Configure `config/mssql_local_cloud.yaml` with Azure SQL target details. Set `SECRET_mssql_source_pass` and `SECRET_mssql_target_pass`. Results will be recorded in `MSSQL_CLOUD_AUDIT.md` upon completion.
- **Cloud → Local**: Configure `config/mssql_cloud_local.yaml` (reusable) with Azure SQL source and local target details. Results are recorded in `MSSQL_CLOUD_AUDIT.md`.

## Reference

- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- Limitations: `docs/mssql/MSSQL_LIMITATIONS.md`
- Migration flow: `docs/mssql/MSSQL_MIGRATION_FLOW.md`
- Object support matrix: `docs/mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md`
- E2E runbook: `docs/mssql/MSSQL_E2E_RUNBOOK.md`
- Test guide: `docs/mssql/MSSQL_TEST_GUIDE.md`
