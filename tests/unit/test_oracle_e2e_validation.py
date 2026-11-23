"""Mocked Oracle E2E validation tests."""
from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import Mock

import pytest

from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle import run_validation as oracle_validation


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.statement = ""
        self.closed = False
        self.data_rows = []
        self.data_offset = 0
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return None

    def execute(self, statement: str, params=()):
        self.statement = statement
        self.params = params
        normalized = statement.upper()
        if normalized.startswith("SELECT ") and ' FROM "' in normalized and "COUNT(*)" not in normalized:
            qualified_name = normalized.rsplit(' FROM ', 1)[1]
            table_name = qualified_name.rsplit('"', 2)[1]
            self.data_rows = self.connection.rows.get(table_name, [])
            self.data_offset = 0

    def fetchall(self):
        statement = self.statement.lower()
        if "from user_tab_comments" in statement or "from user_col_comments" in statement:
            return []
        if "from user_types" in statement:
            return self.connection.type_rows
        if "from user_tab_identity_cols" in statement:
            return self.connection.schemas.get(str(self.params[0]).upper(), {}).get("identity_columns", [])
        if "from user_type_attrs" in statement:
            return self.connection.type_attributes
        if "from user_coll_types" in statement:
            if "select type_name, coll_type from" in statement:
                return [(row[0], row[1]) for row in self.connection.collection_rows]
            return self.connection.collection_rows
        if "from user_nested_tables" in statement or "where table_type is not null" in statement:
            return []
        if "where object_type = 'type'" in statement:
            return [
                (name, status) for name, kind, status in self.connection.owner_objects
                if kind == "TYPE"
            ]
        if "from user_tables" in statement or "from all_tables" in statement:
            return [(table,) for table in self.connection.tables]
        if "from user_part_tables" in statement:
            return []
        if "from user_objects" in statement or "from all_objects" in statement:
            return self.connection.owner_objects
        if "from user_triggers" in statement or "from all_triggers" in statement:
            return self.connection.triggers
        table = str(self.params[0]).upper() if self.params else ""
        schema = self.connection.schemas.get(table, {})
        if "from user_tab_columns" in statement:
            return schema.get("columns", [])
        if "where c.constraint_name = :1" in statement:
            return self.connection.references.get(str(self.params[0]), [])
        if "from user_constraints" in statement and "constraint_type = 'c'" in statement:
            return list(schema.get("check_conditions", {}).items())
        if "where c.table_name = :1" in statement:
            return [
                (*row[:6], "NOT DEFERRABLE", "IMMEDIATE", row[6], row[7])
                for row in schema.get("constraints", [])
            ]
        if "from user_indexes" in statement:
            return schema.get("indexes", [])
        return []

    def fetchone(self):
        if "DBMS_METADATA.GET_DDL" in self.statement.upper():
            return ("CREATE OBJECT DEFINITION",)
        statement = self.statement.upper()
        if "SELECT OBJECT_ID" in statement:
            return (self.connection.identity_object_id,)
        if "SELECT LAST_NUMBER" in statement:
            return (self.connection.identity_last_number,)
        if "SELECT MAX(" in statement:
            return (self.connection.identity_max_value,)
        if 'SELECT COUNT(*) FROM "' in self.statement:
            table_name = self.statement.rsplit('"', 2)[1].upper()
            return (self.connection.row_counts[table_name],)
        return (0,)

    def fetchmany(self, size: int):
        rows = self.data_rows[self.data_offset:self.data_offset + size]
        self.data_offset += len(rows)
        return rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(
        self, tables: list[str], row_counts: dict[str, int],
        schemas: dict | None = None, references: dict | None = None,
        rows: dict[str, list[tuple[Any, ...]]] | None = None,
        owner_objects: list[tuple[str, str, str]] | None = None,
        triggers: list[tuple[str, str, str, str, str]] | None = None,
    ):
        self.tables = tables
        self.row_counts = {name.upper(): count for name, count in row_counts.items()}
        self.schemas = schemas if schemas is not None else _default_schemas()
        self.references = references if references is not None else _default_references()
        self.rows = rows if rows is not None else {
            name.upper(): [(index,) for index in range(1, count + 1)]
            for name, count in row_counts.items()
        }
        self.owner_objects = owner_objects if owner_objects is not None else [
            (name, "TABLE", "VALID") for name in self.tables
        ]
        self.triggers = triggers if triggers is not None else []
        self.type_rows = []
        self.type_attributes = []
        self.collection_rows = []
        self.identity_object_id = 10
        self.identity_last_number = 100
        self.identity_max_value = 99
        self.cursors: list[FakeCursor] = []
        self.closed = False

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class StorageMetadataCursor:
    def __init__(self, connection):
        self.connection = connection
        self.statement = ""
        self.closed = False

    def execute(self, statement, _params=()):
        self.statement = statement
        self.connection.queries.append(statement)
        for fragment, error in self.connection.errors.items():
            if fragment in statement.lower():
                raise error

    def fetchall(self):
        statement = self.statement.lower()
        if "from user_object_tables" in statement:
            return self.connection.object_tables
        if "from user_nested_tables" in statement:
            return self.connection.nested_tables
        raise AssertionError(f"Unexpected metadata query: {self.statement}")

    def close(self):
        self.closed = True


