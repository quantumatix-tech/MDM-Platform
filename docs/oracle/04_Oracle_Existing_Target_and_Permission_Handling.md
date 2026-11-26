# Oracle Existing Target Handling — Final Validation Report

**Project:** Unified DMS / MDM Migration Platform
**Target engine:** Oracle
**Migration mode:** FULL
**Validation status:** PASS for tested local FULL and repeat FULL; additional security and dependency scenarios are subject to the limits below.

## 1. Objective

Define what FULL does when the target already contains data, schema objects or permissions. Oracle reconciles the **current source-managed inventory**, not the entire target PDB or owner. Independent target objects are preserved; unsafe conflicts block migration.

The normal profile is `config/oracle_local_to_local.yaml`, with `SOURCE_OWNER` in `source_db_new` and `TARGET_OWNER` in `target_db_new`. The dedicated permission executor is `TARGET_EXECUTOR`.

Evidence labels used in this document:

- **PASS — current:** observed in the healthcare local-to-local acceptance runs.
- **PASS — historical:** observed in the earlier Oracle customer/order fixture recorded in the supplied existing-target report, not rerun against the healthcare profile.
- **Implemented:** confirmed in the supplied code; separate live scenario verification is not established here.

## 2. Task 1 — Existing Target Handling

| Scenario | FULL behaviour | Evidence |
|---|---|---|
| Existing target PDB and owner | Reuse the configured endpoint and owner; do not drop or recreate the PDB. | PASS — current |
| Existing managed table and data | Reconcile and reload from source. | PASS — current |
| Independent target-only table and rows | Preserve. | PASS — historical: `TARGET_ONLY_TEST` |
| Independent target-only view | Preserve if it does not prevent safe replacement. | PASS — historical: `TARGET_ONLY_VIEW` |
| Independent target-only index | Preserve when unrelated to a destructive change. | PASS — historical: `IDX_TARGET_ONLY_NOTE` |
| Source table removed or renamed between runs | An object absent from the current source inventory is not automatically identified as formerly managed and deleted. | Implemented |
| Empty discovered source table inventory | Block unsafe replacement planning. | Implemented |
| Source object name conflicts with an existing target table of another object type | Block rather than destroy target table data. | Implemented |

**Rule:** Scope is determined by the configured source owner, discovered objects and supported metadata. FULL is not a database reset or automatic purge of everything absent from source.

## 3. Task 2 — Existing Schema Handling

Relevant configuration (already present in the normal YAML):

```yaml
migration:
  mode: full
  stop_on_error: true
  batch_size: 1000
  reconcile_target_schema: true
  oracle_schema_replacement:
    enabled: true
```

| Scenario | FULL behaviour | Evidence |
|---|---|---|
| Target missing a supported source column | Add/reconcile the column where safe. | PASS — historical: `CUSTOMERS.PHONE` |
| Extra column on a managed target table | Remove when dependency and grant checks permit. | PASS — historical: `CUSTOMERS.EXTRA_TARGET` |
| Compatible column length difference | Reconcile to the source definition when safe. | PASS — historical: `CITY` length 100 → 50 |
| Existing PK, FK, UNIQUE and CHECK constraints | Match supported definitions and enforce validated state; framework constraint names may differ. | PASS — current |
| Extra unmatched target constraint or unrelated index | Do not assume it is migration-owned; retain it or block an unsafe change. | Implemented |
| Mismatched existing partition layout | Block; do not silently restructure an incompatible partitioned table. | Implemented |
| Existing user-defined type or materialized view with incompatible definition | Block rather than force unsupported replacement. | Implemented |

Historical schema reconciliation run: `c224f8328b1f48fcb4ffafb3b573b865` (3 tables, 9 rows, 0 errors). Unsafe narrowing, type-family changes, generated/identity changes or column drops with dependencies require review. An extra column may contain target-only data: back up relevant target data before approving reconciliation.

