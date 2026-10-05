-- MySQL E2E Fixture — 04_create_foreign_keys.sql
-- Foreign keys are created inline in 03_create_tables.sql (MySQL requires
-- the referenced table to exist at CREATE TABLE time). This file is reserved
-- for additional FK operations and outputs a confirmation message.

SELECT 'Foreign keys defined inline in 03_create_tables.sql.' AS 'info';