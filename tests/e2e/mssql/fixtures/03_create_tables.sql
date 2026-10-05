-- MSSQL E2E Fixture — 03_create_tables.sql
-- Base tables with identity, computed columns, primary keys, check and
-- default constraints.  Foreign keys are created in 04_create_foreign_keys.sql
-- because they reference tables that must exist first.

-- Customers
CREATE TABLE training.Customers (
    CustomerID   INT IDENTITY(1,1) NOT NULL,
    CustomerCode training.CustomerCode NOT NULL,
    FullName     NVARCHAR(100) NOT NULL,
    Email        VARCHAR(150) NOT NULL,
    Phone        VARCHAR(20) NULL,
    City         VARCHAR(100) NULL,
    Status       VARCHAR(20) NOT NULL CONSTRAINT DF_Customers_Status DEFAULT ('active'),
    IsActive     BIT NOT NULL CONSTRAINT DF_Customers_IsActive DEFAULT (1),
    CreatedAt    DATETIME2 NOT NULL CONSTRAINT DF_Customers_CreatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_Customers PRIMARY KEY (CustomerID),
    CONSTRAINT UQ_Customers_Email UNIQUE (Email),
    CONSTRAINT CK_Customers_Email CHECK (LEN(LTRIM(RTRIM(Email))) > 0),
    CONSTRAINT CK_Customers_Name CHECK (LEN(LTRIM(RTRIM(FullName))) > 0)
);

-- Products
CREATE TABLE training.Products (
    ProductID  INT IDENTITY(1,1) NOT NULL,
    SKU        VARCHAR(50) NOT NULL,
    Name       NVARCHAR(200) NOT NULL,
    Category   VARCHAR(100) NOT NULL CONSTRAINT DF_Products_Category DEFAULT ('General'),
    Price      DECIMAL(10,2) NOT NULL,
    StockQty   INT NOT NULL CONSTRAINT DF_Products_StockQty DEFAULT (0),
    IsActive   BIT NOT NULL CONSTRAINT DF_Products_IsActive DEFAULT (1),
    CreatedAt  DATETIME2 NOT NULL CONSTRAINT DF_Products_CreatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_Products PRIMARY KEY (ProductID),
    CONSTRAINT UQ_Products_SKU UNIQUE (SKU),
    CONSTRAINT CK_Products_Price CHECK (Price >= 0),
    CONSTRAINT CK_Products_Stock CHECK (StockQty >= 0)
);

-- Orders
CREATE TABLE training.Orders (
    OrderID      INT IDENTITY(1,1) NOT NULL,
    OrderNumber  BIGINT NOT NULL,
    CustomerID   INT NOT NULL,
    TotalAmount  DECIMAL(10,2) NULL,
    Status       VARCHAR(50) NOT NULL CONSTRAINT DF_Orders_Status DEFAULT ('pending'),
    OrderDate    DATETIME2 NOT NULL CONSTRAINT DF_Orders_OrderDate DEFAULT (SYSUTCDATETIME()),
    -- Computed column: total plus a one-second stamp to verify computed behaviour
    ModifiedAt   AS DATEADD(SECOND, 1, OrderDate),
    CONSTRAINT PK_Orders PRIMARY KEY (OrderID),
    CONSTRAINT CK_Orders_Amount CHECK (TotalAmount >= 0 OR TotalAmount IS NULL)
);

-- OrderDetails
CREATE TABLE training.OrderDetails (
    DetailID   INT IDENTITY(1,1) NOT NULL,
    OrderID    INT NOT NULL,
    ProductID  INT NOT NULL,
    Quantity   INT NOT NULL,
    UnitPrice  DECIMAL(10,2) NOT NULL,
    -- Computed (persisted) column: line total
    LineTotal  AS Quantity * UnitPrice,
    CONSTRAINT PK_OrderDetails PRIMARY KEY (DetailID),
    CONSTRAINT CK_OrderDetails_Quantity CHECK (Quantity > 0),
    CONSTRAINT CK_OrderDetails_UnitPrice CHECK (UnitPrice >= 0)
);

-- OrderAudit (populated by trigger on Orders insert)
CREATE TABLE training.OrderAudit (
    AuditID       INT IDENTITY(1,1) NOT NULL,
    OrderID       INT NOT NULL,
    Action        VARCHAR(50) NOT NULL,
    AuditTimestamp DATETIME2 NOT NULL CONSTRAINT DF_OrderAudit_AuditTimestamp DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_OrderAudit PRIMARY KEY (AuditID)
);

-- PK_Name_Test — verifies that an explicitly named primary-key constraint
-- is discovered and reproduced on the target.
CREATE TABLE training.PK_Name_Test (
    TestID   INT IDENTITY(1,1) NOT NULL,
    TestName VARCHAR(100) NOT NULL,
    CONSTRAINT PK_TestPrimaryKey PRIMARY KEY (TestID)
);

PRINT 'Tables created: Customers, Products, Orders, OrderDetails, OrderAudit, PK_Name_Test.';
