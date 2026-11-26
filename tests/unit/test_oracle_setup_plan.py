from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from migration_platform.oracle_setup_plan import (
    _assert_live_operation_authority,
    _format_plan_console,
    _make_operation,
    _RuntimeSecretOverlay,
    _secret,
    _validate_operation,
    apply_approved_plan,
    build_plan,
    validate_plan_document,
)
from migration_platform.oracle_setup_plan import _run as _run_sql


class _Provider:
    def get_secret(self, name: str) -> str:
        return f"private-value-for-{name}"


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, binds=None):
        normalized = " ".join(sql.split()).upper()
        assert normalized.startswith("SELECT "), f"PLAN attempted non-SELECT: {sql}"
        if "DBA_TAB_PRIVS" in normalized:
            assert ":CATALOG_NAME" in normalized, f"reserved or missing catalog bind: {sql}"
            assert "catalog_name" in (binds or {}), f"catalog bind value missing: {binds}"
        self.conn.statements.append(sql)
        user = self.conn.user.upper()
        if "SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')" in normalized:
            self.rows = [(user, user, "LOCALCDB", "LOCALDB", "LOCALINSTANCE",
                          self.conn.container, self.conn.service, "localhost")]
        elif "FROM DBA_USERS" in normalized:
            requested_value = ((binds or {}).get("username") or (binds or {}).get("1", "")
                               if isinstance(binds, dict) else (binds[0] if binds else ""))
            requested = str(requested_value).upper()
            if self.conn.admin_missing or requested in self.conn.missing_users:
                self.rows = []
            elif requested == "PERM_EXEC":
                self.rows = [("PERM_EXEC",)]
            elif requested in self.conn.target_users:
                self.rows = [(requested,)]
            elif requested in {"SOURCE_OWNER", "TARGET_OWNER", "TARGET_DBA", "SOURCE_DBA"}:
                self.rows = [("OPEN", "APP_DATA", "NO")]
            else:
                self.rows = []
        elif "FROM DBA_ROLES" in normalized:
            requested_value = ((binds or {}).get("role") or (binds or {}).get("1", "")
                               if isinstance(binds, dict) else (binds[0] if binds else ""))
            requested = str(requested_value).upper()
            role = self.conn.target_roles.get(requested)
            self.rows = [role] if role else []
        elif "FROM USER_USERS" in normalized:
            self.rows = [(user, "OPEN", "APP_DATA")]
        elif "FROM USER_TABLESPACES" in normalized or "FROM DBA_TABLESPACES" in normalized:
            self.rows = [("APP_DATA", "ONLINE", "PERMANENT")]
        elif "FROM DATABASE_PROPERTIES" in normalized:
            self.rows = [("APP_DATA",)]
        elif "FROM DBA_FREE_SPACE" in normalized:
            self.rows = [("APP_DATA", 5000000)]
        elif "FROM DBA_TAB_PRIVS" in normalized:
            self.rows = []
        elif "FROM USER_TS_QUOTAS" in normalized:
            self.rows = [] if self.conn.quota_missing else [(1000000,)]
        elif "FROM DBA_TS_QUOTAS" in normalized:
            self.rows = [(1000000,)]
        elif "FROM USER_OBJECTS" in normalized and "GROUP BY" in normalized:
            self.rows = self.conn.objects
        elif "FROM USER_OBJECTS" in normalized:
            self.rows = [(1,)] if self.conn.has_target_objects else []
        elif "FROM USER_SYS_PRIVS" in normalized:
            if "ADMIN_OPTION" in normalized:
                if "PRIVILEGE, ADMIN_OPTION" in normalized:
                    self.rows = [(privilege, "YES" if privilege in self.conn.sys_admin_options else "NO")
                                 for privilege in sorted(self.conn.direct)]
                elif ":PRIV" in normalized:
                    privilege = str((binds or {}).get("priv", "")).upper()
                    self.rows = [("YES",)] if privilege in self.conn.sys_admin_options else []
                else:
                    self.rows = [(privilege,) for privilege in sorted(self.conn.sys_admin_options)]
            else:
                self.rows = [(p,) for p in sorted(self.conn.direct)]
        elif "FROM USER_ROLE_PRIVS" in normalized:
            if "GRANTED_ROLE" in normalized:
                self.rows = [(role,) for role in sorted(self.conn.executor_role_options)]
            else:
                self.rows = [(role, "YES") for role in sorted(self.conn.executor_role_options)]
        elif "FROM SESSION_PRIVS" in normalized:
            self.rows = [(p,) for p in sorted(self.conn.effective)]
        elif "FROM SESSION_ROLES" in normalized:
            self.rows = []
        elif "FROM SYS.DBA_" in normalized or "FROM ALL_" in normalized:
            if self.conn.catalog_unknown:
                raise PermissionError("catalog denied")
            self.rows = []
        else:
            raise AssertionError(f"Unexpected PLAN query: {sql}")
        return self

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, user, *, objects=(), direct=(), effective=(), catalog_unknown=False,
                 admin_missing=False, has_target_objects=False, service=None, quota_missing=False,
                 missing_users=(), target_roles=None, target_users=(), role_admin_options=(),
                 sys_admin_options=()):
        self.user = user
        self.service = service or ("SOURCE_SERVICE" if "SOURCE" in user.upper() else "TARGET_SERVICE")
        self.container = "SOURCE_PDB" if "SOURCE" in user.upper() else "TARGET_PDB"
        self.objects = list(objects)
        self.direct = set(direct)
        self.effective = set(effective)
        self.catalog_unknown = catalog_unknown
        self.admin_missing = admin_missing
        self.has_target_objects = has_target_objects
        self.quota_missing = quota_missing
        self.missing_users = set(missing_users)
        self.target_roles = {str(k).upper(): tuple(v) for k, v in (target_roles or {}).items()}
        self.target_users = {str(item).upper() for item in target_users}
        self.executor_role_options = {str(item).upper() for item in role_admin_options}
        self.sys_admin_options = {str(item).upper() for item in sys_admin_options}
        self.statements = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def _config(*, security=True):
    return {
        "source": {"engine": "oracle", "connection": {
            "host": "localhost", "port": 1521, "service_name": "SOURCE_SERVICE",
            "username": "SOURCE_OWNER", "password_secret": "source_pass",
        }},
        "target": {"engine": "oracle", "connection": {
            "host": "localhost", "port": 1521, "service_name": "TARGET_SERVICE",
            "username": "TARGET_OWNER", "password_secret": "target_pass",
        }},
        "oracle_permission_migration": {
            "enabled": security,
            "executor": {"username": "PERM_EXEC", "password_secret": "executor_pass"},
            "approve_role_creation_admin_option": True,
            "approve_create_session": True,
        },
        "migration": {"oracle_schema_replacement": {"enabled": True}},
        "secrets": {"provider": "env"},
    }


def _run(monkeypatch, config, *, missing=(), wrong_service=(), source_objects=None,
         target_effective=None, catalog_unknown=(), admin_credentials=None, decisions=None,
         confirm=True, provider=None, permission_inventory=None, target_roles=None,
         target_users=(), executor_role_options=(), target_admin_role_options=(),
         target_admin_sys_options=(), executor_direct=None, target_missing_executor=False,
         target_admin_effective=()):
    fake_driver = SimpleNamespace(makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}")
    monkeypatch.setitem(sys.modules, "oracledb", fake_driver)
    connections = []

    def connect(**kwargs):
        user = kwargs["user"].upper()
        side = "source" if user == "SOURCE_OWNER" else "target" if user == "TARGET_OWNER" else "executor"
        actual_user = user
        if side == "source":
            objects = source_objects if source_objects is not None else [("TABLE", 1), ("VIEW", 1)]
            effective = {"CREATE TABLE", "CREATE VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"}
        elif side == "target":
            objects = []
            effective = target_effective if target_effective is not None else {"CREATE TABLE", "CREATE VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"}
        else:
            objects = []
            effective = set(target_admin_effective) if user == "TARGET_DBA" else set()
        direct = (executor_direct if user == "PERM_EXEC" and executor_direct is not None else
                  {"CREATE SESSION", "CREATE USER", "CREATE ROLE", "CREATE TABLE", "CREATE VIEW",
                   "CREATE TRIGGER", "CREATE PROCEDURE", "CREATE MATERIALIZED VIEW"})
        connection = _Connection(
            actual_user,
            objects=objects,
            direct=direct,
            effective=effective,
            catalog_unknown=side in catalog_unknown,
            admin_missing=user in missing,
            has_target_objects=side == "target",
            service=("WRONG_SERVICE" if side in wrong_service else
                     "SOURCE_SERVICE" if side == "source" else "TARGET_SERVICE"),
            target_roles=target_roles if side == "executor" else None,
            target_users=target_users if side == "executor" else (),
            role_admin_options=(target_admin_role_options if user == "TARGET_DBA"
                                else executor_role_options if user == "PERM_EXEC" else ()),
            sys_admin_options=(target_admin_sys_options if user == "TARGET_DBA" else ()),
            missing_users={"PERM_EXEC"} if user == "TARGET_DBA" and target_missing_executor else (),
        )
        connections.append(connection)
        return connection

    provider = provider or _Provider()
    if permission_inventory is not None:
        from core.connectors.oracle import permissions
        monkeypatch.setattr(permissions, "discover", lambda *_args, **_kwargs: permission_inventory)
    result = build_plan(config, provider=provider, connect=connect,
                        admin_credentials=admin_credentials, decisions=decisions)
    if not confirm:
        return result, connections
    confirmed = {side: result["sides"][side]["identity_confirmation"]["current"]
                 for side in ("source", "target")
                 if result["sides"][side].get("identity_confirmation", {}).get("current")}
    result = build_plan(config, provider=provider, connect=connect,
                        admin_credentials=admin_credentials, confirmed_identities=confirmed,
                        decisions=decisions)
    return result, connections


def test_complete_plan_is_no_change_and_never_executes_mutation(monkeypatch):
    plan, connections = _run(monkeypatch, _config(security=False))
    assert plan["summary"] == "NO CHANGES REQUIRED"
    assert plan["operations"] == []
    assert plan["execution"].startswith("NO SQL DDL")
    assert all(conn.closed for conn in connections)
    assert all(stmt.lstrip().upper().startswith("SELECT ") for c in connections for stmt in c.statements)
    assert "private-value" not in json.dumps(plan)


def test_missing_target_privilege_is_derived_from_populated_source(monkeypatch):
    plan, _ = _run(monkeypatch, _config(security=False), target_effective={"CREATE TABLE"})
    view = plan["sides"]["target"]["object_privileges"]["CREATE VIEW"]
    assert view["status"] == "MISSING"
    assert view["proposed_operation"] == 'GRANT CREATE VIEW TO "TARGET_OWNER";'
    assert plan["sides"]["source"]["object_privileges"]["CREATE VIEW"]["status"] == "READY"


def test_missing_owner_is_only_reported_when_admin_catalog_proves_absence(monkeypatch):
    cfg = _config(security=False)
    # Fake setup admin on target service; it sees no DBA_USERS row for the owner.
    fake_driver = SimpleNamespace(makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}")
    monkeypatch.setitem(sys.modules, "oracledb", fake_driver)
    made = []

    def connect(**kwargs):
        user = kwargs["user"].upper()
        conn = _Connection(user, objects=[("TABLE", 1), ("VIEW", 1)],
                           effective=set() if user == "TARGET_DBA" else {"CREATE TABLE", "CREATE VIEW"},
                           admin_missing=(user == "TARGET_DBA"))
        made.append(conn)
        return conn

    # Make the target admin report its endpoint identity, matching configured service.
    class AdminIdentityConnection(_Connection):
        def cursor(self):
            cursor = super().cursor()
            original = cursor.execute
            def execute(sql, binds=None):
                normalized = " ".join(sql.split()).upper()
                if "SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')" in normalized:
                    self.statements.append(sql)
                    cursor.rows = [(self.user, self.user, "LOCALCDB", "LOCALDB", "LOCALINSTANCE",
                                    "TARGET_PDB", "TARGET_SERVICE", "localhost")]
                    return cursor
                return original(sql, binds)
            cursor.execute = execute
            return cursor

    def connect_matching(**kwargs):
        conn = connect(**kwargs)
        if kwargs["user"].upper() == "TARGET_DBA":
            conn.__class__ = AdminIdentityConnection
        return conn

    admins = {"target": {"username": "TARGET_DBA", "password": "runtime-only"}}
    decisions = {"tablespaces": {"target": "APP_DATA"}, "quota_bytes": {"target": 1000000}}
    plan = build_plan(cfg, provider=_Provider(), connect=connect_matching,
                      admin_credentials=admins, decisions=decisions)
    confirmed = {side: plan["sides"][side]["identity_confirmation"]["current"]
                 for side in ("source", "target") if "identity_confirmation" in plan["sides"][side]}
    plan = build_plan(cfg, provider=_Provider(), connect=connect_matching,
                      admin_credentials=admins, decisions=decisions, confirmed_identities=confirmed)
    owner = plan["sides"]["target"]["owner"]
    assert owner["status"] == "MISSING"
    assert "proposed_operation" not in owner
    assert any(op["operation_type"] == "CREATE_OWNER" for op in plan["operations"])
    quota_op = next(op for op in plan["operations"] if op["operation_type"] == "ASSIGN_FINITE_QUOTA")
    assert quota_op["parameters"] == {"tablespace": "APP_DATA", "quota_bytes": 1000000}
    assert plan["sides"]["target"]["storage_discovery"]["default_permanent_tablespace"] == "APP_DATA"
    assert all(sql.lstrip().upper().startswith("SELECT ") for conn in made for sql in conn.statements)
    assert "private-value" not in json.dumps(plan["operations"])
    assert "runtime-only" not in json.dumps(plan)
    assert "private-value" not in json.dumps(plan)


