# MSSQL E2E Runbook / Demo Guide

Reusable guide for running a MSSQL end-to-end migration demo.
This document answers: "If my lead asks me to run a MSSQL end-to-end migration demo, what exactly do I do?"

---

## STEP 1 — Prepare / connect source database

Ensure the source SQL Server is running and accessible.

```sql
-- Verify connection (use your actual connection details)
SELECT @@VERSION;
```

---

## STEP 2 — Prepare / connect target database

Ensure the target SQL Server is running and accessible. The migration platform creates the target database automatically if it does not exist.

```sql
-- Verify connection (use your actual connection details)
SELECT @@VERSION;
```

---

## STEP 3 — Decide: existing schema or fresh E2E test schema

Choose one of:

**Option A — Existing schema/object set:**

Use an existing database and schema that already contains the objects you want to migrate. Skip to Step 5 after verifying source objects exist.

**Option B — Fresh E2E test schema (recommended for demo):**

Use a representative MSSQL source database containing:

- **Schemas:** `sales`, `billing`
- **Tables:** Tables with data in `sales`, `billing`
- **Data:** Representative rows across tables
- **Identity columns:** IDENTITY-based PKs
- **Computed columns:** WHERE applicable
- **Indexes:** Clustered and non-clustered
- **Views:** Schema-qualified views
- **Functions & Procedures:** T-SQL, schema-qualified
- **Triggers:** AFTER/INSTEAD OF triggers on tables
- **Synonyms:** Schema-qualified
- **UDTs:** XML, JSON, VARBINARY/BINARY, UNIQUEIDENTIFIER, SQL_VARIANT
- **Partitioning:** Partition functions and schemes
- **Comments/Extended Properties:** On schemas, tables, columns
- **Users/Roles/Permissions:** Database roles and permissions

Reset the source fixture to its initial state before each run.

---

## STEP 4 — Create / prepare test objects

If using the fixture (Option B), ensure all test objects are created in the correct dependency order:

1. Schemas → 2. UDTs/Types → 3. Tables → 4. Data → 5. Indexes → 6. Constraints (PK/FK) → 7. Views → 8. Functions/Procedures → 9. Triggers → 10. Synonyms → 11. Comments/Extended Properties → 12. Users/Roles/Grants

Create a migration config file:

```yaml
migration:
  mode: full
  include_schemas:
    - <schema_1>
    - <schema_2>

source:
  engine: mssql
  connection:
    host: <source-host>
    port: <source-port>
    database: <source-database>
    username: <source-username>
    password_secret: SECRET_mssql_source_pass
    ssl: false

target:
  engine: mssql
  connection:
    host: <target-host>
    port: <target-port>
    database: <target-database>
    username: <target-username>
    password_secret: SECRET_mssql_target_pass
    ssl: false

secrets:
  provider: env
```

Set secrets:

```powershell
$env:SECRET_mssql_source_pass = "<source_password>"
$env:SECRET_mssql_target_pass = "<target_password>"
```

---

## STEP 5 — Verify the source before migration

Connect to the source database and confirm expected objects exist:

```sql
-- Tables
SELECT schema_name, table_name
FROM information_schema.tables
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
ORDER BY schema_name, table_name;
```

Also verify:

**Row counts:**

```sql
SELECT table_schema, table_name, COUNT(*) AS row_count
FROM <schema_1>.<table_name>
GROUP BY table_schema, table_name;
```

**Constraints:**

```sql
SELECT tc.table_schema, tc.table_name, tc.constraint_type, tc.constraint_name
FROM information_schema.table_constraints tc
WHERE tc.table_schema IN ('<schema_1>', '<schema_2>')
ORDER BY tc.table_schema, tc.table_name, tc.constraint_name;
```

