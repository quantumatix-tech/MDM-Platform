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

PRINT 'Views created.';
