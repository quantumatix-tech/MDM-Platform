-- ============================================================
-- PostgreSQL Cloud -> Local Smoke Test
-- Source: Azure PostgreSQL
-- Schema: cloud_to_local_test
-- ============================================================

-- CLEANUP
DROP SCHEMA IF EXISTS cloud_to_local_test CASCADE;
DROP ROLE IF EXISTS cloud_to_local_reader;

-- ROLE
CREATE ROLE cloud_to_local_reader NOLOGIN;

-- SCHEMA
CREATE SCHEMA cloud_to_local_test;

-- ============================================================
-- TABLES
-- ============================================================

CREATE TABLE cloud_to_local_test.customers (
    customer_id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE cloud_to_local_test.products (
    product_id SERIAL PRIMARY KEY,
    product_name VARCHAR(100) NOT NULL UNIQUE,
    price NUMERIC(10,2) NOT NULL CHECK (price >= 0)
);

CREATE TABLE cloud_to_local_test.orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    total_amount NUMERIC(10,2) NOT NULL,

    CONSTRAINT fk_order_customer
        FOREIGN KEY (customer_id)
        REFERENCES cloud_to_local_test.customers(customer_id),

    CONSTRAINT fk_order_product
        FOREIGN KEY (product_id)
        REFERENCES cloud_to_local_test.products(product_id)
);

-- ============================================================
-- DATA
-- ============================================================

INSERT INTO cloud_to_local_test.customers
    (customer_name, email)
VALUES
    ('Cloud Customer 1', 'cloud1@example.com'),
    ('Cloud Customer 2', 'cloud2@example.com'),
    ('Cloud Customer 3', 'cloud3@example.com');

INSERT INTO cloud_to_local_test.products
    (product_name, price)
VALUES
    ('Cloud Product 1', 100.00),
    ('Cloud Product 2', 200.00),
    ('Cloud Product 3', 300.00);

INSERT INTO cloud_to_local_test.orders
    (customer_id, product_id, quantity, total_amount)
VALUES
    (1, 1, 2, 200.00),
    (2, 2, 1, 200.00),
    (3, 3, 3, 900.00);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_orders_customer
    ON cloud_to_local_test.orders(customer_id);

CREATE INDEX idx_orders_product
    ON cloud_to_local_test.orders(product_id);

-- ============================================================
-- VIEW
-- ============================================================

CREATE VIEW cloud_to_local_test.order_summary AS
SELECT
    o.order_id,
    c.customer_name,
    p.product_name,
    o.quantity,
    o.total_amount
FROM cloud_to_local_test.orders o
JOIN cloud_to_local_test.customers c
    ON c.customer_id = o.customer_id
JOIN cloud_to_local_test.products p
    ON p.product_id = o.product_id;

-- ============================================================
-- MATERIALIZED VIEW
-- ============================================================

CREATE MATERIALIZED VIEW cloud_to_local_test.customer_order_totals AS
SELECT
    c.customer_id,
    c.customer_name,
    COUNT(o.order_id) AS order_count,
    COALESCE(SUM(o.total_amount), 0) AS total_amount
FROM cloud_to_local_test.customers c
LEFT JOIN cloud_to_local_test.orders o
    ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.customer_name;

-- ============================================================
-- FUNCTION
-- ============================================================

CREATE OR REPLACE FUNCTION cloud_to_local_test.get_customer_count()
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    result_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO result_count
    FROM cloud_to_local_test.customers;

    RETURN result_count;
END;
$$;

-- ============================================================
-- AUDIT TABLE + PROCEDURE
-- ============================================================

