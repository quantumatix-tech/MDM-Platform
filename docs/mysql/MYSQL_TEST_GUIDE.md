# MySQL Object E2E Test Guide

This guide explains how to prepare, run, and verify a MySQL object migration using the repository's MySQL connector. It covers the available directional configs and MySQL-specific structural, data, functional, and negative checks.

A zero source count means **Not exercised in this dataset**. It is not evidence that the object type is unsupported.

## Prerequisites

- MySQL 8.0-compatible source and target servers reachable from the migration host.
- MySQL client (`mysql`) available for connection and verification.
- Python 3.11+ and project dependencies installed (`pip install -e .`).
- Migration accounts with required source metadata/read access and target DDL/data privileges for the selected scope.
- Azure MySQL testing: TLS enabled in the config, network route and firewall access to port 3306.
- Grant testing: required privilege metadata must be visible to the migration account.
- Function/trigger testing: check binary logging policy and target privileges; Error 1419 may occur when `log_bin=ON` and `log_bin_trust_function_creators=OFF`.

## Secret Environment Variables

The `env` secret provider resolves `password_secret` names from `SECRET_<name>` environment variables at runtime.

```powershell
$env:SECRET_mysql_source_pass = "<source_password>"
$env:SECRET_mysql_target_pass = "<target_password>"
```

Do not put real passwords in YAML or commit them to Git.

## Configuration

### Local → Local

There is no dedicated Local → Local YAML config in `config/` at present. The checked-in `config/mysql_local_test.yaml` actually configures Azure MySQL → local MySQL; do not use it as Local → Local without changing and reviewing both endpoints. For a Local → Local test, create a local-only config based on this verified structure:

```yaml
source:
  engine: mysql
  connection:
    host: <source-host>
    port: 3306
    database: <source-database>
    username: <source-user>
    password_secret: mysql_source_pass
    ssl: <true-or-false-for-server>

target:
  engine: mysql
  connection:
    host: <target-host>
    port: 3306
    database: <target-database>
    username: <target-user>
    password_secret: mysql_target_pass
    ssl: <true-or-false-for-server>

migration:
  mode: full
  batch_size: 1000
  reconcile_target_schema: false

secrets:
  provider: env
```

Set each `ssl` value to match the actual server. Other repository options such as retry, validation, and logging are optional to the basic run.

### Local → Azure

Use `config/mysql_onpremise_cloud_test.yaml`. It points from a local/on-premise MySQL source to Azure MySQL, with TLS enabled. Verify the checked-in endpoints/database names before running.

### Azure → Local

Use `config/mysql_local_test.yaml`. It points from Azure MySQL to a local MySQL target, with TLS enabled. Verify both endpoints before running.

### Target Schema Reconciliation

A clean target does not require reconciliation. Enable this only for a dedicated test involving an incompatible existing target table:

```yaml
migration:
  reconcile_target_schema: true
```

The implementation contains a guarded MySQL reconciliation path and verifies staged partition metadata before replacement. This is not a claim of general reconciliation for every possible schema difference. Use a disposable target and consult [MYSQL_LIMITATIONS.md](MYSQL_LIMITATIONS.md).

## Step 1 — Prepare / Reset the MySQL Test Databases

Connect to each server and confirm the version, endpoint, and database. Use the MySQL client installed in your environment:

```powershell
mysql -h <host> -P 3306 -u <user> -p
```

```sql
SELECT VERSION(), @@hostname, DATABASE();
SHOW DATABASES;
SHOW DATABASES LIKE '<test_database>';
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'log_bin_trust_function_creators';
```

There is no checked-in MySQL fixture reset script. Prepare dedicated test databases using the environment's approved process. For a clean comparison, start with a clean target; FULL migration does not remove unrelated target-only objects. Run destructive reset/drop operations only against databases confirmed to be disposable test databases.

If `log_bin=ON` and `log_bin_trust_function_creators=OFF`, function or trigger creation can fail with MySQL Error 1419, subject to account privileges. An authorized administrator may choose to set `SET PERSIST log_bin_trust_function_creators = ON;`. The migration platform does not change this setting or require users to obtain `SUPER` as a default workaround. If the prerequisite is unavailable, record the affected object as environment-blocked.

## Step 2 — Populate the Source

Use a prepared test database or a dedicated representative fixture. No reusable MySQL fixture SQL bundle is checked in. For a focused test, use only the objects relevant to the scenario.

