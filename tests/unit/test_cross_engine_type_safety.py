from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, Schema
from core.connectors.postgresql import PostgresTargetConnector


def _target_with_missing_table(connector):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connector._conn = connection
    return cursor, connection


def test_same_engine_postgresql_uses_source_type_in_generated_ddl():
    connector = PostgresTargetConnector({"database": "target"})
    cursor, _ = _target_with_missing_table(connector)
    schema = Schema(name="orders", columns=[Column(name="payload", source_type="jsonb")])

    connector.create_object_if_missing(schema)

    assert cursor.execute.call_args_list[-1].args[0] == "CREATE TABLE orders (payload jsonb NULL)"
