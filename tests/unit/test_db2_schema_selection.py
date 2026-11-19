from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.source import DB2SourceConnector
from core.connectors.db2.preflight import validate_source_schemas, Db2SchemaNotFoundError, Db2SchemaDuplicateError


@patch('pyodbc.connect')
def test_schema_selection_filters_tables_from_migration_include_schemas(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    cfg = {'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p', 'migration': {'include_schemas': ['HR', 'SALES']}}
    src = DB2SourceConnector(cfg)
    src.connect()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR', 'EMPLOYEES'), ('SALES', 'EMPLOYEES'), ('ADMIN', 'OTHER')]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    pairs = src.list_objects_with_schema()
    assert pairs == [('HR', 'EMPLOYEES'), ('SALES', 'EMPLOYEES')]
    src.close()


def test_preflight_missing_schema():
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR',), ('SALES',)]
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    try:
        validate_source_schemas(conn, ['HR', 'FINANCE'])
        assert False
    except Db2SchemaNotFoundError as e:
        assert 'FINANCE' in str(e)


def test_preflight_multiple_missing():
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR',)]
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    try:
        validate_source_schemas(conn, ['HR', 'FINANCE', 'AUDIT'])
        assert False
    except Db2SchemaNotFoundError as e:
        assert 'FINANCE' in str(e)
        assert 'AUDIT' in str(e)


def test_preflight_duplicate():
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR',)]
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    try:
        validate_source_schemas(conn, ['HR', 'HR'])
        assert False
    except Db2SchemaDuplicateError:
        pass


def test_preflight_no_schemas_raises():
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR',)]
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    try:
        validate_source_schemas(conn, [])
        assert False
    except Exception:
        pass
