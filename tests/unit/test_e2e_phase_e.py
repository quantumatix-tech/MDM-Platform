"""Unit tests for Phase E — full E2E pipeline orchestration,
consolidated report generation, and cleanup safety.

No live MSSQL connection required; uses mocks for database and migration
execution.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.mssql.run_full_e2e import (
    _run_phase_a,
    _run_phase_b,
    _run_phase_c1,
    _run_phase_c2,
    _run_phase_d,
    build_consolidated_report,
)
from tests.e2e.mssql.setup.cleanup import cleanup_databases, is_cleanup_safe
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    E2EConsolidatedReport,
    E2EPhaseSummary,
)

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "e2e" / "mssql" / "config" / "local_to_local.yaml"
)


def _load_config() -> dict:
    import yaml
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _make_phase_summary(
    name: str = "A",
    description: str = "test",
    status: str = STATUS_PASS,
    total: int = 5,
    passed: int = 5,
    failed: int = 0,
    details: dict | None = None,
) -> E2EPhaseSummary:
    return E2EPhaseSummary(
        name=name,
        description=description,
        status=status,
        total_checks=total,
        passed_checks=passed,
        failed_checks=failed,
        details=details or {},
    )


# ============================================================
# Cleanup safety tests
# ============================================================

class TestCleanupSafety:
    @pytest.mark.parametrize("db_name", [
        "master", "model", "msdb", "tempdb",
        "MigrationSource_MSSQL", "MigrationTarget_MSSQL",
    ])
    def test_protected_databases_rejected(self, db_name):
        ok, reasons = is_cleanup_safe([db_name], "^MigrationE2E_MSSQL_.+$")
        assert ok is False
        assert any("protected" in r.lower() or "protected" in r for r in reasons
                   ) or any("protected" in r.lower() for r in reasons)

    def test_e2e_databases_allowed(self):
        ok, reasons = is_cleanup_safe(
            ["MigrationE2E_MSSQL_Source", "MigrationE2E_MSSQL_Target"],
            "^MigrationE2E_MSSQL_.+$",
        )
        assert ok is True
        assert reasons == []

    def test_non_e2e_database_rejected(self):
        ok, reasons = is_cleanup_safe(["ProductionDB"], "^MigrationE2E_MSSQL_.+$")
        assert ok is False
        assert any("pattern" in r or "E2E" in r for r in reasons)

    def test_mixed_names_rejected(self):
        ok, reasons = is_cleanup_safe(
            ["MigrationE2E_MSSQL_Source", "master"],
            "^MigrationE2E_MSSQL_.+$",
        )
        assert ok is False
        assert len(reasons) >= 1

    def test_cleanup_databases_raises_on_protected(self):
        with pytest.raises(ValueError, match="protected"):
            cleanup_databases("h", "p", "u", "pw", ["master"])

    def test_cleanup_databases_raises_on_non_e2e(self):
        with pytest.raises(ValueError, match="pattern"):
            cleanup_databases("h", "p", "u", "pw", ["ProductionDB"])

    def test_cleanup_databases_allows_e2e(self):
        with patch("tests.e2e.mssql.setup.cleanup.reset_database") as mock_reset:
            mock_reset.return_value = True
            result = cleanup_databases(
                "h", "p", "u", "pw",
                ["MigrationE2E_MSSQL_Source"],
                "^MigrationE2E_MSSQL_.+$",
            )
            assert result == {"MigrationE2E_MSSQL_Source": True}
            mock_reset.assert_called_once()


# ============================================================
# E2EPhaseSummary tests
# ============================================================

class TestE2EPhaseSummary:
    def test_default_status_is_pass(self):
        ps = E2EPhaseSummary(name="X", description="test")
        assert ps.status == STATUS_PASS
        assert ps.passed is True

    def test_fail_status(self):
        ps = E2EPhaseSummary(name="X", description="test", status=STATUS_FAIL)
        assert ps.passed is False

    def test_check_counts(self):
        ps = E2EPhaseSummary(
            name="X", description="test",
            total_checks=10, passed_checks=8, failed_checks=2,
        )
        assert ps.passed_checks == 8
        assert ps.failed_checks == 2
        assert ps.total_checks == 10


# ============================================================
# E2EConsolidatedReport — PASS/FAIL propagation
# ============================================================

class TestConsolidatedReportPassFail:
    def _make_report(self, phases: list[E2EPhaseSummary]) -> E2EConsolidatedReport:
        report = E2EConsolidatedReport(
            source_database="src",
            target_database="tgt",
            server="localhost,1533",
            migration_status="success",
            phases=phases,
        )
        report.source_row_counts = {"Customers": 5}
        report.target_row_counts = {"Customers": 5}
        return report

    def test_all_pass_report_passes(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 72, 72, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 12, 12, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 99, 99, 0),
        ]
        report = self._make_report(phases)
        assert report.passed is True
        assert report.total_checks == 15 + 72 + 12 + 7 + 99
        assert report.passed_checks == report.total_checks
        assert report.failed_checks == 0

    def test_one_phase_fails_report_fails(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 72, 72, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 12, 12, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_FAIL, 99, 95, 4),
        ]
        report = self._make_report(phases)
        assert report.passed is False
        assert report.failed_checks == 4
        assert report.passed_checks == 95 + 15 + 72 + 12 + 7

    def test_phase_d_failure_makes_overall_fail(self):
        """A single failing Phase D check should fail the entire report."""
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 72, 72, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 12, 12, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_FAIL, 99, 98, 1),
        ]
        report = self._make_report(phases)
        assert report.passed is False
        assert report.failed_checks == 1

    def test_empty_report_passes(self):
        report = E2EConsolidatedReport()
        assert report.passed is True
        assert report.total_checks == 0


# ============================================================
# Consolidated report serialization
# ============================================================

class TestConsolidatedReportSerialization:
    def test_to_dict_has_required_fields(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 99, 99, 0),
        ]
        report = E2EConsolidatedReport(
            source_database="MigrationE2E_MSSQL_Source",
            target_database="MigrationE2E_MSSQL_Target",
            server="localhost,1533",
            migration_status="success",
            source_row_counts={"Customers": 5},
            target_row_counts={"Customers": 5},
            phases=phases,
        )
        d = report.to_dict()
        assert d["status"] == "PASS"
        assert d["source_database"] == "MigrationE2E_MSSQL_Source"
        assert d["target_database"] == "MigrationE2E_MSSQL_Target"
        assert d["server"] == "localhost,1533"
        assert d["migration_status"] == "success"
        assert d["source_row_counts"] == {"Customers": 5}
        assert d["target_row_counts"] == {"Customers": 5}
        assert d["overall"]["total_checks"] == 15 + 99
        assert d["overall"]["passed_checks"] == 15 + 99
        assert d["overall"]["failed_checks"] == 0
        assert len(d["phases"]) == 2

    def test_to_dict_fail_status(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("C2", "migrate", STATUS_FAIL, 7, 0, 7),
        ]
        report = E2EConsolidatedReport(
            source_database="src",
            target_database="tgt",
            server="localhost,1533",
            migration_status="failed",
            phases=phases,
        )
        d = report.to_dict()
        assert d["status"] == "FAIL"
        assert d["overall"]["failed_checks"] == 7

    def test_json_serializable(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
        ]
        report = E2EConsolidatedReport(
            source_database="src",
            target_database="tgt",
            server="localhost,1533",
            migration_status="success",
            phases=phases,
        )
        d = report.to_dict()
        json.dumps(d)

    def test_phase_d_sub_reports_separated(self):
        """structural_validation, source_target_comparison, and
        functional_validation should each get their own sub-report from
        Phase D's details, not the entire details dict."""
        d_details = {
            "structural_validation": {"status": "PASS", "summary": {"PASS": 76}},
            "source_target_comparison": {"status": "PASS", "summary": {"PASS": 20}},
            "functional_validation": {"status": "PASS", "summary": {"PASS": 9}},
            "row_counts": {"source": {"x": 1}, "target": {"x": 1}},
        }
        d_phase = E2EPhaseSummary(
            name="D", description="validate", status=STATUS_PASS,
            total_checks=105, passed_checks=105, failed_checks=0,
            details=d_details,
        )
        report = E2EConsolidatedReport(
            source_database="src", target_database="tgt",
            server="localhost,1533", migration_status="success",
            phases=[d_phase],
        )
        d = report.to_dict()
        assert d["structural_validation"]["status"] == "PASS"
        assert d["structural_validation"]["summary"]["PASS"] == 76
        assert d["source_target_comparison"]["summary"]["PASS"] == 20
        assert d["functional_validation"]["summary"]["PASS"] == 9
        assert "structural_validation" not in d["structural_validation"]
        assert "row_counts" not in d["structural_validation"]