class StorageMetadataConnection:
    def __init__(self, *, object_tables=(), nested_tables=(), errors=None):
        self.object_tables = list(object_tables)
        self.nested_tables = list(nested_tables)
        self.errors = errors or {}
        self.queries = []
        self.cursors = []

    def cursor(self):
        cursor = StorageMetadataCursor(self)
        self.cursors.append(cursor)
        return cursor


def _report_check(report, name: str) -> dict:
    return next(check for check in report["checks"] if check["name"] == name)


def test_unsafe_table_storage_passes_for_empty_owner_scoped_inventories():
    connection = StorageMetadataConnection()

    assert oracle_validation._unsafe_table_storage(connection) == []
    assert connection.queries == [
        "SELECT table_name FROM user_object_tables ORDER BY table_name",
        "SELECT table_name, parent_table_name FROM user_nested_tables ORDER BY table_name",
    ]
    assert all(cursor.closed for cursor in connection.cursors)


def test_unsafe_table_storage_reports_existing_object_tables_and_nested_storage():
    connection = StorageMetadataConnection(
        object_tables=[("OBJECT_TABLE",)],
        nested_tables=[("NESTED_STORAGE", "PARENT_TABLE")],
    )

    assert oracle_validation._unsafe_table_storage(connection) == [
        "OBJECT_TABLE: object table",
        "NESTED_STORAGE for PARENT_TABLE: nested-table storage",
    ]


@pytest.mark.parametrize(
    ("query_fragment", "error"),
    [
        ("from user_object_tables", RuntimeError("ORA-00904: invalid identifier")),
        ("from user_nested_tables", RuntimeError("ORA-00942: table or view does not exist")),
    ],
)
def test_unsafe_table_storage_fails_closed_when_catalog_query_is_invalid_or_inaccessible(
    query_fragment, error
):
    connection = StorageMetadataConnection(errors={query_fragment: error})

    with pytest.raises(RuntimeError, match="ORA-"):
        oracle_validation._unsafe_table_storage(connection)

    assert connection.cursors[0].closed


@pytest.mark.parametrize("side", ["source", "target"])
def test_validation_keeps_storage_metadata_failure_as_failure(
    monkeypatch, oracle_test_config, oracle_test_secrets, side
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection(tables, {table: 3 for table in tables})
    target = FakeConnection(tables, {table: 3 for table in tables})
    selected = source if side == "source" else target
    connections = iter((source, target))
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))
    read_storage = oracle_validation._unsafe_table_storage

    def fail_selected_storage(connection):
        if connection is selected:
            raise RuntimeError("ORA-00942: catalog view unavailable")
        return read_storage(connection)

    monkeypatch.setattr(oracle_validation, "_unsafe_table_storage", fail_selected_storage)

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    storage_check = _report_check(report, f"{side}_object_table_and_nested_storage")
    assert report["result"] == "FAIL"
    assert storage_check["status"] == "FAIL"
    assert storage_check["message"] == "Unable to verify object-table and nested-table storage metadata"


