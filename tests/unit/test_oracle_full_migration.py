"""Mocked table-and-row Oracle FULL migration contract tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column,
    ForeignKey,
    Index,
    MaterializedViewDef,
    Schema,
    SourceConnector,
    TargetConnector,
    UniqueConstraint,
    UnmappedTypeError,
    UpsertResult,
)
from core.connectors.oracle.indexes import normalize_index_rows
from core.connectors.oracle.precision import oracle_full_unsupported_datatypes
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import (
    OracleTargetConnector,
    _oracle_column_type,
    _oracle_constraint_matches,
    _oracle_constraint_name,
    _schemas_compatible,
)
from core.connectors.oracle.type_mapping import map_oracle_source_column
from core.dependency_order import order_data_load_objects
from core.orchestrator import MigrationOrchestrator
from core.schema_mapping import TypeMappingRegistry


def _connection_cursor():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    # python-oracledb fetchone() returns None when the catalog query has no row.
    # Oracle get_schema() now probes USER_PART_TABLES for optional partition metadata.
    cursor.fetchone.return_value = None
    return connection, cursor


def test_oracle_source_discovers_objects_and_schema_metadata():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [("PARENT_T",), ("CHILD_T",)],
        [
            ("ID", "NUMBER", 22, 22, None, 8, 0, "N", None, "NO", "NO"),
            ("VALUE", "VARCHAR2", 64, 40, "C", None, None, "Y", None, "NO", "NO"),
        ],
        [("PK_PARENT", "P", "ID", 1, None, None, None, None, "USER NAME")],
        [("IDX_VALUE", "NONUNIQUE", "NORMAL", "VALID", "NO", "VALUE", 1, "ASC", None)],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    assert source.list_objects() == ["PARENT_T", "CHILD_T"]
    schema = source.get_schema("PARENT_T")

    assert schema.schema_name == "APP_OWNER"
    assert [(c.name, c.source_type, c.precision, c.scale, c.nullable) for c in schema.columns] == [
        ("ID", "NUMBER", 8, 0, False),
        ("VALUE", "VARCHAR2", None, None, True),
    ]
    assert schema.primary_key == ["ID"]
    assert schema.indexes == [Index(
        name="IDX_VALUE", columns=["VALUE"], index_type="NORMAL"
    )]
    metadata_query = cursor.execute.call_args_list[1].args[0].lower()
    assert "from user_tab_cols" in metadata_query
    assert "hidden_column = 'no'" in metadata_query
    assert "virtual_column" in metadata_query
    index_query = next(
        call.args[0].lower() for call in cursor.execute.call_args_list
        if "from user_indexes i" in call.args[0].lower()
    )
    assert "from user_indexes" in index_query
    assert "constraint_type in ('p','u')" in index_query
    assert "from user_lobs" in index_query


def test_oracle_source_marks_system_generated_not_null_checks():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [("CUSTOMER_NAME", "VARCHAR2", 80, 80, "C", None, None, "N", None, "NO", "NO")],
        [(
            "SYS_C008715", "C", None, 1, None, None, None,
            '"CUSTOMER_NAME" IS NOT NULL', "GENERATED NAME",
        )],
        [],
    ]
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = connection

    schema = source.get_schema("CUSTOMERS")

    assert schema.columns[0].nullable is False
    assert schema.check_constraints[0].name == "SYS_C008715"
    assert schema.options["oracle_generated_check_constraints"] == {"SYS_C008715"}


def test_oracle_schema_discovery_reads_virtual_flag_from_tab_cols_catalog():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [("ID", "NUMBER", 22, 22, None, 8, 0, "N", None, "NO", "NO")],
        [],
        [],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    schema = source.get_schema("T")

    query = cursor.execute.call_args_list[0].args[0].lower()
    assert "from user_tab_cols" in query
    assert "from user_tab_columns" not in query
    assert "virtual_column" in query
    assert schema.columns[0].generated_kind is None


def test_oracle_schema_discovery_preserves_supported_virtual_expression():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [
            ("QUANTITY", "NUMBER", 22, 22, None, 8, 0, "N", None, "NO", "NO"),
            ("UNIT_PRICE", "NUMBER", 22, 22, None, 8, 2, "N", None, "NO", "NO"),
            ("TOTAL_AMOUNT", "NUMBER", 22, 22, None, 10, 2, "Y", '"QUANTITY" * "UNIT_PRICE"', "NO", "YES"),
        ],
        [],
        [],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    schema = source.get_schema("T")

    generated = schema.columns[-1]
    assert generated.generated_kind == "VIRTUAL"
    assert generated.generated == '"QUANTITY" * "UNIT_PRICE"'
    assert generated.default is None


def test_oracle_xmltype_virtual_metadata_without_expression_is_not_generated_column():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [("XML_PAYLOAD", "XMLTYPE", 0, 0, None, None, None, "Y", None, "NO", "YES")],
        [("XML_PAYLOAD", None, None, None, "TRANSPORTABLE BINARY", "YES", "YES")],
        [],
        [],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    schema = source.get_schema("T")

    assert schema.columns[0].source_type == "XMLTYPE"
    assert schema.columns[0].generated is None
    assert schema.columns[0].generated_kind is None
    assert schema.options["oracle_xml_storage"]["XML_PAYLOAD"]["storage_type"] == "TRANSPORTABLE BINARY"


def test_oracle_native_xml_json_and_raw_type_mapping():
    assert _oracle_column_type("oracle", Column("X", "XMLTYPE")) == "XMLTYPE"
    assert _oracle_column_type("oracle", Column("J", "JSON")) == "JSON"
    assert _oracle_column_type("oracle", Column("R", "RAW", size=2000)) == "RAW(2000)"


def test_oracle_failure_report_includes_location_code_and_sanitized_audit_trace(monkeypatch, capsys):
    import core.orchestrator as orchestrator_module
    from core.reporting.report_builder import ReportBuilder
    from migration_platform.__main__ import print_object_failures

    audit = MagicMock()
    monkeypatch.setattr(orchestrator_module, "audit_log", audit)
    orchestrator = object.__new__(MigrationOrchestrator)
    orchestrator._config = {"target": {"engine": "oracle"}}
    failures = []
    all_errors = []
    try:
        try:
            raise ValueError("root socket failure")
        except ValueError as root:
            raise RuntimeError(
                "DPY-1001: not connected; password=hunter2"
            ) from root
    except RuntimeError as exc:
        message = orchestrator._record_object_failure(
            failures, all_errors, "oracle_schema_replacement_verification", "OWNER", exc
        )

    assert "Oracle target" in message
    assert "DPY-1001" in message
    assert "password=<redacted>" in message
    diagnostic = failures[0]["diagnostic"]
    assert diagnostic["location"]["function"] == "test_oracle_failure_report_includes_location_code_and_sanitized_audit_trace"
    assert diagnostic["data_migration_completed"] is True
    assert diagnostic["cause_chain"][-1] == "ValueError: root socket failure"
    assert "hunter2" not in repr(failures)
    audit_details = audit.call_args.kwargs["details"]
    assert "test_oracle_failure_report_includes_location_code" in audit_details["diagnostic"]["stack_trace"]
    assert "hunter2" not in audit_details["diagnostic"]["stack_trace"]
    report_result = {
        "run_id": "oracle-diagnostic",
        "mode": "full",
        "status": "failed",
        "phases": {"object_failures": failures},
    }
    report = ReportBuilder(report_result, 0.0, 1.0)
    assert "DPY-1001" in report.build_json()["phases"]["object_failures"][0]["error"]
    assert "DPY-1001" in report.build_html()
    print_object_failures(report_result)
    terminal = capsys.readouterr().out
    assert "DPY-1001" in terminal
    assert "hunter2" not in terminal


@pytest.mark.parametrize(
    ("failure_side", "expected_connector", "failure_code"),
    [
        ("source", "Oracle source", "DPY-4011"),
        ("target", "Oracle target", "ORA-03113"),
    ],
)
def test_oracle_full_data_load_diagnostics_identify_connector_in_report_and_audit(
    failure_side, expected_connector, failure_code, monkeypatch,
):
    import core.orchestrator as orchestrator_module
    from core.reporting.report_builder import ReportBuilder

    audit = MagicMock()
    monkeypatch.setattr(orchestrator_module, "audit_log", audit)

    class FailingSource(_OneTableOracleSource):
        def export_full(self, object_name, schema_name=None):
            if failure_side == "source":
                yield {"ID": 1}
                raise RuntimeError(f"{failure_code}: connection lost during fetch")
            yield {"ID": 1}

    class FailingTarget(_OneTableOracleTarget):
        def upsert_batch(self, object_name, rows, schema=None):
            if failure_side == "target":
                raise RuntimeError(f"{failure_code}: target write failed")
            return super().upsert_batch(object_name, rows, schema)

    source = FailingSource()
    target = FailingTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 1, "stop_on_error": True},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["status"] == "failed"
    object_failures = result["phases"]["object_failures"]
    data_failure = next(item for item in object_failures if item["phase"] == "data_load")
    run_failure = next(item for item in object_failures if item["phase"] == "run_full")
    for failure in (data_failure, run_failure):
        diagnostic = failure["diagnostic"]
        assert diagnostic["connector"] == expected_connector
        assert diagnostic["error_code"] == failure_code

    report = ReportBuilder(result, 0.0, 1.0).build_json()
    report_failures = report["phases"]["object_failures"]
    assert [item["diagnostic"]["connector"] for item in report_failures] == [
        expected_connector, expected_connector,
    ]
    audit_diagnostics = [
        call.kwargs["details"]["diagnostic"]
        for call in audit.call_args_list
        if call.kwargs.get("phase") in {"data_load", "run_full"}
        and "diagnostic" in call.kwargs.get("details", {})
    ]
    assert [item["connector"] for item in audit_diagnostics] == [
        expected_connector, expected_connector,
    ]
    if failure_side == "source":
        # One batch was written; the next lazy SOURCE fetch raised.
        assert target.rows == [{"ID": 1}]


@pytest.mark.parametrize("expression", [
    "UPPER(NAME)", "QUANTITY + OTHER_TABLE.VALUE", "SELECT 1 FROM DUAL",
    "QUANTITY || UNIT_PRICE", "QUANTITY + MISSING_COLUMN",
])
def test_oracle_schema_discovery_rejects_unsupported_virtual_expression_before_more_queries(expression):
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [
        ("TOTAL_AMOUNT", "NUMBER", 22, 22, None, 10, 2, "Y", expression, "NO", "YES")
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    with pytest.raises(ValueError, match="unsupported expression"):
        source.get_schema("T")

    assert cursor.execute.call_count == 1


def test_oracle_target_creates_virtual_column_with_source_expression():
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    schema = Schema(
        name="VIRTUAL_COLUMN_VALIDATION",
        columns=[
            Column(name="QUANTITY", source_type="NUMBER", precision=8, scale=0, nullable=False),
            Column(name="UNIT_PRICE", source_type="NUMBER", precision=8, scale=2, nullable=False),
            Column(
                name="TOTAL_AMOUNT", source_type="NUMBER", precision=10, scale=2,
                generated='"QUANTITY" * "UNIT_PRICE"', generated_kind="VIRTUAL",
            ),
        ],
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = connection

    assert target.create_object_if_missing(schema) == "created"

    create_sql = next(
        call.args[0] for call in cursor.execute.call_args_list
        if call.args[0].startswith("CREATE TABLE")
    )
    assert '"TOTAL_AMOUNT" NUMBER(10,2) GENERATED ALWAYS AS ("QUANTITY" * "UNIT_PRICE") VIRTUAL' in create_sql


def test_oracle_full_load_omits_virtual_columns_and_preserves_always_identity_mode():
    connection, cursor = _connection_cursor()
    schema = Schema(
        name="VIRTUAL_COLUMN_VALIDATION",
        columns=[
            Column(name="RECORD_ID", source_type="NUMBER", is_identity=True, identity_kind="ALWAYS"),
            Column(name="QUANTITY", source_type="NUMBER"),
            Column(name="UNIT_PRICE", source_type="NUMBER"),
            Column(name="TOTAL_AMOUNT", source_type="NUMBER", generated="QUANTITY * UNIT_PRICE", generated_kind="VIRTUAL"),
        ],
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = connection

    result = target.upsert_batch(
        schema.name,
        [{"RECORD_ID": 5, "QUANTITY": 3, "UNIT_PRICE": 4, "TOTAL_AMOUNT": 12}],
        schema,
    )

    assert result.success_count == 1
    insert_sql = cursor.executemany.call_args.args[0]
    assert "TOTAL_AMOUNT" not in insert_sql
    assert '"RECORD_ID"' in insert_sql
    assert any("GENERATED BY DEFAULT AS IDENTITY" in call.args[0] for call in cursor.execute.call_args_list)
    assert any("GENERATED ALWAYS AS IDENTITY" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_reconciliation_adds_missing_virtual_column(monkeypatch):
    connection, cursor = _connection_cursor()
    source_schema = Schema(
        name="VIRTUAL_COLUMN_VALIDATION",
        columns=[
            Column(name="QUANTITY", source_type="NUMBER", precision=8, scale=0),
            Column(name="UNIT_PRICE", source_type="NUMBER", precision=8, scale=2),
            Column(
                name="TOTAL_AMOUNT", source_type="NUMBER", precision=10, scale=2,
                generated="QUANTITY * UNIT_PRICE", generated_kind="VIRTUAL",
            ),
        ],
    )
    target_schema = Schema(
        name=source_schema.name,
        columns=source_schema.columns[:2],
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = connection
    monkeypatch.setattr(target, "inspect_schema", lambda _name: target_schema)

    assert target.reconcile_oracle_table(source_schema) == "reconciled"

    alter_sql = cursor.execute.call_args.args[0]
    assert 'ADD ("TOTAL_AMOUNT" NUMBER(10,2) GENERATED ALWAYS AS (QUANTITY * UNIT_PRICE) VIRTUAL)' in alter_sql


def test_oracle_reconciliation_rejects_different_virtual_expression_before_ddl(monkeypatch):
    connection, cursor = _connection_cursor()
    source_schema = Schema(
        name="VIRTUAL_COLUMN_VALIDATION",
        columns=[
            Column(name="QUANTITY", source_type="NUMBER"),
            Column(name="UNIT_PRICE", source_type="NUMBER"),
            Column(name="TOTAL_AMOUNT", source_type="NUMBER", generated="QUANTITY * UNIT_PRICE", generated_kind="VIRTUAL"),
        ],
    )
    target_schema = Schema(
        name=source_schema.name,
        columns=[
            Column(name="QUANTITY", source_type="NUMBER"),
            Column(name="UNIT_PRICE", source_type="NUMBER"),
            Column(name="TOTAL_AMOUNT", source_type="NUMBER", generated="QUANTITY + UNIT_PRICE", generated_kind="VIRTUAL"),
        ],
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = connection
    monkeypatch.setattr(target, "inspect_schema", lambda _name: target_schema)

    with pytest.raises(RuntimeError, match="target generated expression differs from source"):
        target.reconcile_oracle_table(source_schema)

    assert cursor.execute.call_count == 0


def test_oracle_schema_discovery_rejects_unsupported_index_types_clearly():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [("CITY", "VARCHAR2", 50, 50, "B", None, None, "Y", None, "NO", "NO")],
        [],
        [("BITMAP_CITY", "NONUNIQUE", "BITMAP", "VALID", "NO", "CITY", 1, "ASC", None)],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    with pytest.raises(ValueError, match="unsupported for FULL migration"):
        source.get_schema("CUSTOMERS")


def test_oracle_index_catalog_normalizes_normal_descending_and_timestamp_tz_keys():
    rows = [
        ("IX_MIXED", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
         "CUSTOMER_ID", 1, "ASC", None),
        ("IX_MIXED", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
         "SYS_NC00002$", 2, "ASC", 'SYS_EXTRACT_UTC("ORDER_DATE")'),
        ("IX_DESC", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
         "SYS_NC00003$", 1, "DESC", 'SYS_OP_DESCEND("CUSTOMER_ID")'),
    ]

    indexes = normalize_index_rows(rows, {
        "CUSTOMER_ID": "NUMBER",
        "ORDER_DATE": "TIMESTAMP(6) WITH TIME ZONE",
    })

    assert indexes["IX_MIXED"]["supported"] is True
    assert indexes["IX_MIXED"]["columns"] == ["CUSTOMER_ID", "ORDER_DATE"]
    assert indexes["IX_MIXED"]["descending_columns"] == []
    assert indexes["IX_DESC"]["supported"] is True
    assert indexes["IX_DESC"]["columns"] == ["CUSTOMER_ID"]
    assert indexes["IX_DESC"]["descending_columns"] == ["CUSTOMER_ID"]


@pytest.mark.parametrize(
    ("index_type", "status", "partitioned", "expression", "column_types", "reason"),
    [
        ("FUNCTION-BASED NORMAL", "VALID", "NO", 'UPPER("CUSTOMER_ID")',
         {"CUSTOMER_ID": "NUMBER"}, "unsupported key expression"),
        ("NORMAL", "UNUSABLE", "NO", None,
         {"CUSTOMER_ID": "NUMBER"}, "index status"),
        ("NORMAL", "VALID", "YES", None,
         {"CUSTOMER_ID": "NUMBER"}, "partitioned state"),
        ("FUNCTION-BASED NORMAL", "VALID", "NO", 'SYS_EXTRACT_UTC("CUSTOMER_ID")',
         {"CUSTOMER_ID": "NUMBER"}, "does not match a TIMESTAMP WITH TIME ZONE"),
    ],
)
def test_oracle_index_catalog_rejects_genuine_or_invalid_index_cases(
    index_type, status, partitioned, expression, column_types, reason,
):
    definition = normalize_index_rows(
        [("IX_TEST", "NONUNIQUE", index_type, status, partitioned,
          "SYS_NC00001$" if expression else "CUSTOMER_ID", 1,
          "ASC", expression)],
        column_types,
    )["IX_TEST"]

    assert definition["supported"] is False
    assert reason in definition["reason"]


def test_oracle_source_discovers_fixture_timestamp_tz_index_as_column_keys():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [
            ("CUSTOMER_ID", "NUMBER", 22, 22, None, 10, 0, "N", None, "NO", "NO"),
            ("ORDER_DATE", "TIMESTAMP WITH TIME ZONE", 13, 13, None, None, None, "N", None, "NO", "NO"),
        ],
        [],
        [
            ("E2E_ORDER_CUSTOMER_IX", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
             "CUSTOMER_ID", 1, "ASC", None),
            ("E2E_ORDER_CUSTOMER_IX", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
             "SYS_NC00002$", 2, "ASC", 'SYS_EXTRACT_UTC("ORDER_DATE")'),
        ],
    ]
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = connection

    schema = source.get_schema("E2E_ORDER")

    assert schema.indexes[0].name == "E2E_ORDER_CUSTOMER_IX"
    assert schema.indexes[0].columns == ["CUSTOMER_ID", "ORDER_DATE"]
    assert schema.indexes[0].descending_columns == []


def test_oracle_target_recreates_intrinsic_tstz_index_using_original_column_ddl():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "E2E_ORDER",
        columns=[
            Column("CUSTOMER_ID", "NUMBER"),
            Column("ORDER_DATE", "TIMESTAMP WITH TIME ZONE"),
        ],
        indexes=[Index(
            "E2E_ORDER_CUSTOMER_IX", ["CUSTOMER_ID", "ORDER_DATE"], index_type="NORMAL"
        )],
    )

    assert target.apply_constraints(schema) == []
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'CREATE INDEX "E2E_ORDER_CUSTOMER_IX" ON "E2E_ORDER" ("CUSTOMER_ID", "ORDER_DATE")' in statements


def test_oracle_target_matches_native_tstz_function_index_to_column_definition():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [("IX_ORDER_DATE", "E2E_ORDER", "NONUNIQUE", "FUNCTION-BASED NORMAL",
          "VALID", "NO", None, "SYS_NC00001$", 1, "ASC",
          'SYS_EXTRACT_UTC("ORDER_DATE")')],
        [("ORDER_DATE", "TIMESTAMP WITH TIME ZONE")],
    ]
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "E2E_ORDER",
        columns=[Column("ORDER_DATE", "TIMESTAMP WITH TIME ZONE")],
        indexes=[Index("IX_ORDER_DATE", ["ORDER_DATE"], index_type="NORMAL")],
    )

    assert target.apply_constraints(schema) == []
    assert not any("CREATE INDEX" in call.args[0] for call in cursor.execute.call_args_list)
    assert not any("DROP INDEX" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_full_rejects_genuine_function_index_before_target_clear():
    class FunctionIndexSource(_OneTableOracleSource):
        def get_schema(self, object_name):
            connection, cursor = _connection_cursor()
            cursor.fetchall.side_effect = [
                [("ID", "NUMBER", 22, 22, None, 10, 0, "N", None, "NO", "NO")],
                [],
                [("IX_FUNCTION", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
                  "SYS_NC00001$", 1, "ASC", 'UPPER("ID")')],
            ]
            source = OracleSourceConnector({"username": "SOURCE_OWNER"})
            source._conn = connection
            return source.get_schema(object_name)

    target = _OneTableOracleTarget()
    result = MigrationOrchestrator(
        FunctionIndexSource(), target,
        {
            "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
            "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
            "migration": {"mode": "full", "batch_size": 20},
            "secrets": {"provider": "env"},
            "validation": {"mode": "count"},
        },
    ).run_full()

    assert result["status"] == "failed"
    assert target.cleared == []
    assert target.rows == []


def test_oracle_source_list_objects_applies_optional_include_table_allowlist():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [
        ("CUSTOMERS",), ("ORACLE_E2E_SAMPLE",), ("ORDERDETAILS",), ("ORDERS",)
    ]
    source = OracleSourceConnector(
        {"include_tables": ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]}
    )
    source._conn = connection

    assert source.list_objects() == ["CUSTOMERS", "ORDERDETAILS", "ORDERS"]
    assert "user_tables" in cursor.execute.call_args.args[0].lower()

    unfiltered_connection, unfiltered_cursor = _connection_cursor()
    unfiltered_cursor.fetchall.return_value = [
        ("CUSTOMERS",), ("ORACLE_E2E_SAMPLE",), ("ORDERDETAILS",), ("ORDERS",)
    ]
    unfiltered_source = OracleSourceConnector({})
    unfiltered_source._conn = unfiltered_connection
    assert unfiltered_source.list_objects() == [
        "CUSTOMERS", "ORACLE_E2E_SAMPLE", "ORDERDETAILS", "ORDERS"
    ]


@pytest.mark.parametrize("row", [
    ("T", "C_DISABLED", "DISABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"),
    ("T", "C_NOVALIDATE", "ENABLED", "NOT VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"),
    ("T", "C_DEF_IMMEDIATE", "ENABLED", "VALIDATED", "DEFERRABLE", "IMMEDIATE"),
    ("T", "C_DEF_DEFERRED", "ENABLED", "VALIDATED", "DEFERRABLE", "DEFERRED"),
])
def test_oracle_constraint_state_preflight_rejects_unpreserved_states(row):
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [row]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    with pytest.raises(ValueError, match=f"{row[1]} has unsupported state"):
        source.validate_constraint_states(["T"])


def test_oracle_constraint_state_preflight_accepts_enabled_validated_immediate_constraints():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [
        ("T", "PK_T", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"),
        ("OTHER_T", "C_DISABLED", "DISABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"),
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    source.validate_constraint_states(["T"])

    sql = cursor.execute.call_args.args[0].lower()
    assert "status" in sql and "validated" in sql and "deferrable" in sql


def test_oracle_target_constraint_state_preflight_rejects_disabled_constraint():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [
        ("T", "UQ_T", "DISABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE")
    ]
    target = OracleTargetConnector({"username": "APP_OWNER"})
    target._conn = connection

    with pytest.raises(ValueError, match="target constraint T.UQ_T has unsupported state"):
        target.validate_constraint_states(["T"])


def test_oracle_source_list_objects_fails_if_allowlisted_table_is_missing():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [("CUSTOMERS",), ("ORDERS",)]
    source = OracleSourceConnector(
        {"include_tables": ["CUSTOMERS", "ORDERDETAILS"]}
    )
    source._conn = connection

    with pytest.raises(ValueError, match="include_tables were not found: ORDERDETAILS"):
        source.list_objects()


def test_oracle_source_list_objects_accepts_empty_schema_without_filter():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    assert source.list_objects() == []
    assert "user_tables" in cursor.execute.call_args.args[0].lower()


def test_oracle_source_discovers_all_owned_tables_without_fixture_or_table_allowlists():
    from tests.e2e.acceptance import load_e2e_config
    from tests.e2e.oracle.config import fixture_names_and_expected_tables

    cfg = load_e2e_config("config/oracle_e2e_acceptance.yaml")
    _fixtures, fixture_expectations = fixture_names_and_expected_tables(cfg)
    discovered = sorted(["CUSTOMER_ARCHIVE", "NEWLY_ADDED_TABLE", "ORDERS"])
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [(name,) for name in discovered],
        [(name,) for name in discovered],
    ]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    first_run = source.list_objects()
    # A new owned table appears on the next catalog discovery without changing
    # YAML, the connector, or the E2E fixture expectation list.
    second_run = source.list_objects()

    assert first_run == discovered
    assert second_run == discovered
    assert "NEWLY_ADDED_TABLE" not in fixture_expectations
    query = cursor.execute.call_args.args[0].upper()
    assert "FROM USER_TABLES" in query
    assert "INCLUDE_TABLES" not in query


def test_oracle_source_discovery_handles_thousands_of_owned_tables_without_allowlist():
    discovered = sorted(f"APP_TABLE_{index:05d}" for index in range(12_000))
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [(name,) for name in discovered]
    source = OracleSourceConnector({"username": "APP_OWNER"})
    source._conn = connection

    names = source.list_objects()

    assert len(names) == 12_000
    assert names[0] == "APP_TABLE_00000"
    assert names[-1] == "APP_TABLE_11999"
    assert source._config.get("include_tables") is None


def test_oracle_source_rejects_multiple_schema_owners_instead_of_widening_scope():
    connection, _cursor = _connection_cursor()
    source = OracleSourceConnector(
        {"username": "APP_OWNER_A", "include_schemas": ["APP_OWNER_A", "APP_OWNER_B"]}
    )
    source._conn = connection

    with pytest.raises(ValueError, match="must contain only that owner"):
        source.list_objects()


def test_oracle_source_streams_batches_and_materializes_lobs():
    connection, cursor = _connection_cursor()
    cursor.description = [("ID",), ("BODY",)]
    cursor.fetchmany.side_effect = [[(1, MagicMock(read=MagicMock(return_value="payload")))], []]
    source = OracleSourceConnector({"fetch_batch_size": 10})
    source._conn = connection

    assert list(source.export_full("T")) == [{"ID": 1, "BODY": "payload"}]
    cursor.execute.assert_called_once_with('SELECT * FROM "T"')


def test_oracle_source_empty_table_export_and_target_empty_batch_are_successful():
    source_connection, source_cursor = _connection_cursor()
    source_cursor.description = [("ID",)]
    source_cursor.fetchmany.return_value = []
    source = OracleSourceConnector({})
    source._conn = source_connection
    assert list(source.export_full("T")) == []

    target_connection, _ = _connection_cursor()
    target = OracleTargetConnector({})
    target._conn = target_connection
    assert target.upsert_batch("T", iter(()), Schema("T")).success_count == 0
    target_connection.cursor.assert_not_called()


def test_oracle_target_creates_table_with_precision_defaults_and_quoted_names():
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    schema = Schema(
        name='"Order Items"',
        columns=[
            Column("ID", "NUMBER", precision=10, scale=0, nullable=False),
            Column('"Display Name"', "VARCHAR2", size=50, nullable=True),
            Column("STATUS", "VARCHAR2", size=12, default="'ready'", nullable=False),
        ],
        primary_key=["ID"],
        options={"oracle_char_semantics": {'"Display Name"': "BYTE"}},
    )

    assert target.create_object_if_missing(schema) == "created"
    ddl = cursor.execute.call_args_list[-1].args[0]
    assert '"Order Items"' in ddl
    assert '"ID" NUMBER(10,0) NOT NULL' in ddl
    assert '"Display Name" VARCHAR2(50 BYTE) NULL' in ddl
    assert '"STATUS" VARCHAR2(12) DEFAULT \'ready\' NOT NULL' in ddl
    assert 'PRIMARY KEY ("ID")' in ddl


@pytest.mark.parametrize("identity_kind", ["ALWAYS", "BY DEFAULT"])
def test_oracle_target_creates_identity_with_source_generation_mode(identity_kind):
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    schema = Schema("IDENTITY_T", columns=[
        Column("RECORD_ID", "NUMBER", nullable=False, is_identity=True,
               identity_kind=identity_kind, identity_seed=1, identity_increment=1),
    ])

    assert target.create_object_if_missing(schema) == "created"

    ddl = cursor.execute.call_args_list[-1].args[0]
    assert f'"RECORD_ID" NUMBER GENERATED {identity_kind} AS IDENTITY' in ddl


def test_oracle_primary_key_uses_generated_name_instead_of_source_name():
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    schema = Schema(
        "ORDERS", columns=[Column("ID", "NUMBER", nullable=False)],
        primary_key=["ID"], primary_key_name="PK_SHARED_SOURCE_NAME",
    )
    target.create_object_if_missing(schema)
    ddl = cursor.execute.call_args.args[0]
    expected = _oracle_constraint_name("ORDERS", "PK", "PK_SHARED_SOURCE_NAME")
    assert f'CONSTRAINT "{expected}" PRIMARY KEY' in ddl
    assert "PK_SHARED_SOURCE_NAME" not in ddl


def test_oracle_primary_key_name_conflict_fails_before_create_table():
    connection, cursor = _connection_cursor()
    target_name = _oracle_constraint_name("ORDERS", "PK", "PK_SHARED_SOURCE_NAME")
    cursor.fetchone.side_effect = [None, ("OTHER_TABLE", "P", None, None)]
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    schema = Schema(
        "ORDERS", columns=[Column("ID", "NUMBER", nullable=False)],
        primary_key=["ID"], primary_key_name="PK_SHARED_SOURCE_NAME",
    )
    with pytest.raises(RuntimeError, match="already used by a different constraint"):
        target.create_object_if_missing(schema)
    assert not any("CREATE TABLE" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_constraint_names_are_stable_distinct_and_oracle_safe():
    first = _oracle_constraint_name("ORDERS", "PK", "PK_SHARED")
    second = _oracle_constraint_name("CUSTOMERS", "PK", "PK_SHARED")
    assert first == _oracle_constraint_name("ORDERS", "PK", "PK_SHARED")
    assert first != second
    assert len(first) <= 30
    assert first.isascii() and first.replace("_", "").isalnum()


def test_oracle_existing_equivalent_constraint_is_verified():
    _, cursor = _connection_cursor()
    cursor.fetchone.return_value = ("T", "U", None, None)
    cursor.fetchall.return_value = [("A",), ("B",)]
    assert _oracle_constraint_matches(cursor, "DMS_UQ_X", "T", "U", ["A", "B"])


def test_oracle_apply_constraints_skips_verified_equivalent_constraint():
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = ("T", "U", None, None)
    cursor.fetchall.return_value = [("A",)]
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("T", unique_constraints=[UniqueConstraint("SOURCE_UQ", ["A"])])
    assert target.apply_constraints(schema) == []
    assert not any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_apply_constraints_propagates_dead_connection_after_safe_rollback():
    connection, cursor = _connection_cursor()
    cursor.execute.side_effect = RuntimeError("DPY-1001: not connected to database")
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("T", unique_constraints=[UniqueConstraint("UQ", ["A"])])

    with pytest.raises(RuntimeError, match="DPY-1001"):
        target.apply_constraints(schema)

    connection.rollback.assert_called_once()
    connection.close.assert_not_called()


def test_oracle_existing_source_named_constraint_is_recognized_as_equivalent():
    _, cursor = _connection_cursor()
    cursor.fetchone.side_effect = [None, ("T", "U", None, None)]
    cursor.fetchall.side_effect = [[("SOURCE_UQ",)], [("A",)]]
    assert _oracle_constraint_matches(cursor, "DMS_UQ_X", "T", "U", ["A"])


def test_oracle_constraint_name_conflict_is_not_silently_accepted():
    _, cursor = _connection_cursor()
    cursor.fetchone.return_value = ("OTHER_TABLE", "U", None, None)
    with pytest.raises(RuntimeError, match="different constraint"):
        _oracle_constraint_matches(cursor, "DMS_UQ_X", "T", "U", ["A"])


def test_oracle_equivalent_target_constraint_with_different_state_is_rejected():
    _, cursor = _connection_cursor()
    cursor.fetchone.return_value = (
        "T", "U", None, None, "ENABLED", "NOT VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"
    )

    with pytest.raises(RuntimeError, match="non-default state"):
        _oracle_constraint_matches(cursor, "DMS_UQ_X", "T", "U", ["A"])


def test_oracle_apply_unique_uses_target_generated_name_and_surfaces_conflict():
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = connection
    schema = Schema("T", unique_constraints=[UniqueConstraint("UQ_SHARED", ["A"])])
    assert target.apply_constraints(schema) == []
    ddl = [call.args[0] for call in cursor.execute.call_args_list if "ALTER TABLE" in call.args[0]][0]
    assert _oracle_constraint_name("T", "UQ", "UQ_SHARED") in ddl
    assert "UQ_SHARED" not in ddl

    cursor.reset_mock()
    cursor.fetchone.return_value = ("OTHER_TABLE", "U", None, None)
    errors = target.apply_constraints(schema)
    assert errors and "different constraint" in errors[0]
    assert not any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_target_loads_batches_and_reports_failed_rows():
    connection, cursor = _connection_cursor()
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("T", columns=[Column("ID", "NUMBER"), Column("V", "VARCHAR2")])

    result = target.upsert_batch("T", iter([{"ID": 1, "V": None}, {"ID": 2, "V": "x"}]), schema)
    assert result.success_count == 2
    cursor.executemany.assert_called_once_with(
        'INSERT INTO "T" ("ID", "V") VALUES (:1, :2)', [(1, None), (2, "x")]
    )
    connection.commit.assert_called_once()

    cursor.executemany.side_effect = RuntimeError("insert failed")
    failed = target.upsert_batch("T", iter([{"ID": 3, "V": None}]), schema)
    assert failed.failure_count == 1
    assert failed.success_count == 0
    connection.rollback.assert_called_once()


def test_oracle_full_load_temporarily_allows_always_identity_then_restores_and_advances():
    connection, cursor = _connection_cursor()
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("IDENTITY_T", columns=[
        Column("RECORD_ID", "NUMBER", nullable=False, is_identity=True,
               identity_kind="ALWAYS"),
        Column("VALUE", "VARCHAR2"),
    ])

    result = target.upsert_batch(
        "IDENTITY_T",
        iter([{"RECORD_ID": 10, "VALUE": "a"}, {"RECORD_ID": 11, "VALUE": "b"}]),
        schema,
    )

    assert result.success_count == 2
    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert statements == [
        'ALTER TABLE "IDENTITY_T" MODIFY ("RECORD_ID" GENERATED BY DEFAULT AS IDENTITY)',
        'ALTER TABLE "IDENTITY_T" MODIFY ("RECORD_ID" GENERATED ALWAYS AS IDENTITY (START WITH LIMIT VALUE))',
    ]
    cursor.executemany.assert_called_once_with(
        'INSERT INTO "IDENTITY_T" ("RECORD_ID", "VALUE") VALUES (:1, :2)',
        [(10, "a"), (11, "b")],
    )
    assert connection.commit.call_count == 2


def test_oracle_by_default_identity_load_does_not_change_generation_mode():
    connection, cursor = _connection_cursor()
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("IDENTITY_T", columns=[
        Column("RECORD_ID", "NUMBER", nullable=False, is_identity=True,
               identity_kind="BY DEFAULT"),
    ])

    result = target.upsert_batch("IDENTITY_T", iter([{"RECORD_ID": 7}]), schema)

    assert result.success_count == 1
    assert cursor.execute.call_count == 0
    cursor.executemany.assert_called_once()


def test_oracle_failed_always_identity_batch_still_restores_generation_mode():
    connection, cursor = _connection_cursor()
    cursor.executemany.side_effect = RuntimeError("batch insert failed")
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("IDENTITY_T", columns=[
        Column("RECORD_ID", "NUMBER", nullable=False, is_identity=True,
               identity_kind="ALWAYS"),
    ])

    result = target.upsert_batch("IDENTITY_T", iter([{"RECORD_ID": 10}]), schema)

    assert result.failure_count == 1
    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert "GENERATED BY DEFAULT AS IDENTITY" in statements[0]
    assert "GENERATED ALWAYS AS IDENTITY (START WITH LIMIT VALUE)" in statements[1]
    connection.rollback.assert_called_once()


def test_oracle_reconciliation_restores_source_identity_generation_mode():
    connection, cursor = _connection_cursor()
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema("IDENTITY_T", columns=[
        Column("RECORD_ID", "NUMBER", nullable=False, is_identity=True,
               identity_kind="BY DEFAULT"),
    ]))
    source = Schema("IDENTITY_T", columns=[
        Column("RECORD_ID", "NUMBER", nullable=False, is_identity=True,
               identity_kind="ALWAYS"),
    ])

    assert target.reconcile_oracle_table(source) == "reconciled"

    assert cursor.execute.call_args.args[0] == (
        'ALTER TABLE "IDENTITY_T" MODIFY ("RECORD_ID" GENERATED ALWAYS AS IDENTITY)'
    )


def test_oracle_full_sync_clears_child_before_parent():
    connection, cursor = _connection_cursor()
    target = OracleTargetConnector({})
    target._conn = connection
    parent = Schema("PARENT_T", schema_name="APP", columns=[Column("ID", "NUMBER")])
    child = Schema(
        "CHILD_T", schema_name="APP", columns=[Column("PID", "NUMBER")],
        foreign_keys=[ForeignKey("FK_CHILD", ["PID"], "PARENT_T", ["ID"], "APP")],
    )
    target._managed_schemas = {"PARENT_T": parent, "CHILD_T": child}

    assert target.clear_objects_for_full_sync(["PARENT_T", "CHILD_T"]) == ["CHILD_T", "PARENT_T"]
    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        'DELETE FROM "CHILD_T"', 'DELETE FROM "PARENT_T"'
    ]


def test_oracle_full_sync_never_clears_unmanaged_target_only_objects():
    connection, cursor = _connection_cursor()
    target = OracleTargetConnector({})
    target._conn = connection
    target._managed_schemas = {"MIGRATED": Schema("MIGRATED")}
    assert target.clear_objects_for_full_sync(["MIGRATED", "TARGET_ONLY"]) == ["MIGRATED"]
    assert [call.args[0] for call in cursor.execute.call_args_list] == ['DELETE FROM "MIGRATED"']


@pytest.mark.parametrize(
    ("source_type", "precision", "scale", "expected"),
    [
        ("NUMBER", 8, 0, "integer"),
        ("NUMBER", 12, 2, "numeric(12,2)"),
        ("VARCHAR2", None, None, "varchar(80)"),
        ("DATE", None, None, "timestamp"),
        ("BLOB", None, None, "bytea"),
    ],
)
def test_oracle_source_cross_engine_type_mappings(source_type, precision, scale, expected):
    column = Column("C", source_type, size=80, precision=precision, scale=scale)
    assert map_oracle_source_column(column, "postgresql", "T") == expected


def test_oracle_target_maps_common_postgresql_types_and_rejects_unknown_types():
    column = Column("C", "numeric(12,2)", precision=None, scale=None)
    assert _oracle_column_type("postgresql", column) == "NUMBER(12,2)"
    assert _oracle_column_type("postgresql", Column("C", "geometry")) is None
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    target = OracleTargetConnector({"source_engine": "postgresql"})
    target._conn = connection
    with pytest.raises(UnmappedTypeError):
        target.create_object_if_missing(Schema("T", columns=[Column("G", "geometry")]))
    assert cursor.execute.call_count == 1

    cursor.reset_mock()
    target.create_object_if_missing(
        Schema("T2", columns=[Column("AMOUNT", "numeric(12,2)")])
    )
    assert '"AMOUNT" NUMBER(12,2)' in cursor.execute.call_args.args[0]


def test_oracle_target_applies_only_in_owner_foreign_keys():
    connection, cursor = _connection_cursor()
    cursor.fetchone.return_value = None
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = connection
    target._managed_schemas = {"CHILD_T": Schema("CHILD_T"), "PARENT_T": Schema("PARENT_T")}
    child = Schema(
        "CHILD_T",
        foreign_keys=[ForeignKey("FK_CHILD", ["PID"], "PARENT_T", ["ID"], "TARGET_OWNER")],
    )

    assert target.apply_constraints(child) == []
    assert 'REFERENCES "PARENT_T" ("ID")' in cursor.execute.call_args.args[0]

    cursor.reset_mock()
    cursor.fetchone.return_value = None
    child.foreign_keys[0].ref_schema = "OTHER_OWNER"
    errors = target.apply_constraints(child)
    assert errors and "cross-owner" in errors[0]
    cursor.execute.assert_not_called()


def test_oracle_target_creates_secondary_index_and_skips_it_on_repeat():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({})
    target._conn = connection
    index = Index("IDX_CUSTOMERS_CITY", ["CITY"], index_type="NORMAL")
    schema = Schema("CUSTOMERS", indexes=[index])

    assert target.apply_constraints(schema) == []
    create_sql = 'CREATE INDEX "IDX_CUSTOMERS_CITY" ON "CUSTOMERS" ("CITY")'
    assert create_sql in [call.args[0] for call in cursor.execute.call_args_list]

    cursor.reset_mock()
    cursor.fetchall.return_value = [(
        "IDX_CUSTOMERS_CITY", "CUSTOMERS", "NONUNIQUE", "NORMAL", "VALID", "NO",
        None, "CITY", 1, "ASC",
    )]
    assert target.apply_constraints(schema) == []
    assert not any("CREATE INDEX" in call.args[0] for call in cursor.execute.call_args_list)
    assert not any("DROP INDEX" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_target_detects_equivalent_index_under_another_name():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [(
        "EXISTING_CITY_INDEX", "CUSTOMERS", "NONUNIQUE", "NORMAL", "VALID", "NO",
        None, "CITY", 1, "ASC",
    )]
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "CUSTOMERS", indexes=[Index("IDX_CUSTOMERS_CITY", ["CITY"], index_type="NORMAL")]
    )

    assert target.apply_constraints(schema) == []
    assert not any("CREATE INDEX" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_target_does_not_duplicate_constraint_backed_equivalent_index():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = [(
        "PK_CUSTOMERS", "CUSTOMERS", "UNIQUE", "NORMAL", "VALID", "NO",
        "P", "CUSTOMER_ID", 1, "ASC",
    )]
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "CUSTOMERS",
        indexes=[Index("IDX_CUSTOMER_ID", ["CUSTOMER_ID"], unique=True, index_type="NORMAL")],
    )

    assert target.apply_constraints(schema) == []
    assert not any("CREATE INDEX" in call.args[0] for call in cursor.execute.call_args_list)
    assert not any("DROP INDEX" in call.args[0] for call in cursor.execute.call_args_list)


def test_oracle_target_creates_unique_descending_secondary_index():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "CUSTOMERS",
        indexes=[Index(
            "IDX_CUSTOMERS_CITY_DESC", ["CITY"], unique=True,
            index_type="NORMAL", descending_columns=["CITY"],
        )],
    )

    assert target.apply_constraints(schema) == []
    assert 'CREATE UNIQUE INDEX "IDX_CUSTOMERS_CITY_DESC" ON "CUSTOMERS" ("CITY" DESC)' in [
        call.args[0] for call in cursor.execute.call_args_list
    ]


def test_oracle_target_replaces_different_source_managed_index_definition():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [
        [(
            "IDX_CUSTOMERS_CITY", "CUSTOMERS", "NONUNIQUE", "NORMAL", "VALID", "NO",
            None, "EMAIL", 1, "ASC",
        )],
        [("CUSTOMERS",)],
    ]
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "CUSTOMERS", indexes=[Index("IDX_CUSTOMERS_CITY", ["CITY"], index_type="NORMAL")]
    )

    assert target.apply_constraints(schema) == []
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'DROP INDEX "IDX_CUSTOMERS_CITY"' in statements
    assert 'CREATE INDEX "IDX_CUSTOMERS_CITY" ON "CUSTOMERS" ("CITY")' in statements


def test_oracle_target_reports_index_create_failure_instead_of_silently_succeeding():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [[], []]
    cursor.execute.side_effect = [None, None, RuntimeError("index DDL rejected")]
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "CUSTOMERS", indexes=[Index("IDX_CUSTOMERS_CITY", ["CITY"], index_type="NORMAL")]
    )

    errors = target.apply_constraints(schema)

    assert errors == ["index IDX_CUSTOMERS_CITY: index DDL rejected"]


def test_unsupported_oracle_index_discovery_failure_preserves_full_target_rows():
    source = _OneTableOracleSource()
    source.get_schema = lambda _name: (_ for _ in ()).throw(
        ValueError("unsupported Oracle BITMAP index")
    )
    target = _OneTableOracleTarget()
    target.rows = [{"ID": 99}]
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["phases"]["full_target_sync"]["status"] == "skipped"
    assert result["phases"]["data_load"]["status"] == "skipped"
    assert target.rows == [{"ID": 99}]
    assert target.cleared == []
    assert "unsupported Oracle BITMAP index" in result["phases"]["object_failures"][0]["error"]


def test_oracle_target_checks_existing_table_column_structure():
    source = Schema(
        "T", primary_key=["ID"], columns=[
            Column("ID", "NUMBER", nullable=False, precision=10, scale=0),
            Column("V", "VARCHAR2", nullable=True, size=20),
        ]
    )
    target = Schema(
        "T", primary_key=["ID"], columns=[
            Column("ID", "NUMBER", nullable=False, precision=10, scale=0),
            Column("V", "VARCHAR2", nullable=True, size=20),
        ]
    )
    assert _schemas_compatible(source, target, "oracle")
    target.columns[1].nullable = False
    assert not _schemas_compatible(source, target, "oracle")


def test_oracle_reconciliation_adds_source_column_to_managed_table():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema(
        "T", columns=[Column("ID", "NUMBER", nullable=False)]
    ))
    source = Schema(
        "T", columns=[
            Column("ID", "NUMBER", nullable=False),
            Column("NEW_VALUE", "VARCHAR2", size=30, nullable=True),
        ],
    )

    assert target.reconcile_oracle_table(source) == "reconciled"
    assert cursor.execute.call_args.args[0] == (
        'ALTER TABLE "T" ADD ("NEW_VALUE" VARCHAR2(30) NULL)'
    )
    assert target._managed_schemas["T"] is source
    connection.commit.assert_called_once_with()


def test_oracle_reconciliation_drops_only_extra_managed_column():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema(
        "T", columns=[
            Column("ID", "NUMBER", nullable=False),
            Column("MANUAL_NOTE", "VARCHAR2", size=40, nullable=True),
        ],
    ))
    source = Schema("T", columns=[Column("ID", "NUMBER", nullable=False)])

    assert target.reconcile_oracle_table(source) == "reconciled"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'ALTER TABLE "T" DROP COLUMN "MANUAL_NOTE"' in statements
    assert not any("DROP TABLE" in statement for statement in statements)
    assert all("TARGET_ONLY_TABLE" not in statement for statement in statements)


def test_oracle_reconciliation_restores_compatible_column_definition_and_is_idempotent():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(side_effect=[
        Schema("T", columns=[Column("NAME", "VARCHAR2", size=20, nullable=True)]),
        Schema("T", columns=[Column("NAME", "VARCHAR2", size=50, nullable=True)]),
    ])
    source = Schema("T", columns=[Column("NAME", "VARCHAR2", size=50, nullable=True)])

    assert target.reconcile_oracle_table(source) == "reconciled"
    assert target.reconcile_oracle_table(source) == "unchanged"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'ALTER TABLE "T" MODIFY ("NAME" VARCHAR2(50))' in statements
    assert sum("ALTER TABLE" in statement for statement in statements) == 1


@pytest.mark.parametrize(
    ("semantics", "length_function", "existing_length", "fits"),
    [
        ("BYTE", "LENGTHB", 50, True),
        ("BYTE", "LENGTHB", 51, False),
        ("CHAR", "LENGTH", 50, True),
        ("CHAR", "LENGTH", 51, False),
    ],
)
def test_oracle_varchar2_narrowing_checks_existing_values_with_source_semantics(
    semantics, length_function, existing_length, fits
):
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = (existing_length,)
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema(
        "CUSTOMERS",
        columns=[Column("CITY", "VARCHAR2", size=100, nullable=True)],
        options={"oracle_char_semantics": {"CITY": semantics}},
    ))
    source = Schema(
        "CUSTOMERS",
        columns=[Column("CITY", "VARCHAR2", size=50, nullable=True)],
        options={"oracle_char_semantics": {"CITY": semantics}},
    )

    if fits:
        assert target.reconcile_oracle_table(source) == "reconciled"
    else:
        with pytest.raises(RuntimeError, match="exceeding the limit"):
            target.reconcile_oracle_table(source)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    fit_query = f'SELECT MAX({length_function}("CITY")) FROM "CUSTOMERS"'
    assert fit_query in statements
    if fits:
        alter = 'ALTER TABLE "CUSTOMERS" MODIFY '
        modify = next(statement for statement in statements if statement.startswith(alter))
        assert modify == f'ALTER TABLE "CUSTOMERS" MODIFY ("CITY" VARCHAR2(50 {semantics}))'
        assert connection.commit.called
    else:
        assert not any("ALTER TABLE" in statement for statement in statements)
        connection.commit.assert_not_called()


def test_oracle_narrowing_rerun_and_phone_extra_column_reconciliation():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = (42,)
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(side_effect=[
        Schema(
            "CUSTOMERS",
            columns=[
                Column("CITY", "VARCHAR2", size=100, nullable=True),
                Column("EXTRA_TARGET", "VARCHAR2", size=30, nullable=True),
            ],
            options={"oracle_char_semantics": {"CITY": "BYTE", "EXTRA_TARGET": "BYTE"}},
        ),
        Schema(
            "CUSTOMERS",
            columns=[
                Column("CITY", "VARCHAR2", size=50, nullable=True),
                Column("PHONE", "VARCHAR2", size=20, nullable=True),
            ],
            options={"oracle_char_semantics": {"CITY": "BYTE"}},
        ),
    ])
    source = Schema(
        "CUSTOMERS",
        columns=[
            Column("CITY", "VARCHAR2", size=50, nullable=True),
            Column("PHONE", "VARCHAR2", size=20, nullable=True),
        ],
        options={"oracle_char_semantics": {"CITY": "BYTE"}},
    )

    assert target.reconcile_oracle_table(source) == "reconciled"
    assert target.reconcile_oracle_table(source) == "unchanged"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    ddl = [statement for statement in statements if statement.startswith("ALTER TABLE")]
    assert ddl == [
        'ALTER TABLE "CUSTOMERS" DROP COLUMN "EXTRA_TARGET"',
        'ALTER TABLE "CUSTOMERS" MODIFY ("CITY" VARCHAR2(50 BYTE))',
        'ALTER TABLE "CUSTOMERS" ADD ("PHONE" VARCHAR2(20) NULL)',
    ]
    assert 'SELECT MAX(LENGTHB("CITY")) FROM "CUSTOMERS"' in statements


def test_oracle_varchar2_narrowing_preserves_not_null_without_repeating_clause():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = (50,)
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema(
        "T",
        columns=[Column("NAME", "VARCHAR2", size=100, nullable=False)],
        options={"oracle_char_semantics": {"NAME": "BYTE"}},
    ))
    source = Schema(
        "T",
        columns=[Column("NAME", "VARCHAR2", size=50, nullable=False)],
        options={"oracle_char_semantics": {"NAME": "BYTE"}},
    )

    assert target.reconcile_oracle_table(source) == "reconciled"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'ALTER TABLE "T" MODIFY ("NAME" VARCHAR2(50 BYTE))' in statements
    assert not any(statement.endswith("NOT NULL)") for statement in statements)


def test_oracle_nullability_only_reconciliation_emits_only_changed_attribute():
    connection, cursor = _connection_cursor()
    cursor.fetchall.return_value = []
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema(
        "T", columns=[Column("NAME", "VARCHAR2", size=50, nullable=False)]
    ))
    source = Schema("T", columns=[Column("NAME", "VARCHAR2", size=50, nullable=True)])

    assert target.reconcile_oracle_table(source) == "reconciled"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'ALTER TABLE "T" MODIFY ("NAME" NULL)' in statements


def test_oracle_reconciliation_rejects_dependency_before_altering_or_clearing():
    connection, cursor = _connection_cursor()
    cursor.fetchall.side_effect = [[("UQ_T_NAME",)], [], [], []]
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.inspect_schema = MagicMock(return_value=Schema(
        "T", columns=[Column("NAME", "VARCHAR2", size=100, nullable=True)],
        options={"oracle_char_semantics": {"NAME": "BYTE"}},
    ))
    source = Schema(
        "T", columns=[Column("NAME", "VARCHAR2", size=50, nullable=True)],
        options={"oracle_char_semantics": {"NAME": "BYTE"}},
    )

    with pytest.raises(RuntimeError, match="constraints"):
        target.reconcile_oracle_table(source)
    assert not any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list)
    assert not any("MAX(LENGTH" in call.args[0] for call in cursor.execute.call_args_list)
    connection.commit.assert_not_called()


class _OneTableOracleSource(SourceConnector):
    def __init__(self):
        self._config = {"username": "SOURCE_OWNER"}
        self.closed = False

    def connect(self):
        pass

    def close(self):
        self.closed = True

    def list_objects(self):
        return ["T"]

    def get_object_count(self, object_name, schema_name=None):
        return 1

    def export_full(self, object_name, schema_name=None):
        yield {"ID": 1}

    def get_schema(self, object_name):
        return Schema(
            "T", schema_name="SOURCE_OWNER",
            columns=[Column("ID", "NUMBER", nullable=False)],
        )


class _OneTableOracleTarget(TargetConnector):
    def __init__(self):
        self._config = {"username": "TARGET_OWNER", "source_engine": "oracle"}
        self.rows = []
        self.cleared = []
        self.created_types = {}
        self.closed = False

    def connect(self):
        pass

    def close(self):
        self.closed = True

    def ensure_database_exists(self):
        pass

    def create_object_if_missing(self, schema):
        self.created_types[schema.name] = [column.target_type for column in schema.columns]
        return "created"

    def upsert_batch(self, object_name, rows, schema=None):
        batch = list(rows)
        self.rows.extend(batch)
        return UpsertResult(success_count=len(batch))

    def delete(self, object_name, document, schema=None):
        pass

    def get_object_count(self, object_name, schema_name=None):
        return len(self.rows)

    def export_full(self, object_name, schema_name=None):
        yield from self.rows

    def clear_objects_for_full_sync(self, objects):
        self.cleared = list(objects)
        self.rows.clear()
        return list(objects)

    def inspect_schema(self, object_name, schema_name=None):
        return Schema(
            "T", schema_name="TARGET_OWNER",
            columns=[Column("ID", "NUMBER", nullable=False)],
        )


def test_full_oracle_run_uses_shared_batch_load_and_structure_count_validation():
    source = _OneTableOracleSource()
    target = _OneTableOracleTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["status"] == "success"
    assert result["phases"]["T"]["success"] == 1
    assert result["phases"]["validation"]["checks"]["T"]["match"] is True
    assert result["phases"]["validation"]["checks"]["T"]["schema_structure"]["match"] is True
    assert target.cleared == ["T"]
    assert source.closed and target.closed


def test_oracle_full_replaces_only_managed_data_repeatedly_without_plan_approval():
    class ReplacementSource(_OneTableOracleSource):
        def __init__(self, rows):
            super().__init__()
            self.rows = list(rows)

        def get_object_count(self, object_name, schema_name=None):
            return len(self.rows)

        def export_full(self, object_name, schema_name=None):
            yield from self.rows

        def validate_schema_replacement_objects(self):
            pass

        def validate_schema_replacement_grants(self):
            pass

        def list_types(self):
            return []

        def list_comments(self):
            return []

        def list_views(self):
            return []

        def list_materialized_views(self):
            return []

        def list_all_sequences(self):
            return []

        def list_functions(self):
            return []

        def get_all_triggers(self):
            return []

        def list_synonyms(self):
            return []

    class ExistingTarget(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.table_exists = False
            self.target_only_rows = [{"ID": 700}]
            self.fail_next_load = False

        def validate_all_constraint_states(self):
            pass

        def inspect_object_privileges(self, **_definitions):
            return {"missing": [], "proposed_grants": []}

        def reconcile_oracle_table(self, _schema):
            self.table_exists = True
            return "reconciled"

        def set_managed_source_objects(self, _definitions):
            pass

        def apply_constraints(self, _schema):
            return []

        def verify_schema_replacement(self, **_definitions):
            return {"status": "success"}

        def plan_schema_replacement(self, **_inventory):
            plan = SimpleNamespace(
                plan_id="REVIEW-THIS-PLAN",
                inventory=(
                    ({"type": "TABLE", "name": "T"},)
                    if self.table_exists else ()
                ),
                cleanup=(),
                blockers=(),
                inspection_errors=(),
                as_dict=lambda: {
                    "status": "ready", "plan_id": "REVIEW-THIS-PLAN",
                    "cleanup": [], "preserved_target_only": [
                        {"type": "TABLE", "name": "TARGET_ONLY"}
                    ],
                },
            )
            return plan

        def apply_schema_replacement(self, *_args, **_kwargs):
            return []

        def create_object_if_missing(self, schema):
            self.table_exists = True
            return super().create_object_if_missing(schema)

        def upsert_batch(self, object_name, rows, schema=None):
            batch = list(rows)
            if self.fail_next_load:
                self.fail_next_load = False
                return UpsertResult(
                    failure_count=len(batch), errors=["injected batch failure"],
                    failed_items=batch,
                )
            return super().upsert_batch(object_name, batch, schema)

        def clear_objects_for_full_sync(self, objects):
            return super().clear_objects_for_full_sync(objects)

    target = ExistingTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {
            "mode": "full", "batch_size": 20,
            "oracle_schema_replacement": {"enabled": True},
        },
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    # First FULL creates and loads the managed table.
    first = MigrationOrchestrator(
        ReplacementSource([{"ID": 1}, {"ID": 2}]), target, config
    ).run_full()
    assert first["status"] == "success"
    assert target.rows == [{"ID": 1}, {"ID": 2}]
    assert first["phases"]["oracle_overwrite_authorization"]["status"] == "not_required"

    # Second and third FULL replace rows, including source changes/deletions,
    # without changing YAML or supplying a plan-specific approval id.
    second = MigrationOrchestrator(
        ReplacementSource([{"ID": 1}, {"ID": 3}]), target, config
    ).run_full()
    assert second["status"] == "success"
    assert target.rows == [{"ID": 1}, {"ID": 3}]
    assert second["phases"]["oracle_overwrite_authorization"]["status"] == "enabled"

    third = MigrationOrchestrator(
        ReplacementSource([{"ID": 3}]), target, config
    ).run_full()
    assert third["status"] == "success"
    assert target.rows == [{"ID": 3}]
    assert target.target_only_rows == [{"ID": 700}]
    assert third["phases"]["oracle_overwrite_authorization"]["status"] == "enabled"

    # A failed load can leave the managed table empty/partial because Oracle
    # commits the clear and batches separately. A subsequent FULL clears the
    # managed scope again and recovers without duplicating rows.
    target.fail_next_load = True
    failed = MigrationOrchestrator(
        ReplacementSource([{"ID": 4}, {"ID": 5}]), target, config
    ).run_full()
    assert failed["status"] != "success"
    assert target.rows == []

    recovered = MigrationOrchestrator(
        ReplacementSource([{"ID": 4}, {"ID": 5}]), target, config
    ).run_full()
    assert recovered["status"] == "success"
    assert target.rows == [{"ID": 4}, {"ID": 5}]
    assert target.target_only_rows == [{"ID": 700}]


def test_full_oracle_run_carries_supported_descending_index_through_preflight_and_validation():
    index = Index(
        "IX_T_ID_DESC", ["ID"], index_type="NORMAL", descending_columns=["ID"]
    )

    class IndexedSource(_OneTableOracleSource):
        def get_schema(self, object_name):
            return Schema(
                "T", schema_name="SOURCE_OWNER",
                columns=[Column("ID", "NUMBER", nullable=False)], indexes=[index],
            )

    class IndexedTarget(_OneTableOracleTarget):
        def create_object_if_missing(self, schema):
            self.created_types[schema.name] = schema
            return "created"

        def inspect_schema(self, object_name, schema_name=None):
            return Schema(
                "T", schema_name="TARGET_OWNER",
                columns=[Column("ID", "NUMBER", nullable=False)], indexes=[index],
            )

    target = IndexedTarget()
    result = MigrationOrchestrator(
        IndexedSource(), target,
        {
            "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
            "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
            "migration": {"mode": "full", "batch_size": 20},
            "secrets": {"provider": "env"},
            "validation": {"mode": "count"},
        },
    ).run_full()

    assert result["status"] == "success"
    assert target.rows == [{"ID": 1}]
    assert target.cleared == ["T"]
    assert result["phases"]["validation"]["checks"]["T"]["schema_structure"]["match"] is True


def test_oracle_full_revalidates_and_prepares_mview_before_clearing_rows_then_refreshes(monkeypatch):
    events = []
    mv = MaterializedViewDef(
        "MV_T", "SELECT * FROM SOURCE_OWNER.T", schema_name="SOURCE_OWNER"
    )

    class MViewSource(_OneTableOracleSource):
        def configure_permission_migration(self, _policy):
            events.append("configure-permissions")

        def validate_schema_replacement_objects(self):
            pass

        def validate_schema_replacement_grants(self):
            pass

        def discover_permission_inventory(self):
            return {"roles": [], "grants": [], "memberships": []}

        def list_materialized_views(self):
            return [mv]

        def list_all_sequences(self):
            return []

        def materialized_view_query_rows(self, _mv):
            return [(1,)]

    class MViewTarget(_OneTableOracleTarget):
        def plan_schema_replacement(self, **_inventory):
            return SimpleNamespace(
                blockers=(), inspection_errors=(), plan_id="PLAN",
                as_dict=lambda: {"status": "ready"},
            )

        def preflight_permission_migration(self, _policy, _permissions, _inventory, *, source_owner, **_kwargs):
            assert source_owner == "SOURCE_OWNER"
            events.append("permission-preflight")
            return {
                    "policy": {"user_mappings": {}, "role_mappings": {}}, "roles": [],
                "object_grants": [], "system_grants": [], "memberships": [],
                    "permission_reconciliation": {
                        "to_grant": [], "to_revoke": [],
                        "preserved_independent": [], "stale": [],
                    },
            }

        def revalidate_existing_materialized_views(self, _mviews):
            events.append("precleanup-revalidate")

        def apply_schema_replacement(self, _plan, **_inventory):
            events.append("replacement-cleanup")
            return []

        def validate_all_constraint_states(self):
            pass

        def set_managed_source_objects(self, _definitions):
            pass

        def validate_object_privileges(self, **_definitions):
            return {"missing": []}

        def reconcile_oracle_table(self, _schema):
            return "reconciled"

        def create_materialized_view(self, _mv):
            events.append("prepare-mview")

        def apply_permission_role_memberships(self, _plan):
            events.append("apply-role-memberships")
            return []

        def apply_permission_grants(self, _plan):
            events.append("apply-permission-grants")
            return []

        def verify_permission_migration(self, _plan):
            events.append("verify-permissions")

        def clear_objects_for_full_sync(self, objects):
            events.append("clear-rows")
            return super().clear_objects_for_full_sync(objects)

        def refresh_materialized_view(self, _name, schema_name=None):
            events.append("refresh-mview")

        def materialized_view_rows(self, _name):
            return [(1,)]

        def apply_constraints(self, _schema):
            return []

        def verify_schema_replacement(self, **_inventory):
            return {"status": "success"}

    source = MViewSource()
    target = MViewTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "oracle_permission_migration": {
            "enabled": True,
            "executor": {
                "username": "PERMISSION_EXECUTOR",
                "password_secret": "oracle_permission_executor_test",
            },
        },
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }
    monkeypatch.setenv("SECRET_oracle_permission_executor_test", "unit-test-secret")

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["status"] == "success"
    assert events.index("precleanup-revalidate") < events.index("replacement-cleanup")
    assert events.index("permission-preflight") < events.index("replacement-cleanup")
    assert events.index("prepare-mview") < events.index("clear-rows")
    assert events.index("clear-rows") < events.index("refresh-mview")
    assert events.index("refresh-mview") < events.index("apply-role-memberships")
    assert events.index("apply-role-memberships") < events.index("apply-permission-grants")
    assert events.index("apply-permission-grants") < events.index("verify-permissions")

    blocked_events = []

    class BlockedPermissionSource(MViewSource):
        def configure_permission_migration(self, _policy):
            pass

    class BlockedPermissionTarget(MViewTarget):
        def plan_schema_replacement(self, **_inventory):
            return SimpleNamespace(
                blockers=(), inspection_errors=(), plan_id="PLAN",
                as_dict=lambda: {"status": "ready"},
            )

        def revalidate_existing_materialized_views(self, _mviews):
            pass

        def preflight_permission_migration(self, *_args, **_kwargs):
            blocked_events.append("permission-preflight")
            raise RuntimeError("mapped target principal is missing")

        def apply_schema_replacement(self, *_args, **_kwargs):
            blocked_events.append("replacement-cleanup")

        def clear_objects_for_full_sync(self, objects):
            blocked_events.append("clear-rows")
            return objects

    blocked_result = MigrationOrchestrator(
        BlockedPermissionSource(), BlockedPermissionTarget(), config
    ).run_full()
    assert blocked_result["status"] == "failed"
    assert blocked_events == ["permission-preflight"]


def test_oracle_full_surfaces_replacement_verification_failure_without_policy_switch():
    events = []

    class StatusRecorder:
        def __init__(self):
            self.errors = []

        def update_status(self, _phase, _progress, errors=None):
            self.errors = list(errors or [])

    class CompleteSource(_OneTableOracleSource):
        def __init__(self):
            super().__init__()
            self._config["include_tables"] = ["T"]

        def validate_schema_replacement_objects(self):
            events.append("source-preflight")

        def validate_schema_replacement_grants(self):
            events.append("grant-preflight")

        def validate_constraint_states(self, _objects):
            events.append("source-constraint-preflight")

        def list_views(self):
            return []

        def list_all_sequences(self):
            return []

        def list_functions(self):
            return []

        def get_all_triggers(self):
            return []

    class ReplacementTarget(_OneTableOracleTarget):
        def plan_schema_replacement(self, *, preserve_target_only=False, **_inventory):
            assert preserve_target_only is True
            return SimpleNamespace(
                blockers=(), inspection_errors=(), plan_id="PLAN",
                as_dict=lambda: {
                    "status": "ready", "cleanup": [],
                    "preservation_policy": "source_managed",
                },
            )

        def apply_schema_replacement(self, _plan, *, preserve_target_only=False, **_inventory):
            assert preserve_target_only is True
            events.append("apply-source-managed-plan")
            return []

        def validate_all_constraint_states(self):
            events.append("target-constraint-preflight")

        def validate_object_privileges(self, **_inventory):
            events.append("target-object-preflight")

        def reconcile_oracle_table(self, schema):
            events.append(f"recreate-{schema.name}")
            return "created"

        def apply_constraints(self, _schema):
            return []

        def verify_schema_replacement(self, **_inventory):
            raise RuntimeError(
                "Oracle schema replacement verification failed: source objects are missing: "
                "CONSTRAINT DMS_CK_F7C9F3680E6B0D614657; password=hunter2"
            )

    source = CompleteSource()
    target = ReplacementTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    status = StatusRecorder()
    result = MigrationOrchestrator(source, target, config, status_server=status).run_full()

    assert result["phases"]["oracle_schema_replacement"] == []
    assert events.index("apply-source-managed-plan") < events.index("recreate-T")
    verification = result["phases"]["oracle_schema_replacement_verification"]
    assert verification["status"] == "failed"
    assert verification["phase"] == "oracle_schema_replacement_verification"
    assert verification["object"] == "TARGET_OWNER"
    assert "DMS_CK_F7C9F3680E6B0D614657" in verification["error"]
    assert "hunter2" not in verification["error"]
    assert result["status"] == "partial_success"
    assert result["errors"] and "oracle_schema_replacement_verification" in result["errors"][0]
    assert "DMS_CK_F7C9F3680E6B0D614657" in result["error"]
    failure = result["phases"]["object_failures"][0]
    assert failure["phase"] == "oracle_schema_replacement_verification"
    assert failure["object"] == "TARGET_OWNER"
    assert "hunter2" not in status.errors[-1]
    assert "DMS_CK_F7C9F3680E6B0D614657" in status.errors[-1]
    assert source._config["include_tables"] == ["T"]


def test_oracle_full_datatype_policy_allows_verified_native_types_and_blocks_unknowns():
    schema = Schema("MIXED", columns=[
        Column("TXT", "VARCHAR2"),
        Column("AMOUNT", "NUMBER"),
        Column("WHEN", "TIMESTAMP(6) WITH TIME ZONE"),
        Column("DOC", "JSON"),
        Column("XML_DOC", "XMLTYPE"),
        Column("BYTES", "RAW"),
        Column("FILE_REF", "BFILE"),
        Column("FUTURE_TYPE", "VECTOR"),
    ])

    assert oracle_full_unsupported_datatypes([schema]) == [
        ("MIXED", "FILE_REF", "BFILE"),
        ("MIXED", "FUTURE_TYPE", "VECTOR"),
    ]


def test_oracle_read_only_replacement_plan_blocks_bfile_compatibility():
    class CompleteSource(_OneTableOracleSource):
        def list_objects(self):
            return ["S31_UNSUPPORTED"]

        def get_schema(self, _name):
            return Schema("S31_UNSUPPORTED", columns=[
                Column("ID", "NUMBER"), Column("EXTERNAL_FILE", "BFILE"),
            ])

        def validate_schema_replacement_objects(self):
            pass

        def validate_constraint_states(self, _objects):
            pass

        def validate_schema_replacement_grants(self):
            pass

        def list_views(self):
            return []

        def list_all_sequences(self):
            return []

        def list_functions(self):
            return []

        def get_all_triggers(self):
            return []

    class ReadOnlyTarget(_OneTableOracleTarget):
        def plan_schema_replacement(self, *, preserve_target_only=False, **_inventory):
            assert preserve_target_only is True
            return SimpleNamespace(
                blockers=(), inspection_errors=(),
                as_dict=lambda: {
                    "status": "ready", "blockers": [],
                    "cleanup": [], "preserved_target_only": [],
                    "preservation_policy": "source_managed",
                },
            )

        def validate_all_constraint_states(self):
            pass

        def validate_object_privileges(self, **_inventory):
            pass

    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full"}, "secrets": {"provider": "env"},
    }
    report = MigrationOrchestrator(
        CompleteSource(), ReadOnlyTarget(), config
    ).plan_oracle_schema_replacement()

    assert report["status"] == "blocked"
    assert any(
        "Unsupported datatype BFILE in S31_UNSUPPORTED.EXTERNAL_FILE" in blocker
        for blocker in report["blockers"]
    )
    assert report["cleanup"] == []


def test_oracle_full_bfile_blocks_before_replacement_and_preserves_target_marker():
    events = []

    class CompleteSource(_OneTableOracleSource):
        def list_objects(self):
            return ["S31_UNSUPPORTED"]

        def get_schema(self, _name):
            return Schema("S31_UNSUPPORTED", columns=[
                Column("ID", "NUMBER"), Column("EXTERNAL_FILE", "BFILE"),
            ])

        def validate_schema_replacement_objects(self):
            events.append("source-object-preflight")

        def validate_constraint_states(self, _objects):
            pass

        def validate_schema_replacement_grants(self):
            pass

        def list_views(self):
            return []

        def list_all_sequences(self):
            return []

        def list_functions(self):
            return []

        def get_all_triggers(self):
            return []

    class ProtectedTarget(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.rows = [{"ID": 99, "MARKER": "MUST_REMAIN"}]

        def validate_constraint_states(self, _objects):
            pass

        def ensure_database_exists(self):
            pass

        def validate_all_constraint_states(self):
            pass

        def validate_object_privileges(self, **_inventory):
            pass

        def plan_schema_replacement(self, **_inventory):
            return SimpleNamespace(
                blockers=(), inspection_errors=(), plan_id="PLAN",
                as_dict=lambda: {"status": "ready", "cleanup": []},
            )

        def apply_schema_replacement(self, *_args, **_kwargs):
            events.append("destructive-cleanup")
            self.rows.clear()
            return []

    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"}, "validation": {"mode": "count"},
    }
    source, target = CompleteSource(), ProtectedTarget()

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["status"] == "failed"
    assert result["phases"]["oracle_source_datatype_preflight"]["status"] == "blocked"
    assert "Unsupported datatype BFILE in S31_UNSUPPORTED.EXTERNAL_FILE" in result["error"]
    assert "destructive-cleanup" not in events
    assert target.rows == [{"ID": 99, "MARKER": "MUST_REMAIN"}]
    assert not target.cleared


def test_oracle_constraint_state_preflight_fails_before_target_writes():
    class UnsupportedConstraintStateSource(_OneTableOracleSource):
        def validate_constraint_states(self, _table_names):
            raise ValueError(
                "Oracle constraint T.C_DEFERRED has unsupported state: ENABLED, "
                "VALIDATED, INITIALLY DEFERRED"
            )

    source = UnsupportedConstraintStateSource()
    target = _OneTableOracleTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["status"] == "failed"
    assert result["phases"]["constraint_state_preflight"] == "failed"
    assert "INITIALLY DEFERRED" in result["phases"]["object_failures"][0]["error"]
    assert target.created_types == {}
    assert target.cleared == []


def test_oracle_parent_schema_failure_skips_child_constraints_and_preserves_data():
    class ParentChildSource(_OneTableOracleSource):
        def list_objects(self):
            return ["PARENT", "CHILD"]

        def get_schema(self, object_name):
            foreign_keys = (
                [ForeignKey("FK_CHILD_PARENT", ["PARENT_ID"], "PARENT", ["ID"], "SOURCE_OWNER")]
                if object_name == "CHILD" else []
            )
            columns = (
                [Column("ID", "NUMBER", nullable=False), Column("PARENT_ID", "NUMBER")]
                if object_name == "CHILD" else [Column("ID", "NUMBER", nullable=False)]
            )
            return Schema(object_name, schema_name="SOURCE_OWNER", columns=columns, foreign_keys=foreign_keys)

    class ParentFailTarget(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.rows = [{"ID": 99}]
            self.applied_constraints = []

        def create_object_if_missing(self, schema):
            if schema.name == "PARENT":
                raise RuntimeError("simulated parent create failure")
            return "created"

        def apply_constraints(self, schema):
            self.applied_constraints.append(schema.name)
            return []

    source = ParentChildSource()
    target = ParentFailTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }
    result = MigrationOrchestrator(source, target, config).run_full()
    assert result["phases"]["full_target_sync"]["status"] == "skipped"
    assert result["phases"]["data_load"]["status"] == "skipped"
    assert result["phases"]["apply_constraints"]["CHILD"].startswith("skipped:")
    assert target.rows == [{"ID": 99}]
    assert target.cleared == []
    assert target.applied_constraints == []


def test_oracle_reconciliation_failure_skips_full_clear_and_row_load():
    class FailingReconciliationTarget(_OneTableOracleTarget):
        def __init__(self):
            super().__init__()
            self.rows = [{"ID": 99}]
            self.cleared = []

        def reconcile_oracle_table(self, _schema):
            raise RuntimeError("unsafe dependent constraint")

    source = _OneTableOracleSource()
    target = FailingReconciliationTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20, "reconcile_target_schema": True},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["phases"]["T"]["status"] == "creation_failed"
    assert result["phases"]["full_target_sync"]["status"] == "skipped"
    assert result["phases"]["data_load"]["status"] == "skipped"
    assert target.rows == [{"ID": 99}]
    assert target.cleared == []


def test_oracle_source_owner_mapping_keeps_internal_fk_load_order():
    class TwoTableSource(_OneTableOracleSource):
        def list_objects(self):
            return ["CHILD_T", "PARENT_T"]

        def get_object_count(self, object_name, schema_name=None):
            return 1

        def export_full(self, object_name, schema_name=None):
            yield {"ID": 1}

        def get_schema(self, object_name):
            foreign_keys = (
                [ForeignKey("FK_CHILD", ["PARENT_ID"], "PARENT_T", ["ID"], "SOURCE_OWNER")]
                if object_name == "CHILD_T" else []
            )
            columns = (
                [Column("ID", "NUMBER", nullable=False), Column("PARENT_ID", "NUMBER")]
                if object_name == "CHILD_T" else [Column("ID", "NUMBER", nullable=False)]
            )
            return Schema(
                object_name, schema_name="SOURCE_OWNER", columns=columns,
                foreign_keys=foreign_keys,
            )

    source = TwoTableSource()
    target = _OneTableOracleTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["phases"]["data_load_order"] == ["PARENT_T", "CHILD_T"]


def test_preloaded_acceptance_table_fk_graph_orders_customers_orders_orderdetails():
    schemas = {
        "ORDERDETAILS": Schema(
            "ORDERDETAILS", schema_name="SOURCE_OWNER",
            columns=[Column("ORDER_ID", "NUMBER")],
            foreign_keys=[ForeignKey(
                "FK_ORDERDETAILS_ORDERS", ["ORDER_ID"], "ORDERS", ["ORDER_ID"], "SOURCE_OWNER"
            )],
        ),
        "ORDERS": Schema(
            "ORDERS", schema_name="SOURCE_OWNER",
            columns=[Column("ORDER_ID", "NUMBER"), Column("CUSTOMER_ID", "NUMBER")],
            foreign_keys=[ForeignKey(
                "FK_ORDERS_CUSTOMERS", ["CUSTOMER_ID"], "CUSTOMERS", ["CUSTOMER_ID"], "SOURCE_OWNER"
            )],
        ),
        "CUSTOMERS": Schema(
            "CUSTOMERS", schema_name="SOURCE_OWNER", columns=[Column("CUSTOMER_ID", "NUMBER")]
        ),
    }

    assert order_data_load_objects(schemas, list(schemas)) == [
        "CUSTOMERS", "ORDERS", "ORDERDETAILS"
    ]


def test_oracle_source_cross_engine_mapping_is_applied_in_full_orchestration():
    source = _OneTableOracleSource()
    source.get_schema = lambda _name: Schema(
        "T", schema_name="SOURCE_OWNER",
        columns=[Column("AMOUNT", "NUMBER", precision=12, scale=2)],
    )
    target = _OneTableOracleTarget()
    config = {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "postgresql", "connection": {"database": "target_db"}},
        "migration": {"mode": "full", "batch_size": 20},
        "secrets": {"provider": "env"},
        "validation": {"mode": "count"},
    }

    result = MigrationOrchestrator(source, target, config).run_full()

    assert result["status"] == "success"
    assert target.created_types["T"] == ["numeric(12,2)"]


@pytest.mark.parametrize(
    ("source_type", "target_engine", "expected"),
    [
        ("NUMBER", "mysql", "INT"),
        ("VARCHAR2", "mssql", "NVARCHAR(40)"),
        ("BINARY_DOUBLE", "postgresql", "double precision"),
        ("CLOB", "mysql", "LONGTEXT"),
    ],
)
def test_oracle_source_cross_engine_types(source_type, target_engine, expected):
    column = Column("C", source_type, size=40, precision=8, scale=0)
    assert map_oracle_source_column(column, target_engine, "T") == expected


@pytest.mark.parametrize(
    ("source_engine", "target_engine", "source_type", "expected"),
    [
        ("oracle", "postgresql", "NUMBER", "numeric"),
        ("oracle", "mysql", "CLOB", "LONGTEXT"),
        ("oracle", "mssql", "TIMESTAMP WITH TIME ZONE", "DATETIMEOFFSET"),
        ("postgresql", "oracle", "bytea", "BLOB"),
    ],
)
def test_oracle_type_registry_entries(source_engine, target_engine, source_type, expected):
    registry = TypeMappingRegistry()
    assert "oracle" in registry.get_supported_engines()
    assert registry.map_type(source_engine, target_engine, source_type) == expected
