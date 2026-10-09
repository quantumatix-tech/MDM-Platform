# PostgreSQL Migration — Final Test Report

**Project:** Unified DMS / MDM Migration Platform  
**Migration Mode:** FULL  
**PostgreSQL:** 17.5  
**Source DB:** `MigrationE2E_PostgreSQL_Source`  
**Target DB:** `MigrationE2E_PostgreSQL_Target`  
**Schema:** `training`

---

## Task 1 — Existing Target Handling

### Limitations
1. **Same-name index/constraint:** Reconciliation is name-based; an object with the same name but a different definition is not automatically replaced.
2. **Dependent objects:** Removing an extra column, partition, view, or routine can fail under PostgreSQL `RESTRICT` when dependencies exist. `CASCADE` is intentionally not used.
3. **View replacement:** Incompatible view column type/order changes can make `CREATE OR REPLACE VIEW` fail. A scoped drop/recreate fallback exists, but dependencies can still cause safe failure.
4. **Routine return-type changes:** `CREATE OR REPLACE FUNCTION` cannot change the return type. The exact routine drop/recreate fallback can be blocked by dependencies.
5. **Target-only standalone objects:** Target-only views, functions, and procedures are not globally deleted; this is intentional preservation behavior.

### Test Matrix

| # | Scenario | Test Action / Expected Behavior | Actual Result | Status |
|---:|---|---|---|---|
| 1 | Existing target database | Run migration against an existing target DB | DB reused and migration completed | ✅ PASS |
| 2 | Existing target data / extra row | Run FULL migration; source state should replace extra target data | Extra row removed | ✅ PASS |
| 3 | Matching row with conflicting data | Change target value and run FULL migration | Source value restored | ✅ PASS |
| 4 | Target-only table | Create `manual_table` with data | Table and data preserved | ✅ PASS |
| 5 | Extra target column | Add `manual_note` absent from source | `manual_note` removed | ✅ PASS |
| 6 | Extra-column manual data | Populate the extra column before migration | Column and data removed | ✅ PASS |
| 7 | Existing target schema / extra object | Add `run3_extra` and run FULL migration | `run3_extra` removed | ✅ PASS |
| 8 | Extra index/constraint | Add `idx_customers_run3_extra` | Extra index removed | ✅ PASS |
| 9 | Source-managed routine | Modify `fn_GetOrderTotal` on target | Source routine restored; target-only function preserved | ✅ PASS |
| 10 | Extra partition | Add `partitionedorders_2026` | Extra partition removed | ✅ PASS |

---

## Task 2 — Existing Schema Handling

### Limitations
1. **Target-only schema validator:** An intentionally preserved schema such as `manual_test` may be marked `unexpected` by source–target comparison, causing `Overall: FAIL`.
2. **PostgreSQL dependencies:** A column type change can be blocked by a dependent target-only view, rule, or generated expression. The migration avoids `CASCADE` and should safely roll back.
3. **Type conversion:** Some PostgreSQL datatype conversions are unsupported or incompatible; reconciliation may fail/roll back rather than force a destructive conversion.
4. **Target-only dependent objects:** Unrelated target-only dependents are not deleted; dependency conflicts can cause safe failure.

### Test Matrix

| # | Scenario | Test Action / Expected Behavior | Actual Result | Status |
|---:|---|---|---|---|
| 1 | Existing schema | Keep `training` on target before migration | Existing schema reused; migration completed | ✅ PASS |
| 2 | Empty existing schema | Empty `training`, then run migration | Source objects created and data migrated | ✅ PASS |
| 3 | Schema with extra object | Create target-only `manual_test` schema and `manual_table` | Target-only schema/object preserved | ✅ PASS |
| 4 | Schema definition conflict | Change target `customers.fullname` to `VARCHAR(50)`; source is `VARCHAR(100)` | Target column and `vw_ordersummary.customername` restored to `VARCHAR(100)` | ✅ PASS |
| 5 | Repeat FULL migration | Run FULL migration again on the same target | 33/33 rows, 0 failed, 100%; structural and functional validation passed | ✅ PASS |

---

## Task 3 — Existing Table & Data Handling

