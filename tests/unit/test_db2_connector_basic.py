from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors import DB2SourceConnector, DB2TargetConnector
from core.connectors.db2 import DB2SourceConnector as DB2Src, DB2TargetConnector as DB2Tgt


def test_db2_imports_and_registration():
    assert DB2SourceConnector is DB2Src
    assert DB2TargetConnector is DB2Tgt


@patch('pyodbc.connect')
def test_db2_source_mock_connect(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    src = DB2SourceConnector({
        'host': '127.0.0.1',
        'port': 25000,
        'database': 'DBPRACT',
        'username': 'db2admin',
        'password': 'test'
    })
    src.connect()
    assert src._conn is mock_conn
    src.close()
    assert src._conn is None


@patch('pyodbc.connect')
def test_db2_target_mock_connect(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    tgt = DB2TargetConnector({
        'host': '127.0.0.1',
        'port': 25000,
        'database': 'DBPRACT',
        'username': 'db2admin',
        'password': 'test'
    })
    tgt.connect()
    assert tgt._conn is mock_conn
    tgt.close()
    assert tgt._conn is None