def _default_schemas():
    tables = ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]
    schemas = {}
    for table in tables:
        columns = [("ID", "NUMBER", 22, 22, None, 10, 0, "N")]
        constraints = [(f"PK_{table}", "P", "ENABLED", "VALIDATED", None, None, "ID", 1)]
        schemas[table] = {"columns": columns, "constraints": constraints}
    schemas["ORDERS"]["columns"].append(("CUSTOMER_ID", "NUMBER", 22, 22, None, 10, 0, "N"))
    schemas["ORDERS"]["constraints"].append(("FK_ORDERS_CUSTOMERS", "R", "ENABLED", "VALIDATED", "PK_CUSTOMERS", "NO ACTION", "CUSTOMER_ID", 1))
    schemas["ORDERDETAILS"]["columns"].append(("ORDER_ID", "NUMBER", 22, 22, None, 10, 0, "N"))
    schemas["ORDERDETAILS"]["constraints"].append(("FK_DETAILS_ORDERS", "R", "ENABLED", "VALIDATED", "PK_ORDERS", "NO ACTION", "ORDER_ID", 1))
    return schemas


def _default_references():
    return {
        "PK_CUSTOMERS": [("CUSTOMERS", "ID")],
        "PK_ORDERS": [("ORDERS", "ID")],
    }


def _preloaded_config(config):
    config["e2e"]["source_mode"] = "preloaded"
    config["source"]["connection"]["include_tables"] = [
        "CUSTOMERS", "ORDERS", "ORDERDETAILS"
    ]
    config["e2e"].pop("fixtures", None)
    config["e2e"]["expected"] = {
        "objects": ["CUSTOMERS", "ORDERS", "ORDERDETAILS"],
        "row_counts": {"CUSTOMERS": 3, "ORDERS": 3, "ORDERDETAILS": 3},
        "foreign_keys": [
            {"table": "ORDERS", "referenced_table": "CUSTOMERS"},
            {"table": "ORDERDETAILS", "referenced_table": "ORDERS"},
        ],
    }
    return config


def test_validation_compares_configured_source_target_tables_and_row_counts(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection(
        [*tables, "ORACLE_E2E_SAMPLE"], {table: 3 for table in tables}
    )
    target = FakeConnection(tables, {table: 3 for table in tables})
    configs: list[dict] = []

    def connect(connection_config, _driver):
        configs.append(connection_config)
        return source if connection_config["username"] == config["source"]["connection"]["username"] else target

    monkeypatch.setattr(oracle_validation, "connect", connect)
    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())
    assert report["result"] == "PASS"
    assert report["source_owner"] == config["e2e"]["resources"]["source"]["name"]
    assert report["target_owner"] == config["e2e"]["resources"]["target"]["name"]
    assert report["source_service_name"] == config["source"]["connection"]["service_name"]
    assert report["target_service_name"] == config["target"]["connection"]["service_name"]
    assert _report_check(report, "source_tables")["actual"] == sorted(tables)
    assert _report_check(report, "source_tables")["excluded"] == ["ORACLE_E2E_SAMPLE"]
    assert _report_check(report, "target_tables")["actual"] == sorted(tables)
    assert _report_check(report, "row_count:CUSTOMERS") == {
        "name": "row_count:CUSTOMERS",
        "status": "PASS",
        "expected_source_rows": 3,
        "source": 3,
        "target": 3,
    }
    assert [item["password"] for item in configs] == [
        oracle_test_secrets["source"], oracle_test_secrets["target"]
    ]
    assert source.closed and target.closed
    for secret in oracle_test_secrets.values():
        assert secret not in repr(report)


