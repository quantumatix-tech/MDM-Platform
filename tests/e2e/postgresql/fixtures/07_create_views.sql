-- PostgreSQL E2E Fixture — 07_create_views.sql
-- A view that aggregates order summary data across Orders and Customers.

CREATE VIEW training.vw_OrderSummary AS
SELECT
    o.OrderID,
    o.OrderNumber,
    c.CustomerCode,
    c.FullName AS CustomerName,
    c.City,
    o.OrderStatus,
    o.TotalAmount,
    COALESCE(od.LineTotal, 0) AS TotalLineItems
FROM training.Orders o
JOIN training.Customers c ON o.CustomerID = c.CustomerID
LEFT JOIN training.OrderDetails od ON od.OrderID = o.OrderID
ORDER BY o.OrderID;

\echo 'View [training.vw_OrderSummary] created.'