# ============================================================
# build_consolidated_report
# ============================================================

class TestBuildConsolidatedReport:
    def test_build_aggregation(self):
        config = _load_config()
        phase_a = _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0)
        phase_b = _make_phase_summary(
            "B", "source", STATUS_PASS, 72, 72, 0,
            details={"row_counts": {"Customers": 5, "Orders": 5}},
        )
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 12, 12, 0)
        phase_c2 = _make_phase_summary(
            "C2", "migrate", STATUS_PASS, 7, 7, 0,
            details={"migration_status": "success"},
        )
        d_details = {
            "source_target_comparison": {
                "phases": [
                    {"name": "tables", "checks": [
                        {"name": "comparison", "status": "PASS",
                         "details": {"missing": [], "unexpected": [], "mismatches": []}}
                    ]}
                ]
            },
            "row_counts": {"target": {"Customers": 5}},
        }
        phase_d = _make_phase_summary(
            "D", "validate", STATUS_PASS, 99, 99, 0, details=d_details,
        )

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert report.passed is True
        assert report.source_database == "MigrationE2E_MSSQL_Source"
        assert report.target_database == "MigrationE2E_MSSQL_Target"
        assert report.migration_status == "success"
        assert report.source_row_counts == {"Customers": 5, "Orders": 5}
        assert report.target_row_counts == {"Customers": 5}
        assert report.missing_objects == []
        assert report.unexpected_objects == []
        assert report.metadata_mismatches == []

    def test_build_detects_missing_objects(self):
        config = _load_config()
        phase_b = _make_phase_summary("B", "source", STATUS_PASS, 72, 72, 0)
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 12, 12, 0)
        phase_c2 = _make_phase_summary(
            "C2", "migrate", STATUS_PASS, 7, 7, 0,
            details={"migration_status": "success"},
        )
        d_details = {
            "source_target_comparison": {
                "phases": [
                    {"name": "tables", "checks": [
                        {"name": "comparison", "status": "FAIL",
                         "details": {
                             "missing": ["Orders"], "unexpected": [], "mismatches": []
                         }}
                    ]}
                ]
            },
            "row_counts": {"target": {}},
        }
        phase_d = _make_phase_summary(
            "D", "validate", STATUS_FAIL, 99, 98, 1, details=d_details,
        )
        phase_a = _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0)

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert report.missing_objects == ["Orders"]
        assert report.passed is False


