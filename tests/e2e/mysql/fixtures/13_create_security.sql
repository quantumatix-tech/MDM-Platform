-- MySQL E2E Fixture — 13_create_security.sql
-- MySQL has no row-level security (RLS) policies and no GRANT-based role model
-- equivalent to PostgreSQL's.  The security_users migration path maps MySQL
-- accounts and their direct grants, but for E2E we skip account creation to
-- avoid duplicating source credentials on the local test instance.
--
-- MySQL native accounts (root@localhost) are used for the migration.

SELECT 'MySQL has no RLS policies; security is managed via accounts/grants.' AS 'info';