def test_failed_owner_login_is_unknown_not_missing(monkeypatch):
    cfg = _config(security=False)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(makedsn=lambda *args, **kwargs: "dsn"))

    def fail_connect(**_kwargs):
        raise PermissionError("sensitive connection detail")

    plan = build_plan(cfg, provider=_Provider(), connect=fail_connect)
    assert plan["sides"]["source"]["owner"]["status"] == "UNKNOWN"
    assert "sensitive connection detail" not in json.dumps(plan)
    assert plan["sides"]["source"]["identity"]["status"] == "UNKNOWN"


def test_wrong_owner_identity_blocks_inspection(monkeypatch):
    plan, connections = _run(monkeypatch, _config(security=False), wrong_service={"source"})
    assert plan["sides"]["source"]["identity"]["status"] == "BLOCKED"
    assert plan["sides"]["source"]["object_privileges"] == {}
    assert all(all(s.lstrip().upper().startswith("SELECT ") for s in c.statements) for c in connections)


def test_permission_approval_flags_are_reported_as_policy_not_apply_consent(monkeypatch):
    plan, _ = _run(monkeypatch, _config())
    assert plan["approval_policy"]["approve_role_creation_admin_option"] is True
    assert plan["approval_policy"]["approve_create_session"] is True
    assert "do not authorize" in plan["approval_policy"]["warning"]


def test_missing_quota_blocks_existing_owner_without_planning_change(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(makedsn=lambda *a, **k: "dsn"))

    def connect(**kwargs):
        user = kwargs["user"].upper()
        side = "SOURCE" if user == "SOURCE_OWNER" else "TARGET"
        return _Connection(user, objects=[("TABLE", 1)] if side == "SOURCE" else [],
                           effective={"CREATE TABLE"}, service=f"{side}_SERVICE",
                           quota_missing=side == "TARGET")

    plan = build_plan(_config(security=False), provider=_Provider(), connect=connect)
    quota = plan["sides"]["target"]["quota"]
    assert quota["status"] == "BLOCKED"
    assert "proposed_operation" not in quota
    assert plan["sides"]["target"]["tablespace"]["status"] == "READY"


def test_catalog_access_failure_is_unknown_not_proven_missing(monkeypatch):
    plan, _ = _run(monkeypatch, _config(), catalog_unknown={"source"})
    item = plan["sides"]["source"]["catalog_visibility"]["DBA_USERS"]
    assert item["status"] == "UNKNOWN"
    assert "proposed_operation" not in item


def test_catalog_probe_reports_sanitized_oracle_error_code():
    from types import SimpleNamespace

    from migration_platform.oracle_setup_plan import _catalog_probe

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, *_args):
            error = SimpleNamespace(code=942, message="ORA-00942: table or view does not exist")
            raise RuntimeError(error)

    class Connection:
        def cursor(self): return Cursor()

    probe = _catalog_probe(Connection(), "DBA_USERS")
    assert probe["readable"] is None
    assert probe["error_code"] == "ORA-00942"
    assert "ORA-00942" in probe["reason"]
    assert "SELECT" not in probe["reason"]


def test_plan_counts_unverified_catalogs_separately():
    plan = {"sides": {
        "source": {"identity": {"current": "{}"}, "owner": {"current": "SOURCE_OWNER"},
                   "tablespace": {"current": "USERS"}, "quota": {"current": "READY"},
                   "object_privileges": {}, "catalog_visibility": {
                       "DBA_USERS": {"status": "UNKNOWN"},
                       "DBA_ROLES": {"status": "MISSING"}}},
        "target": {"identity": {"current": "{}"}, "owner": {"current": "TARGET_OWNER"},
                   "tablespace": {"current": "USERS"}, "quota": {"current": "READY"},
                   "object_privileges": {}, "catalog_visibility": {}},
    }, "permission_executor": {"account": {}, "catalog_visibility": {}}}
    rendered = _format_plan_console(plan)
    assert "Missing catalog grants: 1" in rendered
    assert "Unverified catalog grants: 1" in rendered


def test_privileged_read_only_inspection_turns_verified_missing_source_grant_into_operation(monkeypatch):
    config = _config(security=True)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(
        SYSDBA="SYSDBA", makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}"))
    connections = []

    class AdminConnection(_Connection):
        def cursor(self):
            cursor = super().cursor()
            original = cursor.execute
            def execute(sql, binds=None):
                normalized = " ".join(sql.split()).upper()
                if self.user == "SOURCE_DBA" and "FROM DBA_TAB_PRIVS" in normalized:
                    raise PermissionError("ORA-01031: insufficient privileges")
                if getattr(self, "is_sysdba", False) and "SYS_CONTEXT('USERENV','ISDBA')" in normalized:
                    self.statements.append(sql)
                    cursor.rows = [("TRUE",)]
                    return cursor
                return original(sql, binds)
            cursor.execute = execute
            return cursor

    def connect(**kwargs):
        user = kwargs["user"].upper()
        service = kwargs["dsn"].rsplit("/", 1)[-1]
        is_sysdba = kwargs.get("mode") == "SYSDBA"
        if user == "SOURCE_OWNER":
            conn = _Connection(user, objects=[("TABLE", 1)], effective={"CREATE TABLE"},
                               catalog_unknown=True, service=service)
        elif user == "TARGET_OWNER":
            conn = _Connection(user, objects=[], effective={"CREATE TABLE"}, service=service)
        elif user == "SOURCE_DBA":
            conn = AdminConnection(user, objects=[], direct={"CREATE SESSION"},
                effective={"CREATE USER", "ALTER USER", "GRANT ANY PRIVILEGE"}, service=service)
            conn.container = "SOURCE_PDB"
        elif user == "TARGET_DBA":
            conn = AdminConnection(user, objects=[], direct={"CREATE SESSION"},
                effective={"CREATE USER", "ALTER USER", "GRANT ANY PRIVILEGE"}, service=service)
            conn.container = "TARGET_PDB"
        elif is_sysdba:
            conn = AdminConnection("SOURCE_DBA", objects=[], service=service)
            conn.is_sysdba = True
            conn.container = "SOURCE_PDB"
        else:
            conn = _Connection(user, objects=[], service=service)
        # Owner and admin sessions use the same configured endpoint identities.
        if user == "SOURCE_OWNER": conn.container = "SOURCE_PDB"
        if user == "TARGET_OWNER": conn.container = "TARGET_PDB"
        connections.append(conn)
        return conn

    admins = {
        "source": {"username": "SOURCE_DBA", "password": "hidden-source-admin"},
        "target": {"username": "TARGET_DBA", "password": "hidden-target-admin"},
    }
    first = build_plan(config, provider=_Provider(), connect=connect, admin_credentials=admins,
        privileged_credentials={"source": {"username": "SYS", "password": "hidden-sysdba"}})
    confirmations = {side: first["sides"][side]["identity_confirmation"]["current"]
                     for side in ("source", "target")}
    plan = build_plan(config, provider=_Provider(), connect=connect, admin_credentials=admins,
        privileged_credentials={"source": {"username": "SYS", "password": "hidden-sysdba"}},
        confirmed_identities=confirmations)
    inspection = plan["privileged_read_only_inspection"]["source"]
    assert inspection["status"] == "READY", inspection
    assert inspection["direct_select_grants"]["SOURCE_OWNER"]["DBA_USERS"] is False
    assert plan["sides"]["source"]["catalog_visibility"]["DBA_USERS"]["status"] == "MISSING"
    assert any(op["scope"] == "SOURCE" and op["operation_type"] == "GRANT_CATALOG_ACCESS"
               and op["parameters"]["view"] == "DBA_USERS" for op in plan["operations"])
    assert all(statement.lstrip().upper().startswith("SELECT ")
               for connection in connections for statement in connection.statements)
    assert "hidden-sysdba" not in json.dumps(plan)


def test_read_only_cli_prompts_for_existing_owner_secret_without_environment(
        monkeypatch, tmp_path, capsys):
    import os

    import migration_platform.oracle_setup_plan as setup

    config = _config(security=False)
    path = tmp_path / "oracle-plan.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.delenv("SECRET_source_pass", raising=False)
    tokens = {"source": "source-confirm", "target": "target-confirm"}
    builds = []

    def fake_build(_config, *, provider, admin_credentials=None, privileged_credentials=None,
                   confirmed_identities=None, decisions=None):
        admins = admin_credentials or {}
        confirmed = confirmed_identities or {}
        source_secret = _secret(provider, "source_pass", "source")[1] == "AVAILABLE"
        builds.append((set(admins), source_secret))
        sides = {}
        for side in ("source", "target"):
            identity = {"db_unique_name": side.upper(), "container_name": f"{side.upper()}_PDB",
                        "service_name": f"{side.upper()}_SERVICE"}
            discovered = "READY" if side == "source" and "source" in admins else (
                "MISSING" if "target" in admins else "UNKNOWN")
            sides[side] = {
                "identity": {"status": "READY", "current": json.dumps(identity)},
                "identity_confirmation": {"status": "READY" if side in confirmed else "UNKNOWN",
                    "current": tokens[side]},
                "owner": {"status": "READY" if side == "source" and source_secret else
                          "MISSING" if discovered == "MISSING" else "UNKNOWN",
                          "current": "existing" if source_secret else "runtime owner secret unavailable"},
                "account_discovery": {"status": discovered},
                "tablespace": {"status": "READY", "current": "USERS"},
                "quota": {"status": "READY", "current": "finite"},
                "object_privileges": {}, "catalog_visibility": {},
            }
        ready = (source_secret and set(admins) == {"source", "target"}
                 and set(confirmed) == {"source", "target"})
        return {"schema": "oracle-setup-plan/v1", "sides": sides,
                "permission_executor": {"account": {"status": "NOT APPLICABLE"},
                    "account_discovery": {"status": "NOT APPLICABLE"},
                    "catalog_visibility": {}},
                "operations": [], "blockers": [], "approval_ready": ready,
                "plan_status": "NO CHANGES REQUIRED" if ready else "BLOCKED / UNKNOWN REQUIREMENTS PRESENT",
                "fingerprint": "a" * 64}

    monkeypatch.setattr(setup, "build_plan", fake_build)
    answers = iter(["SOURCE_ADMIN", "TARGET_ADMIN", tokens["source"], tokens["target"]])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    prompts = []
    def hidden(prompt):
        prompts.append(prompt)
        return "source-owner-current-password"
    monkeypatch.setattr("getpass.getpass", hidden)
    assert setup.main(["--config", str(path)]) == 0
    assert any("Existing SOURCE owner password" in prompt for prompt in prompts)
    assert all("TARGET owner password" not in prompt and "executor password" not in prompt
               for prompt in prompts)
    assert any(secret_available for _admins, secret_available in builds)
    output = capsys.readouterr().out
    assert "source-owner-current-password" not in output
    assert os.environ.get("SECRET_source_pass") is None


def test_populated_source_and_no_fixture_config_are_supported_and_repeatable(monkeypatch):
    cfg = _config(security=False)
    assert "e2e" not in cfg
    first, _ = _run(monkeypatch, cfg)
    second, _ = _run(monkeypatch, cfg)
    assert first == second
    assert first["sides"]["source"]["owner"]["status"] == "READY"
    assert first["sides"]["source"]["object_privileges"]


