from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column, CommentDef, EventDef, FunctionDef, GrantDef, Index, Schema, TriggerDef,
    UserDef,
)
from core.connectors.mysql import (
    MySQLSourceConnector,
    MySQLTargetConnector,
    _normalize_mysql_set_value,
)
from core.connectors.mysql.objects import table as mysql_table_ops


def _target() -> tuple[MySQLTargetConnector, MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    connection = MagicMock()
    connection.cursor.return_value = cursor
    target = MySQLTargetConnector({"database": "target", "source_engine": "mysql"})
    target._conn = connection
    return target, cursor, connection


def _reconciliation_connection(target_exists: bool = True):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.side_effect = [None, ("customers",) if target_exists else None]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection, cursor


def test_reconcile_existing_table_replaces_without_backup(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = []

    result = mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"},
        schema, {"customers"},
    )

    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert result == "reconciled"
    assert any(statement == "DROP TABLE `customers`" for statement in sql)
    assert any(statement.startswith("RENAME TABLE `__dms_stage_") and "TO `customers`" in statement for statement in sql)
    assert not any("__dms_backup_" in statement for statement in sql)
    create_ddl = next(statement for statement in sql if statement.startswith("CREATE TABLE `__dms_stage_"))
    assert "`id` INT" in create_ddl
    connection.commit.assert_called()


def test_reconcile_missing_table_promotes_staged_without_backup(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=False)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = []

    result = mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"},
        schema, {"customers"},
    )

    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert result == "reconciled"
    assert "RENAME TABLE `__dms_stage_" in sql[-1]
    assert "TO `customers`" in sql[-1]
    assert all("RENAME TABLE `customers` TO" not in statement for statement in sql)
    assert not any("__dms_backup_" in statement for statement in sql)
    connection.commit.assert_called()


def test_reconcile_error_still_rolls_back_and_cleans_staging(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=False)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    monkeypatch.setattr(mysql_table_ops, "_create_table", lambda *args: None)
    monkeypatch.setattr(mysql_table_ops, "_schema", MagicMock(inspect_schema=MagicMock(side_effect=RuntimeError("inspect failed"))))

    with pytest.raises(RuntimeError, match="inspect failed"):
        mysql_table_ops.reconcile_mysql_table(
            connection, {"database": "target", "source_engine": "mysql"},
            schema, {"customers"},
        )

    connection.rollback.assert_called_once()
    assert any(
        call.args[0].startswith("DROP TABLE IF EXISTS `__dms_stage_")
        for call in cursor.execute.call_args_list
    )


def _prepare_reconciliation(monkeypatch, schema):
    monkeypatch.setattr(
        mysql_table_ops._schema,
        "inspect_schema",
        lambda *args: Schema(name="staged", columns=[], partition_method=schema.partition_method,
                             partition_expression=schema.partition_expression, partitions=schema.partitions),
    )


def test_reconcile_allows_managed_foreign_key_child_and_drops_its_fk(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = [("orders", "fk_orders_customers")]

    mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"},
        schema, {"customers", "orders"},
    )

    assert any(
        call.args[0] == "ALTER TABLE `orders` DROP FOREIGN KEY `fk_orders_customers`"
        for call in cursor.execute.call_args_list
    )
    assert any(call.args[0] == "DROP TABLE `customers`" for call in cursor.execute.call_args_list)
    assert not any("__dms_backup_" in call.args[0] for call in cursor.execute.call_args_list)