**Indexes:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, i.name AS index_name, i.type_desc
FROM sys.indexes i
JOIN sys.tables t ON i.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>')
ORDER BY s.name, t.name, i.name;
```

**Foreign keys (including cross-schema):**

```sql
SELECT fk.name AS fk_name,
       fs.name AS source_schema, ft.name AS source_table,
       ts.name AS target_schema, tt.name AS target_table
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
JOIN sys.tables ft ON fkc.parent_object_id = ft.object_id
JOIN sys.schemas fs ON ft.schema_id = fs.schema_id
JOIN sys.tables tt ON fkc.referenced_object_id = tt.object_id
JOIN sys.schemas ts ON tt.schema_id = ts.schema_id
WHERE fs.name IN ('<schema_1>', '<schema_2>')
ORDER BY fs.name, ft.name, fk.name;
```

---

## STEP 6 — Run the migration

```powershell
python -m migration_platform --config <config-file>.yaml --mode full --no-live-ui
```

Use `--no-live-ui` for CI/log-friendly output. Omit it for the interactive UI.

Where `<config-file>` is the path to your migration YAML (e.g., `config/mssql_local_test.yaml`).

---

## STEP 7 — Read the migration result / report

After completion, the CLI prints the run ID, report paths, and final status. Check:

| Field | What to look for |
|---|---|
| Run status | `SUCCESS` or `FAILED` |
| Migrated tables | Number of tables migrated |
| Migrated rows | Total rows migrated (should match source) |
| Failed objects | List of objects that failed (should be empty) |
| Errors | Error messages (should be none) |
| Success percentage | 100% for a fully successful run |

Reports are also saved under `reports/<run_id>.html` and `reports/<run_id>.json`.

---

## STEP 8 — Verify the target

### A. Structural verification

**Schemas:**

```sql
SELECT schema_name FROM information_schema.schemata
WHERE schema_name IN ('<schema_1>', '<schema_2>');
```

**Tables and row counts:**

```sql
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;
```

**Primary keys:**

```sql
SELECT tc.table_schema, tc.table_name, kcu.column_name, tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema IN ('<schema_1>', '<schema_2>') AND tc.constraint_type = 'PRIMARY KEY'
ORDER BY tc.table_schema, tc.table_name;
```

**Foreign keys:**

```sql
SELECT fk.name, fs.name AS source_schema, ft.name AS source_table,
       ts.name AS target_schema, tt.name AS target_table
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
JOIN sys.tables ft ON fkc.parent_object_id = ft.object_id
JOIN sys.schemas fs ON ft.schema_id = fs.schema_id
JOIN sys.tables tt ON fkc.referenced_object_id = tt.object_id
JOIN sys.schemas ts ON tt.schema_id = ts.schema_id
WHERE fs.name IN ('<schema_1>', '<schema_2>')
ORDER BY fs.name, ft.name;
```

**Indexes:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, i.name AS index_name, i.type_desc
FROM sys.indexes i
JOIN sys.tables t ON i.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>')
ORDER BY s.name, t.name, i.name;
```

**Identity columns:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
       IDENT_CURRENT(s.name + '.' + t.name) AS current_identity
FROM sys.identity_columns ic
JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
JOIN sys.tables t ON ic.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
```

**Views:**

```sql
SELECT table_schema, table_name FROM information_schema.views
WHERE table_schema IN ('<schema_1>', '<schema_2>');
```

**Functions and procedures:**

```sql
SELECT s.name AS schema_name, o.name AS object_name, o.type_desc
FROM sys.objects o
JOIN sys.schemas s ON o.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>') AND o.type IN ('FN', 'IF', 'TF', 'P')
ORDER BY s.name, o.name;
```

**Triggers:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, tr.name AS trigger_name, tr.is_disabled
FROM sys.triggers tr
JOIN sys.tables t ON tr.parent_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>')
ORDER BY s.name, t.name;
```

**Synonyms:**

```sql
SELECT s.name AS schema_name, syn.name AS synonym_name, syn.base_object_name
FROM sys.synonyms syn
JOIN sys.schemas s ON syn.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
```

**Extended properties / comments:**

```sql
SELECT ep.class_desc, ep.name, ep.value,
       s.name AS schema_name, o.name AS object_name, c.name AS column_name
FROM sys.extended_properties ep
LEFT JOIN sys.objects o ON ep.major_id = o.object_id
LEFT JOIN sys.columns c ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
LEFT JOIN sys.schemas s ON o.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>')
ORDER BY s.name, o.name;
```

### B. Functional verification

