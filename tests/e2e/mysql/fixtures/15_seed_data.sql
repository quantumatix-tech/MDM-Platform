-- MySQL E2E Fixture — 15_seed_data.sql
-- Deterministic test data.  All values are fixed — no random generation.
-- AUTO_INCREMENT columns auto-generate; OrderNumber uses a placeholder
-- value (the sequence is not available in MySQL without a separate table).

-- Temporarily disable FK checks for controlled insert order
SET FOREIGN_KEY_CHECKS = 0;

-- Customers: 5 rows
INSERT INTO customers (customercode, fullname, email, phone, city, status, isactive) VALUES
    ('CUST001', 'Alice Johnson',   'alice@example.com',   '9876543210', 'Mumbai',     'active', TRUE),
    ('CUST002', 'Bob Smith',       'bob@example.com',     '9123456789', 'Delhi',      'active', TRUE),
    ('CUST003', 'Carol White',     'carol@example.com',   '9988776655', 'Bangalore',  'inactive', FALSE),
    ('CUST004', 'David Brown',     'david@example.com',   '9001122334', 'Chennai',    'active', TRUE),
    ('CUST005', 'Eva Green',       'eva@example.com',     '9871234567', 'Hyderabad',   'active', TRUE);

-- Products: 5 rows
INSERT INTO products (sku, name, category, price, stockqty, isactive) VALUES
    ('SKU001', 'Laptop Pro 15',   'Electronics', 75000.00, 50, TRUE),
    ('SKU002', 'Wireless Mouse',  'Electronics',  1200.00, 200, TRUE),
    ('SKU003', 'USB-C Hub',       'Electronics',  2500.00, 150, TRUE),
    ('SKU004', 'Desk Chair',      'Furniture',   12000.00, 30, TRUE),
    ('SKU005', 'Standing Desk',   'Furniture',   25000.00, 20, TRUE);

-- Orders: 5 rows (ordernumber is a simple sequential value)
INSERT INTO orders (ordernumber, customerid, totalamount, orderstatus) VALUES
    (1000, 1, 77400.00, 'delivered'),
    (1001, 1,  3500.00,  'shipped'),
    (1002, 2,  2500.00,  'shipped'),
    (1003, 3, 75000.00,  'delivered'),
    (1004, 4,  8600.00,  'pending');

-- OrderDetails: 8 rows
INSERT INTO orderdetails (orderid, productid, quantity, unitprice) VALUES
    (1, 1, 1, 75000.00),
    (1, 2, 2,  1200.00),
    (2, 3, 1,  2500.00),
    (2, 4, 1,  1000.00),
    (3, 5, 1, 25000.00),
    (4, 1, 1, 75000.00),
    (5, 2, 3,  1200.00),
    (5, 3, 2,  2500.00);

-- PK_Name_Test: 2 rows
INSERT INTO pk_name_test (testname) VALUES ('Test Alpha'), ('Test Beta');

-- PartitionedOrders: 3 rows (spans multiple partitions)
INSERT INTO partitionedorders (orderdate, amount, status) VALUES
    ('2024-06-15', 15000.00, 'shipped'),
    ('2025-02-01', 25000.00, 'delivered'),
    ('2025-08-01', 30000.00, 'pending');

SET FOREIGN_KEY_CHECKS = 1;

SELECT 'Seed data inserted: 5 customers, 5 products, 5 orders, 8 orderdetails, 2 pk_name_test, 3 partitionedorders.' AS 'info';