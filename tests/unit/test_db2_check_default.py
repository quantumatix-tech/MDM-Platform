from unittest.mock import MagicMock

from core.connectors.db2.objects.check_default import (
    discover_check_constraints,
    discover_column_defaults,
)
from core.connectors.db2.objects.target_check_default import (
    apply_planned_check,
    apply_planned_default,
    plan_check,
    plan_default,
)


def _conn(rows):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = rows[0] if rows else None
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn

def _conn_with_side_effect(side_effect):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = side_effect
    cur.fetchall.side_effect = side_effect
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn

def test_discover_checks():
    conn = _conn([("CK1", "T", "SALARY >= 0", "Y")])
    res = discover_check_constraints(conn, "TEST16")
    assert len(res) == 1

def test_discover_defaults():
    conn = _conn([("T", "SALARY", "1000.00")])
    res = discover_column_defaults(conn, "TEST16")
    assert len(res) == 1

def test_plan_default_missing():
    conn = _conn([(None,)])
    # need table/col exists
    conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [("T",), ("C",), (None,)]
    res = plan_default(conn, "TEST16", "T", "C", "1000.00")
    assert res["status"] in ("would_apply", "already_present", "object_missing")

def test_plan_check_missing():
    conn = _conn([])
    conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [("T",), None]
    res = plan_check(conn, "TEST16", "T", "CK1", "SALARY >= 0")
    assert res["status"] in ("would_apply", "already_present", "object_missing", "conflict")

def test_plan_default_already_present():
    """Test that plan_default returns already_present when target matches source."""
    conn = _conn([(None,)])
    conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        ("T",),  # table exists
        ("C",),  # column exists
        ("1000.00",),  # current default
    ]
    res = plan_default(conn, "TEST16", "T", "C", "1000.00")
    assert res["status"] == "already_present"
    assert res["reason"] == "default matches"

def test_plan_default_conflict():
    """Test that plan_default returns conflict when target has different default."""
    conn = _conn([(None,)])
    conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        ("T",),  # table exists
        ("C",),  # column exists
        ("2000.00",),  # current default differs from source
    ]
    res = plan_default(conn, "TEST16", "T", "C", "1000.00")
    assert res["status"] == "conflict"
    assert res["reason"] == "target default differs"

def test_apply_planned_default_exception_captures_sqlstate():
    """Test that apply_planned_default captures SQLSTATE from pyodbc exception."""
    import pyodbc
    conn = _conn([(None,)])
    cur = conn.cursor.return_value.__enter__.return_value
    # Need enough fetchone calls for: initial plan_default (3) + recheck (3) = 6
    cur.fetchone.side_effect = [
        ("T",),  # table exists - initial
        ("C",),  # column exists - initial
        (None,),  # current default is NULL - initial
        ("T",),  # table exists - recheck
        ("C",),  # column exists - recheck
        (None,),  # current default is NULL - recheck
    ]
    # Simulate pyodbc exception with SQLSTATE on the ALTER TABLE execute
    # pyodbc.Error typically has args[0]=SQLSTATE, args[1]=message
    exc = pyodbc.Error("42000", "Syntax error near SET DEFAULT")
    def execute_side_effect(sql, *args):
        if "ALTER TABLE" in sql:
            raise exc
    cur.execute.side_effect = execute_side_effect

    res = apply_planned_default(conn, "TEST16", "T", "C", "1000.00")
    assert res["status"] == "failed"
    assert "SQLSTATE=42000" in res["reason"]
    assert "Syntax error" in res["reason"]

def test_apply_planned_check_exception_captures_sqlstate():
    """Test that apply_planned_check captures SQLSTATE from pyodbc exception."""
    import pyodbc
    conn = _conn([(None,)])
    cur = conn.cursor.return_value.__enter__.return_value
    # Need enough fetchone calls for: initial plan_check (2) + recheck (2) = 4
    cur.fetchone.side_effect = [
        ("T",),  # table exists - initial
        None,    # check doesn't exist - initial
        ("T",),  # table exists - recheck
        None,    # check doesn't exist - recheck
    ]
    exc = pyodbc.Error("42809", "Constraint already exists")
    def execute_side_effect(sql, *args):
        if "ALTER TABLE" in sql:
            raise exc
    cur.execute.side_effect = execute_side_effect

    res = apply_planned_check(conn, "TEST16", "T", "CK1", "SALARY >= 0")
    assert res["status"] == "failed"
    assert "SQLSTATE=42809" in res["reason"]
    assert "Constraint already exists" in res["reason"]
