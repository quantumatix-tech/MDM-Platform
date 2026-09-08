-- PostgreSQL E2E Audit Fixture — Comments

-- Schema comments
COMMENT ON SCHEMA public IS 'Standard public schema for migration E2E audit';
COMMENT ON SCHEMA audit_test IS 'Non-public schema for PostgreSQL object migration audit';

-- Table comments
COMMENT ON TABLE public.customers IS 'Customer master data for E2E audit';
COMMENT ON TABLE public.products IS 'Product catalog for E2E audit';
COMMENT ON TABLE public.orders IS 'Order transactions for E2E audit';
COMMENT ON TABLE audit_test.test_customers IS 'Audit test customers table';
COMMENT ON TABLE audit_test.test_orders IS 'Audit test orders table';
COMMENT ON TABLE audit_test.fk_parent IS 'Parent table for cross-schema FK audit';
COMMENT ON TABLE audit_test.fk_child IS 'Child table for cross-schema FK audit';
COMMENT ON TABLE audit_test.procedure_test_log IS 'Log table for procedure E2E verification';

-- Column comments
COMMENT ON COLUMN public.customers.email IS 'Unique customer email address';
COMMENT ON COLUMN public.customers.status IS 'Customer status enum';
COMMENT ON COLUMN audit_test.test_customers.email IS 'Unique test customer email';

-- View comments
COMMENT ON VIEW public.customer_order_summary IS 'Aggregated customer order statistics';
COMMENT ON VIEW audit_test.order_summary IS 'Audit test order summary view';

-- Materialized view comments
COMMENT ON MATERIALIZED VIEW public.customer_balance_summary IS 'Materialized customer balance summary';
COMMENT ON MATERIALIZED VIEW audit_test.audit_test_customer_summary IS 'Audit test materialized view';

-- Function comments
COMMENT ON FUNCTION public.get_customer_count() IS 'Returns total customer count in public schema';
COMMENT ON FUNCTION audit_test.get_customer_count() IS 'Returns total test customer count';

-- Sequence comments
COMMENT ON SEQUENCE public.customers_customer_id_seq IS 'Primary key sequence for customers';
COMMENT ON SEQUENCE audit_test.test_orders_order_id_seq IS 'Primary key sequence for audit_test.test_orders';
