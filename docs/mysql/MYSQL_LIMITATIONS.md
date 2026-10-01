# MySQL Limitations

This document records current limits and validation boundaries for the MySQL migration path. It separates implementation behavior, audit coverage, environment dependencies, and differences between database engines. A source object count of zero means **Not exercised** in that run; it does not mean unsupported.

## A. Implementation Limitations

### User credentials and privilege scope

- **Category:** Implementation limitation
- **Impact:** MySQL account discovery filters locked accounts. User/direct-permission migration does not copy authentication credentials or password definitions. Role memberships and some privilege scopes are outside the supported MySQL security path; global privileges are handled only for explicitly selected users and a supported subset. Table, database, column, and routine grants depend on the implemented grant path and source metadata visibility. A grantee may need to exist on the target before a grant can be applied.
- **Workaround:** Provision target authentication separately. Select intended accounts using the supported security-user configuration/CLI option, review reported grants, and apply out-of-scope permissions separately under the site's security policy.
- **Tracking:** `core/connectors/mysql.py`, `migration_platform/__main__.py`, `tests/unit/test_mysql_datatypes.py`, and [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md). Do not grant broad `SELECT ON mysql.*` or `SUPER` as a routine migration prerequisite.

### FULL migration retains unrelated target-only objects

- **Category:** Implementation safety boundary
- **Impact:** FULL migration processes objects in the source migration scope; it does not sweep the target and delete arbitrary objects absent from the source. This includes Events not present in the source snapshot. Extra target objects can therefore remain after a successful run.
- **Workaround:** Use a clean dedicated target for exact source/target comparisons. Remove unrelated target-only objects separately only through an approved environment cleanup process.
- **Tracking:** MySQL event tests assert there is no target-wide Event pruning; the Azure audit records retained target-only Events. This is intentional protection against deleting application-owned or out-of-scope objects, not a migration failure.

### Target schema reconciliation is scoped and opt-in

- **Category:** Implementation/configuration limitation
- **Impact:** `migration.reconcile_target_schema: true` enables the implemented reconciliation path for supported MySQL target structure mismatches; it is not a general-purpose repair for every schema difference. Clearing rows does not make incompatible DDL equivalent.
- **Workaround:** Prefer a clean target for ordinary E2E tests. Use a disposable target and enable reconciliation only for a dedicated, supported mismatch scenario.
- **Tracking:** The option is present in MySQL configs and the orchestrator checks it before reconciliation. The local audit documents tested partition reconciliation; see [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md) and [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md).

### View rewriting has a defined boundary

- **Category:** Implementation limitation
- **Impact:** The connector rewrites source database qualifiers when they identify migrated dependencies. It does not rewrite arbitrary SQL text, string literals, comments, or unrelated external database references. A view that intentionally depends on an external database may continue to do so.
- **Workaround:** Review cross-database view dependencies and ensure external objects exist on the target, or adjust those definitions separately.
- **Tracking:** `core/connectors/mysql.py`; local audit evidence verifies migrated views against target-local objects.

## B. Validation / Audit Scope Limitations

### Datatype coverage is representative, not exhaustive

- **Category:** Validation/audit scope limitation
- **Impact:** The checked fixture covers representative MySQL numeric, text/character, binary, temporal and MySQL-specific types, but not every server version, type variant, expression, collation, or edge value.
- **Workaround:** Add focused data and metadata validation for production-specific type variants.
- **Tracking:** Local audit and `tests/unit/test_mysql_datatypes.py`; the tests include SET normalization. Successful representative coverage is not an exhaustive compatibility claim.

### Partition coverage is narrower than implementation scope

- **Category:** Validation/audit scope limitation
- **Impact:** Connector DDL generation handles MySQL `RANGE`, `RANGE COLUMNS`, `LIST`, `LIST COLUMNS`, `HASH`, and `KEY` partition methods. Live audit coverage is narrower: the documented local fixture exercises a representative RANGE/YEAR partition case. Other methods and combinations are not exhaustively validated in live E2E.
- **Workaround:** Compare source/target `SHOW CREATE TABLE` and `information_schema.partitions` for required production strategies; create focused tests for any unexercised method.
- **Tracking:** `core/connectors/mysql.py`, [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md), and [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md).

### Cross-database dependency graphs are not exhaustively validated

- **Category:** Validation/audit scope limitation
- **Impact:** Tested cross-database foreign-key/dependency behavior does not establish correctness for every dependency shape. Circular or complex cross-database graphs have not been exhaustively exercised.
- **Workaround:** Ensure referenced databases/objects are available and in scope; validate complex or circular dependencies separately before production migration.
- **Tracking:** Local audit documents tested cross-database FK behavior. MySQL databases act as object namespaces; this does not imply arbitrary dependency graphs were tested.

### Comments and metadata coverage

- **Category:** Validation/audit scope limitation
- **Impact:** Table and column comments are within the implemented/tested path. Other possible metadata locations outside that path are not established by those tests.
- **Workaround:** Identify required metadata types explicitly and verify them with a focused fixture.
- **Tracking:** MySQL connector metadata queries and local audit evidence for table/column comments.

