from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import EventDef, FunctionDef, TriggerDef
from core.connectors.mysql import MySQLSourceConnector, MySQLTargetConnector, _rewrite_mysql_definer


SOURCE_DEFINER = "`mysql_test`@`%`"
TARGET_DEFINER = "mysql_admin@%"


def _target(*, routine_definer: str = TARGET_DEFINER):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    connection = MagicMock()
    connection.cursor.return_value = cursor
    target = MySQLTargetConnector({"database": "target", "routine_definer": routine_definer})
    target._conn = connection
    return target, cursor, connection


@pytest.mark.parametrize(
    ("kind", "ddl"),
    [
        (
            "function",
            "CREATE DEFINER=`mysql_test`@`%` FUNCTION `word_count`(value TEXT) "
            "RETURNS INT SQL SECURITY DEFINER RETURN CHAR_LENGTH(value)",
        ),
        (
            "procedure",
            "CREATE DEFINER=`mysql_test`@`%` PROCEDURE `save_note`(IN value TEXT) "
            "SQL SECURITY DEFINER BEGIN INSERT INTO notes(body) VALUES (value); END",
        ),
    ],
)
def test_mysql_routine_definer_is_rewritten_without_changing_routine_ddl(kind, ddl):
    target, cursor, connection = _target()
    routine = FunctionDef(name="word_count" if kind == "function" else "save_note", kind=kind, ddl=ddl)

    target.create_function(routine)

    executed_ddl = cursor.execute.call_args_list[1].args[0]
    expected = ddl.replace(f"DEFINER={SOURCE_DEFINER}", "DEFINER=`mysql_admin`@`%`")
    assert executed_ddl == expected
    assert "SQL SECURITY DEFINER" in executed_ddl
    assert connection.commit.called


def test_mysql_trigger_definer_is_rewritten_without_changing_trigger_ddl():
    target, cursor, _ = _target()
    ddl = (
        "CREATE DEFINER=`mysql_test`@`%` TRIGGER `notes_before_insert` "
        "BEFORE INSERT ON notes FOR EACH ROW SET NEW.body = TRIM(NEW.body)"
    )

    target.create_trigger(TriggerDef(name="notes_before_insert", table="notes", ddl=ddl))

    assert cursor.execute.call_args_list[1].args[0] == ddl.replace(
        f"DEFINER={SOURCE_DEFINER}", "DEFINER=`mysql_admin`@`%`"
    )


def test_mysql_event_definer_is_rewritten_without_changing_event_schedule_or_body():
    target, cursor, _ = _target()
    ddl = (
        "CREATE DEFINER=`mysql_test`@`%` EVENT `purge_notes` "
        "ON SCHEDULE EVERY 1 DAY DO DELETE FROM notes WHERE created_at < NOW() - INTERVAL 30 DAY"
    )

    target.create_event(EventDef(name="purge_notes", ddl=ddl))

    assert cursor.execute.call_args_list[1].args[0] == ddl.replace(
        f"DEFINER={SOURCE_DEFINER}", "DEFINER=`mysql_admin`@`%`"
    )


def test_mysql_definer_rewrite_leaves_ddl_without_definer_unchanged():
    ddl = "CREATE FUNCTION word_count(value TEXT) RETURNS INT RETURN CHAR_LENGTH(value)"

    assert _rewrite_mysql_definer(ddl, TARGET_DEFINER) == ddl


@pytest.mark.parametrize(
    "ddl",
    [
        (
            "CREATE FUNCTION word_count(value TEXT) RETURNS TEXT "
            "RETURN 'CREATE DEFINER=`mysql_test`@`%` FUNCTION fake() RETURNS INT RETURN 1'"
        ),
        (
            "CREATE PROCEDURE save_note() BEGIN "
            "/* CREATE DEFINER=`mysql_test`@`%` PROCEDURE fake() */ SELECT 1; END"
        ),
        (
            "CREATE TRIGGER notes_before_insert BEFORE INSERT ON notes FOR EACH ROW "
            "BEGIN SET @note = 'CREATE DEFINER=`mysql_test`@`%` TRIGGER fake'; END"
        ),
        (
            "CREATE EVENT purge_notes ON SCHEDULE EVERY 1 DAY "
            "DO /* CREATE DEFINER=`mysql_test`@`%` EVENT fake */ DELETE FROM notes"
        ),
    ],
)
def test_mysql_definer_rewrite_does_not_match_body_strings_or_comments(ddl):
    assert _rewrite_mysql_definer(ddl, TARGET_DEFINER) == ddl


def test_mysql_definer_rewrite_changes_only_header_not_body_text():
    ddl = (
        "CREATE DEFINER=`mysql_test`@`%` PROCEDURE save_note() "
        "BEGIN SET @note = 'CREATE DEFINER=`mysql_test`@`%` PROCEDURE fake()'; END"
    )

    rewritten = _rewrite_mysql_definer(ddl, TARGET_DEFINER)

    assert rewritten == ddl.replace(
        "CREATE DEFINER=`mysql_test`@`%` PROCEDURE save_note()",
        "CREATE DEFINER=`mysql_admin`@`%` PROCEDURE save_note()",
    )


def test_mysql_definer_rewrite_leaves_compatible_definer_unchanged():
    ddl = "CREATE DEFINER=`mysql_admin`@`%` FUNCTION word_count() RETURNS INT RETURN 1"

    assert _rewrite_mysql_definer(ddl, TARGET_DEFINER) == ddl


def test_mysql_explicit_routine_definer_override_is_used():
    target, cursor, _ = _target(routine_definer="migration_owner@localhost")
    ddl = "CREATE DEFINER=`mysql_test`@`%` FUNCTION word_count() RETURNS INT RETURN 1"

    target.create_function(FunctionDef(name="word_count", ddl=ddl))

    assert "DEFINER=`migration_owner`@`localhost`" in cursor.execute.call_args_list[1].args[0]


def test_mysql_target_connect_derives_routine_definer_from_current_user(monkeypatch):
    import mysql.connector

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = ("mysql_admin@%",)
    connection = MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(mysql.connector, "connect", lambda **_: connection)
    monkeypatch.setattr("core.connectors.mysql.ensure_driver", lambda *_: None)
    target = MySQLTargetConnector({
        "host": "target", "database": "target", "username": "mysql_admin", "ssl": False,
    })

    target.connect()

    assert target._routine_definer == "mysql_admin@%"
    assert cursor.execute.call_args.args[0] == "SELECT CURRENT_USER()"


def test_mysql_source_show_create_preserves_source_definer_for_target_rewrite():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.column_names = ["Function", "sql_mode", "Create Function", "character_set_client"]
    ddl = "CREATE DEFINER=`mysql_test`@`%` FUNCTION word_count() RETURNS INT RETURN 1"
    cursor.fetchone.return_value = ("word_count", "", ddl, "utf8mb4")
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "source"})
    source._conn = connection

    assert source._show_create("FUNCTION", "word_count") == ddl
