-- PostgreSQL E2E Fixture — 14_create_comments.sql
-- PostgreSQL uses COMMENT ON ... for metadata annotations.
-- (MSSQL equivalent is extended properties in 14_apply_extended_props.sql.)

COMMENT ON TABLE training.Customers IS 'Customer master data — E2E fixture.';
COMMENT ON TABLE training.Products IS 'Product catalog — E2E fixture.';
COMMENT ON TABLE training.Orders IS 'Order header — E2E fixture.';
COMMENT ON TABLE training.OrderDetails IS 'Order line items — E2E fixture.';
COMMENT ON TABLE training.OrderAudit IS 'Audit trail populated by trigger — E2E fixture.';
COMMENT ON TABLE training.PK_Name_Test IS 'Verifies named PK constraint discovery — E2E fixture.';
COMMENT ON TABLE training.PartitionedOrders IS 'Range partitioned by order date — E2E fixture.';

COMMENT ON COLUMN training.Customers.Email IS 'Unique email address.';
COMMENT ON COLUMN training.Orders.OrderNumber IS 'Populated from training.Seq_OrderNumber.';

COMMENT ON SEQUENCE training.Seq_OrderNumber IS 'Generator for Orders.OrderNumber.';

\echo 'Comments applied to tables, columns, and sequences.'
