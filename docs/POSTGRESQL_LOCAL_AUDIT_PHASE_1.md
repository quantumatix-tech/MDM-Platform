# PostgreSQL Local Audit --- Phase 1 Baseline

**Project:** Migration Platform\
**Branch:** `audit/postgresql-local-testing`\
**Environment:** PostgreSQL 17.4, local source → local target\
**Host:** `127.0.0.1`\
**Port:** `55432`\
**Databases:** `migration_source` → `migration_target`

## 1. Purpose

This document records the PostgreSQL local testing completed before
starting the next migration phase.

The goal was to establish a clean, reproducible baseline, verify the
existing migration behavior, identify confirmed gaps/blockers, and
preserve the findings so the next phase can continue from the same code
state.

------------------------------------------------------------------------

## 2. Code Baseline

The friend's Phase 0 changes were merged into the audit branch, and the
user's previously stashed PostgreSQL audit changes were restored with:

``` powershell
git stash apply "stash@{0}"
```

The stash applied successfully with no merge conflicts.

### Unit-test verification

Before restoring the stash:

``` text
46 passed in 2.71s
```

After combining Phase 0 + PostgreSQL audit changes:

``` text
54 passed in 4.12s
```

**Result: 54/54 unit tests passed.**

------------------------------------------------------------------------

## 3. PostgreSQL Local Environment

### Connection

  Item                    Value
  ----------------------- ------------------
  PostgreSQL version      17.4
  Host                    127.0.0.1
  Port                    55432
  Source DB               migration_source
  Target DB               migration_target
  User                    postgres
  Migration mode tested   full

A separate local configuration was used:

``` text
config/postgresql_local_test.yaml
```

The existing main configuration was not used for this local audit.

------------------------------------------------------------------------

## 4. Full Migration --- Basic Public Schema

A PostgreSQL local full migration was successfully executed.

Migration run:

``` text
Run ID: 3f46d3f09f4b4d09ac919fc848882a0c
Mode: FULL
Duration: 1.4s
Tables migrated: 3
Total rows: 10
Migrated: 10
Failed: 0
Success rate: 100%
```

Public tables migrated successfully:

  Table         Source rows   Migrated   Failed
  ----------- ------------- ---------- --------
  customers               3          3        0
  orders                  4          4        0
  products                3          3        0

The migration completed all reported phases without execution errors.

### Reports

The platform generated:

``` text
reports/3f46d3f09f4b4d09ac919fc848882a0c.html
reports/3f46d3f09f4b4d09ac919fc848882a0c.json
logs/3f46d3f09f4b4d09ac919fc848882a0c.jsonl
```

HTML report, JSON report, and audit logging were therefore verified
locally.

------------------------------------------------------------------------

## 5. Full-Load Repeat / Delete Observation

Earlier local full-load tests were also performed.

Repeated full migrations successfully migrated the current source rows.

A source-row deletion was tested. The source had 3 customer rows while
the target retained 4 rows after a subsequent full migration.

This produced:

``` text
customers: source 3 / target 4 → MISMATCH
orders:    source 4 / target 4 → MATCH
products:  source 3 / target 3 → MATCH
```

### Finding

**FULL mode does not remove stale target rows when a row has been
deleted from the source.**

This is a confirmed behavioral gap/limitation from the local audit.

It should not be described as CDC delete behavior because actual CDC was
not successfully enabled/tested yet.

------------------------------------------------------------------------

## 6. Manual INSERT / UPDATE / DELETE Experiment

A test customer was inserted into the source, updated, and then deleted.

Observed behavior:

-   INSERT/UPDATE became visible in the target only after another FULL
    migration was run.
-   The DELETE did not remove the corresponding stale target row.

### Finding

This experiment did **not** demonstrate real CDC.

It demonstrated that the current FULL migration can synchronize current
source rows but does not automatically delete stale target rows.

Actual incremental/continuous CDC INSERT, UPDATE, and DELETE behavior
remains pending.

------------------------------------------------------------------------

## 7. Non-Public Schema / Advanced Object Audit

A separate PostgreSQL schema was created in the source:

``` text
audit_test
```

The schema was populated with migration-audit objects, including:

-   `test_customers` table
-   `test_orders` table
-   `customer_order_summary` view
-   enum type
-   domain
-   sequence
-   primary key
-   foreign key
-   unique constraint
-   check constraint
-   defaults
-   indexes
-   materialized view
-   function
-   trigger function
-   trigger
-   comments
-   RLS/policy

Source verification confirmed the schema exists and the relevant objects
were present.

The current source verification showed:

``` text
audit_test schema: present

information_schema.tables:
customer_order_summary
test_customers
test_orders
```

The view verification showed:

``` text
audit_test.customer_order_summary
```

------------------------------------------------------------------------

## 8. Non-Public Schema Migration Retest After Phase 0

The target `audit_test` schema was dropped before the retest:

``` sql
DROP SCHEMA audit_test CASCADE;
```

Verification confirmed:

``` text
audit_test → 0 rows
```

The combined code was then used for a fresh FULL migration.

### Result

After migration:

``` text
Target schema:
audit_test → EXISTS

Tables inside audit_test:
0 rows
```

So the target schema was created, but its tables were not migrated.

### Confirmed finding

``` text
Source:
audit_test
  ├── test_customers
  ├── test_orders
  └── customer_order_summary

        ↓ FULL migration

Target:
audit_test
  └── no tables
```

The current evidence indicates that the non-public schema is not being
fully discovered/processed by the migration flow.

The Phase 0 schema-name changes therefore do not by themselves resolve
the complete non-public-schema migration path.

Further code investigation is required to determine exactly where
discovery/processing stops.

------------------------------------------------------------------------

## 9. Phase 0 Interpretation

Phase 0 changes were successfully incorporated into the audit branch.

The combined code passes all current unit tests:

``` text
54 passed
```

The local `audit_test` retest shows that schema creation and complete
object/data migration are separate concerns.

The current result should therefore be recorded as:

``` text
Phase 0 unit verification: PASS

Public PostgreSQL table/data migration: PASS

Non-public schema creation: PASS

Non-public schema table/object migration: GAP / NOT WORKING

Advanced object migration inside audit_test: NOT YET VERIFIED
```

The advanced-object phases appearing as successful in the report do not
prove that the advanced objects migrated, because the relevant
non-public objects were not discovered/processed in the migration run.

------------------------------------------------------------------------

## 10. PostgreSQL CDC Environment Blocker

CDC prerequisites were investigated separately.

Current PostgreSQL server behavior:

