# MySQL Local Audit — Final Report

> **Task 13 update:** Earlier statements that users/roles are unsupported
> predate the allowlisted security-principal implementation. Canonical Local →
> Azure evidence is in `MYSQL_LOCAL_TO_AZURE_AUDIT.md`, run
> `39c9933250be4f2daadacaf44ccdc45f`. Local → Local security-principal
> verification remains not yet executed.

**Project:** Migration Platform  
**Branch:** `feature/unified-dms-platform`  
**Environment:** MySQL Community Server 26.7.0, local source → local target  
**Host:** `127.0.0.1`  
**Port:** `3306`  
**Databases:** `mysql_migration_source` → `mysql_migration_target`  
**Migration mode:** `FULL`

---

## 1. Objective

Establish a reproducible baseline for MySQL object migration on the
`feature/unified-dms-platform` branch and verify the implemented Local → Local
FULL migration path.

The audit verifies discovery, creation, migration, target-side structural
reconciliation, data loading, runtime behavior, reporting, and validation for
the supported MySQL scope.

The audited scope includes:

- Databases / migration scope
- Tables and data
- Columns and primary keys
- AUTO_INCREMENT
- Generated columns
- UNIQUE constraints
- Secondary and composite indexes
- CHECK constraints
- Foreign keys
- Cross-schema foreign-key behavior
- Defaults
- Views and target-local dependencies
- Functions
- Procedures
- Triggers and trigger dependencies
- Events
- Partitions
- Representative MySQL datatypes
- Table and column comments
- Supported table and routine grants
- Target schema reconciliation
- Object-level failure isolation and reporting

Azure / remote MySQL was not part of this Local → Local audit.

---

## 2. Environment

| Item | Value |
|---|---|
| MySQL version | 26.7.0 Community Server |
| Host | `127.0.0.1` |
| Port | `3306` |
| Source DB | `mysql_migration_source` |
| Target DB | `mysql_migration_target` |
| Migration user | `mysql_test` |
| Migration mode | `full` |
| Config | `config/mysql_local_test.yaml` |
| Target schema reconciliation | Enabled for final reconciliation run |

The same local MySQL server was used as both source and target.

Function and trigger creation was initially blocked by the local binary-logging
policy. A MySQL administrator explicitly applied:

```sql
SET PERSIST log_bin_trust_function_creators = ON;
```

The setting was verified after MySQL restart. The migration platform itself does
not modify this server-global setting.

---

## 3. Test Fixture

The Local → Local audit used deterministic fixtures that were expanded during
testing as individual object gaps were identified.

The fixture set included:

- `customers`
- `products`
- `orders`
- `customer_audit`
- `customer_insert_log`
- `migration_log`
- `procedure_test_log`
- `manual_test_core`
- `tbl_partition_test`
- `tbl_datatype_test`
- Cross-schema FK test objects
- `customer_order_summary`
- Functions
- Procedures
- `trg_customer_status_audit`
- `trg_after_customer_insert_log`
- `evt_migration_e2e_final`
- `evt_event_migration_test`
- AUTO_INCREMENT columns
- Generated column `products.price_with_tax`
- UNIQUE, secondary, and composite indexes
- CHECK constraints
- Foreign-key dependencies
- Table and column comments
- Table-grant and routine-grant test scenarios

### Partition fixture

`tbl_partition_test` was created with:

```sql
CREATE TABLE tbl_partition_test (
    id INT NOT NULL,
    name VARCHAR(100) NOT NULL,
    created_at DATE NOT NULL,
    PRIMARY KEY (id, created_at)
)
PARTITION BY RANGE (YEAR(created_at)) (
    PARTITION p2025 VALUES LESS THAN (2026),
    PARTITION p2026 VALUES LESS THAN (2027),
    PARTITION pmax VALUES LESS THAN MAXVALUE
);
```

The fixture contained three rows.

### Datatype fixture

`tbl_datatype_test` contains 31 representative columns covering:

- Integer and numeric types
- DECIMAL / NUMERIC
- FLOAT / DOUBLE
- CHAR / VARCHAR
- TEXT / MEDIUMTEXT / LONGTEXT
- BINARY / VARBINARY
- BLOB / MEDIUMBLOB / LONGBLOB
- DATE / TIME
- DATETIME(6) / TIMESTAMP(6)
- YEAR
- BOOLEAN / BOOL
- JSON
- ENUM
- SET

The fixture was intentionally expanded during the audit rather than treated as
one immutable snapshot.

