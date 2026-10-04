-- PostgreSQL E2E Fixture — 13_create_security.sql
-- Row-Level Security (RLS) policy and a role-based grant.
-- The customers table is restricted so that only rows matching the
-- current session's application context are visible.

-- Enable RLS on Customers
ALTER TABLE training.Customers ENABLE ROW LEVEL SECURITY;

-- Policy: users see only customers in their assigned city.
-- The city is passed via the myapp.current_city GUC (custom variable).
CREATE POLICY p_customers_city_isolation
    ON training.Customers
    FOR ALL
    USING (City = current_setting('myapp.current_city', true))
    WITH CHECK (City = current_setting('myapp.current_city', true));

-- Create a migration role and grant table access to it.
-- PostgreSQL 17 does not support CREATE ROLE IF NOT EXISTS, so
-- we use a DO block with a conditional guard.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'migration_role') THEN
        CREATE ROLE migration_role LOGIN PASSWORD 'e2e_migration_role_only';
    END IF;
END$$;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA training TO migration_role;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA training TO migration_role;
GRANT USAGE ON SCHEMA training TO migration_role;

\echo 'Security: RLS policy [p_customers_city_isolation] on Customers, role [migration_role] created.'
