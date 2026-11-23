"""Mocked tests for non-destructive Oracle E2E setup."""
from __future__ import annotations

import copy
import json
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from core.connectors.oracle._models import OracleConnectionError
from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle import run_setup as oracle_setup
from tests.e2e.oracle.config import OracleE2EError
from tests.e2e.oracle.config import connect as connect_oracle
from tests.e2e.oracle.owner_policy import validate_oracle_owner_resources
from tests.e2e.oracle.setup.fixture_loader import (
    FixtureResult,
    load_fixture,
    parse_fixture_sql,
    resolve_fixture_files,
    validate_fixture_sql,
)
from tests.unit.oracle_test_settings import load_oracle_test_config, seed_oracle_test_secrets


def make_config(fixture_path: str) -> E2EConfig:
    config = load_oracle_test_config()
    config["e2e"]["fixture_path"] = fixture_path
    return E2EConfig(config)


class FakeCursor:
    def __init__(
        self, connection, count: int = 0, objects: list[str] | None = None
    ):
        self.connection = connection
        self.count = count
        self.objects = objects or []
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str):
        self.statements.append(statement)

    def fetchone(self):
        if 'SELECT COUNT(*) FROM "' in self.statements[-1]:
            table_name = self.statements[-1].split('"')[1]
            return (self.connection.row_counts.get(table_name, 0),)
        return (self.count,)

    def fetchall(self):
        return [(name,) for name in self.objects]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(
        self, count: int = 0, objects: list[str] | None = None,
        row_counts: dict[str, int] | None = None,
    ):
        self.count = count
        self.objects = objects or []
        self.row_counts = {name.upper(): value for name, value in (row_counts or {}).items()}
        self.cursors: list[FakeCursor] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        cursor = FakeCursor(self, self.count, self.objects)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _fixture_dir(tmp_path: Path, config: E2EConfig) -> Path:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for fixture in config.raw["e2e"]["fixtures"]:
        (fixtures / fixture["file"]).write_text(fixture["sql"], encoding="utf-8")
    return fixtures


@pytest.mark.parametrize("side", ["source", "target"])
def test_invalid_owner_fails_before_any_connection(tmp_path, monkeypatch, side):
    cfg = make_config(str(tmp_path))
    bad = copy.deepcopy(cfg.raw["e2e"]["resources"][side])
    bad["name"] = cfg.raw[side]["connection"]["service_name"]
    cfg.raw["e2e"]["resources"][side] = bad
    connect = Mock(side_effect=AssertionError("connect must not be called"))
    monkeypatch.setattr(oracle_setup, "connect", connect)

    with pytest.raises(ValueError, match="service/PDB"):
        oracle_setup.run_setup(cfg, driver=Mock())

    connect.assert_not_called()


def test_missing_secret_fails_safely_before_connection(
    tmp_path, monkeypatch, oracle_test_config
):
    cfg = E2EConfig(oracle_test_config)
    fixtures = _fixture_dir(tmp_path, cfg)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    secret_ref = cfg.source_connection["password_secret"]
    monkeypatch.delenv(f"SECRET_{secret_ref}", raising=False)
    connect = Mock(side_effect=AssertionError("connect must not be called"))
    monkeypatch.setattr(oracle_setup, "connect", connect)

    with pytest.raises(RuntimeError, match="credential could not be resolved") as error:
        oracle_setup.run_setup(cfg, driver=Mock())

    assert secret_ref not in str(error.value)
    connect.assert_not_called()


def test_same_source_and_target_owner_is_rejected_before_connection(tmp_path, monkeypatch):
    cfg = make_config(str(tmp_path))
    shared_owner = copy.deepcopy(cfg.raw["e2e"]["resources"]["source"])
    cfg.raw["e2e"]["resources"]["target"] = shared_owner
    cfg.raw["target"]["connection"]["username"] = shared_owner["name"]
    connect = Mock(side_effect=AssertionError("connect must not be called"))
    monkeypatch.setattr(oracle_setup, "connect", connect)

    with pytest.raises(ValueError, match="must be distinct"):
        oracle_setup.run_setup(cfg, driver=Mock())

    connect.assert_not_called()


def test_unsafe_yaml_fixture_name_fails_before_connection(tmp_path, monkeypatch):
    cfg = make_config(str(tmp_path))
    cfg.raw["e2e"]["fixtures"][0]["file"] = "../outside.sql"
    connect = Mock(side_effect=AssertionError("connect must not be called"))
    monkeypatch.setattr(oracle_setup, "connect", connect)

    with pytest.raises(ValueError, match="plain .sql filenames"):
        oracle_setup.run_setup(cfg, driver=Mock())

    connect.assert_not_called()


@pytest.mark.parametrize(
    ("side", "field", "value", "message"),
    [
        ("source", "host", "", "host must be configured"),
        ("target", "port", None, "port must be configured"),
    ],
)
def test_missing_endpoint_settings_fail_before_connection(
    tmp_path, monkeypatch, oracle_test_config, oracle_test_secrets,
    side, field, value, message
):
    cfg = E2EConfig(oracle_test_config)
    fixtures = _fixture_dir(tmp_path, cfg)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    if value is None:
        cfg.raw[side]["connection"].pop(field)
    else:
        cfg.raw[side]["connection"][field] = value
    connect = Mock(side_effect=AssertionError("connect must not be called"))
    monkeypatch.setattr(oracle_setup, "connect", connect)

    with pytest.raises(ValueError, match=message):
        oracle_setup.run_setup(cfg, driver=Mock())

    connect.assert_not_called()


