# MySQL Object E2E Test Guide

This guide explains how to run and verify MySQL object migration end-to-end using
the current MySQL Local → Local test configuration. It preserves the
MySQL-specific testing already documented in the project, including datatype,
partition, function/trigger policy, view isolation, and grant-visibility testing.

Azure / remote MySQL testing is **NOT STARTED**. Do not mark an Azure path
verified until an actual migration run and target inspection have been completed.

---

## Prerequisites

- MySQL 26.7.0 or compatible MySQL server running and accessible.
- `mysql` client available.
- Python 3.11+ with project dependencies installed.
- Source and target databases accessible to the migration account.
- Migration account has the object/data privileges required by the configured scope.
- On a binary-logged server, `log_bin_trust_function_creators` may affect
  function/trigger creation.
- For grant testing, source grant metadata must be visible to the migration
  identity and the target grantee must already exist.

The migration platform does **not** grant `SUPER` and does not change MySQL
server-global or persistent variables.

## Environment Variables

### Windows PowerShell

```powershell
$env:SECRET_mysql_source_pass = "<source_password>"
$env:SECRET_mysql_target_pass = "<target_password>"
```

### Bash / Linux / macOS

```bash
export SECRET_mysql_source_pass=<source_password>
export SECRET_mysql_target_pass=<target_password>
```

## Configuration

Use:

```text
config/mysql_local_test.yaml
```

The configuration uses source/target connection values and the corresponding
secret references.

For target structural reconciliation:

```yaml
migration:
  reconcile_target_schema: false
```

Set it to `true` only when specifically testing existing incompatible target
structures.

---

# Step 1 — Prepare / Reset the MySQL Test Databases

Connect to MySQL:

```powershell
& "C:\Program Files\MySQL\MySQL Server 26.7\bin\mysql.exe" `
  -h 127.0.0.1 -P 3306 -u mysql_test -p
```

Check the server:

```sql
SELECT VERSION();

SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'log_bin_trust_function_creators';
```

Check databases:

```sql
SHOW DATABASES LIKE 'mysql_migration_source';
SHOW DATABASES LIKE 'mysql_migration_target';
```

### Function / trigger Error 1419 prerequisite

With binary logging enabled and:

```text
log_bin_trust_function_creators = OFF
```

function/trigger creation can fail with Error 1419.

The platform records each affected object once as:

```text
FUNCTION: BLOCKED
TRIGGER: BLOCKED
```

and does not count it as migrated.

If an administrator intentionally enables the prerequisite:

```sql
SET PERSIST log_bin_trust_function_creators = ON;
```

`SET GLOBAL ...` is runtime-only and can be lost after restart.

The migration platform never executes either command itself.

---

# Step 2 — Populate the Source Fixture

Use the existing MySQL fixture/test data. Completed Local → Local coverage
includes:

- Tables and columns
- Primary keys
- Foreign keys
- Cross-database foreign keys
- UNIQUE constraints
- CHECK constraints
- Defaults
- `AUTO_INCREMENT`
- Generated columns
- Secondary and composite indexes
- Table and column comments
- Views
- Functions
- Procedures
- Triggers
- Events
- Partitions
- Representative MySQL datatypes
- Explicit table grants / routine grants when metadata is visible

Use descriptive object names such as:

```text
vw_...
fn_...
sp_...
trg_...
evt_...
```

Do not use `E2E` in test object names.

---

# Step 3 — Verify the Source Fixture

```sql
USE mysql_migration_source;
SHOW TABLES;
```

### Table definitions

```sql
SHOW CREATE TABLE customers;
SHOW CREATE TABLE products;
SHOW CREATE TABLE orders;
```

### Columns

```sql
SELECT
    TABLE_NAME,
    COLUMN_NAME,
    ORDINAL_POSITION,
    COLUMN_DEFAULT,
    IS_NULLABLE,
    DATA_TYPE,
    COLUMN_TYPE,
    EXTRA,
    COLUMN_KEY
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'mysql_migration_source'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
```

### Indexes

```sql
SHOW INDEX FROM customers;
SHOW INDEX FROM products;
SHOW INDEX FROM orders;
```

### Constraints

```sql
SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
WHERE CONSTRAINT_SCHEMA = 'mysql_migration_source'
ORDER BY TABLE_NAME, CONSTRAINT_NAME;
```

### Foreign keys

```sql
SELECT
    TABLE_NAME,
    CONSTRAINT_NAME,
    REFERENCED_TABLE_SCHEMA,
    REFERENCED_TABLE_NAME
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = 'mysql_migration_source'
  AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, CONSTRAINT_NAME;