Where applicable, the audited MySQL object set includes:

- Tables, columns, representative MySQL data types, and rows
- Primary keys, UNIQUE and CHECK constraints, defaults, foreign keys (including tested cross-database references)
- `AUTO_INCREMENT`, generated columns, secondary/composite indexes, and partitions
- Views, functions, procedures, triggers, and events
- Table and column comments
- Supported grants/security principals, where metadata is visible and the target privileges allow it

The audit exercised representative numeric, character/text, binary, temporal, JSON, ENUM, and SET data. The list is representative, not exhaustive. The connector normalizes MySQL SET values returned as collections; focused coverage is in `tests/unit/test_mysql_datatypes.py`.

## Step 3 — Verify the Source

Connect to the source database. These MySQL metadata queries establish the baseline; compare exact row counts for representative tables before migration.

### Tables

```sql
SHOW TABLES;
```

Confirm the expected base tables are present. To distinguish base tables from views:

```sql
SELECT TABLE_NAME, TABLE_TYPE
FROM information_schema.tables
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_TYPE, TABLE_NAME;
```

### Columns / data types

```sql
SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, COLUMN_TYPE,
       IS_NULLABLE, COLUMN_DEFAULT, EXTRA, GENERATION_EXPRESSION, COLUMN_COMMENT
FROM information_schema.columns
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, ORDINAL_POSITION;
```

Confirm types, nullability, defaults, `AUTO_INCREMENT`, generated expressions, and comments for the fixture.

### Row counts

`information_schema.tables.TABLE_ROWS` can be approximate for some engines. Use `COUNT(*)` for exact per-table baselines:

```sql
SELECT COUNT(*) AS row_count FROM `<table_name>`;
```

### Indexes

```sql
SHOW INDEX FROM `<table_name>`;
```

Or inventory all indexes:

```sql
SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE
FROM information_schema.statistics
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
```

Confirm key columns and their order, uniqueness, and index type.

### Constraints

```sql
SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE
FROM information_schema.table_constraints
WHERE CONSTRAINT_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, CONSTRAINT_TYPE, CONSTRAINT_NAME;
```

### Foreign keys

```sql
SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA,
       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM information_schema.key_column_usage
WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION;
```

Check `REFERENCED_TABLE_SCHEMA` as well as the referenced table, especially for cross-database FK cases.

### Views

```sql
SHOW FULL TABLES WHERE Table_type = 'VIEW';
SHOW CREATE VIEW `<view_name>`;
```

### Functions / procedures

```sql
SHOW FUNCTION STATUS WHERE Db = DATABASE();
SHOW PROCEDURE STATUS WHERE Db = DATABASE();
SHOW CREATE FUNCTION `<function_name>`;
SHOW CREATE PROCEDURE `<procedure_name>`;
```

Run only the applicable `SHOW CREATE` statement for objects present.

### Triggers

```sql
SHOW TRIGGERS FROM `<database_name>`;
SHOW CREATE TRIGGER `<trigger_name>`;
```

### Events

```sql
SHOW EVENTS FROM `<database_name>`;
SHOW CREATE EVENT `<event_name>`;
SHOW VARIABLES LIKE 'event_scheduler';
```

Event definition metadata and scheduler-driven runtime are separate checks.

### Partitions

```sql
SELECT TABLE_NAME, PARTITION_NAME, PARTITION_METHOD, PARTITION_EXPRESSION,
       PARTITION_DESCRIPTION, PARTITION_ORDINAL_POSITION
FROM information_schema.partitions
WHERE TABLE_SCHEMA = DATABASE() AND PARTITION_NAME IS NOT NULL
ORDER BY TABLE_NAME, PARTITION_ORDINAL_POSITION;
```

### Comments

Table and column comments are included in the columns query above. Table comments can also be inspected with:

```sql
SELECT TABLE_NAME, TABLE_COMMENT
FROM information_schema.tables
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE';
```

### Grants / security

```sql
SHOW GRANTS;
```

This shows the current account. Grant discovery for other accounts depends on metadata visibility. User/security migration does not copy account passwords or authentication secrets.

## Step 4 — Run the Migration

Set the source and target secrets from the selected config, then run from the repository root.

Local → Local (use your reviewed local-only config):

```powershell
python -m migration_platform --config <local-to-local-config>.yaml --mode full --no-live-ui
```

Local → Azure:

```powershell
python -m migration_platform --config config/mysql_onpremise_cloud_test.yaml --mode full --no-live-ui
```