def test_setup_connects_to_validated_owners_checks_empty_schemas_and_loads_source(
    tmp_path, monkeypatch, oracle_test_secrets
):
    config = load_oracle_test_config()
    fixtures = _fixture_dir(tmp_path, E2EConfig(config))
    cfg = E2EConfig(config)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    source = FakeConnection(
        objects=config["e2e"]["expected"]["objects"]
    )
    target = FakeConnection()
    connect_configs: list[dict] = []

    def connect(config, _driver):
        connect_configs.append(config)
        return source if config["username"] == cfg.source_connection["username"] else target

    monkeypatch.setattr(oracle_setup, "connect", connect)
    report = oracle_setup.run_setup(cfg, driver=Mock())

    owners = validate_oracle_owner_resources(cfg)
    assert owners == {
        "source": cfg.raw["e2e"]["resources"]["source"]["name"],
        "target": cfg.raw["e2e"]["resources"]["target"]["name"],
    }
    assert [config["username"] for config in connect_configs] == [
        owners["source"], owners["target"]
    ]
    assert [config["password"] for config in connect_configs] == [
        oracle_test_secrets["source"], oracle_test_secrets["target"]
    ]
    expected_statements = [fixture["sql"] for fixture in config["e2e"]["fixtures"]]
    assert [cursor.statements[0] for cursor in source.cursors[1:-1]] == expected_statements
    assert source.commits == len(expected_statements)
    assert source.closed and target.closed
    assert report["setup_complete"] is True
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["source_connection"]["status"] == "PASS"
    assert checks["target_connection"]["status"] == "PASS"
    assert checks["source_empty"] == {
        "name": "source_empty", "status": "PASS", "object_count": 0
    }
    assert checks["target_empty"] == {
        "name": "target_empty", "status": "PASS", "object_count": 0
    }
    assert checks["fixture_load"]["status"] == "PASS"
    assert [item["ok"] for item in checks["fixture_load"]["scripts"]] == [True, True]
    assert checks["expected_objects"]["status"] == "PASS"


def test_yaml_only_configuration_changes_drive_setup_without_code_changes(
    tmp_path, monkeypatch
):
    raw = load_oracle_test_config()
    original_prefix = raw["e2e"]["oracle_test_owner_prefix"]
    changed_prefix = f"ALT_{original_prefix}"
    original_table = raw["e2e"]["expected"]["objects"][0]
    changed_table = changed_prefix + original_table.removeprefix(original_prefix)
    original_pattern = raw["e2e"]["resources"]["source"]["safe_pattern"]
    for side in ("source", "target"):
        raw[side]["connection"]["host"] = f"changed-{raw[side]['connection']['host']}"
        raw[side]["connection"]["port"] += 1
        raw[side]["connection"]["service_name"] += f"_{side.upper()}_ALT"
        raw[side]["connection"]["username"] = changed_prefix + side.upper()
        raw[side]["connection"]["password_secret"] += "_alt"
        resource = raw["e2e"]["resources"][side]
        resource["name"] = raw[side]["connection"]["username"]
        resource["safe_pattern"] = original_pattern.replace(original_prefix, changed_prefix)
    raw["e2e"]["oracle_test_owner_prefix"] = changed_prefix
    raw["e2e"]["expected"]["objects"] = [changed_table]
    for fixture in raw["e2e"]["fixtures"]:
        fixture["file"] = f"changed_{fixture['file']}"
        fixture["sql"] = fixture["sql"].replace(original_table, changed_table)
    fixture_dir = tmp_path / "changed-fixtures"
    fixture_dir.mkdir()
    raw["e2e"]["fixture_path"] = str(fixture_dir)
    for fixture in raw["e2e"]["fixtures"]:
        (fixture_dir / fixture["file"]).write_text(fixture["sql"], encoding="utf-8")

    yaml_path = tmp_path / "oracle-config.yaml"
    yaml_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_oracle_test_config(yaml_path)
    secret_values = seed_oracle_test_secrets(config, monkeypatch)
    cfg = E2EConfig(config)
    source = FakeConnection(objects=config["e2e"]["expected"]["objects"])
    target = FakeConnection()
    connect_configs: list[dict] = []

    def connect(connection_config, _driver):
        connect_configs.append(connection_config)
        return source if connection_config["username"] == cfg.source_connection["username"] else target

    monkeypatch.setattr(oracle_setup, "connect", connect)
    report = oracle_setup.run_setup(cfg, driver=Mock())

    assert report["setup_complete"] is True
    assert report["source_owner"] == config["e2e"]["resources"]["source"]["name"]
    assert report["target_owner"] == config["e2e"]["resources"]["target"]["name"]
    assert report["source_service_name"] == config["source"]["connection"]["service_name"]
    assert report["target_service_name"] == config["target"]["connection"]["service_name"]
    assert [item["host"] for item in connect_configs] == [
        config[side]["connection"]["host"] for side in ("source", "target")
    ]
    assert [item["port"] for item in connect_configs] == [
        config[side]["connection"]["port"] for side in ("source", "target")
    ]
    assert [item["service_name"] for item in connect_configs] == [
        config[side]["connection"]["service_name"] for side in ("source", "target")
    ]
    assert [item["username"] for item in connect_configs] == [
        config[side]["connection"]["username"] for side in ("source", "target")
    ]
    assert [item["password"] for item in connect_configs] == [
        secret_values["source"], secret_values["target"]
    ]
    object_check = next(check for check in report["checks"] if check["name"] == "expected_objects")
    assert object_check["objects"] == config["e2e"]["expected"]["objects"]


