-- MSSQL E2E Fixture — 05_create_indexes.sql
-- Non-clustered indexes (PK/clustered indexes are created inline with tables).
-- Unique constraints on tables create unique non-clustered indexes automatically.

CREATE NONCLUSTERED INDEX IX_Customers_City
    ON training.Customers (City)
    WHERE IsActive = 1;

CREATE NONCLUSTERED INDEX IX_Products_Category
    ON training.Products (Category);

CREATE NONCLUSTERED INDEX IX_Orders_CustomerID
    ON training.Orders (CustomerID);

CREATE NONCLUSTERED INDEX IX_OrderDetails_ProductID
    ON training.OrderDetails (ProductID)
    INCLUDE (Quantity, UnitPrice);

PRINT 'Indexes created.';