Azure → Local:

```powershell
python -m migration_platform --config config/mysql_local_test.yaml --mode full --no-live-ui
```

The CLI prints the run ID and status. Reports are written under `reports/`; an audit JSONL may be present under `logs/` for the run.

## Step 5 — Verify the Migration Result

Record run ID, direction, final status, table/row counts, failed objects, errors, and success percentage from the CLI/report. Review `reports/<run_id>.json` and `reports/<run_id>.html` and `logs/<run_id>.jsonl` if generated. Compare target findings with the source baseline from Step 3.

### 5a. Structural Verification

Repeat the applicable Step 3 metadata checks on the target. Use `SHOW CREATE TABLE` for full DDL and confirm:

```sql
SHOW CREATE TABLE `<table_name>`;
```

Check columns/defaults, PK/UNIQUE/CHECK/FK constraints, indexes, `AUTO_INCREMENT`, generated expressions, views, routines, triggers, events, partitions, comments, and grants only where the source fixture includes them. For partitions, compare `SHOW CREATE TABLE` and `information_schema.partitions` for method, expression, ordered partition names, boundaries, and data placement. Do not manually add target partitions to manufacture a match.

For routines/triggers, compare `DEFINER` in `SHOW CREATE` with the configured/observed target behavior. In MySQL→MySQL runs the CLI configures preservation of the source definer by default; account existence and privileges still affect whether stored-object creation/runtime succeeds. Do not claim this succeeds for every account/server combination.

### 5b. Data Validation

For each migrated table, repeat exact `COUNT(*)` on source and target. Compare with the report's source, migrated, and failed row counts. Check representative values with a stable primary-key order:

```sql
SELECT * FROM `<table_name>` ORDER BY `<primary_key>` LIMIT 10;
```

For generated columns, compare the generation expression and computed values; the loader excludes generated values from ordinary inserts so MySQL calculates them. MySQL SET values are normalized by the connector; datatype tests cover this behavior.

### 5c. Functional / Object Validation

Run behavior checks only against a dedicated test fixture with safe test arguments:

- Query a migrated view and compare its result with the source.
- Call a representative function in a `SELECT`; invoke a procedure with `CALL` and verify its intended result/effect.
- Apply controlled DML for a trigger and inspect its expected side effect.
- Confirm generated columns recalculate from base-column changes.
- Check partition definitions and partitioned-table data; live audit coverage is for tested scenarios, not every partition strategy.
- Inspect event definition/status. Runtime depends on Event Scheduler, timing, definer, and privileges. Azure audit evidence records scheduler `OFF`, so Azure automatic event execution was not verified.
- Use `SHOW GRANTS` and visible privilege metadata for grant checks. If the account cannot see grant metadata, classify the check as **Environment Blocked / Privilege Visibility**, not a pass or unsupported.

### 5d. Negative / Error Tests

Use disposable fixture data only. Record the attempted object and observed MySQL error; do not run negative writes against production/shared databases.

- Duplicate a value protected by a known UNIQUE constraint; expect MySQL to reject it.
- Insert an invalid CHECK value or child FK reference; expect constraint rejection.
- Exercise a target-object failure in a test scope and inspect per-object failures/reporting. The orchestrator isolates object errors by default; `migration.stop_on_error: true` changes the failure policy.
- Re-run a migration only when the target/data state is understood; report success alone does not prove there are no pre-existing target-only objects.

**Cross-engine type safety:** for cross-engine paths, a missing target type mapping raises `UnmappedTypeError`; the target must not silently receive source-native MySQL DDL. Same-engine MySQL retains native type behavior where implemented. This is a safety behavior, not proof every type mapping is available.

## Step 6 — Run Unit Tests

Run from the repository root:

```powershell
python -m pytest tests/unit -q
```

Focused MySQL connector coverage:

```powershell
python -m pytest tests/unit/test_mysql_datatypes.py tests/unit/test_mysql_definer.py tests/unit/test_mysql_events.py -q
```

Record the actual result from the current branch; do not reuse a historical test count.

## Step 7 — Run Integration Tests

Run when the integration database/container prerequisites are available:

```powershell
python -m pytest tests/integration -q -x
```

The project marks integration tests with `integration: integration tests requiring real database containers`; this command runs the integration directory without a marker filter. If execution stops before migration logic due to authentication, missing database, network/firewall, service, or environment configuration, record an environment blocker rather than an implementation failure.