def test_reconcile_multiple_managed_tables_with_foreign_key_dependency(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    customer_schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    order_schema = Schema(name="orders", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, customer_schema)
    cursor.fetchall.side_effect = [[("orders", "fk_orders_customers")], []]
    cursor.fetchone.side_effect = [None, ("customers",), None, ("orders",)]

    for schema in (customer_schema, order_schema):
        mysql_table_ops.reconcile_mysql_table(
            connection, {"database": "target", "source_engine": "mysql"},
            schema, {"customers", "orders"},
        )

    assert any(
        call.args[0] == "ALTER TABLE `orders` DROP FOREIGN KEY `fk_orders_customers`"
        for call in cursor.execute.call_args_list
    )
    assert sum(call.args[0].startswith("DROP TABLE `") for call in cursor.execute.call_args_list) == 2
    assert not any("__dms_backup_" in call.args[0] for call in cursor.execute.call_args_list)


def test_reconcile_preserves_target_only_manual_table(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = []
    mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"}, schema, {"customers"}
    )
    assert not any("DROP TABLE `manual_table`" in call.args[0] for call in cursor.execute.call_args_list)


def test_reconcile_success_promotes_stage_without_temporary_tables(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = []

    mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"},
        schema, {"customers"},
    )

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any(statement.startswith("RENAME TABLE") and "__dms_stage_" in statement and "TO `customers`" in statement for statement in statements)
    assert any(statement.startswith("CREATE TABLE `__dms_stage_") for statement in statements)
    assert not any("__dms_backup_" in statement for statement in statements)


def test_reconcile_source_schema_replaces_different_and_extra_columns(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    schema = Schema(name="customers", columns=[
        Column(name="id", source_type="INT", nullable=False),
        Column(name="email", source_type="VARCHAR(200)"),
    ])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = []

    mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"}, schema, {"customers"}
    )

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    create_ddl = next(statement for statement in statements if statement.startswith("CREATE TABLE `__dms_stage_"))
    assert "`id` INT NOT NULL" in create_ddl
    assert "`email` VARCHAR(200) NULL" in create_ddl
    assert "legacy_column" not in create_ddl
    assert "DROP TABLE `customers`" in statements
    assert not any("__dms_backup_" in statement for statement in statements)


def test_reconcile_does_not_overwrite_preexisting_stage_named_user_table(monkeypatch):
    connection, cursor = _reconciliation_connection(target_exists=True)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    _prepare_reconciliation(monkeypatch, schema)
    cursor.fetchall.return_value = []
    cursor.fetchone.side_effect = [("occupied_stage",), None, ("customers",)]

    mysql_table_ops.reconcile_mysql_table(
        connection, {"database": "target", "source_engine": "mysql"}, schema, {"customers"}
    )

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert sum("FROM INFORMATION_SCHEMA.TABLES" in statement for statement in statements) == 3
    assert not any("DROP TABLE IF EXISTS" in statement for statement in statements)
    assert any(statement.startswith("RENAME TABLE `__dms_stage_") and "TO `customers`" in statement for statement in statements)


def test_mysql_grant_sql_maps_database_and_table_to_target_database():
    target, cursor, connection = _target()
    target._config["database"] = "target_db"

    target.apply_grant(GrantDef(
        privileges="SELECT", object_type="DATABASE", object_name="source_db",
        schema_name="source_db", grantee="reader", grantee_host="%",
    ))
    target.apply_grant(GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="source_db.customers",
        schema_name="source_db", grantee="reader", grantee_host="%",
    ))

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements == [
        "GRANT SELECT ON `target_db`.* TO `reader`@`%`",
        "GRANT SELECT ON TABLE `target_db`.`customers` TO `reader`@`%`",
    ]
    assert all("source_db" not in statement for statement in statements)
    assert connection.commit.call_count == 2


def test_mysql_grant_failure_is_reported_as_partial_success_target_name():
    from core.connectors.base import GrantDef, UpsertResult
    from core.orchestrator import MigrationOrchestrator

    source = MagicMock()
    target = MagicMock(spec=MySQLTargetConnector)
    source.get_partitioned_tables.return_value = []
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = Schema(
        name="customers", columns=[Column(name="id", source_type="INT")]
    )
    source.get_object_count.return_value = 1
    source.export_full.return_value = iter([{"id": 1}])
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.get_all_triggers.return_value = []
    source.list_grants.return_value = [GrantDef(
        privileges="SELECT", object_type="DATABASE", object_name="source_db",
        schema_name="source_db", grantee="mysql_test", grantee_host="%",
    )]
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    target.apply_grant.side_effect = RuntimeError(
        "1044 (42000): Access denied for user to database 'target_db'"
    )
    target.inspect_schema.return_value = None
    orchestrator = MigrationOrchestrator(source, target, {
        "source": {"engine": "mysql"},
        "target": {"engine": "mysql", "connection": {"database": "target_db"}},
        "migration": {"stop_on_error": False},
    })
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    result = orchestrator.run_full()

    assert result["status"] == "partial_success"
    assert result["phases"]["grants"] == [
        "GRANT ... ON target_db.* TO mysql_test: failed "
        "(1044 (42000): Access denied for user to database 'target_db')"
    ]
    assert "source_db.source_db" not in str(result["phases"]["grants"])
    target.apply_grant.assert_called_once()


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


