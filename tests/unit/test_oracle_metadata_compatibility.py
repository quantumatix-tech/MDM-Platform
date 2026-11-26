from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle import check_metadata_compatibility as checker
from tests.e2e.oracle import config as oracle_e2e_config
from tests.e2e.oracle.config import OracleE2EError


def _run_checker_cli(capsys, args):
    exit_code = checker.main(args)
    summary_text = capsys.readouterr().out
    summary = json.loads(summary_text)
    details_path = summary.get("details_file")
    if details_path:
        try:
            report = json.loads(Path(details_path).read_text(encoding="utf-8"))
        finally:
            Path(details_path).unlink(missing_ok=True)
    else:
        report = summary
    return exit_code, report, summary, summary_text


class FakeOracleError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection, errors=()):
        self.connection = connection
        self.errors = tuple(errors)
        self.statement = ""
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, *_args, **_kwargs):
        self.statement = str(statement)
        self.connection.statements.append(self.statement)
        if "select * from user_sequences where 1 = 0" in self.statement.casefold():
            self.description = [(name,) for name in (
                "SEQUENCE_NAME", "MIN_VALUE", "MAX_VALUE", "INCREMENT_BY",
                "CYCLE_FLAG", "ORDER_FLAG", "CACHE_SIZE", "LAST_NUMBER",
            )]
        if any(marker.casefold() in self.statement.casefold() for marker in self.errors):
            raise FakeOracleError('ORA-00904: "TYPE_NAME": invalid identifier')

    def fetchone(self):
        sql = self.statement.casefold()
        if "sys_context" in sql:
            return (
                self.connection.username,
                "CDB_TEST",
                self.connection.container,
                self.connection.service,
            )
        if "select user from dual" in sql:
            return (self.connection.username,)
        return None

    def fetchall(self):
        sql = self.statement.casefold()
        if "select user from dual" in sql:
            return [(self.connection.username,)]
        if "select t.table_name from user_tables t" in sql:
            return [(name,) for name in self.connection.tables]
        if "select o.object_name, o.status, t.typecode" in sql:
            return list(self.connection.types)
        if "from user_type_attrs order by type_name" in sql:
            return list(self.connection.type_attributes)
        if "from user_tab_cols where table_name = :1" in sql:
            return list(self.connection.columns)
        if "from user_objects" in sql and "object_type not in" in sql:
            return list(self.connection.unsupported_objects)
        return []

    def fetchmany(self, *_args, **_kwargs):
        return self.fetchall()

    def close(self):
        pass


class FakeConnection:
    def __init__(
        self, username, container, service, errors=(), *, tables=(), columns=(),
        unsupported_objects=(), types=(), type_attributes=(),
    ):
        self.username = username
        self.container = container
        self.service = service
        self.errors = errors
        self.tables = tuple(tables)
        self.columns = tuple(columns)
        self.unsupported_objects = tuple(unsupported_objects)
        self.types = tuple(types)
        self.type_attributes = tuple(type_attributes)
        self.statements = []

    def cursor(self, *_args, **_kwargs):
        return FakeCursor(self, self.errors)

    def close(self):
        pass


class FakeDriver:
    Error = FakeOracleError


class AuthDriver:
    Error = FakeOracleError

    def __init__(self, *, failures=()):
        self.failures = set(failures)
        self.calls = []

    @staticmethod
    def makedsn(host, port, *, service_name):
        return f"{host}:{port}/{service_name}"

    def connect(self, *, user, password, dsn, **_kwargs):
        self.calls.append({"user": user, "password": password, "dsn": dsn})
        if user in self.failures:
            raise FakeOracleError("ORA-01017: invalid username/password; logon denied")
        service = dsn.rsplit("/", 1)[-1]
        container = "SOURCE_PDB_A" if service == "source_service" else "TARGET_PDB_B"
        return FakeConnection(user, container, service)