def test_validation_cli_preserves_historical_report_and_writes_current_config_values(
    tmp_path, monkeypatch, capsys, oracle_test_config
):
    config = _preloaded_config(oracle_test_config)
    cfg = E2EConfig(config)
    report = {
        "source_owner": config["e2e"]["resources"]["source"]["name"],
        "target_owner": config["e2e"]["resources"]["target"]["name"],
        "source_service_name": config["source"]["connection"]["service_name"],
        "target_service_name": config["target"]["connection"]["service_name"],
        "result": "PASS",
        "checks": [{"name": "report_config", "status": "PASS"}],
    }
    monkeypatch.setattr(oracle_validation, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setattr(oracle_validation, "run_validation", lambda _cfg: report)
    monkeypatch.setattr(oracle_validation, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_validation.py", "--config", "configured.yaml"])
    historical_path = tmp_path / "validation_report.json"
    historical_content = json.dumps(
        {"source_owner": "HISTORICAL_SOURCE", "service_name": "HISTORICAL_SERVICE"}
    )
    historical_path.write_text(historical_content, encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        oracle_validation.main()

    assert exit_info.value.code == 0
    assert historical_path.read_text(encoding="utf-8") == historical_content
    generated = list(tmp_path.glob("validation_report_*.json"))
    assert len(generated) == 1
    new_report = json.loads(generated[0].read_text(encoding="utf-8"))
    assert new_report == report
    assert f"report: {generated[0]}" in capsys.readouterr().out


def test_validation_extracts_primary_key_column_name_from_constraint_row(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source_schemas = copy.deepcopy(_default_schemas())
    target_schemas = copy.deepcopy(_default_schemas())
    source_schemas["CUSTOMERS"]["constraints"][0] = (
        "PK_CUSTOMERS", "P", "ENABLED", "VALIDATED", None, None, "CUSTOMER_ID", 1
    )
    target_schemas["CUSTOMERS"]["constraints"][0] = (
        "PK_CUSTOMERS", "P", "ENABLED", "VALIDATED", None, None, "CUSTOMER_ID", 1
    )
    source = FakeConnection(
        tables, {table: 3 for table in tables}, schemas=source_schemas
    )
    target = FakeConnection(
        tables, {table: 3 for table in tables}, schemas=target_schemas
    )
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "structure:CUSTOMERS")
    assert check["source"]["primary_key"] == ["CUSTOMER_ID"]
    assert check["target"]["primary_key"] == ["CUSTOMER_ID"]


def test_validation_fails_for_missing_or_unexpected_tables(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection([], {})
    target = FakeConnection([*tables, "EXTRA"], {})
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    assert report["result"] == "FAIL"
    assert _report_check(report, "source_tables")["status"] == "FAIL"
    assert _report_check(report, "target_tables")["actual"] == sorted([*tables, "EXTRA"])
    assert _report_check(report, "row_count:CUSTOMERS")["status"] == "FAIL"
    assert source.closed and target.closed


def test_validation_uses_live_source_count_instead_of_configured_row_counts(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection(tables, {table: 3 for table in tables})
    target = FakeConnection(tables, {**{table: 3 for table in tables}, "CUSTOMERS": 4})
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    row_check = _report_check(report, "row_count:CUSTOMERS")
    assert report["result"] == "FAIL"
    assert row_check["expected_source_rows"] == 3
    assert row_check["source"] == 3
    assert row_check["target"] == 4


def test_validation_does_not_require_configured_row_counts(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    del config["e2e"]["expected"]["row_counts"]
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection(tables, {table: 6 for table in tables})
    target = FakeConnection(tables, {table: 6 for table in tables})
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())
    assert report["result"] == "PASS"
    assert _report_check(report, "row_count:CUSTOMERS")["expected_source_rows"] == 6


def test_validation_discovers_schema_wide_tables_and_preserves_target_only_tables(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    config["e2e"]["expected"].pop("objects")
    config["e2e"]["expected"].pop("row_counts")
    config["e2e"]["expected"].pop("foreign_keys")
    config["source"]["connection"].pop("include_tables")
    tables = ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]
    schema_source = copy.deepcopy(_default_schemas())
    schema_target = copy.deepcopy(_default_schemas())
    schema_source["CUSTOMERS"]["indexes"] = [
        ("IDX_CUSTOMER_ID", "NONUNIQUE", "NORMAL", "VALID", "NO", "ID", 1, "ASC", None)
    ]
    schema_target["CUSTOMERS"]["indexes"] = [
        ("IDX_TARGET_CUSTOMER_ID", "NONUNIQUE", "NORMAL", "VALID", "NO", "ID", 1, "ASC", None)
    ]
    schema_source["MV_CUSTOMER"] = {
        "columns": [("ID", "NUMBER", 22, 22, None, 10, 0, "N")],
    }
    schema_target["MV_CUSTOMER"] = copy.deepcopy(schema_source["MV_CUSTOMER"])
    source_owner_objects = [
        *((table, "TABLE", "VALID") for table in tables),
        ("V_CUSTOMER", "VIEW", "VALID"),
        ("MV_CUSTOMER", "MATERIALIZED VIEW", "VALID"),
        ("CUSTOMER_SEQ", "SEQUENCE", "VALID"),
        ("CUSTOMER_PKG", "PACKAGE", "VALID"),
        ("CUSTOMER_PKG", "PACKAGE BODY", "VALID"),
        ("CUSTOMER_TRG", "TRIGGER", "VALID"),
        ("CUSTOMER_SYNONYM", "SYNONYM", "VALID"),
        ("IDX_CUSTOMER_ID", "INDEX", "VALID"),
    ]
    target_owner_objects = [
        *((table, "TABLE", "VALID") for table in [*tables, "TARGET_ONLY"]),
        *source_owner_objects[len(tables):],
        ("TARGET_ONLY_VIEW", "VIEW", "VALID"),
    ]
    source = FakeConnection(
        tables,
        {table: 4 for table in tables},
        schemas=schema_source,
        owner_objects=source_owner_objects,
        triggers=[("CUSTOMER_TRG", "ENABLED", "BEFORE EACH ROW", "INSERT", "CUSTOMERS")],
    )
    target = FakeConnection(
        [*tables, "TARGET_ONLY"],
        {table: 4 for table in tables},
        schemas=schema_target,
        owner_objects=target_owner_objects,
        triggers=[("CUSTOMER_TRG", "ENABLED", "BEFORE EACH ROW", "INSERT", "CUSTOMERS")],
    )
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    assert report["result"] == "PASS"
    assert _report_check(report, "source_tables")["actual"] == sorted(tables)
    assert _report_check(report, "target_tables")["status"] == "PASS"
    assert _report_check(report, "row_count:CUSTOMERS")["expected_source_rows"] == 4
    assert _report_check(report, "indexes:CUSTOMERS")["status"] == "PASS"
    assert _report_check(report, "supported_object_presence")["status"] == "PASS"
    assert {item["name"] for item in _report_check(report, "supported_object_presence")["target_only"]} == {"TARGET_ONLY_VIEW"}
    assert _report_check(report, "trigger_inventory")["status"] == "PASS"


def test_validation_exact_parity_is_null_safe_and_finds_changed_and_extra_rows(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    schema = copy.deepcopy(_default_schemas())
    schema["CUSTOMERS"]["columns"].append(
        ("VALUE", "VARCHAR2", 20, 20, "B", None, None, "Y")
    )
    source = FakeConnection(
        tables,
        {table: 2 for table in tables},
        schemas=copy.deepcopy(schema),
        rows={"CUSTOMERS": [(1, None), (2, "same")], "ORDERS": [(1,), (2,)], "ORDERDETAILS": [(1,), (2,)]},
    )
    target = FakeConnection(
        tables,
        {table: 2 for table in tables},
        schemas=copy.deepcopy(schema),
        rows={"CUSTOMERS": [(1, None), (2, "changed"), (3, "extra")], "ORDERS": [(1,), (2,)], "ORDERDETAILS": [(1,), (2,)]},
    )
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "content_parity:CUSTOMERS")
    assert check["status"] == "FAIL"
    assert check["missing_source_rows"] == 1
    assert check["extra_target_rows"] == 2


def test_validation_reports_unsupported_source_objects_as_failure(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection(
        tables,
        {table: 3 for table in tables},
        owner_objects=[*((table, "TABLE", "VALID") for table in tables), ("SOME_TYPE", "TYPE BODY", "VALID")],
    )
    target = FakeConnection(tables, {table: 3 for table in tables})
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "unsupported_source_objects")
    assert check["status"] == "FAIL"
    assert check["objects"] == [{"name": "SOME_TYPE", "type": "TYPE BODY", "status": "VALID"}]
    assert report["result"] == "FAIL"


def test_validation_does_not_claim_pass_when_permission_graph_is_enabled_but_unverified(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    config["oracle_permission_migration"] = {"enabled": True}
    tables = config["e2e"]["expected"]["objects"]
    connections = iter([
        FakeConnection(tables, {table: 1 for table in tables}),
        FakeConnection(tables, {table: 1 for table in tables}),
    ])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "permissions_and_grants")
    assert check["status"] == "FAIL"
    assert "Permission graph could not be completely verified" in check["message"]
    assert report["result"] == "FAIL"


def test_validation_compares_permission_graph_through_target_executor_read_only(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    from core.connectors.base import GrantDef, RoleDef, RoleMembershipDef, UserDef
    from core.connectors.oracle import permissions

    config = _preloaded_config(oracle_test_config)
    config["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "CATALOG_READER", "password_secret": "executor_secret"},
    }
    tables = config["e2e"]["expected"]["objects"]
    source, target, executor = [FakeConnection(tables, {table: 1 for table in tables}) for _ in range(3)]
    connections = iter([source, target, executor])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))
    monkeypatch.setattr(oracle_validation, "_permission_executor_connection_config", lambda *_: {"username": "CATALOG_READER"})
    identities = {
        id(target): {"user": "TARGET_OWNER", "db_unique_name": "DB", "container_name": "PDB", "service_name": "TARGET"},
        id(executor): {"user": "CATALOG_READER", "db_unique_name": "DB", "container_name": "PDB", "service_name": "TARGET"},
    }
    monkeypatch.setattr(permissions, "_identity", lambda conn: identities[id(conn)])
    graph = {
        "users": [UserDef("APP_USER")], "roles": [RoleDef("READ_ROLE")],
        "memberships": [RoleMembershipDef("APP_USER", "READ_ROLE")],
        "grants": [GrantDef("SELECT", "TABLE", "DATA_T", "READ_ROLE", "OWNER")],
    }
    scopes = []

    def discover(conn, _owner, _policy, *, catalog_scope="owner"):
        scopes.append(catalog_scope)
        return graph

    monkeypatch.setattr(permissions, "discover", discover)
    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())
    check = _report_check(report, "permissions_and_grants")
    assert check["status"] == "PASS"
    assert check["operation"] == "read_only"
    assert scopes == ["owner", "dba"]
    assert executor.closed


def test_permission_validation_fails_closed_on_catalog_visibility_gap(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    from core.connectors.oracle import permissions

    config = _preloaded_config(oracle_test_config)
    config["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "CATALOG_READER", "password_secret": "executor_secret"},
    }
    tables = config["e2e"]["expected"]["objects"]
    source, target, executor = [FakeConnection(tables, {table: 1 for table in tables}) for _ in range(3)]
    connections = iter([source, target, executor])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))
    monkeypatch.setattr(oracle_validation, "_permission_executor_connection_config", lambda *_: {"username": "CATALOG_READER"})
    identity = {"user": "OWNER", "db_unique_name": "DB", "container_name": "PDB", "service_name": "TARGET"}
    monkeypatch.setattr(permissions, "_identity", lambda _conn: identity)
    monkeypatch.setattr(permissions, "discover", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("catalog denied")))
    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())
    check = _report_check(report, "permissions_and_grants")
    assert check["status"] == "FAIL"
    assert "RuntimeError" in check["message"]
    assert report["result"] == "FAIL"


