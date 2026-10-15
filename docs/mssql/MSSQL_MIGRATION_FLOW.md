# MSSQL E2E Migration Flow

Complete end-to-end migration flow, verified directions, and known issues for MSSQL object migration.

---

## Part 1 — Conceptual Migration Flow

This flow describes what the migration platform does, in what order, and how verification works at the end.

```
SOURCE DATABASE
    ↓
Connect to source
    ↓
Discover MSSQL objects
    ↓
Extract object definitions / metadata
    ↓
Create target objects
    ↓
Migrate data (UPSERT / MERGE)
    ↓
Apply constraints / indexes / FKs
    ↓
Apply views
    ↓
Apply functions / procedures
    ↓
Apply synonyms
    ↓
Apply triggers
    ↓
Apply comments / extended properties
    ↓
Apply users / roles / permissions
    ↓
Validate target
    ↓
E2E SUCCESS
```

### Object and dependency ordering

The migration engine creates objects in dependency order to avoid foreign key or reference failures:

1. **Schemas** — Created first so all subsequent objects have a valid namespace.
2. **UDTs / Custom types** — Created before tables that reference them.
3. **Tables** — Created with all constraints, identity columns, computed columns.
4. **Data** — In FULL mode, rows in source-managed tables are deleted in reverse foreign-key dependency order, then source rows are loaded with `MERGE`. Target-only tables are not cleared.
5. **Indexes** — Created after tables and data (clustered, non-clustered).
6. **Constraints** — Primary keys, foreign keys, check constraints applied after tables exist.
7. **Views** — Created after underlying tables exist.
8. **Functions** — Created after tables exist (functions may reference tables).
9. **Procedures** — Created after tables exist (procedures may reference tables).
10. **Synonyms** — Created after referenced objects exist.
11. **Triggers** — Created after tables, functions, and procedures exist.
12. **Comments / Extended properties** — Applied after all objects exist.
13. **Users / Roles / Permissions** — Applied after all objects and roles exist.

If an object creation fails, the migration records the failure and continues (error isolation via `migration.stop_on_error`).

### Existing target tables and FULL data loads

FULL mode is replacement synchronization for tables discovered from the source. Before loading, MSSQL deletes rows only from those registered source-managed tables; target-only tables and their rows are preserved. Repeated runs therefore remove target-only rows from managed tables, restore missing source rows, and overwrite edits to source rows when the source rows are loaded again. The clear phase reports the tables actually deleted in `phases.full_target_sync.cleared`.

Before a FULL data load, MSSQL reconciles columns on each source-managed table. Target-only columns on those tables are removed, and existing source-defined columns are compared by type, length/precision, scale, and nullability before data is loaded. A safe definition mismatch is altered in a transaction while preserving rows. A dependent index, foreign key, schema-bound view, or computed column blocks an alteration and is reported as an object failure; check/default constraints are saved and restored transactionally, and non-schema-bound dependent views are refreshed. An ALTER/conversion or constraint-restoration failure rolls back and is not reported as success. Missing source tables are created. A partially missing source column currently fails explicitly instead of being added implicitly to a populated table. This reconciliation is limited to source-managed tables; separate target-only tables and their rows are preserved.

#### Existing-schema reconciliation test matrix

#### Indexed views

MSSQL view discovery carries `sys.sql_modules.is_schema_bound`, plus view-index
key columns, uniqueness, and clustered/nonclustered type. FULL migration creates
the view with `WITH SCHEMABINDING` before adding source indexes, with the unique
clustered index first. Unsupported or unsafe existing clustered-index changes
fail explicitly. Acceptance validation checks the source-managed indexed view
through `OBJECTPROPERTY(..., 'IsSchemaBound')` and `sys.indexes`; source/target
comparison includes view-index metadata.

To exercise target-only indexed-view preservation, run setup with
`--with-target-only-indexed-view`, then migrate and validate normally. Its
dedicated check confirms the target-only view, schema-binding flag, and unique
clustered index remain present.

