# MySQL Limitations

Documented limitations identified during the MySQL object migration audit
on the Unified DMS / Migration Platform Local → Local test environment
(MySQL Community Server 26.7.0).

The audit covered schema, tables, data, constraints, indexes, generated
columns, views, functions, procedures, triggers, events, partitions,
datatypes, comments, grants, dependencies, and end-to-end migration behavior.

Limitations are categorized as:

- **Implementation limitation** — behavior gap in the current migration code
- **Audit scope limitation** — not tested or not fully verified within the
  current audit
- **Environment limitation** — blocked by local server configuration,
  privileges, infrastructure, or external prerequisites
- **Engine capability limitation** — the target MySQL engine does not provide
  a direct equivalent of a source-engine feature

---

## Users, roles, and grants

### MySQL users and roles are not migrated

- **Category:** Implementation limitation
- **Impact:** The current MySQL connector does not discover or create MySQL
  users, roles, role assignments, authentication credentials, or password
  definitions on the target.
- **Workaround:** Create required users and roles on the target separately
  before or after migration.
- **Tracking:** Users, roles, and role assignments are outside the currently
  supported migration scope.

### Global and database-level privileges are not migrated

- **Category:** Implementation limitation
- **Impact:** Global privileges (`ON *.*`) and database-level privileges
  (`ON database.*`) are not discovered and reproduced by the current MySQL
  migration implementation.
- **Workaround:** Apply required global or database-level privileges manually
  on the target.
- **Tracking:** Only supported object-level grant types are processed.

### Table grants are conditionally supported

- **Category:** Implementation limitation
- **Impact:** Explicit table grants can be discovered from
  `INFORMATION_SCHEMA.TABLE_PRIVILEGES` and applied to the target, but the
  target grantee must already exist. The platform does not create the
  grantee account.
- **Workaround:** Create the target account first and ensure the migration
  account has sufficient privilege to apply the grant.
- **Tracking:** Table grants are supported and were exercised during the
  local audit.

### Routine `EXECUTE` grants depend on catalog visibility

- **Category:** Environment limitation
- **Impact:** Routine privileges depend on visibility of
  `INFORMATION_SCHEMA.ROUTINE_PRIVILEGES`. If the connected migration
  account cannot access that metadata, routine grant discovery is skipped
  without aborting supported table-grant processing.
- **Workaround:** Run the migration with an account that has the required
  metadata visibility, where organizational security policy permits it.
- **Tracking:** The connector intentionally degrades to the available grant
  metadata instead of requiring elevated system-schema access.

### Grant metadata can be invisible to the migration account

- **Category:** Environment limitation
- **Impact:** MySQL privilege metadata visible to an administrative account
  is not necessarily visible to the migration account. During the local
  audit, grants belonging to `migration_grant_test@localhost` were visible
  to the administrative account but not to the DMS account
  `mysql_test@%`.
- **Workaround:** Use an appropriately privileged migration account or
  perform grant administration separately.
- **Tracking:** The DMS does not require root or broad system-schema access
  merely to discover grants.

The platform intentionally does not rely on broad permissions such as
`GRANT SELECT ON mysql.*` as a normal migration prerequisite because that
would weaken the least-privilege boundary.

---

## Functions and triggers

### Function and trigger creation can be blocked by binary-log policy

- **Category:** Environment limitation
- **Impact:** When binary logging is enabled and
  `log_bin_trust_function_creators=OFF`, MySQL can reject function or trigger
  creation with error 1419 unless the executing account has the required
  administrative privileges.
- **Workaround:** A MySQL administrator can explicitly authorize the required
  server configuration, for example:

  `SET PERSIST log_bin_trust_function_creators = ON;`

  This is an administrator-controlled server configuration and is not changed
  automatically by the migration platform.
- **Tracking:** The local audit initially encountered this condition. After
  the administrator-approved persistent setting was applied and MySQL was
  restarted, functions and triggers migrated successfully.

The platform reports affected objects as `FUNCTION: BLOCKED` or
`TRIGGER: BLOCKED` instead of falsely reporting them as migrated.