### Limitations
1. **Source-managed scope:** Reconciliation applies to source-managed tables; target-only tables are preserved.
2. **Target-only objects:** Target-only schemas, tables, views, and functions are not globally deleted.
3. **Foreign-key dependencies:** FK dependencies can block `DELETE`, `TRUNCATE`, or schema changes unless handled appropriately.
4. **No blind `CASCADE`:** Reconciliation does not use unrestricted `DROP ... CASCADE`; dependencies may cause safe failure instead of deletion.
5. **Same-name definitions:** Indexes/constraints with the same name but different definitions are not automatically replaced just because their definitions differ.
6. **Incompatible type changes:** Unsupported or unsafe PostgreSQL type conversions may fail rather than force destructive conversion.
7. **Dependency conflicts:** Target-only dependent views, rules, or other objects can prevent source-managed table/column changes.
8. **Zero-row reset:** Clearing FK-related tables for testing requires dependency-aware operations such as `TRUNCATE ... CASCADE`.

### Test Matrix

| # | Scenario | Test Action / Expected Behavior | Actual Result | Status |
|---:|---|---|---|---|
| 1 | Existing table with same data | Run FULL migration against existing data | Migration completed without duplicate rows | ✅ PASS |
| 2 | Existing table with extra rows | Add target-only customer row `id=99` | Extra row removed; target returned to source data | ✅ PASS |
| 3 | Modified existing row | Change target customer data | Source value restored | ✅ PASS |
| 4 | Extra column with data | Add/populate `manual_note` | Extra column and its data removed | ✅ PASS |
| 5 | Empty extra column | Add empty `manual_note` | Extra column removed | ✅ PASS |
| 6 | Target missing source column | Remove `phone` from target | `phone` restored | ✅ PASS |
| 7 | Different column definition | Change `fullname` from `VARCHAR(100)` to `VARCHAR(50)` | `VARCHAR(100)` restored | ✅ PASS |
| 8 | Repeated FULL migration | Run FULL migration multiple times | No duplicate data; source state maintained | ✅ PASS |
| 9 | Target table with zero rows | Clear `customers` while source has 5 rows | All 5 source rows restored | ✅ PASS |
| 10 | Source–target comparison | Validate structure and data after reconciliation | Source-managed tables/data matched expected state | ✅ PASS |

---

## Task 4 — Existing Object Handling

### Limitations
1. **Materialized-view changes:** PostgreSQL requires drop/recreate to change an MV definition; it cannot be safely altered in place.
2. **MV dependency safety:** A target-only object depending on a source-managed MV can block drop/recreate under `RESTRICT`; unsafe `CASCADE` is not used.
3. **MV-specific indexes:** Recreating a source-managed MV can remove target-specific MV indexes because the connector does not currently discover/migrate them.
4. **ENUM replacement:** ENUM replacement requires a type rebuild. A target-only dependency can cause safe failure/rollback rather than `CASCADE`.
5. **Target-only object preservation:** Reconciliation is source-scoped and does not blindly delete unrelated target objects.
6. **Same-name index/constraint differences:** Same-name objects with different definitions are not automatically replaced solely because their definitions differ.
7. **Dependency conflicts:** Dependent views, rules, or generated expressions can safely block structural changes.
8. **Incompatible type changes:** Unsupported PostgreSQL type changes may fail rather than force destructive conversion.
9. **FK-related operations:** FK dependencies can block `DELETE`/reset operations and require dependency-aware handling.
10. **Sequence state:** Owned sequences use existing `MAX(column) + 1` advancement after data load; standalone sequences restore source `last_value` / `is_called`.

### Test Matrix