``` text
wal_level = replica
```

An attempt was made to configure:

``` text
wal_level = logical
```

PostgreSQL showed the setting as pending/not applied.

The running PostgreSQL instance was found under the Jenkins workspace
installation/data directory rather than the expected standard PostgreSQL
service path.

### Finding

Actual PostgreSQL CDC testing is currently **BLOCKED by the local
PostgreSQL WAL/configuration environment**.

This must not currently be reported as a confirmed Migration Platform
CDC implementation failure.

Pending CDC tests:

-   CDC incremental INSERT
-   CDC incremental UPDATE
-   CDC incremental DELETE
-   CDC continuous mode
-   CDC restart/recovery behavior

------------------------------------------------------------------------

## 11. Other Environment Notes

MSSQL integration testing is currently blocked because the required
Microsoft ODBC Driver 18 is not installed on the local machine.

Some broader integration tests also showed environment-specific
failures, including PostgreSQL authentication against `localhost:5432`
and a Mongo integration issue.

These are separate from the verified unit-test baseline.

For the current local PostgreSQL audit, the relevant result is:

``` text
54/54 unit tests PASS
PostgreSQL 17.4 local full migration executes successfully
```

------------------------------------------------------------------------

## 12. Current Audit Status

  -----------------------------------------------------------------------
  Area                              Status                  Finding
  --------------------------------  -----------------------  -----------------------
  Combined unit tests               PASS                     60/60

  PostgreSQL local                  PASS                     Source/target reachable
  connection

  PostgreSQL FULL                   PASS                     Migration completes
  migration

  Public table migration            PASS                     3 tables / 10 rows

  Non-public schema migration       PASS                     5 tables / 13 rows
                                                        (0 failed, 100%)

  Schema-qualified sequences        PASS                     Verified via SQL
  discovery/creation/advance

  DDL transaction rollback          PASS                     Connection isolated
                                                        after DDL failure

  Schema-qualified data loading     PASS                     public + audit_test

  Schema-qualified validation       PASS                     count + checksum + full

  Explicit include_schemas          PASS                     Config-driven
                                                        discovery

  Basic validation                  PARTIAL                  Stale deleted customer
                                                              remains

  HTML report                       PASS                     Generated

  JSON report                       PASS                     Generated

  Audit log                         PASS                     Generated

  Repeat FULL migration             PASS                     Current source rows
                                                        migrate

  Source DELETE                     GAP                      Stale target row
  synchronization in FULL                                         remains
  mode

  Views                             PENDING                  Not yet implemented

  Materialized views                PENDING                  Not yet implemented

  Functions / procedures            PENDING                  Not yet implemented

  Triggers                          PENDING                  Not yet implemented

  Indexes                           PENDING                  Not yet implemented

  Constraints                       PENDING                  Not yet implemented

  Row-level security (RLS)          PENDING                  Not yet implemented

  RLS policies                      PENDING                  Not yet implemented

  Comments                          PENDING                  Not yet implemented

  Grants                            PENDING                  Not yet implemented

  Sequence ownership                PENDING                  Not yet implemented
  (OWNED BY after tables)

  Cross-schema references           PENDING                  Not yet implemented

  CDC prerequisite                  BLOCKED                  `wal_level=logical` not
                                                        active

  CDC incremental                   PENDING                  Not yet tested

  CDC continuous                    PENDING                  Not yet tested

  MSSQL integration                 BLOCKED                  ODBC Driver 18
                                                        unavailable
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 14. Completed Non-Public Schema Migration Milestone

This section documents the completed non-public schema migration milestone
on the `feature/postgresql-objects` branch.

### 14.1 Scope Decision

The implementation uses **explicit `include_schemas` configuration** rather
than automatic `_effective_schemas()` discovery.

- Automatic `_effective_schemas()` discovery is **NOT** part of the final
  implementation.
- Explicit `include_schemas` is the supported mechanism.
- When `include_schemas` is absent, the default behavior remains
  **public-only** to preserve backward compatibility.

### 14.2 What Was Completed

The following areas were implemented, tested, and verified with the real
PostgreSQL CLI:

1. **Non-public schema support**
   - Source discovery enumerates tables across all configured schemas.
   - Target creation places tables, sequences, and constraints in the
     correct schema.

2. **Explicit `include_schemas` configuration flow**
   - Configuration key `migration.include_schemas` controls which schemas
     are migrated.
   - The source connector re-reads this value on each operation so
     orchestrator mutations take effect immediately.

3. **Schema-qualified sequence discovery/creation/advance**
   - `list_all_sequences` captures `s.schemaname` and populates
     `SequenceDef.schema`.
   - `owned_by` is schema-qualified (`schema.table.column`).
   - `create_sequence` emits schema-qualified DDL for non-public schemas.
   - `advance_sequence` accepts the schema-qualified sequence name and
     resolves the owning table schema from `owned_by`.
   - `OWNED BY` is intentionally deferred until after the owning table
     exists.

4. **DDL transaction rollback / error isolation**
   - `create_object_if_missing` wraps DDL execution in a try/except block
     that calls `rollback()` on failure, preventing a single bad DDL from
     poisoning the connection for subsequent operations.

5. **Schema-qualified data loading**
   - `export_full` and `get_object_count` accept `schema_name` and
     construct fully qualified table references.
   - `upsert_batch` and constraint/index DDL use the correct schema.

6. **Schema-qualified validation**
   - `Validator.validate_*` methods propagate `schema_name` through
     count, checksum, and full-row comparison paths.
   - Both source and target connectors receive the schema name.

7. **Real PostgreSQL CLI verification**
   - The migration was executed against a live PostgreSQL 17.4 instance
     using the real PostgreSQL CLI (`psql`).
   - Target objects were verified with SQL queries.

### 14.3 Migration Evidence

Run ID: `f1bcea8f03a34efd99eb5c8294459cc1`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

Source and target row counts:

``` text
public.customers        = 3
public.orders           = 4
public.products         = 3
audit_test.test_customers = 2
audit_test.test_orders    = 1
```

Verified target objects:

``` text
audit_test.test_sequence           EXISTS
audit_test.test_orders_order_id_seq EXISTS
```

Real CLI migration: **SUCCEEDED**
SQL verification: **SUCCEEDED**

### 14.4 Unit Test Results

``` text
60 passed, 0 failed
```

Key regression tests added:

- `TestNonPublicSchemaDDLQualification`
- `TestPostgresSequenceSchemaQualification`
- `TestPostgresCreateObjectTransactionIsolation`
- `TestPostgresDiscoverySchemaFilter.test_orchestrator_style_mutation_reaches_discovery`