```

### Views

```sql
SHOW FULL TABLES
FROM mysql_migration_source
WHERE TABLE_TYPE = 'VIEW';

SHOW CREATE VIEW mysql_migration_source.customer_order_summary;
```

### Functions / Procedures / Triggers / Events

```sql
SHOW FUNCTION STATUS
WHERE Db = 'mysql_migration_source';

SHOW PROCEDURE STATUS
WHERE Db = 'mysql_migration_source';

SHOW TRIGGERS FROM mysql_migration_source;

SHOW EVENTS FROM mysql_migration_source;
```

### Partitions

```sql
SELECT
    TABLE_NAME,
    PARTITION_NAME,
    PARTITION_METHOD,
    PARTITION_EXPRESSION,
    PARTITION_DESCRIPTION,
    PARTITION_ORDINAL_POSITION
FROM INFORMATION_SCHEMA.PARTITIONS
WHERE TABLE_SCHEMA = 'mysql_migration_source'
  AND PARTITION_NAME IS NOT NULL
ORDER BY TABLE_NAME, PARTITION_ORDINAL_POSITION;
```

### Comments

```sql
SELECT TABLE_NAME, TABLE_COMMENT
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'mysql_migration_source';

SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'mysql_migration_source'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
```

---

# Step 4 — Run the Migration

Set the secrets and run:

```powershell
python -m migration_platform `
  --config config/mysql_local_test.yaml `
  --mode full `
  --no-live-ui
```

Record:

- Run ID
- Final status
- Tables migrated
- Source rows
- Migrated rows
- Failed objects
- Error count
- Object-level outcomes

A successful row count alone is not object-support evidence.

---

# Step 5 — Verify the Migration Result

## 5a. Automatic / Report Verification

Inspect the generated report and confirm:

- Overall status
- Object migration results
- Failed / blocked objects
- Row counts
- Validation results
- Partition processing
- Function/trigger outcomes
- View/procedure/event outcomes

An object recorded as `BLOCKED`, `FAILED`, or `SKIPPED` must not be counted
as migrated.

## 5b. Manual Target Verification

```sql
USE mysql_migration_target;

SHOW TABLES;

SELECT TABLE_NAME, TABLE_ROWS
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'mysql_migration_target'
ORDER BY TABLE_NAME;
```

For exact counts:

```sql
SELECT COUNT(*) FROM customers;
SELECT COUNT(*) FROM products;
SELECT COUNT(*) FROM orders;
```

### Table definitions

```sql
SHOW CREATE TABLE customers;
SHOW CREATE TABLE products;
SHOW CREATE TABLE orders;
```

Verify columns, nullability, types, defaults, generated columns,
`AUTO_INCREMENT`, keys, constraints, comments and partition definitions.

### Indexes

```sql
SHOW INDEX FROM customers;
SHOW INDEX FROM products;
SHOW INDEX FROM orders;
```

### Constraints / FKs

```sql
SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
WHERE CONSTRAINT_SCHEMA = 'mysql_migration_target'
ORDER BY TABLE_NAME, CONSTRAINT_NAME;

SELECT
    TABLE_NAME,
    CONSTRAINT_NAME,
    REFERENCED_TABLE_SCHEMA,
    REFERENCED_TABLE_NAME
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = 'mysql_migration_target'
  AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, CONSTRAINT_NAME;
```

### View isolation

```sql
SHOW FULL TABLES
FROM mysql_migration_target
WHERE TABLE_TYPE = 'VIEW';

SHOW CREATE VIEW mysql_migration_target.customer_order_summary;
```

The target view must not contain migrated dependencies qualified with:

```text
mysql_migration_source.customers
mysql_migration_source.orders
```

After migration, insert a customer only in target and a separate customer only
in source. The target view must see the target-only row and must not depend on
the source-only row.

### Functions / procedures

```sql
SHOW FUNCTION STATUS
WHERE Db = 'mysql_migration_target';

SHOW PROCEDURE STATUS
WHERE Db = 'mysql_migration_target';
```

Inspect definitions with `SHOW CREATE FUNCTION` and `SHOW CREATE PROCEDURE`,
then execute them and verify expected results/effects.

### Triggers

```sql
SHOW TRIGGERS FROM mysql_migration_target;
```

