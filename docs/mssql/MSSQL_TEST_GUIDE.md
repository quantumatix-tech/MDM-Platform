# MSSQL Object E2E Test Guide

This guide explains how to run the MSSQL object migration end-to-end test.
It is written to be reusable for any MSSQL source/target database pair and any schema set.

## Prerequisites

- Microsoft SQL Server (local or cloud) running and accessible
- `sa` account or role with `CREATE DATABASE`, `CREATE SCHEMA`, `CREATE TABLE`,
  and object ownership privileges on both source and target
- `sqlcmd` or `psql`-equivalent client available in `PATH`
- Python 3.11+ with project dependencies installed (`pip install -e .`)
- ODBC Driver 18 for SQL Server (required for MSSQL connectivity)

---

## Secret environment variables

The E2E config uses `password_secret` references resolved by the `env` secrets
provider. Set these before running the migration:

```powershell
# Windows PowerShell
$env:SECRET_mssql_source_pass = "<source_password>"
$env:SECRET_mssql_target_pass = "<target_password>"
```

---

## Configuration template

Create a migration config file (e.g., `config/my_mssql_e2e.yaml`):

```yaml
migration:
  mode: full
  include_schemas:
    - <schema_1>
    - <schema_2>

source:
  engine: mssql
  connection:
    host: <source_host>
    port: <source_port>
    database: <source_database>
    username: <source_username>
    password_secret: SECRET_mssql_source_pass
    ssl: false

target:
  engine: mssql
  connection:
    host: <target_host>
    port: <target_port>
    database: <target_database>
    username: <target_username>
    password_secret: SECRET_mssql_target_pass
    ssl: false

secrets:
  provider: env
```

---

## Step 1 — Prepare the MSSQL test databases

Ensure both source and target databases exist. The migration platform creates
the target database automatically, but the source database must already exist.

```powershell
# Create source database if it does not exist (adapt for your environment)
sqlcmd -S <source_host>,<source_port> -U <source_username> -Q "CREATE DATABASE <source_database>;"
```

---

## Step 2 — Populate the source

Use your own DDL/data or a fixture to populate the source database with a
representative test set. The source should include:

- **Schemas:** `<schema_1>`, `<schema_2>`
- **Tables:** With data, constraints, indexes, identity columns, computed columns
- **Views:** Schema-qualified
- **Functions / Procedures:** T-SQL, schema-qualified
- **Triggers:** AFTER / INSTEAD OF triggers
- **Synonyms:** Schema-qualified
- **UDTs:** XML, JSON, VARBINARY, UNIQUEIDENTIFIER, SQL_VARIANT
- **Partitioning:** Partition functions and schemes
- **Users / Roles / Permissions:** Database roles with appropriate grants
- **Foreign keys:** Including cross-schema FKs

---

## Step 3 — Verify the source

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
ORDER BY fs.name, ft.name;
```

**Identity columns:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name
FROM sys.identity_columns ic
JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
JOIN sys.tables t ON ic.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
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

---

## Step 4 — Run the migration

```powershell
python -m migration_platform --config config/my_mssql_e2e.yaml --mode full --no-live-ui
```

After completion, the CLI prints the run ID, report paths, and final status.
Reports are saved under `reports/<run_id>.html` and `reports/<run_id>.json`.

---

## Step 5 — Verify the migration result

### 5a. Structural verification

Connect to the **target** database and inspect key objects:

**Schemas:**

```sql
SELECT schema_name FROM information_schema.schemata
WHERE schema_name IN ('<schema_1>', '<schema_2>');
```

**Tables and row counts:**

```sql
SELECT schema_name, table_name FROM information_schema.tables
WHERE schema_name IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
ORDER BY schema_name, table_name;
```

**Primary keys:**

```sql
SELECT tc.table_schema, tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema IN ('<schema_1>', '<schema_2>') AND tc.constraint_type = 'PRIMARY KEY';
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
WHERE fs.name IN ('<schema_1>', '<schema_2>');
```

**Indexes:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, i.name AS index_name
FROM sys.indexes i
JOIN sys.tables t ON i.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
```