def test_nonempty_source_or_target_schema_fails_without_loading(
    tmp_path, monkeypatch, oracle_test_config, oracle_test_secrets
):
    fixtures = _fixture_dir(tmp_path, E2EConfig(oracle_test_config))
    cfg = E2EConfig(oracle_test_config)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    source = FakeConnection(count=1)
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    with pytest.raises(RuntimeError, match="already contains 1 objects"):
        oracle_setup.run_setup(cfg, driver=Mock())

    assert source.closed and target.closed
    assert source.commits == 0


@pytest.mark.parametrize(
    ("actual_tables", "message"),
    [
        (["CUSTOMERS", "ORDERS"], "missing=.*ORDERDETAILS"),
    ],
)
def test_preloaded_setup_rejects_missing_or_unexpected_source_tables(
    monkeypatch, oracle_test_config, oracle_test_secrets, actual_tables, message
):
    config = oracle_test_config
    expected = ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]
    config["e2e"]["source_mode"] = "preloaded"
    config["e2e"].pop("fixtures", None)
    config["e2e"]["expected"] = {
        "objects": expected,
        "row_counts": {name: 3 for name in expected},
    }
    config["source"]["connection"]["include_tables"] = expected
    config["e2e"].pop("fixture_path", None)
    source = FakeConnection(objects=actual_tables)
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    with pytest.raises(RuntimeError, match=message):
        oracle_setup.run_setup(E2EConfig(config), driver=Mock())

    assert source.commits == 0
    assert not any("CREATE" in statement or "INSERT" in statement for cursor in source.cursors for statement in cursor.statements)
    assert source.closed and target.closed


def test_preloaded_setup_verifies_exact_tables_and_counts_without_source_writes(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = oracle_test_config
    expected = ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]
    config["e2e"]["source_mode"] = "preloaded"
    config["e2e"].pop("fixtures", None)
    config["e2e"]["expected"] = {
        "objects": expected,
        "row_counts": {name: 3 for name in expected},
    }
    config["source"]["connection"]["include_tables"] = expected
    config["e2e"].pop("fixture_path", None)
    source = FakeConnection(objects=expected, row_counts={name: 3 for name in expected})
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_setup.run_setup(E2EConfig(config), driver=Mock())

    checks = {check["name"]: check for check in report["checks"]}
    assert report["setup_complete"] is True
    assert checks["fixture_load"]["status"] == "SKIP"
    assert checks["source_tables"]["objects"] == sorted(expected)
    assert checks["source_tables"]["excluded"] == []
    for table in expected:
        row_check = checks[f"source_row_count:{table}"]
        assert row_check["name"] == f"source_row_count:{table}"
        assert row_check["status"] == "PASS"
        assert row_check["expected"] == 3
        assert row_check["actual"] == 3
        assert row_check["comparison"] == "configured_expectation"
    assert source.commits == 0
    assert not any("CREATE" in statement or "INSERT" in statement for cursor in source.cursors for statement in cursor.statements)
    assert source.closed and target.closed


