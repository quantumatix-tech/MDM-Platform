-- MySQL E2E Fixture — 01_create_schemas.sql
-- MySQL databases are namespaces; the E2E database itself serves as the schema.
-- This script verifies we are connected to the correct database.

SELECT DATABASE() AS 'current_database';
SELECT 'MySQL E2E schema is the current database.' AS 'info';
