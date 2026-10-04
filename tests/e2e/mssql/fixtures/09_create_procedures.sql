-- MSSQL E2E Fixture — 09_create_procedures.sql
-- Stored procedure that joins Customers and Orders.
-- CREATE PROCEDURE must be the only statement in its batch (requires GO).

CREATE PROCEDURE training.usp_GetCustomerOrders
    @customer_id INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        c.CustomerID,
        c.FullName,
        c.Email,
        o.OrderID,
        o.OrderNumber,
        o.TotalAmount,
        o.Status,
        o.OrderDate
    FROM training.Customers c
    JOIN training.Orders o ON c.CustomerID = o.CustomerID
    WHERE c.CustomerID = @customer_id;
END;
GO

PRINT 'Stored procedure [training.usp_GetCustomerOrders] created.';
