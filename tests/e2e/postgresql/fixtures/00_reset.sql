-- PostgreSQL E2E Fixture — 00_reset.sql
-- Drops and recreates the training schema so the fixture is reproducible
-- on re-runs without a full database reset.

DROP SCHEMA IF EXISTS training CASCADE;

CREATE SCHEMA training;

-- PostgreSQL has no PRINT statement; RAISE NOTICE outputs to the psql
-- console (stderr by default) when running with -f.
\echo 'Schema [training] reset.'
