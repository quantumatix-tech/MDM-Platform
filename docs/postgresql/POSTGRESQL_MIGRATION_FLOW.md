# PostgreSQL E2E Migration Flow

Complete end-to-end migration flow, verified directions, and known issues for PostgreSQL object migration on the `feature/postgresql-objects` branch.

---

## Part 1 — Conceptual Migration Flow

This flow describes what the migration platform does, in what order, and how verification works at the end.

```
SOURCE DATABASE
    ↓
Connect to source
    ↓
Discover PostgreSQL objects
    ↓
Extract object definitions / metadata
    ↓
Create target objects
    ↓
Migrate data
    ↓
Apply dependencies / indexes / sequences
    ↓
Apply views / materialized views
    ↓
Apply functions / procedures / triggers
    ↓
Apply comments
    ↓
Apply grants / privileges
    ↓
Apply RLS / policies
    ↓
Validate target
    ↓
E2E SUCCESS
```

### Object and dependency ordering

The migration engine creates objects in dependency order to avoid foreign key or reference failures:

1. **Schemas** — Created first so all subsequent objects have a valid namespace.
2. **Roles** — Created before grants are applied (a role must exist before it can receive grants).
3. **Tables** — Created with all constraints (PK, UK, CHECK, defaults, generated columns).
4. **Data** — Upserted into tables after table structures exist.
5. **Indexes** — Created after tables and data (B-tree, unique, expression, partial).
6. **Sequences** — Discovered, created, ownership attached (`OWNED BY`), and synchronized to `MAX(column)+1`.
7. **Views** — Created after underlying tables exist.
8. **Materialized views** — Created `WITH NO DATA`, then refreshed after tables and data are ready.
9. **Functions** — Created after tables exist (functions may reference tables).
10. **Procedures** — Created after tables exist (procedures may reference tables).
11. **Triggers** — Created after tables, functions, and procedures exist (triggers reference table and function).
12. **Comments** — Applied after all objects exist (comments reference existing objects).
13. **Grants / Privileges** — Applied after all objects and roles exist (grants reference objects and roles).
14. **RLS / Policies** — Applied after tables exist and RLS is enabled.

If an object creation fails, the migration rolls back the current DDL transaction and continues (single bad DDL does not poison the connection).

---

## Part 2 — E2E Migration Directions

Three complete E2E migration directions have been verified:

| Direction | Description | Config |
|---|---|---|
| **Local → Local** | Local PostgreSQL → local PostgreSQL | `config/postgresql_object_e2e.yaml` |
| **Local → Cloud** | Local PostgreSQL → Azure PostgreSQL | `config/postgresql_cloud_test.yaml` |
| **Cloud → Local** | Azure PostgreSQL → local PostgreSQL | `config/postgresql_cloud_to_local.yaml` |

### A. Local → Local

**Verified result available in repository history:**

- Branch: `feature/postgresql-objects`
- Environment: PostgreSQL 17.4, local source → local target
- Source DB: `migration_source` → Target DB: `migration_target`
- Schemas: `public`, `audit_test`
- Tables migrated: 8
- Rows migrated: 37
- Failed: 0
- Success rate: 100%
- Unit tests: 100 passed

**Objects verified:**

- Schemas (public + audit_test)
- Tables with data (customers, products, orders, test_customers, test_orders, fk_parent, fk_child, procedure_test_log)
- Primary keys, UNIQUE constraints, CHECK constraints, defaults
- Foreign keys (including cross-schema `audit_test.fk_child → public.customers`)
- Sequences with ownership and synchronization
- Indexes (B-tree, unique)
- Views and materialized views (schema-qualified, refreshed)
- Functions and procedures (SQL and PL/pgSQL)
- Triggers and trigger functions (BEFORE UPDATE, schema-qualified)
- Comments (schema, table, column, view, function, sequence)
- Grants (schema USAGE/CREATE, table SELECT/INSERT/UPDATE/DELETE, column-level, sequence USAGE/SELECT, function EXECUTE)
- RLS (ENABLE ROW LEVEL SECURITY + permissive policies)
- Role creation (`audit_user`)

Full evidence in `POSTGRESQL_LOCAL_AUDIT_PHASE_1.md`.

### B. Local → Cloud

**Verified run ID:** `db5169f4f57747c1a31d17185ffeb097`

**Result:** 100% successful.

- Source: Local PostgreSQL 17.4
- Target: Azure PostgreSQL Flexible Server (v16)
- Schema: `e2e_test`
- Tables migrated: 4 (customers, orders, products, procedure_test_log)
- Rows migrated: ~28
- Failed: 0
- Success rate: 100%
- SSL: `true` (required for Azure)

**Objects verified:**

- Role creation (`e2e_test_reader`)
- Schema grants (USAGE)
- Table grants (SELECT, INSERT)
- Sequence grants (USAGE + SELECT via `pg_class.relacl` + `aclexplode`)
- Views and materialized views (queryable and refreshable)
- Functions and procedures (executable via `SELECT` / `CALL`)
- Triggers (fire on INSERT/UPDATE)
- Comments
- RLS policies (permissive, role-scoped)
- Functional INSERT test proving sequence privileges work

