# MySQL E2E Runbook / Demo Guide

Operational steps to prepare a MySQL source, migrate it, and demonstrate the result. Use the configuration for the direction being tested; Local → Local and cloud-direction evidence are distinct.

---

## STEP 1 — Prepare / Reset MySQL DB

There is no checked-in MySQL fixture reset script. Do not run a guessed reset or drop command against a shared database. Use a dedicated source and target database, confirm their names from the selected config, and apply the environment's approved cleanup/recreation process if a clean state is required. `FULL` migration does not remove unrelated target-only objects, so a clean target is important for an unambiguous comparison.

Check the connection and selected database on both ends:

```powershell
mysql -h <host> -P 3306 -u <user> -p
```

```sql
SELECT VERSION(), @@hostname, DATABASE();
SHOW DATABASES;
```

The checked-in directional configs are `config/mysql_local_test.yaml` (Azure source → local target) and `config/mysql_onpremise_cloud_test.yaml` (local/on-premise source → Azure target). Verify endpoints and ensure the named databases exist before the run.

## STEP 2 — Populate Source Fixture

Use an existing test database or populate a dedicated source with the representative MySQL objects required for the demo. The repository does not contain a MySQL fixture SQL/reset bundle, so use the existing environment preparation procedure; do not assume PostgreSQL fixture scripts apply.

The documented MySQL audit fixture exercised tables/data, columns and representative datatypes, primary/unique keys, foreign keys, CHECK constraints, defaults, `AUTO_INCREMENT`, generated columns, indexes, views, routines, triggers, events, partitions, table/column comments and supported grants/security metadata. Consult [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md) for scope and caveats. A category with zero source objects is **Not exercised in this dataset**, not unsupported.

If you are building the fixture, prepare in dependency order: tables/columns and defaults; generated columns and keys; foreign keys/indexes/partitions; routines and views; triggers/events; comments; grants/security metadata.

## STEP 3 — Verify Source

Run in the selected source database and save the baseline for comparison. Metadata visibility depends on the account.

**Tables and row counts** (repeat count for each table):

```sql
SELECT TABLE_NAME FROM information_schema.tables
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME;
SELECT COUNT(*) AS row_count FROM `<table_name>`;
```

**Columns, keys, constraints, and indexes:**

```sql
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, GENERATION_EXPRESSION
FROM information_schema.columns WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, ORDINAL_POSITION;

SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE
FROM information_schema.table_constraints WHERE CONSTRAINT_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, CONSTRAINT_TYPE, CONSTRAINT_NAME;

SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA,
       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM information_schema.key_column_usage
WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION;

SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE
FROM information_schema.statistics WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
```

**Views, routines, triggers, events, and partitions:**

```sql
SELECT TABLE_NAME FROM information_schema.views
WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME;

SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.routines
WHERE ROUTINE_SCHEMA = DATABASE() ORDER BY ROUTINE_TYPE, ROUTINE_NAME;

SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING
FROM information_schema.triggers WHERE TRIGGER_SCHEMA = DATABASE()
ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME;

SELECT EVENT_NAME, STATUS, EVENT_TYPE, EXECUTE_AT, INTERVAL_VALUE, INTERVAL_FIELD
FROM information_schema.events WHERE EVENT_SCHEMA = DATABASE() ORDER BY EVENT_NAME;

SELECT TABLE_NAME, PARTITION_NAME, PARTITION_METHOD, PARTITION_EXPRESSION, PARTITION_DESCRIPTION
FROM information_schema.partitions
WHERE TABLE_SCHEMA = DATABASE() AND PARTITION_NAME IS NOT NULL
ORDER BY TABLE_NAME, PARTITION_ORDINAL_POSITION;
```

**Comments and grants:**

```sql
SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.tables
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE';
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM information_schema.columns
WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_COMMENT <> '';
SHOW GRANTS;
```

`SHOW GRANTS` describes the current account; discovery of grants for other accounts may require additional metadata visibility.

## STEP 4 — Run Migration

The repository configs use MySQL `source`/`target` sections with `password_secret`, `migration.mode: full`, and `secrets.provider: env`. The `env` provider resolves `password_secret: mysql_source_pass` and `mysql_target_pass` at runtime as `SECRET_mysql_source_pass` and `SECRET_mysql_target_pass`.

Set placeholders in PowerShell; never put real passwords in the config or docs:

```powershell
$env:SECRET_mysql_source_pass = "<source-password>"
$env:SECRET_mysql_target_pass = "<target-password>"
```

Run the matching configuration from the repository root:

**Local/on-premise → Azure MySQL**

```powershell
python -m migration_platform --config config/mysql_onpremise_cloud_test.yaml --mode full --no-live-ui
```

**Azure MySQL → Local MySQL**

```powershell
python -m migration_platform --config config/mysql_local_test.yaml --mode full --no-live-ui
```

Both checked-in files are directional; inspect endpoints before execution. For a separate Local → Local environment, use an appropriate local config based on the repository structure and verified local audit, and record that direction separately.

## STEP 5 — Verify Migration Result

Read the CLI's run ID and inspect `reports/<run_id>.json` and `.html`; inspect `logs/<run_id>.jsonl` when the run generated an audit log.

