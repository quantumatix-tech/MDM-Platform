# Existing Target Handling — Final Validation Report

**Project:** Unified DMS / MDM Migration Platform  
**Scope:** Existing Target Handling  
**Migration Mode:** FULL  
**Target Engine:** Microsoft SQL Server (MSSQL)  
**Validation Status:** **PASS WITH LIMITATIONS**

## 1. Objective

Validate FULL migration behavior when the MSSQL target already contains tables, data, schema differences, database objects, constraints, and target-only objects.

## 2. Task 1 — Existing Target Handling

| # | Test Case | Result | Status |
|---|---|---|---|
| 1 | Existing target database | Existing target database reused; migration completed | PASS |
| 2 | Modified target row | `Customers.CustomerID = 2` restored to the source value | PASS |
| 3 | Extra target row | `CustomerID = 999` removed; `Customers` returned to 5 rows | PASS |
| 4 | Missing target row | `CustomerID = 5` (`Eva Green`) restored | PASS |
| 5 | Repeat FULL migration | `Customers = 5` and `OrderAudit = 5` after rerun; no dedicated duplicate-key query was run during this final check | PASS* |
| 6 | Target-only table preservation | `training.TargetOnly_Test` and its row remained intact during the test | PASS |
| 7 | Extra target column | `Customers.ExtraTargetColumn` removed | PASS |
| 8 | Missing source-managed target table | `Products` recreated, 5 rows loaded, and `FK_OrderDetails_Products` recreated | PASS |
| 9 | Source-managed row counts | All 7 managed tables matched; total 33 rows | PASS |
| 10 | Acceptance validation | Source structural checks: 72 passed; comparison passed; functional validation: 9/9 passed | PASS |

**Task 1 Result: PASS**

**Limitations**
1. Repeat migration row counts were checked, but a dedicated duplicate-key query was not run during the final check.
2. `training.TargetOnly_Test` was intentionally deleted after its preservation test to allow exact source-target schema validation; it is not present in the final target.
3. Acceptance evidence: `tests/e2e/mssql/reports/acceptance_validation_MigrationE2E_MSSQL_Target.json`.

## 3. Task 2 — Existing Schema Handling

| # | Scenario | Result | Status |
|---|---|---|---|
| 1 | Existing schema | Existing `training` schema reused successfully | PASS |
| 2 | Empty existing schema | Required tables/objects created and data migrated | PASS |
| 3 | Target-only schema/object | Target-only schema/object preserved during the test | PASS |
| 4 | Schema definition conflict | `Customers.FullName` target definition was verified as `NVARCHAR(150)`, with `max_length = 200` bytes and `is_nullable = 0` | PASS |
| 5 | Repeat FULL migration | Latest recorded repeat run processed 7 tables and 33 rows with 0 errors | PASS |

**Task 2 Result: PASS WITH LIMITATIONS**

**Limitations**
1. Mock-based failure-safety regression passed, but live MSSQL failure-injection testing was pending at the time of this task's validation. A later live failure-injection run is recorded under Task 7.
2. Schema-conflict coverage was limited to the tested `Customers.FullName` conflict; other conflict combinations were not comprehensively tested.
3. Performance testing covered 7 tables and 33 rows; large-scale migration performance was not tested.

## 4. Task 3 — Existing Table & Data Handling

| # | Test Case | Result | Status |
|---|---|---|---|
| 1 | Existing target table with same data | FULL migration completed for 7 tables and 33 rows with 0 errors | PASS |
| 2 | Existing target table with extra rows | Extra row in `PK_Name_Test` remained after one run; subsequent FULL run returned row count 2. Result was inconsistent | NEEDS RECHECK |
| 3 | Modified existing row | `Customers.CustomerID = 1` was restored to `Alice Johnson` | PASS |
| 4 | Extra populated column | Extra column added to `Customers` was removed | PASS |
| 5 | Empty extra column | Empty extra column was removed | PASS |
| 6 | Target missing a source column | `Customers.Phone` was restored | PASS |
| 7 | Different target column definition | `Phone` changed from target `VARCHAR(80) NULL` back to source `VARCHAR(20) NULL` | PASS |
| 8 | Repeated FULL migration | Run `c4d42501a90e4d37ac5da0f89ca1eec9` completed; expected row counts remained unchanged | PASS |
| 9 | Existing target table with zero rows | Two expected rows restored to `PK_Name_Test` | PASS |
| 10 | Source-target row-count comparison | Seven tables: Customers 5, Products 5, Orders 5, OrderDetails 8, OrderAudit 5, PK_Name_Test 2, PartitionedOrders 3; total 33 | PASS — counts only |
| 11 | Exact `Customers` data comparison | All five displayed source and target records matched, including timestamps | PASS — Customers only |
| 12 | Target-only table preservation | Target-only sentinel table and its test row remained intact | PASS |

**Task 3 Result: PASS WITH ONE ITEM NEEDING RECHECK**

