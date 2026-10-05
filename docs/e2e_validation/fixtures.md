# Fixture Inventory

Inventory of all deterministic SQL fixture scripts used by the E2E acceptance
tests (Mode 2). Source: `tests/e2e/<engine>/fixtures/`.

## Overview

| Engine | Scripts | Tables | Source Rows | Partitions | Last Verified |
|--------|--------:|-------:|------------:|-----------:|:-------------|
| MSSQL | 16 | 7 | 28 | 4 | Live Mode 2 |
| MySQL | 14 | 7 | 28 | 4 | Live Mode 2 |
| PostgreSQL | 15 | 7 | 28 | 4 | Live Mode 2 |

All engines reproduce the same logical schema: 7 tables, 28 rows of source
data, identical business entities (Customers, Products, Orders, OrderDetails,
OrderAudit, PK_Name_Test, PartitionedOrders).

## MSSQL — 16 Scripts

| Script | Purpose | Key Objects |
|--------|---------|-------------|
| `00_reset.sql` | Drop/recreate training schema | DROP SCHEMA + CREATE SCHEMA |
| `01_create_schemas.sql` | Create schema, users, logins | training schema |
| `02_create_types.sql` | User-defined TYPE | CustomerCode (varchar aliased type) |
| `03_create_tables.sql` | Base tables + constraints | 7 tables, 7 PKs, 7 CKs |
| `04_create_foreign_keys.sql` | FK constraints | 3 FKs |
| `05_create_indexes.sql` | Non-PK indexes | 4 indexes (1 with INCLUDE) |
| `06_create_sequences.sql` | Sequences | Seq_OrderNumber (bigint) |
| `07_create_views.sql` | Views | vw_CustomerOrders, vw_ProductSales |
| `08_create_functions.sql` | Scalar + table-valued | fn_CalculateTax, fn_CustomerOrderStats |
| `09_create_procedures.sql` | Stored procedures | usp_GetCustomerOrders |
| `10_create_triggers.sql` | Triggers | trg_Orders_Insert (enabled), trg_OrderDetails_Audit (disabled) |
| `11_create_synonyms.sql` | Synonyms | syn_Orders → training.Orders, syn_OrderDetails → training.OrderDetails |
| `12_create_partitions.sql` | Partition function + scheme | PF_OrderDate, PS_OrderDate, 4 partitions |
| `13_create_security.sql` | Roles, users, memberships | Role_ReadOnly, Role_DataWriter, E2E_TestUser |
| `14_apply_extended_props.sql` | Extended properties | 12 MS_Description entries |
| `15_seed_data.sql` | Deterministic row data | 28 rows across 7 tables |

### MSSQL Row Counts (from `15_seed_data.sql`)

| Table | Rows | Notes |
|-------|-----:|-------|
| Customers | 5 | CustomerCode UDT applied |
| Products | 5 | |
| Orders | 5 | OrderNumber from Seq_OrderNumber |
| OrderDetails | 8 | LineTotal computed column |
| OrderAudit | 5 | Populated by trigger on Orders insert |
| PK_Name_Test | 2 | Custom PK constraint name |
| PartitionedOrders | 3 | Spans multiple partitions (2024, 2025-H1, 2025-H2) |
| **Total** | **28** | |

## MySQL — 14 Scripts

| Script | Purpose | Key Objects |
|--------|---------|-------------|
| `00_reset.sql` | Drop/recreate objects | DROP TABLE/VIEW/PROCEDURE/FUNCTION/TRIGGER |
| `01_create_schemas.sql` | Create schema | training |
| `03_create_tables.sql` | Base tables + constraints | 7 tables, 7 PKs, 7 CKs (inline) |
| `04_create_foreign_keys.sql` | FK constraints | 3 FKs |
| `05_create_indexes.sql` | Non-PK indexes | 4 indexes |
| `06_create_sequences.sql` | *(placeholder — MySQL uses AUTO_INCREMENT)* | No-op |
| `07_create_views.sql` | Views | vw_OrderSummary |
| `08_create_functions.sql` | Functions | fn_GetOrderTotal |
| `09_create_procedures.sql` | Procedures | sp_UpdateProductStock |
| `10_create_triggers.sql` | Triggers | tr_AuditOrder |
| `12_create_partitions.sql` | Native partitioning | RANGE on TO_DAYS(orderdate), 4 partitions |
| `13_create_security.sql` | Grants | GRANT SELECT/INSERT/etc to e2e_user |
| `14_create_comments.sql` | Table + column comments | 7 table comments, 2 column comments |
| `15_seed_data.sql` | Deterministic row data | 28 rows across 7 tables |

### MySQL Row Counts (from `15_seed_data.sql`)

| Table | Rows | Notes |
|-------|-----:|-------|
| customers | 5 | |
| products | 5 | |
| orders | 5 | ordernumber uses literal values 1000–1004 |
| orderdetails | 8 | linetotal generated column |
| orderaudit | 5 | Populated by tr_AuditOrder trigger |
| pk_name_test | 2 | Explicit PK constraint name |
| partitionedorders | 3 | Spans p2024, p2025_h1, p2025_h2 |
| **Total** | **28** | |