| Field | What to record/check |
|---|---|
| Status | `SUCCESS`, `PARTIAL_SUCCESS`, or `FAILED` |
| Run ID | CLI output |
| Tables / rows | Compare migrated counts with the recorded source scope/counts |
| Failed objects / errors | Exact objects and messages |
| Success percentage | Read with status and failure counts |

### 5a Structural

Run the applicable Step 3 metadata queries on the target and compare with source. Check table/column definitions, constraints, foreign keys, indexes, views, routines, triggers, events, partitions, comments and grants only where the source contains them. Additional focused checks:

```sql
SELECT TABLE_NAME, COLUMN_NAME, EXTRA, GENERATION_EXPRESSION
FROM information_schema.columns WHERE TABLE_SCHEMA = DATABASE()
  AND (EXTRA LIKE '%auto_increment%' OR GENERATION_EXPRESSION <> '');

SHOW CREATE TABLE `<table_name>`;
SHOW CREATE VIEW `<view_name>`;
SHOW CREATE FUNCTION `<function_name>`;
SHOW CREATE PROCEDURE `<procedure_name>`;
SHOW CREATE TRIGGER `<trigger_name>`;
SHOW CREATE EVENT `<event_name>`;
SHOW GRANTS;
```

Use only the applicable `SHOW CREATE` statement for objects present in the dataset.

### 5b Data

Repeat the source row-count query on the target for each migrated table. Compare source, migrated, and failed row counts in the report. For representative content, compare a small stable sample using a primary-key order:

```sql
SELECT * FROM `<table_name>` ORDER BY `<primary_key>` LIMIT 10;
```

### 5c Functional / Object verification

In a dedicated test database, query a migrated view, call a representative function in `SELECT`, and call a procedure with safe arguments using `CALL`. Verify trigger side effects through controlled DML, generated values through inserts/updates of base columns, and constraint behavior through valid operations. Event metadata/definition does not establish runtime: automatic execution requires Event Scheduler, definer and privileges. Azure audit evidence records `event_scheduler=OFF`, so Azure automatic event runtime was not verified.

### 5d Negative tests

Use only disposable fixture rows in a dedicated test database. Attempt a known-invalid CHECK or foreign-key value and a duplicate value for a known UNIQUE key; confirm MySQL rejects it with the expected constraint error. Roll back or remove test rows as appropriate. Do not run negative DML against production or shared data. Record the tested object and observed result; skip categories absent from the fixture.

## STEP 6 — Unit Tests

From the repository root:

```powershell
python -m pytest tests/unit -q
```

For focused MySQL tests:

```powershell
python -m pytest tests/unit/test_mysql_datatypes.py tests/unit/test_mysql_definer.py tests/unit/test_mysql_events.py -q
```

Record the actual command result for the current branch; do not copy historical test counts.

## STEP 7 — Integration Tests

Run when the configured MySQL infrastructure and credentials are available:

```powershell
python -m pytest tests/integration -q -x
```

If setup fails on authentication, database availability, network/firewall, or service configuration before migration behavior runs, record it as an environment/setup failure rather than a product result. See [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md) for test expectations.

## Troubleshooting

| Symptom | Check |
|---|---|
| Error 1045 / access denied | Username, password secret, host-scoped account grants, Azure authentication |
| Timeout / cannot connect | Host, port 3306, server status, firewall and network path |
| Missing source objects | Selected database, source inventory, metadata privileges and report scope |
| Row-count mismatch | Source/target counts, failed batches/objects, target pre-existing rows and report |
| Function/trigger error 1419 | `SHOW VARIABLES LIKE 'log_bin';`, `SHOW VARIABLES LIKE 'log_bin_trust_function_creators';`, target privileges; administrator policy may be needed |
| Event does not run | `SHOW VARIABLES LIKE 'event_scheduler';`, event status, definer and privileges; metadata success is separate from runtime |
| Missing grants | `SHOW GRANTS;` and account metadata visibility; passwords/credentials are not migrated |
| Partial success | Inspect object-level failures and exact report errors |

Do not change server-global policy settings or grant broad privileges as an automatic troubleshooting step; use the approved MySQL administrator procedure.

## Local → Azure

Use `config/mysql_onpremise_cloud_test.yaml`. Verify TLS/network access, endpoints, and secrets before running Step 4. The repository report for this direction records run `9f15eae2f1a84933a7ffe9746b828932`: 6 tables, 6,340 source rows, 6,340 migrated rows, 0 failed, 100%. This is evidence for that dataset/run, not every MySQL object or runtime behavior. The Azure audit records `event_scheduler=OFF` and automatic event execution as not verified; consult [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md) for scope and additional direction-specific evidence.

## Azure → Local

Use `config/mysql_local_test.yaml` after confirming its Azure source and local target endpoints. The audit documents run `fb374e4480d84894b22d5917807b507f`: 13 tables, 45 source rows, 45 migrated rows, 0 failed, 100%. This is separate from Local → Azure and Local → Local evidence. Consult [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md) and [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md) for evidence details.

## References

- [MySQL Local Audit](MYSQL_LOCAL_AUDIT.md)
- [MySQL Local → Azure Audit](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
- [MySQL Test Guide](MYSQL_TEST_GUIDE.md)
- [MySQL Object Support Matrix](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- [MySQL Limitations](MYSQL_LIMITATIONS.md)
- [MySQL Migration Flow](MYSQL_MIGRATION_FLOW.md)
