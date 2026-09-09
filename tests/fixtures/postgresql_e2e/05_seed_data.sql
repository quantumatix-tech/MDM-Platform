-- PostgreSQL E2E Audit Fixture — Deterministic Test Data

-- Public schema
INSERT INTO customers (full_name, email, phone, city, status) VALUES
    ('Alice Johnson',   'alice@example.com',   '9876543210', 'Mumbai',     'active'),
    ('Bob Smith',       'bob@example.com',     '9123456789', 'Delhi',      'active'),
    ('Carol White',     'carol@example.com',   '9988776655', 'Bangalore',  'inactive'),
    ('David Brown',     'david@example.com',   '9001122334', 'Chennai',    'active'),
    ('Eva Green',       'eva@example.com',     '9871234567', 'Hyderabad',  'pending');

INSERT INTO products (name, category, price, stock_qty, is_active) VALUES
    ('Laptop Pro 15',     'Electronics', 75000.00, 50,  true),
    ('Wireless Mouse',    'Electronics',  1200.00, 200, true),
    ('USB-C Hub',         'Electronics',  2500.00, 150, true),
    ('Desk Chair',        'Furniture',   12000.00, 30,  true),
    ('Standing Desk',     'Furniture',   25000.00, 20,  true),
    ('Notebook A4',       'Stationery',    150.00, 500, true),
    ('Blue Pen Set',      'Stationery',     80.00, 1000,true),
    ('Coffee Mug',        'Kitchen',       350.00, 300, true),
    ('Water Bottle',      'Kitchen',       450.00, 250, true),
    ('Desk Lamp',         'Electronics',  1800.00, 100, true);

INSERT INTO orders (customer_id, product_id, quantity, total_amount, status) VALUES
    (1, 1, 1,  75000.00, 'delivered'),
    (1, 2, 2,   2400.00, 'delivered'),
    (2, 3, 1,   2500.00, 'shipped'),
    (3, 4, 1,  12000.00, 'delivered'),
    (4, 5, 1,  25000.00, 'pending'),
    (5, 6, 10,  1500.00, 'delivered'),
    (1, 7, 5,    400.00, 'delivered'),
    (2, 8, 3,   1050.00, 'shipped'),
    (3, 9, 2,    900.00, 'pending'),
    (4, 10,1,   1800.00, 'delivered'),
    (5, 1, 1,  75000.00, 'cancelled'),
    (2, 2, 3,   3600.00, 'delivered');

-- audit_test schema
INSERT INTO audit_test.test_customers (full_name, email, city, public_customer_id) VALUES
    ('Object Test One', 'object1@test.com', 'Indore',  1),
    ('Object Test Two', 'object2@test.com', 'Bhopal',  2),
    ('Object Test Three','object3@test.com','Pune',    3);

INSERT INTO audit_test.test_orders (customer_id, order_date) VALUES
    (1, '2026-09-01 10:00:00'),
    (2, '2026-09-02 11:00:00');

INSERT INTO audit_test.fk_parent (name) VALUES
    ('Parent Alpha'),
    ('Parent Beta');

INSERT INTO audit_test.fk_child (parent_id, public_customer_id) VALUES
    (1, 1),
    (2, 2),
    (1, 3);
