# Oracle Migration Flow and Connector Design

End-to-end execution flow and technical reference for Oracle-to-Oracle FULL migration in MDM-Platform. This document describes the normal connector workflow, not the fixture-based E2E runner.

**Verified environment:** Oracle AI Database 26ai Free, local → local, `SOURCE_OWNER` → `TARGET_OWNER`, using `config/oracle_local_to_local.yaml`.

---

## Part 1 — Conceptual Migration Flow

Run from the repository root after provisioning the endpoints and setting the runtime secrets (see Document 01):

```powershell
python -m migration_platform --config config/oracle_local_to_local.yaml --mode full
```

For command-line execution without keeping the report server running, add `--exit-after-run`.

```text
SOURCE OWNER / SERVICE                 TARGET OWNER / SERVICE
           │                                      │
           └──────── Connect both endpoints ──────┘
                              ↓
                Discover SOURCE tables and objects
                              ↓
             Validate types, constraints, dependencies,
                 overwrite policy and permissions
                              ↓
                Apply safe managed replacement plan
                              ↓
              Create types, sequences, tables, partitions
                              ↓
              Prepare synonyms, views, routines, triggers,
                 materialized views; check definitions
                              ↓
                 Suspend managed application triggers
                              ↓
                  Clear managed TARGET table rows
                      (child before parent)
                              ↓
                     Load SOURCE rows in batches
                      (parent before child)
                              ↓
             Apply constraints / indexes; synchronize identity
                and sequences; restore trigger state
                              ↓
                  Reapply ordinary managed views;
                refresh and compare materialized views
                              ↓
              Complete remaining objects and comments
                              ↓
               Apply users, roles, memberships and grants
                              ↓
             Validate final managed objects, table structures,
                 partitions, permissions and data
                              ↓
                    Write JSON / HTML / audit reports
```

The console uses shared phase names such as Extensions, Schemas and Row-Level Security. A completed shared phase does not mean that Oracle provisions a PDB or supports extensions or row-level security migration. The actual Oracle operations and their results are recorded in the JSON report.

### Object and dependency ordering

| Step | What FULL actually does | Implementation / safety |
|---|---|---|
| 1. Connection | Resolves configured secrets, connects to both PDB services as their owners and verifies the target is accessible. | `OracleSourceConnector.connect()`, `OracleTargetConnector.connect()`, `ensure_database_exists()`; no database or PDB creation. |
| 2. Source discovery | Lists application tables and captures supported schema and object definitions. Excludes materialized-view storage tables and internal identity sequences from independent migration. | `list_objects()`, `get_schema()`, `_oracle_replacement_inventory()`. |
| 3. Preflight | Checks supported types, source/target constraint states, dependencies, replacement scope, overwrite authorization and required privileges. Discovers the permission graph when enabled. | `plan_schema_replacement()`, `preflight_permission_migration()`, `inspect_object_privileges()`. Unsafe or unknown states block execution. |
| 4. Managed replacement | Applies the authorized cleanup plan for conflicting managed objects, retaining independent target-only objects. | `apply_schema_replacement()`; no blanket schema drop. Oracle DDL can commit here. |
| 5. Types and sequences | Creates supported custom types in dependency order and sequences before tables that reference them. | `create_type()`, `create_sequence()`; existing sequence state is not intentionally rewound. |
| 6. Tables and partitions | Creates missing tables or reconciles supported column definitions. Includes PK and identity definitions; partitioned tables are prepared through their dedicated path. | `create_object_if_missing()`, `reconcile_oracle_table()`; partition definitions validated before row clearing. |
| 7. Early object preparation | Prepares synonyms, ordinary views, routines/packages, triggers and materialized views, with definition and compilation checks. Synonyms whose dependencies are not yet available can be deferred. | Source-local dependency ordering, separate package specification/body, safe owner remapping. |
| 8. FULL clear | Suspends source-managed triggers and deletes rows from prepared managed tables in reverse FK order. | `clear_objects_for_full_sync()`; unrelated target-only tables are not cleared. Preparation failures skip clear/load. |
| 9. Data load | Exports source rows and inserts them using bound parameters, in FK parent-first order and configured batches. | `export_full()`, `upsert_batch()`; despite the common method name, native FULL uses INSERT after clear. |
| 10. Constraints and indexes | Applies supported unique, check and foreign-key constraints and secondary indexes. PK/UNIQUE backing indexes are not migrated separately. | `apply_constraints()`; preserves ordered index keys and supported directions. |
| 11. State synchronization | Restores identity generation mode where temporarily changed, advances identity generators past loaded values, advances supported standalone sequences and restores source trigger state. | Identity work occurs during batch loading; later sequence/state phases complete synchronization. |
| 12. Views and materialized views | Reapplies ordinary managed views after later table/constraint/identity DDL. Refreshes supported materialized views and compares their stored results against the source query. | `create_view()`, `refresh_materialized_view()`; prevents early compilation from being mistaken for final validity. |
| 13. Remaining objects and comments | Completes deferred object work and applies table/column comments. Routine and trigger definitions prepared earlier are reused rather than blindly recreated. | Owner-aware object helpers in `objects/`. |
| 14. Security | Creates missing mapped principals under the configured policy, applies memberships, object/column grants and approved system grants, and verifies permissions. | `permissions.py`; the executor and TARGET owner perform their authorized operations. |
| 15. Validation | Verifies replacement inventory, supported table definitions, managed constraint states, partition-wise row counts, final object status and data checks. | `verify_schema_replacement()`, `verify_managed_object_status()`, shared `validate()`. Required failures prevent a successful FULL result. |

