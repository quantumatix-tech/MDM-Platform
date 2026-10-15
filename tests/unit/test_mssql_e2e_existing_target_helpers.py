from __future__ import annotations

from unittest.mock import MagicMock

from tests.e2e.mssql.validate_missing_table import drop_product_target_safely


def test_missing_table_helper_drops_referencing_fk_before_products():
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("FK_OrderDetails_Products", "training", "OrderDetails")
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False

    dropped = drop_product_target_safely(conn)

    assert dropped == ["FK_OrderDetails_Products"]
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[1] == (
        'ALTER TABLE [training].[OrderDetails] '
        'DROP CONSTRAINT [FK_OrderDetails_Products]'
    )
    assert statements[2] == "DROP TABLE [training].[Products]"
    conn.commit.assert_called_once_with()