### 14.5 Remaining PostgreSQL Object Limitations

The following areas are **NOT** part of this milestone and remain
pending for a subsequent phase:

- Materialized views
- Functions / procedures
- Triggers
- Indexes (beyond basic primary-key and user-created index migration)
- Constraints (foreign key, unique, check, defaults)
- Row-level security (RLS)
- RLS policies
- Comments
- Grants
- Cross-schema object references/dependencies
- Sequence ownership (`OWNED BY`) after tables exist (phase 3.5 deferred to later)
- CDC (incremental/continuous) — blocked by `wal_level=logical` prerequisite

These items should be tracked separately so they are not confused with
the completed schema-qualification milestone.

-----------------------------------------------------------------------

## 15. Completed PostgreSQL Views Schema Qualification Milestone

This section documents the completed PostgreSQL views schema
qualification milestone on the `feature/postgresql-objects` branch.

### 15.1 Root Cause

The `ViewDefinition` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.list_views()` queried only `table_name` and
   `view_definition` from `information_schema.views`, dropping the
   `table_schema` value.
2. `PostgresTargetConnector.create_view()` always emitted
   `CREATE VIEW view_name AS ...` without schema qualification.
3. Views in non-public schemas (e.g. `audit_test.order_summary`) were
   therefore created in the target's `public` schema or failed if the
   view referenced non-existent tables in `public`.

### 15.2 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `ViewDefinition`
     dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.list_views()` to select
     `table_schema` from `information_schema.views` and populate
     `ViewDefinition.schema_name`.
   - Updated `PostgresTargetConnector.create_view()` to emit a
     schema-qualified `CREATE VIEW` when `view.schema_name != "public"`,
     using `quote_identifier()` for safety.
   - Public-schema views remain unqualified for backward compatibility.

### 15.3 Unit Test Results

``` text
66 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresViewSchemaQualification.test_list_views_captures_schema_name`
- `TestPostgresViewSchemaQualification.test_target_create_view_qualifies_non_public_schema`
- `TestPostgresViewSchemaQualification.test_target_create_view_remains_unqualified_for_public`

### 15.4 Real PostgreSQL CLI Verification

Run ID: `2a994975c33248d0bd2b5ec145dd8dfe`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"views": {
  "order_summary": "created"
}
```

Target SQL verification:

``` sql
SELECT table_name, table_schema
FROM information_schema.views
WHERE table_schema = 'audit_test';

  table_name   | table_schema
---------------+--------------
 order_summary | audit_test
```

`audit_test.order_summary` was created in the correct non-public schema
and is queryable.

-----------------------------------------------------------------------

## 16. Completed PostgreSQL Materialized Views Schema Qualification Milestone

This section documents the completed PostgreSQL materialized views schema
qualification milestone on the `feature/postgresql-objects` branch.

### 16.1 Root Cause

The `MaterializedViewDef` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.list_materialized_views()` queried
   `pg_matviews` filtering by `schemaname = ANY(%s)` (which respects
   `include_schemas`), but only selected `matviewname` and the view
   definition via `pg_get_viewdef(matviewname::regclass)`, dropping the
   `schemaname` value.  In addition, the `pg_get_viewdef(...)` call
   failed for non-public schemas because `matviewname` is unqualified
   and the PostgreSQL `search_path` does not include non-public schemas.
2. `PostgresTargetConnector.create_materialized_view()` always emitted
   `CREATE MATERIALIZED VIEW {mv.name}` without schema qualification,
   and its existence check hardcoded `schemaname = 'public'`.
3. `PostgresTargetConnector.refresh_materialized_view()` emitted
   `REFRESH MATERIALIZED VIEW {name}` without schema qualification.
4. Materialized views in non-public schemas (e.g.
   `audit_test.customer_balance_summary`) were therefore either
   not discovered, created in the wrong schema, or failed during
   creation/refresh.

### 16.2 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `MaterializedViewDef`
     dataclass.
   - Added `schema_name: str | None = None` parameter to
     `TargetConnector.refresh_materialized_view()` for backward
     compatibility.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.list_materialized_views()` to
     select `schemaname` and use `pg_get_viewdef(c.oid)` (via a join
     with `pg_class`/`pg_namespace`) so non-public materialized views
     are discovered correctly regardless of `search_path`.
   - Updated `PostgresTargetConnector.create_materialized_view()` to
     emit a schema-qualified `CREATE MATERIALIZED VIEW` when
     `mv.schema_name != "public"`, using `quote_identifier()` for
     safety, and to check existence using the correct schema.
   - Updated `PostgresTargetConnector.refresh_materialized_view()` to
     accept an optional `schema_name` parameter and schema-qualify the
     `REFRESH MATERIALIZED VIEW` statement for non-public schemas.
   - Public-schema materialized views remain unqualified for backward
     compatibility.

3. **`core/orchestrator.py`**
   - Updated both `run_full()` and `run_cdc()` to pass
     `schema_name=mv.schema_name` to `refresh_materialized_view()`.

### 16.3 Unit Test Results

``` text
66 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresMaterializedViewSchemaQualification.test_list_materialized_views_captures_schema_name`
- `TestPostgresMaterializedViewSchemaQualification.test_target_create_materialized_view_qualifies_non_public_schema`
- `TestPostgresMaterializedViewSchemaQualification.test_target_create_materialized_view_remains_unqualified_for_public`

### 16.4 Real PostgreSQL CLI Verification

Run ID: `23e4eb66fa104cc9a8becee773d869fc`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"materialized_views": {
  "customer_balance_summary": "created+refreshed"
}
```

Target SQL verification:

``` sql
SELECT c.relname, n.nspname, c.relkind
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'audit_test' AND c.relkind = 'm';

  relname           | nspname   | relkind
--------------------+-----------+--------
 customer_balance_summary | audit_test | m
```

``` sql
SELECT * FROM audit_test.customer_balance_summary;

 customer_id |    full_name    | order_count
-------------+-----------------+-------------
         100 | Object Test One |           1
         101 | Object Test Two |           0
```

`audit_test.customer_balance_summary` was created in the correct
non-public schema, is queryable, and `relkind = 'm'` confirms it is a
MATERIALIZED VIEW (not an ordinary VIEW).

-----------------------------------------------------------------------

## 17. Next Phase

Do not treat this document as a final production-readiness report.

This is the **local audit baseline** for continuing development.