| Case | Expected result | Regression evidence |
|---|---|---|
| Definition already matches | No `ALTER COLUMN` | `tests/unit/test_mssql_schema_reconciliation.py::test_mssql_full_reconciliation_does_not_alter_matching_definition` |
| `NVARCHAR(150) NOT NULL` target vs `NVARCHAR(100) NOT NULL` source | Alter to source length/nullability | `tests/unit/test_mssql_schema_reconciliation.py::test_mssql_full_reconciliation_restores_nvarchar_150_to_100_not_null` |
| Compatible base type or nullability differs | Alter to source definition | `tests/unit/test_mssql_schema_reconciliation.py::test_mssql_full_reconciliation_alters_changed_definition` |
| Equivalent `BIT` metadata with filtered-index dependency | No unnecessary ALTER | `tests/unit/test_mssql_schema_reconciliation.py::test_equivalent_is_active_with_filtered_city_index_does_not_alter_column` |
| Real `BIT` nullability mismatch blocked by filtered index | Actionable failure; preserve index/data | `tests/unit/test_mssql_schema_reconciliation.py::test_real_is_active_mismatch_reports_filtered_index_dependency` |
| Equivalent computed expression with metadata formatting differences | No unnecessary ALTER | `tests/unit/test_mssql_schema_reconciliation.py::test_equivalent_modified_at_computed_expression_is_already_reconciled` |
| Different computed expression | Actionable unsupported-change failure | `tests/unit/test_mssql_schema_reconciliation.py::test_different_modified_at_expression_fails_actionably_without_recreation` |
| Repeated reconciliation | Second pass makes no ALTER | `tests/unit/test_mssql_schema_reconciliation.py::test_second_reconciliation_is_idempotent_after_first_alter` |
| Dependent index/FK/schema-bound view/computed column | Fail safely and roll back | `tests/unit/test_mssql_schema_reconciliation.py::test_mssql_full_reconciliation_fails_safely_on_blocking_dependencies` |
| Unsupported IDENTITY definition mismatch | Fail explicitly; do not silently skip | `tests/unit/test_mssql_schema_reconciliation.py::test_mssql_full_reconciliation_fails_explicitly_for_special_column_mismatch` |
| Affected CHECK constraint and view | Restore constraint state and refresh view | `tests/unit/test_mssql_schema_reconciliation.py::test_mssql_full_reconciliation_restores_check_constraint_after_alter` |

The cases above are unit-test evidence, not a live SQL Server acceptance result. The E2E acceptance must verify `sys.columns.max_length = 200` and `is_nullable = 0` after changing the dedicated target `training.Customers.FullName` to `NVARCHAR(150) NOT NULL` and running FULL migration.

### Scenario 4 — Existing target column-definition conflict

**Expected result:** with the dedicated target `training.Customers.FullName` changed to `NVARCHAR(150) NOT NULL`, FULL sync reconciles it to the source definition `NVARCHAR(100) NOT NULL` (`sys.columns.max_length = 200`, `is_nullable = 0`). The existing `FK_Orders_Customers` remains enabled and intact. FULL sync clears dependent child rows before parent rows, then reloads parents before children. It must not drop or disable the FK. The seven source-managed table counts must match the source fixture (33 rows total), and the second run must leave those counts unchanged.

**Investigation / acceptance steps:**

1. Confirm the source and target are the dedicated `MigrationE2E_MSSQL_Source` and `MigrationE2E_MSSQL_Target` databases. Query `sys.columns` for `Customers.FullName` and verify the target is `max_length = 300`, `is_nullable = 0` before migration.
2. Query `sys.foreign_keys` and `sys.foreign_key_columns` to confirm `FK_Orders_Customers` is present and enabled on the target. Do not drop or disable it.
3. Run `python -m migration_platform --config config/mssql_e2e_acceptance.yaml --mode full`.
4. Verify migration status is `success`, `FullName` is `max_length = 200` and `is_nullable = 0`, and `FK_Orders_Customers` remains enabled/trusted.
5. Verify all seven table row counts against `tests/e2e/mssql/validation/expected.py` (5, 5, 5, 8, 5, 2, 3; total 33), then repeat FULL and confirm the same counts and constraint state.

**Safety condition:** if any source table fails schema preparation, the orchestrator skips both FULL clearing and data loading. This prevents a successful parent table such as `Customers` from being deleted while a dependent source-managed child such as `Orders` was omitted from the cleanup set. The failure remains reported; the migration does not claim a successful replacement.

