-- MSSQL E2E Fixture — 08_create_functions.sql
-- Scalar function and inline table-valued function (ITVF).
-- Each function must be the only statement in its batch (requires GO).

-- Scalar function: calculate 8 % tax on an amount.
CREATE FUNCTION training.fn_CalculateTax(@amount DECIMAL(10,2))
RETURNS DECIMAL(10,2)
AS
BEGIN
    RETURN @amount * 0.08;
END;
GO

-- Inline table-valued function: orders for a given customer.
CREATE FUNCTION training.fn_CustomerOrderStats(@customer_id INT)
RETURNS TABLE
AS
RETURN (
    SELECT
        o.OrderID,
        o.OrderNumber,
        o.TotalAmount,
        o.Status
    FROM training.Orders o
    WHERE o.CustomerID = @customer_id
);
GO

PRINT 'Functions created.';
