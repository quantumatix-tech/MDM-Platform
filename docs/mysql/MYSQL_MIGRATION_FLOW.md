# MySQL E2E Migration Flow

This document summarizes how the platform discovers, migrates, validates, and reports MySQL data and objects. It describes the current FULL-mode path and the direction-specific E2E evidence available in the repository. For executable test steps, object status, and detailed limitations, see the linked guides below.

## Part 1 — Conceptual Migration Flow

```text
MySQL source
    ↓
Load configuration and resolve secrets
    ↓
Connect to source and target
    ↓
Snapshot MySQL Events and stored-object DEFINER dependencies
    ↓
Prepare configured target database
    ↓
Discover tables and MySQL metadata
    ↓
Create tables (partition definitions are part of MySQL table DDL)
    ↓
Optional supported partition-table reconciliation
    ↓
Clear migrated target tables and load source rows in batches
    ↓
Apply indexes / keys / constraints; synchronize AUTO_INCREMENT
    ↓
Create views → functions/procedures → triggers → events
    ↓
Apply comments → users/security principals → direct grants
    ↓
Validate counts and partition metadata; build object results
    ↓
Write audit log and JSON/HTML reports
    ↓
SUCCESS / PARTIAL_SUCCESS / FAILED
```

The diagram shows the MySQL FULL path. Some operations are conditional on source objects, configuration, and privileges. In particular, Events are snapshotted near connection time because a one-time Event can run while migration is in progress.

## Part 2 — MySQL Object and Dependency Ordering

The MySQL path in `MigrationOrchestrator.run_full()` follows this high-level order:

1. Resolve `password_secret` values and connect to source and target.
2. Snapshot source Events; for MySQL → MySQL, snapshot routines and triggers and prepare required DEFINER accounts before stored objects.
3. Ensure the configured target database exists; discover source tables and metadata.
4. Create ordinary target tables. Partitioned table definitions are emitted with their table DDL. If enabled, supported MySQL partition mismatches may be reconciled and verified.
5. Suspend matching existing target triggers for a FULL data reload. Clear rows in the migrated target tables, then load source rows in batches in parent-before-child foreign-key order.
6. Apply indexes and constraints, including foreign keys and CHECK constraints; synchronize table-bound `AUTO_INCREMENT` values after loading explicit keys.
7. Create views, functions/procedures, triggers, and Events. Triggers are recreated after the data load; Events use the source-start snapshot.
8. Apply table/column comments, user/security-principal operations, and direct grants.
9. Build object-migration counts, run configured validation, finalize staged reconciliation if validation succeeds, and determine final status.

Referenced tables must exist before foreign keys are applied. Parent data is loaded before child data. Views and stored objects follow their referenced tables. Function/trigger creation may be affected by target MySQL binary-log policy; Event creation is separate from Event Scheduler runtime.

MySQL uses the configured **database** as the object namespace. The connector reads metadata for the configured source database and writes to the configured target database; it does not treat MySQL as having PostgreSQL-style independent schemas.

## Part 3 — E2E Migration Directions

| Direction | Description | Status / Evidence |
|---|---|---|
| Local → Local | Local MySQL → local MySQL | A checked-in report records a successful 5-table / 5,710-row run. An earlier report provides broad 11-table / 44-row evidence, detailed in Part 4. |
| Local → Cloud | Local MySQL → Azure MySQL | Successful direction-specific run `9f15eae2f1a84933a7ffe9746b828932`; 6 tables, 6,340 rows, 0 failed. |
| Cloud → Local | Azure MySQL → local MySQL | Successful direction-specific run `fb374e4480d84894b22d5917807b507f`; 13 tables, 45 rows, 0 failed. |

These runs use different fixtures. Do not use one direction's counts or object coverage as proof for another direction.

## Part 4 — Local → Local

The checked-in report `reports/1584f5a5ebc04e8a8d9e288ee150b23c.json` records MySQL → MySQL FULL status `success`, 5 tables, 5,710 source/migrated rows, 0 failed rows, and count validation matches. In that report, 73 columns and 5 primary keys migrated. Many other object categories had source count 0 and were not exercised in this run.

