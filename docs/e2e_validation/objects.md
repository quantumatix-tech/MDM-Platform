# E2E Object Test Matrix

Cross-engine comparison of every database object in the E2E fixtures.
Source of truth: `tests/e2e/<engine>/validation/expected.py`.

## Summary

| Object Type | MSSQL | MySQL | PostgreSQL |
|-------------|------:|------:|-----------:|
| **Database** | 1 (MigrationE2E_MSSQL_Source) | 1 (MigrationE2E_MySQL_Source) | 1 (MigrationE2E_PostgreSQL_Source) |
| **Schema** | 1 (training) | 1 (training) | 1 (training) |
| **Tables** | 7 | 7 | 7 |
| **PK Constraints** | 7 | 7 | 7 |
| **FK Constraints** | 3 | 3 | 3 |
| **Unique Constraints** | 2 | 2 | 2 |
| **Check Constraints** | 7 | 7 | 7 |
| **Default Constraints** | 13 | 0 | 0 |
| **Indexes (non-PK)** | 4 | 4 | 4 |
| **Views** | 2 | 1 | 1 |
| **Functions** | 2 | 1 | 2 |
| **Procedures** | 1 | 1 | 1 |
| **Triggers** | 2 | 1 | 1 |
| **Sequences** | 1 | 0 | 1 |
| **Synonyms** | 2 | 0 | 0 |
| **User Types** | 1 | 1 (ENUM) | 1 (ENUM) |
| **Partitions** | 1 PF + 1 PS + 4 parts | 4 partitions | 4 partitions |
| **Extended Properties** | 12 | 0 | 0 |
| **Comments** | 0 | 9 | 8 |
| **RLS Policies** | 0 | 0 | 1 |
| **Roles** | 2 | 0 | 1 |
| **Users** | 1 | 0 | 0 |
| **Grants** | 0 | 0 | 40 |
| **Roles/Users Grants** | 1 membership | 0 | 0 |
| **Source Rows** | 28 | 28 | 28 |

## Tables (all engines: 7 tables, 28 source rows)

| Table | MSSQL Rows | MySQL Rows | PG Rows | Columns | Identity PK |
|-------|----------:|----------:|-------:|--------:|:-----------:|
| Customers | 5 | 5 | 5 | 9 | Yes |
| Products | 5 | 5 | 5 | 8 | Yes |
| Orders | 5 | 5 | 5 | 7 | Yes |
| OrderDetails | 8 | 8 | 8 | 6 | Yes |
| OrderAudit | 5 | 5 | 5 | 4 | Yes |
| PK_Name_Test | 2 | 2 | 2 | 2 | Yes |
| PartitionedOrders | 3 | 3 | 3 | 4 | Yes |
| **Total** | **28** | **28** | **28** | | |

## Constraints

### Primary Keys (7 per engine)

| Engine | PK Name | Table | Columns |
|--------|---------|-------|---------|
| MSSQL | PK_Customers | Customers | CustomerID |
| MSSQL | PK_Products | Products | ProductID |
| MSSQL | PK_Orders | Orders | OrderID |
| MSSQL | PK_OrderDetails | OrderDetails | DetailID |
| MSSQL | PK_OrderAudit | OrderAudit | AuditID |
| MSSQL | PK_TestPrimaryKey | PK_Name_Test | TestID |
| MSSQL | PK_PartitionedOrders | PartitionedOrders | PartitionID, OrderDate |
| MySQL | (auto) | customers | customerid |
| MySQL | (auto) | products | productid |
| MySQL | (auto) | orders | orderid |
| MySQL | (auto) | orderdetails | detailid |
| MySQL | (auto) | orderaudit | auditid |
| MySQL | (auto) | partitionedorders | partitionid,orderdate |
| MySQL | (auto) | pk_name_test | testid |
| PostgreSQL | pk_customers | customers | customerid |
| PostgreSQL | pk_products | products | productid |
| PostgreSQL | pk_orders | orders | orderid |
| PostgreSQL | pk_orderdetails | orderdetails | detailid |
| PostgreSQL | pk_orderaudit | orderaudit | auditid |
| PostgreSQL | pk_partitionedorders | partitionedorders | partitionid,orderdate |
| PostgreSQL | pk_testprimarykey | pk_name_test | testid |