### Example: actual healthcare table order

The verified source contains five application tables with these foreign-key dependencies:

```text
DEPARTMENTS ──→ DOCTORS ───────┐
                              ├──→ APPOINTMENTS ──→ PAYMENTS
PATIENTS ─────────────────────┘
```

A valid parent-first load order is `DEPARTMENTS`, `DOCTORS`, `PATIENTS`, `APPOINTMENTS`, `PAYMENTS`. Clear uses the reverse dependency order. Independent tables may be ordered differently by the dependency sorter; the dependency rule, not this exact example list, is the contract. Unsupported FK cycles or dependencies outside the managed scope are rejected rather than disabling integrity indiscriminately.

---

## Part 2 — Connector Architecture

### Shared framework and Oracle-specific implementation

```text
migration_platform/__main__.py
  ├─ YAML + common secret provider
  ├─ SOURCE_CONNECTORS / TARGET_CONNECTORS
  └─ MigrationOrchestrator.run_full()
       ├─ OracleSourceConnector  → SOURCE service / owner
       ├─ Shared Schema / object definitions and dependency sorter
       ├─ OracleTargetConnector  → TARGET service / owner
       │    └─ Permission executor connection (when enabled)
       └─ Validation, reporting and audit logs

migration_platform/oracle_setup_plan.py
  └─ Separate administrative PLAN / APPLY workflow
     (not a required step in every FULL run)
```

### Key files and responsibilities

