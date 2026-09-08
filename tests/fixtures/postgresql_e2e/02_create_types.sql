-- PostgreSQL E2E Audit Fixture — Custom Types (public schema only)
--
-- NOTE: The current create_type() implementation hardcodes the public
-- schema in its existence check.  Types are therefore created in the
-- public schema only.  Non-public type migration is a documented
-- limitation tracked in POSTGRESQL_LIMITATIONS.md.
--
-- NOTE: Domain DDL generation currently includes auto-generated NOT NULL
-- check constraints from the catalog, which produces invalid SQL.
-- The fixture uses a simple domain without NOT NULL or CHECK constraints
-- to stay within the current implementation's safe boundary.

CREATE TYPE customer_status AS ENUM ('active', 'inactive', 'pending');

CREATE DOMAIN product_category AS VARCHAR(100) DEFAULT 'General';
