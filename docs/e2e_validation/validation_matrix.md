# E2E Validation Matrix

Maps every validation phase across the three engines' Mode 2 acceptance
workflows. Source: `tests/e2e/<engine>/validation/`.

## Validation Pipeline

The Mode 2 acceptance workflow runs three validator types in sequence:

```
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ Source Validator │  │ Target Validator │  │  Comparator      │
│ (pre-migration)  │  │ (pre-migration)  │  │ (post-migration) │
│ Checks source    │  │ Checks target is │  │ Compares source  │
│ matches fixture  │  │ clean & protected│  │ vs target        │
└─────────────────┘  └──────────────────┘  └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ Functional       │
                    │ Validator        │
                    │ Runtime queries  │
                    └──────────────────┘
```

## Phase Coverage Matrix

Key: **S** = Source validator | **T** = Target pre-validator | **C** = Comparator | **F** = Functional validator

### MSSQL (22 source phases, 17 comparison categories, 9 functional phases)

| Phase | S | T | C | F | Object Validated |
|-------|:--:|:--:|:--:|:---:|-----------------|
| database | ✓ | | | | Database existence |
| schemas | ✓ | | ✓ | | `training` schema |
| tables | ✓ | | ✓ | | 7 base tables |
| columns | ✓ | | ✓ | | All table columns |
| primary_keys | ✓ | | ✓ | | 7 PK constraints |
| foreign_keys | ✓ | | ✓ | | 3 FK constraints |
| unique_constraints | ✓ | | ✓ | | 2 unique constraints |
| check_constraints | ✓ | | | | 7 check constraints |
| default_constraints | ✓ | | | | 13 default constraints |
| indexes | ✓ | | ✓ | | 4 non-PK indexes |
| views | ✓ | | ✓ | | 2 views |
| functions | ✓ | | ✓ | | 2 functions |
| procedures | ✓ | | ✓ | | 1 procedure |
| triggers | ✓ | | ✓ | | 2 triggers |
| sequences | ✓ | | ✓ | | 1 sequence |
| synonyms | ✓ | | ✓ | | 2 synonyms |
| user_defined_types | ✓ | | ✓ | | 1 UDT (CustomerCode) |
| partitioning | ✓ | | ✓ | | Partition function + scheme + 4 partitions |
| security | ✓ | | ✓ | | 2 roles, 1 user, 1 membership |
| extended_properties | ✓ | | ✓ | | 12 MS_Description entries |
| row_counts | ✓ | | ✓ | | 28 source rows across 7 tables |
| trigger_data | ✓ | | | | OrderAudit populated by trigger |
| target_safety | | ✓ | | | DB exists & not protected |
| target_connectivity | | ✓ | | | Target connection works |
| clean_state | | ✓ | | | Target has no data |
| functional_view | | | | ✓ | vw_CustomerOrders returns data |
| functional_scalar_function | | | | ✓ | fn_CalculateTax returns result |
| functional_inline_tvf | | | | ✓ | fn_CustomerOrderStats returns rows |
| functional_stored_procedure | | | | ✓ | usp_GetCustomerOrders executes |
| functional_trigger_enabled | | | | ✓ | trg_Orders_Insert fires on insert |
| functional_trigger_disabled | | | | ✓ | trg_OrderDetails_Audit is disabled |
| functional_synonym | | | | ✓ | syn_Orders resolves to Orders |
| functional_partitioned_table | | | | ✓ | PartitionedOrders routing works |
| functional_sequence | | | | ✓ | Seq_OrderNumber advances correctly |

### MySQL (19 source phases, 14 comparison categories, 9 functional phases)

| Phase | S | T | C | F | Object Validated |
|-------|:--:|:--:|:--:|:---:|
| database | ✓ | | | | Database existence |
| schemas | ✓ | | ✓ | | `training` schema |
| tables | ✓ | | ✓ | | 7 base tables |
| columns | ✓ | | ✓ | | All table columns |
| primary_keys | ✓ | | ✓ | | 7 PK constraints |
| foreign_keys | ✓ | | ✓ | | 3 FK constraints |
| unique_constraints | ✓ | | ✓ | | 2 unique constraints |
| check_constraints | ✓ | | ✓ | | 7 check constraints |
| indexes | ✓ | | ✓ | | 4 non-PK indexes |
| views | ✓ | | ✓ | | 1 view |
| routines | ✓ | | ✓ | | 1 function + 1 procedure |
| triggers | ✓ | | ✓ | | 1 trigger |
| partitions | ✓ | | ✓ | | 4 partitions (RANGE) |
| enum_types | ✓ | | | | 1 ENUM type |
| comments | ✓ | | ✓ | | 9 table/column comments |
| row_counts | ✓ | | ✓ | | 28 source rows across 7 tables |
| trigger_data | ✓ | | | | OrderAudit populated by trigger |
| target_safety | | ✓ | | | DB exists & not protected |
| target_connectivity | | ✓ | | | Target connection works |
| clean_state | | ✓ | | | Target has no data |
| functional_view | | | | ✓ | vw_OrderSummary returns data |
| functional_scalar_function | | | | ✓ | fn_GetOrderTotal returns result |
| functional_procedure | | | | ✓ | sp_UpdateProductStock executes |
| functional_trigger | | | | ✓ | tr_AuditOrder fires on insert |
| functional_foreign_key | | | | ✓ | FK enforces referential integrity |
| functional_check_constraint | | | | ✓ | CHECK rejects invalid data |
| functional_auto_increment | | | | ✓ | AUTO_INCREMENT advances correctly |
| functional_partition_routing | | | | ✓ | PartitionedOrders routing works |
| functional_enum | | | | ✓ | ENUM constraint enforces values |