A separate earlier Local → Local report, `reports/74c3b8076a6244bf94104b997b2cf89d.json`, records FULL `success`: 11 tables, 44/44 rows, and 0 failed. Its object results record 78 columns, 11 primary keys, 9 `AUTO_INCREMENT` columns, 6 indexes, 3 unique constraints, 2 foreign keys, 4 CHECK constraints, 2 generated columns, 15 defaults, 3 partitions, 8 comments, 1 view, 2 functions, 2 procedures, 2 triggers, and 1 Event; grants had source count 0. The corresponding JSON, HTML, and JSONL artifacts are present. This Sep 16 report is separate from the later-dated Sep 29 five-table/5,710-row report above.

## Part 5 — Local → Azure

Run `9f15eae2f1a84933a7ffe9746b828932` is recorded in the report files and Local → Azure audit:

| Metric | Result |
|---|---:|
| Mode / status | FULL / SUCCESS |
| Tables | 6 |
| Source / migrated rows | 6,340 / 6,340 |
| Failed rows | 0 |
| Success | 100% |
| Duration | about 355.7 seconds |
| Columns / primary keys | 66 / 6 |
| Grants / security principals | 18 / 3 |

This fixture's report has zero source counts for multiple object categories. Those categories were not exercised by this run; this does not establish that they are unsupported. Azure-specific Event runtime was not verified where the target Event Scheduler was OFF.

## Part 6 — Cloud → Local

Run `fb374e4480d84894b22d5917807b507f` is recorded in `reports/`, `logs/`, and the audit/migration-flow documentation:

| Metric | Result |
|---|---:|
| Mode / status | FULL / SUCCESS |
| Tables | 13 |
| Source / migrated rows | 45 / 45 |
| Failed rows | 0 |
| Success | 100% |
| Duration | about 15.7 seconds |

The report's object counts include 65 columns, 13 primary keys, 9 `AUTO_INCREMENT` columns, 13 indexes, 3 unique constraints, 3 foreign keys, 6 CHECK constraints, 2 generated columns, 13 defaults, 14 partitions, 17 comments, 1 grant, 12 security principals, 2 views, 2 functions, 2 procedures, 2 triggers, and 2 Events.

This is broad evidence for the specific Cloud → Local fixture, not exhaustive proof of every MySQL feature variation or Event runtime behavior.

## Part 7 — Object Discovery Summary

The MySQL connector uses MySQL metadata and DDL sources, including:

| Metadata | Use |
|---|---|
| `INFORMATION_SCHEMA.TABLES` | Table inventory, engine/options, table comments |
| `INFORMATION_SCHEMA.COLUMNS` | Types, defaults, nullability, generated expressions, `AUTO_INCREMENT`, column comments |
| `INFORMATION_SCHEMA.KEY_COLUMN_USAGE` | Primary/foreign-key columns and referenced database/table/column |
| `INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS` | Foreign-key update/delete rules |
| `INFORMATION_SCHEMA.STATISTICS` | Index names, columns, order, uniqueness, and type |
| `INFORMATION_SCHEMA.TABLE_CONSTRAINTS` / `CHECK_CONSTRAINTS` | Constraint types and CHECK expressions |
| `INFORMATION_SCHEMA.VIEWS` | View discovery/definitions |
| `INFORMATION_SCHEMA.ROUTINES` + `SHOW CREATE` | Function/procedure discovery and full DDL |
| `INFORMATION_SCHEMA.TRIGGERS` + `SHOW CREATE TRIGGER` | Trigger discovery and DDL |
| `INFORMATION_SCHEMA.EVENTS` + `SHOW CREATE EVENT` | Event definition, status, schedule, time zone, and definer |
| `INFORMATION_SCHEMA.PARTITIONS` | MySQL partition method, expression, names, and boundaries |
| MySQL privilege metadata | Supported database/table/column/routine/global direct-grant discovery, subject to visibility |