def test_runtime_admin_with_setup_authority_is_allowed_for_select_only_inspection(monkeypatch):
    cfg = _config(security=False)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(makedsn=lambda *a, **k: "dsn"))

    statements = []
    def connect(**kwargs):
        user = kwargs["user"].upper()
        conn = _Connection(user, objects=[("TABLE", 1)] if user == "SOURCE_OWNER" else [],
                           direct={"CREATE SESSION"}, effective={"CREATE TABLE"},
                           service="SOURCE_SERVICE" if "SOURCE" in user else "TARGET_SERVICE")
        if user == "TARGET_DBA":
            conn.effective.add("CREATE USER")
        statements.append(conn)
        return conn

    plan = build_plan(cfg, provider=_Provider(), connect=connect,
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}})
    assert plan["sides"]["target"]["administrator_inspection"]["status"] == "READY"
    assert "broad DBA authority" not in json.dumps(plan)
    assert all(sql.lstrip().upper().startswith("SELECT ") for conn in statements for sql in conn.statements)


def test_missing_executor_is_confirmed_by_target_catalog_reader(monkeypatch):
    cfg = _config(security=True)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(makedsn=lambda *a, **k: "dsn"))

    class AdminConn(_Connection):
        def cursor(self):
            cursor = super().cursor()
            original = cursor.execute
            def execute(sql, binds=None):
                if "SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')" in " ".join(sql.split()).upper():
                    self.statements.append(sql)
                    cursor.rows = [(self.user, self.user, "LOCALCDB", "LOCALDB", "LOCALINSTANCE",
                                    "TARGET_PDB", "TARGET_SERVICE", "localhost")]
                    return cursor
                return original(sql, binds)
            cursor.execute = execute
            return cursor

    def connect(**kwargs):
        user = kwargs["user"].upper()
        side = "SOURCE" if user == "SOURCE_OWNER" else "TARGET"
        cls = AdminConn if user == "TARGET_DBA" else _Connection
        return cls(user, objects=[("TABLE", 1), ("VIEW", 1)] if side == "SOURCE" else [],
                   direct={"CREATE SESSION"}, effective=set() if user == "TARGET_DBA" else {"CREATE TABLE", "CREATE VIEW"},
                   service=f"{side}_SERVICE", missing_users={"PERM_EXEC"} if user == "TARGET_DBA" else ())

    plan = build_plan(cfg, provider=_Provider(), connect=connect,
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}})
    executor = plan["permission_executor"]["account"]
    assert executor["status"] == "MISSING", json.dumps(plan, indent=2)
    assert any(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"])
    assert "private-value" not in json.dumps(plan)


def test_existing_executor_available_secret_is_verified_without_recreation(monkeypatch):
    config = _config(security=True)
    plan, _ = _run(monkeypatch, config, source_objects=[], confirm=True)
    # The regular fake catalog reader reports the configured executor as an
    # existing account; its available runtime password must authenticate.
    assert plan["permission_executor"]["account"]["status"] == "READY"
    assert not any(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"])
    assert "private-value-for-executor_pass" not in json.dumps(plan)


def test_existing_executor_authentication_failure_is_not_treated_as_absent(monkeypatch):
    config = _config(security=True)
    admins = {"target": {"username": "TARGET_DBA", "password": "runtime-only"}}
    first, _ = _run(monkeypatch, config, source_objects=[], admin_credentials=admins)
    confirmations = {side: first["sides"][side]["identity_confirmation"]["current"]
                     for side in ("source", "target")}

    def connect(**kwargs):
        if kwargs["user"].upper() == "PERM_EXEC":
            raise RuntimeError("ORA-01017: invalid username/password")
        user = kwargs["user"].upper()
        return _Connection(user,
            objects=[],
            direct={"CREATE SESSION", "CREATE USER", "CREATE ROLE", "CREATE TABLE", "CREATE VIEW",
                    "CREATE TRIGGER", "CREATE PROCEDURE", "CREATE MATERIALIZED VIEW"},
            effective={"CREATE TABLE", "CREATE VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"},
            service="SOURCE_SERVICE" if user == "SOURCE_OWNER" else "TARGET_SERVICE")

    plan = build_plan(config, provider=_Provider(), connect=connect, admin_credentials=admins,
                      confirmed_identities=confirmations)
    executor = plan["permission_executor"]["account"]
    assert executor["status"] == "UNKNOWN"
    assert "ORA-01017" in executor["current"]
    assert not any(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"])


def test_existing_executor_with_missing_secret_is_blocked_without_password_reset(monkeypatch):
    class MissingExecutorSecret:
        def get_secret(self, name):
            if name == "executor_pass":
                raise KeyError(name)
            return f"private-value-for-{name}"

    admins = {"target": {"username": "TARGET_DBA", "password": "runtime-only"}}
    plan, _ = _run(monkeypatch, _config(security=True), source_objects=[],
                   admin_credentials=admins, provider=MissingExecutorSecret())
    executor = plan["permission_executor"]
    assert executor["account"]["status"] == "BLOCKED"
    assert "password secret is unavailable" in executor["account"]["current"]
    assert not any(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"])
    assert not any(op["account"] == "PERM_EXEC" for op in plan["operations"])


def _mapped_security_inventory(*, roles=(), users=("SRC_USER",), include_session=True):
    from core.connectors.base import GrantDef, RoleMembershipDef

    role_mappings = {role: f"DST_{role}" for role in roles}
    user_mappings = {user: f"DST_{user}" for user in users}
    grants = ([GrantDef("CREATE SESSION", "SYSTEM", "", users[0], "SOURCE_OWNER")]
              if include_session and users else [])
    memberships = [RoleMembershipDef(users[0], role) for role in roles] if users else []
    return {
        "user_mappings": user_mappings,
        "role_mappings": role_mappings,
        "roles": [SimpleNamespace(name=role) for role in roles],
        "memberships": memberships,
        "grants": grants,
    }


def test_missing_executor_authority_is_remediable_not_a_plan_blocker(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",))
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        executor_direct=set(),
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}},
        target_admin_sys_options={"CREATE USER", "CREATE ROLE", "CREATE SESSION", "CREATE TABLE",
                                  "CREATE VIEW", "CREATE TRIGGER", "CREATE PROCEDURE",
                                  "CREATE MATERIALIZED VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"},
    )

    executor = plan["permission_executor"]
    assert plan["approval_ready"] is True, json.dumps({
        "approval_policy": plan["approval_policy"],
        "executor_authority": executor["authority"],
        "operations": [(op["operation_type"], op["parameters"], op["authority"])
                       for op in plan["operations"]],
        "blockers": plan["blockers"],
    }, indent=2)
    assert executor["authority"]["CREATE USER"]["status"] == "MISSING BUT REMEDIABLE"
    assert executor["authority"]["CREATE ROLE"]["status"] == "MISSING BUT REMEDIABLE"
    assert executor["authority"]["CREATE SESSION"]["status"] == "MISSING BUT REMEDIABLE"
    assert {op["parameters"].get("privilege") for op in plan["operations"]
            if op["operation_type"] in {"GRANT_SYSTEM_PRIVILEGE", "GRANT_APPROVED_ADMIN_OPTION"}} == {
                "CREATE USER", "CREATE ROLE", "CREATE SESSION"
            }
    assert not any(blocker["code"] == "CONDITIONAL_ROLE_ADMIN_OPTION"
                   for blocker in plan["blockers"])


def test_fresh_executor_gets_only_inventory_driven_missing_authority(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",))
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory, target_missing_executor=True,
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}},
        target_admin_sys_options={"CREATE USER", "CREATE ROLE", "CREATE SESSION", "CREATE TABLE",
                                  "CREATE VIEW", "CREATE TRIGGER", "CREATE PROCEDURE",
                                  "CREATE MATERIALIZED VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"},
        target_admin_effective={"CREATE USER"},
    )

    executor = plan["permission_executor"]
    assert plan["approval_ready"] is True, json.dumps({
        "executor": executor, "operations": plan["operations"], "blockers": plan["blockers"]
    }, indent=2)
    assert executor["account"]["status"] == "MISSING"
    assert {privilege: executor["authority"][privilege]["status"] for privilege in
            ("CREATE USER", "CREATE ROLE", "CREATE SESSION")} == {
                "CREATE USER": "MISSING BUT REMEDIABLE",
                "CREATE ROLE": "MISSING BUT REMEDIABLE",
                "CREATE SESSION": "MISSING BUT REMEDIABLE",
            }
    assert sum(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"]) == 1
    assert sum(op["operation_type"] == "GRANT_APPROVED_ADMIN_OPTION"
               and op["parameters"].get("privilege") == "CREATE SESSION"
               for op in plan["operations"]) == 1
    assert sum(op["operation_type"] == "GRANT_SYSTEM_PRIVILEGE"
               and op["parameters"].get("privilege") == "CREATE SESSION"
               for op in plan["operations"]) == 0


def test_missing_target_role_is_deferred_to_executor_full_creation(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",))
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        target_admin_sys_options={"CREATE SESSION"},
    )

    role_state = plan["conditional_security_prerequisites"]["role_admin_option"][0]
    assert role_state["target_role"] == "DST_SRC_ROLE"
    assert role_state["status"] == "MISSING BUT REMEDIABLE"
    assert "creates it during FULL" in role_state["reason"]
    assert not any(op.get("parameters", {}).get("role") == "DST_SRC_ROLE"
                   for op in plan["operations"])
    assert not any(blocker["code"] == "CONDITIONAL_ROLE_ADMIN_OPTION"
                   for blocker in plan["blockers"])


def test_existing_target_role_without_executor_admin_option_gets_exact_operation(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",), include_session=False)
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        target_roles={"DST_SRC_ROLE": ("DST_SRC_ROLE", "N", "NO", "NONE")},
        target_admin_role_options={"DST_SRC_ROLE"},
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}},
        target_admin_sys_options={"CREATE USER", "CREATE ROLE", "CREATE SESSION", "CREATE TABLE",
                                  "CREATE VIEW", "CREATE TRIGGER", "CREATE PROCEDURE",
                                  "CREATE MATERIALIZED VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"},
    )

    role_state = plan["conditional_security_prerequisites"]["role_admin_option"][0]
    role_ops = [op for op in plan["operations"]
                if op["operation_type"] == "GRANT_APPROVED_ADMIN_OPTION"
                and op["parameters"].get("role") == "DST_SRC_ROLE"]
    assert plan["approval_ready"] is True, json.dumps(plan, indent=2)
    assert role_state["status"] == "MISSING BUT REMEDIABLE"
    assert len(role_ops) == 1
    assert role_ops[0]["preview_sql"] == 'GRANT "DST_SRC_ROLE" TO "PERM_EXEC" WITH ADMIN OPTION;'
    assert role_ops[0]["authority"]["exact_execution_authority_verified"] is True


def test_existing_target_role_with_executor_admin_option_needs_no_role_grant(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",), include_session=False)
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        target_roles={"DST_SRC_ROLE": ("DST_SRC_ROLE", "N", "NO", "NONE")},
        executor_role_options={"DST_SRC_ROLE"},
    )

    role_state = plan["conditional_security_prerequisites"]["role_admin_option"][0]
    assert role_state["status"] == "READY"
    assert not any(op.get("parameters", {}).get("role") == "DST_SRC_ROLE"
                   for op in plan["operations"])


def test_mixed_existing_and_missing_roles_are_planned_independently(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE_A", "SRC_ROLE_B"), include_session=False)
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        target_roles={"DST_SRC_ROLE_A": ("DST_SRC_ROLE_A", "N", "NO", "NONE")},
        target_admin_role_options={"DST_SRC_ROLE_A"},
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}},
        target_admin_sys_options={"CREATE USER", "CREATE ROLE", "CREATE SESSION", "CREATE TABLE",
                                  "CREATE VIEW", "CREATE TRIGGER", "CREATE PROCEDURE",
                                  "CREATE MATERIALIZED VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"},
    )

    statuses = {item["target_role"]: item["status"]
                for item in plan["conditional_security_prerequisites"]["role_admin_option"]}
    assert statuses == {
        "DST_SRC_ROLE_A": "MISSING BUT REMEDIABLE",
        "DST_SRC_ROLE_B": "MISSING BUT REMEDIABLE",
    }
    role_ops = [op["parameters"]["role"] for op in plan["operations"]
                if op["operation_type"] == "GRANT_APPROVED_ADMIN_OPTION"
                and "role" in op["parameters"]]
    assert role_ops == ["DST_SRC_ROLE_A"]
    assert plan["approval_ready"] is True, json.dumps(plan, indent=2)


