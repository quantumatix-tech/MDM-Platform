# PostgreSQL Object E2E Test Guide

This guide explains how to run the PostgreSQL object migration end-to-end test
using the reusable fixture in `tests/fixtures/postgresql_e2e/`.

## Prerequisites

- PostgreSQL 17.4 (or compatible) running on `127.0.0.1:55432`
- `postgres` superuser with password `postgres`
- `psql` client available in `PATH`
- Python 3.11+ with project dependencies installed (`pip install -e .`)

## Environment variables

The E2E config uses `password_secret` references resolved by the `env` secrets
provider.  Set these before running the migration:

```bash
# Windows PowerShell
$env:SECRET_source_db_pass = "postgres"
$env:SECRET_target_db_pass = "postgres"

# Bash / Linux / macOS
export SECRET_source_db_pass=postgres
export SECRET_target_db_pass=postgres
```

## Step 1 — Prepare / reset the PostgreSQL test databases

Ensure both `migration_source` and `migration_target` exist.  The migration
platform creates `migration_target` automatically, but `migration_source` must
already exist.

```bash
# Create source database if it does not exist
export PGPASSWORD=postgres
psql -h 127.0.0.1 -p 55432 -U postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'migration_source';" | grep -q 1 || psql -h 127.0.0.1 -p 55432 -U postgres -c "CREATE DATABASE migration_source;"
```

## Step 2 — Reset the source fixture

Reset `migration_source` to the clean fixture state:

```bash
# PowerShell
powershell -ExecutionPolicy Bypass -File tests\fixtures\postgresql_e2e\reset_source.ps1

# Bash
bash tests/fixtures/postgresql_e2e/reset_source.sh
```

Or via Python:

```bash
python tests/fixtures/postgresql_e2e/verify_migration.py --reset-source
```

## Step 3 — Verify the source fixture

Connect to `migration_source` and confirm objects exist:

```bash
psql -h 127.0.0.1 -p 55432 -U postgres -d migration_source -c "
    SELECT table_schema, table_name FROM information_schema.tables
    WHERE table_schema IN ('public', 'audit_test') AND table_type = 'BASE TABLE'
    ORDER BY table_schema, table_name;
"
```

Expected tables:
- `public.customers`
- `public.products`
- `public.orders`
- `audit_test.test_customers`
- `audit_test.test_orders`
- `audit_test.fk_parent`
- `audit_test.fk_child`
- `audit_test.procedure_test_log`

## Step 4 — Run the migration

```bash
python -m migration_platform --config config/postgresql_object_e2e.yaml --mode full --no-live-ui
```

The `--no-live-ui` flag suppresses the rich terminal dashboard and prints raw
JSON-like output suitable for CI logs.  Omit it for the interactive UI.

After completion, the CLI prints the run ID, report paths, and final status.

## Step 5 — Verify the migration result

### 5a. Automatic verification

Run the bundled verification script:

```bash
python tests/fixtures/postgresql_e2e/verify_migration.py
```

This checks schemas, tables, row counts, views, materialized views, functions,
procedures, triggers, sequences, indexes, comments, grants, RLS policies, and
cross-schema foreign keys.

### 5b. Manual verification queries

Connect to `migration_target` and inspect key objects:

```sql
-- Schemas
SELECT schema_name FROM information_schema.schemata
WHERE schema_name IN ('public', 'audit_test');

-- Tables and row counts
SELECT table_schema, table_name, (SELECT COUNT(*) FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = table_schema AND c.relname = table_name) AS approx_rows
FROM information_schema.tables
WHERE table_schema IN ('public', 'audit_test') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;

-- Cross-schema FK
SELECT tc.constraint_name, kcu.column_name, ccu.table_schema, ccu.table_name AS ref_table
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
WHERE tc.table_schema = 'audit_test' AND tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = 'public'
ORDER BY tc.constraint_name;

-- Sequences and ownership
SELECT schemaname, sequencename FROM pg_sequences
WHERE schemaname IN ('public', 'audit_test')
ORDER BY schemaname, sequencename;

-- Views
SELECT table_schema, table_name FROM information_schema.views
WHERE table_schema IN ('public', 'audit_test');

-- Materialized views
SELECT schemaname, matviewname FROM pg_matviews
WHERE schemaname IN ('public', 'audit_test');

-- Functions and procedures
SELECT n.nspname, p.proname, p.prokind
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('public', 'audit_test') AND p.prokind IN ('f', 'p')
ORDER BY n.nspname, p.proname;

-- Triggers
SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('public', 'audit_test') AND NOT t.tgisinternal;

-- Comments
SELECT n.nspname, c.relname, d.description
FROM pg_description d
JOIN pg_class c ON d.objoid = c.oid AND d.objsubid = 0
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('public', 'audit_test')
ORDER BY n.nspname, c.relname;

-- Grants
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema IN ('public', 'audit_test') AND grantee = 'audit_user'
ORDER BY table_schema, table_name;

-- RLS policies
SELECT n.nspname, c.relname, pol.polname, pol.polcmd
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('public', 'audit_test');
```

## Step 6 — Run unit tests

```bash
python -m pytest tests/unit -q
```

Expected: **94 passed**.

## Step 7 — Run integration tests (optional)

```bash
python -m pytest tests/integration -m integration -q
```

These require live database connections and are marked with `@pytest.mark.integration`.

## Troubleshooting

### `relation "xxx" does not exist` during migration
Ensure the source fixture was reset and all SQL scripts ran successfully.
Check for errors in the `psql` output from the reset script.

### `permission denied for schema audit_test`
The `audit_user` role must exist and have `USAGE` on both schemas.
Re-run `00_setup_role.sql`.

### `wal_level` CDC errors
CDC requires `wal_level = logical` on the source PostgreSQL instance.
This is an environment prerequisite, not a migration failure.
See `docs/postgresql/POSTGRESQL_LIMITATIONS.md`.

### `create_type` skips non-public types
This is a documented limitation.  Types are created only in the `public` schema
because `create_type()` hardcodes the public schema in its existence check.
See `docs/postgresql/POSTGRESQL_LIMITATIONS.md`.

## Reference

- Fixture README: `tests/fixtures/postgresql_e2e/README.md`
- Fixture SQL: `tests/fixtures/postgresql_e2e/`
- Migration config: `config/postgresql_object_e2e.yaml`
- Object support matrix: `docs/postgresql/POSTGRESQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/postgresql/POSTGRESQL_LIMITATIONS.md`
- Final audit report: `docs/postgresql/POSTGRESQL_LOCAL_AUDIT_PHASE_1.md`