Catalog visibility is account-dependent. `SHOW CREATE` supplies stored-object DDL; metadata discovery alone does not demonstrate functional runtime.

## Part 8 — MySQL Migration Execution Chain

```text
python -m migration_platform
        ↓
migration_platform/__main__.py
        ↓
Load YAML config; resolve CLI/options and select connectors
        ↓
Resolve source/target secrets and connect
        ↓
core/connectors/mysql.py ↔ MySQL metadata and DDL/data
        ↓
core/orchestrator.py coordinates phases and object results
        ↓
Validation + audit logging + JSON/HTML report generation
```

- **CLI/configuration:** selects source/target engines, migration mode, MySQL account allowlist, reconciliation, batch size, validation, retry, secrets, and logging options.
- **MySQL connector:** implements MySQL connections, catalog discovery, `SHOW CREATE` extraction, target DDL, row loading, grant operations, and MySQL-specific handling.
- **Orchestrator:** orders dependencies, coordinates phases, records per-object outcomes, validates, chooses final status, and closes connectors after the run.
- **Reporting:** the CLI prints run ID, mode, duration, audit log and report paths. Files are written under `logs/<run_id>.jsonl` and `reports/<run_id>.json` / `.html` when generated.

## Part 9 — Data Migration and Reconciliation Behavior

In FULL mode, the MySQL target connector deletes rows from the known migrated tables before data loading. It uses FK-safe `DELETE` operations rather than `TRUNCATE`, and restores its connection-scoped foreign-key checks afterward. Existing matching triggers are suspended around the reload and recreated later. Rows are loaded in batches after ordering tables parent-before-child; the batch DML uses MySQL `INSERT ... ON DUPLICATE KEY UPDATE`.

FULL does not mean “drop every target object.” The path clears rows for migrated tables but retains unrelated target-only objects, including Events not in the source snapshot. A clean dedicated target is still useful for exact comparisons.

Generated-column values are excluded from ordinary row inserts so MySQL recalculates them. `AUTO_INCREMENT` is synchronized after explicit source IDs are loaded. Reconciliation is opt-in and narrowly implemented for supported MySQL partitioned-table mismatches; it stages and verifies a replacement and retains a backup until validation succeeds. It is not a general schema-diff repair.

## Part 10 — Error Isolation

`migration.stop_on_error` defaults to `false`. At implemented object-level failure points, an object failure is recorded and eligible independent work can continue; the run may finish as `partial_success`. Setting it to `true` enables fail-fast behavior at the relevant table/data error points. Dependency failures can affect dependent work, and not every phase has identical isolation semantics.

The report distinguishes migrated, blocked, skipped, failed, and unsupported counts where applicable. A zero source count is not reported as proof of unsupported capability. See [MYSQL_LIMITATIONS.md](MYSQL_LIMITATIONS.md) and [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md) for scope.

## Part 11 — MySQL-Specific Safety and Policy Behavior

### Cross-engine type safety

For MySQL targets, same-engine MySQL columns can retain their MySQL source type. For cross-engine inputs, a target mapping must be supplied/resolved; a missing target type raises `UnmappedTypeError` with table, column, source type, and engine context instead of silently emitting source-native DDL. This guard does not mean every cross-engine type has a mapping.

### Functions and triggers

If `log_bin=ON` and `log_bin_trust_function_creators=OFF`, MySQL can reject function or trigger creation with Error 1419, depending on privileges. The platform reports the affected object as blocked where recognized; it does not change the server-global setting.

### Events and DEFINER

Events are discovered and snapshotted before table/data phases, then recreated after triggers. The one-time Event safety window defaults to 300 seconds: an enabled one-time Event due, past due, or too close to its scheduled time is blocked rather than shifted or re-enabled. MySQL-to-MySQL stored objects use the configured/current target DEFINER handling; account availability and target authority still apply.

Event DDL migration is separate from Event Scheduler runtime. Automatic execution depends on Scheduler state, event status/schedule/timing, definer, and privileges. The documented Azure target had `event_scheduler=OFF`; runtime there was not verified.

