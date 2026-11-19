from unittest.mock import MagicMock

from core.connectors.db2.objects.target_grant import check_existing_db2_grant


def _conn(rows):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = rows[0] if rows else None
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_schema_existing():
    g = {"object_type": "SCHEMA", "object_name": "TEST14", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "CREATEIN"}
    assert check_existing_db2_grant(_conn([("Y",)]), "TEST14", g) == "already_present"


def test_table_existing_grantable():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "SELECT"}
    assert check_existing_db2_grant(_conn([("G",)]), "TEST14", g) == "already_present"


def test_table_missing():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "SELECT"}
    assert check_existing_db2_grant(_conn([("N",)]), "TEST14", g) == "missing"


def test_column_existing():
    g = {"object_type": "COLUMN", "object_name": "EMPLOYEES", "column": "SALARY", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "UPDATE"}
    assert check_existing_db2_grant(_conn([("U","N")]), "TEST14", g) == "already_present"


def test_column_mismatch():
    g = {"object_type": "COLUMN", "object_name": "EMPLOYEES", "column": "SALARY", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "UPDATE"}
    assert check_existing_db2_grant(_conn([]), "TEST14", g) == "missing"


def test_sequence_existing():
    g = {"object_type": "SEQUENCE", "object_name": "SEQ_EMP_ID", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "USAGE"}
    assert check_existing_db2_grant(_conn([("Y",)]), "TEST14", g) == "already_present"


def test_routine_unresolved():
    g = {"object_type": "ROUTINE", "object_name": "FN_DOUBLE", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "EXECUTE"}
    conn = _conn([("Y",), ("Y",)])
    assert check_existing_db2_grant(conn, "TEST14", g) == "unresolved"


def test_unsupported():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "ALTER"}
    assert check_existing_db2_grant(_conn([]), "TEST14", g) == "unsupported"


def test_wrong_grantee_type():
    g = {"object_type": "TABLE", "object_name": "EMPLOYEES", "grantee": "DB2ADMIN", "grantee_type": "G", "privilege": "SELECT"}
    assert check_existing_db2_grant(_conn([]), "TEST14", g) == "missing"