### Production-scale and unusual workloads

- **Category:** Validation/audit scope limitation
- **Impact:** E2E evidence does not cover every production data volume, long-running workload, unusual SQL expression, privilege model, dependency graph, or server/version combination.
- **Workaround:** Run a production-specific assessment and representative load/validation tests.
- **Tracking:** Audits describe concrete fixtures and directions; consult [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md) for repeatable checks.

## C. Environment / Pre-existing State Limitations

### Function and trigger creation can be blocked by binary-log policy

- **Category:** Environment limitation
- **Impact:** When `log_bin=ON` and `log_bin_trust_function_creators=OFF`, MySQL may reject function or trigger creation with Error 1419, depending on privileges. A blocked object is not evidence that the migration code lacks support.
- **Workaround:** Check `SHOW VARIABLES LIKE 'log_bin';` and `SHOW VARIABLES LIKE 'log_bin_trust_function_creators';`. If approved, a MySQL administrator may apply `SET PERSIST log_bin_trust_function_creators = ON;`. The migration platform does not change this server setting or acquire `SUPER`. If the prerequisite cannot be met, report the object as blocked/environment-dependent.
- **Tracking:** Local audit documents the policy prerequisite and successful function/trigger migration after administrator action; `core/orchestrator.py` reports blocked function/trigger cases.

### Grant discovery depends on privilege metadata visibility

- **Category:** Environment limitation
- **Impact:** `INFORMATION_SCHEMA.TABLE_PRIVILEGES` and `ROUTINE_PRIVILEGES` visibility depends on the migration account. If routine privilege metadata is unavailable, routine grant discovery can be skipped while supported grant processing continues. A missing visible grant is not proof that the grant does not exist.
- **Workaround:** Use an appropriately authorized account where policy permits, or handle those grants separately. Avoid broad system-schema access solely to force a test pass.
- **Tracking:** `core/connectors/mysql.py` and `tests/unit/test_mysql_datatypes.py`; local audit records account-dependent visibility.

### Target grantee and stored-object privileges

- **Category:** Environment limitation
- **Impact:** Applying grants requires target privileges and may require the grantee account to exist. Creating routines, triggers, events, or users also depends on target authorization and server policy.
- **Workaround:** Provision accounts and required privileges through approved DBA procedures; review per-object results.
- **Tracking:** MySQL connector grant/security paths and limitations documented in [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md).

### Event Scheduler runtime is environment-dependent

- **Category:** Environment limitation
- **Impact:** Event definition migration and Event runtime are different checks. Automatic execution depends on `event_scheduler`, event status and schedule, target time/state, definer, and privileges. The Azure audit observed `event_scheduler=OFF`; an attempt to enable it with the migration account returned Error 1227. Azure event metadata/state was checked, but automatic runtime was not verified.
- **Workaround:** Verify `SHOW VARIABLES LIKE 'event_scheduler';` and event metadata. Have the server administrator configure the scheduler when supported and authorized, then test a controlled event. Do not treat `STATUS=ENABLED` alone as proof of execution.
- **Tracking:** [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md); runtime remains blocked/not executed in the documented Azure environment.

### One-time Event safety window

- **Category:** Implementation safety behavior / validation scope
- **Impact:** An enabled one-time Event due, past due, or within `migration.one_time_event_safety_lead_seconds` (default 300 seconds) is blocked because the original execution timing cannot safely be preserved. The orchestrator snapshots and rechecks event timing; unsafe events are not created/replaced on the target. The platform does not enable an expired event to compensate.
- **Workaround:** Schedule the source event sufficiently far in the future and rerun in a controlled environment. Keep scheduler/runtime validation separate from DDL migration.
- **Tracking:** `core/orchestrator.py` and `tests/unit/test_mysql_events.py` verify due/near events are blocked without target replacement. Azure event metadata runs do not establish runtime behavior for every event schedule.

### Network, TLS, authentication, and managed-server policy

- **Category:** Environment limitation
- **Impact:** Remote/Azure connectivity requires reachable endpoints, firewall/network access, correct TLS settings, credentials, and target privileges. Failures before source/target operations do not establish an object-migration defect.
- **Workaround:** Confirm endpoint, port 3306, TLS, firewall allow-list, secrets, and account grants before rerunning.
- **Tracking:** Both Local → Azure and Azure → Local have documented E2E results; exact prerequisites remain environment-specific.

### Pre-existing target contents affect comparisons

- **Category:** Pre-existing-state limitation
- **Impact:** Existing target rows or objects can affect counts and object comparisons. In particular, target-only objects are intentionally retained; successful migration does not mean the target is an exact mirror.
- **Workaround:** Begin with a clean dedicated target or record known pre-existing state. Never clean a shared/production target as part of an audit.
- **Tracking:** The local and Azure audits call out target-state boundaries; FULL mode does not prune unmanaged target-only objects.

## D. MySQL / Engine Capability Limitations

### DDL transaction behavior differs between engines