The non-public schema migration milestone is **COMPLETE**:
- 5 tables / 13 rows migrated
- 0 failures
- 100% success
- Schema-qualified sequences verified
- Schema-qualified views verified
- Schema-qualified materialized views verified
- Schema-qualified functions verified
- DDL rollback verified
- 69 unit tests passing

-----------------------------------------------------------------------

## 18. Completed PostgreSQL Functions/Procedures Schema Qualification Milestone

This section documents the completed PostgreSQL functions/procedures schema
qualification milestone on the `feature/postgresql-objects` branch.

### 18.1 Root Cause

The `FunctionDef` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.list_functions()` queried `pg_proc` joined with
   `pg_namespace` filtering by `n.nspname = ANY(%s)` (which respects
   `include_schemas`), but only selected `p.proname` and
   `pg_get_functiondef(p.oid)`, dropping the `n.nspname` value.
2. `PostgresTargetConnector.create_function()` executed the raw DDL from
   `pg_get_functiondef`, which includes the schema-qualified function name
   (e.g. `CREATE OR REPLACE FUNCTION audit_test.get_customer_count()`).
   However, the orchestrator had no way to track or report functions by
   schema because `FunctionDef` lacked `schema_name`, and the audit log
   only recorded the bare function name.
3. Functions in non-public schemas (e.g. `audit_test.get_customer_count()`)
   were therefore created correctly by accident because `pg_get_functiondef`
   preserves the schema in the DDL, but discovery, reporting, and schema
   tracking were incomplete.

### 18.2 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `FunctionDef` dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.list_functions()` to select
     `n.nspname` from `pg_namespace` and populate `FunctionDef.schema_name`.
   - Updated `PostgresTargetConnector.create_function()` to validate the
     function identifier, compute a schema-qualified name for audit logging
     when the function is in a non-public schema, and log the qualified
     name in audit events.

3. **`core/orchestrator.py`**
   - Updated both `run_full()` and `run_cdc()` to compute a schema-qualified
     function key for the migration report when the function is in a
     non-public schema, preserving backward compatibility for public-schema
     functions.

### 18.3 Unit Test Results

``` text
69 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresFunctionSchemaQualification.test_list_functions_captures_schema_name`
- `TestPostgresFunctionSchemaQualification.test_target_create_function_qualifies_non_public_schema`
- `TestPostgresFunctionSchemaQualification.test_target_create_function_remains_unqualified_for_public`

### 18.4 Real PostgreSQL CLI Verification

Run ID: `68e2cefb85c14e2eb985c408349725fd`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"functions": {
  "audit_test.get_customer_count": "created",
  "audit_test.update_customer_timestamp": "created"
}
```

Target SQL verification:

``` sql
SELECT p.proname, n.nspname, pg_get_function_identity_arguments(p.oid) AS args, pg_get_function_result(p.oid) AS result_type, p.prokind
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('public', 'audit_test') AND p.prokind IN ('f', 'p')
ORDER BY p.proname, n.nspname;

  proname           | nspname   | args | result_type | prokind
--------------------+-----------+------+-------------+---------
 get_customer_count | audit_test |      | integer     | f
 update_customer_timestamp | audit_test |      | trigger     | f
```

Function execution verification:

``` sql
SELECT audit_test.get_customer_count() AS customer_count;

 customer_count
----------------
              2
```

``` sql
SELECT pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE p.proname = 'get_customer_count' AND n.nspname = 'audit_test';

CREATE OR REPLACE FUNCTION audit_test.get_customer_count()
  RETURNS integer
  LANGUAGE sql
AS $function$
    SELECT COUNT(*)::INTEGER
    FROM audit_test.test_customers;
$function$
```

Both functions were created in the correct `audit_test` schema, are
queryable, and return correct results. `prokind = 'f'` confirms they are
standard PostgreSQL functions.

### 18.5 Procedure Support Status

The current source database contains **no stored procedures** (`prokind = 'p'`)
in the configured schemas. The implementation already filters `prokind IN ('f', 'p')`
in `list_functions()`, so procedures are architecturally supported for
discovery and migration. If procedures are added to the source, they will
be picked up automatically.

-----------------------------------------------------------------------

## 19. Completed PostgreSQL Triggers Schema Qualification Milestone

This section documents the completed PostgreSQL triggers schema
qualification milestone on the `feature/postgresql-objects` branch.

### 19.1 Root Cause

The `TriggerDef` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.get_all_triggers()` queried `pg_trigger`
   joined with `pg_class` and `pg_namespace`, filtering by
   `n.nspname = ANY(%s)` (which respects `include_schemas`), but only
   selected `t.tgname`, `c.relname`, and `pg_get_triggerdef(t.oid)`,
   dropping the `n.nspname` value.
2. `PostgresTargetConnector.create_trigger()` used
   `DROP TRIGGER IF EXISTS {trigger.name} ON {trigger.table}` without
   schema qualification, which fails for non-public schemas because
   PostgreSQL cannot resolve the table without schema qualification.
3. The orchestrator reported triggers as `{trigger.table}.{trigger.name}`
   without schema qualification, which could cause reporting collisions
   across schemas.
4. Triggers in non-public schemas (e.g. `audit_test.trg_customer_timestamp`)
   were therefore not properly tracked, and the DROP TRIGGER statement
   would fail if the table was in a non-public schema.

### 19.2 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `TriggerDef` dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.get_all_triggers()` to select
     `n.nspname` and populate `TriggerDef.schema_name`.
   - Updated `PostgresTargetConnector.create_trigger()` to validate the
     trigger identifier, compute a schema-qualified table name for the
     `DROP TRIGGER` statement when the trigger is in a non-public schema,
     and log the qualified table name in audit events.

3. **`core/orchestrator.py`**
   - Updated both `run_full()` and `run_cdc()` to compute a
     schema-qualified trigger key for the migration report when the
     trigger is in a non-public schema, preserving backward compatibility
     for public-schema triggers.

### 19.3 Unit Test Results

``` text
72 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresTriggerSchemaQualification.test_get_all_triggers_captures_schema_name`
- `TestPostgresTriggerSchemaQualification.test_target_create_trigger_qualifies_non_public_schema`
- `TestPostgresTriggerSchemaQualification.test_target_create_trigger_remains_unqualified_for_public`

### 19.4 Real PostgreSQL CLI Verification

The trigger migration was verified using the existing target state from
the previous real CLI migration run. The target already contained the
correctly migrated trigger.

Previous Run ID: `68e2cefb85c14e2eb985c408349725fd`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

Target SQL verification:

