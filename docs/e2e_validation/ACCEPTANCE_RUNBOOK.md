# Acceptance E2E Runbook (Mode 2)

## Overview

The **acceptance E2E** workflow (Mode 2) validates the real migration platform
end-to-end by:

1. **Setting up** deterministic source and clean target databases with fixtures.
2. **Running the actual migration** via the migration platform CLI.
3. **Validating** that the target database matches the source structurally and
   functionally.

This is the acceptance-gate test — distinct from the **full E2E** (Mode 1)
which performs migration internally with built-in phase runners.

## Prerequisites

### Docker services

Three containerized database instances must be running. The E2E compose file is
local to the developer environment (not committed to the repo). Use the test
compose for reference:

```yaml
services:
  mssql-e2e:
    image: mcr.microsoft.com/mssql/server:2022-latest
    ports: ["1533:1433"]
    environment:
      ACCEPT_EULA: "Y"
      SA_PASSWORD: "<set via runtime env, never commit>"
```

For MySQL and PostgreSQL, see `docker/docker-compose.test.yml` for reference
ports and environment variables.

### Environment variables (credentials)

**All passwords must be supplied via runtime environment variables only.**
Never commit passwords to source files, configs, or reports.

| Engine        | Secret env var                      | Value |
|---------------|-------------------------------------|-------|
| MSSQL         | `SECRET_mssql_e2e_source_pass`      | (runtime) |
| MSSQL         | `SECRET_mssql_e2e_target_pass`      | (runtime) |
| MySQL         | `SECRET_mysql_e2e_source_pass`      | (runtime) |
| PostgreSQL    | `SECRET_postgresql_e2e_source_pass` | (runtime) |

```bash
export SECRET_mssql_e2e_source_pass="<source_password>"
export SECRET_mssql_e2e_target_pass="<target_password>"
export SECRET_mysql_e2e_source_pass="<source_password>"
export SECRET_postgresql_e2e_source_pass="<source_password>"
```

### Connection parameters (live environment)

| Engine | Host | Port | User | Database (source/target) |
|--------|------|------|------|--------------------------|
| MSSQL | localhost | 1533 | sa | MigrationE2E_MSSQL_Source / MigrationE2E_MSSQL_Target |
| MySQL | localhost | 33062 | root | MigrationE2E_MySQL_Source / MigrationE2E_MySQL_Target |
| PostgreSQL | localhost | 55432 | postgres | MigrationE2E_PostgreSQL_Source / MigrationE2E_PostgreSQL_Target |

## Config files

Each engine has an acceptance config in `config/`:

| Engine | Config | Safe Pattern |
|--------|--------|-------------|
| MSSQL | `config/mssql_e2e_acceptance.yaml` | `^MigrationE2E_MSSQL_.+$` |
| MySQL | `config/mysql_e2e_acceptance.yaml` | `^MigrationE2E_MySQL_.+$` |
| PostgreSQL | `config/postgresql_e2e_acceptance.yaml` | `^MigrationE2E_PostgreSQL_.+$` |

## Runbook

### 1. Start database containers

```bash
docker compose -f docker-compose.e2e.yaml up -d
```

### 2. Set secret passwords

```bash
export SECRET_mssql_e2e_source_pass="*****"
export SECRET_mssql_e2e_target_pass="*****"
export SECRET_mysql_e2e_source_pass="*****"
export SECRET_postgresql_e2e_source_pass="*****"
```

### 3. Setup

Create deterministic source + clean target:

```bash
# MSSQL
python tests/e2e/mssql/run_setup.py --config config/mssql_e2e_acceptance.yaml

# MySQL
python tests/e2e/mysql/run_setup.py --config config/mysql_e2e_acceptance.yaml

# PostgreSQL
python tests/e2e/postgresql/run_setup.py --config config/postgresql_e2e_acceptance.yaml
```

Each runner prints a summary of tables created, rows inserted, and protected
databases.

### 4. Run migration (external CLI)

Use the migration platform CLI with the acceptance config:

```bash
python -m migration_platform --config config/mssql_e2e_acceptance.yaml --mode full
```

### 5. Validate

```bash
# MSSQL
python tests/e2e/mssql/run_validation.py --config config/mssql_e2e_acceptance.yaml

# MySQL
python tests/e2e/mysql/run_validation.py --config config/mysql_e2e_acceptance.yaml

# PostgreSQL
python tests/e2e/postgresql/run_validation.py --config config/postgresql_e2e_acceptance.yaml
```

Exit code 0 = all checks passed.

### 6. Cleanup

```bash
# Per-engine
python tests/e2e/<engine>/run_setup.py --config config/<engine>_e2e_acceptance.yaml --cleanup

# Or shared cleanup runner
python tests/e2e/acceptance_clean.py --engine mssql --config config/mssql_e2e_acceptance.yaml
```

## Safety

- Database names must match `^MigrationE2E_.+$` (configurable via `safe_db_pattern`).
- Protected databases (system catalogs, production migration DBs) are never dropped:
  - **MSSQL**: master, model, msdb, tempdb, MigrationSource_MSSQL, MigrationTarget_MSSQL
  - **MySQL**: mysql, information_schema, performance_schema, sys, MigrationSource_MySQL, MigrationTarget_MySQL
  - **PostgreSQL**: postgres, template0, template1, migration_source, migration_target, MigrationSource_PostgreSQL, MigrationTarget_PostgreSQL
- The `--cleanup` flag is destructive — it drops the E2E databases.
- Reports contain no credentials — passwords are never written to JSON output.

## Reports

JSON reports are written to `tests/e2e/<engine>/reports/`:

| Report | Description |
|--------|-------------|
| `acceptance_setup_<target_db>.json` | Setup verification report |
| `acceptance_validation_<target_db>.json` | Full validation report |

## Troubleshooting

### MSSQL: "Login failed for user 'sa'"

- Verify the container is running: `docker ps | findstr mssql`
- Verify the password matches the `SA_PASSWORD` set at container creation
- Verify port mapping: `1533:1433`

### MSSQL: "ODBC Driver 18 for SQL Server not found"

This driver is an OS component, not a Python package. Install:
- Windows: via SQL Server installer or Windows Update
- Linux: `sudo apt-install msodbcsql18`

### Trigger data mismatch (OrderAudit)

The `OrderAudit` table is populated by an insert trigger. If row counts differ
between source and target, verify the trigger was migrated and is enabled.

### Partition routing

PartitionedOrders must route rows to the correct partition. Verify partition
boundaries match between source and target schemas.

## Validation Phases

See [validation_matrix.md](validation_matrix.md) for the full phase-by-phase
check matrix across all three engines.

## Object Inventory

See [objects.md](objects.md) for the complete cross-engine object inventory.

## Fixture Reference

See [fixtures.md](fixtures.md) for the fixture script-by-script inventory.

## Cleanup & Security

See [cleanup_security.md](cleanup_security.md) for cleanup procedures and
security safeguards.