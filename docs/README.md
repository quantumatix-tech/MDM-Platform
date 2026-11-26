# Migration Platform Documentation

This directory contains project documentation organized by topic.

## Index

| Document | Description |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System architecture, connector contracts, 19-phase pipeline |
| [RUNBOOK.md](RUNBOOK.md) | Operational runbook: secret providers, migration modes, failure handling |
| [USER_GUIDE.md](USER_GUIDE.md) | Complete user guide: install, config reference, CLI, troubleshooting |
| [PHASE_0.md](PHASE_0.md) | Phase 0 stabilization: cross-engine type safety, error isolation, WAL safety |
| [postgresql/README.md](postgresql/README.md) | PostgreSQL documentation entry point |
| [mssql/README.md](mssql/README.md) | MSSQL documentation entry point |
| [postgresql/POSTGRESQL_LOCAL_AUDIT_PHASE_1.md](postgresql/POSTGRESQL_LOCAL_AUDIT_PHASE_1.md) | Final PostgreSQL local audit report |
| [postgresql/POSTGRESQL_OBJECT_SUPPORT_MATRIX.md](postgresql/POSTGRESQL_OBJECT_SUPPORT_MATRIX.md) | PostgreSQL object migration support matrix |
| [postgresql/POSTGRESQL_TEST_GUIDE.md](postgresql/POSTGRESQL_TEST_GUIDE.md) | PostgreSQL E2E test guide with exact commands |
| [postgresql/POSTGRESQL_LIMITATIONS.md](postgresql/POSTGRESQL_LIMITATIONS.md) | Current PostgreSQL migration limitations |
| [mssql/MSSQL_LOCAL_AUDIT.md](mssql/MSSQL_LOCAL_AUDIT.md) | MSSQL Local→Local final audit report |
| [mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md](mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md) | MSSQL object migration support matrix |
| [mssql/MSSQL_TEST_GUIDE.md](mssql/MSSQL_TEST_GUIDE.md) | MSSQL E2E test guide with exact commands |
| [mssql/MSSQL_LIMITATIONS.md](mssql/MSSQL_LIMITATIONS.md) | Current MSSQL migration limitations |
| [oracle/README.md](oracle/README.md) | Oracle documentation entry point |
| [oracle/ORACLE_SETUP_PLAN.md](oracle/ORACLE_SETUP_PLAN.md) | Portable Oracle owner/executor PLAN and approval-based APPLY setup |
| [oracle/ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md](oracle/ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md) | Practical fresh Oracle PDB onboarding, interactive setup, runtime secrets, and normal local-to-local FULL workflow |
| [oracle/ORACLE_FULL_MIGRATION.md](oracle/ORACLE_FULL_MIGRATION.md) | Oracle FULL migration and repeat managed-state overwrite behavior |
| [oracle/ORACLE_OBJECT_SUPPORT_MATRIX.md](oracle/ORACLE_OBJECT_SUPPORT_MATRIX.md) | Oracle capability boundaries and evidence classification |
| [oracle/ORACLE_TEST_GUIDE.md](oracle/ORACLE_TEST_GUIDE.md) | Safe unit, integration, and Oracle E2E test commands |
| [oracle/ORACLE_POPULATED_SCHEMA_RUNBOOK.md](oracle/ORACLE_POPULATED_SCHEMA_RUNBOOK.md) | Existing populated SOURCE/TARGET workflow |

## Oracle local-to-local quick start

Use the [practical Oracle local-to-local runbook](oracle/ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md)
for the tested environment example, ordered PDB setup, exact interactive
prompts, secret entry, command counts, and live-status boundaries. It follows
the normal migration YAML and the existing setup planner; PDB creation is
manual DBA work, while reviewed owner/executor provisioning is performed by
the approval-based APPLY command. For planner behavior and security details,
see [Oracle setup PLAN and APPLY](oracle/ORACLE_SETUP_PLAN.md).

The final corrected local acceptance includes FULL run
`bfd97adbdcd14347a5108d8ef6792e72` and repeat FULL run
`f79b3095da5e406e92e75416a3f1fc8e`. Both report five tables, 50 rows, zero
migration errors, and `managed_object_status_validation.status = verified`.
The supplied independent read-only scan found no missing or mismatched SOURCE
objects, INVALID TARGET objects, or TARGET compilation errors. See the
[Oracle runbook](oracle/ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md) for scope and limits.
