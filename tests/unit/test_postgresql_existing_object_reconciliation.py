from __future__ import annotations

from unittest.mock import MagicMock

from core.connectors.base import FunctionDef, ViewDefinition
from core.connectors.postgresql.objects.function import create_function, discover_functions
from core.connectors.postgresql.objects.view import create_view


def _connection_and_cursor():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_modified_source_managed_function_is_replaced_from_source():
    conn, cursor = _connection_and_cursor()
    func = FunctionDef(
        name="get_customer_count",
        schema_name="training",
        identity_arguments="",
        ddl=(
            "CREATE FUNCTION training.get_customer_count() RETURNS integer "
            "LANGUAGE sql AS $function$ SELECT 5; $function$"
        ),
    )

    create_function(conn, func)

    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert executed == [
        "CREATE OR REPLACE FUNCTION training.get_customer_count() RETURNS integer "
        "LANGUAGE sql AS $function$ SELECT 5; $function$"
    ]
    assert not any(statement.startswith("DROP ") for statement in executed)
    conn.commit.assert_called_once_with()


def test_function_return_type_mismatch_recreates_only_source_identity():
    conn, cursor = _connection_and_cursor()
    cursor.execute.side_effect = [RuntimeError("cannot change return type"), None, None]
    func = FunctionDef(
        name="get_customer_count",
        schema_name="training",
        identity_arguments="",
        ddl=(
            "CREATE OR REPLACE FUNCTION training.get_customer_count() RETURNS integer "
            "LANGUAGE sql AS $function$ SELECT 5; $function$"
        ),
    )

    create_function(conn, func)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[0].startswith("CREATE OR REPLACE FUNCTION training.get_customer_count()")
    assert statements[1] == 'DROP FUNCTION IF EXISTS "training"."get_customer_count"()'
    assert statements[2].endswith("SELECT 5; $function$")
    assert not any("CASCADE" in statement.upper() for statement in statements)
    assert not any("manual_target_function" in statement for statement in statements)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_called_once_with()


def test_source_managed_procedure_uses_replace_semantics():
    conn, cursor = _connection_and_cursor()
    procedure = FunctionDef(
        name="refresh_customer_cache",
        schema_name="training",
        kind="procedure",
        identity_arguments="",
        ddl="CREATE PROCEDURE training.refresh_customer_cache() LANGUAGE sql AS $$ SELECT 1 $$",
    )

    create_function(conn, procedure)

    assert cursor.execute.call_args.args[0].startswith(
        "CREATE OR REPLACE PROCEDURE training.refresh_customer_cache()"
    )
    assert not any(
        call.args[0].startswith("DROP ")
        for call in cursor.execute.call_args_list
    )


def test_postgresql_discovery_marks_procedures_for_replay():
    conn, cursor = _connection_and_cursor()
    cursor.fetchall.return_value = [
        (
            "refresh_customer_cache",
            "training",
            "CREATE OR REPLACE PROCEDURE training.refresh_customer_cache()",
            "p",
            "",
        )
    ]

    routines = discover_functions(conn, ("training",))

    assert len(routines) == 1
    assert routines[0].kind == "procedure"
    assert routines[0].identity_arguments == ""


def test_procedure_recreation_drops_only_source_identity():
    conn, cursor = _connection_and_cursor()
    cursor.execute.side_effect = [RuntimeError("routine definition changed"), None, None]
    procedure = FunctionDef(
        name="refresh_customer_cache",
        schema_name="training",
        kind="procedure",
        identity_arguments="integer",
        ddl=(
            "CREATE OR REPLACE PROCEDURE training.refresh_customer_cache(customer_id integer) "
            "LANGUAGE sql AS $$ SELECT 1 $$"
        ),
    )

    create_function(conn, procedure)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[1] == (
        'DROP PROCEDURE IF EXISTS "training"."refresh_customer_cache"(integer)'
    )
    assert statements[2].startswith(
        "CREATE OR REPLACE PROCEDURE training.refresh_customer_cache(customer_id integer)"
    )
    assert not any("manual_target_procedure" in statement for statement in statements)
    assert not any("CASCADE" in statement.upper() for statement in statements)
    assert "CREATE OR REPLACE PROCEDURE" in routines[0].ddl


def test_modified_source_managed_view_is_replaced_from_source():
    conn, cursor = _connection_and_cursor()
    view = ViewDefinition(
        name="customer_summary",
        schema_name="training",
        definition="SELECT customerid, 5 AS customer_count FROM training.customers",
    )

    create_view(conn, view)

    assert cursor.execute.call_args.args[0] == (
        'CREATE OR REPLACE VIEW "training"."customer_summary" AS '
        "SELECT customerid, 5 AS customer_count FROM training.customers"
    )
    assert not any(
        call.args[0].startswith("DROP ")
        for call in cursor.execute.call_args_list
    )
    conn.commit.assert_called_once_with()


def test_incompatible_managed_view_rebuild_is_scoped_and_non_cascading():
    conn, cursor = _connection_and_cursor()
    cursor.execute.side_effect = [RuntimeError("view column type changed"), None, None]
    view = ViewDefinition(
        name="customer_summary",
        schema_name="training",
        definition="SELECT customerid::text AS customerid FROM training.customers",
    )

    create_view(conn, view)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[0].startswith('CREATE OR REPLACE VIEW "training"."customer_summary"')
    assert statements[1] == 'DROP VIEW "training"."customer_summary"'
    assert statements[2].startswith('CREATE VIEW "training"."customer_summary"')
    assert not any("CASCADE" in statement.upper() for statement in statements)
    assert not any("manual_view" in statement for statement in statements)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_called_once_with()