@pytest.mark.parametrize(
    ("index_type", "expected_ddl"),
    [
        ("FULLTEXT", "CREATE FULLTEXT INDEX `ft_content` ON `documents` (`content`)"),
        ("SPATIAL", "CREATE SPATIAL INDEX `sp_location` ON `documents` (`location`)"),
    ],
)
def test_mysql_special_index_types_are_preserved_when_created(index_type, expected_ddl):
    target, cursor, connection = _target()
    column = "content" if index_type == "FULLTEXT" else "location"
    cursor.fetchone.return_value = None

    target.apply_constraints(Schema(
        name="documents",
        indexes=[Index(
            name="ft_content" if index_type == "FULLTEXT" else "sp_location",
            columns=[column],
            index_type=index_type,
        )],
    ))

    assert cursor.execute.call_args_list[1].args[0] == expected_ddl
    connection.commit.assert_called_once()


def test_mysql_schema_discovery_retains_fulltext_index_type():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = (None, "InnoDB", "utf8mb4_0900_ai_ci")
    cursor.fetchall.side_effect = [
        [
            ("title", "VARCHAR(255)", "YES", 255, None, "", None, None),
            ("content", "TEXT", "YES", None, None, "", None, None),
        ],
        [],
        [
            ("ft_content", 1, "title", "FULLTEXT"),
            ("ft_content", 1, "content", "FULLTEXT"),
        ],
        [], [], [], [],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "source"})
    source._conn = connection

    schema = source.get_schema("documents")

    assert schema.indexes == [
        Index(name="ft_content", columns=["title", "content"], index_type="FULLTEXT")
    ]


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
        [("'migration_grant_test'@'localhost'", "mysql_migration_source", "customers", "SELECT", "YES")],
        [], [], [], [], [("migration_grant_test", "localhost")],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "mysql_migration_source",
        "security_users": [{"user": "migration_grant_test", "host": "localhost"}],
    })
    source._conn = connection

    grants = source.list_grants()

    assert grants == [GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="customers",
        grantee="migration_grant_test", schema_name="mysql_migration_source", grant_option=True,
        grantee_host="localhost",
    )]
    assert "TABLE_SCHEMA" in cursor.execute.call_args_list[0].args[0]


def test_mysql_source_routine_execute_grant_is_discovered_when_catalog_is_visible():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [],
        [("'migration_grant_test'@'localhost'", "refresh_customers", "PROCEDURE", "EXECUTE", "NO")],
        [], [], [], [("migration_grant_test", "localhost")],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "mysql_migration_source",
        "security_users": [{"user": "migration_grant_test", "host": "localhost"}],
    })
    source._conn = connection

    assert source.list_grants() == [GrantDef(
        privileges="EXECUTE", object_type="PROCEDURE", object_name="refresh_customers",
        grantee="migration_grant_test", schema_name="mysql_migration_source",
        grantee_host="localhost",
    )]
    assert "ROUTINE_PRIVILEGES" in cursor.execute.call_args_list[1].args[0]


def test_mysql_grant_discovery_does_not_bypass_metadata_visibility_with_mysql_system_tables():
    """An empty least-privilege catalog result remains empty; no system-table fallback exists."""
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.return_value = []
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
    cursor.fetchall.side_effect = [
        [("'migration_grant_test'@'localhost'", "mysql_migration_source", "customers", "SELECT", "YES")],
        [], [], [], [("migration_grant_test", "localhost")],
    ]
    cursor.execute.side_effect = lambda sql, *_args: (
        (_ for _ in ()).throw(RuntimeError("ROUTINE_PRIVILEGES unavailable"))
        if "ROUTINE_PRIVILEGES" in sql else None
    )
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "mysql_migration_source",
        "security_users": [{"user": "migration_grant_test", "host": "localhost"}],
    })
    source._conn = connection

    assert source.list_grants() == [GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="customers",
        grantee="migration_grant_test", schema_name="mysql_migration_source", grant_option=True,
        grantee_host="localhost",
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


def test_mysql_list_users_preserves_host_and_allowlist_selects_only_users():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.return_value = [("security_test_user", "%")]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "source",
        "security_users": [{"user": "security_test_user", "host": "%"}],
    }); source._conn = connection

    assert source.list_users() == [UserDef(name="security_test_user", host="%")]
    sql = cursor.execute.call_args.args[0]
    assert "SELECT User,Host" in sql
    assert "authentication_string" not in sql
    assert "plugin" not in sql
    assert "read_role" not in repr(source.list_users())


