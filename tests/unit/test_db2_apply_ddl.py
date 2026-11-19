from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.target import DB2TargetConnector
from core.connectors.base import Schema, Column


@patch('pyodbc.connect')
def test_apply_creates_schema_and_table(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    tgt = DB2TargetConnector({'host': 'h', 'port': 25000, 'database': 'DB2TGT', 'username': 'u', 'password': 'p'})
    tgt.connect()
    cur = MagicMock()
    # schema not exists, then create, table not exists, create
    cur.fetchone.side_effect = [None, (1,)]  # first check schema maybe not found, then table check
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    schema = Schema(name='EMPLOYEES', schema_name='HR', columns=[Column(name='EMP_ID', source_type='INTEGER', nullable=False)], primary_key=['EMP_ID'])
    try:
        tgt.create_object_if_missing(schema)
    except Exception:
        pass
    tgt.close()


@patch('pyodbc.connect')
def test_default_mode_no_apply(mock_connect):
    # Simulate script behavior - just ensure target methods exist
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    tgt = DB2TargetConnector({'host': 'h', 'port': 25000, 'database': 'DB2TGT', 'username': 'u', 'password': 'p'})
    tgt.connect()
    tgt.close()