---

## 4. Test Configuration

Primary Local → Local configuration:

```yaml
migration:
  mode: full
  reconcile_target_schema: true
```

The final partition-reconciliation run used:

```yaml
migration:
  reconcile_target_schema: true
```

This allows supported incompatible existing target table structures to be
reconciled with the source during FULL migration.

`reconcile_target_schema` is configuration-controlled. It is not enabled
implicitly for every deployment.

FULL migration also does not delete unrelated, manually-created target-only
objects. Such objects are outside the migration set.

---

## 5. Audit Method

1. **Fixture setup** — Prepare the local MySQL source fixture.
2. **Source verification** — Query source metadata using MySQL CLI and
   `INFORMATION_SCHEMA`.
3. **Migration execution** — Run the Local → Local FULL migration.
4. **Report verification** — Inspect run status, row counts, object-result
   counts, failures, and errors.
5. **Target verification** — Use `SHOW CREATE` and `INFORMATION_SCHEMA` queries
   to compare target definitions with source.
6. **Runtime verification** — Execute representative positive and negative
   tests for constraints, generated columns, views, routines, triggers, and
   datatypes.
7. **Dependency verification** — Verify foreign keys, view dependencies, and
   trigger dependencies.
8. **Reconciliation verification** — Confirm incompatible supported target
   structures are reconciled when enabled.
9. **Cross-schema verification** — Test MySQL cross-database FK behavior in the
   local environment.
10. **Regression verification** — Run focused and broader unit tests after fixes.
11. **Evidence collection** — Record actual run IDs, SQL results, failures,
    root causes, fixes, limitations, and environment blockers separately.

---

## 6. Object Coverage

| Category | Status | Evidence |
|---|---|---|
| Tables | **Verified** | 11 tables migrated in final FULL run |
| Columns | **Verified** | 78 columns reported migrated in final run; source/target metadata checked |
| Primary keys | **Verified** | 11 PKs reported |
| AUTO_INCREMENT | **Verified** | Definition and runtime behavior tested |
| Generated columns | **Verified** | `products.price_with_tax` preserved and recalculated by target |
| UNIQUE constraints | **Verified** | Duplicate insert rejected with Error 1062 |
| Secondary/composite indexes | **Verified** | Target metadata verified |
| CHECK constraints | **Verified** | Invalid values rejected with Error 3819 |
| Foreign keys | **Verified** | 2 migrated FKs; invalid references rejected with Error 1452 |
| Cross-schema FK | **Verified** | Dedicated local cross-database scenario tested |
| Defaults | **Verified** | Source defaults preserved and runtime-checked |
| Views | **Verified** | Target view uses target-local migrated dependencies |
| Functions | **Verified** | 2 functions migrated in successful final run |
| Procedures | **Verified** | 2 procedures migrated and runtime-tested |
| Triggers | **Verified** | 2 triggers migrated and runtime-tested |
| Events | **Verified** | One-time event fixture migrated and metadata verified |
| Partitions | **Verified** | 3 RANGE/YEAR partitions preserved and reconciled |
| Comments | **Verified** | Table and column comments matched source |
| Representative datatypes | **Verified** | 31-column fixture passed |
| Table grants | **Conditionally supported** | Connector/reporting paths tested; live cross-account visibility limited |
| Routine EXECUTE grants | **Conditionally supported** | Depends on routine privilege catalog visibility |
| Users / roles | **Not supported** | No user/role migration path |
| Global privileges | **Not supported** | No global privilege migration path |
| Database-level privileges | **Not supported** | No database-level privilege migration path |

---

## 7. E2E Results

### Unit and regression testing

The MySQL implementation was regression-tested throughout the audit.

Focused tests were added for:

- Cross-engine type safety
- Error isolation
- WAL/source restart safety
- Views
- Trigger lifecycle
- Partitions
- Partition reporting
- Datatype handling
- Comments
- Grants
- Target schema reconciliation
- Object-result accounting

The final target-schema-reconciliation implementation passed its focused and
broader unit regression suites before the final live migration.

### Latest successful migration run

```text
Run ID: 74c3b8076a6244bf94104b997b2cf89d
Mode: FULL
Tables migrated: 11
Total rows: 44
Migrated: 44
Failed: 0
Success rate: 100%
Errors: 0
Overall status: SUCCESS
```

### Final object-result counts

