-- MSSQL E2E Fixture — 10_create_triggers.sql
-- Two triggers: one AFTER INSERT (enabled), one AFTER UPDATE (disabled).
-- CREATE TRIGGER must be the only statement in its batch (requires GO).

-- Trigger: on Orders insert, log an audit row.
CREATE TRIGGER training.trg_Orders_Insert
ON training.Orders
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO training.OrderAudit (OrderID, Action)
    SELECT i.OrderID, 'INSERT'
    FROM inserted i;
END;
GO

-- Trigger: on OrderDetails update, log audit (intentionally disabled to
-- verify that disabled-state is preserved by migration).
CREATE TRIGGER training.trg_OrderDetails_Audit
ON training.OrderDetails
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO training.OrderAudit (OrderID, Action)
    SELECT i.OrderID, 'UPDATE'
    FROM inserted i;
END;
GO

-- Disable the second trigger to test disabled-state preservation.
ALTER TABLE training.OrderDetails DISABLE TRIGGER trg_OrderDetails_Audit;
GO

PRINT 'Triggers created (trg_Orders_Insert enabled, trg_OrderDetails_Audit disabled).';
