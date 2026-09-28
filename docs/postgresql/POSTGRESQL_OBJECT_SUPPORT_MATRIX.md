# PostgreSQL Object Support Matrix

Classification of PostgreSQL object types based on the completed E2E
tests on the `feature/postgresql-objects` branch
(Localâ†’Local audit, Localâ†’Cloud, and Cloudâ†’Local E2E runs).

## Classification keys

| Class | Meaning |
|---|---|
| **Supported / Verified** | Object migrates end-to-end in real PostgreSQL E2E tests |
| **Partial** | Migrates with known constraints (e.g., schema-scoped) |
| **Out of Scope** | Intentionally not part of current implementation |
| **Environment Blocked** | Implementation exists but cannot be verified locally due to environment |

## E2E Validation Status Key

| Status | Meaning |
|---|---|
| **All E2E runs** | Verified in Localâ†’Local, Localâ†’Cloud, and Cloudâ†’Local |
| **Localâ†’Local** | Verified in Localâ†’Local E2E only |
| **Localâ†’Cloud** | Verified in Localâ†’Cloud E2E only |
| **Cloudâ†’Local** | Verified in Cloudâ†’Local E2E only |
| **Not E2E tested** | Implementation exists but not verified in any E2E run |

## Schemas

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE SCHEMA` | Supported / Verified | All E2E runs | Non-public schemas created and populated in all three E2E directions |

## Tables

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE TABLE` | Supported / Verified | All E2E runs | Tables created with data in `public`, `audit_test`, `e2e_test`, `cloud_to_local_test` |
| `DROP/CREATE IF NOT EXISTS` | Supported / Verified | Localâ†’Local | `create_object_if_missing` uses `IF NOT EXISTS` |
| Primary key | Supported / Verified | All E2E runs | SERIAL / IDENTITY-based PKs on all tables |
| Foreign key | Supported / Verified | All E2E runs | `ordersâ†’customers`, `ordersâ†’products`, `test_ordersâ†’test_customers`, cross-schema FKs |
| Cross-schema FK | Supported / Verified | Localâ†’Local | `audit_test.fk_childâ†’public.customers` verified in Localâ†’Local; Cloudâ†’Local fixture contained only same-schema FKs |
| UNIQUE constraint | Supported / Verified | All E2E runs | `customers.email`, `products.name`, `test_customers.email` |
| Check constraint | Supported / Verified | All E2E runs | `price >= 0`, `stock_qty >= 0`, `quantity > 0` |
| Defaults | Supported / Verified | Localâ†’Local | `DEFAULT NOW()`, `DEFAULT 'active'`, domain default |
| `GENERATED ALWAYS AS` | Supported / Verified | Localâ†’Local | Detected via `attgenerated = 's'`, preserved in DDL |
| `NOT NULL` | Supported / Verified | Localâ†’Local | Inline in column DDL |
| Partitioned table | Out of Scope | Not E2E tested | `create_partition` hardcodes public schema in existence check; documented limitation |

## Table data

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Row data migration | Supported / Verified | All E2E runs | Full data migrated across all three directions (37 rows Localâ†’Local, ~28 rows Localâ†’Cloud, 9 rows Cloudâ†’Local) |
| Data type preservation | Supported / Verified | All E2E runs | VARCHAR, INTEGER, NUMERIC, TIMESTAMP, BOOLEAN, SERIAL/IDENTITY verified |

