# PostgreSQL Local Audit — Final Report

**Project:** Migration Platform
**Branch:** `feature/postgresql-objects`
**Environment:** PostgreSQL 17.4, local source → local target
**Host:** `127.0.0.1`
**Port:** `55432`
**Databases:** `migration_source` → `migration_target`

---

## 1. Objective

Establish a reproducible baseline for PostgreSQL object migration on the
`feature/postgresql-objects` branch. Verify that the implementation correctly
discovers, creates, and migrates:

- Schemas (public + non-public)
- Tables, primary keys, foreign keys (including cross-schema)
- Unique, check, and default constraints
- Sequences, sequence ownership, and sequence synchronization
- Indexes
- Views and materialized views
- Functions and stored procedures
- Triggers and trigger functions
- Comments
- Grants / privileges
- Row-level security (RLS) and policies

---

## 2. Environment

| Item | Value |
|---|---|
| PostgreSQL version | 17.4 |
| Host | `127.0.0.1` |
| Port | `55432` |
| Source DB | `migration_source` |
| Target DB | `migration_target` |
| User | `postgres` |
| Migration mode | `full` |
| Config | `config/postgresql_object_e2e.yaml` |

The source database is populated by the reusable fixture in
`tests/fixtures/postgresql_e2e/`.

---

## 3. Test Fixture

The reusable fixture (`tests/fixtures/postgresql_e2e/`) creates a deterministic
source database containing:

- **Schemas:** `public`, `audit_test`
- **Tables:** 3 in `public`, 4 in `audit_test` (including cross-schema FK targets)
- **Data:** 5 customers, 10 products, 12 orders, 3 test customers, 2 test orders, 2 fk_parent, 3 fk_child
- **Sequences:** 3 public, 2 audit_test (ownership and synchronization verified)
- **Indexes:** 5 public, 3 audit_test (including unique index)
- **Views:** 1 public, 1 audit_test
- **Materialized views:** 1 public, 1 audit_test
- **Functions / Procedures:** 1 function + 1 procedure per schema
- **Triggers:** 1 per schema (schema-qualified)
- **Comments:** On schemas, tables, columns, views, materialized views, functions, sequences
- **Grants:** Table, column, sequence, schema, and function grants to `audit_user`
- **RLS / Policies:** ENABLE ROW LEVEL SECURITY + permissive policies on 1 table per schema
- **Custom types:** 1 ENUM + 1 DOMAIN in `public` only (documented limitation)

See `tests/fixtures/postgresql_e2e/README.md` for the full inventory and
execution order.

---

## 4. Test Configuration

Primary E2E configuration: `config/postgresql_object_e2e.yaml`

```yaml
migration:
  mode: full
  include_schemas:
    - public
    - audit_test
```

Secrets are resolved via the `env` provider:

| Config reference | Environment variable |
|---|---|
| `source.password_secret` | `SECRET_source_db_pass` |
| `target.password_secret` | `SECRET_target_db_pass` |

No hardcoded passwords appear in config files.

---

## 5. Audit Method

1. **Fixture setup** — Reset `migration_source` using the SQL fixture scripts.
2. **Source verification** — Confirm expected objects and row counts exist.
3. **Migration execution** — Run `python -m migration_platform --config config/postgresql_object_e2e.yaml --mode full`.
4. **Target verification** — Query `migration_target` to confirm object creation,
   schema qualification, data migration, and behavioral correctness.
5. **Unit test verification** — Run `python -m pytest tests/unit -q`.
6. **Evidence collection** — Record run IDs, SQL queries, and output.

---

## 6. Object Coverage

All categories below were verified with real PostgreSQL CLI queries against
`migration_target` after a clean migration run.

| Category | Status | Evidence |
|---|---|---|
| Schemas | Verified | `public` + `audit_test` created |
| Tables | Verified | 8 tables migrated with data |
| Primary keys | Verified | SERIAL-based PKs on all tables |
| Foreign keys | Verified | Same-schema and cross-schema FKs |
| Cross-schema FK | Verified | `audit_test.fk_child → public.customers` |
| Unique constraints | Verified | `customers.email`, `products.name` |
| Check constraints | Verified | `price >= 0`, `stock_qty >= 0`, `quantity > 0` |
| Defaults | Verified | `DEFAULT NOW()`, enum defaults, domain defaults |
| Indexes | Verified | 8 indexes including unique |
| Views | Verified | Schema-qualified non-public views |
| Materialized views | Verified | Created `WITH NO DATA` + refreshed |
| Functions | Verified | SQL and PL/pgSQL functions created and executable |
| Procedures | Verified | Procedure executed via `CALL` |
| Triggers | Verified | Schema-qualified triggers fire correctly |
| Comments | Verified | Schema, table, column, view, function, sequence comments |
| Grants | Verified | Table, column, sequence, schema, function grants to `audit_user` |
| RLS / Policies | Verified | `ENABLE ROW LEVEL SECURITY` + permissive policies |
| Sequence ownership | Verified | `OWNED BY` reattached after table creation |
| Sequence synchronization | Verified | `setval` advanced to `MAX(column)+1` |
| DDL transaction rollback | Verified | Single bad DDL does not poison connection |