def test_preloaded_setup_discovers_all_source_tables_without_fixed_expectations(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = oracle_test_config
    config["e2e"]["source_mode"] = "preloaded"
    config["e2e"].pop("fixtures", None)
    config["e2e"].pop("expected", None)
    config["source"]["connection"].pop("include_tables", None)
    source_tables = ["DISCOVERED_ALPHA", "DISCOVERED_BETA"]
    source = FakeConnection(
        objects=source_tables,
        row_counts={"DISCOVERED_ALPHA": 3, "DISCOVERED_BETA": 7},
    )
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_setup.run_setup(E2EConfig(config), driver=Mock())

    checks = {check["name"]: check for check in report["checks"]}
    assert report["setup_complete"] is True
    assert checks["source_tables"]["scope"] == "schema_wide"
    assert checks["source_tables"]["objects"] == sorted(source_tables)
    assert checks["source_row_count:DISCOVERED_ALPHA"]["actual"] == 3
    assert checks["source_row_count:DISCOVERED_BETA"]["actual"] == 7
    assert checks["source_row_count:DISCOVERED_ALPHA"]["comparison"] == "discovered_baseline"
    assert source.commits == 0
    assert not any("CREATE" in statement or "INSERT" in statement for cursor in source.cursors for statement in cursor.statements)
    assert source.closed and target.closed


def test_preloaded_setup_fails_on_row_count_mismatch_without_writing_source(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = oracle_test_config
    expected = ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]
    config["e2e"]["source_mode"] = "preloaded"
    config["e2e"].pop("fixtures", None)
    config["e2e"]["expected"] = {
        "objects": expected,
        "row_counts": {name: 3 for name in expected},
    }
    config["source"]["connection"]["include_tables"] = expected
    config["e2e"].pop("fixture_path", None)
    source = FakeConnection(
        objects=expected,
        row_counts={"CUSTOMERS": 3, "ORDERS": 2, "ORDERDETAILS": 3},
    )
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    with pytest.raises(RuntimeError, match="row count validation failed for ORDERS"):
        oracle_setup.run_setup(E2EConfig(config), driver=Mock())

    assert source.commits == 0
    assert source.closed and target.closed


def test_preloaded_setup_ignores_but_reports_unallowlisted_source_tables(
    monkeypatch, oracle_test_config, oracle_test_secrets
):
    config = oracle_test_config
    expected = ["CUSTOMERS", "ORDERS", "ORDERDETAILS"]
    config["e2e"]["source_mode"] = "preloaded"
    config["e2e"].pop("fixtures", None)
    config["e2e"]["expected"] = {
        "objects": expected,
        "row_counts": {name: 3 for name in expected},
    }
    config["source"]["connection"]["include_tables"] = expected
    config["e2e"].pop("fixture_path", None)
    source = FakeConnection(
        objects=[*expected, "ORACLE_E2E_SAMPLE"],
        row_counts={name: 3 for name in expected},
    )
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    report = oracle_setup.run_setup(E2EConfig(config), driver=Mock())

    checks = {check["name"]: check for check in report["checks"]}
    assert report["setup_complete"] is True
    assert checks["expected_objects"]["status"] == "PASS"
    assert checks["expected_objects"]["excluded"] == ["ORACLE_E2E_SAMPLE"]
    assert source.commits == 0
    assert source.closed and target.closed


def test_expected_objects_are_taken_from_yaml_and_checked(tmp_path, monkeypatch):
    raw = load_oracle_test_config()
    seed_oracle_test_secrets(raw, monkeypatch)
    fixtures = _fixture_dir(tmp_path, E2EConfig(raw))
    raw["e2e"]["fixture_path"] = str(fixtures)
    cfg = E2EConfig(raw)
    source = FakeConnection(objects=[])
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    with pytest.raises(RuntimeError, match="source table validation failed"):
        oracle_setup.run_setup(cfg, driver=Mock())

    assert source.closed and target.closed
    assert source.commits == len(raw["e2e"]["fixtures"])


def test_fixture_sql_rejects_destructive_multistatement_or_cross_owner_sql(
    oracle_test_config
):
    config = E2EConfig(oracle_test_config)
    fixtures = config.raw["e2e"]["fixtures"]
    table = config.raw["e2e"]["expected"]["objects"][0]
    other_owner = config.raw["e2e"]["resources"]["target"]["name"]
    seed_sql = next(
        fixture["sql"]
        for fixture in fixtures
        if fixture["sql"].lstrip().upper().startswith("INSERT INTO")
    )
    invalid_sql = (
        f"DROP TABLE {table}",
        f"CREATE USER {other_owner}",
        f"INSERT INTO {other_owner}.{table} VALUES (1)",
        f"{seed_sql}; DELETE FROM {table}",
        "BEGIN NULL; END;",
    )
    for sql in invalid_sql:
        with pytest.raises(ValueError):
            validate_fixture_sql(sql)


def test_fixture_loader_executes_only_sorted_allowed_scripts(tmp_path, oracle_test_config):
    config = oracle_test_config["e2e"]["fixtures"]
    for fixture in reversed(config):
        (tmp_path / fixture["file"]).write_text(fixture["sql"], encoding="utf-8")
    unlisted = f"{Path(config[0]['file']).stem}_unlisted.sql"
    (tmp_path / unlisted).write_text(config[0]["sql"], encoding="utf-8")
    connection = FakeConnection()

    result = load_fixture(connection, tmp_path, [fixture["file"] for fixture in config])

    assert result.all_ok
    assert [item["script"] for item in result.results] == sorted(
        fixture["file"] for fixture in config
    )
    assert connection.commits == len(config)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TYPE VALUE_T AS OBJECT (AMOUNT NUMBER)",
        "CREATE TYPE VALUE_ARRAY_T AS VARRAY(5) OF VALUE_T",
        "CREATE TYPE VALUE_NT AS TABLE OF VARCHAR2(20)",
        "ALTER TABLE CHILD_T ADD CONSTRAINT CHILD_FK FOREIGN KEY (PARENT_ID) REFERENCES PARENT_T (ID)",
        "CREATE MATERIALIZED VIEW MV_T BUILD IMMEDIATE REFRESH COMPLETE ON DEMAND AS SELECT ID FROM SOURCE_T",
        "CREATE SYNONYM SOURCE_ALIAS FOR SOURCE_T",
        "COMMENT ON COLUMN TABLE_T.VALUE IS 'fixture value'",
        "CREATE FUNCTION F RETURN NUMBER IS BEGIN RETURN 1; END;",
        "CREATE PROCEDURE P IS BEGIN NULL; END;",
        "CREATE TRIGGER TRG AFTER INSERT ON T FOR EACH ROW BEGIN NULL; END;",
    ],
)
def test_fixture_sql_accepts_supported_oracle_statement_categories(sql):
    assert validate_fixture_sql(sql) == sql