```text
Tables           11
Columns          78
Primary Keys     11
AUTO_INCREMENT    9
Indexes           6
Unique            3
Foreign Keys      2
Checks            4
Generated         2
Defaults         15
Partitions        3
Comments          8
Grants            0
Views             1
Functions         2
Procedures        2
Triggers          2
Events            1
```

`Grants = 0` is interpreted together with the grant-metadata visibility
testing. It is **not** treated as proof that no grants exist on the source.

---

## 8. Validation Evidence

### Database and table inventory

The final source migration scope contained:

```text
11 base tables
1 view
82 columns
11 primary keys
3 unique definitions
2 foreign keys
4 checks
8 indexes
4 routines
2 triggers
1 event
3 partitions
44 total rows
```

The target had previously contained one manually-created target-only table,
`child_cross_schema_test`, created for a separate cross-schema experiment.
It was not migration output and was removed before the final source-to-target
comparison:

```sql
DROP TABLE IF EXISTS mysql_migration_target.child_cross_schema_test;
```

The final audit therefore compares the migration scope rather than counting
that unrelated manual test object as a migration result.

### Table and data migration

The final FULL run reported:

```text
Total source rows: 44
Migrated rows:      44
Failed rows:         0
Errors:              0
```

Runtime checks additionally verified:

- Newly inserted source customer data migrated to target.
- Target AUTO_INCREMENT continued to generate identifiers.
- Generated columns were recalculated by MySQL.
- Invalid CHECK values were rejected.
- Invalid FK references were rejected.
- Duplicate UNIQUE values were rejected.

### AUTO_INCREMENT

`customers.id` was verified as:

```text
AUTO_INCREMENT
```

A source-side insert generated a new identifier, and target-side AUTO_INCREMENT
behavior was verified after migration.

### Generated columns

The source and target `products` definitions contain:

```sql
price_with_tax decimal(12,2)
GENERATED ALWAYS AS ((price * 1.18)) STORED
```

Generated columns are excluded from ordinary data-load DML.

Runtime verification confirmed that MySQL calculates the target value from the
generated expression.

### UNIQUE constraints

The source and target rejected duplicate customer email values with:

```text
ERROR 1062
```

This verified both metadata preservation and runtime enforcement.

### CHECK constraints

The source and target rejected invalid values with:

```text
ERROR 3819
```

The audited checks included customer status and product price/stock checks.

### Foreign keys

The final `orders` table retained:

```text
fk_orders_customer
fk_orders_product
```

Invalid customer/product references were rejected on the target with:

```text
ERROR 1452
```

### Cross-schema foreign key

A dedicated cross-database scenario was created using the existing privileged
local databases.

The target-side test child table referenced:

```text
mysql_migration_source.parent_cross_schema_test(id)
```

A valid reference insert succeeded.

An invalid reference insert failed with:

```text
ERROR 1452
```

This verifies that MySQL itself enforces cross-database foreign keys in the
tested local environment.

The migration implementation separately maps dependencies belonging to the
migration set to the configured target database, which is the required
behavior when both sides are being migrated.

### Defaults

Source and target metadata/runtime checks verified defaults, including customer
status and string defaults.

A historical defect in which string defaults were emitted without the required
MySQL quoting was fixed and the corrected definitions were verified.

### Views

The `customer_order_summary` view originally retained source-database-qualified
references in the target:

```text
mysql_migration_source.customers
mysql_migration_source.orders
```

This caused the target view to read source tables and was identified as a
migration-code defect.

The permanent fix rewrites source-database-qualified identifiers that refer to
migrated objects to the configured target database.

Manual verification confirmed:

- Target view exists.
- Target view executes.
- A source-only customer appears through the target view after migration.
- A target-only customer appears through the target view.
- The target view no longer depends on the source database for its migrated
  `customers` and `orders` dependencies.

**Status: VERIFIED.**

### Functions and procedures

Routine DDL extraction initially selected the wrong `SHOW CREATE` field.

The authoritative routine DDL extraction was corrected.

Final successful migration:

```text
Functions:   2
Procedures:  2
```

Runtime verification confirmed function results and procedure behavior on the
target.

The function `fn_get_active_customer_count()` was verified on source and target.
Procedure execution was also verified through the procedure test log.

### Triggers and dependencies

The trigger dependency fixture was:

```text
customers
  └── trg_after_customer_insert_log
          └── customer_insert_log
```

The trigger writes:

```text
NEW.id
NEW.name
NEW.status
```

to `customer_insert_log`.

Source runtime insertion generated the expected dependency-log row.

After migration, the target trigger existed and a target runtime insertion
generated the expected target dependency-log row.