def test_mysql_list_users_without_allowlist_filters_locked_accounts():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.return_value = [("app_user", "10.%")]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "source"}); source._conn = connection

    assert source.list_users() == [UserDef(name="app_user", host="10.%")]
    assert cursor.execute.call_count == 1
    assert "account_locked='N'" in cursor.execute.call_args.args[0]
    assert "mysql.role_edges" not in cursor.execute.call_args.args[0]


def test_mysql_list_grants_consolidates_direct_user_scopes_and_excludes_role_permissions():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [
            ("'app_user'@'%'", "source", "customers", "SELECT", "YES"),
            ("'reader_role'@'%'", "source", "customers", "SELECT", "NO"),
        ],
        [("'app_user'@'%'", "refresh_customers", "PROCEDURE", "EXECUTE", "NO")],
        [("'app_user'@'%'", "source", "CREATE", "NO")],
        [("'app_user'@'%'", "source", "customers", "id", "UPDATE", "YES")],
        [
            ("'app_user'@'%'", "def", "PROCESS", "NO"),
            ("'app_user'@'%'", "def", "ROLE_ADMIN", "YES"),
            ("'app_user'@'%'", "def", "PROXY", "YES"),
            ("'app_user'@'%'", "def", "USAGE", "NO"),
            ("'reader_role'@'%'", "def", "SELECT", "YES"),
        ],
        [("app_user", "%")],
    ]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "source",
        "security_users": [{"user": "app_user", "host": "%"}],
    }); source._conn = connection

    grants = source.list_grants()

    assert grants == [
        GrantDef("SELECT", "TABLE", "customers", "app_user", "source", True, "%"),
        GrantDef("EXECUTE", "PROCEDURE", "refresh_customers", "app_user", "source", False, "%"),
        GrantDef("CREATE", "DATABASE", "source", "app_user", "source", False, "%"),
        GrantDef("UPDATE", "COLUMN", "customers.id", "app_user", "source", True, "%"),
    ]
    executed = " ".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "INFORMATION_SCHEMA.USER_PRIVILEGES" in executed
    assert "mysql.role_edges" not in executed
    assert all(grant.grantee != "reader_role" for grant in grants)


def test_mysql_global_grants_require_allowlisted_user_and_supported_privilege():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [], [], [], [],
        [
            ("'app_user'@'%'", "def", "SELECT", "NO"),
            ("'app_user'@'%'", "def", "CREATE", "NO"),
            ("'app_user'@'%'", "def", "SYSTEM_USER", "NO"),
            ("'other_user'@'%'", "def", "SELECT", "NO"),
            ("'app_user'@'%'", "def", "ROLE_ADMIN", "YES"),
            ("'app_user'@'%'", "def", "PROXY", "YES"),
        ],
        [("app_user", "%")],
    ]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "source",
        "security_users": [{"user": "app_user", "host": "%"}],
    }); source._conn = connection

    assert source.list_grants() == [GrantDef(
        "SELECT", "GLOBAL", "*", "app_user", "*", False, "%",
    )]


def test_mysql_global_grants_are_not_discovered_without_explicit_user_selection():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [], [], [], [],
        [("'app_user'@'%'", "def", "SELECT", "NO")],
        [("app_user", "%")],
    ]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "source"}); source._conn = connection

    assert source.list_grants() == []


def test_mysql_user_allowlist_filters_existing_table_grants_without_regression():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [
        [("'security_test_user'@'%'", "source", "customers", "SELECT", "YES"),
         ("'unrelated_user'@'%'", "source", "customers", "SELECT", "YES")],
        [], [], [], [], [("security_test_user", "%")],
    ]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "source",
        "security_users": [{"user": "security_test_user", "host": "%"}],
    }); source._conn = connection

    assert source.list_grants() == [GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="customers",
        grantee="security_test_user", schema_name="source", grant_option=True,
        grantee_host="%",
    )]


