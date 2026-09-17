from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, CommentDef, GrantDef, Schema
from core.connectors.mysql import (
    MySQLSourceConnector,
    MySQLTargetConnector,
    _normalize_mysql_set_value,
)


def _target() -> tuple[MySQLTargetConnector, MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    connection = MagicMock()
    connection.cursor.return_value = cursor
    target = MySQLTargetConnector({"database": "target", "source_engine": "mysql"})
    target._conn = connection
    return target, cursor, connection


def test_mysql_set_value_uses_declared_member_order_for_target_insert():
    target, cursor, connection = _target()
    schema = Schema(
        name="tbl_datatype_test",
        columns=[
            Column(name="id", source_type="INT", nullable=False),
            Column(name="set_col", source_type="SET('email','sms','push')"),
            Column(name="enum_col", source_type="ENUM('active','closed')"),
            Column(name="json_col", source_type="JSON"),
            Column(name="binary_col", source_type="BLOB"),
        ],
    )

    result = target.upsert_batch(
        "tbl_datatype_test",
        iter([{
            "id": 1,
            "set_col": {"sms", "email"},
            "enum_col": "active",
            "json_col": {"nested": True},
            "binary_col": b"\\x00\\x01",
        }]),
        schema,
    )

    assert result.success_count == 1
    assert result.failure_count == 0
    values = cursor.execute.call_args.args[1]
    assert values == [1, "email,sms", "active", {"nested": True}, b"\\x00\\x01"]
    connection.commit.assert_called_once()


def test_non_set_values_are_unchanged():
    assert _normalize_mysql_set_value("email,sms", "SET('email','sms')") == "email,sms"
    assert _normalize_mysql_set_value(b"binary", "BLOB") == b"binary"
    assert _normalize_mysql_set_value({"email"}, "ENUM('email')") == {"email"}


def test_mysql_comment_discovery_includes_base_tables_and_ignores_views():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [("customers", "Customers table")],
        [("customers", "email", "Customer email")],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "source"})
    source._conn = connection

    comments = source.list_comments()

    assert [(c.object_type, c.object_name) for c in comments] == [
        ("TABLE", "customers"),
        ("COLUMN", "customers.email"),
    ]
    table_query = cursor.execute.call_args_list[0].args[0]
    column_query = cursor.execute.call_args_list[1].args[0]
    assert "TABLE_TYPE='BASE TABLE'" in table_query
    assert "JOIN INFORMATION_SCHEMA.TABLES" in column_query
    assert "t.TABLE_TYPE='BASE TABLE'" in column_query


def test_mysql_existing_table_comment_is_applied():
    target, cursor, connection = _target()
    cursor.fetchone.return_value = ("BASE TABLE",)

    target.apply_comment(CommentDef("TABLE", "customers", "Updated customers comment", "source"))

    assert cursor.execute.call_args_list[0].args == (
        "SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
        ("target", "customers"),
    )
    assert cursor.execute.call_args_list[1].args[0] == (
        "ALTER TABLE `customers` COMMENT = 'Updated customers comment'"
    )
    connection.commit.assert_called_once()


def test_mysql_comment_target_view_is_rejected_before_alter_table():
    target, cursor, connection = _target()
    cursor.fetchone.return_value = ("VIEW",)

    with pytest.raises(RuntimeError, match="not a BASE TABLE"):
        target.apply_comment(CommentDef("TABLE", "customer_order_summary", "View comment", "source"))

    assert cursor.execute.call_count == 1
    assert "ALTER TABLE" not in cursor.execute.call_args.args[0]
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_mysql_existing_column_comment_preserves_target_column_definition():
    target, cursor, connection = _target()
    cursor.fetchone.side_effect = [
        ("BASE TABLE",),
        (
            "datetime(6)", "NO", "CURRENT_TIMESTAMP(6)",
            "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)", "",
            "utf8mb4", "utf8mb4_0900_ai_ci",
        ),
    ]

    target.apply_comment(CommentDef("COLUMN", "customers.updated_at", "Last update time", "source"))

    metadata_sql, metadata_params = cursor.execute.call_args_list[1].args
    assert "FROM INFORMATION_SCHEMA.COLUMNS" in metadata_sql
    assert metadata_params == ("target", "customers", "updated_at")
    assert "COLUMN_FORMAT" not in metadata_sql
    assert "STORAGE" not in metadata_sql
    assert cursor.execute.call_args_list[2].args[0] == (
        "ALTER TABLE `customers` MODIFY COLUMN `updated_at` datetime(6) "
        "CHARACTER SET `utf8mb4` COLLATE `utf8mb4_0900_ai_ci` NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT 'Last update time'"
    )
    connection.commit.assert_called_once()