## Part 12 — Validation Flow

The configured validation runs after object phases and contributes to final status. For a practical MySQL review, compare source and target in this order:

1. Configured target database and table inventory.
2. Columns, MySQL types, defaults, generated expressions, and keys.
3. Indexes, CHECK constraints, and foreign keys.
4. Per-table source/target row counts and representative values.
5. `AUTO_INCREMENT` state and generated-column values where present.
6. Partition metadata and row counts for partitioned tables.
7. Views and stored routine definitions; execute representative routines separately when safe.
8. Trigger definitions and controlled trigger behavior.
9. Event definitions/status separately from scheduler-driven runtime.
10. Table/column comments and visible supported grants/security results.
11. Object failures, blocked/skipped results, validation status, and final run status.

The standard E2E configs use count validation. Validation and object-result summaries are included in the report; exact SQL and negative tests are in [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md).

## Part 13 — Current E2E Evidence Summary

| Direction | Run ID | Tables | Rows (source/migrated) | Failed | Result |
|---|---|---:|---:|---:|---|
| Local → Local (Sep 29 report) | `1584f5a5ebc04e8a8d9e288ee150b23c` | 5 | 5,710 / 5,710 | 0 | SUCCESS; many object categories not exercised |
| Local → Cloud | `9f15eae2f1a84933a7ffe9746b828932` | 6 | 6,340 / 6,340 | 0 | SUCCESS; direction-specific fixture |
| Cloud → Local | `fb374e4480d84894b22d5917807b507f` | 13 | 45 / 45 | 0 | SUCCESS; broad object fixture |
| Local → Local (Sep 16 report) | `74c3b8076a6244bf94104b997b2cf89d` | 11 | 44 / 44 | 0 | SUCCESS; report and audit artifacts available; earlier run |

Interpretation rules:

- Not Exercised ≠ Unsupported.
- Environment Blocked ≠ Implementation Failure.
- Unit Tested ≠ E2E Verified.
- Successful FULL migration ≠ exhaustive object-support proof.

## Part 14 — E2E Issues and Resolved Findings

The MySQL audits record fixes now represented in the connector/orchestrator and tests:

- Cross-engine missing type mappings now raise `UnmappedTypeError` instead of passing through source-native types.
- Generated-column values are omitted from normal inserts; MySQL computes them.
- CHECK-expression escaping and foreign-key target database mapping were corrected.
- Routine DDL extraction uses `SHOW CREATE`; trigger cleanup/order was corrected for migration data loads.
- Routine grant metadata failure no longer prevents supported table-grant processing.
- Supported partition reconciliation stages and verifies table metadata before replacement.

These are resolved findings, not current failures. See the local audit and limitations document for detail.

## Part 15 — Known Environment Dependencies

MySQL runs may depend on source/target account privileges for database/table creation, `ALTER`, `DROP`, indexes, `REFERENCES`, triggers, events, routines, users, and grants. Grant discovery depends on privilege metadata visibility. Functions/triggers may depend on binary-log policy; Event runtime depends on Event Scheduler and event/definer privileges. Azure additionally requires network/firewall access, TLS, valid credentials, and service-supported configuration. Classify a prerequisite failure separately from an implementation failure.

## Part 16 — Evidence and Related Documentation

- [MySQL Local Audit](MYSQL_LOCAL_AUDIT.md)
- [MySQL Local → Azure Audit](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
- [MySQL Object Support Matrix](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- [MySQL Test Guide](MYSQL_TEST_GUIDE.md)
- [MySQL Limitations](MYSQL_LIMITATIONS.md)
- [MySQL E2E Runbook](MYSQL_E2E_RUNBOOK.md)

```text
Connect and resolve secrets
    → snapshot MySQL Event / DEFINER metadata
    → prepare database and discover tables
    → create tables and partition definitions
    → FULL clear of migrated-table rows + parent-first batch load
    → constraints/indexes + AUTO_INCREMENT
    → views/routines/triggers/Events/comments/grants
    → validation and object summary
    → audit log + JSON/HTML report + final status
```




