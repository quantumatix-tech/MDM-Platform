# PostgreSQL Limitations

Documented limitations identified during the PostgreSQL object migration audit
on the `feature/postgresql-objects` branch (PostgreSQL 17.4, 94 unit tests
passing, commit `906dc89`).

Limitations are categorized as:
- **Implementation limitation** — behavior gap in the current migration code
- **Audit scope limitation** — not tested or verified within the current audit
- **Environment limitation** — blocked by local infrastructure or prerequisites

## Custom types

### `create_type()` hardcodes public schema in existence check

- **Category:** Implementation limitation
- **Impact:** `CREATE TYPE` (ENUM, DOMAIN, COMPOSITE) is created only in the
  `public` schema.  Non-public types are skipped because the existence check
  queries `pg_type` joined with `pg_namespace` filtering by `n.nspname =
  'public'`, ignoring the source type's actual schema.
- **Workaround:** Keep custom types in the `public` schema.
- **Tracking:** Documented in `POSTGRESQL_OBJECT_SUPPORT_MATRIX.md` as Partial.

### Non-public type existence check

- **Category:** Implementation limitation
- **Impact:** Same root cause as above.  Even if a type exists in a non-public
  schema, `create_type()` cannot discover it and attempts to recreate it.
- **Workaround:** None within current implementation.

## Partitions

### `create_partition()` hardcodes public schema

- **Category:** Implementation limitation
- **Impact:** The existence check filters by `n.nspname = 'public'`, so
  partition children in non-public schemas are never found.  Additionally, the
  `PARTITION OF` clause is not schema-qualified, which would fail for non-public
  parent tables even if the existence check were fixed.
- **Workaround:** Do not migrate non-public partitions with the current code.
- **Tracking:** Documented as Out of Scope in the support matrix.

## Trigger state

### Trigger enabled/disabled state not migrated

- **Category:** Implementation limitation
- **Impact:** Triggers are always created in their default enabled state.
  The source catalog `pg_trigger.tgenabled` is not read, and `ALTER TRIGGER ...
  ENABLE / DISABLE` is not emitted.
- **Workaround:** Manually enable or disable triggers on the target after
  migration.
- **Tracking:** Listed as Partial in the support matrix.

## Row Level Security

### `FORCE ROW LEVEL SECURITY` not migrated

- **Category:** Implementation limitation
- **Impact:** `relforcerowsecurity` is not read from the source catalog and
  not applied on the target.  Policies are created with PostgreSQL's default
  (`FORCE ROW LEVEL SECURITY` = off).
- **Workaround:** Manually enable `FORCE ROW LEVEL SECURITY` on target tables
  if required.
- **Tracking:** Listed as Partial in the support matrix.

### RLS policy roles (`polroles`) not migrated

- **Category:** Implementation limitation
- **Impact:** PostgreSQL policies can be restricted to specific roles via
  `polroles`.  The current implementation creates policies that apply to all
  roles by default.
- **Workaround:** Manually adjust policy roles on the target.
- **Tracking:** Listed as Partial in the support matrix.

## Comments

### Minor comment-object coverage gaps

- **Category:** Audit scope limitation
- **Impact:** `list_comments()` enumerates comments on schemas, tables,
  columns, views, materialized views, functions, and sequences.  Comments on
  indexes, constraints, and other minor object types are not explicitly
  enumerated.
- **Workaround:** Manually apply missing comments if needed.
- **Tracking:** Listed as Partial in the support matrix.

## Deferrable constraints

### Deferrable FK options not explicitly tested end-to-end

- **Category:** Audit scope limitation
- **Impact:** The source discovery captures `ON DELETE` and `ON UPDATE` actions.
  Deferrable (`INITIALLY DEFERRED` / `INITIALLY IMMEDIATE`) options are
  preserved in the catalog but were not explicitly tested for cross-schema
  foreign keys in the E2E fixture.
- **Tracking:** Acknowledged in the audit report but not blocking.

## Circular cross-schema dependencies

### Circular dependencies not specifically tested

- **Category:** Audit scope limitation
- **Impact:** Cross-schema FKs where tables mutually reference each other were
  not constructed in the E2E fixture.  The orchestrator creates constraints
  after all tables exist, which handles most ordering issues, but circular
  scenarios remain unverified.
- **Tracking:** Acknowledged in the audit report.

## Full mode stale-row behavior

### FULL mode does not delete stale target rows

- **Category:** Implementation limitation (documented behavioral gap)
- **Impact:** Running a subsequent FULL migration synchronizes current source
  rows but does not remove rows that were deleted from the source.
- **Workaround:** Manually delete stale rows or use CDC incremental mode once
  available.
- **Tracking:** Documented in `POSTGRESQL_LOCAL_AUDIT_PHASE_1.md` Section 5.

## Role migration

### Roles are not created or migrated

- **Category:** Implementation limitation
- **Impact:** If a source grantee or policy role does not exist on the target,
  grants are skipped and RLS policy enforcement depends on PostgreSQL's role
  resolution.
- **Workaround:** Manually create required roles on the target before running
  the migration.
- **Tracking:** Documented in the audit report.

## CDC — `wal_level` prerequisite

### Local CDC testing blocked by `wal_level = replica`

- **Category:** Environment limitation
- **Impact:** The local PostgreSQL instance runs with `wal_level = replica`.
  Logical replication CDC (`cdc-incremental` and `cdc-continuous` modes)
  requires `wal_level = logical`.  The platform includes a preflight check
  (`_ensure_wal_level_logical`) that gates CDC behind this prerequisite.
- **Workaround:** Configure the source PostgreSQL instance with
  `wal_level = logical` and restart the service.  Set
  `cdc.allow_source_service_restart = true` only if you explicitly authorize
  the platform to restart the service.
- **Tracking:** Environment Blocked in the support matrix.  This is **not** a
  migration implementation failure.

## MSSQL integration

### ODBC Driver 18 unavailable

- **Category:** Environment limitation
- **Impact:** MSSQL integration tests cannot run because Microsoft ODBC Driver
  18 is not installed on the local development machine.
- **Workaround:** Install the driver or run MSSQL tests in an environment with
  the driver available.
