from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.db2.target import DB2TargetConnector
from core.connectors.base import Schema, Column


@patch('pyodbc.connect')
def test_target_preflight(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    cur = MagicMock()
    cur.fetchone.side_effect = [('DB2TGT',), ('DB2ADMIN',)]
    mock_conn.cursor.return_value = cur
    tgt = DB2TargetConnector({'host': 'h', 'port': 25000, 'database': 'DB2TGT', 'username': 'u', 'password': 'p'})
    tgt.connect()
    pf = tgt.preflight()
    assert pf['current_server'] == 'DB2TGT'
    tgt.close()


def test_build_create_table_sql():
    from core.connectors.db2.objects import ddl as db2_ddl
    schema = Schema(
        name='EMPLOYEES',
        schema_name='HR',
        columns=[
            Column(name='EMP_ID', source_type='INTEGER', nullable=False),
            Column(name='EMP_NAME', source_type='VARCHAR', nullable=True, size=50),
        ],
        primary_key=['EMP_ID'],
    )
    sql = db2_ddl.build_create_table_sql(schema)
    assert '"HR"' in sql
    assert '"EMPLOYEES"' in sql
    assert 'PRIMARY KEY' in sql


@patch('pyodbc.connect')
def test_create_object_if_missing_does_not_overwrite(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    tgt = DB2TargetConnector({'host': 'h', 'port': 25000, 'database': 'DB2TGT', 'username': 'u', 'password': 'p'})
    tgt.connect()
    cur = MagicMock()
    # schema exists, table exists
    cur.fetchone.side_effect = [(1,), (1,)]
    mock_conn.cursor.return_value.__enter__.return_value = cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    schema = Schema(name='EMPLOYEES', schema_name='HR', columns=[Column(name='EMP_ID', source_type='INTEGER', nullable=False)], primary_key=['EMP_ID'])
    tgt.create_object_if_missing(schema)
    tgt.close()