# ============================================================
# Phase runner — failure propagation (mocked)
# ============================================================

class TestPhaseARunner:
    def test_phase_a_aborts_on_unprotected_db(self):
        config = _load_config()
        config["e2e"]["source_database"] = "ProductionDB"

        summary = _run_phase_a(config)
        assert summary.status == STATUS_FAIL
        assert summary.failed_checks > 0
        assert "error" in summary.details

    def test_phase_a_aborts_on_protected_db(self):
        config = _load_config()
        config["e2e"]["source_database"] = "master"

        summary = _run_phase_a(config)
        assert summary.status == STATUS_FAIL


class TestPhaseC2Runner:
    def test_phase_c2_handles_migration_failure(self):
        config = _load_config()
        with patch(
            "tests.e2e.mssql.run_full_e2e._run_migration"
        ) as mock_migrate:
            mock_migrate.side_effect = RuntimeError("connection timeout")
            summary = _run_phase_c2(config)

            assert summary.status == STATUS_FAIL
            assert summary.failed_checks == 1
            assert "connection timeout" in summary.details["error"]

    def test_phase_c2_passes_on_success(self):
        config = _load_config()
        with patch(
            "tests.e2e.mssql.run_full_e2e._run_migration"
        ) as mock_migrate:
            mock_migrate.return_value = {
                "status": "success",
                "phases": {
                    "discover": {"objects": ["Customers", "Orders"]},
                    "create_tables": {"success": 2, "failure": 0},
                },
            }
            summary = _run_phase_c2(config)
            assert summary.status == STATUS_PASS
            assert summary.details.get("failures", []) == []
            assert summary.details["migration_status"] == "success"

    def test_phase_c2_detects_object_failures(self):
        config = _load_config()
        with patch(
            "tests.e2e.mssql.run_full_e2e._run_migration"
        ) as mock_migrate:
            mock_migrate.return_value = {
                "status": "partial",
                "phases": {
                    "discover": {"objects": ["Customers", "Orders"]},
                    "Customers": {"success": 1, "failure": 0},
                    "Orders": {"success": 0, "failure": 3},
                },
            }
            summary = _run_phase_c2(config)
            assert summary.status == STATUS_FAIL
            assert "Orders" in str(summary.details.get("failures", []))


