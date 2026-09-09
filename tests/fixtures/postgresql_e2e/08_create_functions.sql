-- PostgreSQL E2E Audit Fixture — Functions, Procedures, and Trigger Functions

-- Public schema functions
CREATE OR REPLACE FUNCTION get_customer_count()
RETURNS integer
LANGUAGE sql
AS $$
    SELECT COUNT(*)::INTEGER FROM customers;
$$;

CREATE OR REPLACE FUNCTION update_customer_timestamp()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.created_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

-- audit_test schema functions
CREATE OR REPLACE FUNCTION audit_test.get_customer_count()
RETURNS integer
LANGUAGE sql
AS $$
    SELECT COUNT(*)::INTEGER FROM audit_test.test_customers;
$$;

CREATE OR REPLACE FUNCTION audit_test.update_customer_timestamp()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.created_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

-- Public schema procedure
CREATE OR REPLACE PROCEDURE test_procedure(IN p_message text)
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO procedure_test_log (message, created_at) VALUES (p_message, CURRENT_TIMESTAMP);
END;
$$;

-- audit_test schema procedure
CREATE OR REPLACE PROCEDURE audit_test.test_procedure(IN p_message text)
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO audit_test.procedure_test_log (message, created_at) VALUES (p_message, CURRENT_TIMESTAMP);
END;
$$;