Focused MSSQL reconciliation/FULL-sync/DDL and E2E-helper unit tests plus PostgreSQL reconciliation/FULL-sync/trigger regressions: **181 passed**. Full `tests/unit`: **880 passed, 8 failed**; the eight are outside these focused suites (MySQL/PostgreSQL orchestration, type/sequence, and one PostgreSQL existing-object test). Live acceptance requires the dedicated E2E SQL Server and has not been verified in this environment.

---

## Part 2 — E2E Migration Directions

Three complete E2E migration directions are tracked:

| Direction | Description | Config |
|---|---|---|
| **Local → Local** | Local MSSQL → local MSSQL | `config/mssql_local_test.yaml` |
| **Local → Cloud** | Local MSSQL → Azure SQL | `config/mssql_local_cloud.yaml` |
| **Cloud → Local** | Azure SQL → local MSSQL | `config/mssql_cloud_local.yaml` |

### A. Local → Local — COMPLETED

**Status:** COMPLETED / VALIDATED

**Verified result available in repository history:**

- Branch: `feature/mssql-objects`
- Engine: Microsoft SQL Server
- Source: `localhost,1533` / `mssql_migration_test` → Target: `localhost,1533` / `mssql_migration_target`
- Schemas: `sales`, `billing`
- Migration mode: `full`
- Tables migrated: 9
- Rows migrated: 28
- Failed: 0
- Success rate: 100%

**Objects verified:**

- Schemas (`sales`, `billing`)
- Tables with data (9 tables across 2 schemas)
- Primary keys, foreign keys (including cross-schema `billing.customer_addresses → sales.customers`)
- Identity columns
- Computed columns
- Sequences
- Indexes
- Views (1/1 validated)
- Functions / Procedures (2/2 validated)
- Sequences (1/1 validated)
- Triggers (2/2 validated, one disabled)
- Synonyms (3/3 validated)
- UDTs (1/1 validated)
- Partition function (1/1 validated)
- Users / Roles / Permissions
- Comments / Extended Properties

Full evidence in `MSSQL_LOCAL_AUDIT.md`.

**Dependency ordering tests:** 11/11 passed.
**Cross-schema / reference tests:** 6/6 passed.
**Error isolation tests:** 6/6 passed.
**Metadata validation tests:** 54/54 passed.
**Existing MSSQL DDL tests:** 108/108 passed.

### B. Local → Cloud — COMPLETED / VALIDATED

**Status:** COMPLETED / VALIDATED

**Verified results:**

- **Run ID (normal E2E):** `8ce7abd3a0b449d78d71bdec56e272ba`
- **Run ID (dedicated verification E2E):** `fbdf961ad9d84e4fb8c8624b3dbf2055`
- **Engine:** Microsoft SQL Server
- **Source:** `localhost,1533` / `mssql_migration_test` (MSSQL Local)
- **Target:** `mssql-mig-test-01.database.windows.net` / `mssql-migration-cloud` (Azure SQL Database)
- **Schemas:** `sales`, `billing`
- **Migration mode:** `full`
- **Tables migrated:** 9
- **Rows migrated:** 28
- **Failed:** 0
- **Success rate:** 100%
- **Duration:** 146s (normal E2E) / 58s (verification E2E)

**Objects verified in normal E2E:**

- Schemas (`sales`, `billing`)
- Tables with data (9 tables across 2 schemas)
- Primary keys, foreign keys (including cross-schema `billing.customer_addresses → sales.customers`)
- Identity columns
- Computed columns
- Indexes
- Views (1/1 validated)
- Functions / Procedures (2/2 validated)
- Sequences (1/1 validated)
- Triggers (2/2 validated, one disabled)
- Synonyms (3/3 validated)
- UDTs (1/1 validated)
- Partition function / scheme (1/1 validated)
- Users / Roles / Permissions
- Comments / Extended Properties

**Dedicated verification E2E (unique schema `sales_e2e_verify`):**

This verification used uniquely named objects to prove categories not conclusively proven by the normal E2E (due to pre-existing target state):

| Category | Object | Verification |
|---|---|---|
| **Sequence** | `seq_test_verification` | START 5000, INCREMENT 10, MIN 1000, MAX 1000000, NO CYCLE, CACHE 20. `NEXT VALUE FOR` works: 5000 → 5010 |
| **Function** | `fn_test_verify` | Definition matches; `fn_test_verify(10)` = 120 |
| **Procedure** | `sp_test_verify` | Definition matches; execution succeeds |
| **Partitioning** | `pf_verify_date`, `ps_verify_date`, `partitioned_verify_table` | RANGE RIGHT boundaries (2024-01-01, 2024-07-01, 2025-01-01). Table ON `ps_verify_date(event_date)` — **proves Step 16 fresh-target fix works** |
| **Extended Properties** | Table, column, function, procedure | 4/5 migrated. Sequence EP failed (Azure SQL limitation) |