| Repository path | Responsibility |
|---|---|
| `migration_platform/__main__.py` | CLI entry point, engine registries, connector initialization and FULL dispatch. |
| `core/connectors/base.py` | Shared `SourceConnector`, `TargetConnector`, `Schema` and object definition contracts. |
| `core/orchestrator.py` | FULL sequencing, Oracle inventory, preflight, dependency handling, data load, objects, security and final validation. |
| `core/dependency_order.py` | Shared FK ordering used for parent-first load and reverse-order clear. |
| `core/secrets/` | Shared secret-provider creation and resolution. |
| `core/connectors/oracle/source.py` | `OracleSourceConnector`: owner-local discovery, schema decoding and row export. |
| `core/connectors/oracle/target.py` | `OracleTargetConnector`: target DDL/DML, reconciliation, object application, constraints, identity and final checks. |
| `core/connectors/oracle/_models.py` | Oracle connection/metadata helpers and catalog-related utilities. |
| `core/connectors/oracle/precision.py` | Native datatype allowlist, precision-safe data transport and comparison helpers. |
| `core/connectors/oracle/type_mapping.py` | Scalar type mapping for supported cross-engine paths; not the native FULL acceptance contract. |
| `core/connectors/oracle/indexes.py` | Normalizes native index metadata to ordered column keys, including supported intrinsic DESC representations. |
| `core/connectors/oracle/replacement.py` | Builds target inventory and replacement plans, validates dependencies and overwrite authorization, applies planned cleanup. |
| `core/connectors/oracle/permissions.py` | Permission policy, principal mapping, authority checks, reconciliation, provenance and verification. |
| `core/connectors/oracle/objects/` | Object-specific discovery and DDL helpers: sequences, views, routines, triggers, types, partitions, synonyms, comments and security. |
| `migration_platform/oracle_setup_plan.py` | One-time/conditional setup planning and explicitly approved provisioning. |
| `core/reporting/report_builder.py` | Shared report generation. |

### Connection and owner scope

- `service_name` selects the Oracle service/PDB; `username` selects the owner. They are different concepts.
- SOURCE and TARGET may use the same listener but distinct PDB services. The verified configuration uses `source_db_new` and `target_db_new`.
- The connector uses `python-oracledb` Thin mode. Optional TCPS support is not live-accepted in this local test.
- Discovery uses the connected owner's catalogs. `include_schemas`, if provided, must match that owner; `include_tables` narrows table discovery. Owner-wide discovery is the normal local configuration.
- Runtime uses the SOURCE owner for reads, the TARGET owner for schema/data operations and the configured TARGET executor for authorized user/role operations. PDB administrator setup remains separate. Normal FULL does not log in as SYS.

### Consistency with other database engines

| Concern | Common framework | Oracle-specific implementation |
|---|---|---|
| Configuration and CLI | Shared YAML, secrets, connector registries and FULL entry point. | Oracle service name and owner scope. |
| Schema and objects | Shared schema/object definitions and engine-native connector methods. | Oracle dictionaries, native types, identity, PL/SQL and local owner remapping. |
| Data and dependency order | Shared batch orchestration, FK order and reporting. | Native INSERT after managed DELETE, with Oracle LOB/precision handling. |
| Existing target | Managed reconciliation and dependency-aware safety. | Oracle replacement plan and implicit DDL commit boundaries. |
| Security | Engine-native principal/grant operations under orchestration. | Explicit executor authority, role ADMIN OPTION checks and permission provenance. |
| Validation | Common result/validation contracts. | Oracle catalog status, constraint, partition, materialized-view and grant checks. |

Oracle follows the same architectural pattern as MySQL, PostgreSQL, MSSQL and Db2. Engine-specific SQL and safety rules remain in the appropriate connector. Do not add Oracle SQL to peer connectors or force identical DDL semantics across engines.

---

## Part 3 — Object Discovery Summary

