-- MySQL E2E Fixture — 14_create_comments.sql
-- MySQL supports table and column comments via the COMMENT clause in DDL.
-- For table-level comments, ALTER TABLE ... COMMENT works.
-- For column comments, we use ALTER TABLE ... MODIFY COLUMN to add the comment.

ALTER TABLE customers COMMENT = 'Customer master data — E2E fixture.';
ALTER TABLE products COMMENT = 'Product catalog — E2E fixture.';
ALTER TABLE orders COMMENT = 'Order header — E2E fixture.';
ALTER TABLE orderdetails COMMENT = 'Order line items — E2E fixture.';
ALTER TABLE orderaudit COMMENT = 'Audit trail populated by trigger — E2E fixture.';
ALTER TABLE pk_name_test COMMENT = 'Verifies named PK constraint discovery — E2E fixture.';
ALTER TABLE partitionedorders COMMENT = 'Range partitioned by order date — E2E fixture.';

-- Column comments require MODIFY COLUMN (the column definition must be repeated fully).
ALTER TABLE customers MODIFY COLUMN email VARCHAR(150) NOT NULL COMMENT 'Unique email address.';
ALTER TABLE orders MODIFY COLUMN ordernumber BIGINT NOT NULL COMMENT 'Populated from AUTO_INCREMENT.';

SELECT 'Comments applied to tables and columns.' AS 'info';