Inspect with:

```sql
SHOW CREATE TRIGGER mysql_migration_target.trg_after_customer_insert_log;
```

Do not mark trigger coverage PASS from metadata alone.

### Events

```sql
SHOW EVENTS FROM mysql_migration_target;
```

Runtime execution depends on Event Scheduler state and valid definer/privileges.

---

# Step 5c — Negative / Functional Tests

1. **Duplicate UNIQUE value** — duplicate insert must be rejected.
2. **Invalid FOREIGN KEY** — invalid referenced key must be rejected.
3. **Invalid CHECK** — violating value must be rejected.
4. **Default value** — omitted defaulted column must receive expected value.
5. **Generated column** — generated expression must calculate expected value.
6. **AUTO_INCREMENT** — omitted ID must receive the next identifier.
7. **View functionality** — target view must return expected target data.
8. **Procedure functionality** — target procedure must produce expected effect.
9. **Function functionality** — target function must return expected value.
10. **Trigger functionality** — target trigger must produce its expected effect.
11. **Event functionality** — event metadata and, where applicable, scheduled
    behavior must be verified.
12. **Partition structure** — structure and data must both be verified.

### Trigger dependency fixture

```sql
CREATE TABLE customer_insert_log (
    id INT NOT NULL AUTO_INCREMENT,
    customer_id INT NOT NULL,
    customer_name VARCHAR(100) NOT NULL,
    customer_status VARCHAR(20) NOT NULL,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) COMMENT='Customer insert trigger test log';

CREATE TRIGGER trg_after_customer_insert_log
AFTER INSERT ON customers
FOR EACH ROW
INSERT INTO customer_insert_log
    (customer_id, customer_name, customer_status)
VALUES
    (NEW.id, NEW.name, NEW.status);
```

Verify the source trigger with a source insert, run a clean migration, then
insert a target customer and verify the target log row.

Do not grant `SUPER` to the migration account solely for this test.

---

# Step 6 — Task 10: Partition Testing

Create:

```sql
CREATE TABLE tbl_partition_test (
    id INT NOT NULL,
    name VARCHAR(100) NOT NULL,
    created_at DATE NOT NULL,
    PRIMARY KEY (id, created_at)
)
PARTITION BY RANGE (YEAR(created_at)) (
    PARTITION p2025 VALUES LESS THAN (2026),
    PARTITION p2026 VALUES LESS THAN (2027),
    PARTITION pmax VALUES LESS THAN MAXVALUE
);

INSERT INTO tbl_partition_test
    (id, name, created_at)
VALUES
    (1, 'Partition 2025', '2025-06-15'),
    (2, 'Partition 2026', '2026-09-13'),
    (3, 'Partition Future', '2027-01-01');
```

After clean FULL migration:

```sql
SHOW CREATE TABLE mysql_migration_target.tbl_partition_test;
```

```sql
SELECT
    PARTITION_NAME,
    PARTITION_METHOD,
    PARTITION_EXPRESSION,
    PARTITION_DESCRIPTION,
    PARTITION_ORDINAL_POSITION
FROM INFORMATION_SCHEMA.PARTITIONS
WHERE TABLE_SCHEMA = 'mysql_migration_target'
  AND TABLE_NAME = 'tbl_partition_test'
  AND PARTITION_NAME IS NOT NULL
ORDER BY PARTITION_ORDINAL_POSITION;
```

Confirm:

- `RANGE(YEAR(created_at))`
- `p2025`
- `p2026`
- `pmax`
- correct boundaries
- correct ordering
- `MAXVALUE`
- 3/3 rows

The partition definition must be present in the initial target `CREATE TABLE`.
No manual target-side `ALTER` is part of the test.

Partitions are integrated into the existing Object Migration Results category as
`Partitions`. They are not rendered as a separate UI panel. The Migration
Timeline `Create Partitions` phase reports the actual number processed; detailed
structure remains available in report JSON.

---

# Step 7 — Task 11: Specific MySQL Data Types Testing

Task 11 verifies MySQL-specific data types and their values.

The dedicated fixture:

```text
tbl_datatype_test
```

contains 31 columns.

### Datatypes covered