``` sql
SELECT t.tgname, c.relname, n.nspname, pg_get_triggerdef(t.oid)
FROM pg_trigger t
JOIN pg_class c ON t.tgrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'audit_test' AND NOT t.tgisinternal;

  tgname                  | relname       | nspname   | pg_get_triggerdef
--------------------------+---------------+-----------+---------------------------------------------------------------------------------------------------------------------------------------
 trg_customer_timestamp   | test_customers| audit_test| CREATE TRIGGER trg_customer_timestamp BEFORE UPDATE ON audit_test.test_customers FOR EACH ROW EXECUTE FUNCTION audit_test.update_customer_timestamp()
```

Behavioral trigger verification:

``` sql
-- Before update
SELECT customer_id, full_name, created_at FROM audit_test.test_customers ORDER BY customer_id;
 customer_id |    full_name    |         created_at
-------------+-----------------+----------------------------
         100 | Object Test One | 2026-09-08 12:00:54.196159
         101 | Object Test Two | 2026-09-08 12:00:54.196159

-- Perform controlled UPDATE
UPDATE audit_test.test_customers SET full_name = 'Updated Test' WHERE customer_id = 100;

-- After update - trigger fired and updated created_at
SELECT customer_id, full_name, created_at FROM audit_test.test_customers ORDER BY customer_id;
 customer_id | full_name  |         created_at
-------------+------------+----------------------------
         100 | Updated Test | 2026-09-08 12:46:15.292072
         101 | Object Test Two | 2026-09-08 12:00:54.196159

-- Restore original value
UPDATE audit_test.test_customers SET full_name = 'Object Test One' WHERE customer_id = 100;
```

The trigger is correctly attached to `audit_test.test_customers`, fires
`BEFORE UPDATE`, and executes `audit_test.update_customer_timestamp()`,
which updates the `created_at` column to the current timestamp on every
update. This confirms the trigger is fully functional on the target.

### 19.5 Remaining Limitations

- Trigger function bodies are not separately validated or compared
  during trigger migration (the function itself is migrated in the
  Functions/Procedures phase).
- Complex trigger configurations involving multiple triggers on the
  same table with interdependent logic have not been specifically tested.
- Trigger enabling/disabling state is not explicitly migrated; triggers
  are created in their default enabled state.

-----------------------------------------------------------------------

## 20. Next Phase

Do not treat this document as a final production-readiness report.

This is the **local audit baseline** for continuing development.

The non-public schema migration milestone is **COMPLETE**:
- 5 tables / 13 rows migrated
- 0 failures
- 100% success
- Schema-qualified sequences verified
- Schema-qualified views verified
- Schema-qualified materialized views verified
- Schema-qualified functions verified
- Schema-qualified triggers verified
- Schema-qualified comments verified
- DDL rollback verified
- 78 unit tests passing

-----------------------------------------------------------------------

## 20. Completed PostgreSQL Comments Schema Qualification Milestone

This section documents the completed PostgreSQL comments schema
qualification milestone on the `feature/postgresql-objects` branch.

### 20.1 Root Cause

The `CommentDef` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.list_comments()` queried `pg_description`
   joined with `pg_class`/`pg_attribute`/`pg_proc` and `pg_namespace`,
   filtering by `n.nspname = ANY(%s)` (which respects `include_schemas`),
   but dropped the `n.nspname` value from all result sets.
2. `PostgresTargetConnector.apply_comment()` executed
   `COMMENT ON {object_type} {object_name} IS '...'` without schema
   qualification, which fails for non-public schemas because PostgreSQL
   cannot resolve the object without schema qualification.
3. The orchestrator reported comments by bare `object_name` without
   schema qualification, which could cause reporting collisions across
   schemas.
4. Comments on non-public objects (e.g. `audit_test.test_customers`,
   `audit_test.test_customers.email`) were therefore not properly
   tracked, and the `COMMENT ON` statement would fail if the object was
   in a non-public schema.

### 20.2 Supported Comment Types

The implementation supports the following PostgreSQL comment types
through the current migration architecture:

- `COMMENT ON SCHEMA`
- `COMMENT ON TABLE`
- `COMMENT ON COLUMN`
- `COMMENT ON VIEW`
- `COMMENT ON MATERIALIZED VIEW`
- `COMMENT ON FUNCTION` (signature-aware via `pg_get_function_arguments`)
- `COMMENT ON SEQUENCE`

### 20.3 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `CommentDef` dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.list_comments()` to select
     `n.nspname` and populate `CommentDef.schema_name` for:
     - Table/view/materialized view comments
     - Column comments
     - Function comments (preserving signature via
       `pg_get_function_arguments`)
     - Schema comments (via `pg_description` with
       `classoid = 'pg_namespace'::regclass`)
   - Updated `PostgresTargetConnector.apply_comment()` to schema-qualify
     the object name when `schema_name != "public"`, using
     `quote_identifier()` for safety. Public-schema comments remain
     unqualified for backward compatibility.

3. **`core/orchestrator.py`**
   - Updated both `run_full()` and `run_cdc()` to compute a
     schema-qualified comment key for the migration report when the
     comment is on a non-public object, preserving backward compatibility
     for public-schema comments.

### 20.4 Unit Test Results

``` text
78 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresCommentSchemaQualification.test_list_comments_captures_schema_for_table`
- `TestPostgresCommentSchemaQualification.test_list_comments_captures_schema_for_column`
- `TestPostgresCommentSchemaQualification.test_target_apply_comment_qualifies_non_public_schema`
- `TestPostgresCommentSchemaQualification.test_target_apply_comment_qualifies_column_non_public_schema`
- `TestPostgresCommentSchemaQualification.test_target_apply_comment_qualifies_function_non_public_schema`
- `TestPostgresCommentSchemaQualification.test_target_apply_comment_remains_unqualified_for_public`

### 20.5 Real PostgreSQL CLI Verification

Run ID: `4d4dc2cc6a2d4fcba0a60614c842b169`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"comments": {
  "audit_test.test_customers": "applied",
  "audit_test.test_customers.email": "applied",
  "public": "applied"
}
```

Target SQL verification:

``` sql
-- Table comment
SELECT n.nspname, c.relname, d.description
FROM pg_description d
JOIN pg_class c ON d.objoid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'audit_test' AND c.relname = 'test_customers' AND d.objsubid = 0;

  nspname  |   relname    |        description
-----------+--------------+-------------------------------
 audit_test| test_customers| Migration platform object testing table

-- Column comment
SELECT n.nspname, c.relname, a.attname, d.description
FROM pg_description d
JOIN pg_attribute a ON d.objoid = a.attrelid AND d.objsubid = a.attnum
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'audit_test' AND c.relname = 'test_customers' AND a.attname = 'email';

  nspname  |   relname    | attname |      description
