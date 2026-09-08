-- PostgreSQL E2E Audit Fixture — Grants and Privileges
--
-- Requires the audit_user role created by 00_setup_role.sql.
--
-- NOTE: Column-level grants are omitted from this fixture because the
-- current apply_grant() implementation generates incorrect PostgreSQL
-- syntax for column grants (GRANT ... ON COLUMN table.column TO user
-- instead of GRANT privilege (column) ON table TO user).  This is a
-- documented implementation limitation.

-- Schema grants
GRANT USAGE, CREATE ON SCHEMA public TO audit_user;
GRANT USAGE ON SCHEMA audit_test TO audit_user;

-- Table grants (public)
GRANT SELECT, INSERT ON public.customers TO audit_user;
GRANT SELECT ON public.products TO audit_user;
GRANT SELECT, INSERT ON public.orders TO audit_user;

-- Table grants (audit_test)
GRANT SELECT, INSERT ON audit_test.test_customers TO audit_user;
GRANT SELECT ON audit_test.test_orders TO audit_user;
GRANT SELECT ON audit_test.fk_parent TO audit_user;
GRANT SELECT ON audit_test.fk_child TO audit_user;

-- Sequence grants
GRANT USAGE, SELECT ON public.customers_customer_id_seq TO audit_user;
GRANT USAGE, SELECT ON public.products_product_id_seq TO audit_user;
GRANT USAGE, SELECT ON public.orders_order_id_seq TO audit_user;
GRANT USAGE, SELECT ON audit_test.test_orders_order_id_seq TO audit_user;

-- Function grants
GRANT EXECUTE ON FUNCTION public.get_customer_count() TO audit_user;
GRANT EXECUTE ON FUNCTION audit_test.get_customer_count() TO audit_user;
