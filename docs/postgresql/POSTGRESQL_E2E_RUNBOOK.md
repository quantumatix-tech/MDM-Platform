# PostgreSQL E2E Runbook / Demo Guide

Reusable guide for running a PostgreSQL end-to-end migration demo.
This document answers: "If my lead asks me to run a PostgreSQL end-to-end migration demo, what exactly do I do?"

The E2E test setup uses the repository fixture in `tests/fixtures/postgresql_e2e/` which creates representative PostgreSQL objects including tables, data, constraints, indexes, sequences, views, materialized views, functions, procedures, triggers, comments, grants and RLS.

---

## STEP 1 — Prepare / connect source database

Ensure the source database server is running and accessible.

```bash
# Verify connection (use your actual connection details)
psql -h <source-host> -p <source-port> -U <source-user> -d <source-database> -c "SELECT version();"
```

---

## STEP 2 — Prepare / connect target database

Ensure the target database server is running and accessible. The migration platform creates the target database automatically if it does not exist.

```bash
# Verify connection (use your actual connection details)
psql -h <target-host> -p <target-port> -U <target-user> -d <target-database> -c "SELECT version();"
```

---

## STEP 3 — Decide: existing schema or fresh E2E test schema

Choose one of:

**Option A — Existing schema/object set:**

Use an existing database and schema that already contains the objects you want to migrate. Skip to Step 5 after verifying source objects exist.

**Option B — Fresh E2E test schema (recommended for demo):**

Use the reusable fixture in `tests/fixtures/postgresql_e2e/` which creates a deterministic source database containing:

- **Schemas:** `public`, `audit_test`
- **Tables:** 3 in `public`, 4 in `audit_test` (including cross-schema FK targets)
- **Data:** ~37 rows across 7 tables
- **Sequences:** SERIAL and explicit sequences with ownership
- **Indexes:** B-tree and unique indexes
- **Views & Materialized Views:** 1 each per schema
- **Functions & Procedures:** SQL and PL/pgSQL, including trigger functions
- **Triggers:** BEFORE UPDATE triggers on tables
- **Comments:** On schemas, tables, columns, views, functions, sequences
- **Grants:** Table, column, sequence, schema, and function grants to a test role
- **RLS / Policies:** ENABLE ROW LEVEL SECURITY + permissive policies

Reset the source fixture:

```bash
bash tests/fixtures/postgresql_e2e/reset_source.sh
```

---

## STEP 4 — Create / prepare test objects

If using the fixture (Option B), the reset script in Step 3 already creates all test objects in the correct dependency order:

1. Roles → 2. Drop objects → 3. Types → 4. Tables → 5. Data → 6. Indexes → 7. Views → 8. Functions/Procedures → 9. Triggers → 10. Comments → 11. Grants → 12. RLS

If using your own schema (Option A), ensure your source database has a representative mix of PostgreSQL objects. The E2E test setup should include at minimum:

- Tables with primary keys, unique constraints, check constraints, and foreign keys
- Data in tables (for row count verification)
- Indexes (B-tree and unique)
- Sequences with ownership
- Views and materialized views
- Functions and procedures
- Triggers
- Comments on schemas, tables, columns
- Grants (schema, table, column, sequence, function/procedure EXECUTE)
- RLS policies

Create a migration config file:

```yaml
migration:
  mode: full
  include_schemas:
    - <schema_1>
    - <schema_2>

source:
  host: <source-host>
  port: <source-port>
  database: <source-database>
  username: <source-username>
  password_secret: SECRET_source_db_pass
  ssl: false

target:
  host: <target-host>
  port: <target-port>
  database: <target-database>
  username: <target-username>
  password_secret: SECRET_target_db_pass
  ssl: false
```

Set secrets:

```bash
# Windows PowerShell
$env:SECRET_source_db_pass = "<source_password>"
$env:SECRET_target_db_pass = "<target_password>"

# Bash / Linux / macOS
export SECRET_source_db_pass=<source_password>
export SECRET_target_db_pass=<target_password>
```

---

## STEP 5 — Verify the source before migration

Connect to the source database and confirm expected objects exist:

```bash
psql -h <source-host> -p <source-port> -U <source-user> -d <source-database> -c "
    SELECT table_schema, table_name FROM information_schema.tables
    WHERE table_schema IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
    ORDER BY table_schema, table_name;
"
```