def test_mysql_create_user_preserves_host_without_copying_credentials():
    target, cursor, connection = _target()
    target.create_user_if_not_exists("app_user", "10.%")

    assert cursor.execute.call_args.args[0] == "CREATE USER IF NOT EXISTS `app_user`@`10.%`"
    assert "IDENTIFIED" not in cursor.execute.call_args.args[0]
    connection.commit.assert_called_once()


def test_mysql_target_grants_apply_user_host_for_database_column_and_global_scopes():
    target, cursor, connection = _target()
    grants = [
        GrantDef("CREATE", "DATABASE", "source", "app_user", "source", False, "%"),
        GrantDef("UPDATE", "COLUMN", "customers.id", "app_user", "source", True, "%"),
        GrantDef("PROCESS", "GLOBAL", "*", "app_user", "*", False, "%"),
    ]

    for grant in grants:
        target.apply_grant(grant)

    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        "GRANT CREATE ON `target`.* TO `app_user`@`%`",
        "GRANT UPDATE ON `target`.`customers` (`id`) TO `app_user`@`%` WITH GRANT OPTION",
        "GRANT PROCESS ON *.* TO `app_user`@`%`",
    ]
    assert connection.commit.call_count == 3


def test_mysql_target_has_no_role_capabilities_and_classifies_denials():
    target, _cursor, _connection = _target()
    capabilities = target.get_capabilities()
    assert "roles" not in capabilities
    assert "role_memberships" not in capabilities
    assert target.is_authorization_error(RuntimeError("Access denied for GRANT"))
    assert not target.is_authorization_error(RuntimeError("unknown table"))
    assert not hasattr(target, "create_security_principal")
    from core.connectors.mysql import MySQLTargetConnector
    assert "create_role_if_not_exists" not in MySQLTargetConnector.__dict__
    assert "create_role_membership" not in MySQLTargetConnector.__dict__
    assert not hasattr(target, "create_role_if_not_exists")
    assert not hasattr(target, "create_role_membership")
    source = MySQLSourceConnector({"database": "source"})
    assert not hasattr(source, "list_roles")
    assert not hasattr(source, "list_role_memberships")


def test_mysql_allowlist_filters_existing_table_grants_without_regression():
    cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.__exit__.return_value = False
    cursor.fetchall.side_effect = [[
        ("'security_test_user'@'%'", "source", "customers", "SELECT", "YES"),
        ("'unrelated_user'@'%'", "source", "customers", "SELECT", "YES"),
    ], [], [], [], [], [("security_test_user", "%")]]
    connection = MagicMock(); connection.cursor.return_value = cursor
    source = MySQLSourceConnector({
        "database": "source",
        "security_users": [{"user": "security_test_user", "host": "%"}],
    }); source._conn = connection

    assert source.list_grants() == [GrantDef(
        privileges="SELECT", object_type="TABLE", object_name="customers",
        grantee="security_test_user", schema_name="source", grant_option=True,
        grantee_host="%",
    )]


def test_mysql_common_orchestrator_has_no_role_migration_path():
    from core.connectors.base import UserDef
    from core.orchestrator import MigrationOrchestrator

    source = MagicMock()
    target = MagicMock()
    source.list_objects.return_value = []
    source.list_extensions.return_value = []
    source.list_schemas.return_value = []
    source.list_types.return_value = []
    source.list_all_sequences.return_value = []
    source.list_partition_functions.return_value = []
    source.list_partition_schemes.return_value = []
    source.get_partitioned_tables.return_value = []
    source.list_views.return_value = []
    source.list_materialized_views.return_value = []
    source.list_functions.return_value = []
    source.list_synonyms.return_value = []
    source.get_all_triggers.return_value = []
    source.list_comments.return_value = []
    source.list_partitions.side_effect = AttributeError("no list_partitions")
    source.list_users.return_value = [UserDef("broken", host="%"), UserDef("working", host="localhost")]
    source.list_grants.return_value = [
        GrantDef("SELECT", "TABLE", "customers", "broken", "source", False, "%"),
        GrantDef("SELECT", "TABLE", "customers", "working", "source", False, "localhost"),
        GrantDef("UPDATE", "TABLE", "customers", "working", "source", False, "localhost"),
    ]
    target.get_capabilities.return_value = {
        "security_principals": {"supported": True, "mode": "direct"},
    }

    def create_user(name, host=None):
        if name == "broken":
            raise RuntimeError("CREATE USER denied")

    def apply_grant(grant):
        if grant.privileges == "SELECT":
            raise RuntimeError("Access denied for GRANT")

    target.create_user_if_not_exists.side_effect = create_user
    target.apply_grant.side_effect = apply_grant
    target.is_authorization_error.side_effect = lambda error: "Access denied" in str(error)
    orchestrator = MigrationOrchestrator(source, target, {
        "source": {"engine": "mysql"}, "target": {"engine": "mysql"},
        "migration": {"stop_on_error": False},
    })

    result = orchestrator.run_full()

    assert result["status"] == "partial_success"
    assert source.list_users.called
    assert source.list_grants.called
    source.list_roles.assert_not_called()
    source.list_role_memberships.assert_not_called()
    target.create_role_if_not_exists.assert_not_called()
    target.create_role_membership.assert_not_called()
    assert target.apply_grant.call_count == 2
    assert "skipped: user creation failed" in result["phases"]["grants"][0]
    assert "skipped: NOT AUTHORIZED" in result["phases"]["grants"][1]
    assert result["phases"]["grants"][2].endswith(": applied")


