from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.source import DB2SourceConnector


@patch('pyodbc.connect')
def test_unqualified_ambiguous_raises_with_schemas(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    src.connect()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR',), ('SALES',)]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    try:
        src.get_schema('EMPLOYEES')
        assert False, 'should raise'
    except ValueError as e:
        msg = str(e)
        assert 'Ambiguous' in msg
        assert 'HR' in msg
        assert 'SALES' in msg
    src.close()


@patch('pyodbc.connect')
def test_qualified_resolves_correctly(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    src.connect()
    cur = MagicMock()
    cur.fetchone.return_value = None
    cur.fetchall.side_effect = [
        [('EMP_ID', 'INTEGER', 'SYSIBM', 'N', 4, None, 1, ' ', 'N', None), ('EMP_NAME', 'VARCHAR', 'SYSIBM', 'Y', 50, None, 2, ' ', 'N', None)],
        [],  # defaults
        [('EMP_ID',)],
        [],
        [],
        [],
    ]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    schema = src.get_schema('EMPLOYEES', schema_name='HR')
    assert schema.schema_name == 'HR'
    assert schema.primary_key == ['EMP_ID']
    src.close()