def _config() -> E2EConfig:
    raw = {
        "source": {
            "engine": "oracle",
            "connection": {
                "host": "127.0.0.1", "port": 1521, "username": "MIGR_SOURCE_A",
                "service_name": "source_service", "password_secret": "source_pass",
            },
        },
        "target": {
            "engine": "oracle",
            "connection": {
                "host": "127.0.0.1", "port": 1521, "username": "MIGR_TARGET_B",
                "service_name": "target_service", "password_secret": "target_pass",
            },
        },
        "secrets": {
            "provider": "env", "local_encrypted_file": {"file": "not-opened-by-owner-auth"},
        },
        "e2e": {
            "oracle_test_owner_prefix": "MIGR_",
            "source_mode": "preloaded",
            "resources": {
                "source": {
                    "kind": "schema", "name": "MIGR_SOURCE_A",
                    "safe_pattern": r"^MIGR_.*$",
                },
                "target": {
                    "kind": "schema", "name": "MIGR_TARGET_B",
                    "safe_pattern": r"^MIGR_.*$",
                },
            },
            "owner_provisioning": {
                "source": {"expected_container_name": "SOURCE_PDB_A"},
                "target": {"expected_container_name": "TARGET_PDB_B"},
            },
        },
        "oracle_permission_migration": {
            "enabled": False, "secret_provider": "local_encrypted_file",
            "executor": {"username": "PERMISSION_EXECUTOR", "password_secret": "executor_pass"},
        },
    }
    return E2EConfig(raw)


def _install_fake_connections(
    monkeypatch, cfg, *, source_container="SOURCE_PDB_A", target_errors=(),
    source_errors=(), source_tables=(), source_columns=(), unsupported_source_objects=(),
    source_types=(), source_type_attributes=(),
):
    source_connection = FakeConnection(
        "MIGR_SOURCE_A", source_container, "source_service",
        errors=tuple(source_errors) + (("object_type = 'TYPE BODY'",)
                                      if source_container != "SOURCE_PDB_A" else ()),
        tables=source_tables, columns=source_columns,
        unsupported_objects=unsupported_source_objects,
        types=source_types, type_attributes=source_type_attributes,
    )
    target_connection = FakeConnection(
        "MIGR_TARGET_B", "TARGET_PDB_B", "target_service", errors=target_errors,
    )
    connections = {"MIGR_SOURCE_A": source_connection, "MIGR_TARGET_B": target_connection}
    monkeypatch.setattr(checker, "resolver_for_side", lambda *_args: object())
    monkeypatch.setattr(
        checker,
        "connection_config",
        lambda _cfg, side, owner, _resolver: {
            "username": owner,
            "password": f"secret-for-{side}",
            "service_name": cfg.raw[side]["connection"]["service_name"],
        },
    )
    monkeypatch.setattr(checker, "connect", lambda endpoint, _driver: connections[endpoint["username"]])
    return connections


def test_query_audit_collects_multiple_catalog_errors_and_keeps_scanning():
    raw = FakeConnection("MIGR_SOURCE_A", "SOURCE_PDB_A", "source_service", errors=(
        "FROM user_objects", "FROM user_types",
    ))
    audit = checker.QueryAudit("source")
    audit.stage = "source.types"
    connection = checker.ReadOnlyConnection(raw, audit)

    for statement, should_fail in (
        ("SELECT type_name FROM user_objects", True),
        ("SELECT type_name FROM user_types", True),
        ("SELECT table_name FROM user_tables", False),
    ):
        with connection.cursor() as cursor:
            cursor.execute(statement)
            if should_fail:
                with pytest.raises(FakeOracleError, match="ORA-00904"):
                    cursor.fetchall()
            else:
                cursor.fetchall()

    assert len(audit.failures) == 2
    assert all(item["error_code"] == "ORA-00904" for item in audit.failures)
    assert all(item["stage"] == "source.types" for item in audit.failures)
    assert audit.failures[0]["views"] == ["USER_OBJECTS"]
    assert len(audit.queries) == 1
    assert raw.statements[-1] == "SELECT table_name FROM user_tables"


def test_read_only_proxy_blocks_mutation_and_allows_only_metadata_session_setting():
    raw = FakeConnection("MIGR_SOURCE_A", "SOURCE_PDB_A", "source_service")
    audited = checker.ReadOnlyConnection(raw, checker.QueryAudit("source"))
    with audited.cursor() as cursor:
        with pytest.raises(RuntimeError, match="blocked non-SELECT"):
            cursor.execute("DROP TABLE arbitrary_table")
        with pytest.raises(RuntimeError, match="blocked non-SELECT"):
            cursor.execute("BEGIN EXECUTE IMMEDIATE 'DROP TABLE x'; END;")
        with pytest.raises(RuntimeError, match="FOR UPDATE"):
            cursor.execute("SELECT username FROM user_users FOR UPDATE")
        cursor.execute(
            "BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM("
            "DBMS_METADATA.SESSION_TRANSFORM, 'EMIT_SCHEMA', FALSE); END;"
        )
    assert len(raw.statements) == 1
    assert raw.statements[0].startswith("BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM")


