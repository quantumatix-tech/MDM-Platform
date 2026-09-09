# PostgreSQL E2E Audit Fixture

Reproducible PostgreSQL source database for end-to-end migration testing of
all supported PostgreSQL object types on the `feature/postgresql-objects` branch.

## What the fixture creates

### Schemas
- `public` — standard schema with core tables, types, views, and functions
- `audit_test` — non-public schema for schema-qualification testing

### Supported object coverage
| Object | public | audit_test | Notes |
|---|---|---|---|
| Tables | 3 | 4 | customers, products, orders + test tables |
| Primary keys | 3 | 4 | SERIAL-based |
| Foreign keys | 2 | 3 | Includes cross-schema FK (audit_test → public) |
| Unique constraints | 2 | 1 | customers.email, products.name, test_customers.email |
| Check constraints | 4 | 0 | price >= 0, stock_qty >= 0, quantity > 0, product_category domain |
| Defaults | multiple | multiple | Including enum and domain defaults |
| ENUM type | 1 | 0 | `customer_status` (public only — documented limitation) |
| DOMAIN type | 1 | 0 | `product_category` (public only — documented limitation) |
| Sequences | 3 | 2 | Discovered via `pg_sequences`, ownership synced |
| Indexes | 5 | 3 | Including unique index |
| Views | 1 | 1 | Schema-qualified |
| Materialized views | 1 | 1 | Schema-qualified, refreshed |
| Functions | 1 | 1 | SQL and PL/pgSQL |
| Procedures | 1 | 1 | IN parameter, writes to log table |
| Triggers | 1 | 1 | BEFORE UPDATE, schema-qualified |
| Trigger functions | 1 | 1 | PL/pgSQL timestamp updater |
| Comments | 15+ | 8+ | Schema, table, column, view, matview, function, sequence |
| Grants | 10+ | 6+ | Schema, table, column, sequence, function |
| RLS + policies | 1 table + 2 policies | 1 table + 2 policies | ENABLE ROW LEVEL SECURITY + permissive policies |

### Deterministic data
- **public.customers**: 5 rows
- **public.products**: 10 rows
- **public.orders**: 12 rows
- **audit_test.test_customers**: 3 rows (with `public_customer_id` cross-schema FK)
- **audit_test.test_orders**: 2 rows
- **audit_test.fk_parent**: 2 rows
- **audit_test.fk_child**: 3 rows (cross-schema to `public.customers`)

## Execution order

Run the scripts in this exact order:

1. `00_setup_role.sql` — creates `audit_user` role
2. `01_drop_objects.sql` — drops existing objects for clean reset
3. `02_create_types.sql` — ENUM and DOMAIN (public schema only)
4. `03_public_tables.sql` — public schema tables with constraints
5. `04_audit_test_tables.sql` — non-public schema tables with constraints
6. `05_seed_data.sql` — deterministic row data
7. `06_create_indexes.sql` — indexes after tables
8. `07_create_views.sql` — views and materialized views after tables
9. `08_create_functions.sql` — functions, procedures, trigger functions
10. `09_create_triggers.sql` — triggers after functions exist
11. `10_apply_comments.sql` — comments after all objects exist
12. `11_apply_grants.sql` — grants after all objects exist
13. `12_apply_rls.sql` — RLS policies after tables exist

## How to reset/recreate

### PowerShell (Windows)
```powershell
$env:PGPASSWORD = 'postgres'
$scripts = @(
    'tests\fixtures\postgresql_e2e\00_setup_role.sql',
    'tests\fixtures\postgresql_e2e\01_drop_objects.sql',
    'tests\fixtures\postgresql_e2e\02_create_types.sql',
    'tests\fixtures\postgresql_e2e\03_public_tables.sql',
    'tests\fixtures\postgresql_e2e\04_audit_test_tables.sql',
    'tests\fixtures\postgresql_e2e\05_seed_data.sql',
    'tests\fixtures\postgresql_e2e\06_create_indexes.sql',
    'tests\fixtures\postgresql_e2e\07_create_views.sql',
    'tests\fixtures\postgresql_e2e\08_create_functions.sql',
    'tests\fixtures\postgresql_e2e\09_create_triggers.sql',
    'tests\fixtures\postgresql_e2e\10_apply_comments.sql',
    'tests\fixtures\postgresql_e2e\11_apply_grants.sql',
    'tests\fixtures\postgresql_e2e\12_apply_rls.sql'
)
foreach ($s in $scripts) {
    psql -h 127.0.0.1 -p 55432 -U postgres -d migration_source -f $s
}
```

### Bash (Linux/macOS/WSL)
```bash
export PGPASSWORD=postgres
for s in tests/fixtures/postgresql_e2e/0*.sql; do
    psql -h 127.0.0.1 -p 55432 -U postgres -d migration_source -f "$s"
done
```

### Python (in-place reset)
```bash
python tests/fixtures/postgresql_e2e/verify_migration.py --reset-source
```

## Expected schemas

- `public`
- `audit_test`

## Expected objects

Run the following queries against `migration_source` after setup:

```sql
-- Tables
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema IN ('public', 'audit_test') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;

-- Views
SELECT table_schema, table_name FROM information_schema.views
WHERE table_schema IN ('public', 'audit_test')
ORDER BY table_schema, table_name;

-- Materialized views
SELECT schemaname, matviewname FROM pg_matviews
WHERE schemaname IN ('public', 'audit_test')
ORDER BY schemaname, matviewname;

-- Functions + Procedures
SELECT n.nspname, p.proname, p.prokind
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('public', 'audit_test') AND p.prokind IN ('f', 'p')
ORDER BY n.nspname, p.proname;

-- Triggers
SELECT n.nspname, c.relname, t.tgname
FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname IN ('public', 'audit_test') AND NOT t.tgisinternal
ORDER BY n.nspname, c.relname, t.tgname;

-- Sequences
SELECT schemaname, sequencename FROM pg_sequences
WHERE schemaname IN ('public', 'audit_test')
ORDER BY schemaname, sequencename;

-- Indexes
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE schemaname IN ('public', 'audit_test') AND indexname NOT LIKE '%_pkey'
ORDER BY schemaname, tablename, indexname;
```

## Expected row counts

| Schema | Table | Expected rows |
|---|---|---|
| public | customers | 5 |
| public | products | 10 |
| public | orders | 12 |
| audit_test | test_customers | 3 |
| audit_test | test_orders | 2 |
| audit_test | fk_parent | 2 |
| audit_test | fk_child | 3 |
| audit_test | procedure_test_log | 0 |

## How it maps to the migration YAML

The fixture is designed for use with `config/postgresql_object_e2e.yaml`:

```yaml
migration:
  mode: full
  include_schemas:
    - public
    - audit_test
```

The `include_schemas` list controls which schemas the source connector discovers
and the target connector creates.  All objects in the fixture live within these
two schemas, so a single migration run migrates the complete fixture.

## Limitations

- **Custom types** (`customer_status`, `product_category`) are created only in
  the `public` schema because `create_type()` currently hardcodes the public
  schema in its existence check.  Non-public type migration is a documented
  limitation.
- **Partitions** are not included because `create_partition()` has a similar
  public-schema-only existence check and does not schema-qualify the
  `PARTITION OF` clause.
- **CDC** is not tested because local `wal_level = logical` is an environment
  prerequisite.

See `docs/postgresql/POSTGRESQL_LIMITATIONS.md` for the full list.