## Troubleshooting

### Connection / Authentication

**Problem:** connection timeout or access denied.  
**Check:** host/port 3306, server availability, firewall, TLS, selected database, secret variable spelling, and host-scoped account grants.  
**Next:** correct the environment/config and retry; do not classify a pre-migration connection failure as an object migration result.

### Error 1419

**Problem:** function/trigger creation is rejected under binary logging.  
**Check:** `SHOW VARIABLES LIKE 'log_bin';` and `SHOW VARIABLES LIKE 'log_bin_trust_function_creators';`, plus target privileges.  
**Next:** request the approved administrator configuration if appropriate. The platform does not change server-global settings; do not seek `SUPER` as a default workaround. Otherwise report the object as blocked/environment-dependent.

### SET datatype issue

**Problem:** SET values fail or differ.  
**Check:** source/target `COLUMN_TYPE`, values, and the report; run `tests/unit/test_mysql_datatypes.py`.  
**Next:** retain the exact failing value as a focused case. The connector contains SET normalization, but do not infer all datatype variants from it.

### View still references source

**Problem:** migrated view points at the old database.  
**Check:** `SHOW CREATE VIEW` on source and target and verify which referenced objects are in migration scope.  
**Next:** references to migrated objects are rewritten; arbitrary external database references are outside that rewrite boundary and need separate handling.

### Partition structure missing

**Problem:** target definition or partition metadata differs.  
**Check:** `SHOW CREATE TABLE` plus `information_schema.partitions`; verify reconciliation setting and target state.  
**Next:** use a clean target or run the dedicated `reconcile_target_schema: true` scenario; do not manually add partitions as a workaround.

### Grant metadata not visible

**Problem:** expected grant is absent from migration inventory.  
**Check:** `SHOW GRANTS` and metadata visibility for the migration identity.  
**Next:** use approved privileges or classify the check as **Environment Blocked / Privilege Visibility**. Do not grant broad system-schema access merely to force a pass.

### Target-only objects

**Problem:** target contains objects absent from source.  
**Check:** whether they predated the run and whether they belong to the migration scope.  
**Next:** FULL migration does not delete unrelated target-only objects; clean only a dedicated target through its approved reset procedure.

### Foreign key / dependency failure

**Problem:** FK creation or data load fails.  
**Check:** source FK metadata including `REFERENCED_TABLE_SCHEMA`, referenced table availability, and report errors.  
**Next:** ensure dependencies are in scope and available on target; record complex/circular dependency patterns as not verified unless separately exercised.

## Adding Local → Azure MySQL Testing

1. Use `config/mysql_onpremise_cloud_test.yaml`; verify source/target endpoints and database names.
2. Set `SECRET_mysql_source_pass` and `SECRET_mysql_target_pass` as above.
3. Confirm TLS, outbound access to port 3306, and Azure firewall allow-listing.
4. Run:

   ```powershell
   python -m migration_platform --config config/mysql_onpremise_cloud_test.yaml --mode full --no-live-ui
   ```
5. Verify target objects/data and report. Record the run in [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md), keeping metadata and runtime claims separate.

Repository evidence records Local → Azure run `9f15eae2f1a84933a7ffe9746b828932`: 6 tables, 6,340 source rows, 6,340 migrated, 0 failed, 100%. Azure event scheduler was observed OFF; automatic event runtime was not verified. This evidence applies to that run and dataset.

## Adding Azure MySQL → Local Testing

1. Use `config/mysql_local_test.yaml`; verify Azure source and local target settings.
2. Set both secret environment variables and confirm Azure TLS/network access.
3. Run:

   ```powershell
   python -m migration_platform --config config/mysql_local_test.yaml --mode full --no-live-ui
   ```
4. Verify target structure/data and record the result in the direction-appropriate audit documentation.

The audit records Azure → Local run `fb374e4480d84894b22d5917807b507f`: 13 tables, 45 source rows, 45 migrated, 0 failed, 100%. Keep it separate from Local → Azure and Local → Local evidence.

## Reference

- [MySQL E2E Runbook](MYSQL_E2E_RUNBOOK.md)
- [MySQL Local Audit](MYSQL_LOCAL_AUDIT.md)
- [MySQL Local → Azure Audit](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
- [MySQL Object Support Matrix](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- [MySQL Limitations](MYSQL_LIMITATIONS.md)
- [MySQL Migration Flow](MYSQL_MIGRATION_FLOW.md)
