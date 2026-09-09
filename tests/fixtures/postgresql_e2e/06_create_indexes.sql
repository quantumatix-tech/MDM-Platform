-- PostgreSQL E2E Audit Fixture — Indexes

-- Public schema
CREATE INDEX idx_customers_email ON public.customers(email);
CREATE INDEX idx_customers_city ON public.customers(city);
CREATE INDEX idx_orders_status ON public.orders(status);
CREATE INDEX idx_orders_date ON public.orders(order_date);
CREATE UNIQUE INDEX idx_products_name ON public.products(name);

-- audit_test schema
CREATE INDEX idx_test_customers_email ON audit_test.test_customers(email);
CREATE INDEX idx_test_orders_date ON audit_test.test_orders(order_date);
CREATE INDEX idx_fk_child_public_customer ON audit_test.fk_child(public_customer_id);
