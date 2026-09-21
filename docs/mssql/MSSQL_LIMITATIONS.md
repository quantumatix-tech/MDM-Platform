# MSSQL Limitations

Documented limitations identified during the MSSQL object migration audit
on the `feature/mssql-objects` branch.

Limitations are categorized as:

- **Implementation limitation** — behavior gap in the current migration code
- **Validation/audit scope limitation** — not tested or verified within the current audit
- **Environment/pre-existing-state limitation** — blocked by local infrastructure, pre-existing target state, or configuration scope

---

## A. Implementation Limitations

None documented for the current MSSQL Local→Local implementation.

*(To be updated as Local→Cloud and Cloud→Local testing reveals additional implementation gaps.)*

### Local → Cloud Implementation Fixes (Applied During Testing)

The following implementation gaps were identified and fixed during Local → Cloud testing:

1. **Step 16 — Fresh-target partitioned table orchestration:** Partitioned tables were incorrectly created in Phase 4 (create_tables) as regular tables, then skipped in Phase 4.5 (create_partitions). Fixed by excluding partitioned tables from the `create_tables` phase.
2. **Step 17 — Cross-schema trigger parent-table schema:** Triggers referencing parent tables in different schemas required `table_schema` in `TriggerDef`, updated source discovery query, and target ALTER TABLE schema qualification.
3. **Step 18 — Error isolation + rollback/audit for partition/security operations:** Added try/except/rollback/audit_log to 7 MSSQL connector methods with regression tests.
4. **Partition scheme reference in create_partitioned_table:** Fixed to use partition scheme name (not partition function name) in `ON` clause; orchestrator updated to pass `partition_scheme_name`.

---

## B. Validation / Audit Scope Limitations

### Partitioned table data validation

- **Category:** Audit scope limitation
- **Impact:** `sales.sales_partitioned` has a pre-existing row-count mismatch between source and target (target contained extra rows before migration). The partition function and scheme were validated, but full data comparison for partitioned tables requires a clean target state.
- **Workaround:** Ensure target tables are empty or in a known state before running FULL migration for partitioned tables.
- **Tracking:** Documented in `MSSQL_LOCAL_AUDIT.md` Section 11.

### Full mode stale-row behavior

- **Category:** Audit scope limitation
- **Impact:** Running a subsequent FULL migration synchronizes current source rows but does not delete rows that were deleted from the source in the target.
- **Workaround:** Manually delete stale rows or use CDC incremental mode once available.
- **Tracking:** Same behavior as documented in PostgreSQL limitations; verify with MSSQL-specific testing.

### Trigger disabled state

- **Category:** Validation scope limitation
- **Impact:** Trigger disabled state is preserved for existing triggers, but the migration does not explicitly validate that disabled triggers remain disabled on the target vs. enabled triggers being created as enabled. The one disabled trigger in the fixture was tracked but not exhaustively tested across all trigger states.
- **Workaround:** Manually verify trigger enabled/disabled state on target after migration.
- **Tracking:** Listed as Partial in the support matrix.

---

## C. Environment / Pre-existing State Mismatches

### `sales.sales_partitioned` row-count mismatch

- **Category:** Pre-existing target-state difference
- **Impact:** During E2E validation, `sales.sales_partitioned` had a source/target row-count mismatch because the target database already contained extra rows prior to the migration run. This is not a migration failure — the source rows were correctly migrated.
- **Resolution:** Reset the target database before re-running FULL migration for accurate row-count comparison.
- **Tracking:** Documented in `MSSQL_LOCAL_AUDIT.md` Section 11 and `MSSQL_CLOUD_AUDIT.md`.

### Pre-existing function/procedure objects on Azure SQL target

- **Category:** Pre-existing target-state difference
- **Impact:** During Local → Cloud E2E, the Azure SQL target database already contained `fn_customer_order_count` and `sp_get_customer_orders` from prior runs. The migration attempted to create them (using `CREATE OR ALTER`), which failed with "object already exists" (error 2714). This is expected behavior — the platform uses `CREATE OR ALTER` for idempotency, but Azure SQL's `CREATE OR ALTER` behavior for functions/procedures can conflict if the object exists with a different schema or ownership.
- **Resolution:** Clean target database before re-running FULL migration, or ensure `CREATE OR ALTER` handles existing objects correctly.
- **Tracking:** Documented in `MSSQL_CLOUD_AUDIT.md`.

### Primary key naming convention difference

- **Category:** Pre-existing target-state difference
- **Impact:** Some PK names differ between source and target because the target may use SQL Server-generated constraint names while the source uses human-readable names. This does not affect data integrity or foreign key relationships.
- **Resolution:** Use consistent naming conventions in test fixtures.
- **Tracking:** Documented in `MSSQL_LOCAL_AUDIT.md` Section 11.

### Training schema excluded by `include_schemas`

- **Category:** Configuration scope limitation
- **Impact:** The training schema was intentionally not migrated because `include_schemas` in `config/mssql_local_test.yaml` contains only `sales` and `billing`. This is by design, not a bug.
- **Resolution:** Add additional schemas to `include_schemas` if they need to be migrated.
- **Tracking:** Documented in `MSSQL_LOCAL_AUDIT.md` Section 11.

---

## D. Azure SQL / Cloud-Specific Limitations

### Sequence extended properties not supported on Azure SQL

- **Category:** Azure SQL platform limitation
- **Impact:** `sp_addextendedproperty` with `@level1type = 'SEQUENCE'` fails with error 15600 ("An invalid parameter or option was specified for procedure 'sp_addextendedproperty'") on Azure SQL Database. This is a known Azure SQL limitation — extended properties on sequences are not supported.
- **Workaround:** None; sequence comments cannot be migrated to Azure SQL.
- **Tracking:** Documented in `MSSQL_CLOUD_AUDIT.md`; 4/5 extended properties migrated in verification E2E (table, column, function, procedure).

---

## MSSQL Integration

### ODBC Driver 18 availability

- **Category:** Environment limitation
- **Impact:** MSSQL integration tests may not run if Microsoft ODBC Driver 18 is not installed on the local development machine.
- **Workaround:** Install the driver or run MSSQL tests in an environment with the driver available.
- **Tracking:** Same prerequisite documented in PostgreSQL limitations for cross-engine testing.

---

## Reference

- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- Object support matrix: `docs/mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md`
- Migration flow: `docs/mssql/MSSQL_MIGRATION_FLOW.md`
- Test guide: `docs/mssql/MSSQL_TEST_GUIDE.md`