| # | Scenario | Test Action | Actual Result | Status |
|---:|---|---|---|---|
| 1 | Modified table structure | Modify target table structure and run FULL migration | Source structure restored | ✅ PASS |
| 2 | Modified table data | Modify an existing target row | Source data restored | ✅ PASS |
| 3 | Target-only table | Create target-only table with data | Table and data preserved | ✅ PASS |
| 4 | Modified view | Modify `vw_ordersummary` on target | Source view definition restored | ✅ PASS |
| 5 | Modified function | Modify `fn_GetOrderTotal()` | Source function restored | ✅ PASS |
| 6 | Modified procedure | Modify `sp_updateproductstock` | Source procedure restored | ✅ PASS |
| 7 | Existing triggers | Repeat FULL migration with triggers present | No duplicate trigger/audit rows observed | ✅ PASS |
| 8 | Foreign keys | Validate existing FK relationships | FKs preserved; migration completed | ✅ PASS |
| 9 | Extra target index | Add target-only index | Extra index removed | ✅ PASS |
| 10 | Source-managed indexes/constraints | Validate after reconciliation | Preserved correctly | ✅ PASS |
| 11 | ENUM modification | Add target-only `target_modified` label | Extra label removed; source ENUM restored | ✅ PASS |
| 12 | Column comment modification | Modify `customers.fullname` comment | Target modification cleared/restored | ✅ PASS |
| 13 | Source-defined comments | Validate source comments | Comments applied correctly | ✅ PASS |
| 14 | Target-only materialized view | Create `manual_target_mv` | Preserved | ✅ PASS |
| 15 | Target-only procedure | Create `manual_target_procedure` | Preserved | ✅ PASS |
| 16 | Repeat FULL migration | Run multiple FULL migrations | No duplicate data/objects observed | ✅ PASS |
| 17 | Extra target partition | Add target-only partition | Extra partition removed | ✅ PASS |
| 18 | Source partition structure | Compare/reconcile source partitions | Source partition structure restored | ✅ PASS |
| 19 | Target-only view | Create target-only view | Preserved | ✅ PASS |
| 20 | Target-only function | Create target-only function | Preserved | ✅ PASS |
| 21 | Sequence reconciliation | Modify target sequence definition/state; test unconsumed sequence | Source definition and `(last_value, is_called)` restored | ✅ PASS |
| 22 | Source-managed materialized view | Modify target MV definition/data | MV recreated from source; result matched | ✅ PASS |

---

## Task 5 — Overwrite / Skip / Update Behavior

### Limitations
1. **Policy coverage:** Tests demonstrate FULL migration source-state reconciliation only; separate `overwrite`, `skip`, and `update` flags and every combination were not independently verified.
2. **Internal artifacts:** MySQL-specific backup/staging artifact checks are not applicable to this PostgreSQL test.

### Test Matrix

| # | Scenario | Test Action | Actual Result | Status |
|---:|---|---|---|---|
| 1 | Existing data conflict | Change target `customers.customer_id = 1`; run FULL migration | Source value restored | ✅ PASS |
| 2 | Extra column | Add `task5_extra_col` to target `customers` | Extra column removed | ✅ PASS |
| 3 | Extra index | Add `idx_task5_extra` | Extra index removed; source indexes remained | ✅ PASS |
| 4 | Target-only table | Create `task5_target_only` with custom data | Table preserved | ✅ PASS |
| 5 | Target-only data | Insert `MUST SURVIVE` | Data remained unchanged | ✅ PASS |

---

## Task 6 — Duplicate & Conflict Handling

### Limitations
1. **Internal artifacts:** MySQL-specific backup/staging artifact validation is not applicable to this PostgreSQL test.
2. **Conflict policy coverage:** Tests verify observed FULL migration reconciliation, not every possible overwrite/skip/update configuration.
3. **Constraint validation scope:** Duplicate checks were performed on `training.customers`, not every database table.
4. **Primary-key conflict evidence:** This scenario was previously verified; the latest run specifically reconfirmed unique-email conflict restoration and final duplicate checks.

### Test Matrix

| # | Scenario | Test Action | Expected Result | Actual Result | Status |
|---:|---|---|---|---|---|
| 1 | Existing target record conflicts with source PK | Reconcile existing target record through FULL migration | Source state restored without duplicates | Previously verified as reconciled to source state | ✅ PASS |
| 2 | Existing target record conflicts with source unique key | Change target `customerid = 2` email to `task6_conflict@example.com` | Source email restored; conflict eliminated | Restored to `bob@example.com`; no duplicate emails remained in checked data | ✅ PASS |
| 3 | Duplicate PK insertion | Insert another row with `customerid = 1` | PostgreSQL rejects duplicate PK | `duplicate key value violates unique constraint "pk_customers"` | ✅ PASS |
| 4 | Duplicate unique-key insertion | Insert another row with `email = alice@example.com` | PostgreSQL rejects duplicate unique key | `duplicate key value violates unique constraint "uq_customers_email"` | ✅ PASS |
| 5 | Final duplicate PK check | Group target records by `customerid` | No duplicate PKs | `(0 rows)` | ✅ PASS |
| 6 | Final duplicate email check | Group target records by email | No duplicate emails | `(0 rows)` | ✅ PASS |
| 7 | Final customer row count | Count `training.customers` | 5 rows | 5 rows | ✅ PASS |
| 8 | MySQL-specific artifact check | Consider `__dms_backup_*` / `__dms_stage_*` | PostgreSQL artifact behavior evaluated separately | Not performed; MySQL artifact check is N/A | ➖ N/A |