def test_fixture_parser_handles_multiple_sql_statements_and_plsql_delimiters():
    sql = """CREATE TABLE FIRST_T (VALUE_TEXT VARCHAR2(30));
CREATE TABLE SECOND_T (VALUE_TEXT VARCHAR2(30));
CREATE PROCEDURE P IS
BEGIN
    UPDATE FIRST_T SET VALUE_TEXT = 'internal; semicolon';
    IF SQL%ROWCOUNT = 0 THEN
        NULL;
    END IF;
END;
/
CREATE TRIGGER T AFTER INSERT ON FIRST_T FOR EACH ROW
BEGIN
    NULL;
END;
/
"""
    statements = parse_fixture_sql(sql)
    assert len(statements) == 4
    assert "'internal; semicolon'" in statements[2]
    assert statements[2].endswith("END;")
    assert statements[3].startswith("CREATE TRIGGER")


def test_fixture_parser_keeps_oracle_quoted_string_semicolons_inside_statement():
    sql = "INSERT INTO T (VALUE_TEXT) VALUES ('one; two');\n"
    assert parse_fixture_sql(sql) == [
        "INSERT INTO T (VALUE_TEXT) VALUES ('one; two')"
    ]
    q_sql = "INSERT INTO T (VALUE_TEXT) VALUES (q'[three; four]');"
    assert parse_fixture_sql(q_sql) == [
        "INSERT INTO T (VALUE_TEXT) VALUES (q'[three; four]')"
    ]


def test_loader_executes_all_statements_in_selected_category_file_and_fails_before_writes(tmp_path):
    fixture = tmp_path / "03_create_tables.sql"
    fixture.write_text(
        "CREATE TABLE FIRST_T (ID NUMBER); CREATE TABLE SECOND_T (ID NUMBER);",
        encoding="utf-8",
    )
    connection = FakeConnection()
    result = load_fixture(connection, tmp_path, [fixture.name])
    assert result.all_ok
    assert result.results == [{
        "script": fixture.name, "ok": True, "statement_count": 2,
    }]
    assert connection.cursors[0].statements == [
        "CREATE TABLE FIRST_T (ID NUMBER)",
        "CREATE TABLE SECOND_T (ID NUMBER)",
    ]
    assert connection.commits == 2

    fixture.write_text(
        "CREATE TABLE THIRD_T (ID NUMBER); DROP TABLE THIRD_T;",
        encoding="utf-8",
    )
    blocked = FakeConnection()
    with pytest.raises(ValueError, match="unsupported SQL operation"):
        load_fixture(blocked, tmp_path, [fixture.name])
    assert blocked.cursors == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TYPE BODY VALUE_T AS MEMBER PROCEDURE P IS BEGIN NULL; END; END;",
        "CREATE USER SOME_USER IDENTIFIED BY SOME_PASSWORD",
        "GRANT SELECT ON T TO R",
        "ALTER TABLE T DROP CONSTRAINT C",
        "CREATE TABLE T (ID NUMBER); INSERT INTO T VALUES (1)",
        "CREATE PROCEDURE P IS BEGIN NULL; END; INSERT INTO T VALUES (1)",
        "CREATE PROCEDURE P IS BEGIN EXECUTE IMMEDIATE 'DROP TABLE T'; END;",
        "CREATE TABLE OWNER_NAME.TABLE_NAME (ID NUMBER)",
    ],
)
def test_fixture_sql_rejects_unsafe_or_unsupported_oracle_statements(sql):
    with pytest.raises(ValueError):
        validate_fixture_sql(sql)


