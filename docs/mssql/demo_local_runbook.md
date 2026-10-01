# MSSQL Migration Demo — Local to Local E2E Runbook

## Purpose

This runbook is the **step-by-step local MSSQL demo** for explaining the Migration Platform to a reviewer or manager.

The flow is intentionally simple:

```text
SQL Server (local)
        │
        ├── Source DB
        │     ├── PART 1 — Basic objects + data
        │     └── PART 2 — Advanced objects + data (optional)
        │
        ├── Source verification
        │
        ├── Target pre-migration verification
        │
        ▼
Migration Platform
        │
        ├── Discover source metadata
        ├── Create target schema/objects
        ├── Migrate data
        └── Run configured validation
        │
        ▼
Target verification
        │
        └── Source vs Target comparison
```

> **Demo scope:** local MSSQL source → local MSSQL target, same SQL Server instance, port `1533`.
>
> **Security:** passwords are never stored in this YAML. Use PowerShell environment variables.

---

# 0. Prerequisites

Before starting the demo, confirm:

- SQL Server is running locally.
- SQL Server is listening on `localhost,1533`.
- `sqlcmd` is installed.
- The Migration Platform project is available.
- Python environment is ready.
- The repository contains `config/mssql_demo.yaml`.

Project root used in this runbook:

```text
F:\Quantumatrix\Projects\P2Migration\Migration_platform
```

Quick SQL Server connectivity check:

```powershell
sqlcmd -S localhost,1533 -U sa -d master -C
```

If the installed `sqlcmd` is under a specific path, use that executable instead.

---

# STEP 1 — Create and Verify Source Database

## 1.1 Connect to `master`

```powershell
sqlcmd -S localhost,1533 -U sa -d master -C
```

If SQL authentication requires a password prompt, enter the local `sa` password.

## 1.2 Create source database

Run:

```sql
IF DB_ID('mssql_source_db1') IS NULL
BEGIN
    CREATE DATABASE mssql_source_db1;
END
GO

SELECT
    name AS database_name,
    state_desc AS database_state
FROM sys.databases
WHERE name = 'mssql_source_db1';
GO
```

Expected:

```text
database_name       database_state
------------------  --------------
mssql_source_db1    ONLINE
```

---

# STEP 2 — Create and Verify Target Database

Remain connected to `master`.

## 2.1 Create target database

```sql
IF DB_ID('mssql_target_db2') IS NULL
BEGIN
    CREATE DATABASE mssql_target_db2;
END
GO

SELECT
    name AS database_name,
    state_desc AS database_state
FROM sys.databases
WHERE name = 'mssql_target_db2';
GO
```

Expected:

```text
database_name       database_state
------------------  --------------
mssql_target_db2    ONLINE
```

At this point:

```text
SOURCE
mssql_source_db1 → ONLINE

TARGET
mssql_target_db2 → ONLINE
```

The target must still be empty.

---

# STEP 3 — Connect to Source Database

Exit `sqlcmd`:

```text
EXIT
```

Connect to source:

```powershell
sqlcmd -S localhost,1533 -U sa -d mssql_source_db1 -C
```

Verify:

```sql
SELECT DB_NAME() AS current_database;
GO
```

Expected:

```text
current_database
----------------
mssql_source_db1
```

---

# STEP 4 — PART 1: Create Basic MSSQL Objects

PART 1 is the **mandatory core migration demo**.

It creates:

- `sales` schema
- 3 tables
- identity columns
- primary keys
- foreign keys
- unique constraints
- check constraints
- default constraints
- indexes
- sample data

Run this complete block while connected to `mssql_source_db1`.