### PostgreSQL (22 source phases, 20 comparison categories, 11 functional phases)

| Phase | S | T | C | F | Object Validated |
|-------|:--:|:--:|:--:|:---:|
| database | ✓ | | | | Database existence |
| schemas | ✓ | | ✓ | | `training` schema |
| tables | ✓ | | ✓ | | 7 base tables |
| columns | ✓ | | ✓ | | All table columns |
| primary_keys | ✓ | | ✓ | | 7 PK constraints |
| foreign_keys | ✓ | | ✓ | | 3 FK constraints |
| unique_constraints | ✓ | | ✓ | | 2 unique constraints |
| check_constraints | ✓ | | ✓ | | 7 check constraints |
| indexes | ✓ | | ✓ | | 4 non-PK indexes |
| views | ✓ | | ✓ | | 1 view |
| functions | ✓ | | ✓ | | 2 functions |
| procedures | ✓ | | ✓ | | 1 procedure |
| triggers | ✓ | | ✓ | | 1 trigger |
| sequences | ✓ | | ✓ | | 1 sequence |
| partitions | ✓ | | ✓ | | 4 partitions (RANGE) |
| user_defined_types | ✓ | | ✓ | | 1 enum type (OrderStatus) |
| security | ✓ | | ✓ | | 1 role (migration_role) |
| rls_policies | ✓ | | ✓ | | 1 RLS policy |
| grants | ✓ | | ✓ | | 40 grants |
| comments | ✓ | | ✓ | | 8 comments |
| row_counts | ✓ | | ✓ | | 28 source rows across 7 tables |
| trigger_data | ✓ | | | | OrderAudit populated by trigger |
| target_safety | | ✓ | | | DB exists & not protected |
| target_connectivity | | ✓ | | | Target connection works |
| clean_state | | ✓ | | | Target has no data |
| functional_view | | | | ✓ | vw_OrderSummary returns data |
| functional_scalar_function | | | | ✓ | fn_getordertotal returns result |
| functional_procedure | | | | ✓ | sp_updateproductstock executes |
| functional_trigger | | | | ✓ | tr_auditorder fires on insert |
| functional_foreign_key | | | | ✓ | FK enforces referential integrity |
| functional_check_constraint | | | | ✓ | CHECK rejects invalid data |
| functional_identity | | | | ✓ | GENERATED BY DEFAULT AS IDENTITY |
| functional_sequence | | | | ✓ | seq_ordernumber advances correctly |
| functional_partition_routing | | | | ✓ | PartitionedOrders routing works |
| functional_enum | | | | ✓ | OrderStatus enum enforces values |
| functional_rls | | | | ✓ | Row-level security policy enforces |

## Source Validation Check Counts

| Engine | Total Checks | Status |
|--------|------------:|--------|
| MSSQL | 72 | All PASS |
| MySQL | — | PASS |
| PostgreSQL | — | PASS |

## Cross-Engine Phase Differences

| Phase | MSSQL | MySQL | PostgreSQL |
|-------|-------|-------|------------|
| default_constraints | ✓ (13) | — | — |
| extended_properties | ✓ (12) | — | — |
| enum_types | — | ✓ (1) | ✓ (1, as user_defined_types) |
| comments | — | ✓ (9) | ✓ (8) |
| roles/users | ✓ (security) | — | ✓ (security + grants) |
| RLS policies | — | — | ✓ |
| synonyms | ✓ | — | — |
| trigger_data | ✓ | ✓ | ✓ |

## Reports

Each run writes JSON reports:

| Report | Location | Contents |
|--------|----------|----------|
| Setup report | `tests/e2e/<engine>/reports/acceptance_setup_<db>.json` | DB state, protected list, schema verification |
| Validation report | `tests/e2e/<engine>/reports/acceptance_validation_<db>.json` | All phases, check results, summary counts |

## Mode 1 E2E Results (Automated Regression)

| Engine | Checks | Status |
|--------|-------:|--------|
| MSSQL | 212/212 | PASS |
| PostgreSQL | 179/179 | PASS |
| MySQL | 151/151 | PASS |
| **Aggregate** | **542/542** | **PASS** |

## Mode 2 Live Results (Acceptance)

| Engine | Setup | Migration | Validation | Source Rows | Target Rows |
|--------|-------|-----------|------------|------------:|------------:|
| MSSQL | 7 tables, 28 rows | 33 events, 0 failures | 72 checks PASS, 7/7 row counts match | 28 | 28 |
| MySQL | 7 tables, 28 rows | PASS | All checks PASS, 7/7 row counts match | 28 | 28 |
| PostgreSQL | 7 tables, 28 rows | PASS | All checks PASS, 7/7 row counts match | 28 | 28 |

**Note on MSSQL row counts:** The migration reports 33 migration rows/events
while source and target both hold 28 logical data rows. The difference (5)
comes from the `trg_Orders_Insert` trigger on `Orders`, which fires during
migration and creates 5 `OrderAudit` records on the target. Both source and
target end with the same row counts (28), validated by the `row_counts`
comparison phase.