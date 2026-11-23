from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.objects import table as db2_table
from core.connectors.db2.source import DB2SourceConnector


@patch('pyodbc.connect')
def test_list_objects_with_schema_preserves_identity(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    src.connect()
    cur = MagicMock()
    cur.fetchall.return_value = [('SALES', 'EMPLOYEES'), ('HR', 'EMPLOYEES'), ('HR', 'DEPARTMENTS')]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    pairs = src.list_objects_with_schema()
    assert ('SALES', 'EMPLOYEES') in pairs
    assert ('HR', 'EMPLOYEES') in pairs
    assert ('HR', 'DEPARTMENTS') in pairs
    # list_objects should not deduplicate across schemas by name alone
    names = src.list_objects()
    assert 'EMPLOYEES' in names
    src.close()


@patch('pyodbc.connect')
def test_get_schema_schema_qualified(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    src.connect()
    cur = MagicMock()
    cur.fetchone.return_value = None  # no ambiguity resolution needed if schema passed
    cur.fetchall.side_effect = [
        [('ID', 'INTEGER', 'SYSIBM', 'N', 4, None, 1, ' ', 'N', None), ('NAME', 'VARCHAR', 'SYSIBM', 'Y', 50, None, 2, ' ', 'N', None)],
        [],  # defaults
        [('ID',)],
        [],
        [],
        [],
    ]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    schema = src.get_schema('EMPLOYEES', schema_name='HR')
    assert schema.schema_name == 'HR'
    assert schema.primary_key == ['ID']
    assert len(schema.columns) == 2
    src.close()


@patch('pyodbc.connect')
def test_ambiguous_table_raises(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    cur = MagicMock()
    cur.fetchone.side_effect = None
    cur.fetchall.return_value = [('SALES',), ('HR',)]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    try:
        db2_table.get_schema_for_table(mock_conn, 'EMPLOYEES', schema_name=None)
        assert False, 'should raise'
    except ValueError as e:
        assert 'Ambiguous' in str(e)