```sql
USE mssql_source_db1;
GO

/* =========================================================
   1. SCHEMA
   ========================================================= */

IF SCHEMA_ID('sales') IS NULL
    EXEC('CREATE SCHEMA sales');
GO


/* =========================================================
   2. CUSTOMERS
   ========================================================= */

CREATE TABLE sales.customers
(
    customer_id INT IDENTITY(1,1) NOT NULL,
    customer_name VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL,
    city VARCHAR(100) NULL,
    age INT NULL,

    created_date DATETIME2 NOT NULL
        CONSTRAINT DF_customers_created_date
        DEFAULT (SYSDATETIME()),

    CONSTRAINT PK_customers
        PRIMARY KEY (customer_id),

    CONSTRAINT UQ_customers_email
        UNIQUE (email)
);
GO


/* =========================================================
   3. PRODUCTS
   ========================================================= */

CREATE TABLE sales.products
(
    product_id INT IDENTITY(1,1) NOT NULL,
    product_name VARCHAR(150) NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    stock_quantity INT NOT NULL,

    is_active BIT NOT NULL
        CONSTRAINT DF_products_is_active
        DEFAULT (1),

    created_date DATETIME2 NOT NULL
        CONSTRAINT DF_products_created_date
        DEFAULT (SYSDATETIME()),

    CONSTRAINT PK_products
        PRIMARY KEY (product_id),

    CONSTRAINT UQ_products_name
        UNIQUE (product_name),

    CONSTRAINT CK_products_price
        CHECK (price > 0),

    CONSTRAINT CK_products_stock
        CHECK (stock_quantity >= 0)
);
GO


/* =========================================================
   4. ORDERS
   ========================================================= */

CREATE TABLE sales.orders
(
    order_id INT IDENTITY(1001,1) NOT NULL,
    customer_id INT NOT NULL,
    product_id INT NOT NULL,
    order_amount DECIMAL(10,2) NOT NULL,

    order_status VARCHAR(20) NOT NULL
        CONSTRAINT DF_orders_status
        DEFAULT ('NEW'),

    order_date DATETIME2 NOT NULL
        CONSTRAINT DF_orders_date
        DEFAULT (SYSDATETIME()),

    CONSTRAINT PK_orders
        PRIMARY KEY (order_id),

    CONSTRAINT CK_orders_amount
        CHECK (order_amount > 0),

    CONSTRAINT CK_orders_status
        CHECK
        (
            order_status IN ('NEW', 'PAID', 'CANCELLED')
        ),

    CONSTRAINT FK_orders_customers
        FOREIGN KEY (customer_id)
        REFERENCES sales.customers(customer_id),

    CONSTRAINT FK_orders_products
        FOREIGN KEY (product_id)
        REFERENCES sales.products(product_id)
);
GO


/* =========================================================
   5. INDEXES
   ========================================================= */

CREATE INDEX IX_customers_city
ON sales.customers(city);
GO

CREATE INDEX IX_orders_customer_id
ON sales.orders(customer_id);
GO

CREATE INDEX IX_orders_customer_product
ON sales.orders(customer_id, product_id);
GO

CREATE INDEX IX_products_price
ON sales.products(price);
GO


/* =========================================================
   6. CUSTOMER DATA
   ========================================================= */

INSERT INTO sales.customers
(
    customer_name,
    email,
    city,
    age
)
VALUES
    ('Nitesh', 'nitesh@test.com', 'Indore', 25),
    ('Amit', 'amit@test.com', 'Bhopal', 28),
    ('Rahul', 'rahul@test.com', 'Khandwa', 27),
    ('Priya', 'priya@test.com', 'Ujjain', 24);
GO


/* =========================================================
   7. PRODUCT DATA
   ========================================================= */

INSERT INTO sales.products
(
    product_name,
    price,
    stock_quantity
)
VALUES
    ('Laptop', 55000.00, 10),
    ('Keyboard', 1500.00, 25),
    ('Mouse', 800.00, 50),
    ('Monitor', 12000.00, 15);
GO


/* =========================================================
   8. ORDER DATA
   ========================================================= */

INSERT INTO sales.orders
(
    customer_id,
    product_id,
    order_amount,
    order_status
)
VALUES
    (1, 1, 55000.00, 'PAID'),
    (1, 2, 1500.00, 'NEW'),
    (2, 3, 800.00, 'PAID'),
    (3, 4, 12000.00, 'NEW'),
    (4, 2, 1500.00, 'CANCELLED');
GO


SELECT
    'PART 1 CREATION COMPLETED' AS status,
    DB_NAME() AS database_name;
GO
```

Expected:

```text
PART 1 CREATION COMPLETED
mssql_source_db1
```

### PART 1 data count

The SQL above creates:

```text
customers → 4 rows
products  → 4 rows
orders    → 5 rows
-------------------
TOTAL     → 13 rows
```

---

# STEP 5 — PART 2: Create Advanced MSSQL Objects (Optional)

PART 2 is optional.

Use it when the demo needs to show advanced MSSQL object migration.

It covers:

- computed column
- sequence
- scalar function
- table-valued function
- stored procedure
- trigger
- synonym
- user-defined type
- partition function
- partition scheme
- partitioned table
- extended properties
- database user
- database role
- role membership
- object permissions

> If the manager only needs the basic end-to-end migration flow, skip PART 2 and continue to STEP 6.

Run the following while connected to `mssql_source_db1`.