def test_type_value_canonicalization_preserves_nulls_and_nested_table_multisets():
    nested_a = {
        "__oracle_collection__": "ITEM_NT", "kind": "nested_table",
        "items": [None, Decimal("1.20"), Decimal("2.30")],
    }
    nested_b = {
        "__oracle_collection__": "item_nt", "kind": "nested_table",
        "items": [Decimal("2.30"), None, Decimal("1.20")],
    }
    assert oracle_validation._canonical_value(nested_a) == oracle_validation._canonical_value(nested_b)
    assert oracle_validation._canonical_value(None) != oracle_validation._canonical_value({
        "__oracle_collection__": "ITEM_NT", "kind": "nested_table", "items": [],
    })
    assert oracle_validation._canonical_value({
        "__oracle_object__": "VALUE_T", "attributes": {"AMOUNT": None},
    }) != oracle_validation._canonical_value(None)
    varray_a = {"__oracle_collection__": "ITEM_VA", "kind": "varray", "items": [1, 2]}
    varray_b = {"__oracle_collection__": "ITEM_VA", "kind": "varray", "items": [2, 1]}
    assert oracle_validation._canonical_value(varray_a) != oracle_validation._canonical_value(varray_b)


def test_type_definition_metadata_normalizes_owner_mapping_and_rejects_methods():
    type_inventory = [("VALUE_T", "TYPE", "VALID"), ("VALUE_ARRAY_T", "TYPE", "VALID")]
    source = FakeConnection([], {}, owner_objects=type_inventory)
    target = FakeConnection([], {}, owner_objects=type_inventory)
    source.type_rows = [("VALUE_T", "OBJECT", 1, 0, "NO", "NO", None)]
    target.type_rows = [("VALUE_T", "OBJECT", 1, 0, "NO", "NO", None)]
    source.type_rows.append(("VALUE_ARRAY_T", "COLLECTION", 0, 0, "NO", "NO", None))
    target.type_rows.append(("VALUE_ARRAY_T", "COLLECTION", 0, 0, "NO", "NO", None))
    source.type_attributes = [("VALUE_T", 1, "AMOUNT", None, "SYS", "NUMBER", 22, 20, 5, None)]
    target.type_attributes = list(source.type_attributes)
    source.collection_rows = [("VALUE_ARRAY_T", "VARYING ARRAY", 10, "SOURCE_OWNER", "VALUE_T", None)]
    target.collection_rows = [("VALUE_ARRAY_T", "VARYING ARRAY", 10, "TARGET_OWNER", "VALUE_T", None)]
    left, left_errors = oracle_validation._read_type_definitions(source, "SOURCE_OWNER")
    right, right_errors = oracle_validation._read_type_definitions(target, "TARGET_OWNER")
    assert left == right
    assert left_errors == right_errors == []
    assert oracle_validation._collection_kinds(source) == {"value_array_t": "varray"}

    target.type_rows = [("VALUE_T", "OBJECT", 1, 1, "NO", "NO", None)]
    _definition, errors = oracle_validation._read_type_definitions(target, "TARGET_OWNER")
    assert errors


