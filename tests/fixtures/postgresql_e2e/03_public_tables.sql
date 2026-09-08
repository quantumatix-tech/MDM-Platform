-- PostgreSQL E2E Audit Fixture — Public Schema Tables

CREATE TABLE customers (
    customer_id SERIAL PRIMARY KEY,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    phone VARCHAR(20),
    city VARCHAR(100),
    status customer_status DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE products (
    product_id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    category product_category DEFAULT 'General',
    price NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    stock_qty INTEGER DEFAULT 0 CHECK (stock_qty >= 0),
    is_active BOOLEAN DEFAULT true
);

CREATE TABLE orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    total_amount NUMERIC(10, 2),
    status VARCHAR(50) DEFAULT 'pending',
    order_date TIMESTAMP DEFAULT NOW(),
    CONSTRAINT fk_orders_customers FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_orders_products FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE RESTRICT ON UPDATE CASCADE
);
