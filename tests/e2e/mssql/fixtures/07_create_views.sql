-- MSSQL E2E Fixture — 07_create_views.sql
-- Views referencing tables and other views.

CREATE VIEW training.vw_CustomerOrders
AS
SELECT
    c.CustomerID,
    c.FullName,
    COUNT(o.OrderID)                       AS OrderCount,
    ISNULL(SUM(o.TotalAmount), 0)          AS TotalSpent,
    MAX(o.OrderDate)                       AS LastOrderDate
FROM training.Customers c
LEFT JOIN training.Orders o ON c.CustomerID = o.CustomerID
GROUP BY c.CustomerID, c.FullName;
GO

CREATE VIEW training.vw_ProductSales
AS
SELECT
    p.ProductID,
    p.SKU,
    p.Name                  AS ProductName,
    COUNT(od.DetailID)      AS TimesOrdered,
    ISNULL(SUM(od.LineTotal), 0) AS TotalRevenue
FROM training.Products p
LEFT JOIN training.OrderDetails od ON p.ProductID = od.ProductID
GROUP BY p.ProductID, p.SKU, p.Name;
GO

-- Indexed view regression: keep the source-managed schema binding and unique
-- clustered index through FULL migration. The source table and view column
-- names are schema-qualified as required by SQL Server SCHEMABINDING.
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET ARITHABORT ON;
SET NUMERIC_ROUNDABORT OFF;
GO

CREATE TABLE training.Task4_SourceIV_Base (
    ID int NOT NULL CONSTRAINT PK_Task4_SourceIV_Base PRIMARY KEY CLUSTERED,
    TestValue int NOT NULL
);
INSERT INTO training.Task4_SourceIV_Base (ID, TestValue) VALUES (1, 100), (2, 200);
GO

CREATE VIEW training.Task4_SourceManaged_IV
WITH SCHEMABINDING
AS
SELECT ID, COUNT_BIG(*) AS RowCount
FROM training.Task4_SourceIV_Base
GROUP BY ID;
GO

CREATE UNIQUE CLUSTERED INDEX CIX_Task4_SourceManaged_IV
ON training.Task4_SourceManaged_IV (ID);
GO

PRINT 'Views created.';