```sql
USE mssql_source_db1;
GO


/* =========================================================
   1. COMPUTED COLUMN
   ========================================================= */

ALTER TABLE sales.products
ADD price_with_tax AS (price * 1.18);
GO


/* =========================================================
   2. SEQUENCE
   ========================================================= */

CREATE SEQUENCE sales.order_sequence
    AS INT
    START WITH 10000
    INCREMENT BY 1;
GO


/* =========================================================
   3. SCALAR FUNCTION
   ========================================================= */

CREATE FUNCTION sales.fn_calculate_tax
(
    @amount DECIMAL(10,2)
)
RETURNS DECIMAL(10,2)
AS
BEGIN
    RETURN @amount * 0.18;
END;
GO


/* =========================================================
   4. TABLE-VALUED FUNCTION
   ========================================================= */

CREATE FUNCTION sales.fn_customer_orders
(
    @customer_id INT
)
RETURNS TABLE
AS
RETURN
(
    SELECT
        order_id,
        customer_id,
        product_id,
        order_amount,
        order_status,
        order_date
    FROM sales.orders
    WHERE customer_id = @customer_id
);
GO


/* =========================================================
   5. STORED PROCEDURE
   ========================================================= */

CREATE PROCEDURE sales.sp_get_customer_orders
    @customer_id INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        order_id,
        customer_id,
        product_id,
        order_amount,
        order_status,
        order_date
    FROM sales.orders
    WHERE customer_id = @customer_id;
END;
GO


/* =========================================================
   6. AUDIT TABLE FOR TRIGGER
   ========================================================= */

CREATE TABLE sales.order_audit
(
    audit_id INT IDENTITY(1,1) NOT NULL
        CONSTRAINT PK_order_audit
        PRIMARY KEY,

    order_id INT NOT NULL,

    action_type VARCHAR(20) NOT NULL,

    audit_date DATETIME2 NOT NULL
        CONSTRAINT DF_order_audit_date
        DEFAULT (SYSDATETIME())
);
GO


/* =========================================================
   7. INSERT TRIGGER
   ========================================================= */

CREATE TRIGGER sales.trg_orders_insert
ON sales.orders
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO sales.order_audit
    (
        order_id,
        action_type
    )
    SELECT
        order_id,
        'INSERT'
    FROM inserted;
END;
GO


/* =========================================================
   8. TRIGGER TEST DATA
   ========================================================= */

INSERT INTO sales.orders
(
    customer_id,
    product_id,
    order_amount,
    order_status
)
VALUES
    (1, 3, 800.00, 'NEW');
GO


/* =========================================================
   9. SYNONYM
   ========================================================= */

CREATE SYNONYM sales.customer_data
FOR sales.customers;
GO


/* =========================================================
   10. USER-DEFINED TYPE
   ========================================================= */

CREATE TYPE sales.CustomerCode
FROM VARCHAR(20) NOT NULL;
GO


/* =========================================================
   11. PARTITION FUNCTION
   ========================================================= */

CREATE PARTITION FUNCTION OrderDatePartitionFunction
(
    DATETIME2
)
AS RANGE RIGHT
FOR VALUES
(
    '20260101',
    '20260701',
    '20270101'
);
GO


/* =========================================================
   12. PARTITION SCHEME
   ========================================================= */

CREATE PARTITION SCHEME OrderDatePartitionScheme
AS PARTITION OrderDatePartitionFunction
ALL TO ([PRIMARY]);
GO


/* =========================================================
   13. PARTITIONED TABLE
   ========================================================= */

CREATE TABLE sales.partitioned_orders
(
    order_id INT NOT NULL,
    order_date DATETIME2 NOT NULL,
    order_amount DECIMAL(10,2) NOT NULL,

    CONSTRAINT PK_partitioned_orders
        PRIMARY KEY CLUSTERED
        (
            order_id,
            order_date
        )
)
ON OrderDatePartitionScheme(order_date);
GO


/* =========================================================
   14. PARTITIONED DATA
   ========================================================= */

INSERT INTO sales.partitioned_orders
(
    order_id,
    order_date,
    order_amount
)
VALUES
    (1, '2025-12-15', 1000.00),
    (2, '2026-03-15', 2000.00),
    (3, '2026-08-15', 3000.00),
    (4, '2027-02-15', 4000.00);
GO


/* =========================================================
   15. EXTENDED PROPERTY — TABLE
   ========================================================= */

EXEC sys.sp_addextendedproperty
    @name = N'Description',
    @value = N'Customer master table for migration testing',
    @level0type = N'SCHEMA',
    @level0name = N'sales',
    @level1type = N'TABLE',
    @level1name = N'customers';
GO


/* =========================================================
   16. EXTENDED PROPERTY — COLUMN
   ========================================================= */

EXEC sys.sp_addextendedproperty
    @name = N'Description',
    @value = N'Unique email address of customer',
    @level0type = N'SCHEMA',
    @level0name = N'sales',
    @level1type = N'TABLE',
    @level1name = N'customers',
    @level2type = N'COLUMN',
    @level2name = N'email';
GO


/* =========================================================
   17. DATABASE USER
   ========================================================= */

IF NOT EXISTS
(
    SELECT 1
    FROM sys.database_principals
    WHERE name = 'migration_demo_user'
)
BEGIN
    CREATE USER migration_demo_user WITHOUT LOGIN;
END;
GO


/* =========================================================
   18. DATABASE ROLE
   ========================================================= */

IF NOT EXISTS
(
    SELECT 1
    FROM sys.database_principals
    WHERE name = 'migration_demo_role'
)
BEGIN
    CREATE ROLE migration_demo_role;
END;
GO


/* =========================================================
   19. ROLE MEMBERSHIP
   ========================================================= */

IF NOT EXISTS
(
    SELECT 1
    FROM sys.database_role_members AS drm
    INNER JOIN sys.database_principals AS rp
        ON drm.role_principal_id = rp.principal_id
    INNER JOIN sys.database_principals AS mp
        ON drm.member_principal_id = mp.principal_id
    WHERE rp.name = 'migration_demo_role'
      AND mp.name = 'migration_demo_user'
)
BEGIN
    ALTER ROLE migration_demo_role
    ADD MEMBER migration_demo_user;
END;
GO


/* =========================================================
   20. OBJECT PERMISSIONS
   ========================================================= */

GRANT SELECT ON sales.customers TO migration_demo_role;
GRANT SELECT ON sales.products TO migration_demo_role;
GRANT SELECT ON sales.orders TO migration_demo_role;
GO


SELECT
    'PART 2 CREATION COMPLETED' AS status,
    DB_NAME() AS database_name;
GO
```