**Limitations**
1. Extra-row removal in Scenario 2 was inconsistent; the root cause was not confirmed.
2. Exact row-by-row comparison evidence is limited to `Customers`. Other tables were validated using row counts.
3. `Customers.Phone` has a different physical ordinal: position 5 in source and position 9 in target. The displayed data type and nullability match, but strict schema equivalence is not established.
4. A FULL migration report with zero errors does not, by itself, prove exact data equality or complete schema equality.
5. Results are limited to the configured MSSQL E2E databases and tested fixtures; they do not guarantee behavior for other schemas, data volumes, or production workloads.

## 5. Task 4 — Existing Object Handling

| # | Scenario | Result | Status |
|---|---|---|---|
| 1 | Modified existing view | Source view definition restored | PASS |
| 2 | Modified existing function | Source function definition restored | PASS |
| 3 | Modified existing procedure | Source procedure definition restored | PASS |
| 4 | Trigger handling | Tested trigger enabled/disabled states preserved | PASS |
| 5 | Foreign-key reconciliation | 3 foreign keys were enabled and trusted | PASS |
| 6 | Extra target index | Source-absent extra index removed | PASS |
| 7 | Indexes and constraints | Source-managed index restored; extra index removal verified | PASS |
| 8 | Column comments | Comments restored | PASS |
| 9 | Source-defined comments | Comments applied successfully | PASS |
| 10 | Target-only indexed view | View and clustered index preserved | PASS |
| 11 | Target-only procedure | Procedure preserved | PASS |
| 12 | Partition structure | 6 source and 6 target destinations matched | PASS |
| 13 | Source-managed partition structure | Live comparison found no mismatch | PASS |
| 14 | Target-only view | View preserved | PASS |
| 15 | Target-only function | Function preserved | PASS |
| 16 | Sequence reconciliation | Target settings reconciled; next value `1005` verified | PASS |
| 17 | Source-managed indexed view | `IsSchemaBound = 1`; unique clustered index present | PASS |
| 18 | Internal backup-table cleanup | No applicable internal artifact creation path was identified | N/A |
| 19 | Internal staging-table cleanup | No applicable internal artifact creation path was identified | N/A |
| 20 | Same name, different object type | Target procedure conflict replaced by source VIEW; normalized definition matched | PASS |
| 21 | Dependency/creation order | Base table created before indexed view | PASS |
| 22 | Permissions and grants | 237 permission entries; no missing/extra entries; memberships matched | PASS |
| 23 | Repeat FULL migration | Two-run snapshots showed no target catalog changes | PASS |
| 24 | Final inventory and definitions | Acceptance validator passed; inventory differences reported separately | PASS |

**Task 4 Result: PASS WITH OPEN DEPENDENCY AND VALIDATION LIMITATIONS**

**Limitations**
1. Sequence reconciliation was updated to preserve a higher target current value. The validator was also updated to report sequence definition metadata separately from runtime state; these are distinct checks.
2. A same-name VIEW-versus-stored-procedure conflict was identified and tested, but replacement still requires dependency review before execution in environments with dependent objects. External application references cannot be fully verified by database-only checks.
3. Indexed-view fixture and validation were corrected to use the actual `ID` column.
4. Partition reconciliation and validation were improved; the live comparison reported 6 matching source and target destinations.
5. Final inventory comparison was adjusted to distinguish object definitions, target-only objects, and object-type conflicts.
6. Internal backup/staging cleanup is marked **N/A** because no applicable creation path was identified. Actual cleanup behavior was not proven by a live test.

## 6. Task 5 — FULL Migration Reconciliation Behavior

The cases below refer to scenarios already tested in Tasks 1, 3, and 4; they are cross-references, not additional independent test runs.

| # | Scenario | Result | Status |
|---|---|---|---|
| 1 | Existing data conflict | Target modification was restored to the source value (`CustomerID = 2` / `Alice Johnson`) | PASS — Task 1/3 |
| 2 | Extra column on a source-managed table | Extra target column removed | PASS — Task 3 |
| 3 | Extra index on a source-managed table | Extra target index removed | PASS — Task 4 |
| 4 | Target-only table preservation | Target-only table preserved | PASS — Task 1/3 |
| 5 | Target-only table data preservation | Sentinel data remained unchanged | PASS — Task 1/3 |

**Task 5 Result: PASS FOR THE TESTED RECONCILIATION CASES**

## 7. Task 6 — Duplicate & Conflict Handling

| # | Scenario | Result | Status |
|---|---|---|---|
| 1 | Existing target record conflicts with source primary-key record | Target row restored to source state during FULL migration | PASS — Task 1/3 |
| 2 | Existing target record conflicts with source unique key | Duplicate `alice@example.com` assignment was rejected by `UQ_Customers_Email`; target data remained unchanged | PASS |
| 3 | Duplicate primary-key insertion | Duplicate `CustomerID = 1` rejected by `PK_Customers` | PASS |
| 4 | Duplicate unique-key insertion | Duplicate `alice@example.com` rejected by `UQ_Customers_Email` | PASS |
| 5 | Final duplicate primary-key check | Zero duplicate `CustomerID` values | PASS |
| 6 | Final duplicate unique-key check | Zero duplicate Email values | PASS |
| 7 | Final `Customers` row count | Source and target both contained 5 rows | PASS |