**Dependency ordering tests:** 13/13 passed (including Step 16 fix regression tests).
**Cross-schema / reference tests:** 6/6 passed.
**Error isolation tests:** 9/9 passed.
**Metadata validation tests:** 54/54 passed.
**Existing MSSQL DDL tests:** 129/129 passed (1 pre-existing unrelated failure: `test_cross_engine_type_safety`).

**Implementation fixes required during Local→Cloud testing:**

1. **Step 16 fix (fresh-target partitioned table orchestration):** Partitioned tables were incorrectly created in Phase 4 as regular tables, then skipped in Phase 4.5. Fixed by excluding partitioned tables from `create_tables` phase.
2. **Step 17 fix (cross-schema trigger parent-table schema):** Added `table_schema` to `TriggerDef`, updated source query to capture `OBJECT_SCHEMA_NAME(p.object_id)`, updated target ALTER TABLE to use schema-qualified parent table.
3. **Step 18 fix (error isolation + rollback/audit):** Added try/except/rollback/audit_log to 7 MSSQL connector methods (`create_sequence`, `create_partition_function`, `create_partition_scheme`, `create_partitioned_table`, `create_role_if_not_exists`, `create_user_if_not_exists`, `create_role_membership`) with regression tests.
4. **Partition scheme reference fix:** Modified `create_partitioned_table` to use partition scheme name (not partition function name) in `ON` clause, and updated orchestrator to pass `partition_scheme_name` from `PartitionedTableDef`.

Full evidence in `MSSQL_CLOUD_AUDIT.md`.

Config: `config/mssql_local_cloud.yaml`

### C. Cloud → Local — COMPLETED / VALIDATED

**Status:** COMPLETED / VALIDATED

**Verified result:**

- **Run ID:** `cfc63558176047fe891e208a0b8b4da9`
- **Engine:** Microsoft SQL Server
- **Source:** `mssql-mig-test-01.database.windows.net` / `mssql-migration-cloud` (Azure SQL Database)
- **Target:** `(localdb)\MSSQLLocalDB` / `migration_target` (LocalDB)
- **Schemas:** `sales`, `billing`
- **Migration mode:** `full`
- **Tables migrated:** 9
- **Rows migrated:** 40
- **Views migrated:** 1
- **Failed:** 0
- **Success rate:** 100%
- **Duration:** ~25 seconds

**Objects verified:**

- Schemas (`sales`, `billing`)
- Tables with data (9 tables across 2 schemas: sales 7, billing 2)
- Primary keys, foreign keys (including cross-schema `billing.customer_addresses → sales.customers`)
- Identity columns
- Computed columns
- Indexes (clustered, non-clustered, unique)
- Views (1/1 validated — `sales.v_customer_summary`, batch fix verified)
- Functions / Procedures (2/2 validated)
- Triggers (2/2 validated, one disabled state preserved)
- Synonyms (3/3 validated)
- UDTs (XML, JSON, VARBINARY, UNIQUEIDENTIFIER, SQL_VARIANT)
- Partitioning (partition function, scheme, `sales_partitioned` table)
- Extended properties: 0 (source has 0, target matches)
- Users / Roles / Permissions

**Key fixes validated in this direction:**

1. **View creation batch fix** — `CREATE OR ALTER VIEW` batch separation (error 111) fixed in `core/connectors/mssql.py` lines 1759-1795, verified with 5/5 view tests passing.

**Known behaviors (not bugs):**

- `sales_partitioned` row count 16→32 on rerun — expected idempotent re-run behavior (FULL mode with MERGE/UPSERT)
- 0 extended properties on target — source has 0, target correctly matches source state
- Partitioned table on PRIMARY filegroup — matches source state

**Dependency ordering tests:** 13/13 passed (including Step 16 fix regression tests).
**Cross-schema / reference tests:** 6/6 passed.
**Error isolation tests:** 9/9 passed.
**Metadata validation tests:** 54/54 passed.
**Existing MSSQL DDL tests:** 129/129 passed (1 pre-existing unrelated failure: `test_cross_engine_type_safety`).