The existing `trg_customer_status_audit` trigger was also regression-tested.

**Status: PASS.**

### Events

A safe one-time migration fixture was added:

```text
evt_event_migration_test
```

The event was discovered and migrated during FULL migration.

Target event metadata and definition were verified.

The separate fixture `evt_migration_e2e_final` was also used during the audit.

Event execution is environment-dependent on Event Scheduler state, definer, and
privileges; the audit therefore verifies the migrated event definition and the
tested one-time fixture rather than claiming exhaustive scheduler coverage.

#### Task 9 Event state and schedule safety

The earlier Event implementation read only an Event name and `SHOW CREATE EVENT`
at the late Event phase. This could silently observe a preserved one-time Event
after it fired, at which point MySQL reports it as `DISABLED`.

The implementation now snapshots `EVENT_TYPE`, `STATUS`, `EXECUTE_AT`, interval,
`STARTS`, `ENDS`, `ON_COMPLETION`, `TIME_ZONE`, definer metadata, and authoritative
`SHOW CREATE EVENT` immediately after source connection. It creates the target
Event from that snapshot, rewrites only the definer, and uses the recorded Event
time zone while replaying literal schedules.

An enabled one-time Event that is due or falls within the configured 300-second
default safety window at either source snapshot or target creation is reported
as `EVENT: BLOCKED`. DMS does not drop or replace the target Event in that case;
it also does not force an expired Event back to `ENABLED`.

Existing Azure metadata evidence, migration run
`474f0d5157784c4d9bdf8d25f35d9523`, verified `evt_recurring_event_test` as
`RECURRING`, `ENABLED`, `EVERY 1 MINUTE`, `STARTS 2026-09-21 13:06:28`, no end,
and `ON COMPLETION PRESERVE`; `SHOW CREATE EVENT` retained that schedule and
rewrote the definer to `mysql_admin@%`. The Azure target `event_scheduler` was
`OFF`, so target runtime execution is **BLOCKED / NOT EXECUTED**, not passed.

The Task 9 dedicated Local → Azure re-test requires the configured
`mysql_source_pass` and `mysql_target_pass` environment secrets. They were not
available in the current execution environment, so no new source test Events,
migration run, target inspection, or cleanup evidence is claimed here.

#### Task 9 final Azure Event reconciliation observation

After Task 9 test-object cleanup, the source contained only
`evt_migration_e2e_final`. The final Local → Azure FULL migration,
`740ba5585c394826acd9257260c3332e`, reported Events `MIGRATED=1`,
`BLOCKED=0`, and `FAILED=0`.

The target retained prior test Events `evt_live_event_test`,
`evt_live_event_test_2`, `evt_recurring_event_test`,
`evt_task9_one_time_future`, and `evt_task9_recurring_enabled`, in addition to
`evt_migration_e2e_final`. This is intentional: FULL Event processing is scoped
to the source snapshot and has no managed-Event ownership registry or global
target Event deletion policy. The DMS must not infer ownership from test-like
names, alter target scheduler state, or delete these target-only Events.

### Partitions

The source `tbl_partition_test` uses:

```sql
PARTITION BY RANGE (YEAR(created_at))
```

with:

```text
p2025 → VALUES LESS THAN (2026)
p2026 → VALUES LESS THAN (2027)
pmax  → VALUES LESS THAN MAXVALUE
```

An earlier live regression found that partition metadata was discovered and
reported while an already-existing unpartitioned target table was reused.

The root cause was that the normal create-if-missing path did not replace an
existing incompatible target structure.

The implementation was changed to support target schema reconciliation:

```yaml
migration:
  reconcile_target_schema: true
```

The replacement is staged and verified before the old target table is removed
after successful migration processing.

Final target verification showed:

```text
p2025 | RANGE | year(created_at) | 2026
p2026 | RANGE | year(created_at) | 2027
pmax  | RANGE | year(created_at) | MAXVALUE
```

Verified:

- Partition method preserved
- `YEAR(created_at)` expression preserved
- Partition names preserved
- Partition order preserved
- `2026` boundary preserved
- `2027` boundary preserved
- `MAXVALUE` preserved
- 3 source rows present on target

**Data migration: PASS.**  
**Partition structure: PASS.**  
**Overall partition test: PASS.**

The normal Object Migration Results reporting also records the three processed
partitions, and the Migration Timeline reports the actual `Create Partitions`
count.

### Datatypes

The 31-column `tbl_datatype_test` fixture initially failed with:

```text
Python type set cannot be converted
```

The source connector returned MySQL SET values as Python `set`/`frozenset`,
which could not be bound directly to the target.

The connector was corrected to serialize SET members into MySQL's
comma-separated representation in declared member order.

Final datatype validation reported:

```text
tbl_datatype_test: 1/1 row
Overall rows: 44/44
Failed: 0
Errors: 0
```

Manual verification covered:

- JSON
- ENUM
- SET
- BINARY / VARBINARY
- BLOB / MEDIUMBLOB / LONGBLOB
- TEXT / MEDIUMTEXT / LONGTEXT
- DECIMAL / NUMERIC precision and scale
- FLOAT / DOUBLE
- DATE
- TIME
- DATETIME(6)
- TIMESTAMP(6)
- YEAR
- BOOLEAN / BOOL

MySQL `BOOLEAN` / `BOOL` is represented as `TINYINT(1)`.

**Status: PASS for the documented representative fixture.**

### Comments and metadata

The first comment migration attempt reported comments as migrated while target
metadata did not retain all source comments.

Source comment extraction and target comment application were corrected.

The audit then verified the following target values:

```text
MANUAL COMMENT TEST - Customers
MANUAL COLUMN COMMENT - Customer Name
MANUAL COLUMN COMMENT - Customer Email
```

Table and column comments matched the tested source metadata.

**Status: PASS.**

### Grants

Supported table-grant discovery/application and routine-grant handling were
covered by focused automated tests.

The live cross-account scenario exposed a metadata-visibility boundary:

```text
CURRENT_USER() = mysql_test@%
```

The DMS account could not see the `TABLE_PRIVILEGES` rows that `root` could see
for the separate:

```text
migration_grant_test@localhost
```

account.

Root could see the explicit `SELECT` and `INSERT` grants, while the migration
account returned no corresponding visible rows.

No broad `mysql.*` metadata access was used as a workaround.

Therefore:

- Supported grant connector/reporting behavior was tested.
- Cross-account live grant discovery remained blocked by metadata visibility.
- `Grants = 0` in the migration report is not interpreted as proof that no
  grants exist.

---

## 9. Cross-Schema Testing History

### Scenario 1 — Cross-database foreign key

**Scenario Tested**

A child table in the target database referenced a parent table in the source
database:

```text
mysql_migration_target.child_cross_schema_test
        ↓
mysql_migration_source.parent_cross_schema_test
```

**Failure Observed**

A separate attempt to create dedicated cross-schema test databases failed due
to available permissions.

**Root Cause**

The migration account did not have permission to create the additional test
databases.

**Fix / Test Adjustment**

The scenario was recreated inside the existing privileged migration databases
instead of treating the permission limitation as a MySQL FK limitation.

**Verification**

- Valid cross-schema FK insert succeeded.
- Invalid reference failed with Error 1452.

**Final Result**

**PASS — MySQL cross-database FK enforcement verified in the local environment.**

### Scenario 2 — Cross-schema migration dependency mapping

**Scenario Tested**

Migrated dependencies must reference the target database when both the source
object and its dependency belong to the migration set.

**Failure Observed**

Target FK definitions could retain the source database namespace.

**Root Cause**

Source namespace was not consistently mapped to the target namespace.

**Fix**

Target dependency generation was corrected to map migrated source objects to the
configured target database.

**Final Result**

**PASS — Target-local dependency mapping verified for migrated objects.**

### Scenario 3 — Cross-account grant visibility

**Scenario Tested**

A separate account had explicit table privileges on source and target.

**Failure Observed**

The migration account could not see the explicit grant rows through
`INFORMATION_SCHEMA.TABLE_PRIVILEGES`.

**Root Cause**

MySQL metadata visibility is privilege/account dependent.

**Fix / Resolution**

No broad system-schema privilege was added merely to manufacture a PASS.
The scenario was recorded as an environment/visibility limitation.

**Final Result**

**BLOCKED for live cross-account discovery; supported connector behavior remains
covered by focused tests.**

---

## 10. Testing History

The following implementation issues were identified during the audit, fixed, and
regression-tested.

### Duplicate object Error 1061

**Failure:** Equivalent target objects could cause duplicate object errors.

**Root Cause:** Existing target metadata was not consistently treated as a
verified existing state.

**Fix:** Existing equivalent metadata is recorded as `verified_existing`
instead of blindly attempting duplicate creation.

**Result:** Fixed and regression-tested.