def test_identity_sequence_state_must_be_above_table_maximum():
    connection = FakeConnection([], {})
    safe = oracle_validation._identity_sequence_state(connection, "ORDERS", {"ID": {}})
    assert safe["ID"]["safe_next_value"] is True
    connection.identity_last_number = 99
    unsafe = oracle_validation._identity_sequence_state(connection, "ORDERS", {"ID": {}})
    assert unsafe["ID"]["safe_next_value"] is False


def test_validation_detects_secondary_index_definition_mismatch(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source_schemas = copy.deepcopy(_default_schemas())
    target_schemas = copy.deepcopy(_default_schemas())
    source_schemas["CUSTOMERS"]["indexes"] = [
        ("SOURCE_INDEX", "NONUNIQUE", "NORMAL", "VALID", "NO", "ID", 1, "ASC", None)
    ]
    target_schemas["CUSTOMERS"]["indexes"] = [
        ("TARGET_INDEX", "UNIQUE", "NORMAL", "VALID", "NO", "ID", 1, "ASC", None)
    ]
    source = FakeConnection(tables, {table: 3 for table in tables}, schemas=source_schemas)
    target = FakeConnection(tables, {table: 3 for table in tables}, schemas=target_schemas)
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "indexes:CUSTOMERS")
    assert check["status"] == "FAIL"
    assert report["result"] == "FAIL"