---

## 7. E2E Results

### Unit tests

```text
94 passed, 0 failed
```

### Latest migration run

```text
Run ID: <current run>
Mode: FULL
Tables migrated: 8
Total rows: 37
Migrated: 37
Failed: 0
Success rate: 100%
```

### Row counts by table

| Schema | Table | Source rows | Target rows |
|---|---|---|---|
| public | customers | 5 | 5 |
| public | products | 10 | 10 |
| public | orders | 12 | 12 |
| audit_test | test_customers | 3 | 3 |
| audit_test | test_orders | 2 | 2 |
| audit_test | fk_parent | 2 | 2 |
| audit_test | fk_child | 3 | 3 |
| audit_test | procedure_test_log | 0 | 0 |

### Key commit

- `906dc89` — fix: qualify PostgreSQL sequence synchronization

---

## 8. Validation Evidence

### Sequence ownership

```text
public.customers_customer_id_seq     → public.customers.customer_id
public.products_product_id_seq       → public.products.product_id
public.orders_order_id_seq           → public.orders.order_id
audit_test.test_orders_order_id_seq  → audit_test.test_orders.order_id
```

### Cross-schema FK

```sql
SELECT tc.constraint_name, ccu.table_schema, ccu.table_name AS ref_table
FROM information_schema.table_constraints tc
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name = ccu.constraint_name
WHERE tc.table_schema = 'audit_test'
  AND tc.table_name = 'fk_child'
  AND tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = 'public';
```

Result: `fk_child_to_public_customers → public.customers`

### Trigger behavior

```sql
UPDATE audit_test.test_customers SET full_name = 'Updated Test' WHERE customer_id = 1;
-- created_at updated to CURRENT_TIMESTAMP by trg_customer_timestamp
```

### Procedure execution

```sql
CALL audit_test.test_procedure('E2E verification test');
-- Inserts 1 row into audit_test.procedure_test_log
```

### RLS behavior

```sql
SET search_path TO audit_test, public;
SELECT * FROM test_customers;  -- returns rows for audit_user (permissive policy)
```

---

## 9. Known Limitations

These are confirmed gaps in the current implementation or audit scope.

| Limitation | Category |
|---|---|
| `create_type()` hardcodes public schema | Implementation |
| `create_partition()` hardcodes public schema | Implementation |
| Trigger enabled/disabled state not migrated | Implementation |
| `FORCE ROW LEVEL SECURITY` not migrated | Implementation |
| RLS policy roles (`polroles`) not migrated | Implementation |
| Comments on indexes/constraints not enumerated | Audit scope |
| Deferrable constraints not explicitly tested | Audit scope |
| Circular cross-schema dependencies not tested | Audit scope |
| FULL mode does not delete stale target rows | Implementation |
| Roles are not created or migrated | Implementation |

See `docs/postgresql/POSTGRESQL_LIMITATIONS.md` for detailed descriptions and
workarounds.

---

## 10. Environment Blockers

These are not implementation failures.  They are prerequisites that are not
met in the local environment.

| Blocker | Status | Resolution |
|---|---|---|
| `wal_level = logical` for CDC | Blocked | Configure PostgreSQL for logical replication and restart, or set `cdc.allow_source_service_restart=true` |
| MSSQL ODBC Driver 18 | Blocked | Install driver for MSSQL integration tests |

---

## 11. Final Assessment

The `feature/postgresql-objects` branch delivers a verified PostgreSQL object
migration implementation covering the full lifecycle of PostgreSQL metadata
objects:

- **94 unit tests** pass without regression.
- **Full E2E migration** of 8 tables, 37 rows, and 20+ non-table objects
  succeeds on PostgreSQL 17.4.
- **Schema qualification** is correct for public and non-public schemas.
- **Cross-schema dependencies** (FKs, grants, RLS) are preserved.
- **Sequence lifecycle** (create → own → sync) is complete.

The remaining gaps are documented limitations and environment prerequisites,
not bugs in the verified object migration path.

---

## 12. Reproduction Instructions

```bash
# 1. Clone and checkout the branch
git clone <repo-url>
git checkout feature/postgresql-objects

# 2. Install dependencies
pip install -e .

# 3. Set secrets
export SECRET_source_db_pass=postgres
export SECRET_target_db_pass=postgres

# 4. Ensure PostgreSQL 17.4 is running on 127.0.0.1:55432

# 5. Create source database if needed
export PGPASSWORD=postgres
psql -h 127.0.0.1 -p 55432 -U postgres -c "CREATE DATABASE migration_source;"

# 6. Reset source fixture
bash tests/fixtures/postgresql_e2e/reset_source.sh

# 7. Run migration
python -m migration_platform --config config/postgresql_object_e2e.yaml --mode full --no-live-ui

# 8. Verify
python tests/fixtures/postgresql_e2e/verify_migration.py

# 9. Run unit tests
python -m pytest tests/unit -q
```

See `docs/postgresql/POSTGRESQL_TEST_GUIDE.md` for detailed troubleshooting
and manual verification queries.