def test_complete_cli_preflight_uses_dynamic_names_and_actual_connector_discovery(
    monkeypatch, capsys,
):
    cfg = _config()
    _install_fake_connections(monkeypatch, cfg)
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())
    exit_code, output, summary, _ = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 1  # empty SOURCE is a replacement-plan blocker
    assert output["status"] == "FAIL"
    assert output["read_only"] is True
    assert output["identity"]["source"]["container_name"] == "SOURCE_PDB_A"
    assert output["identity"]["target"]["container_name"] == "TARGET_PDB_B"
    assert output["target"]["emptiness_verdict"] == "EMPTY"
    assert output["executed_catalog_queries"]["count"] > 10
    assert output["feature_failures"]
    assert summary["target_inventory"]["verdict"] == "EMPTY"
    assert "query_inventory" not in summary
    assert summary["details_file"]
    assert set(summary) == {"status", "blockers", "target_inventory", "details_file"}


def test_cli_completes_successful_metadata_discovery_for_one_table(monkeypatch, capsys):
    cfg = _config()
    source_column = ("ID", "NUMBER", 22, None, None, 10, 0, "N", None, "NO", "NO")
    _install_fake_connections(
        monkeypatch, cfg, source_tables=("APP_SAMPLE",), source_columns=(source_column,),
    )
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())

    exit_code, output, _summary, _ = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 0, output
    assert output["status"] == "PASS"
    assert output["source"]["source.list_objects"]["objects"] == ["APP_SAMPLE"]
    assert output["source"]["source.get_schema[APP_SAMPLE]"]["columns"] == 1
    assert output["target"]["emptiness_verdict"] == "EMPTY"
    assert output["executed_catalog_queries"]["failed_count"] == 0
    assert any("FROM user_tab_cols" in item["statement"] for item in output["query_inventory"])


def test_cli_reports_unsupported_owner_objects_without_skipping_other_discovery(
    monkeypatch, capsys,
):
    cfg = _config()
    connections = _install_fake_connections(
        monkeypatch, cfg, unsupported_source_objects=(("JAVA_ARTIFACT", "JAVA CLASS"),),
    )
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())

    exit_code, output, _summary, _ = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 1
    assert output["source"]["source.replacement_object_support"]["status"] == "FAIL"
    assert "JAVA CLASS JAVA_ARTIFACT" in output["source"]["source.replacement_object_support"]["error"]
    assert "source.types" in output["source"]
    assert connections["MIGR_SOURCE_A"].statements
    type_body_sql = next(
        query["statement"] for query in output["query_inventory"]
        if "object_type = 'TYPE BODY'" in query["statement"]
    )
    assert "SELECT object_name FROM user_objects" in type_body_sql
    assert "TYPE_NAME" not in type_body_sql.upper()
    assert all(
        statement.startswith(("SELECT", "BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM"))
        for connection in connections.values()
        for statement in connection.statements
    )


def test_cli_reports_timezone_alias_transport_blocker_and_continues_target_checks(
    monkeypatch, capsys,
):
    cfg = _config()
    connections = _install_fake_connections(
        monkeypatch, cfg,
        source_types=(("VALUE_T", "VALID", "OBJECT", "NO", "NO"),),
        source_type_attributes=(("VALUE_T", "SYS", "TIMESTAMP WITH TZ", None),),
    )
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())

    exit_code, output, summary, _ = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 1
    assert output["source"]["source.types"]["status"] == "FAIL"
    assert "TIMESTAMP WITH TZ resolves to TIMESTAMP WITH TIME ZONE" in (
        output["source"]["source.types"]["error"]
    )
    assert "naive datetime without the source time-zone displacement" in (
        output["source"]["source.types"]["error"]
    )
    assert output["target"]["target.owner_object_emptiness"]["status"] == "PASS"
    assert any(
        "FROM user_type_attrs ORDER BY type_name, attr_no" in item["statement"]
        for item in output["query_inventory"]
    )
    assert connections["MIGR_SOURCE_A"].statements
    assert summary["status"] == "FAIL"