Also verify:

**Row counts:**

```sql
SELECT table_schema, table_name,
       (SELECT COUNT(*) FROM <schema_1>.<table_name>) AS row_count
FROM information_schema.tables
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;
```

**Definitions, constraints, indexes:**

```sql
-- Constraints
SELECT tc.table_schema, tc.table_name, tc.constraint_type, tc.constraint_name
FROM information_schema.table_constraints tc
WHERE tc.table_schema IN ('<schema_1>', '<schema_2>')
ORDER BY tc.table_schema, tc.table_name, tc.constraint_name;

-- Indexes
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname IN ('<schema_1>', '<schema_2>')
ORDER BY schemaname, tablename, indexname;
```

**Privileges:**

```sql
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND grantee = '<test_role>'
ORDER BY table_schema, table_name;

SELECT has_sequence_privilege('<test_role>', '<schema>.<sequence_name>', 'USAGE');
SELECT has_sequence_privilege('<test_role>', '<schema>.<sequence_name>', 'SELECT');
```

**RLS / policies:**

```sql
SELECT n.nspname, c.relname, pol.polname, pol.polcmd
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('<schema_1>', '<schema_2>');
```

---

## STEP 6 — Run the migration

```bash
python -m migration_platform --config <config-file>.yaml --mode full --no-live-ui
```

Use `--no-live-ui` for CI/log-friendly output. Omit it for the interactive UI.

Where `<config-file>` is the path to your migration YAML (e.g., `config/postgresql_object_e2e.yaml`).

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

Example output:

```text
Run ID: <run-id>
Mode: FULL
Tables migrated: 8
Total rows: 37
Migrated: 37
Failed: 0
Success rate: 100%
Final status: SUCCESS
```

---

## STEP 8 — Verify the target

### A. Structural verification

Connect to the target database and confirm objects were created correctly:

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

**Constraints:**

```sql
SELECT tc.table_schema, tc.table_name, tc.constraint_type, tc.constraint_name
FROM information_schema.table_constraints tc
WHERE tc.table_schema IN ('<schema_1>', '<schema_2>')
ORDER BY tc.table_schema, tc.table_name, tc.constraint_name;
```

**Indexes:**

```sql
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE schemaname IN ('<schema_1>', '<schema_2>')
ORDER BY schemaname, tablename, indexname;
```

**Sequences:**

```sql
SELECT schemaname, sequencename FROM pg_sequences
WHERE schemaname IN ('<schema_1>', '<schema_2>')
ORDER BY schemaname, sequencename;
```

**Views and materialized views:**

```sql
SELECT table_schema, table_name FROM information_schema.views
WHERE table_schema IN ('<schema_1>', '<schema_2>');

SELECT schemaname, matviewname FROM pg_matviews
WHERE schemaname IN ('<schema_1>', '<schema_2>');
```

**Functions and procedures:**

```sql
SELECT n.nspname, p.proname, p.prokind
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('<schema_1>', '<schema_2>') AND p.prokind IN ('f', 'p')
ORDER BY n.nspname, p.proname;
```

**Triggers:**

```sql
SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('<schema_1>', '<schema_2>') AND NOT t.tgisinternal;
```

**Grants:**

```sql
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND grantee = '<test_role>'
ORDER BY table_schema, table_name;
```

**RLS policies:**

```sql
SELECT n.nspname, c.relname, pol.polname, pol.polcmd
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('<schema_1>', '<schema_2>');
```

### B. Functional verification

Test that migrated objects actually work:

**Query migrated tables:**

```sql
SELECT * FROM <schema_1>.<table_name> LIMIT 10;
```

**Check row counts match source:**

```sql
SELECT COUNT(*) FROM <schema_1>.<table_name>;
-- Compare with source row count
```

**Test view:**

```sql
SELECT * FROM <schema_1>.<view_name> LIMIT 10;
```

**Query materialized view:**

```sql
SELECT * FROM <schema_1>.<matview_name>;
```

**Call function:**

```sql
SELECT <schema_1>.<function_name>();
```

**Call procedure:**

```sql
CALL <schema_1>.<procedure_name>(<args>);
```

**Test trigger behavior:**