## Sequences

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE SEQUENCE` | Supported / Verified | All E2E runs | Explicit and SERIAL-based sequences created |
| `START WITH / INCREMENT BY / MINVALUE / MAXVALUE` | Supported / Verified | Localâ†’Local | Properties preserved from source catalog |
| `CYCLE / NO CYCLE` | Supported / Verified | Localâ†’Local | `cycle` flag migrated |
| Sequence ownership (`OWNED BY`) | Supported / Verified | All E2E runs | `apply_sequence_ownership` reattaches after table creation |
| Sequence synchronization | Supported / Verified | Localâ†’Local | `sync_sequence` (schema-qualified) advances to `MAX(column)+1` |
| Sequence privileges (USAGE / SELECT) | Supported / Verified | All E2E runs | Grants verified via `pg_class.relacl` + `aclexplode` |

## Indexes

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| B-tree index | Supported / Verified | All E2E runs | `pg_get_indexdef` DDL or constructed column-list DDL |
| Unique index | Supported / Verified | All E2E runs | `idx_products_name` created as `UNIQUE INDEX` |
| Expression / partial index | Supported / Verified | Localâ†’Local | Full DDL from `pg_get_indexdef` preserved |

## Views

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Standard view | Supported / Verified | All E2E runs | Created in correct schema; data queryable on target |
| `CREATE OR REPLACE VIEW` | Supported / Verified | Localâ†’Local, Cloudâ†’Local | Target emits `CREATE OR REPLACE VIEW` |
| Schema-qualified view | Supported / Verified | All E2E runs | Non-public views created with schema qualification |

## Materialized Views

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE MATERIALIZED VIEW` | Supported / Verified | All E2E runs | Created `WITH NO DATA` then refreshed |
| `REFRESH MATERIALIZED VIEW` | Supported / Verified | All E2E runs | Schema-qualified refresh after creation |
| Schema-qualified matview | Supported / Verified | All E2E runs | Created and refreshed in non-public schemas |
| Materialized view data | Supported / Verified | Cloudâ†’Local | Queryable after refresh on target |

## Functions

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| SQL function | Supported / Verified | All E2E runs | `get_customer_count()` executed and returned correct count |
| PL/pgSQL function | Supported / Verified | Localâ†’Local, Cloudâ†’Local | Functions created in correct schema |
| Schema-qualified function | Supported / Verified | All E2E runs | Functions created with schema qualification |
| Function EXECUTE privilege | Supported / Verified | All E2E runs | `GRANT EXECUTE ON FUNCTION` verified |

## Procedures

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Procedure | Supported / Verified | All E2E runs | `test_procedure()` / `log_message()` executed via `CALL` |
| Procedure EXECUTE privilege | Supported / Verified | Cloudâ†’Local | `GRANT EXECUTE ON PROCEDURE` verified separately from FUNCTION grants |

