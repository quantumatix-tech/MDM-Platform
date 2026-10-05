-- MySQL E2E Fixture — 05_create_indexes.sql
-- Secondary indexes that are NOT backing PK, UNIQUE, or FK constraints.

CREATE INDEX idx_orders_customerid ON orders(customerid);
CREATE INDEX idx_orders_orderdate ON orders(orderdate);
CREATE INDEX idx_orderdetails_orderid ON orderdetails(orderid);
CREATE INDEX idx_orderdetails_productid ON orderdetails(productid);

SELECT 'Indexes created: idx_orders_customerid, idx_orders_orderdate, idx_orderdetails_orderid, idx_orderdetails_productid.' AS 'info';