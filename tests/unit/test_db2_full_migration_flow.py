from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.source import DB2SourceConnector
from core.connectors.db2.target import DB2TargetConnector
from core.connectors.base import Schema, Column


@patch('pyodbc.connect')
def test_schema_qualified_independent(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p', 'migration': {'include_schemas': ['HR', 'SALES']}})
    src.connect()
    cur = MagicMock()
    cur.fetchall.return_value = [('HR', 'EMPLOYEES'), ('SALES', 'EMPLOYEES')]
    src._conn.cursor.return_value.__enter__.return_value = cur
    src._conn.cursor.return_value.__exit__.return_value = False
    pairs = src.list_objects_with_schema()
    assert ('HR', 'EMPLOYEES') in pairs
    assert ('SALES', 'EMPLOYEES') in pairs
    src.close()


@patch('pyodbc.connect')
def test_target_upsert_batch(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    tgt = DB2TargetConnector({'host': 'h', 'port': 25000, 'database': 'DB2TGT', 'username': 'u', 'password': 'p'})
    tgt.connect()
    cur = MagicMock()
    mock_conn.cursor.return_value = cur
    schema = Schema(name='EMPLOYEES', schema_name='HR', columns=[Column(name='EMP_ID', source_type='INTEGER', nullable=False), Column(name='NAME', source_type='VARCHAR', nullable=True)], primary_key=['EMP_ID'])
    rows = [{'EMP_ID': 1, 'NAME': 'A'}, {'EMP_ID': 2, 'NAME': None}]
    res = tgt.upsert_batch('EMPLOYEES', iter(rows), schema=schema)
    assert res.success_count == 2
    tgt.close()