class TestPhaseBRunner:
    def test_phase_b_builds_summary_from_report(self):
        """Phase B should extract row counts and check totals from the
        validation report."""
        config = _load_config()

        mock_report = MagicMock()
        mock_report.passed = True
        mock_report.summary = {"PASS": 72, "FAIL": 0}
        mock_report.total_duration_s = 5.0
        mock_report.phases = []

        mock_params = {"host": "h", "port": "p", "username": "u", "password": "pw"}

        with patch(
            "tests.e2e.mssql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.mssql.run_full_e2e.pyodbc.connect"), \
             patch(
                "tests.e2e.mssql.validation.source_validator.SourceValidator"
            ) as MockSV:
            MockSV.return_value.validate.return_value = mock_report
            summary = _run_phase_b(config)

        assert summary.status == STATUS_PASS
        assert summary.total_checks == 72
        assert summary.passed_checks == 72
        assert summary.failed_checks == 0


class TestPhaseC1Runner:
    def test_phase_c1_handles_protected_target(self):
        config = _load_config()
        config["e2e"]["target_database"] = "master"
        mock_params = {"host": "h", "port": "p", "username": "u", "password": "pw"}

        with patch(
            "tests.e2e.mssql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ):
            summary = _run_phase_c1(config)
        assert summary.status == STATUS_FAIL
        assert "error" in summary.details

    def test_phase_c1_aborts_on_safety_error(self):
        config = _load_config()
        config["e2e"]["target_database"] = "ProductionDB"
        mock_params = {"host": "h", "port": "p", "username": "u", "password": "pw"}

        with patch(
            "tests.e2e.mssql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ):
            summary = _run_phase_c1(config)
        assert summary.status == STATUS_FAIL


class TestPhaseDRunner:
    def test_phase_d_aggregates_three_sub_reports(self):
        config = _load_config()

        mock_target_report = MagicMock()
        mock_target_report.passed = True
        mock_target_report.summary = {"PASS": 76, "FAIL": 0}
        mock_target_report.total_duration_s = 3.0
        mock_target_report.phases = []

        mock_comp_report = MagicMock()
        mock_comp_report.passed = True
        mock_comp_report.summary = {"PASS": 20, "FAIL": 0}
        mock_comp_report.total_duration_s = 0.0

        mock_func_report = MagicMock()
        mock_func_report.passed = True
        mock_func_report.summary = {"PASS": 9, "FAIL": 0}
        mock_func_report.total_duration_s = 0.0

        mock_comparator = MagicMock()
        mock_comparator.compare.return_value = None
        mock_comparator.to_report.return_value = mock_comp_report
        mock_comparator.categories = []

        mock_params = {"host": "h", "port": "p", "username": "u", "password": "pw"}

        with patch(
            "tests.e2e.mssql.run_phase_d._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.mssql.run_phase_d.pyodbc.connect") as mock_conn, \
             patch(
                "tests.e2e.mssql.run_full_e2e.TargetValidator"
            ) as MockTV, \
             patch(
                "tests.e2e.mssql.run_full_e2e.SourceTargetComparator"
            ) as MockComp, \
             patch(
                "tests.e2e.mssql.run_full_e2e.FunctionalValidator"
            ) as MockFunc:

            mock_conn.return_value = MagicMock()
            MockTV.return_value.validate.return_value = mock_target_report
            MockComp.return_value = mock_comparator
            MockFunc.return_value.validate.return_value = mock_func_report

            summary, struct_dict, comp_dict, func_dict = _run_phase_d(config, {})

        assert summary.status == STATUS_PASS
        assert summary.total_checks == 76 + 20 + 9
        assert summary.passed_checks == 76 + 20 + 9
        assert summary.failed_checks == 0