CREATE TABLE cloud_to_local_test.audit_log (
    log_id SERIAL PRIMARY KEY,
    message TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE PROCEDURE cloud_to_local_test.log_message(
    p_message TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO cloud_to_local_test.audit_log(message)
    VALUES (p_message);
END;
$$;

-- ============================================================
-- TRIGGER FUNCTION + TRIGGER
-- ============================================================

CREATE OR REPLACE FUNCTION cloud_to_local_test.update_customer_timestamp()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_customer_updated_at
BEFORE UPDATE ON cloud_to_local_test.customers
FOR EACH ROW
EXECUTE FUNCTION cloud_to_local_test.update_customer_timestamp();

-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON SCHEMA cloud_to_local_test
IS 'Cloud to Local PostgreSQL migration smoke test schema';

COMMENT ON TABLE cloud_to_local_test.customers
IS 'Cloud customer master table';

COMMENT ON COLUMN cloud_to_local_test.customers.customer_name
IS 'Cloud customer full name';

COMMENT ON TABLE cloud_to_local_test.products
IS 'Cloud product master table';

COMMENT ON TABLE cloud_to_local_test.orders
IS 'Cloud customer orders';

COMMENT ON COLUMN cloud_to_local_test.orders.total_amount
IS 'Total order amount';

COMMENT ON VIEW cloud_to_local_test.order_summary
IS 'Cloud to Local order summary view';

COMMENT ON FUNCTION cloud_to_local_test.get_customer_count()
IS 'Returns customer count';

COMMENT ON PROCEDURE cloud_to_local_test.log_message(TEXT)
IS 'Writes a message to the audit log';

COMMENT ON TRIGGER trg_customer_updated_at
ON cloud_to_local_test.customers
IS 'Updates customer updated_at timestamp';

-- ============================================================
-- RLS + POLICY
-- ============================================================

ALTER TABLE cloud_to_local_test.customers
ENABLE ROW LEVEL SECURITY;

CREATE POLICY cloud_to_local_customer_policy
ON cloud_to_local_test.customers
FOR SELECT
USING (customer_id <= 2);

-- ============================================================
-- GRANTS
-- ============================================================

GRANT USAGE
ON SCHEMA cloud_to_local_test
TO cloud_to_local_reader;

GRANT SELECT, INSERT
ON cloud_to_local_test.customers
TO cloud_to_local_reader;

GRANT SELECT
ON cloud_to_local_test.products
TO cloud_to_local_reader;

GRANT SELECT
ON cloud_to_local_test.orders
TO cloud_to_local_reader;

GRANT SELECT
ON cloud_to_local_test.order_summary
TO cloud_to_local_reader;

GRANT SELECT
ON cloud_to_local_test.customer_order_totals
TO cloud_to_local_reader;

GRANT SELECT, INSERT
ON cloud_to_local_test.audit_log
TO cloud_to_local_reader;

-- Explicit sequence privileges
GRANT USAGE, SELECT
ON SEQUENCE cloud_to_local_test.customers_customer_id_seq
TO cloud_to_local_reader;

GRANT USAGE, SELECT
ON SEQUENCE cloud_to_local_test.products_product_id_seq
TO cloud_to_local_reader;

GRANT USAGE, SELECT
ON SEQUENCE cloud_to_local_test.orders_order_id_seq
TO cloud_to_local_reader;

GRANT USAGE, SELECT
ON SEQUENCE cloud_to_local_test.audit_log_log_id_seq
TO cloud_to_local_reader;

GRANT EXECUTE
ON FUNCTION cloud_to_local_test.get_customer_count()
TO cloud_to_local_reader;

GRANT EXECUTE
ON PROCEDURE cloud_to_local_test.log_message(TEXT)
TO cloud_to_local_reader;

-- Column-level grants
GRANT SELECT (customer_name, email)
ON TABLE cloud_to_local_test.customers
TO cloud_to_local_reader;

-- ============================================================
-- SOURCE VERIFICATION
-- ============================================================

SELECT
    'TABLES' AS check_type,
    COUNT(*) AS count
FROM information_schema.tables
WHERE table_schema = 'cloud_to_local_test'
AND table_type = 'BASE TABLE';

SELECT
    'CUSTOMERS' AS object,
    COUNT(*) AS rows
FROM cloud_to_local_test.customers;

SELECT
    'PRODUCTS' AS object,
    COUNT(*) AS rows
FROM cloud_to_local_test.products;

SELECT
    'ORDERS' AS object,
    COUNT(*) AS rows
FROM cloud_to_local_test.orders;

SELECT
    'VIEW' AS object,
    COUNT(*) AS count
FROM pg_views
WHERE schemaname = 'cloud_to_local_test';

SELECT
    'MATVIEW' AS object,
    COUNT(*) AS count
FROM pg_matviews
WHERE schemaname = 'cloud_to_local_test';

SELECT
    'SEQUENCE PRIVILEGES' AS check_type,
    has_sequence_privilege(
        'cloud_to_local_reader',
        'cloud_to_local_test.customers_customer_id_seq',
        'USAGE'
    ) AS usage_privilege,
    has_sequence_privilege(
        'cloud_to_local_reader',
        'cloud_to_local_test.customers_customer_id_seq',
        'SELECT'
    ) AS select_privilege;

SELECT
    'RLS' AS check_type,
    relrowsecurity
FROM pg_class
WHERE oid = 'cloud_to_local_test.customers'::regclass;

SELECT
    polname,
    polcmd
FROM pg_policy
WHERE polrelid = 'cloud_to_local_test.customers'::regclass;

SELECT
    grantee,
    table_name,
    privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'cloud_to_local_test'
AND grantee = 'cloud_to_local_reader';

-- ============================================================
-- END
-- ============================================================