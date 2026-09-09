-- PostgreSQL E2E Audit Fixture — Non-Public Schema Tables (audit_test)

CREATE TABLE audit_test.test_customers (
    customer_id SERIAL PRIMARY KEY,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    city VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW(),
    public_customer_id INTEGER REFERENCES public.customers(customer_id)
);

CREATE TABLE audit_test.test_orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES audit_test.test_customers(customer_id),
    order_date TIMESTAMP DEFAULT NOW()
);

CREATE TABLE audit_test.fk_parent (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL
);

CREATE TABLE audit_test.fk_child (
    id SERIAL PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES audit_test.fk_parent(id) ON DELETE CASCADE,
    public_customer_id INTEGER NOT NULL REFERENCES public.customers(customer_id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE audit_test.procedure_test_log (
    id SERIAL PRIMARY KEY,
    message TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