## 4. Task 3 — Existing Table and Data Handling

| Change made before FULL | Expected target state after successful FULL | Evidence |
|---|---|---|
| Insert/update/delete a source row | Target contains the current source rows and values. | Implemented; exact row comparison PASS — current |
| Change a migrated target row | Source value restored. | PASS — current: overwrite marker on `DEPARTMENTS.LOCATION` removed |
| Insert an extra row into a managed target table | Extra row removed. | PASS — historical: customer ID 999 |
| Delete a target row that exists in source | Source row restored. | PASS — historical: customer ID 3 |
| Drop a supported managed target table | Table and data recreated if preflight permits. | PASS — historical: `ORDERDETAILS` |
| Repeat FULL with unchanged source | No accumulation of duplicate rows. | PASS — current: 50 exact matching rows in acceptance check |
| Insert a row into an independent target-only table | Row preserved. | PASS — historical |

FULL is source-state replacement, not incremental merge or change-data capture. A subsequent successful FULL can overwrite manually edited data in managed tables. It does not overwrite every target table.

## 5. Task 4 — Existing Object Handling

| Object/scenario | Behaviour | Evidence |
|---|---|---|
| Managed ordinary/descending index | Create or reconcile supported column-key definitions. | PASS — current: four secondary indexes, including DESC |
| Same-name index on another table or incompatible index type | Block unsafe replacement. | Implemented |
| Managed views | Apply source definitions; reapply after later table DDL; verify final status. | PASS — current: corrected repeat FULL and final independent scan |
| Managed sequences and identity columns | Use supported sequence handling and synchronize identity generator after load. | PASS — current identity test generated ID 11 |
| Managed routines, packages, triggers, synonyms and types | Create/check supported definitions in dependency order. Incompatible forms can block. | PASS — current inventory and validity; function/package execution checked |
| Materialized view | Supported refresh/build configuration; matching existing definition checked. | PASS — current: result data compared |
| Independent target-only dependent object | Preserve; block if it prevents safe cleanup or replacement. | PASS — historical dependency safety case; implemented |
| Source-managed constraint-backed index | Managed through constraint definition, not copied as a standalone source index. | PASS — current |

**Oracle-specific difference:** Unlike a blanket prune policy, extra objects on an existing target are not automatically dropped merely because they do not appear in source. The planner checks current owner inventory, object types, dependencies and grants before replacement.

## 6. Task 5 — FULL Reconciliation and Permission Handling

### 6.1 Normal migration command

Set the three passwords in each new PowerShell session. Enter the existing password for the account named in each prompt.

```powershell
$env:SECRET_oracle_local_source_owner_pass = Read-Host "SOURCE password"
$env:SECRET_oracle_local_target_owner_pass = Read-Host "TARGET password"
$env:SECRET_oracle_local_permission_executor_pass = Read-Host "Executor password"

python -m migration_platform --config config/oracle_local_to_local.yaml --mode full
```

For automation or acceptance testing, `--exit-after-run` exits after migration instead of keeping the report server running. See Document 01 for account provisioning and secret handling. `Read-Host` as shown is the normal project workflow; use the masked-input alternative in Document 01 if needed.

### 6.2 Who executes permissions?

| Account | Responsibility |
|---|---|
| SOURCE owner | Read source-owned data/objects and discover supported application grants; read the authorized principal catalogs. |
| TARGET owner | Create/manage target schema objects; grant/revoke privileges on its own objects; maintain the permission provenance journal. |
| TARGET executor | Create missing local application users/roles; apply approved role memberships and system grants; verify security metadata. |
| PDB administrator / authorized SYSDBA | Provision runtime accounts, catalog access and required authority during setup; not a routine FULL login. |

Relevant configuration (normal local profile):

```yaml
oracle_permission_migration:
  enabled: true
  executor:
    username: TARGET_EXECUTOR
    password_secret: oracle_local_permission_executor_pass
  new_user_policy: LOCKED
  approve_role_creation_admin_option: true
  approve_create_session: true
```