def test_canonical_and_historical_oracle_fixture_sets_are_separate_and_ordered():
    fixture_dir = Path(__file__).parents[1] / "e2e" / "oracle" / "fixtures"
    canonical_names = [path.name for path in fixture_dir.glob("*.sql")]
    canonical_files = resolve_fixture_files(fixture_dir, canonical_names)
    expected_canonical = [
        "02_create_types.sql", "03_create_tables.sql", "04_create_foreign_keys.sql",
        "05_create_indexes.sql", "06_create_sequences.sql", "07_create_views.sql",
        "08_create_functions.sql", "09_create_procedures.sql", "10_create_triggers.sql",
        "11_create_synonyms.sql", "12_create_partitions.sql", "14_create_comments.sql",
        "15_seed_data.sql", "16_create_materialized_view.sql",
    ]
    assert [path.name for path in canonical_files] == expected_canonical
    statement_counts = {
        path.name: len(parse_fixture_sql(path.read_text(encoding="utf-8"), filename=path.name))
        for path in canonical_files
    }
    assert statement_counts == {
        "02_create_types.sql": 3, "03_create_tables.sql": 6,
        "04_create_foreign_keys.sql": 3, "05_create_indexes.sql": 1,
        "06_create_sequences.sql": 1, "07_create_views.sql": 1,
        "08_create_functions.sql": 1, "09_create_procedures.sql": 1,
        "10_create_triggers.sql": 1, "11_create_synonyms.sql": 1,
        "12_create_partitions.sql": 1, "14_create_comments.sql": 2,
        "15_seed_data.sql": 29, "16_create_materialized_view.sql": 1,
    }
    assert "13_create_security.sql" not in canonical_names

    historical_dir = fixture_dir / "historical"
    historical_names = [path.name for path in historical_dir.glob("*.sql")]
    historical_files = resolve_fixture_files(historical_dir, historical_names)
    assert [path.name for path in historical_files] == [
        "01_create_p10_s46_parent.sql", "01_create_sample.sql",
        "02_seed_p10_s46_parent.sql", "02_seed_sample.sql",
        "03_create_p10_s46_child.sql", "04_create_p10_s46_independent.sql",
        "05_seed_p10_s46_child.sql", "06_seed_p10_s46_independent.sql",
    ]
    history_connection = FakeConnection()
    history_result = load_fixture(history_connection, historical_dir, historical_names)
    assert history_result.all_ok
    assert len(history_connection.cursors) == 8