### Foreign Keys (3 per engine)

| Engine | Constraint | Columns → Ref | Table |
|--------|-----------|---------------|-------|
| MSSQL | FK_Orders_Customers | CustomerID → Customers.CustomerID | Orders |
| MSSQL | FK_OrderDetails_Orders | OrderID → Orders.OrderID | OrderDetails |
| MSSQL | FK_OrderDetails_Products | ProductID → Products.ProductID | OrderDetails |
| MySQL | fk_orders_customers | customerid → customers.customerid | orders |
| MySQL | fk_orderdetails_orders | orderid → orders.orderid | orderdetails |
| MySQL | fk_orderdetails_products | productid → products.productid | orderdetails |
| PostgreSQL | fk_orders_customers | customerid → customers.customerid | orders |
| PostgreSQL | fk_orderdetails_orders | orderid → orders.orderid | orderdetails |
| PostgreSQL | fk_orderdetails_products | productid → products.productid | orderdetails |

### Unique Constraints (2 per engine)

| Engine | Constraint | Table | Columns |
|--------|-----------|-------|---------|
| MSSQL | UQ_Customers_Email | Customers | Email |
| MSSQL | UQ_Products_SKU | Products | SKU |
| MySQL | uq_customers_email | customers | email |
| MySQL | uq_products_sku | products | sku |
| PostgreSQL | uq_customers_email | customers | email |
| PostgreSQL | uq_products_sku | products | sku |

### Check Constraints

| Engine | Constraint | Table |
|--------|-----------|-------|
| MSSQL | CK_Customers_Email | Customers |
| MSSQL | CK_Customers_Name | Customers |
| MSSQL | CK_Products_Price | Products |
| MSSQL | CK_Products_Stock | Products |
| MSSQL | CK_Orders_Amount | Orders |
| MSSQL | CK_OrderDetails_Quantity | OrderDetails |
| MSSQL | CK_OrderDetails_UnitPrice | OrderDetails |
| MySQL | ck_customers_email | customers |
| MySQL | ck_customers_name | customers |
| MySQL | ck_products_price | products |
| MySQL | ck_products_stock | products |
| MySQL | ck_orders_amount | orders |
| MySQL | ck_orderdetails_quantity | orderdetails |
| MySQL | ck_orderdetails_unitprice | orderdetails |
| PostgreSQL | ck_customers_email | customers |
| PostgreSQL | ck_customers_name | customers |
| PostgreSQL | ck_products_price | products |
| PostgreSQL | ck_products_stock | products |
| PostgreSQL | ck_orders_amount | orders |
| PostgreSQL | ck_orderdetails_quantity | orderdetails |
| PostgreSQL | ck_orderdetails_unitprice | orderdetails |

MSSQL also has 13 **Default Constraints** (named, e.g. `DF_Customers_CreatedAt`,
`DF_Products_StockQty`). MySQL and PostgreSQL define defaults inline (not as
named constraints), so they are validated via column-level checks.

## Indexes (4 non-PK per engine)

| Engine | Index | Table | Columns | Included |
|--------|-------|-------|---------|----------|
| MSSQL | IX_Customers_City | Customers | City | — |
| MSSQL | IX_Products_Category | Products | Category | — |
| MSSQL | IX_Orders_CustomerID | Orders | CustomerID | — |
| MSSQL | IX_OrderDetails_ProductID | OrderDetails | ProductID | Quantity, UnitPrice |
| MySQL | idx_orders_customerid | orders | customerid | — |
| MySQL | idx_orders_orderdate | orders | orderdate | — |
| MySQL | idx_orderdetails_orderid | orderdetails | orderid | — |
| MySQL | idx_orderdetails_productid | orderdetails | productid | — |
| PostgreSQL | ix_orders_customerid | orders | customerid | — |
| PostgreSQL | ix_orders_orderdate | orders | orderdate | — |
| PostgreSQL | ix_orderdetails_orderid | orderdetails | orderid | — |
| PostgreSQL | ix_orderdetails_productid | orderdetails | productid | — |