Supported permission scope: local application users and non-authenticated roles; object `SELECT`, `INSERT`, `UPDATE`, `DELETE`; column `INSERT`, `UPDATE`, `REFERENCES`; supported local role memberships; approved `CREATE SESSION`. Newly created target users are locked and externally authenticated. Passwords and source authentication/account settings are not copied. Existing principals are reused without resetting their password, profile or account state. Their managed permissions can be reconciled separately.

### 6.3 When is APPLY needed?

| Change | Next action |
|---|---|
| Initial owner/executor setup | PLAN, then approved APPLY. |
| New supported source object grant or column grant | Direct FULL if authority and policy already permit it. |
| New source application user/role in discovered permission scope | Direct FULL if required provisioning authority and approvals already exist. |
| Source-managed grant revoked | Direct FULL; tracked stale permission is reconciled if revoke authority exists. |
| New missing executor system privilege, role ADMIN OPTION or catalog access | Review PLAN; approve APPLY only for verified missing authority. |
| New terminal session | Set runtime ENV secrets again; no APPLY solely for reopening PowerShell. |
| No setup or source authority changes | Direct FULL; repeat APPLY is not a routine requirement. |

Conditional setup commands, only when required:

```powershell
python -m migration_platform.oracle_setup_plan --config config/oracle_local_to_local.yaml
python -m migration_platform.oracle_setup_plan --config config/oracle_local_to_local.yaml --apply
```

Review actual operations and approve the current fingerprint, not a previously saved fingerprint. Fresh empty-source setup and later source-driven executor authority are different: the healthcare run required three additional grants after users and roles were introduced (`CREATE USER`, `CREATE ROLE`, `CREATE SESSION WITH ADMIN OPTION`); this APPLY succeeded live. The executor's ADMIN OPTION on newly created roles is checked before membership grants. No broad DBA/ANY privilege is required by the normal workflow.

### 6.4 Practical SOURCE and TARGET grant scenarios

The following are examples for an authorized developer using an isolated test environment. They are not additional executed healthcare acceptance tests.

**New SOURCE grant:** connect as `SOURCE_OWNER` to `source_db_new`.

```sql
GRANT SELECT ON PAYMENTS TO HEALTH_READ_ROLE;
```

Run normal FULL. If the grant is supported and preflight succeeds, the target owner applies the equivalent grant on `TARGET_OWNER.PAYMENTS`. No separate APPLY is required for this object grant.

**Remove the same SOURCE grant:**

```sql
REVOKE SELECT ON PAYMENTS FROM HEALTH_READ_ROLE;
```

If the framework issued and recorded the corresponding target grant, next FULL revokes it, subject to verified authority. A pre-existing independent grant is not automatically treated as framework-owned.

**Manually create an independent TARGET object:** connect as `TARGET_OWNER` to `target_db_new`.

```sql
CREATE TABLE TARGET_ONLY_CHECK (ID NUMBER PRIMARY KEY, NOTE VARCHAR2(100));
INSERT INTO TARGET_ONLY_CHECK VALUES (1, 'Preserve this row');
COMMIT;
```

After normal FULL, the independent object and row should remain:

```sql
SELECT ID, NOTE FROM TARGET_ONLY_CHECK;
```

The supplied historical Oracle report confirms preservation of a similar target-only table, view and index. This exact healthcare example has not been run here.

**Manually grant extra TARGET privileges:**

```sql
-- Example only; may intentionally block a subsequent FULL.
GRANT SELECT ON PAYMENTS TO HEALTH_READ_ROLE;
```

If this grant is absent from source and was not issued by the framework, the next FULL can block rather than silently revoke it. Do not use APPLY to bypass this protection.