### Cross-category result contamination

**Failure:** One physical object failure could incorrectly affect unrelated
unique/index/FK result categories.

**Root Cause:** Object-result handling was not sufficiently independent.

**Fix:** Unique, index, and FK outcomes are recorded independently.

**Result:** Fixed and regression-tested.

### False migrated/skipped status

**Failure:** Skipped SQL could appear as successfully migrated.

**Root Cause:** Result accounting did not distinguish successful operations from
skipped/failed operations.

**Fix:** Only successful operations are counted as migrated.

**Result:** Fixed and regression-tested.

### Default value DDL

**Failure:** String defaults could produce invalid target DDL.

**Root Cause:** Source metadata was emitted without required MySQL quoting.

**Fix:** Default value handling was corrected.

**Result:** Fixed and source/target verified.

### Generated-column data loading

**Failure:** Generated-column values were included in normal data-load DML.

**Root Cause:** Loader did not exclude generated columns.

**Fix:** Generated columns are excluded from normal data-load DML.

**Result:** Fixed and target recalculation verified.

### CHECK constraint DDL

**Failure:** CHECK migration could emit incorrectly escaped catalog literals.

**Root Cause:** Catalog expression normalization was incomplete.

**Fix:** CHECK expressions are normalized for target DDL.

**Result:** Fixed and Error 3819 runtime behavior verified.

### Foreign-key namespace mapping

**Failure:** Target FK definitions could retain the source database name.

**Root Cause:** Source namespace was not mapped to target namespace.

**Fix:** Migrated dependencies are mapped to the configured target database.

**Result:** Fixed and Error 1452 runtime behavior verified.

### Routine DDL extraction

**Failure:** Function/procedure DDL could be malformed.

**Root Cause:** Wrong `SHOW CREATE` metadata field was selected.

**Fix:** Authoritative routine DDL field extraction was implemented.

**Result:** Fixed and routine migration/runtime verified.

### Trigger connection lifecycle

**Failure:** `CREATE TRIGGER` could wait indefinitely.

**Root Cause:** Source read connections used non-autocommit behavior and were not
consistently closed. A source read transaction could retain metadata state while
target trigger DDL was attempted.

**Fix:** Source read connections use autocommit; connector cleanup rolls back and
closes connections; failed trigger DDL has rollback handling.

**Result:** Fixed and live trigger migration/runtime verification completed.

### MySQL SET serialization

**Failure:**

```text
Python type set cannot be converted
```

**Root Cause:** Python `set` / `frozenset` values were passed directly to the
target.

**Fix:** SET values are serialized to MySQL's comma-separated representation in
declared member order.

**Result:** Fixed and datatype fixture passed.

### Comment migration

**Failure:** Target table/column comments did not initially match source.

**Root Cause:** Source comment extraction and target application were incomplete.

**Fix:** Table and column comment handling was corrected.

**Result:** Fixed and manually verified.

### Partition reconciliation

**Failure:** Existing unpartitioned target table remained unpartitioned even
though source partition metadata was discovered.

**Root Cause:** Existing target table was reused instead of structurally
reconciled.

**Fix:** `migration.reconcile_target_schema` supports replacement/reconciliation
of supported incompatible target structures.

**Result:** Fixed and live verified with the three-partition fixture.

### Partition reporting

**Failure:** `Create Partitions` timeline could report an incorrect zero count.

**Root Cause:** Reporting did not use the actual processed partition items.

**Fix:** Reporting now records the dynamic processed partition count.

**Result:** Fixed and verified; three partitions are reported for the tested
fixture.

### Function/trigger Error 1419

**Failure:** Function and trigger creation was rejected with MySQL Error 1419.

**Root Cause:** Binary logging was enabled while
`log_bin_trust_function_creators=OFF`, and the migration account lacked the
required administrative privilege.

**Fix:** The platform explicitly reports the function/trigger operation as
blocked and gives administrator remediation. It does not automatically change
server configuration.

A MySQL administrator applied:

```sql
SET PERSIST log_bin_trust_function_creators = ON;
```

The setting persisted after restart.

**Result:** Final functions and triggers migrated successfully.

### MySQL source connection cleanup

**Failure:** Long-lived source read connections could interfere with later
metadata/DDL operations.

**Root Cause:** Connection lifecycle was not consistently closed across full
migration, CDC, and assessment paths.

**Fix:** Source reads use autocommit and connector cleanup closes connections
with rollback handling where appropriate.

