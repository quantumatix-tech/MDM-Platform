from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, Schema, UnmappedTypeError
from core.connectors.mssql import MSSQLTargetConnector
from core.connectors.mysql import MySQLTargetConnector
from core.connectors.postgresql import PostgresTargetConnector


def _target_with_missing_table(connector):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connector._conn = connection
    return cursor, connection


@pytest.mark.parametrize(
    ("connector_class", "target_engine", "mapped_type"),
    [
        (MySQLTargetConnector, "mysql", "INT"),
        (MSSQLTargetConnector, "mssql", "BIGINT"),
    ],
)
def test_cross_engine_mapped_type_is_used_in_generated_ddl(
    connector_class, target_engine, mapped_type
):
    connector = connector_class({"database": "target", "source_engine": "postgresql"})
    cursor, _ = _target_with_missing_table(connector)
    schema = Schema(
        name="orders",
        columns=[Column(name="id", source_type="integer", target_type=mapped_type, nullable=False)],
    )

    connector.create_object_if_missing(schema)

    expected = (
        f"CREATE TABLE `orders` (`id` {mapped_type} NOT NULL)"
        if target_engine == "mysql"
        else f"CREATE TABLE orders (id {mapped_type} NOT NULL)"
    )
    assert cursor.execute.call_args_list[-1].args[0] == expected


@pytest.mark.parametrize(
    ("connector_class", "target_engine"),
    [
        (MySQLTargetConnector, "mysql"),
        (MSSQLTargetConnector, "mssql"),
    ],
)
def test_cross_engine_unmapped_type_raises_actionable_error(connector_class, target_engine):
    connector = connector_class({"database": "target", "source_engine": "postgresql"})
    cursor, connection = _target_with_missing_table(connector)
    schema = Schema(name="orders", columns=[Column(name="payload", source_type="geometry")])

    with pytest.raises(UnmappedTypeError) as error:
        connector.create_object_if_missing(schema)

    message = str(error.value)
    assert "orders" in message
    assert "payload" in message
    assert "geometry" in message
    assert "postgresql" in message
    assert target_engine in message
    assert all("CREATE TABLE" not in call.args[0] for call in cursor.execute.call_args_list)
    connection.commit.assert_not_called()


def test_same_engine_postgresql_uses_source_type_in_generated_ddl():
    connector = PostgresTargetConnector({"database": "target"})
    cursor, _ = _target_with_missing_table(connector)
    schema = Schema(name="orders", columns=[Column(name="payload", source_type="jsonb")])

    connector.create_object_if_missing(schema)

    assert cursor.execute.call_args_list[-1].args[0] == "CREATE TABLE orders (payload jsonb NULL)"
