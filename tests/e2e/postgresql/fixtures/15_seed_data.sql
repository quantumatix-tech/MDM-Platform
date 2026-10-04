-- PostgreSQL E2E Fixture — 15_seed_data.sql
-- Deterministic test data.  All values are fixed — no random generation.
-- Identity columns auto-generate; OrderNumber comes from the sequence.

-- Customers: 5 rows
INSERT INTO training.Customers (CustomerCode, FullName, Email, Phone, City, Status, IsActive) VALUES
    ('CUST001', 'Alice Johnson',   'alice@example.com',   '9876543210', 'Mumbai',     'active', TRUE),
    ('CUST002', 'Bob Smith',       'bob@example.com',     '9123456789', 'Delhi',      'active', TRUE),
    ('CUST003', 'Carol White',     'carol@example.com',   '9988776655', 'Bangalore',  'inactive', FALSE),
    ('CUST004', 'David Brown',     'david@example.com',   '9001122334', 'Chennai',    'active', TRUE),
    ('CUST005', 'Eva Green',       'eva@example.com',     '9871234567', 'Hyderabad',  'active', TRUE);

-- Products: 5 rows
INSERT INTO training.Products (SKU, Name, Category, Price, StockQty, IsActive) VALUES
    ('SKU001', 'Laptop Pro 15',   'Electronics', 75000.00, 50, TRUE),
    ('SKU002', 'Wireless Mouse',  'Electronics',  1200.00, 200, TRUE),
    ('SKU003', 'USB-C Hub',       'Electronics',  2500.00, 150, TRUE),
    ('SKU004', 'Desk Chair',      'Furniture',   12000.00, 30, TRUE),
    ('SKU005', 'Standing Desk',   'Furniture',   25000.00, 20, TRUE);

-- Orders: 5 rows (OrderNumber from sequence, CustomerID references Customers)
INSERT INTO training.Orders (OrderNumber, CustomerID, TotalAmount, OrderStatus) VALUES
    (NEXTVAL('training.Seq_OrderNumber'), 1, 77400.00, 'delivered'),
    (NEXTVAL('training.Seq_OrderNumber'), 1,  3500.00,   'shipped'),
    (NEXTVAL('training.Seq_OrderNumber'), 2,  2500.00,   'shipped'),
    (NEXTVAL('training.Seq_OrderNumber'), 3, 75000.00,  'delivered'),
    (NEXTVAL('training.Seq_OrderNumber'), 4,  8600.00,  'pending');

-- OrderDetails: 8 rows (Quantity * UnitPrice = LineTotal generated column)
INSERT INTO training.OrderDetails (OrderID, ProductID, Quantity, UnitPrice) VALUES
    (1, 1, 1, 75000.00),
    (1, 2, 2,  1200.00),
    (2, 3, 1,  2500.00),
    (2, 4, 1,  1000.00),
    (3, 5, 1, 25000.00),
    (4, 1, 1, 75000.00),
    (5, 2, 3,  1200.00),
    (5, 3, 2,  2500.00);

-- PK_Name_Test: 2 rows
INSERT INTO training.PK_Name_Test (TestName) VALUES ('Test Alpha'), ('Test Beta');

-- PartitionedOrders: 3 rows (spans multiple partitions)
INSERT INTO training.PartitionedOrders (OrderDate, Amount, Status) VALUES
    ('2024-06-15', 15000.00, 'shipped'),
    ('2025-02-01', 25000.00, 'delivered'),
    ('2025-08-01', 30000.00, 'pending');

\echo 'Seed data inserted: 5 Customers, 5 Products, 5 Orders, 8 OrderDetails, 2 PK_Name_Test, 3 PartitionedOrders.'
