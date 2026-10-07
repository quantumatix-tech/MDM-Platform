-- PostgreSQL E2E Fixture — 09_create_procedures.sql
-- A stored procedure that updates product stock after an order ships.

CREATE OR REPLACE PROCEDURE training.sp_UpdateProductStock(
    p_product_id INT,
    p_qty_change INT
)
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE training.Products
    SET StockQty = StockQty + p_qty_change
    WHERE ProductID = p_product_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Product % not found', p_product_id;
    END IF;

    COMMIT;
END;
$$;

\echo 'Procedure [training.sp_UpdateProductStock] created.'
