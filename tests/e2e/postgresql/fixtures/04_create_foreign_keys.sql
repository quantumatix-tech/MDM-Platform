-- PostgreSQL E2E Fixture — 04_create_foreign_keys.sql
-- Foreign key constraints referencing the tables created in 03_create_tables.sql.
-- Created separately because all referenced tables must exist first.

ALTER TABLE training.Orders
    ADD CONSTRAINT FK_Orders_Customers
    FOREIGN KEY (CustomerID) REFERENCES training.Customers(CustomerID);

ALTER TABLE training.OrderDetails
    ADD CONSTRAINT FK_OrderDetails_Orders
    FOREIGN KEY (OrderID) REFERENCES training.Orders(OrderID);

ALTER TABLE training.OrderDetails
    ADD CONSTRAINT FK_OrderDetails_Products
    FOREIGN KEY (ProductID) REFERENCES training.Products(ProductID);

\echo 'Foreign keys created: FK_Orders_Customers, FK_OrderDetails_Orders, FK_OrderDetails_Products.'