**Task 6 Result: PASS FOR THE TESTED DUPLICATE AND CONFLICT CASES**

## 8. Task 7 — Final Target State Validation

| # | Scenario | Result | Status |
|---|---|---|---|
| 1 | Source structural validation | 72 checks passed | PASS |
| 2 | Target structural validation | 7 tables validated | PASS |
| 3 | Schema comparison | Comparison passed | PASS |
| 4 | Managed table comparison | All 7 tables matched | PASS |
| 5 | Column validation | `Customers` physical column order differs | PARTIAL |
| 6 | Primary keys | 7 tables passed | PASS |
| 7 | Foreign keys | 3 enabled/trusted foreign keys passed | PASS |
| 8 | Unique constraints | Source and target validation passed | PASS |
| 9 | Check constraints | All 7 passed | PASS |
| 10 | Indexes | Validation passed | PASS |
| 11 | Views | Validation passed | PASS |
| 12 | Functions and stored procedures | Validation passed | PASS |
| 13 | Triggers | Validation passed | PASS |
| 14 | Sequence metadata | Start value `1000`, increment `1`, current value `1004` in the recorded validation | PASS |
| 15 | Partition validation | 6 source and 6 target destinations matched | PASS |
| 16 | User-defined types | Validation passed | PASS |
| 17 | Security and permissions | Validation passed | PASS |
| 18 | Comments and metadata | Validation passed | PASS |
| 19 | Row counts | Earlier check: 7 tables / 33 rows; latest failure-injection run: 10 tables / 37 source rows | PASS |
| 20 | Exact data comparison | Latest manual check showed matching timestamp values | PASS —  |
| 21 | `datetime2(7)` precision | All 5 `Orders` rows matched at scale 7 | PASS |
| 22 | Repeat migration and duplicate checks | Recorded repeat-migration and duplicate-key checks passed; see Task 1 limitation for the final repeat check | PASS |
| 23 | Target-only object preservation | Dedicated preservation tests passed | PASS |
| 24 | Error-isolation unit tests | 12 tests passed | PASS |
| 25 | Live failure injection | Failed table had 0 rows; subsequent good table had 1 expected row | PASS |
| 26 | Functional acceptance | 9/9 scenarios passed | PASS |

**Task 7 Result: PASS WITH DOCUMENTED LIMITATIONS**

**Limitations**
1. `Customers.Phone` is at ordinal 5 in source and ordinal 9 in target; four other columns are shifted. The current migration contract validates column names and definitions, not physical order.
2. Pytest emitted a Windows access-denied warning while creating `.pytest_cache`; the tests passed despite the warning.
3. Failure-injection tables and trigger remain in the databases unless explicitly cleaned up.
4. The failure-injection run intentionally reports `PARTIAL_SUCCESS`; it must not be described as a complete, zero-error migration.
5. Target-only object preservation passed in dedicated tests, but some historical test-only tables were intentionally deleted during cleanup.
6. The latest live migration used a test trigger to cause one expected failure. The migration continued to the next object, but the overall run was not zero-error.

## 9. Final Target State

### Validated results

- 7 source-managed tables passed the recorded structural/table comparison.
- Earlier row-count comparison covered 7 tables and 33 rows.
- Latest failure-injection run covered 10 tables and 37 source rows: 36 migrated and 1 failed.
- All 5 `Orders` rows matched at `datetime2(7)` scale in the latest manual precision check.
- 3 foreign keys were enabled and trusted.
- 6 source and target partition destinations matched.
- Functional acceptance: 9/9 passed.
- Dedicated target-only object preservation tests passed.

### Acceptance evidence

`tests/e2e/mssql/reports/acceptance_validation_MigrationE2E_MSSQL_Target.json`

## 10. Overall Result

| Task | Area | Result |
|---|---|---|
| Task 1 | Existing Target Handling | PASS — repeat-check limitation noted |
| Task 2 | Existing Schema Handling | PASS — limited conflict/performance coverage |
| Task 3 | Existing Table & Data Handling | NEEDS RECHECK for extra-row scenario |
| Task 4 | Existing Object Handling | PASS — dependency and N/A cleanup limitations noted |
| Task 5 | FULL Migration Reconciliation | PASS for tested cases |
| Task 6 | Duplicate & Conflict Handling | PASS for tested cases |
| Task 7 | Final Target State Validation | PASS WITH LIMITATIONS; live run was `PARTIAL_SUCCESS` |

**FINAL STATUS: VALIDATED FOR THE TESTED SCENARIOS, WITH OPEN LIMITATIONS**

The recorded tests support the results above. They do not establish complete schema or exact data equality across every table, and the live failure-injection run is intentionally a partial-success run rather than a zero-error migration.
