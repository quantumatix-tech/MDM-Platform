# Phase 1 — PostgreSQL Full-Migration Preflight and Planning

## Objective

Phase 1 adds a read-only, PostgreSQL-to-PostgreSQL preflight and migration-planning layer to the existing full migration workflow. Its purpose is to expose connectivity, object, schema, and row-count risks before any migration DDL or DML begins.

## Scope

Implemented scope is limited to PostgreSQL-to-PostgreSQL **full** migrations:

- runtime PostgreSQL configuration validation;
- source and target connectivity preflight;
- source and target table discovery;
- source/target schema comparison and row-count capture;
- a structured per-object migration plan;
- dry-run support and preflight reporting; and
- normal full-run preflight gating.

CDC, WAL management, incremental/continuous migration, rollback/recovery, and unrelated database connectors are explicitly out of scope.

## Before Phase 1

The existing `run_full()` workflow connected to both systems and began migration phases directly. It had reusable source discovery, schema extraction, execution, validation, audit logging, and HTML/JSON reporting, but did not have an explicit read-only target discovery, schema-comparison, or per-object execution plan before creating tables or loading data.

## Design and Preflight Flow

`core/migration_plan.py` introduces the read-only `PostgresMigrationPlanner` and structured dataclasses:

- `MigrationPlan` records source/target connectivity, global warnings/blockers, and readiness.
- `ObjectMigrationPlan` records object/schema identity, existence, schema summaries/comparison, row counts, dependencies, action, warnings, blockers, and readiness.
- `SchemaComparison` records `COMPATIBLE`, `MISMATCH`, or `TARGET_MISSING` with precise differences.

For PostgreSQL-to-PostgreSQL full migrations, the orchestrator now performs:

1. secret resolution and existing schema-scope propagation;
2. configuration validation without including password values;
3. source connection and source table/schema discovery;
4. target connection and read-only target table/schema discovery;
5. source/target row-count capture and schema comparison;
6. deterministic per-object plan creation; then
7. normal existing full-migration execution only when the plan is ready.

If configuration, connectivity, discovery, counting, or schema compatibility is blocked, full execution returns `blocked` before `ensure_database_exists`, table creation, or data loading.

Foreign-key target-table names are retained in sorted dependency lists as a deterministic ordering foundation. Phase 1 does not add a new dependency-execution engine.

## Migration Plan Behavior

Per-object decisions are explicit and testable:

| Condition | Action |
| --- | --- |
| Target table is absent and source inspection/count succeeds | `CREATE_AND_MIGRATE` |
| Target table exists with a compatible compared schema | `MIGRATE` |
| Schema differs, metadata/count inspection fails, or another object blocker is present | `BLOCK` |

Existing target rows are a warning, not a silent condition; the plan states that the existing upsert behavior will be used. Schema comparison detects missing and unexpected columns plus type, nullability, default, generated-expression, and primary-key differences.

## Configuration and Dry Run

`migration.dry_run` is added to the existing configuration schema with a default of `false`. The CLI also supports:

```powershell
python -m migration_platform --config config/postgresql_local_test.yaml --dry-run
```

`--dry-run` is accepted only for full migration mode. It runs the same PostgreSQL preflight/planning flow and writes the normal HTML/JSON reports, but does not call `ensure_database_exists`, `create_object_if_missing`, `upsert_batch`, or other migration write operations. Unit tests directly assert that those write methods are not called.

For a normal PostgreSQL full run, the execution order is now preflight → migration plan → existing full migration → existing validation → final report. Non-PostgreSQL full runs retain their existing behavior; CDC paths were not changed.

## Reporting

The JSON report now includes `preflight`. The HTML report adds a PostgreSQL Migration Plan section showing connection state, global blockers, object/schema, comparison status, source/target row counts, selected action, and warnings or blockers. A focused test confirms both report formats expose the plan.

## Files Changed

### Implementation

- `config/migration_config.schema.yaml`
- `core/migration_plan.py`
- `core/connectors/postgresql.py`
- `core/orchestrator.py`
- `core/reporting/report_builder.py`
- `migration_platform/__main__.py`

### Tests

- `tests/unit/test_migration_plan.py`
- `tests/integration/test_connectors.py`

### Documentation

- `docs/PHASE_1.md`

`config/postgresql_local_test.yaml` was already an uncommitted local change before this Phase 1 work and was preserved without modification.

## Test Evidence

Executed commands and results:

| Command | Result |
| --- | --- |
| `python -m pytest tests/unit/test_migration_plan.py -q` | `8 passed` |
| `python -m pytest tests/unit -q` | `62 passed` |
| `python -m compileall core migration_platform -q` | passed (no output) |
| `python -m pytest tests/integration/test_connectors.py::TestPostgresFullMigration -q` | `4 skipped` because PostgreSQL integration password environment variables were not set |
| `python -m pytest tests/integration -q -x` | began with four PostgreSQL skips and did not produce a completion summary in the local execution window; the next local MongoDB service is unreachable on port `27017` |

`python -m ruff check ...` was attempted but could not run because the local Python environment does not have the `ruff` module installed. No dependency files were changed.

Focused unit coverage includes configuration validation, source connectivity failure, missing target detection, compatible schema, schema mismatch, row counts, action decisions, dependency ordering, warning/blocker handling, dry-run write safety, and HTML/JSON reporting.

### End-to-End Verification Attempt

A subsequent Phase 1 verification run executed the required unit commands again with the same successful results:

| Command | Result |
| --- | --- |
| `python -m pytest tests/unit/test_migration_plan.py -q` | `8 passed` |
| `python -m pytest tests/unit -q` | `62 passed` |
| `python -m pytest tests/integration/test_connectors.py::TestPostgresFullMigration -q` | `4 skipped` |

The verification subprocess could not access either application secret variable or either integration password variable from its environment. The integration test fixture therefore skipped safely. No password values were requested, printed, copied to files, or otherwise exposed.

## Integration Environment and Prerequisites

PostgreSQL integration configuration is now portable through these non-secret environment variables:

- `INTEGRATION_PG_HOST` (default `127.0.0.1`)
- `INTEGRATION_PG_PORT` (default `5432`)
- `INTEGRATION_PG_SOURCE_DATABASE` (default `migration_test`)
- `INTEGRATION_PG_TARGET_DATABASE` (default `migration_target`)
- `INTEGRATION_PG_USERNAME` (default `postgres`)
- `INTEGRATION_PG_SSL` (default `false`)
- `INTEGRATION_PG_SOURCE_PASSWORD` (required; no default)
- `INTEGRATION_PG_TARGET_PASSWORD` (required for target/write tests; no default)
- `INTEGRATION_PG_SAFE_WRITE_TESTS=true` (required to opt in to the existing integration write tests)

At verification time, the agent's subprocess environment did not contain `SECRET_source_db_pass` or `SECRET_target_db_pass`, so it could not safely set `INTEGRATION_PG_SOURCE_PASSWORD` or `INTEGRATION_PG_TARGET_PASSWORD`. Therefore no PostgreSQL database schema, tables, row counts, dry-run report, or full migration were manually inspected or executed. No credentials, infrastructure, databases, tables, or data were modified to bypass this prerequisite.

The complete integration suite also cannot complete locally because MongoDB is unreachable on `127.0.0.1:27017`; this is outside Phase 1's PostgreSQL-only scope. The observed PostgreSQL integration blocker is missing non-secret environment-variable configuration for credentials, not a claimed migration failure.

## Manual End-to-End Verification

Not executed. The verification subprocess could not access the required PostgreSQL secret environment variables, and the explicit safe-write opt-in was absent. Consequently, there is no claim of source/target state inspection, dry-run database-change verification, actual full migration, source/target count validation, or generated local report inspection.

After the prerequisites are supplied, use isolated source/target test tables and run:

```powershell
python -m migration_platform --config config/postgresql_local_test.yaml --dry-run
python -m migration_platform --config config/postgresql_local_test.yaml --mode full
```

Inspect source and target schema/counts before and after each command, and inspect the generated paths returned under `result["report"]`.

## Intentional Non-Changes

- CDC implementation and tests were not redesigned, implemented, or changed.
- WAL behavior was not changed.
- Incremental and continuous migration behavior was not changed.
- Rollback, recovery, checkpoints, and a dependency-execution engine were not implemented.
- Unrelated database connectors were not modified.
- Existing Phase 0 behavior and existing uncommitted work were preserved.

## Final Status

Phase 1 implementation is complete based on the available unit-test evidence: a PostgreSQL full-migration preflight/planning layer, dry-run path, full-run gate, and report visibility are implemented and covered by `62` passing unit tests.

Manual PostgreSQL end-to-end validation and full integration completion remain blocked by missing non-secret integration password environment variables and the explicit safe-write opt-in. The next recommended step is to provide those variables for isolated PostgreSQL test databases/tables, perform the documented dry-run and full-run verification, and review the generated reports before proceeding to the next roadmap phase.

No commit, push, branch switch, reset, stash, revert, or history change was performed.