Expected:

```text
PART 2 CREATION COMPLETED
mssql_source_db1
```

---

# STEP 6 — Source Verification: PART 1

The source database is the **baseline**.

Before migration, verify exactly what exists in the source. After migration, the same categories will be checked in the target.

Run in `mssql_source_db1`.

## 6.1 Schema and tables

```sql
SELECT
    name AS schema_name
FROM sys.schemas
WHERE name = 'sales';
GO

SELECT
    TABLE_SCHEMA,
    TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'sales'
ORDER BY TABLE_NAME;
GO
```

Expected PART 1 tables:

```text
sales.customers
sales.orders
sales.products
```

If PART 2 was created:

```text
sales.order_audit
sales.partitioned_orders
```

## 6.2 Identity columns

```sql
SELECT
    OBJECT_SCHEMA_NAME(object_id) AS schema_name,
    OBJECT_NAME(object_id) AS table_name,
    name AS column_name,
    seed_value,
    increment_value
FROM sys.identity_columns
WHERE OBJECT_SCHEMA_NAME(object_id) = 'sales'
ORDER BY table_name, column_name;
GO
```

## 6.3 Primary keys

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name
FROM sys.key_constraints
WHERE type = 'PK'
  AND OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name;
GO
```

Expected PART 1:

```text
PK_customers
PK_products
PK_orders
```

## 6.4 Foreign keys

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name,
    OBJECT_SCHEMA_NAME(referenced_object_id) AS referenced_schema,
    OBJECT_NAME(referenced_object_id) AS referenced_table
FROM sys.foreign_keys
WHERE OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO
```

Expected:

```text
FK_orders_customers
FK_orders_products
```

## 6.5 Unique constraints

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name
FROM sys.key_constraints
WHERE type = 'UQ'
  AND OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO
```

Expected:

```text
UQ_customers_email
UQ_products_name
```

## 6.6 CHECK constraints

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name,
    definition
FROM sys.check_constraints
WHERE OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO
```

Expected:

```text
CK_products_price
CK_products_stock
CK_orders_amount
CK_orders_status
```

## 6.7 DEFAULT constraints

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name,
    definition
FROM sys.default_constraints
WHERE OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO
```

Expected:

```text
DF_customers_created_date
DF_products_is_active
DF_products_created_date
DF_orders_status
DF_orders_date
```

## 6.8 Indexes

```sql
SELECT
    OBJECT_SCHEMA_NAME(i.object_id) AS schema_name,
    OBJECT_NAME(i.object_id) AS table_name,
    i.name AS index_name,
    i.type_desc,
    i.is_unique,
    i.is_primary_key
FROM sys.indexes AS i
WHERE OBJECT_SCHEMA_NAME(i.object_id) = 'sales'
  AND i.name IS NOT NULL
ORDER BY table_name, index_name;
GO
```

Expected additional non-PK indexes:

```text
IX_customers_city
IX_orders_customer_id
IX_orders_customer_product
IX_products_price
```

## 6.9 Row counts

```sql
SELECT
    'sales.customers' AS table_name,
    COUNT(*) AS row_count
FROM sales.customers

UNION ALL

SELECT
    'sales.products',
    COUNT(*)
FROM sales.products

UNION ALL

SELECT
    'sales.orders',
    COUNT(*)
FROM sales.orders;
GO
```

Expected:

```text
sales.customers → 4
sales.products  → 4
sales.orders    → 5
--------------------
PART 1 TOTAL    → 13
```

---

# STEP 7 — Source Verification: PART 2

Run this only if PART 2 was created.

## 7.1 Computed columns

```sql
SELECT
    OBJECT_SCHEMA_NAME(object_id) AS schema_name,
    OBJECT_NAME(object_id) AS table_name,
    name AS column_name,
    definition
FROM sys.computed_columns
WHERE OBJECT_SCHEMA_NAME(object_id) = 'sales';
GO
```

Expected:

```text
sales.products.price_with_tax
```

## 7.2 Sequence

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS sequence_name,
    start_value,
    increment,
    current_value
FROM sys.sequences
WHERE SCHEMA_NAME(schema_id) = 'sales';
GO
```

Expected:

```text
sales.order_sequence
```

## 7.3 Functions

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS function_name,
    type_desc
FROM sys.objects
WHERE schema_id = SCHEMA_ID('sales')
  AND type IN ('FN', 'IF', 'TF')