def test_oracle_validation_normalizes_timestamp_tz_and_descending_index_signatures():
    columns = [
        ("CUSTOMER_ID", "NUMBER", 22, 22, None, 10, 0, "N", None),
        ("ORDER_DATE", "TIMESTAMP WITH TIME ZONE", 13, 13, None, None, None, "N", None),
    ]
    index_rows = [
        ("IX_ORDER", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
         "CUSTOMER_ID", 1, "ASC", None),
        ("IX_ORDER", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
         "SYS_NC00002$", 2, "ASC", 'SYS_EXTRACT_UTC("ORDER_DATE")'),
        ("IX_DESC", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
         "SYS_NC00003$", 1, "DESC", 'SYS_OP_DESCEND("CUSTOMER_ID")'),
    ]
    connection = FakeConnection(
        ["T"], {"T": 0},
        schemas={"T": {"columns": columns, "indexes": index_rows}},
    )

    structure = oracle_validation._read_table_structure(connection, "T")

    assert structure["unsupported_indexes"] == []
    signatures = {
        index["name"]: [
            (column["name"], column["descending"]) for column in index["columns"]
        ]
        for index in structure["indexes"]
    }
    assert signatures == {
        "IX_ORDER": [("CUSTOMER_ID", False), ("ORDER_DATE", False)],
        "IX_DESC": [("CUSTOMER_ID", True)],
    }