- `TINYINT`, `TINYINT(1)`, `SMALLINT`, `MEDIUMINT`, `INT UNSIGNED`, `BIGINT`, `YEAR`
- `DECIMAL(10,2)`, `NUMERIC(12,4)`
- `FLOAT`, `DOUBLE`
- `CHAR(5)`, `VARCHAR(255)`, `TEXT`, `MEDIUMTEXT`, `LONGTEXT`
- `BINARY(8)`, `VARBINARY(32)`, `BLOB`, `MEDIUMBLOB`, `LONGBLOB`
- `DATE`, `TIME`, `DATETIME(6)`, `TIMESTAMP(6)`
- `BOOLEAN` / `BOOL`
- `JSON`
- `ENUM('new','active','closed')`
- `SET('email','sms','push')`

Run:

```powershell
python -m migration_platform `
  --config config/mysql_local_test.yaml `
  --mode full `
  --no-live-ui
```

### Source definition

```sql
SELECT
    COLUMN_NAME,
    DATA_TYPE,
    COLUMN_TYPE,
    IS_NULLABLE,
    COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'mysql_migration_source'
  AND TABLE_NAME = 'tbl_datatype_test'
ORDER BY ORDINAL_POSITION;
```

### Target definition

```sql
SHOW COLUMNS FROM mysql_migration_target.tbl_datatype_test;
```

All 31 definitions must be compared with source.

### Value verification

Use `SELECT` and `HEX()`:

```sql
SELECT
    id,
    json_col,
    enum_col,
    set_col,
    HEX(binary_col),
    HEX(varbinary_col),
    HEX(blob_col)
FROM mysql_migration_target.tbl_datatype_test;
```

Also compare numeric, date/time and boolean values.

Expected checks include:

- ENUM value such as `active`
- SET representation such as `email,sms`
- JSON document preservation
- binary/LOB values through `HEX()`
- exact DECIMAL/NUMERIC precision and scale
- FLOAT/DOUBLE values
- `DATETIME(6)` / `TIMESTAMP(6)` fractional seconds
- unsigned integer values
- BOOLEAN/BOOL represented by MySQL as `TINYINT(1)`

## SET migration issue and fix

Initial migration failed with:

```text
Python type set cannot be converted
```

Root cause: MySQL SET values were returned as Python `set` / `frozenset`.

The fix in `core/connectors/mysql.py`:

```text
_normalize_mysql_set_value()
```

converts MySQL SET collections to MySQL-compatible comma-separated text while
preserving declared member order.

The fix applies only to MySQL-source SET values and does not alter ENUM, JSON,
binary/LOB or unrelated values.

Automated coverage includes:

```text
tests/unit/test_mysql_datatypes.py
tests/unit/test_cross_engine_type_safety.py
```

Do not use datatype migration success alone as evidence; compare definitions
and values.

---

# Step 8 — Task 13: Users / Roles / Grants Testing

Current MySQL DMS grant scope is:

- Explicit table grants.
- Routine `EXECUTE` grants when `INFORMATION_SCHEMA.ROUTINE_PRIVILEGES` is
  visible.

The DMS does not currently migrate:

- users;
- roles;
- role assignments;
- authentication/password state;
- global privileges;
- database/schema privileges;
- column grants;
- view grants;
- trigger grants;
- event grants.

## Grant test matrix

| Area | Status | Required result |
|---|---|---|
| Existing target grantee | SUPPORTED / TESTED | Direct `GRANT` requires target grantee |
| Table-grant discovery | SUPPORTED / TESTED | `TABLE_PRIVILEGES` discovery preserves schema/grantee |
| Target database mapping | SUPPORTED / TESTED | Source database maps to configured target |
| Successful grant application | SUPPORTED / TESTED | Applied grant is recorded as applied/migrated |
| Failed grant application | SUPPORTED / TESTED | Failure is rolled back and recorded as failed/skipped |
| Routine `EXECUTE` | CONDITIONALLY SUPPORTED / TESTED | Requires visible `ROUTINE_PRIVILEGES` and existing grantee |
| Users / roles / assignments | OUT OF SCOPE / NOT SUPPORTED | No account creation/assignment |
| Global/database/schema/column/view/trigger/event grants | OUT OF SCOPE / NOT SUPPORTED | No current extraction/application path |

## Metadata visibility boundary

An empty `TABLE_PRIVILEGES` result is not evidence that the source account has
no grants. Under the tested least-privilege identity, another account's grants
may be hidden.

Therefore:

```text
BLOCKED BY ENVIRONMENT / PRIVILEGE VISIBILITY
```

must be used when the source grant metadata cannot actually be observed.

Do not grant broad system access merely to manufacture a PASS.

## Controlled live grant test

1. Administrator creates a disposable visible source grant.
2. Same grantee is pre-created on target.
3. Confirm source metadata is visible to the migration identity.
4. Run migration.
5. Verify mapped target grant metadata as administrator.
6. Connect as grantee and test one allowed and one ungranted operation.
7. Remove only disposable test objects/accounts/grants.

---

# Step 9 — Target Schema Reconciliation

When an existing target table has an incompatible supported structure, test with:

```yaml
migration:
  reconcile_target_schema: true
