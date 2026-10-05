-- PostgreSQL E2E Fixture — 05_create_indexes.sql
-- Secondary indexes that are NOT backing PK, UNIQUE, or FK constraints.

CREATE INDEX IX_Orders_CustomerID ON training.Orders(CustomerID);
CREATE INDEX IX_Orders_OrderDate ON training.Orders(OrderDate);
CREATE INDEX IX_OrderDetails_OrderID ON training.OrderDetails(OrderID);
CREATE INDEX IX_OrderDetails_ProductID ON training.OrderDetails(ProductID);

\echo 'Indexes created: IX_Orders_CustomerID, IX_Orders_OrderDate, IX_OrderDetails_OrderID, IX_OrderDetails_ProductID.'