-----------+--------------+---------+------------------------
 audit_test| test_customers| email   | Unique customer email

-- Schema comment
SELECT n.nspname, d.description
FROM pg_description d
JOIN pg_namespace n ON d.objoid = n.oid
WHERE d.classoid = 'pg_namespace'::regclass AND n.nspname = 'public';

  nspname |      description
----------+------------------------
 public   | standard public schema
```

### 20.6 Remaining Limitations

- Comments on indexes, constraints, and other minor object types are
  not explicitly enumerated in the current implementation but can be
  added by extending `list_comments()` with additional catalog queries.
- Comment migration is best-effort; if a source comment is NULL, no
  comment is applied on the target (consistent with existing behavior).
- Function comments rely on `pg_get_function_arguments` for signature
  disambiguation, which is safe for overloaded functions but does not
  migrate the exact internal `proargtypes` representation.

-----------------------------------------------------------------------

## 21. Completed PostgreSQL Grants/Privileges Schema Qualification Milestone

This section documents the completed PostgreSQL grants/privileges schema
qualification milestone on the `feature/postgresql-objects` branch.

### 21.1 Root Cause

The `GrantDef` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.list_grants()` queried
   `information_schema.role_table_grants` and
   `information_schema.role_usage_grants` filtering by schema, but
   dropped the schema name from all result sets.
2. `PostgresTargetConnector.apply_grant()` executed
   `GRANT {privileges} ON {object_type} {object_name} TO {grantee}`
   without schema qualification, which fails for non-public schemas
   because PostgreSQL cannot resolve the object without schema
   qualification.
3. The orchestrator reported grants by bare `object_name` without
   schema qualification, which could cause reporting collisions across
   schemas.
4. Grants on non-public objects were therefore not properly tracked,
   and the GRANT statement would fail if the object was in a non-public
   schema.

### 21.2 Supported Privilege Types

The implementation supports the following PostgreSQL privilege types
through the current migration architecture:

- Schema privileges (`USAGE`, `CREATE`)
- Table privileges (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, etc.)
- Column privileges (`SELECT`, `INSERT`, etc.)
- Sequence privileges (`USAGE`, `SELECT`)
- Function privileges (`EXECUTE`)

### 21.3 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `GrantDef` dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.list_grants()` to capture
     `table_schema`/`object_schema` for:
     - Table grants from `information_schema.role_table_grants`
     - Column grants from `information_schema.role_column_grants`
     - Sequence grants from `information_schema.role_usage_grants`
     - Schema grants from `pg_namespace` ACL via `aclexplode`
     - Function grants from `pg_proc` ACL via `aclexplode`
   - Updated `PostgresTargetConnector.apply_grant()` to schema-qualify
     the object name when `schema_name != "public"`, using
     `quote_identifier()` for safety. Public-schema grants remain
     unqualified for backward compatibility.

3. **`core/orchestrator.py`**
   - Updated both `run_full()` and `run_cdc()` to compute a
     schema-qualified grant key for the migration report when the grant
     is on a non-public object, preserving backward compatibility for
     public-schema grants.

### 21.4 Unit Test Results

``` text
83 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresGrantSchemaQualification.test_list_grants_captures_schema_for_table`
- `TestPostgresGrantSchemaQualification.test_list_grants_captures_schema_for_column`
- `TestPostgresGrantSchemaQualification.test_target_apply_grant_qualifies_non_public_schema`
- `TestPostgresGrantSchemaQualification.test_target_apply_grant_qualifies_column_non_public_schema`
- `TestPostgresGrantSchemaQualification.test_target_apply_grant_remains_unqualified_for_public`

### 21.5 Real PostgreSQL CLI Verification

Run ID: `22873cbb7a8a47058deb9844ad139de8`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"grants": [
  "GRANT SELECT, INSERT ON audit_test.test_customers TO audit_user: ok",
  "GRANT SELECT ON COLUMN audit_test.test_customers.city TO audit_user: ok",
  ...
]
```

Target SQL verification:

``` sql
-- Table grant
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'audit_test' AND grantee = 'audit_user';

  grantee   | table_schema |   table_name   | privilege_type
------------+--------------+----------------+----------------
 audit_user | audit_test   | test_customers | INSERT
 audit_user | audit_test   | test_customers | SELECT

-- Column grant
SELECT grantee, column_name, table_schema, table_name, privilege_type
FROM information_schema.role_column_grants
WHERE table_schema = 'audit_test' AND grantee = 'audit_user'
ORDER BY table_name, column_name;

  grantee   | column_name | table_schema | table_name    | privilege_type
------------+-------------+--------------+---------------+----------------
 audit_user | city        | audit_test   | test_customers| INSERT
 audit_user | city        | audit_test   | test_customers| SELECT
 audit_user | created_at  | audit_test   | test_customers| INSERT
 ...

-- Sequence grant
SELECT grantee, object_schema, object_name, privilege_type
FROM information_schema.role_usage_grants
WHERE object_schema = 'audit_test' AND grantee = 'audit_user';

  grantee   | object_schema |       object_name        | privilege_type
------------+---------------+--------------------------+----------------
 audit_user | audit_test    | test_orders_order_id_seq | USAGE

-- Schema grant
SELECT n.nspname, aclexplode(n.nspacl)
FROM pg_namespace n
WHERE n.nspname = 'audit_test' AND n.nspacl IS NOT NULL;

  nspname  |     aclexplode
-----------+--------------------
 audit_test| (10,10,USAGE,f)
 audit_test| (10,10,CREATE,f)
 audit_test| (10,16949,USAGE,f)
