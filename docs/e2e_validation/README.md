# E2E Testing Framework

End-to-end testing for the Migration Platform, covering three database engines:
**MSSQL** (SQL Server 2022), **MySQL** (8.0), and **PostgreSQL** (15).

## Quick Start

```bash
# 1. Start containerized databases
docker compose -f docker-compose.e2e.yaml up -d

# 2. Set secret passwords
export SECRET_mssql_e2e_source_pass="<password>"
export SECRET_mssql_e2e_target_pass="<password>"
export SECRET_mysql_e2e_source_pass="<password>"
export SECRET_postgresql_e2e_source_pass="<password>"

# 3. Setup (source + target fixtures)
python tests/e2e/<engine>/run_setup.py --config config/<engine>_e2e_acceptance.yaml

# 4. Run migration (external CLI)
python -m migration_platform --config config/<engine>_e2e_acceptance.yaml --mode full

# 5. Validate
python tests/e2e/<engine>/run_validation.py --config config/<engine>_e2e_acceptance.yaml

# 6. Cleanup
python tests/e2e/acceptance_clean.py --engine <engine> --config config/<engine>_e2e_acceptance.yaml
```

## Modes

| Mode | Name | Description |
|------|------|-------------|
| Mode 1 | Full E2E | Internal phased runner — setup, migrate, validate in a single invocation |
| Mode 2 | Acceptance E2E | External migration — setup, then user runs the real migration CLI, then validate |

The acceptance E2E (Mode 2) is the acceptance-gate test. It validates the real
migration platform end-to-end by setting up deterministic source/target databases,
running the actual migration via the CLI, and then validating structural and
functional equivalence.

## E2E Configurations

| Engine | Config | Source DB | Target DB | Safe Pattern |
|--------|--------|-----------|-----------|--------------|
| MSSQL | `config/mssql_e2e_acceptance.yaml` | `MigrationE2E_MSSQL_Source` | `MigrationE2E_MSSQL_Target` | `^MigrationE2E_MSSQL_.+$` |
| MySQL | `config/mysql_e2e_acceptance.yaml` | `MigrationE2E_MySQL_Source` | `MigrationE2E_MySQL_Target` | `^MigrationE2E_MySQL_.+$` |
| PostgreSQL | `config/postgresql_e2e_acceptance.yaml` | `MigrationE2E_PostgreSQL_Source` | `MigrationE2E_PostgreSQL_Target` | `^MigrationE2E_PostgreSQL_.+$` |

## Status

### Mode 1 — Automated Regression E2E

| Engine | Checks | Status |
|--------|-------:|--------|
| MSSQL | 212/212 | PASS |
| PostgreSQL | 179/179 | PASS |
| MySQL | 151/151 | PASS |
| **Aggregate** | **542/542** | **PASS** |

### Mode 2 — Acceptance E2E (Live)

| Engine | Setup | Migration | Validation | Row Counts (S→T) | Status |
|--------|-------|-----------|------------|-------------------|--------|
| MSSQL | 7 tables, 28 rows | 33 events, 0 failures | 72/72 checks PASS | 28→28 (7/7) | PASS |
| PostgreSQL | 7 tables, 28 rows | PASS | All checks PASS | 28→28 (7/7) | PASS |
| MySQL | 7 tables, 28 rows | PASS | All checks PASS | 28→28 (7/7) | PASS |

> **Note on MSSQL row counts:** The migration reports 33 migration rows/events
> while source and target both hold 28 logical data rows. The difference (5)
> comes from the `trg_Orders_Insert` trigger on `Orders`, which fires during
> migration and creates 5 `OrderAudit` records on the target. Both source and
> target end with matching row counts, validated by the `row_counts` comparison
> phase.

## Documentation

| Document | Description |
|----------|-------------|
| [ACCEPTANCE_RUNBOOK.md](ACCEPTANCE_RUNBOOK.md) | Full runbook: setup, migration, validation, cleanup |
| [objects.md](objects.md) | Object test matrix: per-engine object coverage |
| [fixtures.md](fixtures.md) | Fixture inventory: scripts, schemas, and row counts |
| [validation_matrix.md](validation_matrix.md) | Validation checks: what is verified and how |
| [cleanup_security.md](cleanup_security.md) | Cleanup procedure and security safeguards |

## Layout

```
tests/e2e/
├── acceptance/              # Shared Mode 2 utilities (E2EConfig, report writer)
├── acceptance_clean.py      # Shared cleanup runner
├── mssql/
│   ├── fixtures/            # 16 deterministic SQL scripts
│   ├── setup/               # Database reset, safe-pattern guard, cleanup
│   ├── validation/
│   │   ├── expected.py      # Frozen expected state (source of truth)
│   │   └── models.py        # CheckResult, PhaseResult, ValidationReport
│   ├── run_setup.py
│   ├── run_validation.py
│   └── reports/
├── mysql/                   # Same structure
└── postgresql/              # Same structure
```

## Fixture Overview

Each engine ships an identical logical schema with 7 tables, 28 source rows:

| Table | Rows | Description |
|-------|------|-------------|
| Customers | 5 | Customer master with identity PK |
| Products | 5 | Product catalog with SKU |
| Orders | 5 | Customer orders (FK → Customers) |
| OrderDetails | 8 | Order line items (FK → Orders, Products) |
| OrderAudit | 5 | Audit log (trigger-populated) |
| PK_Name_Test | 2 | Primary key with custom constraint name |
| PartitionedOrders | 3 | Partitioned table (4 partitions) |

## Safety

- Database names must match `^MigrationE2E_.+$` (configurable via `safe_db_pattern`).
- Protected databases (system catalogs, production migration DBs) are never dropped.
- Reports contain no credentials — passwords are never written to JSON output.