def test_oracle_validation_still_reports_genuine_expression_index_unsupported():
    connection = FakeConnection(
        ["T"], {"T": 0},
        schemas={"T": {
            "columns": [("CUSTOMER_ID", "NUMBER", 22, 22, None, 10, 0, "N", None)],
            "indexes": [(
                "IX_FUNCTION", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
                "SYS_NC00001$", 1, "ASC", 'UPPER("CUSTOMER_ID")',
            )],
        }},
    )

    structure = oracle_validation._read_table_structure(connection, "T")

    assert structure["indexes"] == []
    assert structure["unsupported_indexes"][0]["name"] == "IX_FUNCTION"
    assert "unsupported key expression" in structure["unsupported_indexes"][0]["reason"]


def test_validation_fails_when_ordered_column_structure_differs(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source = FakeConnection(tables, {table: 3 for table in tables})
    target_schemas = copy.deepcopy(_default_schemas())
    target_schemas["CUSTOMERS"]["columns"][0] = (
        "ID", "NUMBER", 22, 22, None, 12, 0, "N"
    )
    target = FakeConnection(tables, {table: 3 for table in tables}, schemas=target_schemas)
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    assert report["result"] == "FAIL"
    assert _report_check(report, "structure:CUSTOMERS")["status"] == "FAIL"


@pytest.mark.parametrize("side", ["source", "target"])
def test_validation_fails_when_expected_foreign_key_is_missing_or_disabled(
    monkeypatch, oracle_test_config, oracle_test_secrets, side
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source_schema = copy.deepcopy(_default_schemas())
    target_schema = copy.deepcopy(_default_schemas())
    selected = source_schema if side == "source" else target_schema
    selected["ORDERS"]["constraints"][-1] = (
        "FK_ORDERS_CUSTOMERS", "R", "DISABLED", "NOT VALIDATED", "PK_CUSTOMERS",
        "NO ACTION", "CUSTOMER_ID", 1
    )
    source = FakeConnection(tables, {table: 3 for table in tables}, schemas=source_schema)
    target = FakeConnection(tables, {table: 3 for table in tables}, schemas=target_schema)
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "foreign_keys:ORDERS")
    assert report["result"] == "FAIL"
    assert check["status"] == "FAIL"
    assert check["all_enabled_and_validated"] is False


def test_validation_fails_when_configured_foreign_key_is_missing_on_both_sides(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = _preloaded_config(oracle_test_config)
    tables = config["e2e"]["expected"]["objects"]
    source_schemas = copy.deepcopy(_default_schemas())
    target_schemas = copy.deepcopy(_default_schemas())
    for schemas in (source_schemas, target_schemas):
        schemas["ORDERS"]["constraints"] = [
            row for row in schemas["ORDERS"]["constraints"] if row[1] != "R"
        ]
    source = FakeConnection(tables, {table: 3 for table in tables}, schemas=source_schemas)
    target = FakeConnection(tables, {table: 3 for table in tables}, schemas=target_schemas)
    connections = iter([source, target])
    monkeypatch.setattr(oracle_validation, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_validation.run_validation(E2EConfig(config), driver=Mock())

    check = _report_check(report, "foreign_keys:ORDERS")
    assert report["result"] == "FAIL"
    assert check["status"] == "FAIL"
    assert check["required_relationships_present"] is False


def test_validation_closes_source_if_target_connect_fails(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    source = FakeConnection([], {})
    connections = iter([source])

    def connect(_config, _driver):
        try:
            return next(connections)
        except StopIteration:
            raise RuntimeError("target connection failed") from None

    monkeypatch.setattr(oracle_validation, "connect", connect)
    with pytest.raises(RuntimeError, match="target connection failed"):
        oracle_validation.run_validation(E2EConfig(oracle_test_config), driver=Mock())
    assert source.closed
