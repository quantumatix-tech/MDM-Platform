-- MySQL E2E Fixture — 08_create_functions.sql
-- A scalar function that computes the order total for a given order.
-- MySQL requires log_bin_trust_function_creators=1 or a DEFINER that
-- has appropriate privileges; the fixture creator handles this.

DELIMITER $$

CREATE FUNCTION fn_GetOrderTotal(p_order_id INT)
RETURNS DECIMAL(12,2)
DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE total DECIMAL(12,2);
    SELECT COALESCE(SUM(linetotal), 0) INTO total
    FROM orderdetails
    WHERE orderid = p_order_id;
    RETURN total;
END$$

DELIMITER ;

SELECT 'Function [fn_GetOrderTotal] created.' AS 'info';