### Function and trigger migration remains dependent on target privileges

- **Category:** Environment limitation
- **Impact:** Even when the binary-log policy is satisfied, creation can fail
  if the target migration account does not have the privileges required by
  MySQL for the object or its body.
- **Workaround:** Grant the required target privileges or use an approved
  migration account.
- **Tracking:** The platform does not automatically elevate privileges.

---

## Events

### Event Scheduler is a server-level prerequisite

- **Category:** Environment limitation
- **Impact:** MySQL events require the Event Scheduler and appropriate target
  privileges. An event can be created successfully as metadata but cannot
  execute as intended when the required server capability or privileges are
  unavailable.
- **Workaround:** Enable/configure Event Scheduler according to the target
  server's administrative policy and provide the required privileges.
- **Tracking:** The migration fixture included a one-time event and verified
  event metadata on the target.

### Event execution semantics depend on target server state

- **Category:** Environment / audit scope limitation
- **Impact:** Event execution is affected by Event Scheduler state, schedule,
  event status, and target server timing. Metadata migration and functional
  execution therefore need to be considered separately.
- **Workaround:** Validate migrated event metadata first, then validate
  execution in an environment where Event Scheduler behavior is explicitly
  controlled.
- **Tracking:** The local audit used a safe one-time event fixture to avoid
  ambiguity from unrelated active events.

---

## FULL migration and target reconciliation

### FULL migration does not remove unmanaged target-only objects

- **Category:** Implementation limitation
- **Impact:** FULL migration reconciles objects that belong to the migration
  scope, but it does not automatically delete unrelated objects that already
  exist only on the target database.
- **Workaround:** Remove unmanaged target-only objects separately when an
  exact target mirror is required.
- **Tracking:** A manually created `child_cross_schema_test` object was
  present only on the target during E2E comparison. It was removed manually
  before the final comparison.

This behavior prevents the migration platform from deleting arbitrary
target objects that are outside the requested migration scope.

### Existing target table structure may require schema reconciliation

- **Category:** Implementation/configuration limitation
- **Impact:** An existing target table whose structure differs from the source
  cannot be treated as structurally equivalent merely by clearing and
  reloading its rows.
- **Workaround:** Enable the supported target schema reconciliation option
  (`migration.reconcile_target_schema: true`) when FULL migration is expected
  to reconcile incompatible target table structures.
- **Tracking:** This was specifically identified during partition testing.
  The implementation was enhanced to replace incompatible target structures
  safely and was then verified with the partitioned MySQL fixture.

---

## Partitions

### Partition support is limited to supported MySQL partition definitions

- **Category:** Audit scope limitation
- **Impact:** The current audit directly exercised a MySQL `RANGE` partition
  definition using `YEAR(created_at)`, including ordered partitions and
  `MAXVALUE`. Other valid MySQL partition expressions and combinations were
  not exhaustively exercised in live E2E testing.
- **Workaround:** Validate additional partition strategies separately when
  they are outside the tested fixture.
- **Tracking:** Supported implementation includes MySQL table-level
  `RANGE`, `RANGE COLUMNS`, `LIST`, `LIST COLUMNS`, `HASH`, and `KEY`
  definitions. The tested `tbl_partition_test` structure was successfully
  reconciled and verified on the target.

Partition creation is part of the table DDL in MySQL; it does not require a
separate partition plugin or restart in the tested MySQL 26.7.0 environment.

---

## Views

### Cross-database view dependencies have a deliberate rewrite boundary

- **Category:** Implementation limitation
- **Impact:** The view migration logic rewrites source database qualifiers
  when they refer to migrated objects so that migrated views can reference
  target-local objects. It does not rewrite arbitrary cross-database
  references, literals, comments, or unrelated SQL text.
- **Workaround:** Review views containing intentional external database
  dependencies and adjust them separately when those dependencies are not
  part of the migration scope.
- **Tracking:** The local `customer_order_summary` view was verified against
  target-local `customers` and `orders`.