## Triggers

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE TRIGGER` | Supported / Verified | All E2E runs | Triggers attached to correct tables |
| `BEFORE UPDATE` trigger | Supported / Verified | All E2E runs | Fires and updates timestamp column |
| Trigger behavior on target | Supported / Verified | Cloudâ†’Local | Trigger fires correctly on migrated tables (verified with UPDATE test) |
| Schema-qualified trigger | Supported / Verified | Localâ†’Local | Triggers created in non-public schemas |
| Trigger enabled/disabled state | Partial | Localâ†’Local | Default enabled state created; `ENABLE/DISABLE` not migrated |

## Trigger functions

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Trigger function | Supported / Verified | All E2E runs | PL/pgSQL functions used as trigger bodies; discovered and created |

## Comments / metadata

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `COMMENT ON SCHEMA` | Supported / Verified | All E2E runs | Schema comments applied |
| `COMMENT ON TABLE` | Supported / Verified | All E2E runs | Table comments applied |
| `COMMENT ON COLUMN` | Supported / Verified | All E2E runs | Column comments applied (including Cloudâ†’Local) |
| `COMMENT ON VIEW` | Supported / Verified | Cloudâ†’Local | View comments applied |
| `COMMENT ON MATERIALIZED VIEW` | Supported / Verified | Cloudâ†’Local | Matview comments applied |
| `COMMENT ON FUNCTION` | Supported / Verified | Cloudâ†’Local | Function comments applied (signature-aware) |
| `COMMENT ON PROCEDURE` | Supported / Verified | Cloudâ†’Local | Procedure comments applied (signature-aware) |
| `COMMENT ON SEQUENCE` | Supported / Verified | Localâ†’Local | Sequence comments applied |
| `COMMENT ON TRIGGER` | Supported / Verified | Cloudâ†’Local | Trigger comments applied |
| Index / constraint comments | Partial | Not E2E tested | Not explicitly enumerated in current `list_comments()` |

## Roles

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Role creation | Supported / Verified | All E2E runs | Roles created via `create_role_if_not_exists()` before grant application |

## Schema privileges

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Schema `USAGE` | Supported / Verified | All E2E runs | `audit_user` / `e2e_test_reader` / `cloud_to_local_reader` granted on schemas |
| Schema `CREATE` | Supported / Verified | Localâ†’Local | `audit_user` granted `CREATE` on `public` and `audit_test` |

## Table privileges

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Table `SELECT` | Supported / Verified | All E2E runs | Verified on target via `information_schema.role_table_grants` |
| Table `INSERT` | Supported / Verified | All E2E runs | Verified on target |
| Table `UPDATE` / `DELETE` | Supported / Verified | Localâ†’Local, Localâ†’Cloud | Granted and verified |
| Table-level grants to role | Supported / Verified | All E2E runs | Grants applied after object creation |

## Column-level privileges

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Column `SELECT` | Supported / Verified | Cloudâ†’Local | `GRANT SELECT (col1, col2) ON TABLE` verified on target |
| Column `INSERT` / `UPDATE` | Supported / Verified | Localâ†’Local | Column-level grants verified |

## Sequence privileges

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Sequence `USAGE` | Supported / Verified | All E2E runs | Verified via `pg_class.relacl` + `aclexplode` |
| Sequence `SELECT` | Supported / Verified | All E2E runs | Verified via `has_sequence_privilege()` and `relacl` |
| Sequence privilege extraction | Supported / Verified | All E2E runs | `information_schema.role_usage_grants` only captures USAGE; `relacl` + `aclexplode` captures USAGE, SELECT, UPDATE |

## Function / Procedure EXECUTE privileges

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Function `EXECUTE` | Supported / Verified | All E2E runs | `GRANT EXECUTE ON FUNCTION` verified on target |
| Procedure `EXECUTE` | Supported / Verified | Cloudâ†’Local | `GRANT EXECUTE ON PROCEDURE` verified separately from FUNCTION grants |

## Row Level Security (RLS)

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `ENABLE ROW LEVEL SECURITY` | Supported / Verified | All E2E runs | Tables enabled on target |
| `CREATE POLICY ... USING` | Supported / Verified | All E2E runs | Permissive policies with `USING (true)` verified |
| `CREATE POLICY ... WITH CHECK` | Supported / Verified | Localâ†’Local, Cloudâ†’Local | Insert policies with `WITH CHECK (true)` verified |
| RLS functional filtering | Supported / Verified | Cloudâ†’Local | RLS filtering verified using reader role (role-based row filtering tested) |
| `FORCE ROW LEVEL SECURITY` | Partial | Localâ†’Local | `relforcerowsecurity` not migrated; default `false` |
| Policy roles (`polroles`) | Partial | Localâ†’Local | Policies apply to all roles by default; role-specific policies not migrated |

## Validation / row counts / object verification

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Object existence verification | Supported / Verified | All E2E runs | Schemas, tables, views, matviews, functions, procedures, triggers verified on target |
| Row count verification | Supported / Verified | All E2E runs | Source and target row counts compared |
| Constraint verification | Supported / Verified | All E2E runs | PK, UK, FK, check constraints verified on target |
| Index verification | Supported / Verified | All E2E runs | Indexes enumerated and verified on target |
| Privilege verification | Supported / Verified | All E2E runs | Grants verified via catalog queries on target |
| RLS / policy verification | Supported / Verified | All E2E runs | Policies and RLS state verified on target |
| Migration report | Supported / Verified | All E2E runs | Run status, migrated tables, migrated rows, failed objects, errors, success percentage reported by CLI |

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
