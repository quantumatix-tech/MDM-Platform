from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.source import DB2SourceConnector
from core.connectors.db2.objects import table as db2_table


@patch('pyodbc.connect')
def test_get_object_count_schema_qualified(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p', 'migration': {'include_schemas': ['HR', 'SALES']}})
    src.connect()
    cur = MagicMock()
    cur.fetchone.return_value = (3,)
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    assert src.get_object_count('EMPLOYEES', schema_name='HR') == 3
    assert src.get_object_count('EMPLOYEES', schema_name='SALES') == 3
    src.close()


@patch('pyodbc.connect')
def test_export_full_schema_qualified_independent(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p', 'migration': {'include_schemas': ['HR', 'SALES']}})
    src.connect()
    cur = MagicMock()
    cur.description = [('EMP_ID',), ('EMP_NAME',)]
    cur.__iter__ = MagicMock(return_value=iter([(1, 'A'), (2, 'B')]))
    mock_conn.cursor.return_value = cur
    rows = list(src.export_full('EMPLOYEES', schema_name='HR'))
    assert len(rows) == 2
    assert rows[0]['EMP_ID'] == 1
    src.close()


@patch('pyodbc.connect')
def test_ambiguous_unqualified_raises(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    cur = MagicMock()
    cur.fetchall.return_value = [('HR',), ('SALES',)]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    try:
        db2_table.get_schema_for_table(mock_conn, 'EMPLOYEES', schema_name=None)
        assert False
    except ValueError as e:
        assert 'Ambiguous' in str(e)
