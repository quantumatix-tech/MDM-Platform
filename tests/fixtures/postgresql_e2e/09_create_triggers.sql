-- PostgreSQL E2E Audit Fixture — Triggers

-- Public schema trigger
CREATE TRIGGER trg_customer_timestamp
    BEFORE UPDATE ON public.customers
    FOR EACH ROW
    EXECUTE FUNCTION public.update_customer_timestamp();

-- audit_test schema trigger
CREATE TRIGGER trg_customer_timestamp
    BEFORE UPDATE ON audit_test.test_customers
    FOR EACH ROW
    EXECUTE FUNCTION audit_test.update_customer_timestamp();