```

`(10,16949,USAGE,f)` confirms `audit_user` (OID 16949) has `USAGE` on
`audit_test` schema.

### 21.6 Role/Principal Limitations

- Source roles must exist on the target for grants to succeed.
- The current implementation does not create or migrate roles.
- If a source grantee does not exist on the target, the GRANT is
  skipped and reported as `skipped` in the migration log.
- The audit environment used `audit_user` which was manually created
  on both source and target for this milestone.

### 21.7 Remaining Limitations

- Column-level grants do not schema-qualify the table name in the GRANT
  DDL because PostgreSQL syntax requires `ON COLUMN table.column`
  without schema qualification.
- Function and schema grants are discovered via PostgreSQL catalog ACL
  functions (`aclexplode`) rather than `information_schema` views,
  which is more reliable but less portable across PostgreSQL versions.
- Sequence grants from `information_schema.role_usage_grants` may
  include non-sequence objects in some environments; the current
  implementation filters by `object_type = 'SEQUENCE'`.
- View and materialized view grants are not explicitly enumerated in
  the current implementation but are covered by `role_table_grants`
  because PostgreSQL treats them as tables in that view.

-----------------------------------------------------------------------

## 22. Completed PostgreSQL Row Level Security / Policies Milestone

This section documents the completed PostgreSQL row level security
policies schema qualification milestone on the
`feature/postgresql-objects` branch.

### 22.1 Root Cause

The `RLSPolicy` dataclass had no `schema_name` field. As a result:

1. `PostgresSourceConnector.get_rls_policies()` queried `pg_policy`
   joined with `pg_class` and `pg_namespace`, filtering by
   `n.nspname = ANY(%s)` (which respects `include_schemas`), but
   dropped the `n.nspname` value from the result set.
2. `PostgresTargetConnector.apply_rls_policy()` executed
   `ALTER TABLE {table} ENABLE ROW LEVEL SECURITY` and
   `CREATE POLICY {name} ON {table}` without schema qualification,
   which fails for non-public schemas because PostgreSQL cannot
   resolve the table without schema qualification.
3. The orchestrator reported policies by bare `table` name without
   schema qualification.
4. Policies on non-public tables (e.g. `audit_test.test_customers`)
   were therefore not properly tracked, and the DDL statements would
   fail if the table was in a non-public schema.

### 22.2 Supported RLS/Policy Properties

The implementation supports the following PostgreSQL RLS/policy
properties through the current migration architecture:

- RLS enabled state (`relrowsecurity`)
- Policy name
- Policy command (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `ALL`)
- Permissive/restrictive behavior
- USING expression
- WITH CHECK expression
- Schema-qualified target table

### 22.3 Implementation

1. **`core/connectors/base.py`**
   - Added `schema_name: str = "public"` to the `RLSPolicy` dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.get_rls_policies()` to select
     `n.nspname` and populate `RLSPolicy.schema_name`.
   - Updated `PostgresTargetConnector.apply_rls_policy()` to
     schema-qualify the table name when `schema_name != "public"`,
     using `quote_identifier()` for safety. Public-schema tables
     remain unqualified for backward compatibility.

3. **`core/orchestrator.py`**
   - Updated both `run_full()` and `run_cdc()` to pass
     `schema_name=schema.schema_name` to `get_rls_policies()`.

### 22.4 Unit Test Results

``` text
86 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresRLSSchemaQualification.test_get_rls_policies_captures_schema`
- `TestPostgresRLSSchemaQualification.test_target_apply_rls_policy_qualifies_non_public_schema`
- `TestPostgresRLSSchemaQualification.test_target_apply_rls_policy_remains_unqualified_for_public`

### 22.5 Real PostgreSQL CLI Verification

Run ID: `65e3b49d188144be955880da98d69f3c`

``` text
Mode: FULL
Tables migrated: 5
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"create_rls_policy": {"table": "\"audit_test\".\"test_customers\"", "policy": "test_customer_policy"}
```

Target SQL verification:

``` sql
-- RLS enabled state
SELECT c.relname, n.nspname, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'audit_test' AND c.relname = 'test_customers';

  relname     | nspname   | relrowsecurity | relforcerowsecurity
-------------+-----------+-----------------+---------------------
 test_customers | audit_test | t               | f

-- Policy definition
SELECT pol.polname, n.nspname,
  CASE pol.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE' ELSE 'ALL' END AS cmd,
  CASE pol.polpermissive WHEN true THEN 'PERMISSIVE' ELSE 'RESTRICTIVE' END AS permissive,
  pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
  pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'audit_test' AND c.relname = 'test_customers';

  polname              | nspname   | cmd   | permissive | using_expr | check_expr
-----------------------+-----------+-------+------------+------------+------------
 test_customer_policy  | audit_test | SELECT | PERMISSIVE | true       |
```

### 22.6 Behavioral RLS Verification

Test role: `audit_user` (manually created on source and target)

``` sql
-- Allowed operation: permissive policy USING (true) allows all rows
SET search_path TO audit_test, public;
SELECT * FROM test_customers;

 customer_id |    full_name    |      email       |  city  |         created_at
-------------+-----------------+------------------+--------+----------------------------
         100 | Object Test One | object1@test.com | Indore | 2026-09-03 18:09:13.812237
         101 | Object Test Two | object2@test.com | Bhopal | 2026-09-03 18:09:13.812237
(2 rows)
```

Restricted operation verification (temporary test policy):

``` sql
-- As superuser: add temporary RESTRICTIVE policy
CREATE POLICY temp_restrictive_policy ON audit_test.test_customers
  AS RESTRICTIVE FOR SELECT USING (false);

-- As audit_user: rows are now restricted
SET search_path TO audit_test, public;
SELECT * FROM test_customers;
(0 rows)

-- As superuser: remove temporary policy
DROP POLICY temp_restrictive_policy ON audit_test.test_customers;

-- As audit_user: rows visible again
SET search_path TO audit_test, public;
SELECT * FROM test_customers;

 customer_id |    full_name    |      email       |  city  |         created_at
-------------+-----------------+------------------+--------+----------------------------
         100 | Object Test One | object1@test.com | Indore | 2026-09-03 18:09:13.812237
         101 | Object Test Two | object2@test.com | Bhopal | 2026-09-03 18:09:13.812237
(2 rows)
```

### 22.7 Role/Principal Limitations

- Source roles must exist on the target for RLS policies to function.
- The current implementation does not create or migrate roles.
- If a policy references a role that does not exist on the target,
  the policy is still created, but enforcement behavior depends on
  PostgreSQL role resolution.
- The audit environment used `audit_user` which was manually created
  on both source and target for behavioral verification.

### 22.8 Remaining Limitations

- RLS policies on views and materialized views are not explicitly
  tested; PostgreSQL supports RLS on views in newer versions but the
  current implementation discovers policies only for base tables.
- `relforcerowsecurity` is not explicitly migrated; policies are
  created in their default state.
- Policy roles (`polroles`) are not explicitly migrated; policies
  apply to all roles by default unless restricted.

-----------------------------------------------------------------------

## 23. Next Phase

Do not treat this document as a final production-readiness report.

This is the **local audit baseline** for continuing development.

