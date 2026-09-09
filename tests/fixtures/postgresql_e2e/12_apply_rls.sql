-- PostgreSQL E2E Audit Fixture — Row Level Security and Policies

-- Public schema: enable RLS on customers with a permissive policy
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;

CREATE POLICY customer_select_policy ON customers
    AS PERMISSIVE FOR SELECT
    USING (true);

CREATE POLICY customer_insert_policy ON customers
    AS PERMISSIVE FOR INSERT
    WITH CHECK (true);

-- audit_test schema: enable RLS on test_customers with a permissive policy
ALTER TABLE audit_test.test_customers ENABLE ROW LEVEL SECURITY;

CREATE POLICY test_customer_select_policy ON audit_test.test_customers
    AS PERMISSIVE FOR SELECT
    USING (true);

CREATE POLICY test_customer_insert_policy ON audit_test.test_customers
    AS PERMISSIVE FOR INSERT
    WITH CHECK (true);