Full evidence in `POSTGRESQL_CLOUD_AUDIT.md`.

**Implementation fixes required during Local→Cloud testing** (fixed before final successful run):

- Role creation for grant grantees — added `create_role_if_not_exists()`
- Sequence SELECT privilege extraction — replaced `information_schema.role_usage_grants` with `pg_class.relacl` + `aclexplode`
- Transaction abort on grant failure — added explicit rollback
- Implicit sequence grants for INSERT roles

### C. Cloud → Local

**Verified run ID:** `c194716147eb45c0b2fc906daebdba3b`

**Result:**

- 4 tables migrated
- 9 rows migrated
- Failed: 0
- Errors: 0
- Success: 100%
- Final status: SUCCESS

**Source:** Azure PostgreSQL (schema `cloud_to_local_test`)
**Target:** Local PostgreSQL (`cloud_to_local_test` schema)

**Objects migrated and verified:**

| Object Type | Verified |
|---|---|
| Tables + data | Yes |
| Primary keys | Yes |
| UNIQUE constraints | Yes |
| Foreign keys | Yes |
| Indexes | Yes |
| Sequences + sequence ownership | Yes |
| View + view data | Yes |
| Materialized view + data | Yes |
| Function + execution | Yes |
| Procedure + execution | Yes |
| Trigger behavior | Yes |
| Schema/table/column comments | Yes |
| Role creation | Yes |
| Schema/table grants | Yes |
| Column-level grants | Yes |
| Sequence USAGE/SELECT privileges | Yes |
| Function EXECUTE privileges | Yes |
| Procedure EXECUTE privileges | Yes |
| RLS functional filtering (reader role) | Yes |
| Cleanup of temporary procedure test data | Yes |

**Manual functional validations completed for Cloud → Local:**

- Tables + data queried on target
- PK / UNIQUE constraints verified
- Foreign keys verified
- Indexes enumerated on target
- Sequences + sequence ownership verified
- View queried, materialized view queried
- Function called (`SELECT func()`), procedure called (`CALL proc()`)
- Trigger behavior tested (UPDATE fires trigger)
- Schema/table/column comments verified
- Role created on target
- Schema/table/column grants verified
- Sequence USAGE/SELECT privileges verified
- Function/Procedure EXECUTE privileges verified
- RLS functional filtering tested using the reader role
- Temporary procedure test data cleaned up

---

## Part 3 — E2E Issues Found and Fixed

The following real issues were discovered during the Cloud → Local E2E process and corrected in the implementation.

### Issue 1: Column-level GRANT SQL generated with invalid PostgreSQL syntax

During Cloud → Local E2E testing, column-level `GRANT` statements were initially generated with invalid PostgreSQL syntax, causing the migration to fail on the Grants phase.

**Fix:** The implementation was corrected to generate valid PostgreSQL column-level `GRANT` syntax.

**Impact:** Column-level grants (e.g., `GRANT SELECT (col1, col2) ON TABLE ...`) now migrate correctly.

### Issue 2: Procedure grants treated as FUNCTION grants

During Cloud → Local E2E testing, procedure grants were initially handled using `ON FUNCTION` syntax. PostgreSQL distinguishes between functions and procedures in grant statements — `ON FUNCTION` for functions and `ON PROCEDURE` for procedures.

**Fix:** The implementation was corrected to preserve the PostgreSQL object kind and generate:
- `ON FUNCTION` for functions
- `ON PROCEDURE` for procedures

**Impact:** Procedure `EXECUTE` grants now work correctly on PostgreSQL targets.

### Final Cloud → Local result after fixes

- Errors: 0
- Success rate: 100%

---

## Part 4 — Object Discovery Summary

The source connector discovers PostgreSQL objects through catalog queries in this order:

1. **Schemas** — `pg_namespace` (excluding system schemas)
2. **Tables** — `information_schema.tables` + `pg_class`
3. **Columns** — `information_schema.columns` + `pg_attribute`
4. **Constraints** — `information_schema.table_constraints` + `information_schema.key_column_usage`
5. **Foreign keys** — `information_schema.referential_constraints` (schema-qualified)
6. **Indexes** — `pg_indexes` / `pg_get_indexdef`
7. **Sequences** — `pg_sequences` + `pg_class`
8. **Views** — `information_schema.views` / `pg_class` (relkind = 'v')
9. **Materialized views** — `pg_matviews` / `pg_class` (relkind = 'm')
10. **Functions** — `pg_proc` (prokind = 'f')
11. **Procedures** — `pg_proc` (prokind = 'p')
12. **Triggers** — `pg_trigger` (excluding internal)
13. **Comments** — `pg_description`
14. **Grants** — `information_schema.role_*_grants` + `pg_class.relacl` (sequences)
15. **RLS** — `pg_class.relrowsecurity` + `pg_policy`
16. **Roles** — `pg_roles` (verified during grant phase)