| Object | Source method / catalog | Target path |
|---|---|---|
| Tables, columns, identity | `list_objects()`, `get_schema()`; `USER_TABLES`, `USER_TAB_COLS`, `USER_TAB_IDENTITY_COLS` | Table creation/reconciliation and identity synchronization. |
| PK, UNIQUE, CHECK, FK | `get_schema()`; `USER_CONSTRAINTS`, `USER_CONS_COLUMNS` | PK with table DDL; supported remaining constraints via `apply_constraints()`. |
| Secondary indexes | `get_schema()`; `USER_INDEXES`, `USER_IND_COLUMNS`, `USER_IND_EXPRESSIONS` | `indexes.py` normalization; create/compare supported ordered keys. |
| Partitions | Schema/partition discovery; `USER_PART_TABLES`, `USER_TAB_PARTITIONS`, partition key metadata | Partition DDL, definition and partition-row verification. |
| Sequences | `list_all_sequences()`; `USER_SEQUENCES` | Create and advance supported sequence state. |
| Custom types | `list_types()`; owner type/collection metadata | Dependency-ordered type DDL. |
| Ordinary views | `list_views()`; `USER_VIEWS`, dependency metadata | Early creation and final post-table-DDL reapplication. |
| Materialized views | `list_materialized_views()`; `USER_MVIEWS` | Restricted create/revalidate/refresh and query-result comparison. |
| Functions, procedures, packages | `list_functions()`; `USER_OBJECTS`, `DBMS_METADATA.GET_DDL`, dependencies | `create_function()`; specifications before bodies, compilation checks. |
| Triggers | `get_all_triggers()`; `USER_TRIGGERS` | Create, suspend during managed row writes, restore source state. |
| Synonyms | `list_synonyms()`; `USER_SYNONYMS` | `create_synonym()`; supported local base-object owner remapping. |
| Comments | `list_comments()`; `USER_TAB_COMMENTS`, `USER_COL_COMMENTS` | `apply_comment()`. |
| Permissions | `discover_permission_inventory()`; owner grants and authorized DBA security catalogs | Preflight, principal/membership/grant application and verification. |

Catalog access is also used on the TARGET to check dependencies and existing constraint/grant state. Required cross-owner visibility must be provisioned; missing visibility is not interpreted as an empty inventory. For object support restrictions, see Document 02.

### Three important implementation details

1. **Native DESC indexes:** Oracle 26ai returned `FUNCTION-BASED NORMAL`, hidden `SYS_NC...` key, `DESC`, and the bare quoted original column in `USER_IND_EXPRESSIONS`. The normalizer resolves this supported form to the original visible column and preserves DESC. Arbitrary function-based expressions still fail closed.
2. **Identity columns:** `Column.is_identity` carries the identity flag. Native FULL loads explicit IDs; `GENERATED ALWAYS` is temporarily relaxed as required and restored, while `GENERATED BY DEFAULT` retains its mode. The target load path uses `START WITH LIMIT VALUE` to synchronize the generator. The test generated patient ID 11 after loading IDs 1–10.
3. **Final object validity:** Early compilation can become stale after later DDL. Managed views are reapplied after table DDL. Final replacement verification scans the managed inventory in `USER_OBJECTS` and `USER_ERRORS`; replacement verification also checks managed constraints. Independent target-only objects are outside the managed status gate. The current code rejects managed catalog error rows and does not simply rely on successful creation messages.

---

## Part 4 — Existing Target and Failure Handling

FULL is replacement synchronization of source-managed data and supported object definitions, not a global PDB reset. It removes extra rows from managed tables and restores source rows/values on a successful run. The replacement plan preserves independent target-only objects where safe; conflicting dependencies or unsupported states can block the run. Permission reconciliation has separate provenance and authority checks (see Document 04).

| Failure point | Effect / developer action |
|---|---|
| Connection or required catalog access | Resolve endpoint, secret or approved authority problem; do not assume metadata is absent. |
| Unsupported source metadata, unsafe replacement plan or insufficient authority | Stop at the relevant preflight; inspect reported blockers. |
| Table or object preparation | Managed row clear/load is guarded, but already completed Oracle DDL may remain. |
| Clear or batch load | Clear and batches commit separately; inspect partial target state before retry. |
| Constraints, objects, permissions or final validation | Required failure prevents FULL success; previously committed changes may remain. |

Oracle DDL commits implicitly. FULL is **not** one atomic transaction over the entire owner. Never assume a failed report means the target is untouched, even if migrated rows equal zero. Review the actual target inventory and reports before rerunning; do not manually drop or compile objects merely to bypass the reported issue.

---

## Part 5 — Verification and Recorded Results

### Report fields to inspect