**Result:** Trigger and broader migration lifecycle tests completed successfully.

---

## 11. Known Limitations

These are confirmed current implementation boundaries, engine differences, or
audit-scope limitations. Resolved defects above are intentionally not classified
as current limitations.

| Limitation | Category |
|---|---|
| Users, roles, role assignments, authentication/password definitions are not migrated | Implementation |
| Global privileges are not migrated | Implementation |
| Database-level privileges are not migrated | Implementation |
| Table grants require visible grant metadata and an existing target grantee | Implementation / Environment |
| Routine `EXECUTE` grant discovery depends on `ROUTINE_PRIVILEGES` visibility | Environment / Audit scope |
| FULL migration does not delete unrelated target-only objects | Implementation |
| Target structural reconciliation is controlled by `reconcile_target_schema` | Implementation |
| Event behavior depends on Event Scheduler, definer, and privileges | Environment |
| Event enabled-state behavior was not exhaustively tested across all scheduler/definer combinations | Audit scope |
| Datatype coverage beyond the representative 31-column fixture is not exhaustive | Audit scope |
| Circular cross-schema FK dependencies were not exhaustively tested | Audit scope |
| Views intentionally referencing unmanaged external databases remain dependent on those databases | Implementation boundary |
| PostgreSQL materialized views have no direct MySQL materialized-view equivalent | Engine capability |
| PostgreSQL RLS/policies, extensions, domains/custom types, and standalone sequences have no direct native MySQL equivalent | Engine capability |
| MySQL databases and PostgreSQL schemas are not structurally identical namespace concepts | Engine capability |
| MySQL DDL transaction behavior is not equivalent to PostgreSQL transactional DDL | Engine capability |
| Azure / remote MySQL E2E was not performed in this audit | Audit scope |

---

## 12. Environment Blockers and Prerequisites

These are not migration-code failures.

| Blocker / prerequisite | Status | Resolution |
|---|---|---|
| `log_bin_trust_function_creators=OFF` with binary logging enabled | **Resolved for final audit** | Administrator applied `SET PERSIST log_bin_trust_function_creators = ON` and verified after restart |
| Cross-account grant metadata visibility | **Blocked for live scenario** | Requires appropriate metadata visibility or administrator-assisted grant handling |
| Event Scheduler / definer privileges | **Environment-dependent** | Configure target scheduler and required target account/privileges |
| Azure MySQL connectivity, TLS, firewall, and permissions | **See dedicated Local → Azure audit** | Targeted Azure evidence and limitations are in `MYSQL_LOCAL_TO_AZURE_AUDIT.md` |
| Dedicated cross-schema test database creation | **Permission blocked during one test attempt** | Scenario was retested inside existing privileged migration databases |

The DMS does not grant `SUPER`, change server-global variables, disable binary
logging, or use broad system-schema access merely to make the audit appear
successful.

---

## 13. Final Assessment

The `feature/unified-dms-platform` MySQL implementation provides a verified
Local → Local FULL migration path for the audited MySQL object scope.

Final migration evidence:

```text
Run ID: 74c3b8076a6244bf94104b997b2cf89d
Mode: FULL
Tables migrated: 11
Total source rows: 44
Migrated: 44
Failed: 0
Errors: 0
Success rate: 100%
Overall status: SUCCESS
```

The Local → Local audit verified:

- **11 tables and 44 rows** migrated successfully.
- Columns and primary keys were verified.
- Defaults, AUTO_INCREMENT, generated columns, indexes, UNIQUE constraints,
  CHECK constraints, and foreign keys were verified.
- Cross-database FK behavior was tested in a dedicated local scenario.
- The migrated view was verified to use target-local migrated dependencies.
- Functions and procedures were migrated and runtime-verified.
- Triggers and trigger dependencies were migrated and runtime-verified.
- The event fixture was migrated and metadata-verified.
- The three-partition RANGE/YEAR fixture was structurally reconciled and verified.
- The 31-column representative datatype fixture passed after the SET
  serialization fix.
- Table and column comments were migrated and verified.
- Supported grant paths were tested, with live cross-account discovery limited
  by metadata visibility.
- Object-level failure accounting was corrected so failed/skipped operations are
  not falsely counted as migrated.
- Target schema reconciliation was implemented and live-verified for the
  partition regression.
- The final run completed with **0 failed rows and 0 errors**.

The audit also identified implementation defects during testing. Those defects
were fixed and regression-tested and are therefore documented as **resolved
findings**, not current MySQL limitations.

