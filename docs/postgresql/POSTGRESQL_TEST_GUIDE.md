# PostgreSQL Object E2E Test Guide

This guide explains how to run the PostgreSQL object migration end-to-end test
using the reusable fixture in `tests/fixtures/postgresql_e2e/`. It is written
to be reusable for any PostgreSQL source/target database pair and any schema set.

## Prerequisites

- PostgreSQL 17.4 (or compatible) running and accessible
- Superuser or role with `CREATE DATABASE`, `CREATE SCHEMA`, `CREATE ROLE`,
  and object ownership privileges on both source and target
- `psql` client available in `PATH`
- Python 3.11+ with project dependencies installed (`pip install -e .`)

## Environment variables

The E2E config uses `password_secret` references resolved by the `env` secrets
provider. Set these before running the migration:

```bash
# Windows PowerShell
$env:SECRET_source_db_pass = "<source_password>"
$env:SECRET_target_db_pass = "<target_password>"

# Bash / Linux / macOS
export SECRET_source_db_pass=<source_password>
export SECRET_target_db_pass=<target_password>
```

## Configuration template

Create a migration config file (e.g., `config/my_postgres_e2e.yaml`):

```yaml
migration:
  mode: full
  include_schemas:
    - <schema_1>
    - <schema_2>
    # Add all schemas to migrate

source:
  host: <source_host>
  port: <source_port>
  database: <source_database>
  username: <source_username>
  password_secret: SECRET_source_db_pass
  ssl: false  # or true for cloud targets

target:
  host: <target_host>
  port: <target_port>
  database: <target_database>
  username: <target_username>
  password_secret: SECRET_target_db_pass
  ssl: true   # required for Azure PostgreSQL
```

## Step 1 — Prepare / reset the PostgreSQL test databases

Ensure both source and target databases exist. The migration platform creates
the target database automatically, but the source database must already exist.

```bash
# Create source database if it does not exist
export PGPASSWORD=<source_password>
psql -h <source_host> -p <source_port> -U <source_username> \
  -tc "SELECT 1 FROM pg_database WHERE datname = '<source_database>';" | grep -q 1 \
  || psql -h <source_host> -p <source_port> -U <source_username> \
       -c "CREATE DATABASE <source_database>;"
```

## Step 2 — Populate the source fixture

Use the reusable fixture scripts or your own DDL/data to populate the source
database with a representative test set. The fixture in
`tests/fixtures/postgresql_e2e/` creates:

- **Schemas:** `public`, `audit_test` (replace with your schema names)
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
- **Custom types:** ENUM + DOMAIN in `public` only

To use the built-in fixture:

```bash
# PowerShell
powershell -ExecutionPolicy Bypass -File tests\fixtures\postgresql_e2e\reset_source.ps1

# Bash
bash tests/fixtures/postgresql_e2e/reset_source.sh

# Or via Python
python tests/fixtures/postgresql_e2e/verify_migration.py --reset-source
```

For custom schemas, adapt the SQL scripts in `tests/fixtures/postgresql_e2e/`
to reference your schema names.

## Step 3 — Verify the source fixture

Connect to the source database and confirm expected objects exist:

```bash
psql -h <source_host> -p <source_port> -U <source_username> -d <source_database> -c "
    SELECT table_schema, table_name FROM information_schema.tables
    WHERE table_schema IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
    ORDER BY table_schema, table_name;
"
```

Also verify grants, sequences, RLS policies, and cross-schema FKs:

```sql
-- Grants
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND grantee = '<test_role>'
ORDER BY table_schema, table_name;

-- Sequence privileges
SELECT has_sequence_privilege('<test_role>', '<schema>.<sequence_name>', 'USAGE');
SELECT has_sequence_privilege('<test_role>', '<schema>.<sequence_name>', 'SELECT');

-- Cross-schema FKs
SELECT tc.constraint_name, kcu.column_name, ccu.table_schema, ccu.table_name AS ref_table
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
WHERE tc.table_schema = '<schema_2>' AND tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = '<schema_1>'
ORDER BY tc.constraint_name;

-- RLS policies
SELECT n.nspname, c.relname, pol.polname, pol.polcmd
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('<schema_1>', '<schema_2>');
```

## Step 4 — Run the migration

```bash
python -m migration_platform --config config/my_postgres_e2e.yaml --mode full --no-live-ui
```

The `--no-live-ui` flag suppresses the rich terminal dashboard and prints raw
JSON-like output suitable for CI logs. Omit it for the interactive UI.

After completion, the CLI prints the run ID, report paths, and final status.

## Step 5 — Verify the migration result

### 5a. Automatic verification

Run the bundled verification script (adapt for your schemas):

```bash
python tests/fixtures/postgresql_e2e/verify_migration.py
```

This checks schemas, tables, row counts, views, materialized views, functions,
procedures, triggers, sequences, indexes, comments, grants, RLS policies, and
cross-schema foreign keys.

### 5b. Manual verification queries

Connect to the **target** database and inspect key objects:

```sql
-- Schemas
SELECT schema_name FROM information_schema.schemata
WHERE schema_name IN ('<schema_1>', '<schema_2>');

-- Tables and row counts
SELECT table_schema, table_name,
       (SELECT COUNT(*) FROM pg_catalog.pg_class c
           JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
           WHERE n.nspname = table_schema AND c.relname = table_name) AS approx_rows
FROM information_schema.tables
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;

-- Cross-schema FK
SELECT tc.constraint_name, kcu.column_name, ccu.table_schema, ccu.table_name AS ref_table
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
WHERE tc.table_schema = '<schema_2>' AND tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = '<schema_1>'
ORDER BY tc.constraint_name;

-- Sequences and ownership
SELECT schemaname, sequencename FROM pg_sequences
WHERE schemaname IN ('<schema_1>', '<schema_2>')
ORDER BY schemaname, sequencename;

-- Sequence ownership verification
SELECT c.relname AS sequence_name, n.nspname AS schema_name,
       pg_get_serial_sequence(n.nspname || '.' || c.relname, 'customer_id') AS owned_by
FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('<schema_1>', '<schema_2>') AND c.relkind = 'S';

-- Views
SELECT table_schema, table_name FROM information_schema.views
WHERE table_schema IN ('<schema_1>', '<schema_2>');

-- Materialized views
SELECT schemaname, matviewname FROM pg_matviews
WHERE schemaname IN ('<schema_1>', '<schema_2>');

-- Functions and procedures
SELECT n.nspname, p.proname, p.prokind
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('<schema_1>', '<schema_2>') AND p.prokind IN ('f', 'p')
ORDER BY n.nspname, p.proname;

-- Triggers
SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('<schema_1>', '<schema_2>') AND NOT t.tgisinternal;

-- Comments
SELECT n.nspname, c.relname, d.description
FROM pg_description d
JOIN pg_class c ON d.objoid = c.oid AND d.objsubid = 0
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('<schema_1>', '<schema_2>')
ORDER BY n.nspname, c.relname;

-- Table grants
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema IN ('<schema_1>', '<schema_2>') AND grantee = '<test_role>'
ORDER BY table_schema, table_name;

-- Sequence grants (via ACL)
SELECT n.nspname, c.relname, r.rolname AS grantee, acl.privilege_type
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
JOIN aclexplode(c.relacl) acl ON true
JOIN pg_roles r ON r.oid = acl.grantee
WHERE n.nspname IN ('<schema_1>', '<schema_2>') AND c.relkind = 'S'
  AND r.rolname NOT IN ('PUBLIC')
ORDER BY n.nspname, c.relname, r.rolname;

-- RLS policies
SELECT n.nspname, c.relname, pol.polname, pol.polcmd
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('<schema_1>', '<schema_2>');
```

### 5c. Negative / error tests

After successful migration, verify these error conditions are handled:

1. **Duplicate migration (idempotency)** — Re-run the migration; it must not fail
   and must not duplicate objects/rows.

2. **Stale target rows** — Delete a row from source, re-run FULL mode; verify
   the row is NOT deleted from target (documented FULL mode behavior).

3. **Missing role** — Drop the grantee role on target, re-run grants phase;
   verify role is auto-created and grants applied.

4. **Invalid schema name** — Run migration with a non-existent schema in
   `include_schemas`; verify graceful error.

5. **Cross-schema FK collision** — Create same-named PK constraints in two
   schemas (e.g., `public.customers_pkey` and `cloud_test.customers_pkey`),
   add a FK in one schema referencing the table in the other; verify
   FK columns are not duplicated and `ref_schema` is correct.

## Step 6 — Run unit tests

```bash
python -m pytest tests/unit -q
```

Expected: **100 passed** (as of the current branch).

## Step 7 — Run integration tests (optional)

```bash
python -m pytest tests/integration -m integration -q
```

These require live database connections and are marked with `@pytest.mark.integration`.

## Troubleshooting

### `relation "xxx" does not exist` during migration

Ensure the source fixture was reset and all SQL scripts ran successfully.
Check for errors in the `psql` output from the reset script.

### `permission denied for schema <schema_name>`

The test role must exist and have `USAGE` on all included schemas.
Re-run the role/setup SQL script for your fixture.

### `wal_level` CDC errors

CDC requires `wal_level = logical` on the source PostgreSQL instance.
This is an environment prerequisite, not a migration failure.
See `docs/postgresql/POSTGRESQL_LIMITATIONS.md`.

### `create_type` skips non-public types

This is a documented limitation. Types are created only in the `public` schema
because `create_type()` hardcodes the public schema in its existence check.
See `docs/postgresql/POSTGRESQL_LIMITATIONS.md`.

### Cross-schema FK duplicate columns / wrong ref_schema

If you see duplicated FK columns or incorrect `ref_schema`, verify the
source connector's FK query includes these schema-qualified joins:

```sql
JOIN information_schema.referential_constraints rc
  ON tc.constraint_name = rc.constraint_name
  AND tc.constraint_schema = rc.constraint_schema          -- REQUIRED
JOIN information_schema.constraint_column_usage ccu
  ON rc.unique_constraint_name = ccu.constraint_name
  AND rc.unique_constraint_schema = ccu.constraint_schema  -- REQUIRED
```

These were added in `core/connectors/postgresql.py` to fix a regression where
same-named PK constraints in multiple schemas caused duplicate FK columns and
wrong `ref_schema` values. See `TestPostgresCrossSchemaMetadataIsolation.test_get_schema_fk_no_duplicate_columns_under_cross_schema_pk_collision`.

## Reference

- Fixture README: `tests/fixtures/postgresql_e2e/README.md`
- Fixture SQL: `tests/fixtures/postgresql_e2e/`
- Migration config template: `config/postgresql_object_e2e.yaml`
- Object support matrix: `docs/postgresql/POSTGRESQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/postgresql/POSTGRESQL_LIMITATIONS.md`
- Local audit report: `docs/postgresql/POSTGRESQL_LOCAL_AUDIT_PHASE_1.md`
- Cloud audit report: `docs/postgresql/POSTGRESQL_CLOUD_AUDIT.md`