ORDER BY name;
GO
```

Expected:

```text
fn_calculate_tax
fn_customer_orders
```

## 7.4 Function execution

```sql
SELECT sales.fn_calculate_tax(1000.00) AS calculated_tax;
GO

SELECT *
FROM sales.fn_customer_orders(1);
GO
```

Expected tax:

```text
180.00
```

## 7.5 Stored procedure

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS procedure_name
FROM sys.procedures
WHERE schema_id = SCHEMA_ID('sales');
GO

EXEC sales.sp_get_customer_orders @customer_id = 1;
GO
```

## 7.6 Trigger

```sql
SELECT
    SCHEMA_NAME(t.schema_id) AS schema_name,
    t.name AS trigger_name,
    OBJECT_NAME(t.parent_id) AS parent_object
FROM sys.triggers AS t
WHERE t.parent_class = 1
  AND t.schema_id = SCHEMA_ID('sales');
GO
```

Expected:

```text
trg_orders_insert
```

Verify the trigger-generated audit row:

```sql
SELECT *
FROM sales.order_audit
ORDER BY audit_id;
GO
```

## 7.7 Synonym

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS synonym_name,
    base_object_name
FROM sys.synonyms
WHERE schema_id = SCHEMA_ID('sales');
GO
```

Expected:

```text
customer_data → sales.customers
```

## 7.8 User-defined type

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS type_name,
    system_type_id,
    max_length,
    is_nullable
FROM sys.types
WHERE is_user_defined = 1
  AND schema_id = SCHEMA_ID('sales');
GO
```

Expected:

```text
CustomerCode
```

## 7.9 Partition objects

```sql
SELECT
    name AS partition_function,
    boundary_value_on_right,
    fanout
FROM sys.partition_functions
WHERE name = 'OrderDatePartitionFunction';
GO

SELECT
    ps.name AS partition_scheme,
    pf.name AS partition_function
FROM sys.partition_schemes AS ps
JOIN sys.partition_functions AS pf
    ON ps.function_id = pf.function_id
WHERE ps.name = 'OrderDatePartitionScheme';
GO
```

## 7.10 Partitioned table and data

```sql
SELECT
    OBJECT_SCHEMA_NAME(p.object_id) AS schema_name,
    OBJECT_NAME(p.object_id) AS table_name,
    p.partition_number,
    p.rows
FROM sys.partitions AS p
WHERE p.object_id = OBJECT_ID('sales.partitioned_orders')
  AND p.index_id IN (0, 1)
ORDER BY p.partition_number;
GO

SELECT
    order_id,
    order_date,
    order_amount,
    $PARTITION.OrderDatePartitionFunction(order_date) AS partition_number
FROM sales.partitioned_orders
ORDER BY order_date;
GO
```

## 7.11 Extended properties

```sql
SELECT
    o.name AS object_name,
    ep.name AS property_name,
    ep.value AS property_value
FROM sys.extended_properties AS ep
JOIN sys.objects AS o
    ON ep.major_id = o.object_id
WHERE o.schema_id = SCHEMA_ID('sales');
GO
```

## 7.12 Security objects

```sql
SELECT
    name,
    type_desc,
    authentication_type_desc
FROM sys.database_principals
WHERE name IN
(
    'migration_demo_user',
    'migration_demo_role'
);
GO

SELECT
    role_principal.name AS role_name,
    member_principal.name AS member_name
FROM sys.database_role_members AS drm
JOIN sys.database_principals AS role_principal
    ON drm.role_principal_id = role_principal.principal_id
JOIN sys.database_principals AS member_principal
    ON drm.member_principal_id = member_principal.principal_id
WHERE role_principal.name = 'migration_demo_role';
GO

SELECT
    grantee.name AS grantee_name,
    dp.permission_name,
    dp.state_desc,
    OBJECT_SCHEMA_NAME(dp.major_id) AS schema_name,
    OBJECT_NAME(dp.major_id) AS object_name
FROM sys.database_permissions AS dp
JOIN sys.database_principals AS grantee
    ON dp.grantee_principal_id = grantee.principal_id
WHERE grantee.name = 'migration_demo_role'
ORDER BY object_name;
GO
```

---

# STEP 8 — Target Pre-Migration Verification

Exit source:

```text
EXIT
```

Connect to target:

```powershell
sqlcmd -S localhost,1533 -U sa -d mssql_target_db2 -C
```

Verify:

```sql
SELECT DB_NAME() AS current_database;
GO

SELECT
    TABLE_SCHEMA,
    TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'sales';
GO

SELECT
    name,
    type_desc
FROM sys.objects
WHERE SCHEMA_NAME(schema_id) = 'sales';
GO
```

Expected:

```text
No application tables or objects yet.
```

This gives the clean migration baseline:

```text
SOURCE
mssql_source_db1
    ├── objects
    └── data

TARGET
mssql_target_db2
    └── empty
```

---

# STEP 9 — Migration Configuration

The repository already contains:

```text
config/mssql_demo.yaml
```

The important configuration is:

```yaml
source:
  engine: mssql
  connection:
    host: localhost
    port: 1533
    database: mssql_source_db1
    username: sa
    password_secret: mssql_source_pass
    ssl: false

target:
  engine: mssql
  connection:
    host: localhost
    port: 1533
    database: mssql_target_db2
    username: sa
    password_secret: mssql_target_pass
    ssl: false

migration:
  mode: full
  batch_size: 1000
  include_schemas:
    - sales

secrets:
  provider: env

alerting:
  notifier: none

validation:
  mode: count
  sample_size: 1000

logging:
  level: INFO
```

> **Important:** Do not put the real password into the YAML.

Set secrets in PowerShell:

```powershell
$env:SECRET_mssql_source_pass = "<local-sa-password>"
$env:SECRET_mssql_target_pass = "<local-sa-password>"
```

---

# STEP 10 — Run Migration

Go to the project root:

```powershell
cd F:\Quantumatrix\Projects\P2Migration\Migration_platform
```

Run:

```powershell
python -m migration_platform --config config/mssql_demo.yaml --mode full --no-live-ui
```

### What to explain to the reviewer

A simple explanation:

> "The YAML defines the source and target. The migration engine discovers the source metadata, creates the target objects, migrates the data, and performs the configured validation."

During the run, watch for:

```text
Migration SUCCESS
```

and confirm:

```text
Tables migrated
Rows migrated
Failed rows / errors
Validation result
```

Do not mark the demo successful only because the process exits. Continue with target verification.

---

# STEP 11 — Target Post-Migration Verification

Connect to target:

```powershell
sqlcmd -S localhost,1533 -U sa -d mssql_target_db2 -C
```

First verify:

```sql
SELECT DB_NAME() AS current_database;
GO
```

Expected:

```text
mssql_target_db2
```

---

## 11.1 Target schema and tables

```sql
SELECT
    name AS schema_name
FROM sys.schemas
WHERE name = 'sales';
GO

SELECT
    TABLE_SCHEMA,
    TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'sales'
ORDER BY TABLE_NAME;
GO
```

For PART 1 the expected tables are:

```text
customers
orders
products
```

If PART 2 was enabled:

```text
order_audit
partitioned_orders
```

---

## 11.2 Target row counts

```sql
SELECT
    'sales.customers' AS table_name,
    COUNT(*) AS row_count
FROM sales.customers

UNION ALL

SELECT
    'sales.products',
    COUNT(*)
FROM sales.products

UNION ALL

SELECT
    'sales.orders',
    COUNT(*)
FROM sales.orders;
GO
```

For the PART 1 baseline:

```text
customers → 4
products  → 4
orders    → 5
TOTAL     → 13
```

If PART 2 was executed, also verify:

```sql
SELECT
    'sales.order_audit' AS table_name,
    COUNT(*) AS row_count
FROM sales.order_audit

UNION ALL

SELECT
    'sales.partitioned_orders',
    COUNT(*)
FROM sales.partitioned_orders;
GO
```

---

## 11.3 Target primary keys

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name
FROM sys.key_constraints
WHERE type = 'PK'
  AND OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name;
GO
```

Compare the names with the source.

---

## 11.4 Target foreign keys

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name,
    OBJECT_SCHEMA_NAME(referenced_object_id) AS referenced_schema,
    OBJECT_NAME(referenced_object_id) AS referenced_table
FROM sys.foreign_keys
WHERE OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO
```

Expected PART 1:

```text
FK_orders_customers
FK_orders_products
```

---

## 11.5 Target unique/check/default constraints

```sql
SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name
FROM sys.key_constraints
WHERE type = 'UQ'
  AND OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO

SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name,
    definition
FROM sys.check_constraints
WHERE OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO

SELECT
    OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,
    OBJECT_NAME(parent_object_id) AS table_name,
    name AS constraint_name,
    definition
FROM sys.default_constraints
WHERE OBJECT_SCHEMA_NAME(parent_object_id) = 'sales'
ORDER BY table_name, constraint_name;
GO
```

Compare:

```text
Source constraint names
        ↓
Target constraint names
        ↓
Definitions / relationships
```

---

## 11.6 Target indexes

```sql
SELECT
    OBJECT_SCHEMA_NAME(i.object_id) AS schema_name,
    OBJECT_NAME(i.object_id) AS table_name,
    i.name AS index_name,
    i.type_desc,
    i.is_unique,
    i.is_primary_key
FROM sys.indexes AS i
WHERE OBJECT_SCHEMA_NAME(i.object_id) = 'sales'
  AND i.name IS NOT NULL
ORDER BY table_name, index_name;
GO
```

Confirm the named indexes from the source exist on the target.

---

# STEP 12 — Target PART 2 Verification

Run only when PART 2 was included.

## 12.1 Computed column

