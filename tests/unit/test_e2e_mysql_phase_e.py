"""Unit tests for MySQL Phase E — full E2E pipeline orchestration,
consolidated report generation, and cleanup safety.

No live MySQL connection required; uses mocks for database and migration
execution.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.mysql.run_full_e2e import (
    _run_phase_c2,
    _run_phase_d,
    build_consolidated_report,
)
from tests.e2e.mysql.setup.cleanup import cleanup_databases, is_cleanup_safe
from tests.e2e.mysql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    E2EConsolidatedReport,
    E2EPhaseSummary,
)

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "e2e" / "mysql" / "config" / "local_to_local.yaml"
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


class TestCleanupSafety:
    @pytest.mark.parametrize("db_name", [
        "mysql", "information_schema", "performance_schema", "sys",
        "MigrationSource_MySQL", "MigrationTarget_MySQL",
    ])
    def test_protected_databases_rejected(self, db_name):
        ok, _ = is_cleanup_safe([db_name], "^MigrationE2E_MySQL_.+$")
        assert ok is False

    def test_e2e_databases_allowed(self):
        ok, reasons = is_cleanup_safe(
            ["MigrationE2E_MySQL_Source", "MigrationE2E_MySQL_Target"],
            "^MigrationE2E_MySQL_.+$",
        )
        assert ok is True
        assert reasons == []

    def test_non_e2e_database_rejected(self):
        ok, reasons = is_cleanup_safe(["ProductionDB"], "^MigrationE2E_MySQL_.+$")
        assert ok is False
        assert any("pattern" in r or "E2E" in r for r in reasons)

    def test_mixed_names_rejected(self):
        ok, reasons = is_cleanup_safe(
            ["MigrationE2E_MySQL_Source", "mysql"],
            "^MigrationE2E_MySQL_.+$",
        )
        assert ok is False
        assert len(reasons) >= 1

    def test_cleanup_databases_raises_on_protected(self):
        with pytest.raises(ValueError, match="protected"):
            cleanup_databases("h", "p", "u", "pw", ["mysql"])

    def test_cleanup_databases_raises_on_non_e2e(self):
        with pytest.raises(ValueError, match="pattern"):
            cleanup_databases("h", "p", "u", "pw", ["ProductionDB"])


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


class TestConsolidatedReportPassFail:
    def _make_report(self, phases: list[E2EPhaseSummary]) -> E2EConsolidatedReport:
        return E2EConsolidatedReport(
            source_database="src",
            target_database="tgt",
            server="localhost,33062",
            migration_status="success",
            phases=phases,
        )

    def test_all_pass_report_passes(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 14, 14, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 48, 48, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 8, 8, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 74, 74, 0),
        ]
        report = self._make_report(phases)
        assert report.passed is True
        assert report.total_checks == 14 + 48 + 8 + 7 + 74
        assert report.passed_checks == report.total_checks
        assert report.failed_checks == 0

    def test_one_phase_fails_report_fails(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 14, 14, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 48, 48, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 8, 8, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_FAIL, 74, 70, 4),
        ]
        report = self._make_report(phases)
        assert report.passed is False
        assert report.failed_checks == 4
        assert report.passed_checks == 70 + 14 + 48 + 8 + 7

    def test_empty_report_passes(self):
        report = E2EConsolidatedReport()
        assert report.passed is True
        assert report.total_checks == 0


class TestConsolidatedReportSerialization:
    def test_to_dict_has_required_fields(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 14, 14, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 74, 74, 0),
        ]
        report = E2EConsolidatedReport(
            source_database="MigrationE2E_MySQL_Source",
            target_database="MigrationE2E_MySQL_Target",
            server="127.0.0.1,33062",
            migration_status="success",
            source_row_counts={"customers": 5},
            target_row_counts={"customers": 5},
            phases=phases,
        )
        d = report.to_dict()
        assert d["status"] == "PASS"
        assert d["source_database"] == "MigrationE2E_MySQL_Source"
        assert d["target_database"] == "MigrationE2E_MySQL_Target"
        assert d["server"] == "127.0.0.1,33062"
        assert d["migration_status"] == "success"
        assert d["source_row_counts"] == {"customers": 5}
        assert d["target_row_counts"] == {"customers": 5}
        assert d["overall"]["total_checks"] == 14 + 74
        assert d["overall"]["passed_checks"] == 14 + 74
        assert d["overall"]["failed_checks"] == 0
        assert len(d["phases"]) == 2

    def test_json_serializable(self):
        phases = [_make_phase_summary("A", "setup", STATUS_PASS, 14, 14, 0)]
        report = E2EConsolidatedReport(
            source_database="src", target_database="tgt",
            server="localhost,33062", migration_status="success",
            phases=phases,
        )
        d = report.to_dict()
        json.dumps(d)


class TestBuildConsolidatedReport:
    def test_build_aggregation(self):
        config = _load_config()
        phase_a = _make_phase_summary("A", "setup", STATUS_PASS, 14, 14, 0)
        phase_b = _make_phase_summary(
            "B", "source", STATUS_PASS, 48, 48, 0,
            details={"row_counts": {"customers": 5, "orders": 5}},
        )
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 8, 8, 0)
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
            "row_counts": {"target": {"customers": 5}},
        }
        phase_d = _make_phase_summary(
            "D", "validate", STATUS_PASS, 74, 74, 0, details=d_details,
        )

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert report.passed is True
        assert report.source_database == "MigrationE2E_MySQL_Source"
        assert report.target_database == "MigrationE2E_MySQL_Target"
        assert report.migration_status == "success"
        assert report.source_row_counts == {"customers": 5, "orders": 5}
        assert report.target_row_counts == {"customers": 5}
        assert report.missing_objects == []
        assert report.unexpected_objects == []
        assert report.metadata_mismatches == []

    def test_build_detects_missing_objects(self):
        config = _load_config()
        phase_b = _make_phase_summary("B", "source", STATUS_PASS, 48, 48, 0)
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 8, 8, 0)
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
                             "missing": ["orders"], "unexpected": [], "mismatches": []
                         }}
                    ]}
                ]
            },
            "row_counts": {"target": {}},
        }
        phase_d = _make_phase_summary(
            "D", "validate", STATUS_FAIL, 74, 73, 1, details=d_details,
        )
        phase_a = _make_phase_summary("A", "setup", STATUS_PASS, 14, 14, 0)

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert report.missing_objects == ["orders"]
        assert report.passed is False


class TestPhaseC2Runner:
    def test_phase_c2_handles_migration_failure(self):
        config = _load_config()
        with patch(
            "tests.e2e.mysql.run_full_e2e.run_migration"
        ) as mock_migrate:
            mock_migrate.side_effect = RuntimeError("connection timeout")
            summary = _run_phase_c2(config)

            assert summary.status == STATUS_FAIL
            assert summary.failed_checks == 1
            assert "connection timeout" in summary.details["error"]

    def test_phase_c2_passes_on_success(self):
        config = _load_config()
        with patch(
            "tests.e2e.mysql.run_full_e2e.run_migration"
        ) as mock_migrate:
            mock_migrate.return_value = {
                "status": "success",
                "phases": {
                    "discover": {"objects": ["customers", "orders"]},
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
            "tests.e2e.mysql.run_full_e2e.run_migration"
        ) as mock_migrate:
            mock_migrate.return_value = {
                "status": "partial",
                "phases": {
                    "discover": {"objects": ["customers", "orders"]},
                    "customers": {"success": 1, "failure": 0},
                    "orders": {"success": 0, "failure": 3},
                },
            }
            summary = _run_phase_c2(config)
            assert summary.status == STATUS_FAIL
            assert "orders" in str(summary.details.get("failures", []))


class TestPhaseDRunner:
    def test_phase_d_aggregates_three_sub_reports(self):
        config = _load_config()

        mock_target_report = MagicMock()
        mock_target_report.passed = True
        mock_target_report.summary = {"PASS": 76, "FAIL": 0}
        mock_target_report.phases = []

        mock_comp_report = MagicMock()
        mock_comp_report.passed = True
        mock_comp_report.summary = {"PASS": 20, "FAIL": 0}
        mock_comp_report.to_dict.return_value = {"status": "PASS", "phases": []}

        mock_func_report = MagicMock()
        mock_func_report.passed = True
        mock_func_report.summary = {"PASS": 9, "FAIL": 0}
        mock_func_report.to_dict.return_value = {"status": "PASS", "phases": []}

        mock_comparator = MagicMock()
        mock_comparator.to_report.return_value = mock_comp_report

        with patch(
            "tests.e2e.mysql.run_full_e2e._connect_mysql",
            return_value=MagicMock(),
        ), \
             patch(
                "tests.e2e.mysql.run_full_e2e.MySQLTargetValidator"
            ) as MockTV, \
             patch(
                "tests.e2e.mysql.run_full_e2e.MySQLSourceTargetComparator"
            ) as MockComp, \
             patch(
                "tests.e2e.mysql.run_full_e2e.MySQLFunctionalValidator"
            ) as MockFunc:

            MockTV.return_value.validate.return_value = mock_target_report
            MockComp.return_value = mock_comparator
            MockFunc.return_value.validate.return_value = mock_func_report

            summary, _, _, _ = _run_phase_d(config, {"customers": 5})

        assert summary.status == STATUS_PASS
        assert summary.total_checks == 76 + 20 + 9
        assert summary.passed_checks == 76 + 20 + 9
        assert summary.failed_checks == 0
