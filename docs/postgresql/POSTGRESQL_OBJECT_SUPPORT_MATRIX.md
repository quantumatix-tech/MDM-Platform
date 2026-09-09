# PostgreSQL Object Support Matrix

Classification of PostgreSQL object types based on the completed local audit
on PostgreSQL 17.4 (`feature/postgresql-objects` branch, commit `906dc89`).

## Classification keys

| Class | Meaning |
|---|---|
| **Supported / Verified** | Object migrates end-to-end in real PostgreSQL CLI tests |
| **Partial** | Migrates with known constraints (e.g., schema-scoped) |
| **Out of Scope** | Intentionally not part of current implementation |
| **Environment Blocked** | Implementation exists but cannot be verified locally due to environment |

## Schema

| Object | Class | Evidence / Notes |
|---|---|---|
| `CREATE SCHEMA` | Supported / Verified | Non-public schema `audit_test` created and populated; 5 tables / 13 rows migrated |

## Tables

| Object | Class | Evidence / Notes |
|---|---|---|
| `CREATE TABLE` | Supported / Verified | `public` and `audit_test` tables created with data |
| `DROP/CREATE IF NOT EXISTS` | Supported / Verified | `create_object_if_missing` uses `IF NOT EXISTS` |
| Primary key | Supported / Verified | SERIAL + inline `PRIMARY KEY` |
| Foreign key | Supported / Verified | `orders→customers`, `orders→products`, `test_orders→test_customers` |
| Cross-schema FK | Supported / Verified | `audit_test.fk_child→public.customers` tested; `ref_schema` preserved |
| Unique constraint | Supported / Verified | `customers.email`, `products.name`, `test_customers.email` |
| Check constraint | Supported / Verified | `price >= 0`, `stock_qty >= 0`, `quantity > 0` |
| Default | Supported / Verified | `DEFAULT NOW()`, `DEFAULT 'active'`, domain default |
| `GENERATED ALWAYS AS` | Supported / Verified | Detected via `attgenerated = 's'`, preserved in DDL |
| `NOT NULL` | Supported / Verified | Inline in column DDL |
| Partitioned table | Out of Scope | `create_partition` hardcodes public schema in existence check; documented limitation |

## Sequences

| Object | Class | Evidence / Notes |
|---|---|---|
| `CREATE SEQUENCE` | Supported / Verified | Explicit and SERIAL-based sequences created |
| `START WITH / INCREMENT BY / MINVALUE / MAXVALUE` | Supported / Verified | Properties preserved from source catalog |
| `CYCLE / NO CYCLE` | Supported / Verified | `cycle` flag migrated |
| Sequence ownership (`OWNED BY`) | Supported / Verified | `apply_sequence_ownership` reattaches after table creation |
| Sequence synchronization | Supported / Verified | `sync_sequence` (schema-qualified) advances to `MAX(column)+1` |

## Indexes

| Object | Class | Evidence / Notes |
|---|---|---|
| B-tree index | Supported / Verified | `pg_get_indexdef` DDL or constructed column-list DDL |
| Unique index | Supported / Verified | `idx_products_name` created as `UNIQUE INDEX` |
| Expression / partial index | Supported / Verified | Full DDL from `pg_get_indexdef` preserved |

## Views

| Object | Class | Evidence / Notes |
|---|---|---|
| Standard view | Supported / Verified | `customer_order_summary` and `order_summary` created in correct schema |
| `CREATE OR REPLACE VIEW` | Supported / Verified | Target emits `CREATE OR REPLACE VIEW` |
| Schema-qualified view | Supported / Verified | Non-public views created with schema qualification |

## Materialized Views

| Object | Class | Evidence / Notes |
|---|---|---|
| `CREATE MATERIALIZED VIEW` | Supported / Verified | `customer_balance_summary` created `WITH NO DATA` |
| `REFRESH MATERIALIZED VIEW` | Supported / Verified | Refreshed after creation; schema-qualified refresh |
| Schema-qualified matview | Supported / Verified | `audit_test_customer_summary` created and refreshed |

## Functions

