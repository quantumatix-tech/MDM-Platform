from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.postgresql.objects.table import get_row_count
from core.connectors.postgresql.source import PostgresSourceConnector
from core.connectors.postgresql.target import PostgresTargetConnector


def test_get_row_count_commits_its_read_transaction():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (5,)

    assert get_row_count(conn, "products", "training") == 5

    cursor.execute.assert_called_once_with(
        "SELECT count(*) FROM training.products"
    )
    conn.commit.assert_called_once_with()
    conn.rollback.assert_not_called()


def test_get_row_count_rolls_back_when_query_fails():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError("query failed")

    with pytest.raises(RuntimeError, match="query failed"):
        get_row_count(conn, "products", "training")

    conn.commit.assert_not_called()
    conn.rollback.assert_called_once_with()


@pytest.mark.parametrize(
    "connector_type",
    [PostgresSourceConnector, PostgresTargetConnector],
)
def test_postgresql_connector_close_rolls_back_and_releases_connection(connector_type):
    connector = connector_type({"database": "test"})
    conn = MagicMock()
    connector._conn = conn

    connector.close()
    connector.close()

    conn.rollback.assert_called_once_with()
    conn.close.assert_called_once_with()
    assert connector._conn is None