def test_existing_role_admin_option_requires_exact_admin_authority(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",), include_session=False)
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        target_roles={"DST_SRC_ROLE": ("DST_SRC_ROLE", "N", "NO", "NONE")},
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "runtime-only"}},
        target_admin_sys_options={"CREATE USER", "CREATE ROLE", "CREATE SESSION", "CREATE TABLE",
                                  "CREATE VIEW", "CREATE TRIGGER", "CREATE PROCEDURE",
                                  "CREATE MATERIALIZED VIEW", "CREATE SEQUENCE", "CREATE SYNONYM", "CREATE TYPE"},
    )

    assert plan["approval_ready"] is False
    assert any(blocker["code"] == "TARGET_EXACT_AUTHORITY_MISSING"
               for blocker in plan["blockers"])
    assert any(op.get("parameters", {}).get("role") == "DST_SRC_ROLE"
               for op in plan["operations"])


def test_existing_unsupported_target_role_is_blocked_without_replacement(monkeypatch):
    inventory = _mapped_security_inventory(roles=("SRC_ROLE",), include_session=False)
    plan, _ = _run(
        monkeypatch, _config(), permission_inventory=inventory,
        target_roles={"DST_SRC_ROLE": ("DST_SRC_ROLE", "N", "NO", "PASSWORD")},
        target_admin_role_options={"DST_SRC_ROLE"},
    )

    assert plan["approval_ready"] is False
    assert any(blocker["code"] == "UNSUPPORTED_TARGET_ROLE" for blocker in plan["blockers"])
    assert not any(op.get("parameters", {}).get("role") == "DST_SRC_ROLE"
                   for op in plan["operations"])


def test_structured_operation_validation_rejects_arbitrary_sql_and_bad_quota():
    op = _make_operation(operation_type="ASSIGN_FINITE_QUOTA", side="target", identity={},
        service="TARGET_SERVICE", account="TARGET_OWNER", current={}, desired={},
        parameters={"tablespace": "APP_DATA", "quota_bytes": 1024}, authority={},
        verification={"query": "USER_TS_QUOTAS"})
    op["parameters"]["sql"] = "DROP USER TARGET_OWNER"
    try:
        _validate_operation(op)
        raise AssertionError("arbitrary SQL unexpectedly accepted")
    except ValueError:
        pass


def test_plan_fingerprint_is_deterministic_and_changes_with_scope(monkeypatch):
    config = _config(security=False)
    first, _ = _run(monkeypatch, config)
    second, _ = _run(monkeypatch, config)
    assert first["fingerprint"] == second["fingerprint"]
    config["target"]["connection"]["host"] = "different-host"
    changed, _ = _run(monkeypatch, config)
    assert changed["fingerprint"] != first["fingerprint"]
    validate_plan_document(changed)


def test_empty_source_gets_future_capability_requirements(monkeypatch):
    plan, _ = _run(monkeypatch, _config(security=False), source_objects=[])
    source = plan["sides"]["source"]
    assert source["observed_object_privileges"] == {}
    assert source["approved_capability_privileges"]["CREATE TABLE"] is True
    assert source["approved_capability_privileges"]["CREATE VIEW"] is False
    assert source["object_privileges"]["CREATE TABLE"]["status"] == "READY"
    assert set(plan["supported_object_scope"]) == {
        "TABLE", "VIEW", "SEQUENCE", "TRIGGER", "FUNCTION", "PROCEDURE",
        "PACKAGE", "SYNONYM", "TYPE", "MATERIALIZED VIEW",
    }


def test_operations_without_verified_setup_admin_are_not_approval_ready(monkeypatch):
    plan, _ = _run(monkeypatch, _config(security=False), target_effective={"CREATE TABLE"})
    assert plan["operations"]
    assert all("operation_type" in op and "parameters" in op for op in plan["operations"])
    assert plan["approval_ready"] is False
    assert any(blocker["code"].endswith("ADMIN_REQUIRED") for blocker in plan["blockers"])


def test_plan_export_round_trip_and_secret_values_absent(monkeypatch):
    plan, _ = _run(monkeypatch, _config(security=False))
    validate_plan_document(json.loads(json.dumps(plan)))
    encoded = json.dumps(plan)
    assert "private-value-for" not in encoded
    assert "source_pass" in encoded  # reference only


def test_normal_profile_needs_no_oracle_setup_section():
    config = yaml.safe_load(Path("config/oracle_local_to_local.yaml").read_text(encoding="utf-8"))
    assert "oracle_setup" not in config
    assert config["migration"]["mode"] == "full"
    assert config["migration"]["oracle_schema_replacement"]["enabled"] is True
    assert config["oracle_permission_migration"]["enabled"] is True
    assert config["oracle_permission_migration"]["approve_role_creation_admin_option"] is True
    assert config["oracle_permission_migration"]["approve_create_session"] is True


def test_discovered_identity_is_not_confirmed_implicitly(monkeypatch):
    plan, _ = _run(monkeypatch, _config(security=False), confirm=False)
    identity = plan["sides"]["source"]["identity"]
    confirmation = plan["sides"]["source"]["identity_confirmation"]
    assert identity["status"] == "READY"
    assert json.loads(identity["current"])["container_name"] == "SOURCE_PDB"
    assert confirmation["status"] == "UNKNOWN"
    assert confirmation["current"]
    assert plan["approval_ready"] is False


def test_unsafe_storage_choice_and_quota_exceeding_free_space_are_blocked(monkeypatch):
    admins = {"target": {"username": "TARGET_DBA", "password": "runtime-only"}}
    plan, _ = _run(monkeypatch, _config(security=False), missing={"TARGET_DBA"},
        admin_credentials=admins,
        decisions={"tablespaces": {"target": "SYSTEM"}, "quota_bytes": {"target": 6000000}})
    assert plan["sides"]["target"]["owner"]["status"] == "MISSING"
    assert plan["sides"]["target"]["tablespace"]["status"] == "UNKNOWN"
    assert plan["sides"]["target"]["quota"]["status"] == "UNKNOWN"
    assert not any(op["scope"] == "TARGET" for op in plan["operations"])
    assert plan["approval_ready"] is False


def test_read_only_guard_rejects_mutation_sql():
    with pytest.raises(ValueError, match="SELECT statements only"):
        _run_sql(object(), "DROP USER SOURCE_OWNER")


class _FreshMissingCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, binds=None):
        normalized = " ".join(sql.split()).upper()
        assert normalized.startswith("SELECT "), sql
        self.connection.statements.append(sql)
        user = self.connection.user
        if "SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')" in normalized:
            service = self.connection.service
            self.rows = [(user, user, f"DB_{service}", "LOCALDB", "LOCALINSTANCE",
                          f"PDB_{service}", service, self.connection.host)]
        elif "FROM SESSION_PRIVS" in normalized:
            self.rows = [(value,) for value in (
                "CREATE USER", "ALTER USER", "GRANT ANY PRIVILEGE",
                "GRANT ANY OBJECT PRIVILEGE")]
        elif "FROM SESSION_ROLES" in normalized:
            self.rows = [("PDB_DBA",)]
        elif "GRANTABLE='YES'" in normalized and "FROM DBA_TAB_PRIVS" in normalized:
            view = str((binds or {}).get("catalog_name", "")).upper()
            self.rows = [(1,)] if view in self.connection.direct_object_grant_options else []
        elif "FROM USER_SYS_PRIVS" in normalized or "FROM USER_ROLE_PRIVS" in normalized:
            self.rows = []
        elif "FROM DBA_TABLESPACES" in normalized:
            self.rows = [(self.connection.tablespace, "ONLINE", "PERMANENT")]
        elif "FROM DATABASE_PROPERTIES" in normalized:
            self.rows = [(self.connection.tablespace,)]
        elif "FROM DBA_FREE_SPACE" in normalized:
            self.rows = [(self.connection.tablespace, self.connection.free_bytes)]
        elif ("FROM DBA_TAB_PRIVS" in normalized or "FROM DBA_USERS" in normalized
              or "FROM DBA_ROLES" in normalized):
            self.rows = []
        else:
            raise AssertionError(f"unexpected query: {sql}")
        return self

    def fetchall(self):
        return self.rows


class _FreshMissingConnection:
    def __init__(self, user, service, host, tablespace, free_bytes, direct_object_grant_options=None):
        self.user = user.upper()
        self.service = service
        self.host = host
        self.tablespace = tablespace
        self.free_bytes = free_bytes
        self.direct_object_grant_options = {str(view).upper()
                                            for view in (direct_object_grant_options or set())}
        self.statements = []

    def cursor(self):
        return _FreshMissingCursor(self)

    def close(self):
        pass


def _fresh_owner_plan(monkeypatch, *, decisions=None, provider=None,
                      host="localhost", source_service="SOURCE_DB_NEW",
                      target_service="TARGET_DB_NEW", source_owner="SOURCE_OWNER",
                      target_owner="TARGET_OWNER", executor="TARGET_EXECUTOR",
                      source_admin="SOURCE_PDBADMIN", target_admin="TARGET_PDBADMIN",
                      tablespace="USERS", free_bytes=103809024,
                      source_secret="source_pass", target_secret="target_pass",
                      executor_secret="executor_pass", direct_admin_object_grants=None):
    cfg = _config(security=True)
    cfg["source"]["connection"].update(host=host, username=source_owner, service_name=source_service)
    cfg["target"]["connection"].update(host=host, username=target_owner, service_name=target_service)
    cfg["source"]["connection"]["password_secret"] = source_secret
    cfg["target"]["connection"]["password_secret"] = target_secret
    cfg["oracle_permission_migration"]["executor"].update(username=executor)
    cfg["oracle_permission_migration"]["executor"]["password_secret"] = executor_secret
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}"))
    connections = []

    def connect(**kwargs):
        descriptor = kwargs["dsn"].split("/")
        conn = _FreshMissingConnection(kwargs["user"], descriptor[-1], descriptor[0].split(":")[0],
                                       tablespace, free_bytes, direct_admin_object_grants or set())
        connections.append(conn)
        return conn

    admins = {
        "source": {"username": source_admin, "password": "runtime-only-source"},
        "target": {"username": target_admin, "password": "runtime-only-target"},
    }
    provider = provider or _Provider()
    choices = decisions or {"tablespaces": {}, "quota_bytes": {}}
    first = build_plan(cfg, provider=provider, connect=connect, admin_credentials=admins,
                       decisions=choices)
    confirmations = {side: first["sides"][side]["identity_confirmation"]["current"]
                     for side in ("source", "target")}
    plan = build_plan(cfg, provider=provider, connect=connect, admin_credentials=admins,
                      confirmed_identities=confirmations, decisions=choices)
    return plan, connections