def test_oracle_seed_data_matches_common_peer_cases_and_preserves_oracle_values():
    fixture_dir = Path(__file__).parents[1] / "e2e" / "oracle" / "fixtures"
    seed_path = fixture_dir / "15_seed_data.sql"
    statements = parse_fixture_sql(seed_path.read_text(encoding="utf-8"), filename=seed_path.name)
    insert_counts = Counter(
        re.match(r"(?is)^INSERT\s+INTO\s+([A-Z0-9_$#]+)", statement).group(1).upper()
        for statement in statements
    )
    assert insert_counts == {
        "E2E_CUSTOMER": 5,
        "E2E_PRODUCT": 5,
        "E2E_ORDER": 6,
        "E2E_ORDER_DETAIL": 8,
        "E2E_PK_NAME_TEST": 2,
        "E2E_PARTITIONED_EVENT": 3,
    }

    seed_sql = seed_path.read_text(encoding="utf-8")
    for value in (
        "'CUST001', 'Alice Johnson', 'alice@example' || CHR(46) || 'com',",
        "'CUST002', 'Bob Smith', 'bob@example' || CHR(46) || 'com',",
        "'CUST003', 'Carol White', 'carol@example' || CHR(46) || 'com',",
        "'CUST004', 'David Brown', 'david@example' || CHR(46) || 'com',",
        "'CUST005', 'Eva Green', 'eva@example' || CHR(46) || 'com',",
        "'SKU001', 'Laptop Pro 15', 'Electronics', 75000.00, 50, 1",
        "'SKU002', 'Wireless Mouse', 'Electronics', 1200.00, 200, 1",
        "'SKU003', 'USB-C Hub', 'Electronics', 2500.00, 150, 1",
        "'SKU004', 'Desk Chair', 'Furniture', 12000.00, 30, 1",
        "'SKU005', 'Standing Desk', 'Furniture', 25000.00, 20, 1",
        "1, E2E_ORDER_NUMBER_SEQ.NEXTVAL, 77400.00, 'delivered'",
        "1, E2E_ORDER_NUMBER_SEQ.NEXTVAL, 3500.00, 'shipped'",
        "2, E2E_ORDER_NUMBER_SEQ.NEXTVAL, 2500.00, 'shipped'",
        "3, E2E_ORDER_NUMBER_SEQ.NEXTVAL, 75000.00, 'delivered'",
        "4, E2E_ORDER_NUMBER_SEQ.NEXTVAL, 8600.00, 'pending'",
        "E2E_VALUE_T(987654321012345.12345, 'primary'",
        "E2E_VALUE_ARRAY_T(", "E2E_VALUE_T(NULL, NULL, NULL)",
        "NULL, NULL, EMPTY_CLOB(), EMPTY_BLOB()", "HEXTORAW('01020304')",
        "'Test Alpha'", "'Test Beta'",
        "DATE '2024-06-15', 15000.00, 'shipped'",
        "DATE '2025-02-01', 25000.00, 'delivered'",
        "DATE '2025-08-01', 987654321012345.12345, 'pending'",
        "987654321012345.12345",
    ):
        assert value in seed_sql
    assert seed_sql.count("E2E_ORDER_NUMBER_SEQ.NEXTVAL") == 6

    # These detail rows use generated parent IDs 1–5 in the deterministic
    # insert order; product references are likewise generated as 1–5.
    detail_rows = [
        (int(order_id), int(product_id), int(quantity), Decimal(price))
        for match in re.findall(
            r"(?is)INSERT\s+INTO\s+E2E_ORDER_DETAIL.*?VALUES\s*\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)",
            seed_sql,
        )
        for order_id, product_id, quantity, price in [match]
    ]
    assert detail_rows == [
        (1, 1, 1, Decimal("75000.00")), (1, 2, 2, Decimal("1200.00")),
        (2, 3, 1, Decimal("2500.00")), (2, 4, 1, Decimal("1000.00")),
        (3, 5, 1, Decimal("25000.00")), (4, 1, 1, Decimal("75000.00")),
        (5, 2, 3, Decimal("1200.00")), (5, 3, 2, Decimal("2500.00")),
    ]
    assert all(order_id in range(1, 6) and product_id in range(1, 6)
               for order_id, product_id, _quantity, _price in detail_rows)

    table_ddl = (fixture_dir / "03_create_tables.sql").read_text(encoding="utf-8")
    fk_ddl = (fixture_dir / "04_create_foreign_keys.sql").read_text(encoding="utf-8")
    partition_ddl = (fixture_dir / "12_create_partitions.sql").read_text(encoding="utf-8")
    trigger_ddl = (fixture_dir / "10_create_triggers.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE E2E_PRODUCT" in table_ddl
    assert "CREATE TABLE E2E_PK_NAME_TEST" in table_ddl
    assert "CONSTRAINT E2E_PK_TEST_PRIMARY_KEY PRIMARY KEY (TEST_ID)" in table_ddl
    assert "PRODUCT_ID NUMBER NOT NULL" in table_ddl
    assert "REFERENCES E2E_PRODUCT (PRODUCT_ID)" in fk_ddl
    assert "REFERENCES E2E_ORDER (ORDER_ID)" in fk_ddl
    for partition_date in ("2025-01-01", "2025-07-01", "2026-01-01"):
        assert f"DATE '{partition_date}'" in partition_ddl
    assert "AFTER INSERT ON E2E_ORDER" in trigger_ddl
    assert "INSERT INTO E2E_ORDER_AUDIT" in trigger_ddl
    assert "E2E_ORDER_AUDIT" not in seed_sql
    assert insert_counts["E2E_ORDER"] == 6  # trigger creates six audit rows


def test_e2e_connect_reports_sanitized_oracle_authentication_error():
    password = "sentinel-password-must-not-appear"

    class OracleError(Exception):
        pass

    class ErrorInfo:
        code = 1017
        message = f"ORA-01017: invalid login; leaked={password}"

    class Driver:
        Error = OracleError

        @staticmethod
        def makedsn(host, port, service_name):
            return f"{host}:{port}/{service_name}"

        @staticmethod
        def connect(**_kwargs):
            raise OracleError(ErrorInfo())

    config = {
        "host": "db.example",
        "port": 1521,
        "service_name": "FREEPDB1",
        "username": "E2E_SOURCE",
        "password": password,
    }
    with pytest.raises(OracleConnectionError, match="authentication failed.*ORA-01017") as error:
        connect_oracle(config, Driver)

    assert password not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("side", ["source", "target"])
def test_setup_identifies_connection_side_with_sanitized_error(
    tmp_path, monkeypatch, oracle_test_secrets, side
):
    cfg = make_config(str(tmp_path))
    fixtures = _fixture_dir(tmp_path, cfg)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    source = FakeConnection()
    target = FakeConnection()
    failure = OracleConnectionError("Oracle authentication failed (ORA-01017)")

    def connect(connection_config, _driver):
        if connection_config["username"] == cfg.raw[side]["connection"]["username"]:
            raise failure
        return source if connection_config["username"] == cfg.source_connection["username"] else target

    monkeypatch.setattr(oracle_setup, "connect", connect)
    with pytest.raises(OracleE2EError, match=rf"Oracle {side} connection failed.*ORA-01017") as error:
        oracle_setup.run_setup(cfg, driver=Mock())
    failed_check = next(
        check for check in error.value.report["checks"]
        if check["name"] == f"{side}_connection"
    )
    assert failed_check["status"] == "FAIL"
    assert "ORA-01017" in failed_check["message"]


def test_fixture_failure_reports_script_and_code_without_driver_text(
    tmp_path, monkeypatch, oracle_test_secrets
):
    cfg = make_config(str(tmp_path))
    fixtures = _fixture_dir(tmp_path, cfg)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    source = FakeConnection()
    target = FakeConnection()
    monkeypatch.setattr(
        oracle_setup,
        "connect",
        lambda config, _driver: source if config["username"] == cfg.source_connection["username"] else target,
    )
    monkeypatch.setattr(
        oracle_setup,
        "load_fixture",
        lambda *_args: FixtureResult(
            fixture_dir=str(fixtures),
            results=[
                {
                    "script": cfg.raw["e2e"]["fixtures"][0]["file"],
                    "ok": False,
                    "error_code": 1031,
                }
            ],
        ),
    )

    with pytest.raises(OracleE2EError, match="fixture loading failed.*ORA-01031"):
        oracle_setup.run_setup(cfg, driver=Mock())

    assert source.closed and target.closed


@pytest.mark.parametrize(
    ("source_count", "target_count", "expected_check"),
    [(1, 0, "source_empty"), (0, 1, "target_empty")],
)
def test_nonempty_owner_is_reported_as_failed_empty_check(
    tmp_path, monkeypatch, oracle_test_config, oracle_test_secrets,
    source_count, target_count, expected_check,
):
    fixtures = _fixture_dir(tmp_path, E2EConfig(oracle_test_config))
    cfg = E2EConfig(oracle_test_config)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    source = FakeConnection(count=source_count)
    target = FakeConnection(count=target_count)
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))

    with pytest.raises(OracleE2EError) as error:
        oracle_setup.run_setup(cfg, driver=Mock())

    failed_check = next(
        check for check in error.value.report["checks"]
        if check["name"] == expected_check
    )
    assert failed_check["status"] == "FAIL"
    assert failed_check["object_count"] == 1
    assert source.closed and target.closed
    assert source.commits == 0