| Target permission scenario | Reconciliation policy |
|---|---|
| Desired permission missing | Grant it if supported and authorized. |
| Desired permission already exists with equivalent scope | Retain it; if independently issued, do not automatically adopt it. |
| Source removes framework-issued permission | Revoke the stale tracked permission after authority checks. |
| Extra untracked permission on a managed object to a mapped principal | Fail closed; review ownership and intended policy. |
| Unrelated target-only grants/users/roles | Outside managed reconciliation scope; preserve, subject to dependency and role-exposure safeguards. |
| Existing role has unrelated access and would receive a new member | Block to avoid exposing target-only privileges. |
| Broader grant/admin option or incompatible role state | Block unsupported reconciliation. |

### 6.5 Permission provenance and legacy grants

`DMT_PERMISSION_PROVENANCE` is a framework-owned target table. It records permission keys, migration scope and grant/revoke state. Pending operations are journaled before Oracle DDL and finalized after verification. It is not source application data and must not be dropped during routine cleanup.

The journal binds the source/target identity pair. Reusing the same target owner for another source scope can fail; use a separate approved target owner.

An untracked grant that conflicts with current source policy requires a decision. If the operator explicitly decides it should be managed and removed, the existing one-time legacy adoption workflow is:

```powershell
python -m migration_platform --config config/oracle_local_to_local.yaml --mode full --oracle-permission-adopt-legacy
```

This is **not a read-only command**. Review the exact candidate permissions; type the displayed `APPROVE N LEGACY PERMISSIONS` only if authorized. `N` is the actual candidate count. The workflow rechecks state and authority, records approved adoption and continues FULL. Decline if a grant must remain independent. Never use the flag routinely or blindly approve a revoke.

For a schema-only read-only preview, the existing CLI option is `--oracle-overwrite-plan`; it does not replace permission preflight. The setup PLAN is for account/authority provisioning, not data overwrite or permission-delta planning.

## 7. Task 6 — Duplicate and Conflict Handling

| Scenario | Result / safety rule |
|---|---|
| Repeat FULL with same source rows | Existing managed data is replaced; current acceptance retained 10 rows per table. |
| Source PK/UNIQUE enforcement | Supported definitions and column nullability verified; constraint names can be deterministic `DMS_PK_...`, `DMS_UQ_...`, etc. |
| Redundant explicit NOT NULL on target PK columns | Current target had four extra generated NOT NULL checks; functional constraints and nullability comparison passed. Raw counts need not match. |
| External or retained dependencies prevent safe replacement | Fail closed; inspect dependencies rather than use unrestricted CASCADE. |
| Unsupported metadata or denied catalog access | Fail before unsafe cleanup; do not interpret unavailable inventory as empty. |
| Later DDL invalidates an earlier managed view | Corrected lifecycle reapplies views and checks final managed status; a managed invalid object must not report success. |
| Failure after Oracle DDL | Earlier DDL may already be committed; inspect actual target state before retry. FULL is not an atomic whole-schema rollback. |

## 8. Task 7 — Final Target State Validation (read-only)

Connect as `TARGET_OWNER` to `target_db_new`. The following SQL is for the healthcare acceptance dataset; compare equivalent SOURCE queries when testing a new dataset.

```sql
SELECT 'DEPARTMENTS' table_name, COUNT(*) total_rows FROM DEPARTMENTS
UNION ALL SELECT 'DOCTORS', COUNT(*) FROM DOCTORS
UNION ALL SELECT 'PATIENTS', COUNT(*) FROM PATIENTS
UNION ALL SELECT 'APPOINTMENTS', COUNT(*) FROM APPOINTMENTS
UNION ALL SELECT 'PAYMENTS', COUNT(*) FROM PAYMENTS;

SELECT object_name, object_type, status
FROM user_objects
WHERE generated = 'N' AND status <> 'VALID'
ORDER BY object_type, object_name;

SELECT name, type, line, position, text
FROM user_errors WHERE attribute = 'ERROR';

SELECT grantee, table_name, privilege
FROM user_tab_privs_made
WHERE grantee IN ('HEALTH_READER','HEALTH_WRITER',
                  'HEALTH_READ_ROLE','HEALTH_WRITE_ROLE');

SELECT grantee, table_name, column_name, privilege
FROM user_col_privs_made
WHERE grantee IN ('HEALTH_READER','HEALTH_WRITER',
                  'HEALTH_READ_ROLE','HEALTH_WRITE_ROLE');
```