---

## Cross-schema / cross-database dependencies

### Cross-database dependencies require target dependency availability

- **Category:** Audit scope limitation
- **Impact:** MySQL databases act as namespaces, and objects can reference
  objects in another database. The current audit verified a cross-database
  foreign-key scenario, but arbitrary circular and complex cross-database
  dependency graphs were not exhaustively exercised.
- **Workaround:** Ensure referenced databases and objects are migrated or
  otherwise available on the target before dependent objects are created.
- **Tracking:** Supported dependency ordering and foreign-key recreation were
  verified for the tested scenarios.

### Circular cross-database dependencies were not exhaustively tested

- **Category:** Audit scope limitation
- **Impact:** The migration orchestrator creates supported constraints after
  the required tables exist, which handles normal dependency ordering.
  Complex mutually dependent cross-database objects were not exhaustively
  exercised in the local fixture.
- **Workaround:** Validate circular dependency graphs separately before
  production migration.
- **Tracking:** Acknowledged as unverified beyond the tested dependency
  scenarios.

---

## Datatypes

### Datatype coverage beyond the representative fixture is not exhaustive

- **Category:** Audit scope limitation
- **Impact:** The audit included a representative 31-column MySQL datatype
  fixture covering numeric, character, text, binary/LOB, date/time, JSON,
  ENUM, SET, and related definitions. It does not constitute exhaustive
  coverage of every MySQL datatype variant and edge case.
- **Workaround:** Add targeted fixtures for datatype variants required by a
  production migration.
- **Tracking:** The representative datatype fixture migrated successfully,
  including values and target metadata.

### MySQL `SET` values require connector normalization

- **Category:** Implementation limitation
- **Impact:** MySQL `SET` values are returned by the Python driver as a
  collection type. The connector must serialize the collection into MySQL's
  comma-separated representation before target insertion.
- **Workaround:** None required for the supported implementation.
- **Tracking:** This was identified and fixed during the datatype audit.
  The successful datatype migration verified the corrected behavior.

This is retained as a historical implementation consideration rather than
an unresolved MySQL engine limitation.

---

## Comments and metadata

### Comment coverage is limited to supported metadata objects

- **Category:** Audit scope limitation
- **Impact:** The audit verified table and column comments. Other MySQL
  metadata/comment locations outside the supported discovery and application
  paths were not exhaustively tested.
- **Workaround:** Validate additional metadata types separately when required.
- **Tracking:** Table and column comment migration was fixed and verified
  during the local audit.

---

## DDL and transactional behavior

### MySQL DDL is not transactionally equivalent to PostgreSQL DDL

- **Category:** Engine capability limitation
- **Impact:** MySQL DDL has different transactional and implicit-commit
  semantics from PostgreSQL. A later migration failure cannot be assumed to
  roll back every previously committed DDL operation.
- **Workaround:** Use the migration platform's error isolation, reconciliation,
  validation, and cleanup behavior rather than relying on one database-wide
  DDL transaction.
- **Tracking:** The orchestrator isolates object failures and records
  per-object migration status.

---

## MySQL namespace model

### MySQL databases are not equivalent to PostgreSQL schemas

- **Category:** Engine capability limitation
- **Impact:** MySQL primarily uses databases as object namespaces, whereas
  PostgreSQL supports multiple schemas within a database. A cross-engine
  PostgreSQL-to-MySQL migration therefore cannot assume a one-to-one
  translation of PostgreSQL schema semantics.
- **Workaround:** Define the target database/namespace mapping explicitly
  during migration planning.
- **Tracking:** This is a cross-engine architectural difference rather than
  a MySQL local-to-local migration failure.

---

## PostgreSQL-specific capabilities without direct MySQL equivalents

### Materialized views, RLS, extensions, domains, and standalone sequences

- **Category:** Engine capability limitation
- **Impact:** MySQL does not provide direct native equivalents for several
  PostgreSQL-specific features, including PostgreSQL materialized views,
  Row Level Security policies, extensions, domains/custom types, and
  standalone sequences with PostgreSQL semantics.
