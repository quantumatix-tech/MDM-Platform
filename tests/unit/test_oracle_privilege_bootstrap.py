"""Oracle FULL privilege policy and bootstrap safety contracts."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from core.connectors.oracle import objects
from core.connectors.oracle.target import OracleTargetConnector


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        self.conn.statements.append(sql)
        if sql.startswith("GRANT ") and self.conn.grant_error:
            error = self.conn.grant_error
            if callable(error):
                error = error(sql)
            if error:
                raise error

    def fetchone(self):
        if "SYS_CONTEXT" in self.sql:
            return (
                self.conn.username, self.conn.db, self.conn.pdb,
                self.conn.service,
            )
        return (self.conn.username,)

    def fetchall(self):
        query = " ".join(self.sql.upper().split())
        if "SELECT PRIVILEGE, ADMIN_OPTION FROM USER_SYS_PRIVS" in query:
            return self.conn.admin_privileges
        if "SELECT PRIVILEGE FROM USER_SYS_PRIVS" in query:
            return self.conn.direct_privileges
        if "FROM SESSION_PRIVS" in query:
            return self.conn.effective_privileges
        if "FROM SESSION_ROLES" in query:
            return self.conn.roles
        return []


class _Connection:
    def __init__(self, username, *, pdb="PDB1", service="svc", db="DB1",
                 direct=(), admin_privileges=(), effective_privileges=(), roles=(),
                 grant_error=None):
        self.username = username
        self.pdb = pdb
        self.service = service
        self.db = db
        self.direct_privileges = list(direct)
        self.admin_privileges = list(admin_privileges)
        self.effective_privileges = list(effective_privileges)
        self.roles = list(roles)
        self.grant_error = grant_error
        self.statements = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def _target_conn(username="TARGET_OWNER", **kwargs):
    return _Connection(username, **kwargs)


def _admin_config(username="BOOTSTRAP_ADMIN"):
    return {
        "host": "db.example", "port": 1521, "service_name": "svc",
        "username": username, "password": "secret-value",
    }


def _install_driver(monkeypatch, admin_conn):
    driver = SimpleNamespace(
        connect=lambda **_kwargs: admin_conn,
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}",
        Error=Exception,
    )
    monkeypatch.setitem(sys.modules, "oracledb", driver)


def test_required_privileges_derive_from_inventory_and_mark_compile_grants_direct():
    requirements = objects.required_owner_privileges(
        tables={"T"}, views=[object()], materialized_views=[object()],
        sequences=[object()], routines=[object()], triggers=[object()],
        synonyms=[object()],
    )

    assert requirements == {
        "CREATE MATERIALIZED VIEW": True,
        "CREATE PROCEDURE": True,
        "CREATE SEQUENCE": False,
        "CREATE SYNONYM": False,
        "CREATE TABLE": True,
        "CREATE TRIGGER": True,
        "CREATE VIEW": False,
    }


def test_inspection_accepts_all_available_privileges_including_enabled_roles():
    conn = _Connection(
        "TARGET_OWNER",
        direct=[("CREATE MATERIALIZED VIEW",),
                ("CREATE PROCEDURE",), ("CREATE TRIGGER",), ("CREATE TABLE",)],
        effective_privileges=[("CREATE TABLE",), ("CREATE VIEW",),
                              ("CREATE SEQUENCE",), ("CREATE SYNONYM",)],
        roles=[("APP_ROLE",)],
    )
    needs = {privilege: direct for privilege, direct in {
        "CREATE TABLE": True, "CREATE VIEW": False,
        "CREATE MATERIALIZED VIEW": True, "CREATE PROCEDURE": True,
        "CREATE TRIGGER": True, "CREATE SEQUENCE": False,
        "CREATE SYNONYM": False,
    }.items()}

    report = objects.inspect_privileges(conn, needs)

    assert report["missing"] == []
    assert report["enabled_roles"] == ["APP_ROLE"]


def test_inspection_reports_one_missing_privilege_and_safe_grant_proposal():
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _Connection("TARGET_OWNER")

    report = target.inspect_object_privileges(
        tables=set(), views=[], materialized_views=[], sequences=[], routines=[],
        triggers=[], synonyms=[object()],
    )

    assert report["missing"] == ["CREATE SYNONYM"]
    assert report["proposed_grants"] == [
        'GRANT CREATE SYNONYM TO "TARGET_OWNER";'
    ]


def test_role_only_stored_program_privileges_remain_missing_and_propose_exact_grants():
    conn = _Connection(
        "TARGET_OWNER",
        effective_privileges=[("CREATE PROCEDURE",), ("CREATE TRIGGER",)],
        roles=[("APP_ROLE",)],
    )

    with pytest.raises(RuntimeError, match="direct grants required") as exc_info:
        objects.validate_privileges(
            conn, routines=[object()], triggers=[object()],
            target_owner='TARGET_OWNER',
        )

    message = str(exc_info.value)
    assert "GRANT CREATE PROCEDURE TO \"TARGET_OWNER\"" in message
    assert "GRANT CREATE TRIGGER TO \"TARGET_OWNER\"" in message


def test_authorized_bootstrap_grants_only_allowlisted_privileges(monkeypatch):
    target_conn = _target_conn()
    admin_conn = _Connection(
        "BOOTSTRAP_ADMIN",
        admin_privileges=[("CREATE SYNONYM", "YES"), ("CREATE SEQUENCE", "YES")],
    )
    _install_driver(monkeypatch, admin_conn)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = target_conn

    granted = target.bootstrap_object_privileges(
        ["CREATE SYNONYM"], _admin_config(), target_owner="TARGET_OWNER"
    )

    assert granted == ["CREATE SYNONYM"]
    assert [sql for sql in admin_conn.statements if sql.startswith("GRANT ")] == [
        'GRANT CREATE SYNONYM TO "TARGET_OWNER"'
    ]
    assert admin_conn.closed


def test_wrong_pdb_is_rejected_before_any_grant(monkeypatch):
    admin_conn = _Connection(
        "BOOTSTRAP_ADMIN", pdb="OTHER_PDB",
        admin_privileges=[("CREATE SYNONYM", "YES")],
    )
    _install_driver(monkeypatch, admin_conn)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _target_conn()

    with pytest.raises(RuntimeError, match="different database, PDB, or service"):
        target.bootstrap_object_privileges(
            ["CREATE SYNONYM"], _admin_config(), target_owner="TARGET_OWNER"
        )

    assert not any(sql.startswith("GRANT ") for sql in admin_conn.statements)


def test_bootstrap_requires_direct_admin_authority(monkeypatch):
    admin_conn = _Connection(
        "BOOTSTRAP_ADMIN", admin_privileges=[("CREATE SYNONYM", "NO")]
    )
    _install_driver(monkeypatch, admin_conn)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _target_conn()

    with pytest.raises(RuntimeError, match="not authorized to grant"):
        target.bootstrap_object_privileges(
            ["CREATE SYNONYM"], _admin_config(), target_owner="TARGET_OWNER"
        )

    assert not any(sql.startswith("GRANT ") for sql in admin_conn.statements)


def test_broad_grant_any_privilege_does_not_substitute_for_specific_admin_option(monkeypatch):
    admin_conn = _Connection(
        "BOOTSTRAP_ADMIN", admin_privileges=[("GRANT ANY PRIVILEGE", "NO")]
    )
    _install_driver(monkeypatch, admin_conn)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _target_conn()

    with pytest.raises(RuntimeError, match="specific privilege to be directly granted"):
        target.bootstrap_object_privileges(
            ["CREATE SYNONYM"], _admin_config(), target_owner="TARGET_OWNER"
        )
    assert not any(sql.startswith("GRANT ") for sql in admin_conn.statements)


def test_invalid_owner_and_unsupported_privilege_fail_closed(monkeypatch):
    admin_conn = _Connection("BOOTSTRAP_ADMIN")
    _install_driver(monkeypatch, admin_conn)
    invalid_target = OracleTargetConnector({"username": ""})
    invalid_target._conn = _target_conn()

    with pytest.raises(ValueError):
        invalid_target.bootstrap_object_privileges(
            ["CREATE SYNONYM"], _admin_config()
        )
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _target_conn()
    with pytest.raises(RuntimeError, match="unsupported"):
        target.bootstrap_object_privileges(
            ["CREATE ANY TABLE"], _admin_config(), target_owner="TARGET_OWNER"
        )

    assert not any(sql.startswith("GRANT ") for sql in admin_conn.statements)


def test_quoted_identifier_content_is_escaped_in_grant_statement(monkeypatch):
    unusual_owner = 'TARGET"; DBA'
    target_conn = _target_conn(username=unusual_owner)
    admin_conn = _Connection(
        "BOOTSTRAP_ADMIN", admin_privileges=[("CREATE SYNONYM", "YES")]
    )
    _install_driver(monkeypatch, admin_conn)
    target = OracleTargetConnector({"username": unusual_owner})
    target._conn = target_conn

    target.bootstrap_object_privileges(
        ["CREATE SYNONYM"], _admin_config(), target_owner=unusual_owner
    )

    assert admin_conn.statements[-1] == 'GRANT CREATE SYNONYM TO "TARGET""; DBA"'


def test_partial_bootstrap_failure_reports_prior_grant_and_closes_admin(monkeypatch):
    denied_admin = _Connection(
        "BOOTSTRAP_ADMIN",
        admin_privileges=[("CREATE SEQUENCE", "YES"), ("CREATE SYNONYM", "YES")],
        grant_error=lambda sql: RuntimeError("denied")
        if sql.endswith('TO "TARGET_OWNER"') and "CREATE SYNONYM" in sql else None,
    )
    _install_driver(monkeypatch, denied_admin)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _target_conn()

    with pytest.raises(RuntimeError, match="already granted in this attempt: CREATE SEQUENCE"):
        target.bootstrap_object_privileges(
            ["CREATE SYNONYM", "CREATE SEQUENCE"],
            _admin_config(), target_owner="TARGET_OWNER"
        )

    assert denied_admin.closed
    assert 'GRANT CREATE SEQUENCE TO "TARGET_OWNER"' in denied_admin.statements
    assert 'GRANT CREATE SYNONYM TO "TARGET_OWNER"' in denied_admin.statements


def test_bootstrap_audit_does_not_include_secret(monkeypatch):
    admin_conn = _Connection(
        "BOOTSTRAP_ADMIN", admin_privileges=[("CREATE SYNONYM", "YES")]
    )
    _install_driver(monkeypatch, admin_conn)
    audit = MagicMock()
    monkeypatch.setattr("core.connectors.oracle.target.audit_log", audit)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = _target_conn()

    target.bootstrap_object_privileges(
        ["CREATE SYNONYM"], _admin_config(), target_owner="TARGET_OWNER"
    )

    assert "secret-value" not in repr(audit.call_args_list)


def test_orchestrator_requires_approval_and_rechecks_before_continuing(monkeypatch):
    from core.orchestrator import MigrationOrchestrator

    class Resolver:
        def resolve(self, name):
            assert name == "bootstrap_password"
            return "not-for-reports"

    class Target:
        def __init__(self):
            self.reports = [
                {"missing": ["CREATE SYNONYM"], "proposed_grants": [
                    'GRANT CREATE SYNONYM TO "TARGET_OWNER";'
                ]},
                {"missing": [], "proposed_grants": []},
            ]
            self.grants = []
            self.inspect_calls = 0

        def bootstrap_object_privileges(self, privileges, _admin, *, target_owner):
            self.grants.append((privileges, target_owner))
            return privileges

        def inspect_object_privileges(self, **_inventory):
            self.inspect_calls += 1
            return self.reports.pop(0)

    orch = MigrationOrchestrator.__new__(MigrationOrchestrator)
    orch._config = {
        "target": {"connection": {"username": "TARGET_OWNER"}},
        "oracle_privilege_bootstrap": {
            "enabled": True,
            "approved_privileges": ["CREATE SYNONYM"],
            "admin_connection": {
                "host": "db.example", "port": 1521, "service_name": "svc",
                "username": "BOOTSTRAP_ADMIN", "password_secret": "bootstrap_password",
            },
        },
    }
    orch._target = Target()
    orch._target_secret_resolver = Resolver()
    orch._secret_resolver = None
    inventory = {
        "tables": {"T"}, "views": [], "materialized_views": [], "sequences": [],
        "routines": [], "triggers": [], "synonyms": [],
    }
    audit = MagicMock()
    monkeypatch.setattr("core.orchestrator.audit_log", audit)

    preflight = orch._inspect_oracle_privileges(inventory)
    assert preflight["missing"] == ["CREATE SYNONYM"]
    first = orch._bootstrap_oracle_privileges(inventory, preflight)
    second = orch._bootstrap_oracle_privileges(
        inventory, {"missing": [], "proposed_grants": []}
    )

    assert first["status"] == "completed"
    assert second == {"status": "not_required", "granted": []}
    assert orch._target.grants == [(["CREATE SYNONYM"], "TARGET_OWNER")]
    assert orch._target.inspect_calls == 2
    assert "not-for-reports" not in repr((first, second, audit.call_args_list))


def test_orchestrator_refuses_missing_authorization_or_admin_identity():
    from core.orchestrator import MigrationOrchestrator

    orch = MigrationOrchestrator.__new__(MigrationOrchestrator)
    orch._config = {"oracle_privilege_bootstrap": {"enabled": True}}
    with pytest.raises(RuntimeError, match="not authorized"):
        orch._bootstrap_oracle_privileges(
            {}, {"missing": ["CREATE SYNONYM"], "proposed_grants": []}
        )
    orch._config["oracle_privilege_bootstrap"]["approved_privileges"] = ["CREATE SYNONYM"]
    orch._config["oracle_privilege_bootstrap"]["admin_connection"] = {}
    with pytest.raises(RuntimeError, match="admin_connection is missing"):
        orch._bootstrap_oracle_privileges(
            {}, {"missing": ["CREATE SYNONYM"], "proposed_grants": []}
        )


def test_orchestrator_rejects_non_list_approved_privileges_as_type_error():
    from core.orchestrator import MigrationOrchestrator

    orch = MigrationOrchestrator.__new__(MigrationOrchestrator)
    orch._config = {
        "oracle_privilege_bootstrap": {
            "enabled": True,
            "approved_privileges": "CREATE SYNONYM",
        }
    }

    with pytest.raises(TypeError, match="approved_privileges must be a list"):
        orch._bootstrap_oracle_privileges(
            {}, {"missing": ["CREATE SYNONYM"], "proposed_grants": []}
        )


def test_missing_privilege_blocks_full_before_cleanup():
    from core.connectors.base import Column, Schema, SourceConnector, TargetConnector
    from core.orchestrator import MigrationOrchestrator

    events = []

    class Source(SourceConnector):
        _config: ClassVar[dict[str, str]] = {"username": "SOURCE_OWNER"}
        def connect(self): pass
        def close(self): pass
        def list_objects(self): return ["T"]
        def get_object_count(self, _name, schema_name=None): return 0
        def export_full(self, _name, schema_name=None): return iter(())
        def get_schema(self, _name):
            return Schema("T", schema_name="SOURCE_OWNER", columns=[Column("ID", "NUMBER")])
        def validate_schema_replacement_objects(self): pass
        def validate_schema_replacement_grants(self): pass
        def list_comments(self): return []
        def list_views(self): return []
        def list_materialized_views(self): return []
        def list_all_sequences(self): return []
        def list_functions(self): return []
        def get_all_triggers(self): return []
        def list_synonyms(self): return []

    class Target(TargetConnector):
        _config: ClassVar[dict[str, str]] = {
            "username": "TARGET_OWNER", "source_engine": "oracle"
        }
        def connect(self): pass
        def close(self): pass
        def ensure_database_exists(self): pass
        def create_object_if_missing(self, _schema): return "created"
        def upsert_batch(self, _name, rows, schema=None):
            from core.connectors.base import UpsertResult
            return UpsertResult(success_count=len(list(rows)))
        def delete(self, _name, _document, schema=None): pass
        def get_object_count(self, _name, schema_name=None): return 0
        def export_full(self, _name, schema_name=None): return iter(())
        def validate_all_constraint_states(self): pass
        def plan_schema_replacement(self, **_inventory):
            events.append("plan")
            return SimpleNamespace(
                blockers=(), inspection_errors=(),
                as_dict=lambda: {"status": "ready"},
            )
        def inspect_object_privileges(self, **_inventory):
            events.append("privilege-inspection")
            return {
                "missing": ["CREATE SYNONYM"],
                "proposed_grants": ['GRANT CREATE SYNONYM TO "TARGET_OWNER";'],
            }
        def apply_schema_replacement(self, *_args, **_kwargs):
            events.append("cleanup")
            raise AssertionError("cleanup must not run with missing privileges")

    result = MigrationOrchestrator(Source(), Target(), {
        "source": {"engine": "oracle", "connection": {"username": "SOURCE_OWNER"}},
        "target": {"engine": "oracle", "connection": {"username": "TARGET_OWNER"}},
        "migration": {"mode": "full"},
        "validation": {"mode": "count"},
    }).run_full()

    assert result["status"] == "failed"
    assert events == ["plan", "privilege-inspection"]


def test_unsupported_privilege_inventory_remains_fail_closed():
    with pytest.raises(RuntimeError, match="refused unsupported privilege"):
        objects.inspect_privileges(_Connection("TARGET_OWNER"), {"CREATE ANY TABLE": False})
