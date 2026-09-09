-- PostgreSQL E2E Audit Fixture — Drop and Recreate Schemas
-- Safe to run repeatedly; preserves the public schema but removes all objects.

DROP SCHEMA IF EXISTS audit_test CASCADE;
CREATE SCHEMA audit_test;

-- Clean public schema objects without dropping the schema itself.
DO
$$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
    ) LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT matviewname FROM pg_matviews
        WHERE schemaname = 'public'
    ) LOOP
        EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS public.' || quote_ident(r.matviewname) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT viewname FROM pg_views
        WHERE schemaname = 'public'
    ) LOOP
        EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT typname FROM pg_type
        JOIN pg_namespace n ON pg_type.typnamespace = n.oid
        WHERE n.nspname = 'public'
          AND pg_type.typtype IN ('e', 'd')
    ) LOOP
        EXECUTE 'DROP TYPE IF EXISTS public.' || quote_ident(r.typname) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS sig
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'public'
          AND p.prokind IN ('f', 'p')
    ) LOOP
        EXECUTE 'DROP FUNCTION IF EXISTS public.' || quote_ident(r.sig) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT tgname, c.relname
        FROM pg_trigger t
        JOIN pg_class c ON t.tgrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public'
          AND NOT t.tgisinternal
    ) LOOP
        EXECUTE 'DROP TRIGGER IF EXISTS ' || quote_ident(r.tgname) || ' ON public.' || quote_ident(r.relname) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT sequencename FROM pg_sequences
        WHERE schemaname = 'public'
    ) LOOP
        EXECUTE 'DROP SEQUENCE IF EXISTS public.' || quote_ident(r.sequencename) || ' CASCADE';
    END LOOP;

    FOR r IN (
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname NOT LIKE '%_pkey'
    ) LOOP
        EXECUTE 'DROP INDEX IF EXISTS public.' || quote_ident(r.indexname) || ' CASCADE';
    END LOOP;
END
$$;
