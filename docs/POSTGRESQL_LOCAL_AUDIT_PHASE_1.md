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
  Area                    Status                  Finding
  ----------------------- ----------------------- -----------------------
  Combined unit tests     PASS                    54/54

  PostgreSQL local        PASS                    Source/target reachable
  connection

  PostgreSQL FULL         PASS                    Migration completes
  migration

  Public table migration  PASS                    3 tables / 10 rows

  Basic validation        PARTIAL                 Stale deleted customer
                                                  remains

  HTML report             PASS                    Generated

  JSON report             PASS                    Generated

  Audit log               PASS                    Generated

  Repeat FULL migration   PASS                    Current source rows
                                                  migrate

  Source DELETE           GAP                     Stale target row
  synchronization in FULL                         remains
  mode

  Non-public schema       PASS                    `audit_test` created
  creation

  Non-public schema table GAP                     0 tables migrated
  migration

  Advanced objects in     PENDING                 Discovery/processing
  `audit_test`                                    issue must be
                                                  investigated first

  CDC prerequisite        BLOCKED                 `wal_level=logical` not
                                                  active

  CDC incremental         PENDING                 Not yet tested

  CDC continuous          PENDING                 Not yet tested

  MSSQL integration       BLOCKED                 ODBC Driver 18
                                                  unavailable
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 13. Next Phase

Do not treat this document as a final production-readiness report.

This is the **local audit baseline** for continuing development.

Next phase should investigate the non-public-schema discovery/processing
path and then verify:

1.  `audit_test.test_customers` migration
2.  `audit_test.test_orders` migration
3.  Row counts/data correctness
4.  View migration
5.  Materialized view migration
6.  Function/procedure migration
7.  Trigger migration
8.  Constraints/indexes
9.  RLS/policies
10. Sequence handling
11. Schema-qualified data loading
12. Failure/recovery behavior
13. Validation behavior
14. CDC once PostgreSQL logical WAL is available

The next phase should start from this same combined code baseline so
both developers can continue on the same implementation and use this
document to understand what has already been tested and what remains.
