-- MySQL E2E Fixture — 07_create_views.sql
-- A view that aggregates order summary data across Orders and Customers.

CREATE VIEW vw_OrderSummary AS
SELECT
    o.orderid,
    o.ordernumber,
    c.customercode,
    c.fullname AS customername,
    c.city,
    o.orderstatus,
    o.totalamount,
    COALESCE(od.linetotal, 0) AS totallineitems
FROM orders o
JOIN customers c ON o.customerid = c.customerid
LEFT JOIN orderdetails od ON od.orderid = o.orderid
ORDER BY o.orderid;

SELECT 'View [vw_OrderSummary] created.' AS 'info';