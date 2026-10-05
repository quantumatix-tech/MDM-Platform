-- MySQL E2E Fixture — 10_create_triggers.sql
-- Audit trigger: inserts a row into OrderAudit when a new order is created.

DELIMITER $$

CREATE TRIGGER tr_AuditOrder
AFTER INSERT ON orders
FOR EACH ROW
BEGIN
    INSERT INTO orderaudit (orderid, action)
    VALUES (NEW.orderid, 'INSERT');
END$$

DELIMITER ;

SELECT 'Trigger [tr_AuditOrder] created on orders.' AS 'info';