- **Workaround:** Implement an application-specific or MySQL-native
  alternative where appropriate. Such alternatives are not automatically
  generated by the current MySQL connector.
- **Tracking:** These are capability differences between database engines,
  not silent migration skips.

---

## Azure / remote-server testing

### Azure MySQL E2E coverage remains environment-dependent

- **Category:** Environment limitation
- **Impact:** Azure connectivity depends on network access, firewall/IP
  allow-listing, TLS configuration, approved secrets, and target privileges.
  Local-to-local verification does not prove Azure end-to-end compatibility.
- **Workaround:** Run the migration in the approved Azure environment with
  the required connection, TLS, secret, network, and privilege configuration.
- **Tracking:** Azure-specific E2E verification was not completed as part of
  the local audit.

No host-name-specific migration code path is required; differences are
expected to come from configuration, connectivity, TLS, permissions, and
server policy.

---

## Audit coverage limitations

### Production-scale and exhaustive object coverage was not performed

- **Category:** Audit scope limitation
- **Impact:** The local audit provides focused functional evidence across the
  supported MySQL object categories but does not represent every possible
  production schema shape, SQL expression, privilege model, workload size,
  or dependency graph.
- **Workaround:** Execute production-specific pre-migration assessment and
  targeted validation fixtures before production use.
- **Tracking:** The audit deliberately distinguishes tested scenarios from
  unsupported or unverified scenarios.

---

## Resolved implementation findings

The following issues were discovered during the MySQL audit and fixed. They
are **not current unresolved limitations**, but are retained here so the
audit history remains traceable.

### Default-value quoting

Source default metadata was initially emitted without the quoting required
by MySQL, causing table creation failures for string defaults.

**Status:** Fixed and verified.

### Generated-column data loading

The loader initially attempted to insert generated-column values rather than
allowing MySQL to calculate them.

**Status:** Fixed and verified.

### CHECK constraint escaping

Escaped catalog expressions were initially reused incorrectly when generating
target CHECK constraints.

**Status:** Fixed and verified.

### Foreign-key namespace mapping

Source database qualifiers were initially retained in target foreign-key
definitions.

**Status:** Fixed and verified with valid and invalid FK runtime tests.

### Routine DDL extraction

The implementation initially selected the wrong `SHOW CREATE` output field
for some routine objects.

**Status:** Fixed and verified.

### Trigger connection lifecycle

Open source read transactions and connector cleanup behavior could leave
connections in an undesirable state and interfere with trigger DDL.

The fix added source autocommit, rollback-safe cleanup, and failed-DDL
rollback handling.

**Status:** Fixed and verified.

### Grant discovery fallback

Routine grant discovery could abort the broader grant phase when
`ROUTINE_PRIVILEGES` was unavailable.

**Status:** Fixed. Table-grant discovery continues when routine-grant
metadata is unavailable.

### Cross-engine type safety

MySQL/MSSQL target DDL previously fell back to the source-native type when a
cross-engine mapping was missing.

**Status:** Fixed. Missing cross-engine mappings now raise an explicit
`UnmappedTypeError` instead of silently producing unsafe target DDL.

---

## Final verified disposition

The final MySQL Local → Local verification demonstrated successful migration
of the tested supported object set, including:

- tables and data
- columns and datatypes
- primary keys
- unique constraints
- foreign keys
- CHECK constraints
- indexes
- AUTO_INCREMENT
- generated columns
- defaults
- views
- functions
- procedures
- triggers
- events
- partitions
- table and column comments
- supported grants

The final verified run completed with **11 tables, 44/44 rows migrated,
0 errors, Functions/Procedures 2/2, Triggers 2/2**, after the required
administrator-approved MySQL binary-log function/trigger policy was enabled.

The remaining limitations in this document are therefore primarily
**unsupported object/security scope, environment prerequisites, engine
capability differences, and audit-coverage boundaries**, rather than known
failures in the verified Local → Local migration path.