**Query migrated tables:**

```sql
SELECT TOP 10 * FROM <schema_1>.<table_name>;
```

**Check row counts match source:**

```sql
SELECT COUNT(*) FROM <schema_1>.<table_name>;
-- Compare with source row count
```

**Test view:**

```sql
SELECT * FROM <schema_1>.<view_name>;
```

**Test stored procedure:**

```sql
EXEC <schema_1>.<procedure_name> <args>;
```

**Test trigger behavior:**

```sql
UPDATE <schema_1>.<table_name> SET <column> = '<test>' WHERE <pk_column> = 1;
-- Verify trigger fired (if applicable)
```

**Test synonym:**

```sql
SELECT * FROM <schema_1>.<synonym_name>;
```

**Verify permissions:**

```sql
-- Check role membership
SELECT dp.name AS role_name, mp.name AS member_name
FROM sys.database_role_members drm
JOIN sys.database_principals dp ON drm.role_principal_id = dp.principal_id
JOIN sys.database_principals mp ON drm.member_principal_id = mp.principal_id;
```

---

## STEP 9 — Clean temporary test data

After the demo, clean up temporary test objects from the target:

```sql
DROP SCHEMA <schema_1> CASCADE;
```

Reset the source fixture for the next run as applicable.

---

## STEP 10 — Record final E2E result

Document the outcome:

| Field | Value |
|---|---|
| Date | <date> |
| Run ID | <run-id> |
| Direction | Source → Target |
| Tables migrated | <count> |
| Rows migrated | <count> |
| Failed objects | <list or 0> |
| Errors | <list or none> |
| Success rate | <percentage> |
| Final status | SUCCESS / FAILED |

---

## Demo Scenario

> **Lead says:** "Run MSSQL end-to-end migration and show me that the objects actually migrated."

### 1. Connect source + target

Show that both database servers are reachable:

```sql
SELECT DB_NAME() AS database_name, SUSER_SNAME() AS current_user;
```

### 2. Prepare / verify source objects

Show the source contains the expected objects:

```sql
SELECT schema_name, table_name FROM information_schema.tables
WHERE table_schema = '<test_schema>' AND table_type = 'BASE TABLE';
```

### 3. Show source data

```sql
SELECT TOP 5 * FROM <test_schema>.<table_name>;
```

### 4. Run migration

```powershell
python -m migration_platform --config <config-file>.yaml --mode full --no-live-ui
```

### 5. Show migration report

The CLI output shows: Run ID, tables migrated / rows migrated, failed objects and errors, success percentage, final status.

### 6. Verify target tables / data

```sql
SELECT schema_name, table_name FROM information_schema.tables
WHERE table_schema = '<test_schema>';

SELECT COUNT(*) FROM <target-db>.<test_schema>.<table_name>;
```

### 7. Verify representative object types

| Object Type | Verification Query |
|---|---|
| Views | `SELECT * FROM <schema>.<view_name>;` |
| Functions | `SELECT <schema>.<function_name>();` |
| Procedures | `EXEC <schema>.<procedure_name> <args>;` |
| Triggers | `UPDATE <schema>.<table> SET ...;` — observe trigger behavior |
| Synonyms | `SELECT * FROM <schema>.<synonym_name>;` |
| Grants | Query `sys.database_permissions` for role/member mapping |

### 8. Demonstrate functional behavior

- **Views** return computed/aggregated data
- **Functions** return correct results
- **Stored procedures** execute and produce side effects
- **Triggers** auto-update columns on DML
- **Synonyms** resolve to underlying objects
- **Permissions** allow/deny access as configured

### 9. Show final success result

```text
Run ID: <run-id>
Tables migrated: <count>
Total rows: <count>
Migrated: <count>
Failed: 0
Success rate: 100%
Final status: SUCCESS
```

---

## Reference

- Object support matrix: `docs/mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/mssql/MSSQL_LIMITATIONS.md`
- Migration flow: `docs/mssql/MSSQL_MIGRATION_FLOW.md`
- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- Test guide: `docs/mssql/MSSQL_TEST_GUIDE.md`
- Local E2E config: `config/mssql_local_test.yaml`