def test_enabled_permission_catalog_failure_is_reported_once_and_target_inventory_continues(
    monkeypatch, capsys,
):
    cfg = _config()
    cfg.raw["oracle_permission_migration"]["enabled"] = True
    connections = _install_fake_connections(
        monkeypatch, cfg, source_errors=("SYS.DBA_USERS",),
    )
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())

    exit_code, output, _summary, _ = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 1
    assert output["source"]["source.permission_inventory"]["status"] == "FAIL"
    assert output["source"]["source.replacement_grants"]["status"] == "FAIL"
    assert output["source"]["source.replacement_grants"]["covered_by"] == "source.permission_inventory"
    assert output["target"]["target.read_only_inventory"]["object_count"] == 0
    assert output["target"]["target.read_only_inventory"]["inspection_errors"] == []
    assert output["target"]["target.owner_object_emptiness"]["empty"] is True
    source_catalog_probes = [
        sql for sql in connections["MIGR_SOURCE_A"].statements
        if "SELECT 1 FROM SYS.DBA_USERS WHERE 1 = 0" in sql
    ]
    assert len(source_catalog_probes) == 1
    assert not any(
        item["stage"] == "source.replacement_grants"
        for item in output["query_failures"]
    )


def test_cli_stops_before_catalog_scan_on_unexpected_pdb(monkeypatch, capsys):
    cfg = _config()
    connections = _install_fake_connections(monkeypatch, cfg, source_container="UNEXPECTED_PDB")
    with pytest.raises(checker.OracleIdentityError):
        checker.run_compatibility_check(cfg, driver=SimpleNamespace())
    assert len(connections["MIGR_SOURCE_A"].statements) == 1
    assert connections["MIGR_TARGET_B"].statements == []
    assert not any("USER_OBJECTS" in sql.upper() for connection in connections.values() for sql in connection.statements)


def test_cli_never_calls_failed_target_inventory_empty(monkeypatch, capsys):
    cfg = _config()
    _install_fake_connections(monkeypatch, cfg, target_errors=("FROM user_objects",))
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())

    exit_code, output, summary, _ = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 1
    assert output["target"]["emptiness_verdict"] == "UNVERIFIED"
    assert output["target"]["target.owner_object_emptiness"]["status"] == "FAIL"
    assert output["target"]["target.owner_object_emptiness"]["empty"] is None
    assert any(
        item["stage"] == "target.owner_object_emptiness"
        and item["error_code"] == "ORA-00904"
        and "FROM user_objects" in item["statement"]
        for item in output["query_failures"]
    )
    assert any(
        item.get("stage") == "catalog_queries"
        and any(query["view"] == "USER_OBJECTS" and query["code"] == "ORA-00904"
                for query in item["failures"])
        for item in summary["blockers"]
    )


def test_cli_reports_missing_secret_as_credential_resolution_without_values(monkeypatch, capsys):
    cfg = _config()
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setattr(
        checker, "connection_config",
        lambda *_args: (_ for _ in ()).throw(
            OracleE2EError("Oracle source credential is missing")
        ),
    )
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace())

    exit_code, output, summary, _output_text = _run_checker_cli(
        capsys, ["--config", "config/arbitrary_oracle_acceptance.yaml"]
    )

    assert exit_code == 2
    assert summary["status"] == "FAIL"
    assert summary["blockers"] == [{
        "stage": "credential_resolution", "error": "Oracle source credential is missing",
    }]
    assert summary["target_inventory"]["verdict"] == "UNVERIFIED"
    assert "secret-for-source" not in json.dumps(output)


def test_auth_only_uses_independent_env_secrets_and_ignores_executor_provider(
    monkeypatch, capsys,
):
    cfg = _config()
    cfg.raw["oracle_permission_migration"]["enabled"] = True
    source_password = "source-secret-value"
    target_password = "target-secret-value"
    monkeypatch.setenv("SECRET_source_pass", source_password)
    monkeypatch.setenv("SECRET_target_pass", target_password)
    driver = AuthDriver()
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", driver)

    exit_code = checker.main([
        "--config", "config/arbitrary_oracle_acceptance.yaml", "--auth-only",
    ])
    output_text = capsys.readouterr().out
    output = json.loads(output_text)

    assert exit_code == 0
    assert output["status"] == "PASS"
    assert output["sides"]["source"]["provider"] == "env"
    assert output["sides"]["target"]["provider"] == "env"
    assert output["sides"]["source"]["environment_secret_match"] is True
    assert output["sides"]["target"]["environment_secret_match"] is True
    assert output["sides"]["source"]["connect_arguments"]["dsn"] == (
        "127.0.0.1:1521/source_service"
    )
    assert output["sides"]["target"]["connect_arguments"]["dsn"] == (
        "127.0.0.1:1521/target_service"
    )
    assert [call["password"] for call in driver.calls] == [source_password, target_password]
    assert [call["user"] for call in driver.calls] == ["MIGR_SOURCE_A", "MIGR_TARGET_B"]
    assert source_password not in output_text and target_password not in output_text


