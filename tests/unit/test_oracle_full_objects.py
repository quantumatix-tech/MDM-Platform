"""Mocked Oracle FULL object discovery and application contracts."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, FunctionDef, Schema, SequenceDef, TriggerDef, ViewDefinition
from core.connectors.oracle import objects
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import OracleTargetConnector
from core.orchestrator import MigrationOrchestrator
from tests.unit.test_oracle_full_migration import _OneTableOracleSource, _OneTableOracleTarget


def _connection():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    # A no-row Oracle fetchone() result is None; MagicMock's iterable default
    # is empty and is not representative of python-oracledb behavior.
    cursor.fetchone.return_value = None
    return conn, cursor


def test_oracle_discovers_views_with_ddl_and_source_schema_scope():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[("V_CUSTOMERS",)], [("V_CUSTOMERS", "VALID")], [], []]
    cur.fetchone.return_value = (
        'CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_OWNER"."V_CUSTOMERS" '
        '("ID") AS SELECT 1 ID FROM DUAL',
    )
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    views = source.list_views()

    assert views == [ViewDefinition(
        "V_CUSTOMERS",
        'CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_OWNER"."V_CUSTOMERS" '
        '("ID") AS SELECT 1 ID FROM DUAL',
        schema_name="APP_OWNER",
    )]
    assert any("DBMS_METADATA.GET_DDL" in str(call.args[0]) for call in cur.execute.call_args_list)


def test_oracle_selected_tables_filter_attached_triggers():
    conn, cur = _connection()
    trigger_rows = [
        ("TRG_INCLUDED", "DISCOVERY_INCLUDED_CUSTOMERS", "ENABLED", "BEFORE EACH ROW"),
        ("TRG_EXCLUDED", "CUSTOMERS", "ENABLED", "BEFORE EACH ROW"),
    ]

    def fetchall_for_query():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "FROM USER_TRIGGERS" in sql:
            return trigger_rows
        if "FROM USER_DEPENDENCIES" in sql:
            return []
        raise AssertionError(f"Unexpected fetchall query: {sql}")

    def fetchone_for_query():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "DBMS_METADATA.GET_DDL" in sql:
            return (f"CREATE OR REPLACE TRIGGER {cur.execute.call_args.args[1][1]} "
                    "BEFORE INSERT ON T BEGIN NULL; END;",)
        raise AssertionError(f"Unexpected fetchone query: {sql}")

    cur.fetchall.side_effect = fetchall_for_query
    cur.fetchone.side_effect = fetchone_for_query
    source = OracleSourceConnector({
        "username": "APP_OWNER", "include_tables": ["DISCOVERY_INCLUDED_CUSTOMERS"],
    })
    source._conn = conn

    triggers = source.get_all_triggers()

    assert [trigger.name for trigger in triggers] == ["TRG_INCLUDED"]
    assert "TRG_EXCLUDED" not in {trigger.name for trigger in triggers}
    dependency_checks = [
        call.args[1][0] for call in cur.execute.call_args_list
        if "FROM user_dependencies" in str(call.args[0])
    ]
    ddl_reads = [
        call.args[1][1] for call in cur.execute.call_args_list
        if "DBMS_METADATA.GET_DDL" in str(call.args[0])
    ]
    assert dependency_checks == ["TRG_INCLUDED"]
    assert ddl_reads == ["TRG_INCLUDED"]


def test_oracle_selected_tables_filter_views_by_table_dependencies():
    conn, cur = _connection()
    cur.fetchall.return_value = [
        ("V_INCLUDED", "DISCOVERY_INCLUDED_CUSTOMERS", "TABLE"),
        ("V_EXCLUDED", "CUSTOMERS", "TABLE"),
    ]

    selected = objects._filter_view_order_to_tables(
        conn,
        [("VIEW", "V_INCLUDED"), ("VIEW", "V_EXCLUDED")],
        {"DISCOVERY_INCLUDED_CUSTOMERS"},
    )

    assert selected == [("VIEW", "V_INCLUDED")]


def test_oracle_view_scope_filter_is_not_applied_without_include_tables():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[("V1",)], [("V1", "VALID")], [], []]
    cur.fetchone.return_value = ("CREATE OR REPLACE VIEW V1 AS SELECT 1 X FROM DUAL",)
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    assert [view.name for view in source.list_views()] == ["V1"]


def test_oracle_discovers_sequence_options_and_uses_catalog_high_water():
    conn, cur = _connection()
    cur.description = [(name,) for name in (
        "SEQUENCE_NAME", "MIN_VALUE", "MAX_VALUE", "INCREMENT_BY", "CYCLE_FLAG",
        "CACHE_SIZE", "LAST_NUMBER", "ORDER_FLAG", "SCALE_FLAG", "EXTEND_FLAG",
        "SESSION_FLAG", "KEEP_VALUE", "SHARDED_FLAG",
    )]
    cur.fetchall.side_effect = [[("CUSTOMER_SEQ", 1, 999999, 1, "N", 20, 1081,
                                  "N", "N", "N", "N", "N", "N")], []]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    [seq] = source.list_all_sequences()

    assert seq == SequenceDef(
        name="CUSTOMER_SEQ", schema="APP_OWNER", start_value=1081,
        min_value=1, max_value=999999, increment=1, cycle=False,
        last_value=1081, cache_size=20, is_cached=True,
        options={"order_flag": "N", "scale_flag": "N", "extend_flag": "N",
                 "session_flag": "N", "keep_value": "N", "sharded_flag": "N"},
    )


def test_oracle_rejects_sharded_sequences_instead_of_losing_sequence_semantics():
    conn, cur = _connection()
    cur.description = [(name,) for name in (
        "SEQUENCE_NAME", "MIN_VALUE", "MAX_VALUE", "INCREMENT_BY", "CYCLE_FLAG",
        "CACHE_SIZE", "LAST_NUMBER", "ORDER_FLAG", "SCALE_FLAG", "EXTEND_FLAG",
        "SESSION_FLAG", "KEEP_VALUE", "SHARDED_FLAG",
    )]
    cur.fetchall.side_effect = [[("SHARDED_SEQ", 1, 999, 1, "N", 20, 11,
                                  "N", "N", "N", "N", "N", "Y")], []]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    with pytest.raises(RuntimeError, match="SHARDED sequence.*unsupported"):
        source.list_all_sequences()


def test_oracle_sequence_discovery_maps_column_default_ownership():
    conn, cur = _connection()
    cur.description = [(name,) for name in (
        "SEQUENCE_NAME", "MIN_VALUE", "MAX_VALUE", "INCREMENT_BY", "CYCLE_FLAG",
        "CACHE_SIZE", "LAST_NUMBER", "ORDER_FLAG",
    )]
    cur.fetchall.side_effect = [
        [("CUSTOMER_SEQ", 1, 999999, 1, "N", 20, 1081, "N")],
        [("CUSTOMERS", "CUSTOMER_ID", "APP_OWNER.CUSTOMER_SEQ.NEXTVAL")],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    [seq] = source.list_all_sequences()

    assert seq.owned_by == "CUSTOMERS.CUSTOMER_ID"


def test_oracle_discovery_orders_package_spec_before_routines_and_body():
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("PKG", "PACKAGE", "VALID"), ("PKG", "PACKAGE BODY", "VALID"),
         ("CALC_TOTAL", "FUNCTION", "VALID")],
        [],  # package spec dependency order
        [],  # routine dependency order
        [],  # package body dependency order
        [],  # package source dependencies
        [],  # routine source dependencies
        [],  # package body source dependencies
    ]
    cur.fetchone.return_value = ("CREATE OR REPLACE PACKAGE PKG AS END PKG;",)
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    definitions = source.list_functions()

    assert [(definition.name, definition.kind) for definition in definitions] == [
        ("PKG", "package_spec"), ("CALC_TOTAL", "function"), ("PKG", "package_body")
    ]
    assert all(definition.schema_name == "APP_OWNER" for definition in definitions)


def test_oracle_package_ddl_is_requested_as_separate_specification_and_body():
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("PKG_OBJECT_TEST", "PACKAGE", "VALID"),
         ("PKG_OBJECT_TEST", "PACKAGE BODY", "VALID")],
        [], [], [], [], [],
    ]
    cur.fetchone.side_effect = [
        ("CREATE OR REPLACE PACKAGE PKG_OBJECT_TEST AS PROCEDURE RUN; END PKG_OBJECT_TEST;",),
        ("CREATE OR REPLACE PACKAGE BODY PKG_OBJECT_TEST AS PROCEDURE RUN IS BEGIN NULL; END; END PKG_OBJECT_TEST;",),
    ]
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    definitions = source.list_functions()

    ddl_calls = [call for call in cur.execute.call_args_list if "GET_DDL" in str(call.args[0])]
    assert [call.args[1][0] for call in ddl_calls] == ["PACKAGE_SPEC", "PACKAGE_BODY"]
    assert [definition.kind for definition in definitions] == ["package_spec", "package_body"]


def test_oracle_target_rejects_concatenated_package_spec_and_body_before_execution():
    conn, cur = _connection()
    cur.fetchall.return_value = []
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn
    bad_ddl = (
        "CREATE OR REPLACE PACKAGE PKG_OBJECT_TEST AS PROCEDURE RUN; END;\n/\n"
        "CREATE OR REPLACE PACKAGE BODY PKG_OBJECT_TEST AS BEGIN NULL; END;\n/"
    )

    with pytest.raises(RuntimeError, match=r"DDL boundary/type error.*found \['PACKAGE', 'PACKAGE BODY'\]"):
        target.create_function(FunctionDef(
            "PKG_OBJECT_TEST", bad_ddl, schema_name="MIGRATIONE2E_ORACLE_SOURCE",
            kind="package_spec",
        ))

    assert not any("CREATE OR REPLACE PACKAGE" in str(call.args[0]) for call in cur.execute.call_args_list)


def test_oracle_owner_remapping_changes_qualified_identifiers_not_literals_or_comments():
    ddl = (
        'CREATE OR REPLACE VIEW V1 AS SELECT * FROM "MIGRATIONE2E_ORACLE_SOURCE".CUSTOMERS;\n'
        "-- MIGRATIONE2E_ORACLE_SOURCE.CUSTOMERS stays a comment\n"
        'WHERE x = "migratione2e_oracle_source".CUSTOMERS'
    )

    remapped = objects._remap_owner_qualifiers(
        ddl, "MIGRATIONE2E_ORACLE_SOURCE", "MIGRATIONE2E_ORACLE_OBJECT_TARGET"
    )

    assert '"MIGRATIONE2E_ORACLE_OBJECT_TARGET".CUSTOMERS' in remapped
    assert "-- MIGRATIONE2E_ORACLE_SOURCE.CUSTOMERS" in remapped
    assert '"migratione2e_oracle_source".CUSTOMERS' in remapped


def test_oracle_owner_remapping_fails_closed_on_dynamic_source_owner_reference():
    with pytest.raises(RuntimeError, match="dynamic SQL owner remapping is unsupported"):
        objects._remap_owner_qualifiers(
            "CREATE OR REPLACE PROCEDURE P AS BEGIN "
            "EXECUTE IMMEDIATE 'SELECT * FROM SOURCE_OWNER.CUSTOMERS'; END;",
            "SOURCE_OWNER", "TARGET_OWNER",
        )


def test_oracle_target_remaps_source_owner_qualified_sequence_defaults():
    conn, cur = _connection()
    cur.fetchone.return_value = None
    target = OracleTargetConnector({"source_engine": "oracle", "username": "TARGET_OWNER"})
    target._conn = conn

    target.create_object_if_missing(Schema(
        "T", schema_name="SOURCE_OWNER",
        columns=[Column("ID", "NUMBER", default="SOURCE_OWNER.ID_SEQ.NEXTVAL")],
    ))

    ddl = str(cur.execute.call_args_list[-1].args[0])
    assert 'DEFAULT "TARGET_OWNER".ID_SEQ.NEXTVAL' in ddl
    assert '"SOURCE_OWNER".ID_SEQ.NEXTVAL' not in ddl


def test_oracle_trigger_source_owner_reference_is_remapped_and_target_table_is_required():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [], [("TRIGGER", "VALID")]]
    cur.fetchone.side_effect = [("CUSTOMERS",), None, ("ENABLED",)]
    target = OracleTargetConnector({"username": "MIGRATIONE2E_ORACLE_OBJECT_TARGET"})
    target._conn = conn
    target._managed_schemas["CUSTOMERS"] = MagicMock()
    trigger = TriggerDef(
        "TRG_CUSTOMERS_TEST", "CUSTOMERS",
        'CREATE OR REPLACE TRIGGER TRG_CUSTOMERS_TEST BEFORE INSERT ON '
        '"MIGRATIONE2E_ORACLE_SOURCE"."CUSTOMERS" FOR EACH ROW BEGIN NULL; END;',
        schema_name="MIGRATIONE2E_ORACLE_SOURCE",
    )

    target.create_trigger(trigger)

    ddl = next(
        str(call.args[0]) for call in cur.execute.call_args_list
        if "CREATE OR REPLACE TRIGGER" in str(call.args[0])
    )
    assert '"MIGRATIONE2E_ORACLE_OBJECT_TARGET"."CUSTOMERS"' in ddl
    assert '"MIGRATIONE2E_ORACLE_SOURCE"."CUSTOMERS"' not in ddl


def test_oracle_trigger_metadata_trailing_alter_state_is_extracted_separately():
    conn, cur = _connection()
    metadata_ddl = (
        'CREATE OR REPLACE EDITIONABLE TRIGGER '
        '"MIGRATIONE2E_ORACLE_SOURCE"."TRG_CUSTOMERS_TEST"\n'
        'BEFORE INSERT ON "MIGRATIONE2E_ORACLE_SOURCE"."CUSTOMERS"\n'
        'FOR EACH ROW\nBEGIN\n  :NEW.ID := :NEW.ID;\nEND;\n'
        'ALTER TRIGGER "MIGRATIONE2E_ORACLE_SOURCE"."TRG_CUSTOMERS_TEST" ENABLE;'
    )
    cur.fetchall.side_effect = [
        [("TRG_CUSTOMERS_TEST", "CUSTOMERS", "ENABLED", "BEFORE EACH ROW")],
        [],
    ]
    cur.fetchone.return_value = (metadata_ddl,)
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    [trigger] = source.get_all_triggers()

    assert trigger.is_disabled is False
    assert "ALTER TRIGGER" not in trigger.ddl
    assert trigger.ddl.rstrip().endswith("END;")


def test_oracle_target_executes_trigger_state_clause_separately_after_compile_check():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [], [("TRIGGER", "VALID")]]
    cur.fetchone.side_effect = [("CUSTOMERS",), None, ("ENABLED",)]
    target = OracleTargetConnector({"username": "MIGRATIONE2E_ORACLE_OBJECT_TARGET"})
    target._conn = conn
    target._managed_schemas["CUSTOMERS"] = MagicMock()
    trigger = TriggerDef(
        "TRG_CUSTOMERS_TEST", "CUSTOMERS",
        'CREATE OR REPLACE EDITIONABLE TRIGGER '
        '"MIGRATIONE2E_ORACLE_SOURCE"."TRG_CUSTOMERS_TEST"\n'
        'BEFORE INSERT ON "MIGRATIONE2E_ORACLE_SOURCE"."CUSTOMERS"\n'
        'FOR EACH ROW\nBEGIN\n  :NEW.ID := :NEW.ID;\nEND;\n'
        'ALTER TRIGGER "MIGRATIONE2E_ORACLE_SOURCE"."TRG_CUSTOMERS_TEST" ENABLE;',
        schema_name="MIGRATIONE2E_ORACLE_SOURCE",
    )

    target.create_trigger(trigger)

    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    create_index = next(
        i for i, statement in enumerate(statements)
        if "CREATE OR REPLACE EDITIONABLE TRIGGER" in statement
    )
    compile_status_index = next(
        i for i, statement in enumerate(statements)
        if "user_errors" in statement.casefold()
    )
    alters = [(i, statement) for i, statement in enumerate(statements) if "ALTER TRIGGER" in statement]
    assert "ALTER TRIGGER" not in statements[create_index]
    assert '"MIGRATIONE2E_ORACLE_OBJECT_TARGET"."CUSTOMERS"' in statements[create_index]
    assert len(alters) == 2
    assert all(i > compile_status_index for i, _ in alters)
    assert not any("MIGRATIONE2E_ORACLE_SOURCE" in statement for _, statement in alters)


@pytest.mark.parametrize("modifier", ["FORCE", "NO FORCE", "FORCE EDITIONABLE"])
def test_oracle_view_validator_accepts_supported_force_modifiers(modifier):
    ddl = f"CREATE OR REPLACE {modifier} VIEW V_CUSTOMERS_TEST AS SELECT 1 ID FROM DUAL;"
    assert objects._validate_single_create(ddl, "VIEW", "V_CUSTOMERS_TEST") == ddl


def test_oracle_view_validator_rejects_appended_unrelated_or_multiple_statements():
    for ddl in (
        "CREATE FORCE VIEW V1 AS SELECT 1 ID FROM DUAL; DROP VIEW V1",
        "CREATE VIEW V1 AS SELECT 1 ID FROM DUAL; ALTER VIEW V1 COMPILE",
        "CREATE VIEW V1 AS SELECT 1 ID FROM DUAL; CREATE VIEW V2 AS SELECT 2 ID FROM DUAL",
    ):
        with pytest.raises(RuntimeError, match="DDL boundary/type error"):
            objects._validate_single_create(ddl, "VIEW", "V1")


def test_oracle_single_create_validator_ignores_create_text_in_plsql_literals():
    ddl = "CREATE OR REPLACE PACKAGE BODY P AS BEGIN EXECUTE IMMEDIATE 'CREATE TABLE X(A NUMBER)'; END P;"

    assert objects._validate_single_create(ddl, "PACKAGE BODY", "P") == ddl


def test_oracle_discovery_rejects_cross_schema_dependencies():
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("V1",)], [("V1", "VALID")], [],
        [("OTHER_OWNER", "SECRET_TABLE", "TABLE")],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    with pytest.raises(RuntimeError, match="cross-schema TABLE OTHER_OWNER.SECRET_TABLE"):
        source.list_views()


@pytest.mark.parametrize(
    ("method", "definition", "sql_fragment"),
    [
        ("create_view", ViewDefinition("V1", "CREATE OR REPLACE VIEW V1 AS SELECT 1 X FROM DUAL"), "CREATE OR REPLACE VIEW"),
        ("create_function", FunctionDef("F1", "CREATE OR REPLACE FUNCTION F1 RETURN NUMBER AS BEGIN RETURN 1; END;", kind="function"), "CREATE OR REPLACE FUNCTION"),
        ("create_function", FunctionDef("P1", "CREATE OR REPLACE PROCEDURE P1 AS BEGIN NULL; END;", kind="procedure"), "CREATE OR REPLACE PROCEDURE"),
        ("create_function", FunctionDef("PKG", "CREATE OR REPLACE PACKAGE PKG AS END PKG;", kind="package_spec"), "CREATE OR REPLACE PACKAGE"),
        ("create_function", FunctionDef("PKG", "CREATE OR REPLACE PACKAGE BODY PKG AS END PKG;", kind="package_body"), "CREATE OR REPLACE PACKAGE BODY"),
    ],
)
def test_oracle_applies_views_functions_procedures_and_package_parts(method, definition, sql_fragment):
    conn, cur = _connection()
    expected_type = "VIEW" if method == "create_view" else {
        "function": "FUNCTION", "procedure": "PROCEDURE",
        "package_spec": "PACKAGE", "package_body": "PACKAGE BODY",
    }[definition.kind]
    cur.fetchall.side_effect = [[], [], [], [(expected_type, "VALID")]]
    target = OracleTargetConnector({})
    target._conn = conn

    getattr(target, method)(definition)

    assert any(sql_fragment in str(call.args[0]) for call in cur.execute.call_args_list)
    conn.commit.assert_called()


def test_oracle_reapplies_source_managed_view_on_repeated_full_object_phase():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [], [], [("VIEW", "VALID")], [], [], [], [("VIEW", "VALID")]]
    target = OracleTargetConnector({})
    target._conn = conn
    view = ViewDefinition("V1", "CREATE OR REPLACE VIEW V1 AS SELECT 1 X FROM DUAL")

    target.create_view(view)
    target.create_view(view)

    assert sum("CREATE OR REPLACE VIEW" in str(call.args[0]) for call in cur.execute.call_args_list) == 2
    assert not any("DROP VIEW" in str(call.args[0]) for call in cur.execute.call_args_list)


def test_oracle_replaces_partial_package_spec_idempotently_without_dropping_target_object():
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("PACKAGE",), ("PACKAGE BODY",)], [], [], [("PACKAGE", "VALID")],
        [("PACKAGE",), ("PACKAGE BODY",)], [], [], [("PACKAGE", "VALID")],
    ]
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn
    spec = FunctionDef(
        "PKG_OBJECT_TEST", "CREATE OR REPLACE PACKAGE PKG_OBJECT_TEST AS PROCEDURE RUN; END;",
        schema_name="TARGET_OWNER", kind="package_spec",
    )

    target.create_function(spec)
    target.create_function(spec)

    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert sum("CREATE OR REPLACE PACKAGE PKG_OBJECT_TEST" in sql for sql in statements) == 2
    assert not any("DROP PACKAGE" in sql for sql in statements)


def test_oracle_creates_and_reconciles_sequence_without_rewinding_existing_state():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], []]
    cur.fetchone.side_effect = [None, ("CUSTOMER_SEQ",), (50,)]
    target = OracleTargetConnector({})
    target._conn = conn
    seq = SequenceDef("CUSTOMER_SEQ", 100, 1, 999999, 1, False, cache_size=20)

    target.create_sequence(seq)
    target.create_sequence(seq)

    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert any("CREATE SEQUENCE" in statement and "START WITH 100" in statement for statement in statements)
    assert any("ALTER SEQUENCE" in statement for statement in statements)
    assert not any("DROP SEQUENCE" in statement for statement in statements)


def test_oracle_sequence_creation_preserves_release_specific_sequence_options():
    conn, cur = _connection()
    cur.fetchall.return_value = []
    cur.fetchone.side_effect = [("T",), None]
    target = OracleTargetConnector({})
    target._conn = conn
    target._managed_schemas["T"] = MagicMock()
    seq = SequenceDef(
        "SCALED_SEQ", 50, 1, 999999, 1, False,
        options={"order_flag": "Y", "scale_flag": "Y", "extend_flag": "Y",
                 "session_flag": "Y", "keep_value": "Y"},
    )

    target.create_sequence(seq)

    sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "ORDER" in sql
    assert "SCALE EXTEND" in sql
    assert "SESSION" in sql
    assert "KEEP" in sql


def test_oracle_sequence_uses_oracle_26ai_noscale_keyword_with_nocache_and_noorder():
    conn, cur = _connection()
    cur.fetchone.return_value = None
    target = OracleTargetConnector({})
    target._conn = conn
    seq = SequenceDef(
        "SEQ_OBJECT_TEST", 10, 1, 999999, 1, False, is_cached=False,
        options={"order_flag": "N", "scale_flag": "N"},
    )

    target.create_sequence(seq)

    create_sql = next(
        str(call.args[0]) for call in cur.execute.call_args_list
        if str(call.args[0]).startswith("CREATE SEQUENCE")
    )
    assert "NOCACHE NOORDER NOSCALE" in create_sql
    assert "NO SCALE" not in create_sql


def test_oracle_sequence_default_options_and_repeat_full_preserve_existing_state():
    conn, cur = _connection()
    cur.fetchone.side_effect = [None, ("SEQ_DEFAULT",), (100,)]
    target = OracleTargetConnector({})
    target._conn = conn
    seq = SequenceDef("SEQ_DEFAULT", 50, 1, 999999, 1, False, is_cached=False)

    target.create_sequence(seq)
    target.create_sequence(seq)

    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    create_sql = next(sql for sql in statements if sql.startswith("CREATE SEQUENCE"))
    alter_sql = next(sql for sql in statements if sql.startswith("ALTER SEQUENCE"))
    assert "NOCACHE" in create_sql
    assert "NOORDER" in create_sql
    assert "START WITH 50" in create_sql
    assert "ALTER SEQUENCE" in alter_sql
    assert "START WITH" not in alter_sql
    assert not any("DROP SEQUENCE" in sql for sql in statements)


@pytest.mark.parametrize("generation_type", ["ALWAYS", "BY DEFAULT"])
def test_oracle_identity_schema_preserves_generation_and_options(generation_type):
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("ID", "NUMBER", 22, 22, None, 10, 0, "N", None, "YES", "NO")],
        [("ID", generation_type, "START WITH: 7 INCREMENT BY: 5 MIN_VALUE: 1")],
        [], [],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = conn

    schema = source.get_schema("T")

    assert schema.columns[0].is_identity is True
    assert schema.columns[0].identity_seed == 7
    assert schema.columns[0].identity_increment == 5
    assert schema.columns[0].identity_kind == generation_type
    target = OracleTargetConnector({})
    target._conn = conn
    target._managed_schemas["T"] = schema
    target.sync_auto_increment("T", "ID")
    sync_sql = str(cur.execute.call_args.args[0])
    assert f"GENERATED {generation_type} AS IDENTITY" in sync_sql
    assert "START WITH LIMIT VALUE" in sync_sql


@pytest.mark.parametrize("is_disabled", [True, False])
def test_oracle_trigger_is_disabled_during_creation_then_source_state_is_restored(is_disabled):
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [], [("TRIGGER", "VALID")]]
    cur.fetchone.side_effect = [("T",), None, ("DISABLED" if is_disabled else "ENABLED",)]
    target = OracleTargetConnector({})
    target._conn = conn
    target._managed_schemas["T"] = MagicMock()
    trigger = TriggerDef(
        "TRG_T", "T", "CREATE OR REPLACE TRIGGER TRG_T BEFORE INSERT ON T BEGIN NULL; END;",
        schema_name="APP_OWNER", is_disabled=is_disabled,
    )

    target.create_trigger(trigger)

    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert any("ALTER TRIGGER \"TRG_T\" DISABLE" in statement for statement in statements)
    assert any((" DISABLE" if is_disabled else " ENABLE") in statement for statement in statements)


def test_oracle_compilation_errors_are_reported_not_swallowed():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [], [("VIEW", 3, 4, "ORA-00942: table or view does not exist")], []]
    target = OracleTargetConnector({})
    target._conn = conn

    with pytest.raises(RuntimeError, match="Oracle compilation/status check failed.*ORA-00942"):
        target.create_view(ViewDefinition("V1", "CREATE OR REPLACE VIEW V1 AS SELECT X FROM MISSING"))


def test_oracle_target_object_type_collision_fails_without_drop_or_replace():
    conn, cur = _connection()
    cur.fetchall.return_value = [("TABLE",)]
    target = OracleTargetConnector({})
    target._conn = conn

    with pytest.raises(RuntimeError, match="already exists as TABLE"):
        target.create_view(ViewDefinition("V1", "CREATE OR REPLACE VIEW V1 AS SELECT 1 X FROM DUAL"))

    assert not any("DROP" in str(call.args[0]) for call in cur.execute.call_args_list)


def test_oracle_target_only_dependent_view_blocks_replacement_before_ddl():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [("TARGET_ONLY_VIEW", "VIEW")]]
    target = OracleTargetConnector({})
    target._conn = conn
    target.set_managed_source_objects({"views": [ViewDefinition("SOURCE_VIEW", "DDL")]})

    with pytest.raises(RuntimeError, match="target-only dependent VIEW TARGET_ONLY_VIEW"):
        target.create_view(ViewDefinition("SOURCE_VIEW", "CREATE OR REPLACE VIEW SOURCE_VIEW AS SELECT 1 X FROM DUAL"))

    assert not any("CREATE OR REPLACE VIEW" in str(call.args[0]) for call in cur.execute.call_args_list)


def test_oracle_trigger_suspend_only_disables_matching_enabled_source_trigger():
    conn, cur = _connection()
    cur.fetchone.side_effect = [("T", "ENABLED"), ("T", "DISABLED")]
    target = OracleTargetConnector({})
    target._conn = conn
    triggers = [
        TriggerDef("TRG_ENABLED", "T", "DDL"),
        TriggerDef("TRG_DISABLED", "T", "DDL", is_disabled=True),
    ]

    suspended = target.suspend_triggers_for_data_load(triggers)

    assert [trigger.name for trigger in suspended] == ["TRG_ENABLED"]
    assert target._suspended_trigger_names == ["TRG_ENABLED"]
    target.restore_suspended_triggers()
    assert any("ALTER TRIGGER \"TRG_ENABLED\" ENABLE" in str(call.args[0]) for call in cur.execute.call_args_list)


def test_oracle_privilege_preflight_names_missing_owner_privileges():
    conn, cur = _connection()
    cur.fetchall.return_value = [("CREATE VIEW",)]

    with pytest.raises(RuntimeError, match="CREATE PROCEDURE"):
        objects.validate_privileges(
            conn,
            views=[ViewDefinition("V1", "DDL")], sequences=[],
            routines=[FunctionDef("PKG", "DDL", kind="package_spec")], triggers=[],
        )


def test_oracle_object_preflight_failure_preserves_target_rows_and_skips_full_clear():
    class Source(_OneTableOracleSource):
        def list_all_sequences(self):
            return []

        def list_views(self):
            return [ViewDefinition("V1", "CREATE OR REPLACE VIEW V1 AS SELECT 1 X FROM DUAL")]

    class Target(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.rows = [{"ID": 99}]

        def validate_object_privileges(self, **_definitions):
            return None

        def set_managed_source_objects(self, _definitions):
            return None

        def create_view(self, _view):
            raise RuntimeError("Oracle compilation failed for V1: ORA-00942")

    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }
    target = Target()

    result = MigrationOrchestrator(Source(), target, config).run_full()

    assert result["phases"]["oracle_object_preparation"]["status"] == "failed"
    assert result["phases"]["full_target_sync"]["status"] == "skipped"
    assert result["phases"]["data_load"]["status"] == "skipped"
    assert target.rows == [{"ID": 99}]
    assert target.cleared == []


def test_oracle_sequence_preparation_failure_is_reported_and_skips_mismatch_validation():
    from unittest.mock import patch

    class Source(_OneTableOracleSource):
        def list_all_sequences(self):
            return [SequenceDef("SEQ_OBJECT_TEST", 1, 1, 999999, 1, False)]

    class Target(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.rows = [{"ID": 99}]

        def create_sequence(self, _seq):
            raise RuntimeError("ORA-03049: malformed sequence option")

    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }
    target = Target()

    with patch("core.orchestrator.audit_log") as audit_log_mock:
        result = MigrationOrchestrator(Source(), target, config).run_full()

    [failure] = result["phases"]["object_failures"]
    assert failure["phase"] == "create_sequence"
    assert failure["object"] == "SEQ_OBJECT_TEST"
    assert failure["exception"] == "ORA-03049: malformed sequence option"
    assert "Oracle target" in failure["error"]
    assert "ORA-03049" in failure["error"]
    assert "Inspect target data and schema before rerunning FULL migration." in failure["error"]
    assert failure["diagnostic"]["error_code"] == "ORA-03049"
    assert result["phases"]["validation"]["status"] == "skipped"
    assert target.rows == [{"ID": 99}]
    assert target.cleared == []
    assert any(
        call.kwargs.get("phase") == "create_sequence"
        and call.kwargs.get("status") == "error"
        for call in audit_log_mock.call_args_list
    )


def test_oracle_preflight_error_count_matches_object_failures_without_retry_duplicates():
    from core.connectors.base import FunctionDef

    class Source(_OneTableOracleSource):
        def list_all_sequences(self):
            return []

        def list_functions(self):
            return [
                FunctionDef("PKG_OBJECT_TEST", "CREATE...", "SOURCE_OWNER", "package_spec"),
                FunctionDef("PKG_OBJECT_TEST", "CREATE...", "SOURCE_OWNER", "package_body"),
            ]

        def get_all_triggers(self):
            return [TriggerDef("TRG_CUSTOMERS_TEST", "T", "CREATE...", "SOURCE_OWNER")]

    class Target(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.rows = [{"ID": 99}]

        def set_managed_source_objects(self, _definitions):
            return None

        def validate_object_privileges(self, **_definitions):
            return None

        def create_function(self, definition):
            raise RuntimeError(f"PLS compile error in {definition.kind}")

        def create_trigger(self, _definition):
            raise RuntimeError("ORA-00942: source-owner table reference")

    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }
    target = Target()

    result = MigrationOrchestrator(Source(), target, config).run_full()

    assert len(result["phases"]["object_failures"]) == 3
    assert len(result["errors"]) == 3
    assert target.rows == [{"ID": 99}]
    assert target.cleared == []


def test_oracle_full_object_phase_is_idempotently_reapplied_on_repeat_runs():
    class Source(_OneTableOracleSource):
        def list_all_sequences(self):
            return []

        def list_views(self):
            return [ViewDefinition("V1", "CREATE OR REPLACE VIEW V1 AS SELECT 1 X FROM DUAL")]

    class Target(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.view_applications = 0

        def validate_object_privileges(self, **_definitions):
            return None

        def set_managed_source_objects(self, _definitions):
            return None

        def create_view(self, _view):
            self.view_applications += 1

    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }
    source, target = Source(), Target()

    first = MigrationOrchestrator(source, target, config).run_full()
    second = MigrationOrchestrator(source, target, config).run_full()

    assert first["phases"]["oracle_object_preparation"]["status"] == "success"
    assert second["phases"]["oracle_object_preparation"]["status"] == "success"
    assert target.view_applications == 2  # exactly one preflight application per FULL run