## Views

| Engine | View | Base Tables |
|--------|------|-------------|
| MSSQL | vw_CustomerOrders | Orders, Customers |
| MSSQL | vw_ProductSales | OrderDetails, Products |
| MySQL | vw_OrderSummary | Orders, OrderDetails |
| PostgreSQL | vw_OrderSummary | Orders, OrderDetails |

## Functions

| Engine | Function | Kind |
|--------|----------|------|
| MSSQL | fn_CalculateTax | FN (scalar) |
| MSSQL | fn_CustomerOrderStats | IF (inline table-valued) |
| MySQL | fn_GetOrderTotal | Function |
| PostgreSQL | fn_getordertotal | Function |
| PostgreSQL | trg_auditorder | Function (trigger body) |

## Procedures

| Engine | Procedure |
|--------|-----------|
| MSSQL | usp_GetCustomerOrders |
| MySQL | sp_UpdateProductStock |
| PostgreSQL | sp_updateproductstock |

## Triggers

| Engine | Trigger | Table | Enabled? |
|--------|---------|-------|----------|
| MSSQL | trg_Orders_Insert | Orders | Yes |
| MSSQL | trg_OrderDetails_Audit | OrderDetails | **No** (disabled) |
| MySQL | tr_AuditOrder | orders | Yes |
| PostgreSQL | tr_auditorder | orders | Yes |

## Sequences (MSSQL + PostgreSQL)

| Engine | Sequence | Type | Start | Increment |
|--------|----------|------|-------|-----------|
| MSSQL | Seq_OrderNumber | bigint | 1000 | 1 |
| PostgreSQL | seq_ordernumber | bigint | 1000 | 1 |

MySQL uses `AUTO_INCREMENT` (no standalone sequence object).

## Synonyms (MSSQL only)

| Name | Base Object |
|------|-------------|
| syn_Orders | training.Orders |
| syn_OrderDetails | training.OrderDetails |

## User-Defined Types

| Engine | Type | Base Type | Nullable |
|--------|------|-----------|----------|
| MSSQL | CustomerCode | varchar | No |
| MySQL | orders.orderstatus (ENUM) | enum | N/A |
| PostgreSQL | orderstatus (ENUM) | enum | N/A |

## Partitioning

| Engine | Function | Scheme | Partitions | Partition Key |
|--------|----------|--------|------------|---------------|
| MSSQL | PF_OrderDate | PS_OrderDate | 4 | Range on OrderDate |
| MySQL | (native) | | p2024, p2025_h1, p2025_h2, p_default | Range on OrderDate |
| PostgreSQL | (native) | | partitionedorders_2024, partitionedorders_2025_h1, partitionedorders_2025_h2, partitionedorders_default | Range on OrderDate |

## Extended Properties / Comments

| Engine | Mechanism | Object Pattern | Count |
|--------|-----------|---------------|------:|
| MSSQL | Extended Properties (MS_Description) | Schema, 5 tables, 3 columns, 1 view, 1 function, 1 procedure | 12 |
| MySQL | Table/column comments | 7 tables + 2 columns | 9 |
| PostgreSQL | Column/table comments | 7 tables + 1 column | 8 |

## Security Objects

### MSSQL

| Object | Names |
|--------|-------|
| Roles | Role_ReadOnly, Role_DataWriter |
| Users | E2E_TestUser |
| Role Memberships | Role_ReadOnly ← E2E_TestUser |

### PostgreSQL

| Object | Value |
|--------|-------|
| Roles | migration_role |
| Grants | 40 (4 per table for 7 tables + 1 view + 8 sequences) |

### MySQL

MySQL uses native grants (no explicit role objects in the fixture).

## Validation Coverage

All three engines validate every object listed above against the expected state
in `expected.py`. See [validation_matrix.md](validation_matrix.md) for the
check-by-check mapping.