**Identity columns:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name
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
WHERE s.name IN ('<schema_1>', '<schema_2>') AND o.type IN ('FN', 'IF', 'TF', 'P');
```

**Triggers:**

```sql
SELECT s.name AS schema_name, t.name AS table_name, tr.name AS trigger_name, tr.is_disabled
FROM sys.triggers tr
JOIN sys.tables t ON tr.parent_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
```

**Synonyms:**

```sql
SELECT s.name AS schema_name, syn.name AS synonym_name, syn.base_object_name
FROM sys.synonyms syn
JOIN sys.schemas s ON syn.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
```

**Extended properties:**

```sql
SELECT ep.class_desc, ep.name, ep.value, s.name AS schema_name, o.name AS object_name
FROM sys.extended_properties ep
LEFT JOIN sys.objects o ON ep.major_id = o.object_id
LEFT JOIN sys.schemas s ON o.schema_id = s.schema_id
WHERE s.name IN ('<schema_1>', '<schema_2>');
```

### 5b. Data validation

**Row counts match:**

Compare source and target row counts for each table:

```sql
-- On target
SELECT schema_name, table_name, COUNT(*) AS target_row_count
FROM <schema_1>.<table_name>
GROUP BY schema_name, table_name;

-- Compare with source row counts
```

**FK enforcement test:**

```sql
-- Attempt an invalid insert (should fail with FK violation)
INSERT INTO <schema_1>.<ref_table> (pk_column) VALUES (999999);
-- Expected: Foreign key constraint violation
```

### 5c. Negative / error tests

After successful migration, verify these error conditions are handled:

1. **Duplicate migration (idempotency)** — Re-run the migration; it must not fail
   and must not duplicate rows (UPSERT/MERGE should update existing rows).

2. **Invalid schema name** — Run migration with a non-existent schema in
   `include_schemas`; verify graceful error.

3. **Per-object failure isolation** — Create a source table with invalid DDL;
   verify other objects still migrate and the failure is recorded without stopping the run.

4. **Cross-schema FK collision** — Create same-named PK constraints in two schemas,
   add a FK in one schema referencing the table in the other; verify
   FK columns are correct and `ref_schema` is accurate.

---

## Step 6 — Run unit tests

```powershell
python -m pytest tests/unit -q
```

Expected: All existing unit tests pass (including MSSQL connector tests).

---

## Step 7 — Run integration tests (optional)

Integration tests requiring live MSSQL connections are marked with
`@pytest.mark.integration`. Run with:

```powershell
python -m pytest tests/integration -m integration -q
```

---

## Troubleshooting

### Connection failures

- Verify SQL Server is running and accessible on the configured host/port
- Confirm `SECRET_mssql_source_pass` and `SECRET_mssql_target_pass` environment variables are set
- Check that the user has required permissions on both source and target

### ODBC Driver not found

Install ODBC Driver 18 for SQL Server:

- **Windows:** `choco install msodbcsql18`
- **macOS:** `brew tap microsoft/mssql-release && brew install msodbcsql18`
- **Linux:** See Microsoft documentation

### Object already exists errors

The migration uses `IF NOT EXISTS` patterns. If objects were partially created in a previous run, reset the target database before re-running.

### Foreign key constraint violations during migration

Verify dependency ordering — tables referenced by FKs must be created and populated before the referencing tables. Check that the `include_schemas` list is complete.

---

## Adding Local → Cloud testing

When conducting MSSQL Local → Cloud (Azure SQL) testing:

1. Create a separate config file (e.g., `config/mssql_local_cloud.yaml`)
2. Set `SECRET_mssql_source_pass` (local) and `SECRET_mssql_target_pass` (Azure)
3. Enable `ssl: true` for Azure connections
4. Run the migration and document results in `docs/mssql/MSSQL_CLOUD_AUDIT.md`
5. Update `MSSQL_OBJECT_SUPPORT_MATRIX.md` and `MSSQL_MIGRATION_FLOW.md`
6. Azure-specific considerations: SSL required, elastic pool limits, managed identity options

## Adding Cloud → Local testing

1. Create a separate config file (e.g., `config/mssql_cloud_to_local.yaml`)
2. Set `SECRET_mssql_source_pass` (Azure) and `SECRET_mssql_target_pass` (local)
3. Run the migration and document results in `docs/mssql/MSSQL_CLOUD_TO_LOCAL_AUDIT.md`
4. Update support matrix and migration flow documents

---

## Reference

- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- Object support matrix: `docs/mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/mssql/MSSQL_LIMITATIONS.md`
- Migration flow: `docs/mssql/MSSQL_MIGRATION_FLOW.md`
- E2E runbook: `docs/mssql/MSSQL_E2E_RUNBOOK.md`
- Local E2E config: `config/mssql_local_test.yaml`
