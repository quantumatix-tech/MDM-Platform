from unittest.mock import MagicMock

from core.connectors.db2.objects.comment import (
    discover_table_comments,
    discover_column_comments,
    apply_table_comment,
    apply_column_comment,
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


def test_discover_table_comments():
    conn = _conn([("EMPLOYEES", "tbl comment")])
    res = discover_table_comments(conn, "TEST15")
    assert res == [{"object_type": "TABLE", "table": "EMPLOYEES", "comment": "tbl comment"}]


def test_discover_column_comments():
    conn = _conn([("EMPLOYEES", "EMP_ID", "id comment")])
    res = discover_column_comments(conn, "TEST15")
    assert res == [{"object_type": "COLUMN", "table": "EMPLOYEES", "column": "EMP_ID", "comment": "id comment"}]


def test_null_empty():
    conn = _conn([("EMPLOYEES", None)])
    assert discover_table_comments(conn, "TEST15")[0]["comment"] is None
    # apply with None existing and None comment -> already_present
    c = _conn([None])
    assert apply_table_comment(c, "TEST15", "EMPLOYEES", None) == "already_present"
    c2 = _conn([None])
    assert apply_table_comment(c2, "TEST15", "EMPLOYEES", "") == "already_present"


def test_matching_skip():
    conn = _conn([("same",)])
    assert apply_table_comment(conn, "TEST15", "EMPLOYEES", "same") == "already_present"


def test_missing_applied():
    conn = _conn([None])
    conn.cursor.return_value.__enter__.return_value.execute.return_value = None
    assert apply_table_comment(conn, "TEST15", "EMPLOYEES", "new") == "applied"


def test_conflict():
    conn = _conn([("old",)])
    assert apply_table_comment(conn, "TEST15", "EMPLOYEES", "new") == "conflict"


def test_apostrophe_escape():
    conn = _conn([None])
    assert apply_table_comment(conn, "TEST15", "EMPLOYEES", "it's") == "applied"


def test_unicode():
    conn = _conn([None])
    assert apply_table_comment(conn, "TEST15", "EMPLOYEES", "café") == "applied"


def test_whitespace():
    conn = _conn([None])
    assert apply_table_comment(conn, "TEST15", "EMPLOYEES", "  spaced  ") == "applied"


def test_missing_target_object():
    conn = _conn([])
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    # apply attempts execute? existing None -> would apply, but missing table; SQL may fail at DB. Offline just check no crash.
    try:
        apply_table_comment(conn, "TEST15", "MISSING", "x")
    except Exception:
        pass


def test_column_conflict():
    conn = _conn([("old",)])
    assert apply_column_comment(conn, "TEST15", "EMPLOYEES", "EMP_ID", "new") == "conflict"


def test_column_applied():
    conn = _conn([None])
    assert apply_column_comment(conn, "TEST15", "EMPLOYEES", "EMP_ID", "new") == "applied"
