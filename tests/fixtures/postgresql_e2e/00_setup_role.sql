-- PostgreSQL E2E Audit Fixture — Role Setup
-- Run as superuser (postgres) before other fixture scripts.
-- This creates the audit_user role required by grants and RLS verification.

DO
$$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_user') THEN
        CREATE ROLE audit_user LOGIN PASSWORD 'audit_pass';
    END IF;
END
$$;

GRANT USAGE, CREATE ON SCHEMA public TO audit_user;