- **Category:** Engine capability limitation
- **Impact:** MySQL DDL has implicit-commit and transactional behavior that differs from PostgreSQL. A later failure cannot be assumed to roll back all earlier DDL in a migration.
- **Workaround:** Use object-level error reporting, supported reconciliation, validation, and an approved cleanup/retry plan rather than relying on one database-wide DDL transaction.
- **Tracking:** MySQL orchestrator isolates and records object outcomes; this is an engine semantic difference, not a MySQL defect.

### MySQL databases are not PostgreSQL schemas

- **Category:** Engine capability limitation
- **Impact:** MySQL uses databases as object namespaces and does not provide the same namespace model as PostgreSQL schemas. Cross-engine migration cannot assume a one-to-one semantic mapping.
- **Workaround:** Decide and validate database/namespace mapping during migration planning.
- **Tracking:** Documented in [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md).

### Source-engine features without direct MySQL equivalents

- **Category:** Engine capability limitation
- **Impact:** MySQL has no direct native equivalent for PostgreSQL materialized views, RLS policies, extensions, domains/custom types, or standalone sequences with PostgreSQL semantics. These are cross-engine capability differences, not MySQL-to-MySQL migration failures. MySQL `AUTO_INCREMENT` is table-bound and is not a standalone sequence equivalent.
- **Workaround:** Design an application-specific or MySQL-native alternative where needed; do not expect automatic semantic conversion.
- **Tracking:** MySQL connector capability declarations and [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md).

## E. Azure / Remote MySQL Limitations

### Azure validation is direction-specific and incomplete for some runtime behavior

- **Category:** Validation/audit scope and environment limitation
- **Impact:** Repository evidence includes Local → Azure and Azure → Local runs; these prove only the recorded dataset, configuration, and direction. They do not prove all objects or runtimes behave identically across Azure/server versions. The documented Azure scheduler was OFF, blocking automatic Event runtime verification. Function/trigger creation remains subject to Azure target policy and privileges.
- **Workaround:** Run the relevant direction with approved TLS/network/secrets/privileges and verify target metadata and data. Record metadata migration separately from functional runtime.
- **Tracking:** Local → Azure run `9f15eae2f1a84933a7ffe9746b828932` is recorded as 6 tables, 6,340 source/migrated rows, 0 failed, 100%. Azure → Local run `fb374e4480d84894b22d5917807b507f` is recorded as 13 tables, 45 source/migrated rows, 0 failed, 100%. See [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md), [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md), and [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md). These outcomes must not be combined as one run or direction.

## F. Resolved Implementation Findings

The following are historical defects, not current limitations, based on the repository audit and current code/tests:

| Finding | Resolution / evidence |
|---|---|
| Default-value quoting | MySQL default expressions are rendered with appropriate quoting; documented as fixed and verified in the local audit. |
| Generated-column loading | Generated values are omitted from ordinary load DML so MySQL computes them; audit records recalculation validation. |
| CHECK expression escaping | Escaped catalog expressions are handled when producing target CHECK DDL; audit records constraint validation. |
| Foreign-key database mapping | Migrated dependency references are mapped to target database names; audit records valid/invalid FK checks. |
| Routine DDL extraction | Uses authoritative `SHOW CREATE` output; documented routine migration/runtime evidence. |
| Trigger connection lifecycle | Connector cleanup/rollback behavior was corrected; local audit records successful trigger migration/runtime checks. |
| Grant discovery fallback | Unavailable routine privilege metadata no longer aborts supported table-grant processing; covered by `tests/unit/test_mysql_datatypes.py`. |
| Cross-engine type safety | Missing target type mapping raises `UnmappedTypeError` rather than silently emitting source-native DDL; covered by current implementation and cross-engine tests. |
| MySQL SET value serialization | Driver-returned collection values are normalized to the declared SET representation; `tests/unit/test_mysql_datatypes.py` covers the behavior and the local audit records datatype validation. |

## Final Verified Disposition

The repository documents successful MySQL migrations in Local → Azure and Azure → Local directions, and a successful local report. The latest checked-in Local report is `reports/1584f5a5ebc04e8a8d9e288ee150b23c.json` (5 tables, 5,710 source/migrated rows, success); the audit also retains a historical Local → Local result of 11 tables and 44/44 rows. These are distinct runs and must not be conflated.

The evidence supports the tested object sets, not every MySQL feature or production schema. Security/grant migration is scoped and visibility-dependent; routine/trigger creation can be blocked by server policy; Event metadata does not establish runtime; Azure E2E exists in both directions but some Azure runtime behavior remains unverified. Partition strategies, complex dependency graphs, type variants, and production-scale behavior remain audit-scope boundaries. MySQL DDL and namespace behavior remain engine differences. The resolved findings above are not current known failures.

## Reference

- [MySQL Local Audit](MYSQL_LOCAL_AUDIT.md)
- [MySQL Local → Azure Audit](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
- [MySQL Object Support Matrix](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- [MySQL Test Guide](MYSQL_TEST_GUIDE.md)
- [MySQL E2E Runbook](MYSQL_E2E_RUNBOOK.md)
- [MySQL Migration Flow](MYSQL_MIGRATION_FLOW.md)