---

## Task 7 — Final Target State Validation

### Limitations
1. Validation passed for the configured PostgreSQL acceptance-test dataset and the objects covered by the suite.
2. This result does not establish correctness for every production database, workload, or edge case.
3. Manual test objects were removed to restore the expected acceptance baseline; the final comparison then passed.

### Test Matrix

| # | Scenario | Test Action | Expected Result | Actual Result | Status |
|---:|---|---|---|---|---|
| 1 | Source structural validation | Validate source structure and objects | All expected source checks pass | 55 checks passed, 0 failed | ✅ PASS |
| 2 | Target structural validation | Validate target structure and objects | Expected tables/objects present | Checks passed; 7 expected tables found | ✅ PASS |
| 3 | Schema comparison | Compare source and target schemas | Definitions match | Matched | ✅ PASS |
| 4 | Table comparison | Compare expected tables | No missing/unexpected tables in acceptance baseline | All 7 matched | ✅ PASS |
| 5 | Column validation | Compare columns and data types | Definitions match | All 7 tables passed | ✅ PASS |
| 6 | Primary-key validation | Compare PK definitions | PKs and columns match | 7 validated | ✅ PASS |
| 7 | Foreign-key validation | Compare FK relationships | Expected relationships match | 3 validated | ✅ PASS |
| 8 | Unique-constraint validation | Compare unique constraints | Constraints match | 2 validated | ✅ PASS |
| 9 | Check-constraint validation | Compare check constraints | Constraints exist and match | 7 validated | ✅ PASS |
| 10 | Index validation | Compare expected indexes | Expected indexes match | 4 validated | ✅ PASS |
| 11 | View validation | Compare and functionally test views | Views exist and work | Passed | ✅ PASS |
| 12 | Function/procedure validation | Compare routines and run functional checks | Routines exist and work | Passed | ✅ PASS |
| 13 | Trigger validation | Compare triggers and test behavior | Triggers work | Passed | ✅ PASS |
| 14 | Sequence validation | Compare sequence definitions and behavior | Sequences match and work | Passed | ✅ PASS |
| 15 | Partition validation | Compare partition structures | Structures match | Passed | ✅ PASS |
| 16 | User-defined type validation | Compare custom types | Types match | Passed | ✅ PASS |
| 17 | Security/RLS/grants validation | Check configured security objects | Expected settings match | Passed | ✅ PASS |
| 18 | Comments validation | Compare object comments | Comments match | Passed | ✅ PASS |
| 19 | Row-count validation | Compare all 7 tables | Source and target counts match | 33 rows on each side | ✅ PASS |
| 20 | Functional validation | Test views, routines, triggers, constraints, identity, sequences, partitions, ENUM and RLS | All supported checks pass | 11/11 passed | ✅ PASS |
| 21 | Overall acceptance validation | Run PostgreSQL E2E acceptance validator | Structural, comparison and functional checks pass | Overall result: PASS | ✅ PASS |

---

## Final Summary

| Check | Result |
|---|---|
| Latest FULL migration | 7 tables, 33 rows, 0 failed, 100% |
| Source structural validation | 55 passed, 0 failed |
| Functional validation | 11/11 passed |
| Source / target row counts | 33 / 33 |
| Overall PostgreSQL acceptance validation | **✅ PASS** |

**Commands used**

```powershell
python -m migration_platform --config config/postgresql_e2e_acceptance.yaml --mode full
python tests/e2e/postgresql/run_validation.py --config config/postgresql_e2e_acceptance.yaml
```
