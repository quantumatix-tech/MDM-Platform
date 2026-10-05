-- PostgreSQL E2E Fixture — 08_create_functions.sql
-- A scalar function that computes the order total for a given order.

CREATE OR REPLACE FUNCTION training.fn_GetOrderTotal(p_order_id INT)
RETURNS NUMERIC(12,2)
LANGUAGE plpgsql
AS $$
DECLARE
    total NUMERIC(12,2);
BEGIN
    SELECT COALESCE(SUM(LineTotal), 0) INTO total
    FROM training.OrderDetails
    WHERE OrderID = p_order_id;

    RETURN total;
END;
$$;

\echo 'Function [training.fn_GetOrderTotal] created.'