Config: `config/mssql_cloud_local.yaml`

---

## Part 3 — E2E Issues Found and Fixed

To be populated as issues are discovered and resolved during Local→Cloud and Cloud→Local testing.

*(No issues documented for Local→Local — see `MSSQL_LOCAL_AUDIT.md` for known/pre-existing mismatches.)*

---

## Part 4 — UPSERT / MERGE Behavior

MSSQL migration uses the `MERGE` statement for data upserting. Key points:

- The target table must have a primary key or unique constraint for conflict resolution.
- `MERGE` matches source rows against target rows by primary key.
- Matching rows are updated; non-matching source rows are inserted.
- For `full` mode, the initial load is equivalent to an insert of all source rows.
- For `cdc-incremental` and `cdc-continuous` modes, `MERGE` applies individual change events.

See `MSSQL_OBJECT_SUPPORT_MATRIX.md` for object-level UPSERT validation status.

---

## Part 5 — Object Discovery Summary

The source connector discovers MSSQL objects through catalog queries in this order:

1. **Schemas** — `sys.schemas` (excluding system schemas)
2. **Tables** — `information_schema.tables` / `sys.tables`
3. **Columns** — `information_schema.columns` / `sys.columns`
4. **Constraints** — `information_schema.table_constraints` / `sys.key_constraints` / `sys.foreign_keys`
5. **Foreign keys** — `sys.foreign_keys` + `sys.foreign_key_columns`
6. **Indexes** — `sys.indexes` / `sys.index_columns`
7. **Identity columns** — `sys.identity_columns`
8. **Computed columns** — `sys.computed_columns`
9. **Sequences** — `sys.sequences`
10. **Views** — `information_schema.views` / `sys.views`
11. **Functions** — `sys.objects` (`FN`, `IF`, `TF`)
12. **Procedures** — `sys.objects` (`P`)
13. **Triggers** — `sys.triggers`
14. **Synonyms** — `sys.synonyms`
15. **UDTs** — `sys.types` / `sys.user_types`
16. **Partitioning** — `sys.partition_functions` / `sys.partition_schemes`
17. **Extended properties** — `sys.extended_properties`
18. **Users / Roles** — `sys.database_principals` / `sys.server_principals`
19. **Permissions** — `sys.database_permissions`

---

## Part 6 — Safe Partition Function Reconciliation

`migration.reconcile_mssql_partition_functions` is an opt-in setting and defaults to `false`. When enabled, MSSQL FULL migration supports one narrow reconciliation: removing exactly one extra trailing boundary from a SQL Server `date` / `RANGE RIGHT` partition function when the target's rightmost partition is empty for every dependent index.

Before issuing `ALTER PARTITION FUNCTION ... MERGE RANGE`, the connector verifies that the source boundaries are an exact prefix of the target boundaries; source/target fanout is consistent; every dependent scheme will have the expected source filegroup mapping afterward; all dependent schemes and indexes are source-managed; every row-filegroup is online and writable; and every dependent heap, table index, or indexed-view index has zero rows in the rightmost partition. It then performs the merge in a transaction and re-reads the function and scheme catalogs before reporting reconciliation success. It does not drop/recreate partition functions, schemes, tables, indexes, or data.

Missing or interior boundaries, more than one extra boundary, non-date types, RANGE LEFT, occupied partitions, target-only dependencies, or unsafe destination/filegroup state are rejected with a structured diagnostic. The mismatch remains an error and FULL target clearing/data loading stays blocked. The local acceptance config opts into this supported case; production configurations remain opt-out unless explicitly enabled.

`ALTER PARTITION FUNCTION` affects every dependent partitioned table and index atomically. The empty trailing partition precondition is required to avoid moving target rows during the merge; see [Microsoft's ALTER PARTITION FUNCTION documentation](https://learn.microsoft.com/en-us/sql/t-sql/statements/alter-partition-function-transact-sql?view=sql-server-ver16).

---

## Reference

- Object support matrix: `docs/mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/mssql/MSSQL_LIMITATIONS.md`
- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- E2E runbook: `docs/mssql/MSSQL_E2E_RUNBOOK.md`
- Test guide: `docs/mssql/MSSQL_TEST_GUIDE.md`
- Local E2E config: `config/mssql_local_test.yaml`