Expected for the original dataset: 10 rows per application table, zero invalid managed objects, zero managed compilation errors, 10 object grants and one column grant. Compare exact source/target object name/type pairs; ignore legitimate internal index-name differences and preserve independent target-only objects. Use `TARGET_EXECUTOR` for authorized DBA catalog checks of users, roles, memberships and system grants (see Document 05 for acceptance procedures).

Inspect the JSON report phases `oracle_schema_replacement_plan`, `oracle_schema_replacement_verification` and `oracle_permission_verification`. Review the final `managed_object_status_validation` result. A console success alone is not a substitute for independent checks.

## 9. Final Target State and Evidence

| Evidence | Result |
|---|---|
| Corrected FULL: `bfd97adbdcd14347a5108d8ef6792e72` | 5 tables / 50 rows / 0 errors / 196.2 s |
| Corrected repeat FULL: `f79b3095da5e406e92e75416a3f1fc8e` | 5 tables / 50 rows / 0 errors / 33.9 s |
| Independent final SOURCE object comparison | No missing or mismatched objects |
| Independent final target validity and compiler check | Zero invalid objects; zero compilation errors |
| Earlier healthcare overwrite and security acceptance | Exact 50 rows matched, overwrite marker removed, generated patient ID 11, 10 object grants, 1 column grant, 2 system grants, 3 memberships, 2 locked users, 2 roles |
| Latest reported unit verification | 2,118 project tests passed; 826 Oracle tests passed; 192 focused repeat-FULL/replacement tests passed (overlapping selections) |

Historical target-only preservation and schema-drift cases are recorded separately in the supplied Oracle existing-target report. They are not new tests against the latest healthcare profile. No live test of the manual permission adoption/revocation examples in this document is claimed. Generic integration tests (14 collected) were not executed; guarded Oracle E2E (2) were skipped. Large-scale performance and universal feature parity are not established.

## 10. Overall Result

**PASS for the tested Oracle local FULL and repeat FULL workflow.** Normal migration reuses the existing PDB, reconciles supported source-managed data/objects and permissions, and preserves independent target state subject to safety checks. APPLY is conditional infrastructure authorization, not a prerequisite for each migration.

The seven-task structure follows the existing MySQL/MSSQL/PostgreSQL existing-target reports. Oracle-specific permission handling follows the supplied Oracle permission migration documentation and the current code snapshot, rather than assuming identical behaviour across engines.

### Source references

- Supplied `docs.zip`: `oracle/ORACLE_EXISTING_TARGET_HANDLING.md`, `ORACLE_PERMISSION_MIGRATION.md`, `ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md`, `ORACLE_SETUP_PLAN.md`, `ORACLE_LIMITATIONS.md`; peer existing-target reports and Db2 technical reference.
- Supplied `dmt_migration.zip`: `core/connectors/oracle/replacement.py` (`build_plan`, `apply_plan`), `permissions.py` (`preflight_target`, `_build_reconciliation`, `_apply_tracked_permissions`, `verify`), `target.py` (`reconcile_oracle_table`, `clear_objects_for_full_sync`), `core/orchestrator.py`, `migration_platform/oracle_setup_plan.py`, `config/oracle_local_to_local.yaml`.
- Latest owner-provided console runs and independent verification supersede the older pending acceptance statements in the archive. Latest detailed JSON reports were not independently opened for this document; no database operations were executed during documentation preparation.