```sql
SELECT
    OBJECT_SCHEMA_NAME(object_id) AS schema_name,
    OBJECT_NAME(object_id) AS table_name,
    name AS column_name,
    definition
FROM sys.computed_columns
WHERE OBJECT_SCHEMA_NAME(object_id) = 'sales';
GO
```

## 12.2 Sequence

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS sequence_name,
    start_value,
    increment,
    current_value
FROM sys.sequences
WHERE SCHEMA_NAME(schema_id) = 'sales';
GO
```

## 12.3 Functions

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS function_name,
    type_desc
FROM sys.objects
WHERE schema_id = SCHEMA_ID('sales')
  AND type IN ('FN', 'IF', 'TF')
ORDER BY name;
GO
```

## 12.4 Function execution

```sql
SELECT sales.fn_calculate_tax(1000.00) AS calculated_tax;
GO

SELECT *
FROM sales.fn_customer_orders(1);
GO
```

## 12.5 Stored procedure

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS procedure_name
FROM sys.procedures
WHERE schema_id = SCHEMA_ID('sales');
GO

EXEC sales.sp_get_customer_orders @customer_id = 1;
GO
```

## 12.6 Trigger

```sql
SELECT
    SCHEMA_NAME(t.schema_id) AS schema_name,
    t.name AS trigger_name,
    OBJECT_NAME(t.parent_id) AS parent_object
FROM sys.triggers AS t
WHERE t.parent_class = 1
  AND t.schema_id = SCHEMA_ID('sales');
GO

SELECT *
FROM sales.order_audit
ORDER BY audit_id;
GO
```

## 12.7 Synonym

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS synonym_name,
    base_object_name
FROM sys.synonyms
WHERE schema_id = SCHEMA_ID('sales');
GO

SELECT *
FROM sales.customer_data
ORDER BY customer_id;
GO
```

## 12.8 UDT

```sql
SELECT
    SCHEMA_NAME(schema_id) AS schema_name,
    name AS type_name,
    system_type_id,
    max_length,
    is_nullable
FROM sys.types
WHERE is_user_defined = 1
  AND schema_id = SCHEMA_ID('sales');
GO
```

## 12.9 Partition objects

```sql
SELECT
    name AS partition_function,
    boundary_value_on_right,
    fanout
FROM sys.partition_functions
WHERE name = 'OrderDatePartitionFunction';
GO

SELECT
    ps.name AS partition_scheme,
    pf.name AS partition_function
FROM sys.partition_schemes AS ps
JOIN sys.partition_functions AS pf
    ON ps.function_id = pf.function_id
WHERE ps.name = 'OrderDatePartitionScheme';
GO

SELECT
    OBJECT_SCHEMA_NAME(p.object_id) AS schema_name,
    OBJECT_NAME(p.object_id) AS table_name,
    p.partition_number,
    p.rows
FROM sys.partitions AS p
WHERE p.object_id = OBJECT_ID('sales.partitioned_orders')
  AND p.index_id IN (0, 1)
ORDER BY p.partition_number;
GO
```

## 12.10 Extended properties

```sql
SELECT
    o.name AS object_name,
    ep.name AS property_name,
    ep.value AS property_value
FROM sys.extended_properties AS ep
JOIN sys.objects AS o
    ON ep.major_id = o.object_id
WHERE o.schema_id = SCHEMA_ID('sales');
GO
```

## 12.11 Users, roles and permissions

```sql
SELECT
    name,
    type_desc,
    authentication_type_desc
FROM sys.database_principals
WHERE name IN
(
    'migration_demo_user',
    'migration_demo_role'
);
GO

SELECT
    role_principal.name AS role_name,
    member_principal.name AS member_name
FROM sys.database_role_members AS drm
JOIN sys.database_principals AS role_principal
    ON drm.role_principal_id = role_principal.principal_id
JOIN sys.database_principals AS member_principal
    ON drm.member_principal_id = member_principal.principal_id
WHERE role_principal.name = 'migration_demo_role';
GO

SELECT
    grantee.name AS grantee_name,
    dp.permission_name,
    dp.state_desc,
    OBJECT_SCHEMA_NAME(dp.major_id) AS schema_name,
    OBJECT_NAME(dp.major_id) AS object_name
FROM sys.database_permissions AS dp
JOIN sys.database_principals AS grantee
    ON dp.grantee_principal_id = grantee.principal_id
WHERE grantee.name = 'migration_demo_role'
ORDER BY object_name;
GO
```

---

# STEP 13 — Source vs Target Final Validation

This is the **most important final demo step**.

The goal is not only:

```text
Migration SUCCESS
```

The goal is:

```text
SOURCE
   ↓
objects + data
   ↓
Migration Platform
   ↓
TARGET
   ↓
same objects + same data
```

Use this validation checklist.

