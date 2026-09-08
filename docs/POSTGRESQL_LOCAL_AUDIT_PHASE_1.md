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
63 passed, 0 failed
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

## 16. Next Phase

Do not treat this document as a final production-readiness report.

This is the **local audit baseline** for continuing development.

The non-public schema migration milestone is **COMPLETE**:
- 5 tables / 13 rows migrated
- 0 failures
- 100% success
- Schema-qualified sequences verified
- Schema-qualified views verified
- DDL rollback verified
- 63 unit tests passing

Next phase should focus on the remaining PostgreSQL object categories
listed in Section 14.5, starting with the first category that has the
smallest dependency surface and the clearest end-to-end verification path.

The next phase should start from this same combined code baseline so
both developers can continue on the same implementation and use this
document to understand what has already been tested and what remains.