The remaining limitations are implementation boundaries, engine capability
differences, environment prerequisites, or audit-scope boundaries.

No Azure/remote MySQL PASS is claimed by this Local → Local report.

---

## 14. Reproduction Instructions

### 1. Ensure MySQL is running

Verify the local server is available on:

```text
127.0.0.1:3306
```

### 2. Connect to MySQL

PowerShell:

```powershell
& "C:\Program Files\MySQL\MySQL Server 26.7\bin\mysql.exe" `
  -h 127.0.0.1 -P 3306 -u mysql_test -p
```

### 3. Verify databases

```sql
SHOW DATABASES;
```

Confirm:

```text
mysql_migration_source
mysql_migration_target
```

### 4. Verify function/trigger prerequisite

```sql
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'log_bin_trust_function_creators';
```

If required by the server policy, a MySQL administrator can apply:

```sql
SET PERSIST log_bin_trust_function_creators = ON;
```

Verify after restart:

```sql
SHOW VARIABLES LIKE 'log_bin_trust_function_creators';
```

The migration platform does not perform this administrator operation.

### 5. Verify final migration configuration

Ensure:

```yaml
migration:
  mode: full
  reconcile_target_schema: true
```

### 6. Run Local → Local FULL migration

From the project root:

```powershell
python -m migration_platform `
  --config config\mysql_local_test.yaml `
  --mode full `
  --no-live-ui
```

### 7. Verify target tables

```sql
SHOW TABLES FROM mysql_migration_target;
```

### 8. Verify important table definitions

```sql
SHOW CREATE TABLE mysql_migration_target.customers;
SHOW CREATE TABLE mysql_migration_target.products;
SHOW CREATE TABLE mysql_migration_target.orders;
SHOW CREATE TABLE mysql_migration_target.tbl_partition_test;
```

### 9. Verify partitions

```sql
SELECT
    TABLE_NAME,
    PARTITION_NAME,
    PARTITION_METHOD,
    PARTITION_EXPRESSION,
    PARTITION_DESCRIPTION,
    PARTITION_ORDINAL_POSITION
FROM information_schema.PARTITIONS
WHERE TABLE_SCHEMA = 'mysql_migration_target'
  AND TABLE_NAME = 'tbl_partition_test'
ORDER BY PARTITION_ORDINAL_POSITION;
```

Expected:

```text
p2025
p2026
pmax
```

### 10. Verify row counts

Compare source and target for each migrated table.

Example:

```sql
SELECT COUNT(*) FROM mysql_migration_source.customers;
SELECT COUNT(*) FROM mysql_migration_target.customers;
```

The final migration report must show:

```text
Migrated rows = source rows
Failed rows = 0
Errors = 0
Success rate = 100%
```

### 11. Verify views

```sql
SHOW CREATE VIEW mysql_migration_target.customer_order_summary;
```

Confirm migrated dependencies use the target database rather than
`mysql_migration_source`.

### 12. Verify routines

```sql
SHOW CREATE FUNCTION mysql_migration_target.<function_name>;
SHOW CREATE PROCEDURE mysql_migration_target.<procedure_name>;
```

Execute the appropriate runtime tests.

### 13. Verify triggers

```sql
SHOW TRIGGERS FROM mysql_migration_target;
```

Run the trigger fixture insert and verify the expected row in
`customer_insert_log`.

### 14. Verify events

```sql
SHOW EVENTS FROM mysql_migration_target;
```

Verify the migrated event definition and status.

### 15. Verify datatypes and comments

```sql
SHOW CREATE TABLE mysql_migration_target.tbl_datatype_test;
SHOW CREATE TABLE mysql_migration_target.customers;
```

Verify the representative datatype definitions and tested table/column
comments.

### 16. Run unit tests

```powershell
python -m pytest tests\unit -q
```

The exact final count can change as subsequent project work adds tests; the
audit therefore records the verified regression suites rather than claiming a
fixed future test count.

### 17. Review final migration result

Confirm:

```text
Migrated rows = source rows
Failed rows = 0
Errors = 0
Success rate = 100%
Overall status = SUCCESS
```

Also verify that object-level report counts correspond to target metadata.

---

## 15. Related Documentation

- `docs/mysql/MYSQL_TEST_GUIDE.md`
- `docs/mysql/MYSQL_OBJECT_SUPPORT_MATRIX.md`
- `docs/mysql/MYSQL_LIMITATIONS.md`

These documents should remain consistent with this final MySQL Local → Local
audit report.
