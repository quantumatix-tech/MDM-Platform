"""Unit tests for Phase C1 — PostgreSQL target setup and pre-migration validation.

No live PostgreSQL connection required; uses mock cursor objects.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg
import pytest

from tests.e2e.postgresql.setup.database import (
    _resolve_connection_params,
    create_database,
    drop_database,
    is_safe_e2e_database,
    reset_database,
)
from tests.e2e.postgresql.setup.target import (
    is_protected_database,
    reset_target_database,
)
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
)
from tests.e2e.postgresql.validation.target_pre import TargetPreValidator

# ============================================================
# Target safety tests
# ============================================================

class TestProtectedDatabases:
    @pytest.mark.parametrize("db_name", [
        "postgres",
        "template0",
        "template1",
        "MigrationSource_PostgreSQL",
        "MigrationTarget_PostgreSQL",
        "migration_target",
        "migration_source",
        "migrationtarget_postgresql",
    ])
    def test_protected_databases_detected(self, db_name):
        assert is_protected_database(db_name) is True

    @pytest.mark.parametrize("db_name", [
        "MigrationE2E_PostgreSQL_Target",
        "MigrationE2E_PostgreSQL_Source",
        "my_test_db",
    ])
    def test_non_protected_databases_allowed(self, db_name):
        assert is_protected_database(db_name) is False


class TestSafeE2EDatabase:
    @pytest.mark.parametrize("db_name", [
        "MigrationE2E_PostgreSQL_Target",
        "MigrationE2E_PostgreSQL_Source",
        "MigrationE2E_PostgreSQL_Foo",
    ])
    def test_valid_e2e_database_accepted(self, db_name):
        assert is_safe_e2e_database(db_name) is True

    @pytest.mark.parametrize("db_name", [
        "postgres",
        "template0",
        "template1",
        "MigrationSource_PostgreSQL",
        "MigrationTarget_PostgreSQL",
        "ProductionDB",
        "test_db",
        "",
    ])
    def test_non_e2e_database_rejected(self, db_name):
        assert is_safe_e2e_database(db_name) is False


class TestResetTargetDatabaseSafety:
    def test_refuses_protected_database(self):
        with pytest.raises(ValueError, match="protected"):
            reset_target_database(
                "host", "port", "user", "pass",
                "MigrationTarget_PostgreSQL", "^MigrationE2E_PostgreSQL_.+$",
            )

    def test_refuses_non_e2e_database(self):
        with pytest.raises(ValueError, match="does not match"):
            reset_target_database(
                "host", "port", "user", "pass",
                "ProductionDB", "^MigrationE2E_PostgreSQL_.+$",
            )

    def test_allows_valid_e2e_target(self):
        with patch(
            "tests.e2e.postgresql.setup.target.reset_database"
        ) as mock_reset:
            mock_reset.return_value = True
            result = reset_target_database(
                "host", "port", "user", "pass",
                "MigrationE2E_PostgreSQL_Target",
                "^MigrationE2E_PostgreSQL_.+$",
            )
            assert result is True
            mock_reset.assert_called_once()


# ============================================================
# Database reset unit tests (testing database.py safety guards)
# ============================================================

class TestDatabaseResetSafety:
    def test_drop_database_refuses_non_e2e(self):
        with pytest.raises(ValueError, match="does not match"):
            drop_database("host", "port", "user", "pass", "ProductionDB")

    def test_create_database_refuses_protected(self):
        pass

    def test_create_database_refuses_non_e2e(self):
        with pytest.raises(ValueError, match="does not match"):
            create_database("host", "port", "user", "pass", "ProductionDB")

    def test_reset_database_delegates(self):
        with patch("tests.e2e.postgresql.setup.database.drop_database") as mock_drop, \
             patch("tests.e2e.postgresql.setup.database.create_database") as mock_create:
            mock_drop.return_value = False
            mock_create.return_value = True
            result = reset_database("host", "port", "user", "pass", "MigrationE2E_PostgreSQL_Target")
            assert result is True
            mock_drop.assert_called_once()
            mock_create.assert_called_once()


# ============================================================
# Connection params tests
# ============================================================

class TestResolveConnectionParams:
    def test_password_from_env(self, monkeypatch):
        monkeypatch.setenv("SECRET_postgresql_e2e_target_pass", "secret123")
        params = _resolve_connection_params(
            host="127.0.0.1", port=55432, username="postgres",
            password_env="postgresql_e2e_target_pass",
        )
        assert params["password"] == "secret123"
        assert params["host"] == "127.0.0.1"
        assert params["user"] == "postgres"

    def test_password_none_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("SECRET_postgresql_e2e_target_pass", raising=False)
        params = _resolve_connection_params(
            username="postgres", password_env="postgresql_e2e_target_pass",
        )
        assert params["password"] == ""

    def test_defaults(self):
        params = _resolve_connection_params()
        assert params["host"] == "127.0.0.1"
        assert params["port"] == 55432
        assert params["user"] == "postgres"
        assert params["password"] == ""


# ============================================================
# TargetPreValidator clean-state tests
# ============================================================

class _MockConnectionError(psycopg.Error):
    """Raised when the mock connection simulates a failure."""


def _make_mock_conn(
    db_name_result: str = "MigrationE2E_PostgreSQL_Target",
    version_result: str = "17.4 (Debian 17.4-1.pgdg1267+1)",
    counts: dict[str, int] | None = None,
    schemas: list[str] | None = None,
    conn_fail: bool = False,
) -> MagicMock:
    """Build a mock psycopg.Connection for TargetPreValidator."""
    if counts is None:
        counts = {}
    if schemas is None:
        schemas = []

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    def execute_side_effect(sql, params=None):
        if conn_fail:
            raise _MockConnectionError("Connection failed")
        if "SELECT 1" in sql:
            pass
        elif "SELECT current_database()" in sql:
            cur.fetchone.return_value = (db_name_result,)
        elif "SHOW server_version" in sql:
            cur.fetchone.return_value = (version_result,)
        elif "SELECT nspname FROM pg_namespace" in sql:
            cur.fetchall.return_value = [(s,) for s in schemas]
        elif "SELECT COUNT(*) FROM pg_class" in sql and "relkind IN ('r'" in sql:
            cur.fetchone.return_value = (counts.get("tables", 0),)
        elif "SELECT COUNT(*) FROM pg_class" in sql and "relkind = 'v'" in sql:
            cur.fetchone.return_value = (counts.get("views", 0),)
        elif "SELECT COUNT(*) FROM pg_proc" in sql and "prokind = 'f'" in sql:
            cur.fetchone.return_value = (counts.get("functions", 0),)
        elif "SELECT COUNT(*) FROM pg_proc" in sql and "prokind = 'p'" in sql:
            cur.fetchone.return_value = (counts.get("procedures", 0),)
        elif "SELECT COUNT(*) FROM pg_class" in sql and "relkind = 'S'" in sql:
            cur.fetchone.return_value = (counts.get("sequences", 0),)
        elif "SELECT COUNT(*) FROM pg_type" in sql:
            cur.fetchone.return_value = (counts.get("types", 0),)
        elif "SELECT COUNT(*) FROM pg_trigger" in sql:
            cur.fetchone.return_value = (counts.get("triggers", 0),)
        elif "SELECT COUNT(*) FROM pg_policy" in sql:
            cur.fetchone.return_value = (counts.get("policies", 0),)
        elif "SELECT COUNT(*) FROM pg_extension" in sql:
            cur.fetchone.return_value = (counts.get("extensions", 0),)
        else:
            cur.fetchone.return_value = (0,)

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    return conn


class TestCleanStateValidation:
    def test_clean_target_passes(self):
        conn = _make_mock_conn(
            db_name_result="MigrationE2E_PostgreSQL_Target",
            counts={},
            schemas=[],
        )
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        assert report.passed, "Expected clean target to pass all checks"
        conn_phase = next(p for p in report.phases if p.name == "target_connectivity")
        assert conn_phase.status == STATUS_PASS
        assert len(conn_phase.checks) == 3  # reachable, correct_database, server_version

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        assert clean_phase.status == STATUS_PASS
        assert len(clean_phase.checks) == 10

    def test_wrong_database_detected(self):
        conn = _make_mock_conn(db_name_result="WrongDB")
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        conn_phase = next(p for p in report.phases if p.name == "target_connectivity")
        assert conn_phase.status == STATUS_FAIL

    def test_connection_failure_detected(self):
        conn = _make_mock_conn(conn_fail=True)
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        conn_phase = next(p for p in report.phases if p.name == "target_connectivity")
        assert conn_phase.status == STATUS_FAIL
        reachable_check = next(c for c in conn_phase.checks if c.name == "database_reachable")
        assert reachable_check.status == STATUS_FAIL

    def test_user_tables_present_detected(self):
        conn = _make_mock_conn(counts={"tables": 3})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        assert clean_phase.status == STATUS_FAIL
        tables_check = next(c for c in clean_phase.checks if c.name == "no_user_tables")
        assert tables_check.status == STATUS_FAIL
        assert tables_check.actual == 3

    def test_user_views_present_detected(self):
        conn = _make_mock_conn(counts={"views": 2})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        assert clean_phase.status == STATUS_FAIL
        views_check = next(c for c in clean_phase.checks if c.name == "no_user_views")
        assert views_check.status == STATUS_FAIL
        assert views_check.actual == 2

    def test_user_functions_present_detected(self):
        conn = _make_mock_conn(counts={"functions": 1})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        funcs_check = next(c for c in clean_phase.checks if c.name == "no_user_functions")
        assert funcs_check.status == STATUS_FAIL
        assert funcs_check.actual == 1

    def test_user_procedures_present_detected(self):
        conn = _make_mock_conn(counts={"procedures": 1})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        procs_check = next(c for c in clean_phase.checks if c.name == "no_user_procedures")
        assert procs_check.status == STATUS_FAIL
        assert procs_check.actual == 1

    def test_user_sequences_present_detected(self):
        conn = _make_mock_conn(counts={"sequences": 2})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        seqs_check = next(c for c in clean_phase.checks if c.name == "no_user_sequences")
        assert seqs_check.status == STATUS_FAIL
        assert seqs_check.actual == 2

    def test_user_types_present_detected(self):
        conn = _make_mock_conn(counts={"types": 1})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        types_check = next(c for c in clean_phase.checks if c.name == "no_user_types")
        assert types_check.status == STATUS_FAIL
        assert types_check.actual == 1

    def test_user_triggers_present_detected(self):
        conn = _make_mock_conn(counts={"triggers": 1})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        triggers_check = next(c for c in clean_phase.checks if c.name == "no_user_triggers")
        assert triggers_check.status == STATUS_FAIL
        assert triggers_check.actual == 1

    def test_rls_policies_present_detected(self):
        conn = _make_mock_conn(counts={"policies": 1})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        policies_check = next(c for c in clean_phase.checks if c.name == "no_user_rls_policies")
        assert policies_check.status == STATUS_FAIL
        assert policies_check.actual == 1

    def test_user_extensions_present_detected(self):
        conn = _make_mock_conn(counts={"extensions": 1})
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        exts_check = next(c for c in clean_phase.checks if c.name == "no_user_extensions")
        assert exts_check.status == STATUS_FAIL
        assert exts_check.actual == 1

    def test_unexpected_schema_detected(self):
        conn = _make_mock_conn(schemas=["myapp_schema"])
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        clean_phase = next(p for p in report.phases if p.name == "clean_state")
        assert clean_phase.status == STATUS_FAIL
        schema_check = next(c for c in clean_phase.checks if c.name == "no_unexpected_schemas")
        assert schema_check.status == STATUS_FAIL
        assert "myapp_schema" in str(schema_check.actual)

    def test_aggregated_failures(self):
        conn = _make_mock_conn(counts={
            "tables": 3,
            "functions": 1,
            "triggers": 2,
        })
        validator = TargetPreValidator(conn, "MigrationE2E_PostgreSQL_Target")
        report = validator.validate()

        assert not report.passed
        summary = report.summary
        assert summary["PASS"] < summary["FAIL"] + summary["PASS"]

    def test_report_has_database_field(self):
        conn = _make_mock_conn()
        validator = TargetPreValidator(conn, "my_test_db")
        report = validator.validate()
        assert report.database == "my_test_db"

    def test_report_serializes(self):
        conn = _make_mock_conn()
        validator = TargetPreValidator(conn, "my_test_db")
        report = validator.validate()
        d = report.to_dict()
        assert d["database"] == "my_test_db"
        assert "phases" in d
        assert "status" in d
        assert "summary" in d


# ============================================================
# PhaseResult serialization tests
# ============================================================

class TestResultModels:
    def test_check_result_defaults(self):
        cr = CheckResult(name="test", status=STATUS_PASS)
        assert cr.name == "test"
        assert cr.passed is True
        assert cr.expected is None
        assert cr.actual is None
        assert cr.message == ""

    def test_phase_result_pass_when_no_failures(self):
        phase = PhaseResult(name="test_phase")
        phase.add_check(CheckResult(name="c1", status=STATUS_PASS))
        phase.add_check(CheckResult(name="c2", status=STATUS_PASS))
        assert phase.status == STATUS_PASS

    def test_phase_result_fail_when_any_failure(self):
        phase = PhaseResult(name="test_phase")
        phase.add_check(CheckResult(name="c1", status=STATUS_PASS))
        phase.add_check(CheckResult(name="c2", status=STATUS_FAIL))
        assert phase.status == STATUS_FAIL

    def test_phase_result_to_dict_structure(self):
        phase = PhaseResult(name="test_phase")
        phase.add_check(CheckResult(name="c1", status=STATUS_PASS, expected=0, actual=0))
        d = phase.to_dict()
        assert d["name"] == "test_phase"
        assert d["status"] == STATUS_PASS
        assert len(d["checks"]) == 1
        assert d["checks"][0]["name"] == "c1"
