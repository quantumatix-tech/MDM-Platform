-- MySQL E2E Fixture — 09_create_procedures.sql
-- A stored procedure that updates product stock after an order ships.
-- MySQL procedures can contain COMMIT; no special function-creator flag needed.

DELIMITER $$

CREATE PROCEDURE sp_UpdateProductStock(
    IN p_product_id INT,
    IN p_qty_change INT
)
BEGIN
    UPDATE products
    SET stockqty = stockqty + p_qty_change
    WHERE productid = p_product_id;

    IF ROW_COUNT() = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Product not found';
    END IF;

    COMMIT;
END$$

DELIMITER ;

SELECT 'Procedure [sp_UpdateProductStock] created.' AS 'info';