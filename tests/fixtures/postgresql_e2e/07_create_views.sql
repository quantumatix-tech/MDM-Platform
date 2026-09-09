-- PostgreSQL E2E Audit Fixture — Views and Materialized Views

-- Public schema views
CREATE VIEW public.customer_order_summary AS
SELECT
    c.customer_id,
    c.full_name,
    c.city,
    COUNT(o.order_id) AS order_count,
    COALESCE(SUM(o.total_amount), 0) AS total_spent
FROM public.customers c
LEFT JOIN public.orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.full_name, c.city
ORDER BY c.customer_id;

-- audit_test schema views
CREATE VIEW audit_test.order_summary AS
SELECT
    tc.customer_id,
    tc.full_name,
    COUNT(tord.order_id) AS order_count
FROM audit_test.test_customers tc
LEFT JOIN audit_test.test_orders tord ON tc.customer_id = tord.customer_id
GROUP BY tc.customer_id, tc.full_name
ORDER BY tc.customer_id;

-- Public schema materialized views
CREATE MATERIALIZED VIEW public.customer_balance_summary AS
SELECT
    c.customer_id,
    c.full_name,
    COUNT(o.order_id) AS order_count
FROM public.customers c
LEFT JOIN public.orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.full_name
ORDER BY c.customer_id;

-- audit_test schema materialized views
CREATE MATERIALIZED VIEW audit_test.audit_test_customer_summary AS
SELECT
    tc.customer_id,
    tc.full_name,
    tc.city,
    COUNT(tord.order_id) AS order_count
FROM audit_test.test_customers tc
LEFT JOIN audit_test.test_orders tord ON tc.customer_id = tord.customer_id
GROUP BY tc.customer_id, tc.full_name, tc.city
ORDER BY tc.customer_id;