| Object | Class | Evidence / Notes |
|---|---|---|
| SQL function | Supported / Verified | `get_customer_count()` executed and returned correct count |
| PL/pgSQL trigger function | Supported / Verified | `update_customer_timestamp()` fired on UPDATE |
| Schema-qualified function | Supported / Verified | `audit_test.get_customer_count()` created in `audit_test` |
| Procedure | Supported / Verified | `test_procedure()` executed via `CALL`; inserted row into log table |

## Triggers

| Object | Class | Evidence / Notes |
|---|---|---|
| `CREATE TRIGGER` | Supported / Verified | `trg_customer_timestamp` attached to correct table |
| `BEFORE UPDATE` trigger | Supported / Verified | Fires and updates `created_at` |
| Schema-qualified trigger | Supported / Verified | `audit_test.trg_customer_timestamp` created in `audit_test` |
| Trigger enabled/disabled state | Partial | Default enabled state created; `ENABLE/DISABLE` not migrated |

## Comments

| Object | Class | Evidence / Notes |
|---|---|---|
| `COMMENT ON SCHEMA` | Supported / Verified | `public` and `audit_test` schema comments applied |
| `COMMENT ON TABLE` | Supported / Verified | Table comments applied |
| `COMMENT ON COLUMN` | Supported / Verified | Column comments applied |
| `COMMENT ON VIEW` | Supported / Verified | View comments applied |
| `COMMENT ON MATERIALIZED VIEW` | Supported / Verified | Matview comments applied |
| `COMMENT ON FUNCTION` | Supported / Verified | Function comments applied (signature-aware) |
| `COMMENT ON SEQUENCE` | Supported / Verified | Sequence comments applied |
| Index / constraint comments | Partial | Not explicitly enumerated in current `list_comments()` |

## Grants / Privileges

| Object | Class | Evidence / Notes |
|---|---|---|
| Schema `USAGE` / `CREATE` | Supported / Verified | `audit_user` granted on `public` and `audit_test` |
| Table `SELECT` / `INSERT` / `UPDATE` / `DELETE` | Supported / Verified | `customers`, `orders`, `test_customers` grants verified |
| Column privileges | Supported / Verified | `customers.city`, `test_customers.city` grants verified |
| Sequence `USAGE` / `SELECT` | Supported / Verified | Sequence grants verified via `information_schema.role_usage_grants` |
| Function `EXECUTE` | Supported / Verified | `get_customer_count()` grant verified |
| Role creation | Out of Scope | Platform does not create or migrate roles |

## RLS / Policies

| Object | Class | Evidence / Notes |
|---|---|---|
| `ENABLE ROW LEVEL SECURITY` | Supported / Verified | `customers` and `test_customers` enabled |
| `CREATE POLICY ... USING` | Supported / Verified | Permissive policies with `USING (true)` verified |
| `CREATE POLICY ... WITH CHECK` | Supported / Verified | Insert policies with `WITH CHECK (true)` verified |
| `FORCE ROW LEVEL SECURITY` | Partial | `relforcerowsecurity` not migrated; default `false` |
| Policy roles (`polroles`) | Partial | Policies apply to all roles by default; role-specific policies not migrated |

## Custom Types

| Object | Class | Evidence / Notes |
|---|---|---|
| ENUM | Partial | Created in `public` only; non-public ENUM skipped due to `create_type()` hardcoded public existence check |
| DOMAIN | Partial | Created in `public` only; same hardcoded existence check |
| Composite | Partial | Discovered via catalog; creation follows same public-only pattern |

## CDC

| Object | Class | Evidence / Notes |
|---|---|---|
| Logical replication (pgoutput) | Environment Blocked | Requires `wal_level = logical`; local instance has `wal_level = replica` |
| Publication / replication slot | Environment Blocked | Cannot verify without logical replication enabled |
| Incremental INSERT / UPDATE / DELETE | Environment Blocked | Depends on CDC prerequisite |
| Continuous mode | Environment Blocked | Depends on CDC prerequisite |

## Other

| Object | Class | Evidence / Notes |
|---|---|---|
| `CREATE EXTENSION` | Supported / Verified | Extension discovery and `CREATE EXTENSION IF NOT EXISTS` |
| `CREATE DATABASE` | Supported / Verified | `ensure_database_exists` creates target database if missing |
| DDL transaction rollback | Supported / Verified | Single bad DDL does not poison connection |
| Schema-qualified validation | Supported / Verified | Count, checksum, and full validation paths respect schema |
