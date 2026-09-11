-- PostgreSQL Azure Cloud Test — cloud_test schema tables and data
-- Phase 1: Tables + Constraints

-- Products table
CREATE TABLE cloud_test.products (
    product_id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    category VARCHAR(100) DEFAULT 'General',
    price NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    stock_qty INTEGER DEFAULT 0 CHECK (stock_qty >= 0),
    is_active BOOLEAN DEFAULT true
);

-- Orders table
CREATE TABLE cloud_test.orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES cloud_test.customers(customer_id),
    product_id INTEGER NOT NULL REFERENCES cloud_test.products(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    total_amount NUMERIC(10, 2),
    status VARCHAR(50) DEFAULT 'pending',
    order_date TIMESTAMP DEFAULT NOW()
);

-- Seed products
INSERT INTO cloud_test.products (name, category, price, stock_qty) VALUES
    ('Laptop', 'Electronics', 999.99, 50),
    ('Mouse', 'Electronics', 29.99, 200),
    ('Keyboard', 'Electronics', 79.99, 150),
    ('Monitor', 'Electronics', 299.99, 75),
    ('Desk Chair', 'Furniture', 199.99, 30),
    ('Standing Desk', 'Furniture', 499.99, 15),
    ('Notebook', 'Stationery', 4.99, 500),
    ('Pen Set', 'Stationery', 9.99, 300),
    ('Headphones', 'Electronics', 149.99, 100),
    ('Webcam', 'Electronics', 89.99, 60);

-- Seed orders
INSERT INTO cloud_test.orders (customer_id, product_id, quantity, total_amount, status) VALUES
    (1, 1, 1, 999.99, 'pending'),
    (1, 2, 2, 59.98, 'shipped'),
    (2, 3, 1, 79.99, 'pending'),
    (2, 4, 1, 299.99, 'delivered'),
    (3, 5, 2, 399.98, 'pending'),
    (3, 6, 1, 499.99, 'shipped'),
    (1, 7, 10, 49.90, 'pending'),
    (2, 8, 5, 49.95, 'delivered'),
    (3, 9, 1, 149.99, 'pending'),
    (1, 10, 1, 89.99, 'shipped'),
    (2, 1, 1, 999.99, 'pending'),
    (3, 3, 1, 79.99, 'delivered');