def test_auth_only_reports_missing_source_env_secret_and_still_checks_target(monkeypatch, capsys):
    cfg = _config()
    monkeypatch.delenv("SECRET_source_pass", raising=False)
    monkeypatch.setenv("SECRET_target_pass", "target-only-secret")
    driver = AuthDriver()
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", driver)

    exit_code = checker.main([
        "--config", "config/arbitrary_oracle_acceptance.yaml", "--auth-only",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["sides"]["source"]["stage"] == "credential_resolution"
    assert output["sides"]["source"]["credential_resolution"] == "FAIL"
    assert output["sides"]["target"]["authentication"] == "PASS"
    assert [call["user"] for call in driver.calls] == ["MIGR_TARGET_B"]


def test_auth_only_reports_auth_failure_without_skipping_target(monkeypatch, capsys):
    cfg = _config()
    monkeypatch.setenv("SECRET_source_pass", "source-only-in-memory")
    monkeypatch.setenv("SECRET_target_pass", "target-only-in-memory")
    driver = AuthDriver(failures={"MIGR_SOURCE_A"})
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(sys.modules, "oracledb", driver)

    exit_code = checker.main([
        "--config", "config/arbitrary_oracle_acceptance.yaml", "--auth-only",
    ])
    output_text = capsys.readouterr().out
    output = json.loads(output_text)

    assert exit_code == 1
    assert output["sides"]["source"]["stage"] == "connection"
    assert output["sides"]["source"]["error"] == "ORA-01017"
    assert output["sides"]["target"]["identity"]["status"] == "PASS"
    assert "source-only-in-memory" not in output_text
    assert "target-only-in-memory" not in output_text


def test_endpoint_provider_override_is_used_for_its_owner_only(monkeypatch):
    cfg = _config()
    cfg.raw["target"]["secret_provider"] = "local_encrypted_file"
    calls = []

    class StubResolver:
        def __init__(self, value):
            self.value = value

        def resolve(self, _secret_ref):
            return self.value

    def factory(_config, provider):
        calls.append(provider)
        return StubResolver(f"resolved-by-{provider}")

    monkeypatch.setattr(oracle_e2e_config, "create_secret_provider", factory)
    source_endpoint = oracle_e2e_config.connection_config(
        cfg, "source", "MIGR_SOURCE_A", oracle_e2e_config.resolver_for_side(cfg.raw, "source"),
    )
    target_endpoint = oracle_e2e_config.connection_config(
        cfg, "target", "MIGR_TARGET_B", oracle_e2e_config.resolver_for_side(cfg.raw, "target"),
    )

    assert calls == ["env", "local_encrypted_file"]
    assert source_endpoint["password"] == "resolved-by-env"
    assert target_endpoint["password"] == "resolved-by-local_encrypted_file"


def test_auth_only_uses_explicit_provider_override_without_environment_comparison(
    monkeypatch, capsys,
):
    cfg = _config()
    cfg.raw["target"]["secret_provider"] = "local_encrypted_file"
    monkeypatch.setenv("SECRET_source_pass", "source-env-secret")

    class StubResolver:
        def __init__(self, value):
            self.value = value

        def resolve(self, _secret_ref):
            return self.value

    def factory(_config, provider):
        return StubResolver(
            "source-env-secret" if provider == "env" else "target-local-secret"
        )

    monkeypatch.setattr(oracle_e2e_config, "create_secret_provider", factory)
    monkeypatch.setattr(checker, "load_e2e_config", lambda _path: cfg)
    driver = AuthDriver()
    monkeypatch.setitem(sys.modules, "oracledb", driver)

    exit_code = checker.main([
        "--config", "config/arbitrary_oracle_acceptance.yaml", "--auth-only",
    ])
    output_text = capsys.readouterr().out
    output = json.loads(output_text)

    assert exit_code == 0
    assert output["sides"]["source"]["provider"] == "env"
    assert output["sides"]["source"]["environment_secret_match"] is True
    assert output["sides"]["target"]["provider"] == "local_encrypted_file"
    assert output["sides"]["target"]["environment_secret_match"] is None
    assert [call["password"] for call in driver.calls] == [
        "source-env-secret", "target-local-secret",
    ]
    assert "source-env-secret" not in output_text
    assert "target-local-secret" not in output_text
