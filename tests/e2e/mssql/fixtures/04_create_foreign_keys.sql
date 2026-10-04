-- MSSQL E2E Fixture — 04_create_foreign_keys.sql
-- Foreign keys are added after all parent tables exist.

ALTER TABLE training.Orders ADD CONSTRAINT FK_Orders_Customers
    FOREIGN KEY (CustomerID) REFERENCES training.Customers(CustomerID);

ALTER TABLE training.OrderDetails ADD CONSTRAINT FK_OrderDetails_Orders
    FOREIGN KEY (OrderID) REFERENCES training.Orders(OrderID);

ALTER TABLE training.OrderDetails ADD CONSTRAINT FK_OrderDetails_Products
    FOREIGN KEY (ProductID) REFERENCES training.Products(ProductID);

PRINT 'Foreign keys created.';
