from unittest.mock import MagicMock, patch

from core.connectors.db2.objects.target_grant import plan_db2_grant


def _conn(rows):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = rows[0] if rows else None
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_plan_existing_no_sql():
    g = {"object_type": "SCHEMA", "object_name": "TEST14", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "CREATEIN", "grantable": False}
    r = plan_db2_grant(_conn([("Y",)]), "TEST14", g)
    assert r["status"] == "already_present" and r["sql"] is None


def test_plan_missing_table_sql():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "SELECT", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["status"] == "would_grant"
    assert r["sql"] == "GRANT SELECT ON TABLE TEST14.EMPLOYEES TO USER DB2ADMIN"


def test_plan_column_exact():
    g = {"object_type": "COLUMN", "object_name": "EMPLOYEES", "column": "SALARY", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "UPDATE", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["sql"] == "GRANT UPDATE (SALARY) ON TABLE TEST14.EMPLOYEES TO USER DB2ADMIN"


def test_plan_sequence():
    g = {"object_type": "SEQUENCE", "object_name": "SEQ_EMP_ID", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "USAGE", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["sql"] == "GRANT USAGE ON SEQUENCE TEST14.SEQ_EMP_ID TO USER DB2ADMIN"


def test_plan_function():
    g = {"object_type": "ROUTINE", "object_name": "FN_DOUBLE", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "EXECUTE", "routine_type": "F", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["sql"] == "GRANT EXECUTE ON FUNCTION TEST14.FN_DOUBLE TO USER DB2ADMIN"


def test_plan_procedure():
    g = {"object_type": "ROUTINE", "object_name": "P", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "EXECUTE", "routine_type": "P", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["sql"] == "GRANT EXECUTE ON PROCEDURE TEST14.P TO USER DB2ADMIN"


def test_plan_ambiguous_routine():
    g = {"object_type": "ROUTINE", "object_name": "FN_DOUBLE", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "EXECUTE", "routine_type": "F", "grantable": False}
    conn = _conn([("Y",), ("Y",)])
    r = plan_db2_grant(conn, "TEST14", g)
    assert r["status"] == "unresolved" and r["sql"] is None


def test_plan_public():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "PUBLIC", "grantee_type": "U", "privilege": "SELECT", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["status"] == "approval_required" and r["sql"] is None


def test_plan_unsupported():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "ALTER", "grantable": False}
    r = plan_db2_grant(_conn([]), "TEST14", g)
    assert r["status"] == "unsupported" and r["sql"] is None