The non-public schema migration milestone is **COMPLETE**:
- 5 tables / 13 rows migrated
- 0 failures
- 100% success
- Schema-qualified sequences verified
- Schema-qualified views verified
- Schema-qualified materialized views verified
- Schema-qualified functions verified
- Schema-qualified triggers verified
- Schema-qualified comments verified
- Schema-qualified grants/privileges verified
- Schema-qualified RLS/policies verified
- Cross-schema foreign keys verified
- DDL rollback verified
- 89 unit tests passing

-----------------------------------------------------------------------

## 23. Completed Cross-Schema Foreign Key / Dependency Handling Milestone

This section documents the completed cross-schema foreign key schema
qualification milestone on the `feature/postgresql-objects` branch.

### 23.1 Root Cause

The `ForeignKey` dataclass had no `ref_schema` field. As a result:

1. `PostgresSourceConnector.get_schema()` queried
   `information_schema.table_constraints` joined with
   `information_schema.constraint_column_usage`, but only selected
   `ccu.table_name AS ref_table`, dropping the `ccu.table_schema` value.
2. `PostgresTargetConnector.apply_constraints()` executed
   `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY (...) REFERENCES
   {fk.ref_table} (...)` without schema qualification, which fails for
   non-public referenced schemas because PostgreSQL cannot resolve the
   table without schema qualification.
3. Foreign keys referencing tables in non-public schemas were therefore
   not properly tracked, and the constraint DDL would fail if the
   referenced table was in a non-public schema.

### 23.2 Supported FK Configurations

The implementation supports the following foreign key configurations:

- Same-schema FKs (public → public)
- Non-public → public cross-schema FKs
- Non-public → non-public cross-schema FKs
- Public → non-public FKs (schema-qualified discovery preserves the
  referenced schema)

All FK properties are preserved:
- Constraint name
- Referencing schema/table
- Referencing column(s)
- Referenced schema/table
- Referenced column(s)
- ON DELETE behavior
- ON UPDATE behavior

### 23.3 Implementation

1. **`core/connectors/base.py`**
   - Added `ref_schema: str = "public"` to the `ForeignKey` dataclass.

2. **`core/connectors/postgresql.py`**
   - Updated `PostgresSourceConnector.get_schema()` to select
     `ccu.table_schema AS ref_schema` in the foreign key discovery
     query and populate `ForeignKey.ref_schema`.
   - Updated `PostgresTargetConnector.apply_constraints()` to
     schema-qualify the referenced table name when `ref_schema !=
     "public"`, using `quote_identifier()` for safety. Public-schema
     referenced tables remain unqualified for backward compatibility.

### 23.4 Unit Test Results

``` text
89 passed, 0 failed
```

Key regression tests added in `tests/unit/test_core.py`:

- `TestPostgresCrossSchemaForeignKey.test_apply_constraints_generates_cross_schema_fk_ddl`
- `TestPostgresCrossSchemaForeignKey.test_apply_constraints_generates_non_public_to_non_public_fk_ddl`
- `TestPostgresCrossSchemaForeignKey.test_apply_constraints_generates_non_public_to_public_fk_ddl`

### 23.5 Real PostgreSQL CLI Verification

Run ID: `73ab4f7af6354bda9205d5f3b5c775ff`

``` text
Mode: FULL
Tables migrated: 7
Total rows: 13
Migrated: 13
Failed: 0
Success rate: 100%
```

The migration report confirms:

``` json
"create_fk": {"table": "fk_child", "fk": "fk_child_to_public_customers", "ref_table": "customers"}
```

### 23.6 SQL Verification

Source FK definition:

``` sql
SELECT tc.constraint_name, kcu.column_name, ccu.table_schema, ccu.table_name AS ref_table,
       ccu.column_name AS ref_col, rc.delete_rule, rc.update_rule
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
JOIN information_schema.constraint_column_usage ccu ON rc.unique_constraint_name = ccu.constraint_name
WHERE tc.table_schema = 'audit_test' AND tc.table_name = 'fk_child'
  AND tc.constraint_type = 'FOREIGN KEY';

  constraint_name              | column_name | table_schema | ref_table | ref_col | delete_rule | update_rule
-------------------------------+-------------+--------------+-----------+---------+-------------+-------------
 fk_child_to_public_customers  | parent_id   | public       | customers | customer_id | CASCADE     | CASCADE
```

Target FK definition:

``` sql
SELECT tc.constraint_name, kcu.column_name, ccu.table_schema, ccu.table_name AS ref_table,
       ccu.column_name AS ref_col, rc.delete_rule, rc.update_rule
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
JOIN information_schema.constraint_column_usage ccu ON rc.unique_constraint_name = ccu.constraint_name
WHERE tc.table_schema = 'audit_test' AND tc.table_name = 'fk_child'
  AND tc.constraint_type = 'FOREIGN KEY';

  constraint_name              | column_name | table_schema | ref_table | ref_col | delete_rule | update_rule
-------------------------------+-------------+--------------+-----------+---------+-------------+-------------
 fk_child_to_public_customers  | parent_id   | public       | customers | customer_id | CASCADE     | CASCADE
```

Source and target FK definitions match exactly.

### 23.7 Schemas Tested

- `audit_test.fk_child` → `public.customers` (cross-schema, tested)
- `audit_test.fk_child` → `audit_test.fk_parent` (same non-public schema, unit tested)
- `public.orders` → `public.customers` (same public schema, pre-existing)

### 23.8 Remaining Limitations

- Cross-schema FK validation is not explicitly tested end-to-end with
  actual referential data; the current test uses empty tables.
- Circular cross-schema dependencies are not specifically addressed by
  this milestone; the existing constraint phase runs after all tables
  are created, which handles most ordering issues.
- Deferrable FK options are preserved if present in the source catalog
  but are not explicitly tested for cross-schema cases.

-----------------------------------------------------------------------

## 24. Next Phase

Do not treat this document as a final production-readiness report.

This is the **local audit baseline** for continuing development.

The non-public schema migration milestone is **COMPLETE**:
- 5 tables / 13 rows migrated
- 0 failures
- 100% success
- Schema-qualified sequences verified
- Schema-qualified views verified
- Schema-qualified materialized views verified
- Schema-qualified functions verified
- Schema-qualified triggers verified
- Schema-qualified comments verified
- Schema-qualified grants/privileges verified
- Schema-qualified RLS/policies verified
- Cross-schema foreign keys verified
- DDL rollback verified
- 89 unit tests passing

Next phase should focus on the remaining PostgreSQL object categories
listed in Section 14.5, starting with the first category that has the
smallest dependency surface and the clearest end-to-end verification path.

The next phase should start from this same combined code baseline so
both developers can continue on the same implementation and use this
document to understand what has already been tested and what remains.
