from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.base import Schema
from core.connectors.db2.source import DB2SourceConnector


@patch('pyodbc.connect')
def test_get_schema_basic_columns(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    src.connect()
    cur = MagicMock()
    cur.fetchone.side_effect = None
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
    schema = src.get_schema('EMPLOYEES', schema_name='ADMIN')
    assert isinstance(schema, Schema)
    assert schema.name == 'EMPLOYEES'
    assert schema.schema_name == 'ADMIN'
    assert len(schema.columns) == 2
    assert schema.columns[0].name == 'ID'
    assert schema.columns[0].nullable is False
    assert schema.columns[1].name == 'NAME'
    assert schema.columns[1].nullable is True
    assert schema.primary_key == ['ID']
    src.close()