def test_fresh_source_target_and_executor_get_complete_authorized_plan(monkeypatch):
    plan, connections = _fresh_owner_plan(monkeypatch)
    assert plan["approval_ready"] is True, json.dumps(plan["blockers"])
    assert plan["plan_status"] == "READY FOR REVIEW"
    assert plan["sides"]["source"]["owner"]["status"] == "MISSING"
    assert plan["sides"]["target"]["owner"]["status"] == "MISSING"
    assert plan["sides"]["source"]["object_privileges"].get("scope") is None
    assert plan["source_permission_inventory"]["status"].startswith("NOT YET APPLICABLE")
    ops = plan["operations"]
    positions = {op["operation_id"]: index for index, op in enumerate(ops)}
    assert all(positions[dependency] < positions[op["operation_id"]]
               for op in ops for dependency in op["dependencies"])
    rendered = _format_plan_console(plan)
    def operation_line(op):
        kind, params = op["operation_type"], op["parameters"]
        if kind == "CREATE_OWNER":
            detail = f"create owner {op['account']} in {params['permanent_tablespace']}"
        elif kind == "CREATE_EXECUTOR":
            detail = f"create permission executor {op['account']}"
        elif kind == "ASSIGN_FINITE_QUOTA":
            detail = f"set {op['account']} quota to "
        elif kind == "GRANT_SYSTEM_PRIVILEGE":
            detail = f"grant {params['privilege']} to {op['account']}"
        else:
            detail = f"grant SELECT on SYS.{params['view']} to {op['account']} (SYSDBA required on {op['scope']} PDB service)"
        return f"{op['scope']}: {detail}"

    rendered_positions = {op["operation_id"]: rendered.index(operation_line(op)) for op in ops}
    assert all(rendered_positions[dependency] < rendered_positions[op["operation_id"]]
               for op in ops for dependency in op["dependencies"])
    source_create = next(op for op in ops if op["operation_type"] == "CREATE_OWNER" and op["scope"] == "SOURCE")
    target_create = next(op for op in ops if op["operation_type"] == "CREATE_OWNER" and op["scope"] == "TARGET")
    executor_create = next(op for op in ops if op["operation_type"] == "CREATE_EXECUTOR")
    assert source_create["account"] == "SOURCE_OWNER"
    assert target_create["account"] == "TARGET_OWNER"
    assert executor_create["account"] == "TARGET_EXECUTOR"
    assert plan["sides"]["source"]["tablespace"]["current"] == "Selected USERS"
    assert plan["sides"]["target"]["tablespace"]["current"] == "Selected USERS"
    expected_quota = 49 * 1024 * 1024
    assert plan["sides"]["source"]["quota"]["recommendation_bytes"] == expected_quota
    assert plan["sides"]["target"]["quota"]["recommendation_bytes"] == expected_quota
    assert all(op["authority"]["exact_execution_authority_verified"] for op in ops
               if op["operation_type"] != "GRANT_CATALOG_ACCESS")
    assert all(op["authority"]["requires_sysdba"]
               and not op["authority"]["exact_execution_authority_verified"]
               for op in ops if op["operation_type"] == "GRANT_CATALOG_ACCESS")
    catalog_ops = {(op["scope"], op["account"], op["parameters"]["view"])
                   for op in ops if op["operation_type"] == "GRANT_CATALOG_ACCESS"}
    assert {("SOURCE", "SOURCE_OWNER", view) for view in
            ("DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS")} <= catalog_ops
    assert {("TARGET", "TARGET_OWNER", view) for view in
            ("DBA_DEPENDENCIES", "DBA_CONSTRAINTS")} <= catalog_ops
    assert {("TARGET", "TARGET_EXECUTOR", view) for view in
            ("DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS", "DBA_TAB_PRIVS", "DBA_COL_PRIVS")} <= catalog_ops
    for op in ops:
        if op["operation_type"] in {"GRANT_CATALOG_ACCESS", "GRANT_SYSTEM_PRIVILEGE", "ASSIGN_FINITE_QUOTA"}:
            parent = source_create if op["scope"] == "SOURCE" else (
                executor_create if op["account"] == "TARGET_EXECUTOR" else target_create)
            assert parent["operation_id"] in op["dependencies"]
    assert all(all(stmt.lstrip().upper().startswith("SELECT ") for stmt in conn.statements)
               for conn in connections)
    assert "runtime-only" not in json.dumps(plan)


def test_exact_direct_sys_object_grant_option_avoids_sysdba_requirement(monkeypatch):
    plan, _ = _fresh_owner_plan(monkeypatch, direct_admin_object_grants={"DBA_USERS"})
    operations = [operation for operation in plan["operations"]
                  if operation["operation_type"] == "GRANT_CATALOG_ACCESS"
                  and operation["parameters"]["view"] == "DBA_USERS"]
    assert operations
    assert all(operation["authority"]["exact_execution_authority_verified"]
               and not operation["authority"]["requires_sysdba"]
               and operation["authority"]["execution_identity"] in {
                   "SOURCE_PDBADMIN", "TARGET_PDBADMIN"}
               for operation in operations)


def test_live_catalog_grant_authority_requires_direct_grant_option(monkeypatch):
    identity = {"db_unique_name": "LOCALCDB", "container_name": "SOURCE_PDB",
                "service_name": "SOURCE_SERVICE"}
    operation = _make_operation(
        operation_type="GRANT_CATALOG_ACCESS", side="source", identity=identity,
        service="SOURCE_SERVICE", account="SOURCE_OWNER", current={"access": "MISSING"},
        desired={"view": "DBA_USERS", "privilege": "SELECT"},
        parameters={"view": "DBA_USERS"}, authority={"requires_sysdba": False},
        verification={"query": "DBA_USERS", "expected": "SELECT readable"})

    class Connection:
        def __init__(self, grantable): self.grantable = grantable
        def cursor(self):
            connection = self
            class Cursor:
                def __init__(self): self.rows = []
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def execute(self, _sql, _binds=None):
                    self.rows = [(1,)] if connection.grantable else []
                def fetchall(self): return self.rows
            return Cursor()

    _assert_live_operation_authority(Connection(True), operation)
    with pytest.raises(RuntimeError, match="direct SYS object grant option"):
        _assert_live_operation_authority(Connection(False), operation)


