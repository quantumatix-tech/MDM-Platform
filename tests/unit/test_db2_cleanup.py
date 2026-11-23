from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.source import DB2SourceConnector


@patch('pyodbc.connect')
def test_close_idempotent(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    s = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    s.connect()
    s.close()
    s.close()
    assert s._conn is None


@patch('pyodbc.connect')
def test_close_after_error(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    s = DB2SourceConnector({'host': 'h', 'port': 25000, 'database': 'DB2SRC', 'username': 'u', 'password': 'p'})
    s.connect()
    try:
        raise RuntimeError('test')
    except RuntimeError:
        pass
    finally:
        s.close()
    assert s._conn is None