```sql
UPDATE <schema_1>.<table_name> SET <column> = '<test>' WHERE <pk_column> = 1;
-- Verify trigger fired (e.g., updated_at changed)
```

**Verify grants:**

```sql
SET ROLE <test_role>;
SELECT * FROM <schema_1>.<table_name> LIMIT 1;
-- Verify expected access / denial
```

**Test RLS filtering:**

```sql
SET ROLE <test_role>;
SELECT * FROM <schema_1>.<table_name>;
-- Verify only expected rows are returned based on policy
```

---

## STEP 9 — Clean temporary test data

After the demo, clean up temporary test objects from the target:

```sql
DROP SCHEMA <schema_1> CASCADE;
DROP ROLE <test_role>;
```

If using the fixture, the source database can also be reset for the next run:

```bash
bash tests/fixtures/postgresql_e2e/reset_source.sh
```

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

> **Lead says:** "Run PostgreSQL end-to-end migration and show me that the objects actually migrated."

Here is exactly what to demonstrate:

### 1. Connect source + target

Show that both database servers are reachable:

```bash
psql -h <source-host> -p <source-port> -U <source-user> -d <source-database> -c "SELECT current_database(), current_user;"
psql -h <target-host> -p <target-port> -U <target-user> -d <target-database> -c "SELECT current_database(), current_user;"
```

### 2. Prepare / verify source objects

Show the source contains the expected objects:

```sql
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema = '<test_schema>' AND table_type = 'BASE TABLE';
```

### 3. Show source data

```sql
SELECT * FROM <test_schema>.<table_name> LIMIT 5;
```

### 4. Run migration

```bash
python -m migration_platform --config <config-file>.yaml --mode full --no-live-ui
```

### 5. Show migration report

The CLI output shows:

- Run ID
- Tables migrated / rows migrated
- Failed objects and errors
- Success percentage
- Final status (SUCCESS)

### 6. Verify target tables / data

```sql
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema = '<test_schema>';

SELECT COUNT(*) FROM <target-db>.<test_schema>.<table_name>;
```

### 7. Verify representative object types

Show that the migration handled multiple object types:

| Object Type | Verification Query |
|---|---|
| Views | `SELECT * FROM <schema>.<view_name>;` |
| Materialized views | `SELECT * FROM <schema>.<matview_name>;` |
| Function | `SELECT <schema>.<function_name>();` |
| Procedure | `CALL <schema>.<procedure_name>(<args>);` |
| Trigger | `UPDATE <schema>.<table> SET ...;` — observe trigger behavior |
| Sequences | `SELECT * FROM pg_sequences WHERE schemaname = '<schema>';` |
| Grants | `SELECT grantee, privilege_type FROM information_schema.role_table_grants WHERE grantee = '<role>';` |
| RLS | `SET ROLE <role>; SELECT * FROM <schema>.<table>;` — observe filtered rows |

### 8. Demonstrate functional behavior

- **View** returns computed/aggregated data
- **Function** returns correct result (e.g., row count)
- **Procedure** inserts a log row when called
- **Trigger** auto-updates timestamp on UPDATE
- **RLS** filters rows for the reader role
- **Grants** allow/deny access as configured

### 9. Show final success result

Summarize with the migration report:

```text
Run ID: <run-id>
Tables migrated: 8
Total rows: 37
Migrated: 37
Failed: 0
Success rate: 100%
Final status: SUCCESS
```

---

## Reference

- Object support matrix: `docs/postgresql/POSTGRESQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/postgresql/POSTGRESQL_LIMITATIONS.md`
- Migration flow: `docs/postgresql/POSTGRESQL_MIGRATION_FLOW.md`
- Local audit report: `docs/postgresql/POSTGRESQL_LOCAL_AUDIT_PHASE_1.md`
- Cloud audit report: `docs/postgresql/POSTGRESQL_CLOUD_AUDIT.md`
- Fixture README: `tests/fixtures/postgresql_e2e/README.md`
- Fixture SQL: `tests/fixtures/postgresql_e2e/`
- Local E2E config: `config/postgresql_object_e2e.yaml`
- Cloud E2E config: `config/postgresql_cloud_test.yaml`
- Cloud→Local config: `config/postgresql_cloud_to_local.yaml`