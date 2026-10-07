-- PostgreSQL E2E Fixture — 10_create_triggers.sql
-- Audit trigger: inserts a row into OrderAudit when a new order is created.
-- This validates that the trigger fires on the Orders table and populates
-- the dependent OrderAudit table.

-- Trigger function
CREATE OR REPLACE FUNCTION training.trg_AuditOrder()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO training.OrderAudit (OrderID, Action)
    VALUES (NEW.OrderID, 'INSERT');
    RETURN NEW;
END;
$$;

-- Trigger on Orders (fires after INSERT)
CREATE TRIGGER tr_AuditOrder
AFTER INSERT ON training.Orders
FOR EACH ROW
EXECUTE FUNCTION training.trg_AuditOrder();

\echo 'Trigger function and trigger [tr_AuditOrder] created on training.Orders.'