### MySQL Note: ENUM vs User Type

MySQL has no standalone type objects. The `orderstatus` ENUM is declared inline
on the `orders` table — this is the MySQL equivalent of PostgreSQL's user-defined
ENUM type. Script `02_create_types.sql` is intentionally absent for MySQL.

## PostgreSQL — 15 Scripts

| Script | Purpose | Key Objects |
|--------|---------|-------------|
| `00_reset.sql` | Drop/recreate schema | DROP SCHEMA CASCADE + CREATE SCHEMA |
| `01_create_schemas.sql` | Create schema, role, grants | training schema, migration_role |
| `02_create_types.sql` | User-defined ENUM type | OrderStatus (enum) |
| `03_create_tables.sql` | Base tables + constraints | 7 tables, 7 PKs, 7 CKs |
| `04_create_foreign_keys.sql` | FK constraints | 3 FKs |
| `05_create_indexes.sql` | Non-PK indexes | 4 indexes |
| `06_create_sequences.sql` | Sequences | seq_ordernumber (bigint) |
| `07_create_views.sql` | Views | vw_OrderSummary |
| `08_create_functions.sql` | Functions | fn_getordertotal, trg_auditorder |
| `09_create_procedures.sql` | Procedures | sp_updateproductstock |
| `10_create_triggers.sql` | Triggers | tr_auditorder |
| `12_create_partitions.sql` | Native partitioning | 4 partitions on RANGE (orderdate) |
| `13_create_security.sql` | Roles, grants | migration_role with 40 grants |
| `14_create_comments.sql` | Column/table comments | 8 comments |
| `15_seed_data.sql` | Deterministic row data | 28 rows across 7 tables |

### PostgreSQL Row Counts (from `15_seed_data.sql`)

| Table | Rows | Notes |
|-------|-----:|-------|
| customers | 5 | |
| products | 5 | |
| orders | 5 | OrderNumber from seq_ordernumber |
| orderdetails | 8 | linetotal generated column |
| orderaudit | 5 | Populated by tr_auditorder trigger |
| pk_name_test | 2 | Custom PK: pk_testprimarykey |
| partitionedorders | 3 | Spans 4 partitions |
| **Total** | **28** | |

### PostgreSQL Note: RLS

The `customers` table has Row-Level Security enabled with policy
`p_customers_city_isolation`. This is validated in the structural phase.

## Cross-Engine Differences

| Feature | MSSQL | MySQL | PostgreSQL |
|---------|-------|-------|------------|
| Identity columns | IDENTITY(1,1) | AUTO_INCREMENT | GENERATED BY DEFAULT AS IDENTITY |
| Sequences | Standone (Seq_OrderNumber) | None (AUTO_INCREMENT) | Standalone (seq_ordernumber) |
| Generated/computed columns | Computed (ModifiedAt, LineTotal) | AS(...) STORED | Generated always as |
| Partitioning | PF + PS (4 partitions) | Native RANGE (4) | Native RANGE (4) |
| Synonyms | 2 (syn_Orders, syn_OrderDetails) | None | None |
| RLS | None | None | 1 policy on customers |
| Roles | 2 + 1 user | None | 1 role (migration_role) |
| Grants | None | Native | 40 grants |
| Extended props | 12 (MS_Description) | None | None |
| Comments | None | 9 | 8 |
| ENUM type | N/A (OrderStatus is varchar) | Inline ENUM on orders | Standalone enum type |
| Triggers | 2 (1 enabled, 1 disabled) | 1 (enabled) | 1 (enabled) |

## Fixture Execution Order

```
00_reset → 01_create_schemas → 02_create_types → 03_create_tables →
04_create_foreign_keys → 05_create_indexes → 06_create_sequences →
07_create_views → 08_create_functions → 09_create_procedures →
10_create_triggers → 11_create_synonyms (MSSQL only) →
12_create_partitions → 13_create_security →
14_create_comments / 14_apply_extended_props → 15_seed_data
```

MySQL and PostgreSQL omit `11_create_synonyms.sql` (no synonym support).
MySQL also omits `02_create_types.sql` (ENUM inline, no standalone types).
PostgreSQL renames `14_create_comments.sql` to `14_apply_extended_props.sql`
on MSSQL (different metadata mechanism).

## Determinism

All fixture data is deterministic — no `RANDOM()`, no `NEWID()`, no
`NOW()` for seeded values. Identity/sequence/auto-increment columns auto-generate
their PK values during insert. The `OrderNumber` sequence starts at 1000.

## Verification

Each fixture's expected state is encoded in:
- `tests/e2e/mssql/validation/expected.py` (MSSQL)
- `tests/e2e/mysql/validation/expected.py` (MySQL)
- `tests/e2e/postgresql/validation/expected.py` (PostgreSQL)

If you change a script, update the corresponding `expected.py` and re-run
validation.