def test_confirmed_missing_executor_keeps_absence_separate_from_missing_secret(monkeypatch):
    class MissingExecutorSecret:
        def get_secret(self, name):
            if name == "executor_pass":
                raise KeyError(name)
            return f"private-value-for-{name}"

    plan, _ = _fresh_owner_plan(monkeypatch, provider=MissingExecutorSecret())
    executor = plan["permission_executor"]
    assert executor["account"]["status"] == "MISSING"
    assert executor["secret"]["status"] == "BLOCKED"
    assert "password secret is unavailable" in executor["secret"]["current"]
    assert not any(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"])
    assert plan["approval_ready"] is False
    assert "permission_executor.secret" in _format_plan_console(plan)


def test_executor_secret_provider_failure_is_not_treated_as_missing_secret(monkeypatch):
    class BrokenSecretProvider:
        def get_secret(self, _name):
            raise RuntimeError("vault network failure containing no safe detail")

    plan, _ = _fresh_owner_plan(monkeypatch, provider=BrokenSecretProvider())
    executor = plan["permission_executor"]
    assert executor["account"]["status"] == "MISSING"
    assert executor["secret"]["status"] == "BLOCKED"
    assert "lookup failed (RuntimeError)" in executor["secret"]["current"]
    assert "vault network failure" not in json.dumps(plan)
    assert not any(op["operation_type"] == "CREATE_EXECUTOR" for op in plan["operations"])


def test_actual_env_resolver_missing_secrets_prompt_overlay_then_build_full_plan(monkeypatch):
    from core.secrets.factory import create_secret_provider

    names = {"source": "dev_source_pwd", "target": "qa_target_pwd", "executor": "security_runner_pwd"}
    for ref in names.values():
        monkeypatch.delenv(f"SECRET_{ref}", raising=False)
    config = _config(security=True)
    config["source"]["connection"]["password_secret"] = names["source"]
    config["target"]["connection"]["password_secret"] = names["target"]
    config["oracle_permission_migration"]["executor"]["password_secret"] = names["executor"]
    resolver = create_secret_provider(config)

    assert [_secret(resolver, ref, "test")[1] for ref in names.values()] == ["MISSING"] * 3
    blocked, _ = _fresh_owner_plan(
        monkeypatch, provider=resolver, source_secret=names["source"],
        target_secret=names["target"], executor_secret=names["executor"])
    assert blocked["sides"]["source"]["owner"]["status"] == "MISSING"
    assert blocked["sides"]["target"]["owner"]["status"] == "MISSING"
    assert blocked["permission_executor"]["account"]["status"] == "MISSING"
    assert blocked["approval_ready"] is False

    monkeypatch.setenv(f"SECRET_{names['source']}", "env-source-value")
    monkeypatch.setenv(f"SECRET_{names['executor']}", "env-executor-value")
    mixed_resolver = create_secret_provider(config)
    mixed, _ = _fresh_owner_plan(
        monkeypatch, provider=mixed_resolver, source_secret=names["source"],
        target_secret=names["target"], executor_secret=names["executor"])
    mixed_creates = {op["account"] for op in mixed["operations"]
                     if op["operation_type"] in {"CREATE_OWNER", "CREATE_EXECUTOR"}}
    assert mixed_creates == {"SOURCE_OWNER", "TARGET_EXECUTOR"}

    monkeypatch.setenv(f"SECRET_{names['target']}", "env-target-value")
    all_available = create_secret_provider(config)
    available_plan, _ = _fresh_owner_plan(
        monkeypatch, provider=all_available, source_secret=names["source"],
        target_secret=names["target"], executor_secret=names["executor"])
    assert available_plan["approval_ready"] is True
    assert {op["account"] for op in available_plan["operations"]
            if op["operation_type"] in {"CREATE_OWNER", "CREATE_EXECUTOR"}} == {
        "SOURCE_OWNER", "TARGET_OWNER", "TARGET_EXECUTOR"}
    assert "env-source-value" not in json.dumps(available_plan)

    overlay = _RuntimeSecretOverlay(resolver)
    for ref in names.values():
        overlay.values[ref] = f"one-time-{ref}-value"
    ready, _ = _fresh_owner_plan(
        monkeypatch, provider=overlay, source_secret=names["source"],
        target_secret=names["target"], executor_secret=names["executor"])
    assert ready["approval_ready"] is True
    assert {op["account"] for op in ready["operations"]
            if op["operation_type"] in {"CREATE_OWNER", "CREATE_EXECUTOR"}} == {
        "SOURCE_OWNER", "TARGET_OWNER", "TARGET_EXECUTOR"}
    assert "one-time-" not in json.dumps(ready)


def test_fresh_plan_quota_override_is_exact_and_limited_by_free_space(monkeypatch):
    override = 50 * 1024 * 1024
    plan, _ = _fresh_owner_plan(monkeypatch, decisions={
        "tablespaces": {}, "quota_bytes": {"source": override, "target": override}})
    for side in ("source", "target"):
        op = next(item for item in plan["operations"]
                  if item["operation_type"] == "ASSIGN_FINITE_QUOTA" and item["scope"] == side.upper())
        assert op["parameters"]["quota_bytes"] == override
        assert plan["sides"][side]["quota"]["quota_source"] == "user_override"
    blocked, _ = _fresh_owner_plan(monkeypatch, decisions={
        "tablespaces": {}, "quota_bytes": {"source": 110 * 1024 * 1024}})
    assert blocked["sides"]["source"]["quota"]["status"] == "UNKNOWN"
    assert not any(op["operation_type"] == "CREATE_OWNER" and op["scope"] == "SOURCE"
                   for op in blocked["operations"])


def test_quota_recommendation_policy_is_storage_derived_and_conservative():
    from migration_platform.oracle_setup_plan import _recommend_finite_quota
    assert _recommend_finite_quota(103809024) == 49 * 1024 * 1024
    assert _recommend_finite_quota(2 * 1024 * 1024) == 1024 * 1024
    assert _recommend_finite_quota(1024 * 1024) is None
    assert _recommend_finite_quota(None) is None


def test_fresh_setup_uses_environment_specific_names_host_tablespace_and_capacity(monkeypatch):
    plan, _ = _fresh_owner_plan(
        monkeypatch, host="db-dev.example", source_service="SRC_PDB_SERVICE",
        target_service="DEST_PDB_SERVICE", source_owner="APP_SOURCE",
        target_owner="APP_DEST", executor="SECURITY_RUNNER",
        source_admin="SRC_ADMIN", target_admin="DEST_ADMIN",
        tablespace="DEV_DATA", free_bytes=199 * 1024 * 1024)
    assert plan["approval_ready"] is True
    assert plan["sides"]["source"]["identity"]["status"] == "READY"
    assert plan["sides"]["source"]["identity_confirmation"]["status"] == "READY"
    assert plan["sides"]["source"]["quota"]["recommendation_bytes"] == 99 * 1024 * 1024
    assert plan["sides"]["target"]["quota"]["recommendation_bytes"] == 99 * 1024 * 1024
    assert {op["account"] for op in plan["operations"] if op["operation_type"] == "CREATE_OWNER"} == {
        "APP_SOURCE", "APP_DEST"}
    assert next(op for op in plan["operations"] if op["operation_type"] == "CREATE_EXECUTOR")["account"] == "SECURITY_RUNNER"
    assert all(op["service"] in {"SRC_PDB_SERVICE", "DEST_PDB_SERVICE"}
               for op in plan["operations"])


def test_console_plan_is_compact_and_does_not_dump_admin_privileges(monkeypatch):
    plan, _ = _fresh_owner_plan(monkeypatch)
    output = _format_plan_console(plan)
    assert "ORACLE SETUP PLAN" in output
    assert "Missing — will create" in output
    assert "Recommended" not in output  # displayed quota value, not a verbose report
    assert "49 MiB" in output
    assert "PROPOSED OPERATIONS" in output
    assert "create owner SOURCE_OWNER in USERS" in output
    assert "CREATE USER" not in output
    assert "PDBADMIN" not in output
    assert "fingerprint" in output.lower()


def test_apply_cli_collects_all_fresh_account_secrets_before_approval(monkeypatch, tmp_path, capsys):
    import migration_platform.oracle_setup_plan as setup

    config = _config(security=True)
    for ref in ("source_pass", "target_pass", "executor_pass"):
        monkeypatch.delenv(f"SECRET_{ref}", raising=False)
    config_path = tmp_path / "portable-oracle.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    fingerprint = "a" * 64
    secret_names = {
        "source": "source_pass", "target": "target_pass", "executor": "executor_pass",
    }
    tokens = {"source": "source-token", "target": "target-token"}
    operation_specs = [
        ("CREATE_OWNER", "SOURCE", "SOURCE_OWNER", {"permanent_tablespace": "USERS"}),
        ("ASSIGN_FINITE_QUOTA", "SOURCE", "SOURCE_OWNER", {"quota_bytes": 1_048_576, "tablespace": "USERS"}),
        ("CREATE_OWNER", "TARGET", "TARGET_OWNER", {"permanent_tablespace": "USERS"}),
        ("ASSIGN_FINITE_QUOTA", "TARGET", "TARGET_OWNER", {"quota_bytes": 1_048_576, "tablespace": "USERS"}),
        ("CREATE_EXECUTOR", "TARGET", "PERM_EXEC", {}),
        ("GRANT_SYSTEM_PRIVILEGE", "TARGET", "PERM_EXEC", {"privilege": "CREATE SESSION"}),
        *( ("GRANT_CATALOG_ACCESS", "TARGET", "PERM_EXEC", {"view": view})
           for view in ("DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS",
                        "DBA_TAB_PRIVS", "DBA_COL_PRIVS") ),
    ]
    operations = []
    previous = {}
    for index, (kind, scope, account, parameters) in enumerate(operation_specs):
        op_id = f"op-{index}"
        dependency = [previous[account]] if account in previous else []
        operations.append({"operation_id": op_id, "operation_type": kind, "scope": scope,
                           "account": account, "parameters": parameters, "authority": {},
                           "dependencies": dependency})
        previous[account] = op_id

    snapshots = []

    def fake_build(_config, *, provider, admin_credentials=None, privileged_credentials=None,
                   confirmed_identities=None, decisions=None):
        admin_credentials = admin_credentials or {}
        confirmed_identities = confirmed_identities or {}
        decisions = decisions or {"tablespaces": {}, "quota_bytes": {}}
        snapshots.append({"admins": set(admin_credentials), "secrets": set(provider.values),
                          "confirmations": set(confirmed_identities),
                          "tablespaces": dict(decisions.get("tablespaces", {}))})
        sides = {}
        for side in ("source", "target"):
            identity = {"db_unique_name": f"DB_{side.upper()}",
                        "container_name": f"PDB_{side.upper()}",
                        "service_name": f"SERVICE_{side.upper()}"}
            sides[side] = {
                "identity": {"status": "READY", "current": json.dumps(identity)},
                "identity_confirmation": {"status": "READY" if side in confirmed_identities else "UNKNOWN",
                                          "current": tokens[side]},
                "owner": {"status": "MISSING" if side in admin_credentials else "UNKNOWN",
                          "current": "Not inspected"},
                "account_discovery": {"status": "MISSING" if side in admin_credentials else "UNKNOWN"},
                "tablespace": {"status": "READY", "current": "USERS"},
                "storage_discovery": {"default_permanent_tablespace": "USERS",
                                      "suitable_tablespaces": ["USERS"],
                                      "free_bytes": {"USERS": 103_809_024}},
                "quota": {"status": "MISSING", "current": "Missing",
                          "recommendation_bytes": 49 * 1024 * 1024,
                          "approved_quota_bytes": None},
                "object_privileges": {}, "catalog_visibility": {},
            }
        secrets_ready = all(_secret(provider, name, "test")[1] == "AVAILABLE"
                            for name in secret_names.values())
        ready = (set(admin_credentials) == {"source", "target"} and secrets_ready
                 and set(confirmed_identities) == {"source", "target"}
                 and set(decisions.get("tablespaces", {})) == {"source", "target"})
        return {
            "schema": "oracle-setup-plan/v1", "sides": sides,
            "permission_executor": {
                "account": {"status": "MISSING" if "target" in admin_credentials else "UNKNOWN"},
                "catalog_visibility": {}, "authority": {},
            },
            "operations": operations if ready else [], "blockers": [],
            "approval_ready": ready,
            "plan_status": "READY FOR REVIEW" if ready else "INCOMPLETE / BLOCKED PLAN",
            "fingerprint": fingerprint,
            "source_permission_inventory": {"status": "NOT YET APPLICABLE"},
        }

    monkeypatch.setattr(setup, "build_plan", fake_build)
    answers = iter([
        "SOURCE_PDBADMIN", "TARGET_PDBADMIN", tokens["source"], tokens["target"],
        "", "", f"APPLY {fingerprint}",
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    secret_values = iter(["admin-source-hidden", "admin-target-hidden",
                          "source-owner-hidden", "target-owner-hidden", "executor-hidden"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(secret_values))
    applied = {}

    def fake_apply(plan, *, approved_fingerprint, provider, admin_credentials, **_kwargs):
        applied.update({"fingerprint": approved_fingerprint, "secrets": dict(provider.values),
                        "admins": set(admin_credentials), "plan": plan})
        return {"status": "SETUP COMPLETE", "completed": [op["operation_id"] for op in plan["operations"]]}

    monkeypatch.setattr(setup, "apply_approved_plan", fake_apply)
    assert setup.main(["--config", str(config_path), "--apply"]) == 0
    output = capsys.readouterr().out
    assert applied["fingerprint"] == fingerprint
    assert applied["admins"] == {"source", "target"}
    assert set(applied["secrets"]) == set(secret_names.values())
    assert applied["secrets"]["executor_pass"] == "executor-hidden"
    assert "CREATE_EXECUTOR" in {op["operation_type"] for op in applied["plan"]["operations"]}
    assert snapshots[-1]["confirmations"] == {"source", "target"}
    assert snapshots[-1]["tablespaces"] == {"source": "USERS", "target": "USERS"}
    assert "executor-hidden" not in output and "source-owner-hidden" not in output
    assert "admin-source-hidden" not in output and "admin-target-hidden" not in output


def test_actual_env_cli_setup_apply_verification_and_repeat_are_end_to_end_mocked(
        monkeypatch, tmp_path, capsys):
    import os
    import re

    import migration_platform.oracle_setup_plan as setup

    config = _config(security=True)
    config["source"]["connection"]["service_name"] = "SOURCE_SERVICE"
    config["target"]["connection"]["service_name"] = "TARGET_SERVICE"
    config_path = tmp_path / "actual-env-oracle.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    refs = ("source_pass", "target_pass", "executor_pass")
    for ref in refs:
        monkeypatch.delenv(f"SECRET_{ref}", raising=False)

    state = {"accounts": {}, "quotas": {}, "privileges": set(), "catalogs": set(), "ddl": [],
             "auth_failures": [], "verification_failure_once": True}
    tablespace = "DEV_DATA"
    free_bytes = 80 * 1024 * 1024

    class OracleCursor:
        def __init__(self, connection):
            self.connection = connection
            self.rows = []

        def __enter__(self): return self
        def __exit__(self, *_args): return False

        def execute(self, sql, binds=None):
            normalized = " ".join(sql.split()).upper()
            user = self.connection.user
            if normalized.startswith("CREATE USER"):
                account_match = re.search(r'CREATE USER "?([A-Z0-9_$#]+)"?', normalized)
                password_match = re.search(r'IDENTIFIED BY "((?:[^"]|"")*)"', sql, re.IGNORECASE)
                ts_match = re.search(r'DEFAULT TABLESPACE "?([A-Z0-9_$#]+)"?', normalized)
                account = account_match.group(1)
                state["accounts"][account] = {
                    "password": password_match.group(1).replace('""', '"'),
                    "tablespace": ts_match.group(1) if ts_match else tablespace,
                    "status": "OPEN",
                }
                state["ddl"].append(sql)
                self.rows = []
                return
            if normalized.startswith("ALTER USER"):
                match = re.search(r'ALTER USER "?([A-Z0-9_$#]+)"? QUOTA (\d+) ON "?([A-Z0-9_$#]+)"?', normalized)
                state["quotas"][(match.group(1), match.group(3))] = int(match.group(2))
                state["ddl"].append(sql)
                self.rows = []
                return
            if normalized.startswith("GRANT "):
                catalog = re.search(r'GRANT SELECT ON SYS\.([A-Z0-9_$#]+) TO "?([A-Z0-9_$#]+)"?', normalized)
                if catalog:
                    state["catalogs"].add((catalog.group(2), catalog.group(1)))
                else:
                    grant = re.search(r'GRANT ([A-Z ]+) TO "?([A-Z0-9_$#]+)"?', normalized)
                    state["privileges"].add((grant.group(2), grant.group(1)))
                state["ddl"].append(sql)
                self.rows = []
                return
            if "SYS_CONTEXT('USERENV','ISDBA')" in normalized:
                self.rows = [("TRUE" if self.connection.is_sysdba else "FALSE",)]
            elif normalized.startswith("SELECT SYS_CONTEXT"):
                service = self.connection.service
                self.rows = [(user, user, f"DB_{service}", "DB_NAME", "INSTANCE",
                              f"PDB_{service}", service, "mock-host")]
            elif "FROM SESSION_PRIVS" in normalized:
                if user.endswith("PDBADMIN"):
                    self.rows = [(value,) for value in (
                        "CREATE USER", "ALTER USER", "GRANT ANY PRIVILEGE",
                        "GRANT ANY OBJECT PRIVILEGE")]
                else:
                    self.rows = [(privilege,) for grantee, privilege in state["privileges"]
                                 if grantee == user]
            elif "FROM SESSION_ROLES" in normalized:
                self.rows = [("PDB_DBA",)] if user.endswith("PDBADMIN") else []
            elif "FROM USER_SYS_PRIVS" in normalized:
                if "ADMIN_OPTION='YES'" in normalized:
                    self.rows = []
                elif binds and binds.get("priv"):
                    self.rows = [("NO",)] if (user, str(binds["priv"]).upper()) in state["privileges"] else []
                else:
                    self.rows = [(privilege,) for grantee, privilege in state["privileges"] if grantee == user]
            elif "FROM USER_ROLE_PRIVS" in normalized or "FROM DBA_ROLES" in normalized or "FROM DBA_ROLE_PRIVS" in normalized:
                self.rows = []
            elif "FROM DBA_USERS" in normalized:
                account = (binds or {}).get("username") if isinstance(binds, dict) else None
                if not account and isinstance(binds, (list, tuple)) and binds:
                    account = binds[0]
                account = str(account or "").upper()
                found = state["accounts"].get(account)
                if not found:
                    self.rows = []
                elif "ACCOUNT_STATUS, DEFAULT_TABLESPACE, COMMON" in normalized:
                    self.rows = [(found["status"], found["tablespace"], "NO")]
                elif "ACCOUNT_STATUS" in normalized:
                    self.rows = [(found["status"],)]
                else:
                    self.rows = [(account,)]
            elif "FROM DBA_TABLESPACES" in normalized:
                self.rows = [(tablespace, "ONLINE", "PERMANENT")]
            elif "FROM DATABASE_PROPERTIES" in normalized:
                self.rows = [(tablespace,)]
            elif "FROM DBA_FREE_SPACE" in normalized:
                self.rows = [(tablespace, free_bytes)]
            elif "FROM DBA_TS_QUOTAS" in normalized:
                account = str((binds or {}).get("username", "")).upper()
                ts = str((binds or {}).get("tablespace", "")).upper()
                quota = state["quotas"].get((account, ts))
                self.rows = [(quota,)] if quota is not None else []
            elif "FROM DBA_SYS_PRIVS" in normalized:
                account = str((binds or {}).get("username", "")).upper()
                privilege = str((binds or {}).get("privilege", "")).upper()
                exists = (account, privilege) in state["privileges"]
                self.rows = [("NO" if "ADMIN_OPTION" in normalized else privilege,)] if exists else []
            elif "FROM DBA_TAB_PRIVS" in normalized:
                account = str((binds or {}).get("username", "")).upper()
                view = str((binds or {}).get("catalog_name", "")).upper()
                if "GRANTABLE='YES'" in normalized:
                    self.rows = []
                elif (state["verification_failure_once"] and account == "SOURCE_OWNER"
                        and view == "DBA_USERS" and (account, view) in state["catalogs"]):
                    # Simulate Oracle DDL committing before postcondition
                    # verification reports a transient read failure.
                    state["verification_failure_once"] = False
                    self.rows = []
                else:
                    self.rows = [(1,)] if (account, view) in state["catalogs"] else []
            elif "FROM SYS.DBA_TAB_PRIVS" in normalized:
                account = str((binds or {}).get("username", "")).upper()
                view = str((binds or {}).get("catalog_name", "")).upper()
                self.rows = [(1,)] if (account, view) in state["catalogs"] else []
            elif "FROM USER_USERS" in normalized:
                found = state["accounts"].get(user)
                self.rows = [(user, found["status"], found["tablespace"])] if found else []
            elif "FROM USER_TABLESPACES" in normalized:
                self.rows = [(tablespace, "ONLINE", "PERMANENT")]
            elif "FROM USER_TS_QUOTAS" in normalized:
                ts = str((binds or {}).get("ts", "")).upper()
                quota = state["quotas"].get((user, ts))
                self.rows = [(quota,)] if quota is not None else []
            elif ("FROM USER_OBJECTS" in normalized or "FROM ALL_TAB_PRIVS_MADE" in normalized
                  or "FROM ALL_COL_PRIVS_MADE" in normalized or "FROM DBA_DEPENDENCIES" in normalized
                  or "FROM DBA_CONSTRAINTS" in normalized):
                self.rows = []
            elif "FROM SYS.DBA_" in normalized:
                view = re.search(r"FROM SYS\.([A-Z0-9_$#]+)", normalized).group(1)
                if user.endswith("PDBADMIN") or (user, view) in state["catalogs"]:
                    self.rows = []
                else:
                    raise PermissionError("catalog grant absent")
            else:
                self.rows = []

        def fetchall(self): return self.rows

    class OracleConnection:
        def __init__(self, user, service, password, is_sysdba=False):
            self.user, self.service = user.upper(), service
            self.is_sysdba = is_sysdba
            if not self.user.endswith("PDBADMIN") and not self.is_sysdba:
                account = state["accounts"].get(self.user)
                if not account or account["password"] != password:
                    state["auth_failures"].append((self.user, len(account["password"]) if account else None,
                                                   len(password or "")))
                    raise RuntimeError("ORA-01017: invalid username/password")

        def cursor(self): return OracleCursor(self)
        def commit(self): pass
        def close(self): pass

    driver = SimpleNamespace(
        SYSDBA="SYSDBA",
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}",
        connect=lambda **kwargs: OracleConnection(
            kwargs["user"], kwargs["dsn"].rsplit("/", 1)[-1], kwargs.get("password", ""),
            is_sysdba=kwargs.get("mode") == "SYSDBA"))
    monkeypatch.setitem(sys.modules, "oracledb", driver)

    def input_value(prompt=""):
        if "SYSDBA username" in prompt:
            return "SYS"
        if "administrator username" in prompt:
            return "SOURCE_PDBADMIN" if prompt.startswith("SOURCE") else "TARGET_PDBADMIN"
        if "enter confirmation token" in prompt:
            return prompt.rstrip().split()[-1].rstrip(":")
        if "finite quota" in prompt or "Press Enter to accept" in prompt:
            return ""
        if "type APPLY" in prompt:
            return "APPLY " + prompt.rstrip().split()[-1].rstrip(":")
        raise AssertionError(f"unexpected CLI input prompt: {prompt}")

    monkeypatch.setattr("builtins.input", input_value)

    def password_input(prompt):
        if prompt.startswith("SOURCE PDB administrator"):
            return "source-admin-password"
        if prompt.startswith("TARGET PDB administrator"):
            return "target-admin-password"
        if "SOURCE owner password" in prompt:
            return "source-owner-password"
        if "TARGET owner password" in prompt:
            return "target-owner-password"
        if "permission executor password" in prompt:
            return "executor-password"
        if "SYSDBA password" in prompt:
            return "sysdba-password"
        raise AssertionError(f"unexpected hidden password prompt: {prompt}")

    monkeypatch.setattr("getpass.getpass", password_input)
    real_apply = setup.apply_approved_plan
    verification_plans = []

    def capture_apply(*args, **kwargs):
        fresh_plan = kwargs["fresh_plan"]
        def capture_fresh_plan():
            plan = fresh_plan()
            verification_plans.append(plan)
            return plan
        kwargs["fresh_plan"] = capture_fresh_plan
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(setup, "apply_approved_plan", capture_apply)
    result_code = setup.main(["--config", str(config_path), "--apply"])
    assert result_code == 2, json.dumps({
        "verification_status": verification_plans[-1].get("plan_status") if verification_plans else None,
        "diagnostics": setup._blocked_diagnostics(verification_plans[-1]) if verification_plans else None,
        "blockers": verification_plans[-1].get("blockers") if verification_plans else None,
        "remaining_operations": [op["operation_type"] for op in verification_plans[-1].get("operations", [])] if verification_plans else None,
        "auth_failures": state["auth_failures"],
    }, indent=2)
    first_output = capsys.readouterr().out
    assert "SETUP FAILED" in first_output
    assert "Completed operations: 11" in first_output
    assert "postcondition verification" in first_output
    assert "Failed operation:" in first_output
    assert "source-owner-password" not in first_output
    assert "sysdba-password" not in first_output
    assert set(state["accounts"]) == {"SOURCE_OWNER"}
    ddl_after_partial = len(state["ddl"])
    assert ddl_after_partial == 12  # The failed grant committed before its verification read.
    assert all(f"SECRET_{ref}" not in os.environ for ref in refs)

    assert setup.main(["--config", str(config_path), "--apply"]) == 0
    recovery_output = capsys.readouterr().out
    assert "SETUP COMPLETE" in recovery_output
    assert {"SOURCE_OWNER", "TARGET_OWNER", "PERM_EXEC"} <= set(state["accounts"])
    assert len(state["ddl"]) > ddl_after_partial
    assert sum("CREATE USER \"SOURCE_OWNER\"" in sql for sql in state["ddl"]) == 1
    assert sum("GRANT SELECT ON SYS.DBA_USERS TO \"SOURCE_OWNER\"" in sql for sql in state["ddl"]) == 1
    assert all(secret not in recovery_output for secret in (
        "source-owner-password", "target-owner-password", "executor-password",
        "source-admin-password", "target-admin-password", "sysdba-password"))

    ddl_after_first_apply = len(state["ddl"])
    assert setup.main(["--config", str(config_path), "--apply"]) == 0
    repeat_output = capsys.readouterr().out
    assert "NO CHANGES REQUIRED" in repeat_output
    assert len(state["ddl"]) == ddl_after_first_apply


def test_apply_cli_requests_existing_account_passwords_without_recreation(monkeypatch, tmp_path, capsys):
    import migration_platform.oracle_setup_plan as setup

    config = _config(security=True)
    for ref in ("source_pass", "target_pass", "executor_pass"):
        monkeypatch.delenv(f"SECRET_{ref}", raising=False)
    config_path = tmp_path / "existing-accounts.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    tokens = {"source": "source-token", "target": "target-token"}
    refs = {"source": "source_pass", "target": "target_pass", "executor": "executor_pass"}
    seen_operations = []

    def existing_plan(_config, *, provider, admin_credentials=None, privileged_credentials=None,
                      confirmed_identities=None, decisions=None):
        admin_credentials = admin_credentials or {}
        confirmed_identities = confirmed_identities or {}
        sides = {}
        for side in ("source", "target"):
            available = _secret(provider, refs[side], side)[1] == "AVAILABLE"
            owner = ({"status": "READY", "current": f"{side.upper()}_OWNER / OPEN"}
                     if available else
                     {"status": "UNKNOWN", "current": "Runtime owner secret unavailable; owner session and schema inventory cannot be verified"})
            identity = {"db_unique_name": f"DB_{side.upper()}",
                        "container_name": f"PDB_{side.upper()}",
                        "service_name": f"SERVICE_{side.upper()}"}
            sides[side] = {
                "identity": {"status": "READY", "current": json.dumps(identity)},
                "identity_confirmation": {"status": "READY" if side in confirmed_identities else "UNKNOWN",
                                          "current": tokens[side]},
                "account_discovery": {"status": "READY"},
                "owner": owner, "tablespace": {"status": "READY", "current": "DATA_TS"},
                "quota": {"status": "READY", "current": "100 MiB"},
                "object_privileges": {}, "catalog_visibility": {},
            }
        executor_available = _secret(provider, refs["executor"], "executor")[1] == "AVAILABLE"
        executor_account = ({"status": "READY", "current": "EXECUTOR / OPEN"}
                            if executor_available else
                            {"status": "BLOCKED", "current": "Configured executor password secret is unavailable"})
        ready = (set(admin_credentials) == {"source", "target"}
                 and set(confirmed_identities) == {"source", "target"}
                 and all(_secret(provider, ref, "test")[1] == "AVAILABLE" for ref in refs.values()))
        plan = {"schema": "oracle-setup-plan/v1", "sides": sides,
                    "permission_executor": {"account": executor_account,
                                            "account_discovery": {"status": "READY"},
                                            "catalog_visibility": {}, "authority": {}},
                "operations": [], "blockers": [], "approval_ready": ready,
                "plan_status": "NO CHANGES REQUIRED" if ready else "INCOMPLETE / BLOCKED PLAN",
                "fingerprint": "b" * 64,
                "source_permission_inventory": {"status": "READY"}}
        seen_operations.append(list(plan["operations"]))
        return plan

    monkeypatch.setattr(setup, "build_plan", existing_plan)
    answers = iter(["SOURCE_DBA", "TARGET_DBA", tokens["source"], tokens["target"]])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    prompted = []
    secret_values = iter(["source-dba-hidden", "target-dba-hidden",
                          "source-owner-existing", "target-owner-existing", "executor-existing"])

    def secure_prompt(prompt):
        prompted.append(prompt)
        return next(secret_values)

    monkeypatch.setattr("getpass.getpass", secure_prompt)
    monkeypatch.setattr(setup, "apply_approved_plan", lambda *_args, **_kwargs:
                        pytest.fail("existing ready accounts must not be recreated"))
    assert setup.main(["--config", str(config_path), "--apply"]) == 0
    output = capsys.readouterr().out
    assert any("Existing SOURCE owner password" in prompt for prompt in prompted)
    assert any("Existing TARGET owner password" in prompt for prompt in prompted)
    assert any("Existing permission executor password" in prompt for prompt in prompted)
    assert "NO CHANGES REQUIRED" in output
    assert all(not operations for operations in seen_operations)
    assert all(value not in output for value in (
        "source-owner-existing", "target-owner-existing", "executor-existing"))


def _approved_test_plan():
    identity = {"db_unique_name": "LOCALCDB", "container_name": "TARGET_PDB",
                "service_name": "TARGET_SERVICE"}
    operation = _make_operation(
        operation_type="CREATE_EXECUTOR", side="target", identity=identity,
        service="TARGET_SERVICE", account="PERM_EXEC", current={"account": "ABSENT"},
        desired={"account_status": "OPEN"},
        parameters={"password_secret_ref": "executor_pass"}, authority={},
        verification={"query": "DBA_USERS", "expected": "OPEN"})
    payload = {"test": "approved"}
    import hashlib
    plan = {"schema": "oracle-setup-plan/v1", "operations": [operation],
            "fingerprint_payload": payload,
            "fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True).encode()).hexdigest(), "approval_ready": True,
            "plan_status": "READY FOR REVIEW", "blockers": []}
    return plan


def test_apply_rejects_wrong_approval_and_stale_plan_without_connecting():
    plan = _approved_test_plan()
    connect_calls = []
    args = {"config": _config(), "provider": _Provider(), "admin_credentials": {},
            "connect": lambda **kw: connect_calls.append(kw), "fresh_plan": lambda: plan}
    with pytest.raises(ValueError, match="fingerprint"):
        apply_approved_plan(plan, approved_fingerprint="wrong", **args)
    stale_payload = {"test": "changed"}
    import hashlib
    stale = {**plan, "fingerprint_payload": stale_payload,
             "fingerprint": hashlib.sha256(json.dumps(stale_payload, sort_keys=True,
                 separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()}
    with pytest.raises(RuntimeError, match="stale plan"):
        apply_approved_plan(plan, approved_fingerprint=plan["fingerprint"],
                            **{**args, "fresh_plan": lambda: stale})
    assert connect_calls == []


def test_sysdba_catalog_grant_rejects_wrong_pdb_before_any_setup_ddl(monkeypatch):
    import hashlib

    identity = {"db_unique_name": "LOCALCDB", "container_name": "TARGET_PDB",
                "service_name": "TARGET_SERVICE"}
    operation = _make_operation(
        operation_type="GRANT_CATALOG_ACCESS", side="target", identity=identity,
        service="TARGET_SERVICE", account="TARGET_OWNER", current={"access": "MISSING"},
        desired={"view": "DBA_USERS", "privilege": "SELECT"},
        parameters={"view": "DBA_USERS"},
        authority={"requires_sysdba": True},
        verification={"query": "DBA_USERS", "expected": "SELECT readable"})
    payload = {"operations": [operation]}
    plan = {"schema": "oracle-setup-plan/v1", "operations": [operation],
            "fingerprint_payload": payload,
            "fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
            "approval_ready": True, "plan_status": "READY FOR REVIEW", "blockers": []}
    statements = []

    class Connection:
        def cursor(self):
            class Cursor:
                def __init__(self): self.rows = []
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def execute(self, sql, _binds=None):
                    statements.append(sql)
                    if "ISDBA" in sql.upper():
                        self.rows = [("TRUE",)]
                    else:
                        self.rows = [("SYS", "SYS", "LOCALCDB", "LOCALDB", "INSTANCE",
                                      "OTHER_PDB", "TARGET_SERVICE", "localhost")]
                def fetchall(self): return self.rows
            return Cursor()
        def close(self): pass

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(
        SYSDBA="SYSDBA", makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}"))
    result = apply_approved_plan(
        plan, approved_fingerprint=plan["fingerprint"], config=_config(), provider=_Provider(),
        admin_credentials={}, privileged_credentials={
            "target": {"username": "SYS", "password": "private-sys-password"}},
        connect=lambda **kwargs: Connection(), fresh_plan=lambda: plan)
    assert result["status"] == "SETUP FAILED"
    assert result["execution_stage"] == "SYSDBA credential preflight"
    assert result["completed"] == []
    assert not any(sql.upper().startswith("GRANT ") for sql in statements)
    assert "private-sys-password" not in json.dumps(result)


def test_apply_executes_only_typed_operation_and_verifies_postcondition(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(makedsn=lambda *a, **k: "dsn"))
    plan = _approved_test_plan()

    class ApplyConnection:
        def __init__(self):
            self.created = False
            self.statements = []
        def cursor(self):
            owner = self
            class Cursor:
                def __init__(self): self.rows = []
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def execute(self, sql, binds=None):
                    owner.statements.append(sql)
                    normalized = " ".join(sql.split()).upper()
                    if normalized.startswith("SELECT SYS_CONTEXT"):
                        self.rows = [("TARGET_DBA", "TARGET_DBA", "LOCALCDB", "LOCALDB",
                                      "INSTANCE", "TARGET_PDB", "TARGET_SERVICE", "localhost")]
                    elif "FROM DBA_USERS" in normalized:
                        self.rows = [("OPEN",)] if owner.created else []
                    elif "FROM SESSION_PRIVS" in normalized:
                        self.rows = [("CREATE USER",)]
                    elif "FROM SESSION_ROLES" in normalized or "FROM USER_SYS_PRIVS" in normalized or "FROM USER_ROLE_PRIVS" in normalized:
                        self.rows = []
                    elif normalized.startswith("CREATE USER"):
                        owner.created = True
                        self.rows = []
                    else:
                        raise AssertionError(f"unexpected SQL {sql}")
                def fetchall(self): return self.rows
            return Cursor()
        def commit(self): pass
        def close(self): pass

    connection = ApplyConnection()
    config = _config()
    result = apply_approved_plan(
        plan, approved_fingerprint=plan["fingerprint"], config=config,
        provider=_Provider(), admin_credentials={"target": {"username": "TARGET_DBA", "password": "hidden"}},
        connect=lambda **_kw: connection, fresh_plan=lambda: plan)
    assert result["completed"] == [plan["operations"][0]["operation_id"]]
    assert any(statement.startswith("CREATE USER") for statement in connection.statements)
    assert all("DROP" not in statement.upper() for statement in connection.statements)
    assert "private-value-for-executor_pass" not in json.dumps(result)


def test_apply_stops_on_partial_failure_and_requires_a_fresh_retry_plan(monkeypatch):
    import hashlib

    import migration_platform.oracle_setup_plan as setup

    plan = _approved_test_plan()
    create = plan["operations"][0]
    grant = _make_operation(
        operation_type="GRANT_SYSTEM_PRIVILEGE", side="target",
        identity=create["expected_identity"], service=create["service"], account="PERM_EXEC",
        current={"privilege": "MISSING"}, desired={"privilege": "CREATE SESSION"},
        parameters={"privilege": "CREATE SESSION"}, authority={},
        dependencies=[create["operation_id"]],
        verification={"query": "DBA_SYS_PRIVS", "expected": "CREATE SESSION"})
    plan["operations"].append(grant)
    payload = {"test": "partial apply"}
    plan["fingerprint_payload"] = payload
    plan["fingerprint"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    class Connection:
        def cursor(self):
            class Cursor:
                def __init__(self):
                    self.rows = [("TARGET_DBA", "TARGET_DBA", "LOCALCDB", "LOCALDB", "INSTANCE",
                                  "TARGET_PDB", "TARGET_SERVICE", "localhost")]
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def execute(self, _sql, _binds=None): pass
                def fetchall(self): return self.rows
            return Cursor()
        def close(self): pass

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(makedsn=lambda *a, **k: "dsn"))
    monkeypatch.setattr(setup, "_assert_live_operation_authority", lambda *_args: None)
    completed = set()
    monkeypatch.setattr(setup, "_verify_setup_operation",
                        lambda _conn, operation: operation["operation_id"] in completed)

    class OracleDriverErrorInfo:
        code = 1017
        message = "ORA-01017: invalid username/password"

    def execute(_conn, operation, _provider):
        if operation["operation_type"] == "CREATE_EXECUTOR":
            completed.add(operation["operation_id"])
            return
        raise RuntimeError(OracleDriverErrorInfo())

    monkeypatch.setattr(setup, "_execute_setup_operation", execute)
    result = apply_approved_plan(
        plan, approved_fingerprint=plan["fingerprint"], config=_config(), provider=_Provider(),
        admin_credentials={"target": {"username": "TARGET_DBA", "password": "hidden-admin"}},
        connect=lambda **_kwargs: Connection(), fresh_plan=lambda: plan)
    assert result["status"] == "SETUP FAILED"
    assert result["completed"] == [create["operation_id"]]
    assert result["failed_operation"] == grant["operation_id"]
    assert result["oracle_error_code"] == "ORA-01017"
    assert "invalid username/password" in result["reason"]
    assert result["execution_stage"] == "operation execution"
    assert result["failed_scope"] == "TARGET"
    assert "rerun the same APPLY command" in result["corrective_action"]
    assert "hidden-admin" not in json.dumps(result)


def test_fresh_owner_apply_verifies_every_operation_and_repeat_is_noop(monkeypatch):
    import hashlib

    import migration_platform.oracle_setup_plan as setup

    plan, _ = _fresh_owner_plan(monkeypatch)
    users = {}
    quotas = {}
    system_grants = set()
    catalog_grants = set()
    statements = []

    class ApplyConnection:
        def __init__(self, user, service, is_sysdba=False):
            self.user = user.upper()
            self.service = service
            self.is_sysdba = is_sysdba

        def cursor(self):
            owner = self

            class Cursor:
                def __init__(self):
                    self.rows = []

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def execute(self, sql, binds=None):
                    normalized = " ".join(sql.split()).upper()
                    statements.append((owner.user, sql))
                    if "SYS_CONTEXT('USERENV','ISDBA')" in normalized:
                        self.rows = [("TRUE" if owner.is_sysdba else "FALSE",)]
                    elif normalized.startswith("SELECT SYS_CONTEXT"):
                        service = owner.service
                        self.rows = [(owner.user, owner.user, f"DB_{service}", "LOCALDB", "INSTANCE",
                                      f"PDB_{service}", service, "localhost")]
                    elif "FROM SESSION_PRIVS" in normalized:
                        self.rows = [(priv,) for priv in (
                            "CREATE USER", "ALTER USER", "GRANT ANY PRIVILEGE",
                            "GRANT ANY OBJECT PRIVILEGE")]
                    elif "FROM SESSION_ROLES" in normalized:
                        self.rows = [("PDB_DBA",)]
                    elif "FROM USER_SYS_PRIVS" in normalized or "FROM USER_ROLE_PRIVS" in normalized:
                        self.rows = []
                    elif "FROM DBA_USERS" in normalized:
                        status = users.get(str(binds["username"]).upper())
                        self.rows = [(status,)] if status else []
                    elif "FROM DBA_TS_QUOTAS" in normalized:
                        quota = quotas.get((str(binds["username"]).upper(),
                                            str(binds["tablespace"]).upper()))
                        self.rows = [(quota,)] if quota is not None else []
                    elif "FROM DBA_SYS_PRIVS" in normalized:
                        grant = (str(binds["username"]).upper(), str(binds["privilege"]).upper())
                        self.rows = [("YES" if "ADMIN_OPTION" in normalized else grant[1],)] if grant in system_grants else []
                    elif "FROM DBA_TAB_PRIVS" in normalized:
                        grant = (str(binds["username"]).upper(), str(binds["catalog_name"]).upper())
                        self.rows = ([] if "GRANTABLE='YES'" in normalized
                                     else [(1,)] if grant in catalog_grants else [])
                    elif normalized.startswith(("CREATE USER", "ALTER USER", "GRANT ")):
                        self.rows = []
                    else:
                        raise AssertionError(f"unexpected apply query: {sql}")

                def fetchall(self):
                    return self.rows

            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    execute = setup._execute_setup_operation

    def record_execute(conn, operation, provider):
        execute(conn, operation, provider)
        kind, account, params = (operation["operation_type"], operation["account"].upper(),
                                 operation["parameters"])
        if kind in {"CREATE_OWNER", "CREATE_EXECUTOR"}:
            users[account] = "OPEN"
        elif kind == "ASSIGN_FINITE_QUOTA":
            quotas[(account, params["tablespace"].upper())] = params["quota_bytes"]
        elif kind == "GRANT_SYSTEM_PRIVILEGE":
            system_grants.add((account, params["privilege"].upper()))
        elif kind == "GRANT_CATALOG_ACCESS":
            catalog_grants.add((account, params["view"].upper()))

    monkeypatch.setattr(setup, "_execute_setup_operation", record_execute)

    def no_changes_plan():
        payload = {"verified": "fresh owners provisioned"}
        return {"schema": "oracle-setup-plan/v1", "operations": [],
                "fingerprint_payload": payload,
                "fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
                "approval_ready": True, "plan_status": "NO CHANGES REQUIRED",
                "blockers": []}

    config = _config(security=True)
    config["source"]["connection"].update(username="SOURCE_OWNER", service_name="SOURCE_DB_NEW")
    config["target"]["connection"].update(username="TARGET_OWNER", service_name="TARGET_DB_NEW")
    credentials = {
        "source": {"username": "SOURCE_PDBADMIN", "password": "hidden-source"},
        "target": {"username": "TARGET_PDBADMIN", "password": "hidden-target"},
    }
    privileged_credentials = {
        "source": {"username": "SYS", "password": "hidden-source-sysdba"},
        "target": {"username": "SYS", "password": "hidden-target-sysdba"},
    }
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(
        SYSDBA="SYSDBA",
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}"))

    def connect_apply(**kwargs):
        user = kwargs["user"]
        service = kwargs["dsn"].rsplit("/", 1)[-1]
        return ApplyConnection(user, service, kwargs.get("mode") == "SYSDBA")

    result = apply_approved_plan(
        plan, approved_fingerprint=plan["fingerprint"], config=config,
        provider=_Provider(), admin_credentials=credentials,
        privileged_credentials=privileged_credentials, connect=connect_apply,
        fresh_plan=lambda: plan if not users else no_changes_plan())

    assert result["status"] == "SETUP COMPLETE"
    assert set(result["completed"]) == {op["operation_id"] for op in plan["operations"]}
    assert any(sql.lstrip().upper().startswith("CREATE USER") and "SOURCE_OWNER" in sql.upper()
               for _, sql in statements)
    assert any(sql.lstrip().upper().startswith("CREATE USER") and "TARGET_OWNER" in sql.upper()
               for _, sql in statements)
    assert all(not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
               for _, sql in statements)
    assert all(secret not in json.dumps(result) for secret in (
        "hidden-source", "hidden-target", "hidden-source-sysdba", "hidden-target-sysdba"))

    no_op = no_changes_plan()
    repeat = apply_approved_plan(
        no_op, approved_fingerprint=no_op["fingerprint"], config=config,
        provider=_Provider(), admin_credentials=credentials,
        connect=lambda **_kwargs: pytest.fail("no-op repeat must not connect"),
        fresh_plan=no_changes_plan)
    assert repeat["status"] == "SETUP COMPLETE"
    assert repeat["completed"] == []