def test_mysql_definer_users_are_created_before_all_definer_objects():
    from core.orchestrator import MigrationOrchestrator

    source = MagicMock()
    target = MagicMock()
    source.list_objects.return_value = []
    source.list_extensions.return_value = []
    source.list_schemas.return_value = []
    source.list_types.return_value = []
    source.list_all_sequences.return_value = []
    source.list_partition_functions.return_value = []
    source.list_partition_schemes.return_value = []
    source.get_partitioned_tables.return_value = []
    source.list_views.return_value = []
    source.list_materialized_views.return_value = []
    source.list_functions.return_value = [
        FunctionDef("f", kind="function", ddl="CREATE DEFINER=`mysql_test`@`%` FUNCTION f() RETURNS INT RETURN 1"),
        FunctionDef("p", kind="procedure", ddl="CREATE DEFINER=`mysql_test`@`%` PROCEDURE p() SELECT 1"),
    ]
    source.get_all_triggers.return_value = [
        TriggerDef("tr", "tbl", "CREATE DEFINER=`mysql_test`@`%` TRIGGER tr BEFORE INSERT ON tbl FOR EACH ROW SET NEW.id=1")
    ]
    source.list_events.return_value = [
        EventDef("evt", ddl="CREATE DEFINER=`mysql_test`@`%` EVENT evt ON SCHEDULE EVERY 1 DAY DO SELECT 1")
    ]
    source.list_synonyms.return_value = []
    source.list_comments.return_value = []
    source.list_partitions.side_effect = AttributeError("no list_partitions")
    source.list_users.return_value = [UserDef("mysql_test", host="%")]
    source.list_grants.return_value = []
    order: list[str] = []
    target.get_capabilities.return_value = {"security_principals": {"supported": True}}
    account_created = False

    def create_user(name, host=None):
        nonlocal account_created
        account_created = True
        order.append(f"user:{name}@{host}")

    def create_event(event):
        if not account_created:
            raise RuntimeError(
                "4006 (HY000): Operation CREATE USER failed for 'mysql_test'@'%' "
                "as it is referenced as a definer account in an event"
            )
        order.append(f"event:{event.name}")

    target.create_user_if_not_exists.side_effect = create_user
    target.create_function.side_effect = lambda routine: order.append(f"{routine.kind}:{routine.name}")
    target.create_trigger.side_effect = lambda trigger: order.append(f"trigger:{trigger.name}")
    target.create_event.side_effect = create_event
    orchestrator = MigrationOrchestrator(source, target, {
        "source": {"engine": "mysql"}, "target": {"engine": "mysql"},
        "migration": {"stop_on_error": False},
    })

    result = orchestrator.run_full()

    assert result["phases"]["security_users_pre_objects"]["USER mysql_test@%"] == "created"
    assert order == ["user:mysql_test@%", "function:f", "procedure:p", "trigger:tr", "event:evt"]
    assert result["phases"]["events"]["evt"] == "created"
    target.create_user_if_not_exists.assert_called_once_with("mysql_test", "%")
    source.list_roles.assert_not_called()
    source.list_role_memberships.assert_not_called()
    target.create_role_if_not_exists.assert_not_called()
    target.create_role_membership.assert_not_called()
