"""Offline safety tests for Oracle E2E owner provisioning and preflight."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle import owner_provisioning as owner_ops
from tests.e2e.oracle import run_preflight as preflight_ops
from tests.unit.oracle_test_settings import load_oracle_test_config


def _cfg():
    raw = load_oracle_test_config()
    raw["e2e"]["owner_provisioning"] = {"enabled": False}
    return E2EConfig(raw)


class _Cursor:
    def __init__(self, responses):
        self.responses = responses
        self.sql = []
        self.current = []

    def execute(self, sql, binds=None):
        self.sql.append(sql)
        self.current = self.responses.get(sql, [])

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Connection:
    def __init__(self, responses):
        self.cursor_obj = _Cursor(responses)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class DatabaseError(Exception):
    """Shape the driver error attribute used by python-oracledb exceptions."""

    def __init__(self, code, message):
        self.args = (SimpleNamespace(code=code, full_code=f"ORA-{code:05d}", message=message),)

    def __str__(self):
        return self.args[0].message


class _DDLConnection(_Connection):
    def __init__(self, *, fail_sql=None, error=None):
        super().__init__({})
        self.fail_sql = fail_sql
        self.error = error

        def execute(sql, binds=None):
            self.cursor_obj.sql.append(sql)
            if self.fail_sql and self.fail_sql in sql:
                raise self.error
            self.cursor_obj.current = []

        self.cursor_obj.execute = execute


def _provision_cfg():
    cfg = _cfg()
    cfg.raw["source"]["connection"]["service_name"] = "SOURCE_SERVICE_X"
    cfg.raw["target"]["connection"]["service_name"] = "TARGET_SERVICE_Y"
    cfg.raw["e2e"]["owner_provisioning"] = {"enabled": True}
    for side, pdb, admin_name, admin_secret in (
        ("source", "SOURCE_PDB_X", "SOURCE_ADMIN_X", "source_admin_secret"),
        ("target", "TARGET_PDB_Y", "TARGET_ADMIN_Y", "target_admin_secret"),
    ):
        cfg.raw["e2e"]["owner_provisioning"][side] = {
            "expected_container_name": pdb,
            "permanent_tablespace": f"APP_{side.upper()}_DATA",
            "quota_bytes": 4096,
            "admin_connection": {"username": admin_name, "password_secret": admin_secret},
        }
    return cfg


def _install_provision_harness(monkeypatch, cfg, connections, owner_states=None, fail_read=None):
    owner_states = owner_states or {"source": None, "target": None}
    by_connection = {connections[side]: side for side in ("source", "target")}
    for connection in connections.values():
        connection.read_queries = []
    monkeypatch.setattr(owner_ops, "connect", Mock(side_effect=[connections["source"], connections["target"]]))
    monkeypatch.setattr(owner_ops, "_check_identity", lambda *_a, **_kw: None)
    monkeypatch.setattr(owner_ops, "_assert_local_admin", lambda *_a, **_kw: None)

    class Resolver:
        def __init__(self, side):
            self.side = side

        def resolve(self, _reference):
            return f"admin-secret-{self.side}"

    monkeypatch.setattr(owner_ops, "resolver_for_side", lambda _raw, side, *_a: Resolver(side))
    monkeypatch.setattr(
        owner_ops,
        "connection_config",
        lambda _cfg, side, _owner, _resolver: {"password": f"owner-secret-{side}"},
    )

    def query(conn, sql, _binds=None):
        side = by_connection[conn]
        state = owner_states.get(side)
        conn.read_queries.append(sql)
        if fail_read == (side, sql):
            raise DatabaseError(942, 'ORA-00942: table or view "SYS"."DBA_TABLESPACES" does not exist')
        if "SELECT account_status, common, default_tablespace FROM dba_users" in sql:
            return [("OPEN", "NO", f"APP_{side.upper()}_DATA")] if state else []
        if "FROM dba_tablespaces" in sql:
            return [("PERMANENT", "ONLINE")]
        if "FROM dba_data_files" in sql:
            return [(1,)]
        if "FROM dba_objects" in sql:
            return [(0,)]
        if "FROM dba_ts_quotas" in sql:
            return [(4096,)]
        if "SELECT privilege FROM dba_sys_privs WHERE grantee" in sql:
            return [(privilege,) for privilege in (state or {}).get("privileges", set())]
        if "SELECT privilege, admin_option FROM user_sys_privs" in sql:
            return [(privilege, "YES") for privilege in owner_ops._fixture_privileges(cfg)]
        if "SELECT privilege FROM session_privs" in sql:
            return [("CREATE USER",)]
        raise AssertionError(f"Unexpected provisioning preflight query: {sql}")

    monkeypatch.setattr(owner_ops, "_query", query)


def test_provision_requires_explicit_apply_before_any_connection(monkeypatch):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(owner_ops, "connect", connect)
    with pytest.raises(ValueError, match="--apply"):
        owner_ops.provision(_cfg(), driver=Mock(), apply=False)
    connect.assert_not_called()


def test_provision_configuration_requires_non_system_permanent_storage():
    cfg = _cfg()
    cfg.raw["e2e"]["owner_provisioning"].update(
        {
            "enabled": True,
            "source": {
                "expected_container_name": "PDB_A",
                "permanent_tablespace": "SYSTEM",
                "quota_bytes": 1024,
                "admin_connection": {"username": "DBA", "password_secret": "dba_secret"},
            },
        }
    )
    with pytest.raises(ValueError, match="non-SYSTEM permanent"):
        owner_ops._side_policy(cfg, "source")


def test_local_os_sysdba_authentication_is_rejected_by_thin_mode_policy():
    cfg = _cfg()
    cfg.raw["e2e"]["owner_provisioning"].update(
        {
            "enabled": True,
            "source": {
                "expected_container_name": "PDB_A",
                "permanent_tablespace": "APP_DATA",
                "quota_bytes": 1024,
                "admin_connection": {
                    "username": "LOCAL_ADMIN",
                    "password_secret": "admin_secret",
                    "auth_mode": "os_sysdba",
                },
            },
        }
    )
    with pytest.raises(ValueError, match="local OS SYSDBA is unsupported"):
        owner_ops._side_policy(cfg, "source")


@pytest.mark.parametrize(("common", "accepted"), [("NO", True), ("YES", False)])
def test_provisioning_admin_must_be_pdb_local(common, accepted):
    conn = _Connection({"SELECT common FROM dba_users WHERE username = :username": [(common,)]})
    if accepted:
        owner_ops._assert_local_admin(conn, username="LOCAL_ADMIN", side="source")
    else:
        with pytest.raises(RuntimeError, match="must be PDB-local"):
            owner_ops._assert_local_admin(conn, username="COMMON_ADMIN", side="target")


def test_provision_rejects_missing_or_unlimited_quota_configuration():
    cfg = _cfg()
    cfg.raw["e2e"]["owner_provisioning"].update(
        {
            "enabled": True,
            "source": {
                "expected_container_name": "PDB_A",
                "permanent_tablespace": "APP_DATA",
                "quota_bytes": 0,
                "admin_connection": {"username": "DBA", "password_secret": "dba_secret"},
            },
        }
    )
    with pytest.raises(ValueError, match="positive finite integer"):
        owner_ops._side_policy(cfg, "source")


def test_fixture_privileges_are_derived_from_configured_sql_and_shared_policy():
    cfg = _cfg()
    cfg.raw["e2e"]["fixtures"] = [
        {"file": "custom_one.sql", "sql": "CREATE TABLE DYNAMIC_NAME (ID NUMBER);"},
        {
            "file": "custom_two.sql",
            "sql": "CREATE OR REPLACE TRIGGER DYNAMIC_TRG BEFORE INSERT ON DYNAMIC_NAME BEGIN NULL; END;",
        },
        {
            "file": "custom_three.sql",
            "sql": "CREATE MATERIALIZED VIEW DYNAMIC_MV AS SELECT ID FROM DYNAMIC_NAME;",
        },
    ]
    assert owner_ops._fixture_privileges(cfg) == {
        "CREATE SESSION",
        "CREATE TABLE",
        "CREATE TRIGGER",
        "CREATE MATERIALIZED VIEW",
    }


def test_preflight_identity_mismatch_stops_before_owner_queries():
    conn = _Connection(
        {
            "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual": [
                ("MIGRATION_E2E_SOURCE", "WRONG_PDB", "SERVICE")
            ],
        }
    )
    with pytest.raises(RuntimeError, match="PDB does not match"):
        owner_ops.preflight_owner(
            conn,
            owner="MIGRATION_E2E_SOURCE",
            service="SERVICE",
            container="EXPECTED_PDB",
            required={"CREATE TABLE"},
            tablespace=None,
            quota_bytes=None,
            require_empty=False,
        )
    assert len(conn.cursor_obj.sql) == 1


def test_preflight_fails_closed_on_missing_privilege_and_empty_target_policy():
    identity_sql = "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
    conn = _Connection(
        {
            identity_sql: [("MIGRATION_E2E_TARGET", "EXPECTED_PDB", "SERVICE")],
            "SELECT COUNT(*) FROM user_objects": [(0,)],
            "SELECT privilege FROM user_sys_privs": [],
            "SELECT privilege FROM session_privs": [],
            "SELECT max_bytes FROM user_ts_quotas": [(2048,)],
        }
    )
    with pytest.raises(RuntimeError, match="lacks required privileges"):
        owner_ops.preflight_owner(
            conn,
            owner="MIGRATION_E2E_TARGET",
            service="SERVICE",
            container="EXPECTED_PDB",
            required={"CREATE TABLE"},
            tablespace=None,
            quota_bytes=None,
            require_empty=True,
        )


def test_preflight_rejects_populated_target_without_mutation():
    identity_sql = "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
    conn = _Connection(
        {
            identity_sql: [("MIGRATION_E2E_TARGET", "EXPECTED_PDB", "SERVICE")],
            "SELECT COUNT(*) FROM user_objects": [(1,)],
        }
    )
    with pytest.raises(RuntimeError, match="not empty"):
        owner_ops.preflight_owner(
            conn,
            owner="MIGRATION_E2E_TARGET",
            service="SERVICE",
            container="EXPECTED_PDB",
            required=set(),
            tablespace=None,
            quota_bytes=None,
            require_empty=True,
        )
    assert all(
        not sql.lstrip().upper().startswith(("CREATE", "GRANT", "DROP"))
        for sql in conn.cursor_obj.sql
    )


def test_preloaded_source_does_not_require_a_storage_quota():
    identity_sql = "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
    conn = _Connection(
        {
            identity_sql: [("CLIENT_EXISTING_SOURCE", "SOURCE_PDB_X", "SOURCE_SERVICE_X")],
            "SELECT COUNT(*) FROM user_objects": [(3,)],
            "SELECT privilege FROM user_sys_privs": [],
            "SELECT privilege FROM session_privs": [],
            "SELECT max_bytes FROM user_ts_quotas": [],
        }
    )
    result = owner_ops.preflight_owner(
        conn,
        owner="CLIENT_EXISTING_SOURCE",
        service="SOURCE_SERVICE_X",
        container="SOURCE_PDB_X",
        required=set(),
        tablespace=None,
        quota_bytes=None,
        require_empty=False,
        require_quota=False,
    )
    assert result["object_count"] == 3
    assert result["quota_ok"] is False


def test_preloaded_preflight_skips_source_quota_but_keeps_empty_target_gate(monkeypatch):
    cfg = _cfg()
    cfg.raw["e2e"]["source_mode"] = "preloaded"
    cfg.raw["e2e"]["fixtures"] = []
    cfg.raw["e2e"]["owner_provisioning"] = {
        "enabled": False,
        "source": {"expected_container_name": "SOURCE_PDB_X"},
        "target": {"expected_container_name": "TARGET_PDB_X"},
    }
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(preflight_ops, "connection_config", lambda *_a, **_k: {})
    monkeypatch.setattr(preflight_ops, "resolver_for_side", lambda *_a, **_k: object())
    monkeypatch.setattr(preflight_ops, "connect", lambda *_a, **_k: Conn())
    calls = []
    monkeypatch.setattr(
        preflight_ops,
        "preflight_owner",
        lambda _conn, **kwargs: calls.append(kwargs) or {"object_count": 0},
    )
    result = preflight_ops.run(cfg, driver=object())
    assert [call["require_quota"] for call in calls] == [False, True]
    assert calls[0]["require_empty"] is False
    assert calls[1]["require_empty"] is True
    assert result["target_empty"] is True


def test_provision_pdb_mismatch_prevents_all_ddl(monkeypatch, oracle_test_secrets):
    cfg = _cfg()
    cfg.raw["e2e"]["owner_provisioning"].update(
        {
            "enabled": True,
            "source": {
                "expected_container_name": "EXPECTED_PDB",
                "permanent_tablespace": "APP_DATA",
                "quota_bytes": 4096,
                "admin_connection": {
                    "username": "PROVISIONER",
                    "password_secret": "oracle_source_test",
                },
            },
            "target": {
                "expected_container_name": "EXPECTED_PDB",
                "permanent_tablespace": "APP_DATA",
                "quota_bytes": 4096,
                "admin_connection": {
                    "username": "PROVISIONER",
                    "password_secret": "oracle_target_test",
                },
            },
        }
    )
    conn = _Connection(
        {
            "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual": [
                ("PROVISIONER", "OTHER_PDB", "SERVICE")
            ],
        }
    )
    monkeypatch.setattr(owner_ops, "connect", Mock(return_value=conn))
    monkeypatch.setattr(
        owner_ops, "_identity", lambda _conn, **_kwargs: ("PROVISIONER", "OTHER_PDB", "SERVICE")
    )
    with pytest.raises(RuntimeError, match="PDB does not match"):
        owner_ops.provision(cfg, driver=Mock(), apply=True)
    assert all(
        not sql.lstrip().upper().startswith(("CREATE", "GRANT")) for sql in conn.cursor_obj.sql
    )


def test_partial_owner_setup_grants_only_missing_privileges_without_resetting_existing_owner(monkeypatch):
    cfg = _cfg()
    cfg.raw["source"]["connection"]["service_name"] = "SOURCE_SERVICE"
    cfg.raw["target"]["connection"]["service_name"] = "TARGET_SERVICE"
    cfg.raw["e2e"]["owner_provisioning"] = {
        "enabled": True,
        "source": {
            "expected_container_name": "SOURCE_PDB",
            "permanent_tablespace": "APP_SOURCE",
            "quota_bytes": 8192,
            "admin_connection": {"username": "SOURCE_ADMIN", "password_secret": "source_admin_secret"},
        },
        "target": {
            "expected_container_name": "TARGET_PDB",
            "permanent_tablespace": "APP_TARGET",
            "quota_bytes": 8192,
            "admin_connection": {"username": "TARGET_ADMIN", "password_secret": "target_admin_secret"},
        },
    }
    source_conn, target_conn = _Connection({}), _Connection({})
    monkeypatch.setattr(owner_ops, "connect", Mock(side_effect=[source_conn, target_conn]))
    monkeypatch.setattr(owner_ops, "_check_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owner_ops, "_assert_local_admin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owner_ops, "resolver_for_side", lambda *_args, **_kwargs: Mock(resolve=Mock(return_value="offline-admin")))
    monkeypatch.setattr(owner_ops, "connection_config", lambda *_args, **_kwargs: {"password": "offline-only"})

    def query(conn, sql, binds=None):
        side = "source" if conn is source_conn else "target"
        if "SELECT account_status, common, default_tablespace FROM dba_users" in sql:
            return [("OPEN", "NO", "APP_SOURCE")] if side == "source" else []
        if "FROM dba_tablespaces" in sql:
            return [("PERMANENT", "ONLINE")]
        if "FROM dba_data_files" in sql:
            return [(1,)]
        if "FROM dba_objects" in sql:
            return [(0,)]
        if "FROM dba_ts_quotas" in sql:
            return [(8192,)]
        if "SELECT privilege FROM dba_sys_privs WHERE grantee" in sql:
            return [("CREATE SESSION",)] if side == "source" else []
        if "SELECT privilege, admin_option FROM user_sys_privs" in sql:
            return [("CREATE SESSION", "YES"), ("CREATE TABLE", "YES")]
        if "SELECT privilege FROM session_privs" in sql:
            return [("CREATE USER",)]
        raise AssertionError(f"Unexpected offline catalog query: {sql}")

    monkeypatch.setattr(owner_ops, "_query", query)
    result = owner_ops.provision(cfg, driver=object(), apply=True)
    source_statements = source_conn.cursor_obj.sql
    target_statements = target_conn.cursor_obj.sql
    assert source_statements == ['GRANT CREATE TABLE TO "MIGRATION_E2E_SOURCE"']
    assert target_statements[0].startswith('CREATE USER "MIGRATION_E2E_TARGET"')
    assert sorted(target_statements[1:]) == [
        'GRANT CREATE SESSION TO "MIGRATION_E2E_TARGET"',
        'GRANT CREATE TABLE TO "MIGRATION_E2E_TARGET"',
    ]
    assert result["created"] == ["MIGRATION_E2E_TARGET"]
    assert result["completed_existing"] == ["MIGRATION_E2E_SOURCE"]
    assert not any("IDENTIFIED BY" in sql for sql in source_statements)


def test_new_owner_create_uses_oracle_byte_quota_syntax_for_both_pdbs(monkeypatch):
    cfg = _provision_cfg()
    connections = {side: _DDLConnection() for side in ("source", "target")}
    _install_provision_harness(monkeypatch, cfg, connections)

    result = owner_ops.provision(cfg, driver=object(), apply=True)

    for side, connection in connections.items():
        create_sql = next(sql for sql in connection.cursor_obj.sql if sql.startswith("CREATE USER"))
        assert f'QUOTA 4096 ON "APP_{side.upper()}_DATA"' in create_sql
        assert "BYTES ON" not in create_sql
        assert connection.cursor_obj.sql[1:] == [
            f'GRANT CREATE SESSION TO "MIGRATION_E2E_{side.upper()}"',
            f'GRANT CREATE TABLE TO "MIGRATION_E2E_{side.upper()}"',
        ]
    assert result["created"] == ["MIGRATION_E2E_SOURCE", "MIGRATION_E2E_TARGET"]


@pytest.mark.parametrize(
    ("fail_side", "fail_sql", "code", "message", "expected_operation"),
    [
        ("source", "CREATE USER", 922, "ORA-00922: invalid option", "CREATE USER MIGRATION_E2E_SOURCE"),
        ("target", "CREATE USER", 922, "ORA-00922: invalid option", "CREATE USER MIGRATION_E2E_TARGET"),
        ("source", "GRANT CREATE TABLE", 1031, "ORA-01031: insufficient privileges", "GRANT CREATE TABLE TO MIGRATION_E2E_SOURCE"),
        ("target", "GRANT CREATE TABLE", 1031, "ORA-01031: insufficient privileges", "GRANT CREATE TABLE TO MIGRATION_E2E_TARGET"),
    ],
)
def test_provisioning_ddl_error_preserves_stage_operation_sql_and_oracle_diagnostic(
    monkeypatch, fail_side, fail_sql, code, message, expected_operation
):
    cfg = _provision_cfg()
    error = DatabaseError(code, f"{message}; echoed owner-secret-{fail_side}")
    connections = {
        side: _DDLConnection(
            fail_sql=fail_sql if side == fail_side else None,
            error=error if side == fail_side else None,
        )
        for side in ("source", "target")
    }
    _install_provision_harness(monkeypatch, cfg, connections)

    with pytest.raises(RuntimeError) as caught:
        owner_ops.provision(cfg, driver=object(), apply=True)

    detail = str(caught.value)
    assert f"stage=provisioning.{fail_side}" in detail
    assert f"operation={expected_operation}" in detail
    assert f"error_code=ORA-{code:05d}" in detail
    assert message in detail
    assert "SQL=" in detail
    assert f"owner-secret-{fail_side}" not in detail
    if fail_sql == "CREATE USER":
        assert 'IDENTIFIED BY "<redacted>"' in detail
        assert "BYTES ON" not in detail
    else:
        assert f"SQL=GRANT CREATE TABLE TO \"MIGRATION_E2E_{fail_side.upper()}\"" in detail
    if fail_side == "target":
        # SOURCE DDL was executed before TARGET failed; Oracle DDL is not rolled back
        # by closing the Python connection, so recovery must inspect that owner.
        assert any(sql.startswith("CREATE USER") for sql in connections["source"].cursor_obj.sql)


@pytest.mark.parametrize("fail_side", ["source", "target"])
def test_partially_provisioned_owner_recovers_only_missing_grant_without_password_reset(
    monkeypatch, fail_side
):
    cfg = _provision_cfg()
    first_connections = {
        side: _DDLConnection(
            fail_sql="GRANT CREATE TABLE" if side == fail_side else None,
            error=DatabaseError(1031, "ORA-01031: insufficient privileges") if side == fail_side else None,
        )
        for side in ("source", "target")
    }
    _install_provision_harness(monkeypatch, cfg, first_connections)
    with pytest.raises(RuntimeError, match="error_code=ORA-01031"):
        owner_ops.provision(cfg, driver=object(), apply=True)
    assert first_connections[fail_side].cursor_obj.sql[-1] == (
        f'GRANT CREATE TABLE TO "MIGRATION_E2E_{fail_side.upper()}"'
    )

    recovered_connections = {side: _DDLConnection() for side in ("source", "target")}
    recovery_state = (
        {"source": {"privileges": {"CREATE SESSION"}}, "target": None}
        if fail_side == "source"
        else {
            "source": {"privileges": {"CREATE SESSION", "CREATE TABLE"}},
            "target": {"privileges": {"CREATE SESSION"}},
        }
    )
    _install_provision_harness(
        monkeypatch,
        cfg,
        recovered_connections,
        owner_states=recovery_state,
    )
    result = owner_ops.provision(cfg, driver=object(), apply=True)

    expected_recovery = f'GRANT CREATE TABLE TO "MIGRATION_E2E_{fail_side.upper()}"'
    assert recovered_connections[fail_side].cursor_obj.sql == [expected_recovery]
    assert not any("IDENTIFIED BY" in sql for sql in recovered_connections[fail_side].cursor_obj.sql)
    if fail_side == "source":
        assert recovered_connections["target"].cursor_obj.sql[0].startswith("CREATE USER")
        assert result["completed_existing"] == ["MIGRATION_E2E_SOURCE"]
        assert result["created"] == ["MIGRATION_E2E_TARGET"]
    else:
        assert recovered_connections["source"].cursor_obj.sql == []
        assert result["completed_existing"] == ["MIGRATION_E2E_TARGET"]
        assert result["created"] == []


@pytest.mark.parametrize("fail_side", ["source", "target"])
def test_provisioning_catalog_read_failure_preserves_stage_oracle_code_and_query(
    monkeypatch, fail_side
):
    cfg = _provision_cfg()
    tablespace_query = "SELECT contents, status FROM dba_tablespaces WHERE tablespace_name = :ts"
    connections = {side: _DDLConnection() for side in ("source", "target")}
    _install_provision_harness(
        monkeypatch,
        cfg,
        connections,
        fail_read=(fail_side, tablespace_query),
    )

    with pytest.raises(RuntimeError) as caught:
        owner_ops.provision(cfg, driver=object(), apply=True)

    detail = str(caught.value)
    assert f"stage=provisioning.{fail_side}" in detail
    assert "operation=read application tablespace" in detail
    assert "error_code=ORA-00942" in detail
    assert "DBA_TABLESPACES" in detail
    assert "ORA-00942:" in detail
    assert "owner-secret-" not in detail
    assert all(not connection.cursor_obj.sql for connection in connections.values())


@pytest.mark.parametrize(
    ("catalog_sql", "operation"),
    [
        (
            "SELECT max_bytes FROM dba_ts_quotas WHERE username = :owner AND tablespace_name = :ts",
            "read existing owner tablespace quota",
        ),
        (
            "SELECT privilege FROM dba_sys_privs WHERE grantee = :owner",
            "read existing owner system privileges",
        ),
    ],
)
@pytest.mark.parametrize("fail_side", ["source", "target"])
def test_partial_recovery_catalog_visibility_failure_is_reported_and_fails_closed(
    monkeypatch, catalog_sql, operation, fail_side
):
    cfg = _provision_cfg()
    connections = {side: _DDLConnection() for side in ("source", "target")}
    _install_provision_harness(
        monkeypatch,
        cfg,
        connections,
        owner_states={fail_side: {"privileges": {"CREATE SESSION"}}},
        fail_read=(fail_side, catalog_sql),
    )

    with pytest.raises(RuntimeError) as caught:
        owner_ops.provision(cfg, driver=object(), apply=True)

    detail = str(caught.value)
    assert f"stage=provisioning.{fail_side}" in detail
    assert f"operation={operation}" in detail
    assert "error_code=ORA-00942" in detail
    assert catalog_sql.split("FROM ", 1)[1].split(" ", 1)[0].upper() in detail.upper()
    assert all(not connection.cursor_obj.sql for connection in connections.values())


def test_missing_owner_path_does_not_require_existing_owner_quota_or_grant_catalog_reads(monkeypatch):
    cfg = _provision_cfg()
    connections = {side: _DDLConnection() for side in ("source", "target")}
    _install_provision_harness(monkeypatch, cfg, connections)

    for connection in connections.values():
        assert not any("dba_ts_quotas" in sql.casefold() for sql in connection.read_queries)
        assert not any("dba_sys_privs" in sql.casefold() for sql in connection.read_queries)
        assert not any("dba_roles" in sql.casefold() for sql in connection.read_queries)


def _storage_config():
    cfg = _cfg()
    cfg.raw["e2e"]["owner_provisioning"].update(
        {
            "enabled": True,
            "source": {
                "expected_container_name": "SOURCE_PDB",
                "permanent_tablespace": "APP_SOURCE",
                "quota_bytes": 8192,
                "admin_connection": {
                    "username": "PROVISIONER",
                    "password_secret": "oracle_source_test",
                },
            },
            "target": {
                "expected_container_name": "TARGET_PDB",
                "permanent_tablespace": "APP_TARGET",
                "quota_bytes": 8192,
                "admin_connection": {
                    "username": "PROVISIONER",
                    "password_secret": "oracle_target_test",
                },
            },
        }
    )
    cfg.raw["e2e"]["storage_bootstrap"] = {
        "enabled": True,
        "source": {"datafile_path": "D:/oracle_data/source01.dbf", "size": "128M"},
        "target": {"datafile_path": "D:/oracle_data/target01.dbf", "size": "256M"},
    }
    return cfg


def test_disabled_storage_bootstrap_is_a_safe_skip_even_for_apply_workflow(monkeypatch):
    cfg = _cfg()
    connect = Mock(side_effect=AssertionError("disabled bootstrap must not connect"))
    monkeypatch.setattr(owner_ops, "connect", connect)
    assert owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=True)["status"] == "SKIP"
    connect.assert_not_called()


@pytest.mark.parametrize(
    ("path", "size", "message"),
    [
        ("relative.dbf", "128M", "absolute"),
        ("D:/approved/file.dbf", "0M", "K/M/G"),
        ("D:/approved/file.dbf", "512", "K/M/G"),
    ],
)
def test_storage_bootstrap_requires_explicit_absolute_path_and_positive_size(path, size, message):
    cfg = _storage_config()
    cfg.raw["e2e"]["storage_bootstrap"]["source"].update({"datafile_path": path, "size": size})
    with pytest.raises(ValueError, match=message):
        owner_ops._storage_policy(cfg, "source")


def test_storage_bootstrap_creates_only_configured_storage_after_both_preflights(
    monkeypatch, oracle_test_secrets
):
    cfg = _storage_config()
    connections = [_Connection({}), _Connection({})]
    statements = []
    for conn in connections:
        original_cursor = conn.cursor_obj
        original_execute = original_cursor.execute

        def record(sql, binds=None, *, _execute=original_execute):
            statements.append(sql)
            _execute(sql, binds)

        original_cursor.execute = record
    monkeypatch.setattr(owner_ops, "connect", Mock(side_effect=connections))
    monkeypatch.setattr(owner_ops, "_check_identity", Mock())

    def query(_conn, sql, _binds=None):
        if "dba_users" in sql:
            return [("NO",)]
        if "dba_tablespaces" in sql:
            return []
        if "dba_data_files" in sql:
            return []
        if "session_privs" in sql:
            return [("CREATE TABLESPACE",)]
        raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(owner_ops, "_query", query)
    result = owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=True)

    assert result == {"status": "PASS", "created_sides": "source,target"}
    create_statements = [sql for sql in statements if sql.startswith("CREATE TABLESPACE")]
    assert create_statements == [
        "CREATE TABLESPACE \"APP_SOURCE\" DATAFILE 'D:/oracle_data/source01.dbf' SIZE 128M PERMANENT ONLINE",
        "CREATE TABLESPACE \"APP_TARGET\" DATAFILE 'D:/oracle_data/target01.dbf' SIZE 256M PERMANENT ONLINE",
    ]
    assert all(conn.closed for conn in connections)


def test_storage_bootstrap_identity_mismatch_prevents_tablespace_ddl(
    monkeypatch, oracle_test_secrets
):
    cfg = _storage_config()
    conn = _Connection({})
    monkeypatch.setattr(owner_ops, "connect", Mock(return_value=conn))
    monkeypatch.setattr(
        owner_ops,
        "_check_identity",
        Mock(
            side_effect=RuntimeError(
                "Oracle connected PDB does not match configured expected container"
            )
        ),
    )
    with pytest.raises(RuntimeError, match="PDB does not match"):
        owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=True)
    assert conn.cursor_obj.sql == []
    assert conn.closed


def test_storage_bootstrap_requires_command_authorization_when_storage_is_missing(
    monkeypatch, oracle_test_secrets
):
    cfg = _storage_config()
    conn = _Connection({})
    monkeypatch.setattr(owner_ops, "connect", Mock(return_value=conn))
    monkeypatch.setattr(owner_ops, "_check_identity", Mock())
    monkeypatch.setattr(
        owner_ops,
        "_query",
        Mock(side_effect=lambda _c, sql, _b=None: [("NO",)] if "dba_users" in sql else []),
    )
    with pytest.raises(RuntimeError, match="rerun the E2E command with --apply"):
        owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=False)
    assert conn.cursor_obj.sql == []
    assert conn.closed


def test_omf_bootstrap_uses_database_managed_file_creation(monkeypatch, oracle_test_secrets):
    cfg = _storage_config()
    for side in ("source", "target"):
        cfg.raw["e2e"]["storage_bootstrap"][side] = {
            "mode": "oracle_managed_files",
            "size": "384M",
        }
    connections = [_Connection({}), _Connection({})]
    statements = []
    for conn in connections:
        original_execute = conn.cursor_obj.execute

        def record(sql, binds=None, *, _execute=original_execute):
            statements.append(sql)
            _execute(sql, binds)

        conn.cursor_obj.execute = record
    monkeypatch.setattr(owner_ops, "connect", Mock(side_effect=connections))
    monkeypatch.setattr(owner_ops, "_check_identity", Mock())

    def query(_conn, sql, _binds=None):
        if "dba_users" in sql:
            return [("NO",)]
        if "dba_tablespaces" in sql or "dba_data_files" in sql:
            return []
        if "v$parameter" in sql:
            return [("<configured OMF destination>",)]
        if "session_privs" in sql:
            return [("CREATE TABLESPACE",)]
        raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(owner_ops, "_query", query)
    result = owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=True)

    assert result["created_sides"] == "source,target"
    assert statements == [
        'CREATE TABLESPACE "APP_SOURCE" DATAFILE SIZE 384M PERMANENT ONLINE',
        'CREATE TABLESPACE "APP_TARGET" DATAFILE SIZE 384M PERMANENT ONLINE',
    ]


def test_omf_bootstrap_stops_when_database_destination_is_empty(monkeypatch, oracle_test_secrets):
    cfg = _storage_config()
    for side in ("source", "target"):
        cfg.raw["e2e"]["storage_bootstrap"][side] = {
            "mode": "oracle_managed_files",
            "size": "384M",
        }
    conn = _Connection({})
    monkeypatch.setattr(owner_ops, "connect", Mock(return_value=conn))
    monkeypatch.setattr(owner_ops, "_check_identity", Mock())

    def query(_conn, sql, _binds=None):
        if "dba_users" in sql:
            return [("NO",)]
        if "dba_tablespaces" in sql or "dba_data_files" in sql or "v$parameter" in sql:
            return []
        raise AssertionError(f"Unexpected query after empty OMF destination: {sql}")

    monkeypatch.setattr(owner_ops, "_query", query)
    with pytest.raises(RuntimeError, match="no configured db_create_file_dest"):
        owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=True)
    assert conn.cursor_obj.sql == []
    assert conn.closed


def test_storage_bootstrap_reuses_approved_existing_tablespaces_without_ddl(
    monkeypatch, oracle_test_secrets
):
    cfg = _storage_config()
    for side in ("source", "target"):
        cfg.raw["e2e"]["storage_bootstrap"][side] = {"mode": "reuse_existing"}
    connections = [_Connection({}), _Connection({})]
    monkeypatch.setattr(owner_ops, "connect", Mock(side_effect=connections))
    monkeypatch.setattr(owner_ops, "_check_identity", Mock())

    def query(_conn, sql, _binds=None):
        if "dba_users" in sql:
            return [("NO",)]
        if "dba_tablespaces" in sql:
            return [("PERMANENT", "ONLINE")]
        if "dba_data_files" in sql:
            return [("/approved/storage/managed.dbf",)]
        raise AssertionError(f"Unexpected query for existing tablespace reuse: {sql}")

    monkeypatch.setattr(owner_ops, "_query", query)
    result = owner_ops.bootstrap_storage(cfg, driver=Mock(), apply_storage=True)

    assert result == {"status": "PASS", "created_sides": "none"}
    assert all(conn.cursor_obj.sql == [] for conn in connections)
    assert all(conn.closed for conn in connections)