| Area | Source | Target | Expected |
|---|---|---|---|
| Database | `mssql_source_db1` | `mssql_target_db2` | both ONLINE |
| Schema | `sales` | `sales` | match |
| Tables | PART 1/2 tables | PART 1/2 tables | match |
| Identity | identity columns | identity columns | match |
| Primary Keys | named PKs | named PKs | match |
| Foreign Keys | named FKs | named FKs | match |
| Unique Constraints | named UQs | named UQs | match |
| CHECK Constraints | named checks | named checks | match |
| DEFAULT Constraints | named defaults | named defaults | match |
| Indexes | named indexes | named indexes | match |
| Data | source counts | target counts | match |
| Computed Column | if PART 2 | if PART 2 | match |
| Sequence | if PART 2 | if PART 2 | match |
| Functions | if PART 2 | if PART 2 | match |
| Procedures | if PART 2 | if PART 2 | match |
| Triggers | if PART 2 | if PART 2 | match |
| Synonyms | if PART 2 | if PART 2 | match |
| UDT | if PART 2 | if PART 2 | match |
| Partitioning | if PART 2 | if PART 2 | match |
| Extended Properties | if PART 2 | if PART 2 | match |
| Users/Roles | if PART 2 | if PART 2 | match |
| Permissions | if PART 2 | if PART 2 | match |

### Final result to report

```text
SOURCE
mssql_source_db1
      │
      │  metadata + data
      ▼
Migration Platform
      │
      │  migration + validation
      ▼
TARGET
mssql_target_db2
```

Then report:

```text
Schema           → PASS
Tables           → PASS
Constraints      → PASS
Indexes          → PASS
Objects          → PASS
Data             → PASS
Source/Target    → MATCH
Migration        → SUCCESS
```

Only mark an item `PASS` after checking the actual source and target result.

---

# STEP 14 — Optional Automated Fresh-Target Validation

`validate_fresh_target.py` automates the STEP 11/12 catalog checks against the
demo target (`mssql_target_db2`). It reads its connection settings from
environment variables and never stores a password in the file:

```powershell
$env:SQLSERVER_HOST     = "localhost,1533"
$env:SQLSERVER_DATABASE = "mssql_target_db2"
$env:SQLSERVER_USER     = "sa"
$env:SECRET_mssql_target_pass = "<local-sa-password>"

python validate_fresh_target.py
```

It reports schemas, tables and row counts, PK/FK/CHECK/DEFAULT constraints,
identity and computed columns, sequences, indexes, views, functions,
procedures, triggers, synonyms, user-defined types, partition objects,
specialized types, security objects, extended properties, and cross-schema
foreign keys.

The manual verification in this runbook remains the authoritative demo
procedure because it shows the actual SQL Server metadata checks.

---

# STEP 15 — Cleanup After Demo

When the demo is finished, connect to `master`:

```powershell
sqlcmd -S localhost,1533 -U sa -d master -C
```

Drop the temporary databases:

```sql
IF DB_ID('mssql_source_db1') IS NOT NULL
BEGIN
    ALTER DATABASE mssql_source_db1
    SET SINGLE_USER WITH ROLLBACK IMMEDIATE;

    DROP DATABASE mssql_source_db1;
END
GO

IF DB_ID('mssql_target_db2') IS NOT NULL
BEGIN
    ALTER DATABASE mssql_target_db2
    SET SINGLE_USER WITH ROLLBACK IMMEDIATE;

    DROP DATABASE mssql_target_db2;
END
GO

SELECT
    name,
    state_desc
FROM sys.databases
WHERE name IN
(
    'mssql_source_db1',
    'mssql_target_db2'
);
GO
```

Expected:

```text
No rows
```

---

# Demo Summary — What to Tell the Reviewer

The complete demo can be explained in five points:

1. **Prepare source**
   Create a local MSSQL source database and populate it with representative objects/data.

2. **Baseline verification**
   Verify the source before migration so there is a known reference state.

3. **Clean target**
   Create a separate empty target database and verify that no application objects exist.

4. **Run Migration Platform**
   Execute the YAML-driven local-to-local migration.

5. **Prove the result**
   Verify target schema, tables, constraints, indexes, advanced objects, row counts, and finally compare source vs target.

The important conclusion is:

```text
Source baseline
      ↓
Migration
      ↓
Target verification
      ↓
Source vs Target match
      ↓
E2E migration demonstrated
```

---

# Quick Demo Command Sheet

For a live demo, these are the commands to keep ready.

### Connect

```powershell
sqlcmd -S localhost,1533 -U sa -d master -C
```

### Set secrets

```powershell
$env:SECRET_mssql_source_pass = "<local-sa-password>"
$env:SECRET_mssql_target_pass = "<local-sa-password>"
```

### Run migration

```powershell
cd F:\Quantumatrix\Projects\P2Migration\Migration_platform

python -m migration_platform --config config/mssql_demo.yaml --mode full --no-live-ui
```

### Connect to target

```powershell
sqlcmd -S localhost,1533 -U sa -d mssql_target_db2 -C
```

### Final concept

```text
LOCAL SOURCE
    │
    │  metadata + data
    ▼
MIGRATION PLATFORM
    │
    │  create + migrate + validate
    ▼
LOCAL TARGET
    │
    ▼
SOURCE vs TARGET
    │
    └── MATCH → DEMO PASS
```