def test_mysql_existing_generated_column_comment_preserves_expression_and_storage():
    target, cursor, connection = _target()
    cursor.fetchone.side_effect = [
        ("BASE TABLE",),
        (
            "decimal(10,2)", "NO", None, "STORED GENERATED", "(`quantity` * `price`)",
            None, None,
        ),
    ]

    target.apply_comment(CommentDef("COLUMN", "orders.total", "Calculated total", "source"))

    assert cursor.execute.call_args_list[2].args[0] == (
        "ALTER TABLE `orders` MODIFY COLUMN `total` decimal(10,2) GENERATED ALWAYS AS "
        "((`quantity` * `price`)) STORED NOT NULL COMMENT 'Calculated total'"
    )
    connection.commit.assert_called_once()


def test_mysql_existing_auto_increment_column_comment_preserves_auto_increment():
    target, cursor, connection = _target()
    cursor.fetchone.side_effect = [
        ("BASE TABLE",),
        ("bigint unsigned", "NO", None, "auto_increment", "", None, None),
    ]

    target.apply_comment(CommentDef("COLUMN", "customers.id", "Customer identifier", "source"))

    assert cursor.execute.call_args_list[2].args[0] == (
        "ALTER TABLE `customers` MODIFY COLUMN `id` bigint unsigned NOT NULL "
        "AUTO_INCREMENT COMMENT 'Customer identifier'"
    )
    connection.commit.assert_called_once()


def test_mysql_source_table_grant_preserves_grantee_and_source_schema_metadata():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [("'migration_grant_test'@'localhost'", "mysql_migration_source", "customers", "SELECT")],
        [],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "mysql_migration_source"})
    source._conn = connection

    grants = source.list_grants()

    assert grants == [GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="customers",
        grantee="'migration_grant_test'@'localhost'", schema_name="mysql_migration_source",
    )]
    assert "TABLE_SCHEMA" in cursor.execute.call_args_list[0].args[0]


def test_mysql_source_routine_execute_grant_is_discovered_when_catalog_is_visible():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [],
        [("'migration_grant_test'@'localhost'", "refresh_customers", "PROCEDURE", "EXECUTE")],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "mysql_migration_source"})
    source._conn = connection

    assert source.list_grants() == [GrantDef(
        privileges="EXECUTE", object_type="PROCEDURE", object_name="refresh_customers",
        grantee="'migration_grant_test'@'localhost'", schema_name="mysql_migration_source",
    )]
    assert "ROUTINE_PRIVILEGES" in cursor.execute.call_args_list[1].args[0]


def test_mysql_grant_discovery_does_not_bypass_metadata_visibility_with_mysql_system_tables():
    """An empty least-privilege catalog result remains empty; no system-table fallback exists."""
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [[], []]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "mysql_migration_source"})
    source._conn = connection

    assert source.list_grants() == []
    executed_sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "INFORMATION_SCHEMA.TABLE_PRIVILEGES" in executed_sql
    assert "INFORMATION_SCHEMA.ROUTINE_PRIVILEGES" in executed_sql
    assert "mysql.tables_priv" not in executed_sql
    assert "mysql.*" not in executed_sql


def test_mysql_routine_grant_catalog_failure_preserves_table_grants():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.return_value = [
        ("'migration_grant_test'@'localhost'", "mysql_migration_source", "customers", "SELECT"),
    ]
    cursor.execute.side_effect = [None, RuntimeError("ROUTINE_PRIVILEGES unavailable")]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "mysql_migration_source"})
    source._conn = connection

    assert source.list_grants() == [GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="customers",
        grantee="'migration_grant_test'@'localhost'", schema_name="mysql_migration_source",
    )]
    connection.rollback.assert_called_once()


def test_mysql_table_grant_uses_target_database_and_preserves_grantee():
    target, cursor, connection = _target()
    grant = GrantDef(
        privileges="SELECT, INSERT", object_type="TABLE",
        object_name="mysql_migration_source.customers",
        grantee="'migration_grant_test'@'localhost'", schema_name="mysql_migration_source",
    )

    target.apply_grant(grant)

    assert cursor.execute.call_args.args[0] == (
        "GRANT SELECT, INSERT ON TABLE `target`.`customers` "
        "TO 'migration_grant_test'@'localhost'"
    )
    connection.commit.assert_called_once()


def test_mysql_routine_execute_grant_uses_target_database_and_preserves_grantee():
    target, cursor, connection = _target()

    target.apply_grant(GrantDef(
        privileges="EXECUTE", object_type="PROCEDURE", object_name="refresh_customers",
        grantee="'migration_grant_test'@'localhost'", schema_name="mysql_migration_source",
    ))

    assert cursor.execute.call_args.args[0] == (
        "GRANT EXECUTE ON PROCEDURE `target`.`refresh_customers` "
        "TO 'migration_grant_test'@'localhost'"
    )
    connection.commit.assert_called_once()


def test_mysql_table_grant_rolls_back_and_raises_on_target_failure():
    target, cursor, connection = _target()
    cursor.execute.side_effect = RuntimeError("GRANT denied")

    with pytest.raises(RuntimeError, match="GRANT denied"):
        target.apply_grant(GrantDef(
            privileges="SELECT", object_type="TABLE", object_name="customers",
            grantee="'migration_grant_test'@'localhost'", schema_name="mysql_migration_source",
        ))

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
