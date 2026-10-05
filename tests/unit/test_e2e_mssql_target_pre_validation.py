"""Unit tests for Phase C1 — target setup and pre-migration validation.

No live MSSQL connection required; uses mock cursor objects.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.e2e.mssql.setup.target import (
    get_database_state,
    is_protected_database,
    reset_target_database,
)
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
)
from tests.e2e.mssql.validation.target_pre import TargetPreValidator

# ============================================================
# Target safety tests
# ============================================================

class TestProtectedDatabases:
    @pytest.mark.parametrize("db_name", [
        "master",
        "model",
        "msdb",
        "tempdb",
        "MigrationSource_MSSQL",
        "MigrationTarget_MSSQL",
    ])
    def test_protected_databases_detected(self, db_name):
        assert is_protected_database(db_name) is True

    @pytest.mark.parametrize("db_name", [
        "MigrationE2E_MSSQL_Target",
        "MigrationE2E_MSSQL_Source",
        "my_test_db",
    ])
    def test_non_protected_databases_allowed(self, db_name):
        assert is_protected_database(db_name) is False


class TestResetTargetDatabaseSafety:
    def test_refuses_protected_database(self):
        with pytest.raises(ValueError, match="protected"):
            reset_target_database(
                "host", "port", "user", "pass",
                "MigrationTarget_MSSQL", "^MigrationE2E_MSSQL_.+$",
            )

    def test_refuses_non_e2e_database(self):
        with pytest.raises(ValueError, match="does not match"):
            reset_target_database(
                "host", "port", "user", "pass",
                "ProductionDB", "^MigrationE2E_MSSQL_.+$",
            )

    def test_allows_valid_e2e_target(self):
        from unittest.mock import patch
        with patch(
            "tests.e2e.mssql.setup.target.reset_database"
        ) as mock_reset:
            mock_reset.return_value = True
            result = reset_target_database(
                "host", "port", "user", "pass",
                "MigrationE2E_MSSQL_Target",
                "^MigrationE2E_MSSQL_.+$",
            )
            assert result is True
            mock_reset.assert_called_once()


# ============================================================
# Get database state tests
# ============================================================

class TestGetDatabaseState:
    def test_returns_state_desc(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = ("ONLINE",)
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        state = get_database_state(conn, "testdb")
        assert state == "ONLINE"

    def test_returns_none_when_db_missing(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        state = get_database_state(conn, "nonexistent")
        assert state is None


def _make_mock_conn(
    db_name_result: str = "MigrationE2E_MSSQL_Target",
    state_result: str = "ONLINE",
    counts: dict[str, int] | None = None,
    schemas: list[str] | None = None,
) -> MagicMock:
    """Build a mock pyodbc.Connection for TargetPreValidator."""
    if counts is None:
        counts = {}
    if schemas is None:
        schemas = []

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    def execute_side_effect(sql, params=None):
        if "SELECT DB_NAME()" in sql:
            cur.fetchone.return_value = (db_name_result,)
        elif "SELECT state_desc FROM sys.databases" in sql:
            cur.fetchone.return_value = (state_result,)
        elif "SELECT 1" in sql:
            pass
        elif "SELECT COUNT(*)" in sql:
            for check_name, query, _ in [
                ("no_user_tables",
                 "SELECT COUNT(*) FROM sys.tables", 0),
                ("no_user_views",
                 "SELECT COUNT(*) FROM sys.views", 0),
                ("no_user_procedures",
                 "SELECT COUNT(*) FROM sys.objects WHERE type = 'P'", 0),
                ("no_user_functions",
                 "SELECT COUNT(*) FROM sys.objects "
                 "WHERE type IN ('FN','IF','TF')", 0),
                ("no_user_triggers",
                 "SELECT COUNT(*) FROM sys.triggers", 0),
                ("no_user_sequences",
                 "SELECT COUNT(*) FROM sys.sequences", 0),
                ("no_user_synonyms",
                 "SELECT COUNT(*) FROM sys.synonyms", 0),
                ("no_user_defined_types",
                 "SELECT COUNT(*) FROM sys.types "
                 "WHERE is_user_defined = 1", 0),
            ]:
                if check_name in counts:
                    cur.fetchone.return_value = (counts[check_name],)
                    return
            cur.fetchone.return_value = (0,)
        elif "SELECT name FROM sys.schemas" in sql:
            cur.fetchall.return_value = [(s,) for s in schemas]
        else:
            cur.fetchone.return_value = (0,)

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    return conn


# ============================================================
# TargetPreValidator clean-state tests
# ============================================================

class TestCleanStateValidation:
    def test_clean_target_passes(self):
        conn = _make_mock_conn(
            db_name_result="MigrationE2E_MSSQL_Target",
            state_result="ONLINE",
            counts={},
            schemas=[],
        )
        validator = TargetPreValidator(
            conn, "MigrationE2E_MSSQL_Target"
        )
        report = validator.validate()

        assert report.passed, "Expected clean target to pass all checks"
        conn_phase = next(
            p for p in report.phases if p.name == "target_connectivity"
        )
        assert conn_phase.status == STATUS_PASS
        assert len(conn_phase.checks) == 3

        clean_phase = next(
            p for p in report.phases if p.name == "clean_state"
        )
        assert clean_phase.status == STATUS_PASS
        assert len(clean_phase.checks) == 9

    def test_wrong_database_detected(self):
        conn = _make_mock_conn(db_name_result="WrongDB")
        validator = TargetPreValidator(conn, "MigrationE2E_MSSQL_Target")
        report = validator.validate()

        conn_phase = next(
            p for p in report.phases if p.name == "target_connectivity"
        )
        assert conn_phase.status == STATUS_FAIL

    def test_offline_database_detected(self):
        conn = _make_mock_conn(state_result="OFFLINE")
        validator = TargetPreValidator(conn, "MigrationE2E_MSSQL_Target")
        report = validator.validate()

        conn_phase = next(
            p for p in report.phases if p.name == "target_connectivity"
        )
        offline_check = next(
            c for c in conn_phase.checks if c.name == "database_online"
        )
        assert offline_check.status == STATUS_FAIL

    def test_user_tables_present_detected(self):
        conn = _make_mock_conn(counts={"no_user_tables": 3})
        validator = TargetPreValidator(conn, "MigrationE2E_MSSQL_Target")
        report = validator.validate()

        clean_phase = next(
            p for p in report.phases if p.name == "clean_state"
        )
        assert clean_phase.status == STATUS_FAIL
        tables_check = next(
            c for c in clean_phase.checks if c.name == "no_user_tables"
        )
        assert tables_check.status == STATUS_FAIL
        assert tables_check.actual == 3

    def test_unexpected_schema_detected(self):
        conn = _make_mock_conn(schemas=["custom_schema"])
        validator = TargetPreValidator(conn, "MigrationE2E_MSSQL_Target")
        report = validator.validate()

        clean_phase = next(
            p for p in report.phases if p.name == "clean_state"
        )
        assert clean_phase.status == STATUS_FAIL
        schema_check = next(
            c for c in clean_phase.checks if c.name == "no_unexpected_schemas"
        )
        assert schema_check.status == STATUS_FAIL

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