```

The MySQL reconciliation path:

- compares source/target structural signatures;
- stages a source-derived replacement;
- verifies the resulting structure;
- atomically swaps the replacement with the existing table;
- retains the old target table as a temporary `__dms_backup_*` object while the
  migration is still in progress;
- removes the backup only after migration, data load, object phases and
  validation succeed.

If an inbound FK references an unmanaged object outside the migration set,
safe reconciliation can be blocked and must be reported clearly.

---

# Step 10 — End-to-End Validation

Final Local → Local validation should cover:

- object existence;
- table/column metadata;
- row counts;
- data values;
- PK / UK / FK / CHECK;
- defaults;
- AUTO_INCREMENT;
- generated columns;
- indexes;
- comments;
- partitions;
- views;
- functions;
- procedures;
- triggers;
- events;
- supported grants;
- report/object outcomes.

Latest successful Local → Local evidence:

```text
Run ID: 84e6100202584c8cbaf2a52b64276fa2
Mode: FULL
Tables: 11
Rows: 44/44
Failed: 0
Errors: 0
Status: SUCCESS
```

Always record the actual run ID and actual verification output in the audit.

---

# Step 11 — Unit / Integration Testing

Run:

```powershell
python -m pytest tests/unit -q
```

Record the actual result from the current branch/run; do not hard-code an old
test count into the guide.

Integration tests may be run when the required environment is available:

```powershell
python -m pytest tests/integration -q -x
```

If a test is blocked before migration logic executes, record it as an
environment blocker rather than a migration failure.

---

# Cloud / Azure MySQL Testing

Use the same migration code path. Change only connection values, secret
references and TLS settings as required by the cloud environment.

Required Azure evidence includes:

- TLS/authentication;
- source/target connectivity;
- target database privileges;
- supported DDL/object checks;
- datatype verification;
- partition verification;
- routine/trigger policy behavior;
- Event Scheduler behavior;
- grant metadata visibility.

**Azure testing is currently NOT STARTED.**

Do not classify Azure support as verified until an actual Azure migration and
target inspection have occurred.

---

# Troubleshooting

## `Error 1419` during function/trigger creation

Check:

```sql
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'log_bin_trust_function_creators';
```

If required, ask a MySQL administrator to apply:

```sql
SET PERSIST log_bin_trust_function_creators = ON;
```

Do not grant `SUPER` to the migration account solely for this test.

## `Python type set cannot be converted`

Verify that the source column is MySQL `SET` and that
`_normalize_mysql_set_value()` is present in `core/connectors/mysql.py`.

## View still references source database

Run:

```sql
SHOW CREATE VIEW mysql_migration_target.customer_order_summary;
```

Migrated dependencies should reference the target database, not the source
database.

## Partition structure is missing

Check both:

```sql
SHOW CREATE TABLE mysql_migration_target.tbl_partition_test;
```

and the `INFORMATION_SCHEMA.PARTITIONS` query above.

Do not manually add partitions as a test workaround.

## Grant discovery returns no rows

Check:

```sql
SELECT CURRENT_USER();
SELECT USER();
```

Determine whether the migration identity can actually see the source grant
metadata. If not, record the grant test as environment/privilege visibility
blocked.

## Target-only object remains

FULL migration does not automatically delete unmanaged target-only objects.
Remove test-only target objects when a clean target is required.

---

# Reference

- MySQL object support matrix:
  `docs/mysql/MYSQL_OBJECT_SUPPORT_MATRIX.md`
- MySQL limitations:
  `docs/mysql/MYSQL_LIMITATIONS.md`
- MySQL local audit:
  `docs/mysql/MYSQL_LOCAL_AUDIT_FINAL.md`
- MySQL migration configuration:
  `config/mysql_local_test.yaml`
- MySQL connector:
  `core/connectors/mysql.py`
- Unit tests:
  `tests/unit/`
- Integration tests:
  `tests/integration/`
