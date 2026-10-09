from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, CommentDef, Schema, TypeDef
from core.connectors.postgresql.objects.comment import reconcile_column_comments
from core.connectors.postgresql.objects.type import reconcile_enum_types
from core.connectors.postgresql.target import PostgresTargetConnector


def test_full_reconciliation_replaces_enum_without_cascade():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [
        [(label,) for label in ("pending", "processing", "shipped", "delivered", "cancelled", "target_modified")],
        [("training", "orders", "orderstatus", False)],
    ]
    enum = TypeDef(
        name="orderstatus",
        kind="enum",
        schema="training",
        enum_labels=["pending", "processing", "shipped", "delivered", "cancelled"],
        ddl=(
            'CREATE TYPE "training"."orderstatus" AS ENUM '
            "('pending', 'processing', 'shipped', 'delivered', 'cancelled')"
        ),
    )
    managed_orders = Schema(
        name="orders",
        schema_name="training",
        columns=[Column(
            name="orderstatus",
            source_type='"training"."orderstatus"',
            default="'pending'::training.orderstatus",
        )],
    )

    changed = reconcile_enum_types(conn, [enum], [managed_orders])

    statements = [call.args[0] for call in cur.execute.call_args_list]
    assert changed == ["training.orderstatus"]
    assert any('ALTER TYPE "training"."orderstatus" RENAME TO' in sql for sql in statements)
    assert enum.ddl in statements
    assert any(
        'ALTER TABLE "training"."orders" ALTER COLUMN "orderstatus" TYPE '
        '"training"."orderstatus" USING "orderstatus"::text::"training"."orderstatus"'
        in sql
        for sql in statements
    )
    assert any("SET DEFAULT 'pending'::training.orderstatus" in sql for sql in statements)
    assert any(sql.startswith("DROP TYPE ") and sql.endswith(" RESTRICT") for sql in statements)
    assert not any("CASCADE" in sql.upper() for sql in statements)
    conn.commit.assert_called_once_with()
    conn.rollback.assert_not_called()


def test_enum_reconciliation_refuses_target_only_dependent_column():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [
        [("pending",), ("target_modified",)],
        [("training", "manual_table", "status", False)],
    ]
    enum = TypeDef(
        name="orderstatus", kind="enum", schema="training",
        enum_labels=["pending"],
        ddl='CREATE TYPE "training"."orderstatus" AS ENUM (\'pending\')',
    )

    with pytest.raises(RuntimeError, match="not source-managed"):
        reconcile_enum_types(conn, [enum], [])

    assert not any("ALTER TYPE" in call.args[0] for call in cur.execute.call_args_list)
    conn.rollback.assert_called_once_with()


def test_column_comment_reconciliation_clears_modified_source_managed_column():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    managed = Schema(
        name="customers",
        schema_name="training",
        columns=[Column(name="fullname", source_type="character varying(100)")],
    )

    cleared = reconcile_column_comments(conn, [managed], [])

    assert cleared == ["training.customers.fullname"]
    cur.execute.assert_called_once_with(
        'COMMENT ON COLUMN "training"."customers"."fullname" IS NULL'
    )
    conn.commit.assert_called_once_with()


def test_source_column_comment_is_applied_and_not_cleared():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cur = target._conn.cursor.return_value.__enter__.return_value
    managed = Schema(
        name="customers",
        schema_name="training",
        columns=[Column(name="fullname", source_type="character varying(100)")],
    )
    source_comment = CommentDef(
        object_type="COLUMN",
        object_name="customers.fullname",
        comment="SOURCE COLUMN COMMENT",
        schema_name="training",
    )

    target.apply_comment(source_comment)
    cleared = target.reconcile_postgresql_column_comments([managed], [source_comment])

    statements = [call.args[0] for call in cur.execute.call_args_list]
    assert any("IS 'SOURCE COLUMN COMMENT'" in sql for sql in statements)
    assert not any("IS NULL" in sql for sql in statements)
    assert cleared == []