def test_setup_cli_prints_only_recorded_statuses_and_report_path(
    tmp_path, monkeypatch, capsys
):
    cfg = E2EConfig(load_oracle_test_config())
    report = {
        "engine": "oracle",
        "source_owner": "CONFIGURED_SOURCE_OWNER",
        "target_owner": "CONFIGURED_TARGET_OWNER",
        "service_name": "CONFIGURED_SERVICE",
        "source_service_name": "CONFIGURED_SOURCE_SERVICE",
        "target_service_name": "CONFIGURED_TARGET_SERVICE",
        "checks": [
            {"name": "source_connection", "status": "PASS"},
            {"name": "target_connection", "status": "PASS"},
            {
                "name": "fixture_load",
                "status": "PASS",
                "scripts": [
                    {"script": "fixture_a.sql", "ok": True},
                    {"script": "fixture_b.sql", "ok": True},
                    {"script": "fixture_status_missing.sql"},
                ],
            },
            {"name": "expected_objects", "status": "PASS", "objects": ["SAMPLE_TABLE"]},
            {"name": "source_empty", "status": "PASS", "object_count": 0},
            {"name": "target_empty", "status": "PASS", "object_count": 0},
        ],
        "setup_complete": True,
    }
    monkeypatch.setattr(oracle_setup, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setattr(oracle_setup, "run_setup", lambda _cfg: report)
    monkeypatch.setattr(oracle_setup, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_setup.py", "--config", "configured.yaml"])
    historical_path = tmp_path / "setup_report.json"
    historical_content = json.dumps(
        {"source_owner": "HISTORICAL_SOURCE", "service_name": "HISTORICAL_SERVICE"}
    )
    historical_path.write_text(historical_content, encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        oracle_setup.main()

    output = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "Source owner: CONFIGURED_SOURCE_OWNER" in output
    assert "Target owner: CONFIGURED_TARGET_OWNER" in output
    assert "Service: CONFIGURED_SOURCE_SERVICE" in output
    assert "Service: CONFIGURED_TARGET_SERVICE" in output
    assert "[PASS] source_connection" in output
    assert "[PASS] target_connection" in output
    assert "[PASS] fixture_a.sql" in output
    assert "[PASS] fixture_b.sql" in output
    assert "[UNKNOWN] fixture_status_missing.sql" in output
    assert "[PASS] expected_objects" in output
    assert "SETUP: COMPLETE" in output
    assert historical_path.read_text(encoding="utf-8") == historical_content
    generated = list(tmp_path.glob("setup_report_*.json"))
    assert len(generated) == 1
    new_report = json.loads(generated[0].read_text(encoding="utf-8"))
    assert new_report["source_owner"] == "CONFIGURED_SOURCE_OWNER"
    assert new_report["target_owner"] == "CONFIGURED_TARGET_OWNER"
    assert new_report["source_service_name"] == "CONFIGURED_SOURCE_SERVICE"
    assert new_report["target_service_name"] == "CONFIGURED_TARGET_SERVICE"
    assert f"Report: {generated[0]}" in output
    assert "password" not in output.casefold()


def test_setup_cli_writes_and_displays_failed_empty_check_without_secrets(
    tmp_path, monkeypatch, capsys, oracle_test_secrets
):
    cfg = make_config(str(tmp_path))
    fixtures = _fixture_dir(tmp_path, cfg)
    cfg.raw["e2e"]["fixture_path"] = str(fixtures)
    source = FakeConnection(count=1)
    target = FakeConnection()
    connections = iter([source, target])
    monkeypatch.setattr(oracle_setup, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setattr(oracle_setup, "connect", lambda _cfg, _driver: next(connections))
    report_dir = tmp_path / "reports"
    monkeypatch.setattr(oracle_setup, "REPORTS_DIR", report_dir)
    monkeypatch.setattr(sys, "argv", ["run_setup.py", "--config", "configured.yaml"])

    with pytest.raises(SystemExit) as exit_info:
        oracle_setup.main()

    output = capsys.readouterr().out
    report_text = (report_dir / "setup_report.json").read_text(encoding="utf-8")
    assert exit_info.value.code == 1
    assert "[PASS] source_connection" in output
    assert "[PASS] target_connection" in output
    assert "[FAIL] source_empty" in output
    assert "SETUP: FAILED" in output
    assert f"Report: {report_dir / 'setup_report.json'}" in output
    assert '"name": "source_empty"' in report_text
    assert '"status": "FAIL"' in report_text
    for secret in oracle_test_secrets.values():
        assert secret not in output
        assert secret not in report_text