| JSON path under `phases` | What to check |
|---|---|
| `oracle_source_datatype_preflight` | Supported source storage and types. |
| `oracle_schema_replacement_plan` | No unresolved blockers or inspection errors. |
| `oracle_overwrite_authorization` | Replacement policy allows the planned work. |
| `oracle_privilege_preflight` / `oracle_permission_preflight` | Required authority and permission plan, when configured. |
| `oracle_object_preparation` | Early object preparation succeeded. |
| `full_target_sync` / `data_load_order` | Managed clear set and dependency order. |
| `data` / `data_load` | Per-table load outcomes; no failed or skipped required tables. |
| `apply_constraints` / `views` / `materialized_views` | Successful required object work; reapplication and refresh outcomes. |
| `oracle_permission_verification` | Object/column, system-grant and membership verification. |
| `oracle_schema_replacement_verification` | `status: verified`, partition-wise row counts and final managed-object result. |
| `oracle_schema_replacement_verification.managed_object_status_validation` | `status: verified`, empty `invalid_source_managed_objects`, zero `source_managed_compilation_errors`. |
| `validation` / `object_failures` | Required data checks pass and no required object failures. |

Reports: `reports/<run_id>.json`, `reports/<run_id>.html` and `logs/<run_id>.jsonl`. The default `validation.mode: count` is not a substitute for independent full row-value or object-definition comparison.

### Verified migration directions

| Direction | Status | Config / evidence |
|---|---|---|
| Local → Local | Passed for documented native scope | `config/oracle_local_to_local.yaml`; first and repeat FULL, independent object verification. |
| Local → Cloud | Not live-verified here | No accepted cloud execution evidence. |
| Cloud → Local | Not live-verified here | No accepted cloud execution evidence. |

### Latest local acceptance evidence

| Run | Run ID | Tables / rows | Errors | Duration |
|---|---|---|---|---|
| Corrected FULL | `bfd97adbdcd14347a5108d8ef6792e72` | 5 / 50 | 0 | 196.2 s |
| Corrected repeat FULL | `f79b3095da5e406e92e75416a3f1fc8e` | 5 / 50 | 0 | 33.9 s |

Final independent read-only verification after the corrected repeat FULL:

```text
Missing or mismatched SOURCE objects: []
Invalid TARGET objects: []
TARGET compilation errors: []
FINAL ORACLE OBJECT VERIFICATION: PASS
```

Earlier independent acceptance also verified exact data parity, functional constraints and nullability, supported indexes/partitions, views/materialized-view results, routine/package execution, synonyms, comments, grants, locked users, role memberships, overwrite of a modified target row and identity generation. The prior repeat-run invalid view finding is closed for the tested local environment. These results are fixture-specific, not proof of every supported variant or production workload.

Latest reported test results: 2,118 project unit tests, 826 Oracle unit tests and 192 focused repeat-FULL/replacement regression tests passed (overlapping selections). Oracle-scope Ruff, compilation and `git diff --check` passed. Fourteen generic integration tests were collected but not run; two guarded Oracle live E2E tests were skipped. The isolated Oracle-specific integration suite was unavailable.

The exact nested managed-status result in both corrected JSON reports was not supplied for this consolidation; the report field above is verified from the code contract, while live acceptance is grounded in the supplied console and independent catalog results. No additional database execution was performed to prepare this document.

## Reference

- [01 — Oracle Setup and Migration Guide](01_Oracle_Setup_and_Migration_Guide.md): prerequisites, configuration, setup prompts and commands.
- [02 — Oracle Object Support Matrix](02_Oracle_Object_Support_Matrix.md): feature coverage and restrictions.
- [04 — Oracle Existing Target and Permission Handling](04_Oracle_Existing_Target_and_Permission_Handling.md): detailed replacement and security scenarios (planned companion).
- [05 — Oracle Testing and Acceptance Runbook](05_Oracle_Testing_and_Acceptance_Runbook.md): reproducible test SQL, validation and troubleshooting (planned companion).

Consolidation basis: supplied `docs.zip` (Oracle migration flow, connector design, FULL guide and local runbook; PostgreSQL/MSSQL migration flows and Db2 technical reference), the later supplied `dmt_migration.zip` implementation snapshot, and final operator/Codex acceptance transcripts. Older pending-live statements in the source documents are superseded only where the later evidence establishes a pass.
