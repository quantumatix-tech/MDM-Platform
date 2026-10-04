"""Unit tests for Phase E — full PostgreSQL E2E pipeline orchestration,
consolidated report generation, and cleanup safety.

No live PostgreSQL connection required; uses mocks for database and migration
execution.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.postgresql.run_full_e2e import (
    _run_phase_a,
    _run_phase_b,
    _run_phase_c1,
    _run_phase_c2,
    _run_phase_d,
    build_consolidated_report,
)
from tests.e2e.postgresql.setup.cleanup import cleanup_databases, is_cleanup_safe
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    E2EConsolidatedReport,
    E2EPhaseSummary,
)

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "e2e" / "postgresql" / "config" / "local_to_local.yaml"
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


def _mock_validation_report(
    passed: bool = True,
    pass_count: int = 10,
    fail_count: int = 0,
    phases: list | None = None,
):
    """Build a MagicMock that mimics ValidationReport."""
    mock_report = MagicMock()
    mock_report.passed = passed
    mock_report.summary = {"PASS": pass_count, "FAIL": fail_count}
    mock_report.total_duration_s = 0.5
    mock_report.phases = phases or []
    return mock_report


# ============================================================
# Cleanup safety tests
# ============================================================

class TestCleanupSafety:
    @pytest.mark.parametrize("db_name", [
        "postgres", "template0", "template1",
        "MigrationSource_PostgreSQL", "MigrationTarget_PostgreSQL",
        "migration_source", "migration_target",
    ])
    def test_protected_databases_rejected(self, db_name):
        ok, reasons = is_cleanup_safe([db_name], "^MigrationE2E_PostgreSQL_.+$")
        assert ok is False
        assert any("protected" in r for r in reasons)

    def test_e2e_databases_allowed(self):
        ok, reasons = is_cleanup_safe(
            ["MigrationE2E_PostgreSQL_Source", "MigrationE2E_PostgreSQL_Target"],
            "^MigrationE2E_PostgreSQL_.+$",
        )
        assert ok is True
        assert reasons == []

    def test_non_e2e_database_rejected(self):
        ok, reasons = is_cleanup_safe(["ProductionDB"], "^MigrationE2E_PostgreSQL_.+$")
        assert ok is False
        assert any("pattern" in r or "E2E" in r for r in reasons)

    def test_mixed_names_rejected(self):
        ok, reasons = is_cleanup_safe(
            ["MigrationE2E_PostgreSQL_Source", "postgres"],
            "^MigrationE2E_PostgreSQL_.+$",
        )
        assert ok is False
        assert len(reasons) >= 1

    def test_cleanup_databases_raises_on_protected(self):
        with pytest.raises(ValueError, match="protected"):
            cleanup_databases("h", "p", "u", "pw", ["postgres"])

    def test_cleanup_databases_raises_on_non_e2e(self):
        with pytest.raises(ValueError, match="pattern"):
            cleanup_databases("h", "p", "u", "pw", ["ProductionDB"])

    def test_cleanup_databases_allows_e2e(self):
        with patch("tests.e2e.postgresql.setup.cleanup.reset_database") as mock_reset:
            mock_reset.return_value = True
            result = cleanup_databases(
                "h", "p", "u", "pw",
                ["MigrationE2E_PostgreSQL_Source"],
                "^MigrationE2E_PostgreSQL_.+$",
            )
            assert result == {"MigrationE2E_PostgreSQL_Source": True}
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

    def test_duration_s_default_zero(self):
        ps = E2EPhaseSummary(name="X", description="test")
        assert ps.duration_s == 0.0


# ============================================================
# E2EConsolidatedReport — PASS/FAIL propagation
# ============================================================

class TestConsolidatedReportPassFail:
    def _make_report(self, phases: list[E2EPhaseSummary]) -> E2EConsolidatedReport:
        report = E2EConsolidatedReport(
            source_database="MigrationE2E_PostgreSQL_Source",
            target_database="MigrationE2E_PostgreSQL_Target",
            server="127.0.0.1,55432",
            migration_status="success",
            phases=phases,
        )
        report.source_row_counts = {"customers": 5}
        report.target_row_counts = {"customers": 5}
        return report

    def test_all_pass_report_passes(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 55, 55, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 109, 109, 0),
        ]
        report = self._make_report(phases)
        assert report.passed is True
        assert report.total_checks == 15 + 55 + 13 + 7 + 109
        assert report.passed_checks == report.total_checks
        assert report.failed_checks == 0

    def test_one_phase_fails_report_fails(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 55, 55, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_FAIL, 109, 105, 4),
        ]
        report = self._make_report(phases)
        assert report.passed is False
        assert report.failed_checks == 4

    def test_phase_d_failure_makes_overall_fail(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 55, 55, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_FAIL, 109, 108, 1),
        ]
        report = self._make_report(phases)
        assert report.passed is False
        assert report.failed_checks == 1

    def test_empty_report_passes(self):
        report = E2EConsolidatedReport()
        assert report.passed is True
        assert report.total_checks == 0

    def test_phase_b_failure_fails_report(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("B", "source", STATUS_FAIL, 55, 50, 5),
            _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 7, 7, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 109, 109, 0),
        ]
        report = self._make_report(phases)
        assert report.passed is False
        assert report.failed_checks == 5


# ============================================================
# Consolidated report serialization
# ============================================================

class TestConsolidatedReportSerialization:
    def test_to_dict_has_required_fields(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 109, 109, 0),
        ]
        report = E2EConsolidatedReport(
            source_database="MigrationE2E_PostgreSQL_Source",
            target_database="MigrationE2E_PostgreSQL_Target",
            server="127.0.0.1,55432",
            migration_status="success",
            source_row_counts={"customers": 5},
            target_row_counts={"customers": 5},
            phases=phases,
        )
        d = report.to_dict()
        assert d["status"] == "PASS"
        assert d["source_database"] == "MigrationE2E_PostgreSQL_Source"
        assert d["target_database"] == "MigrationE2E_PostgreSQL_Target"
        assert d["server"] == "127.0.0.1,55432"
        assert d["migration_status"] == "success"
        assert d["source_row_counts"] == {"customers": 5}
        assert d["target_row_counts"] == {"customers": 5}
        assert d["overall"]["total_checks"] == 15 + 109
        assert d["overall"]["passed_checks"] == 15 + 109
        assert d["overall"]["failed_checks"] == 0
        assert len(d["phases"]) == 2

    def test_to_dict_fail_status(self):
        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0),
            _make_phase_summary("C2", "migrate", STATUS_FAIL, 7, 0, 7),
        ]
        report = E2EConsolidatedReport(
            source_database="src", target_database="tgt",
            server="127.0.0.1,55432", migration_status="failed",
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
            source_database="src", target_database="tgt",
            server="127.0.0.1,55432", migration_status="success",
            phases=phases,
        )
        d = report.to_dict()
        json.dumps(d)

    def test_phase_d_sub_reports_separated(self):
        d_details = {
            "structural_validation": {"status": "PASS", "summary": {"PASS": 76}},
            "source_target_comparison": {"status": "PASS", "summary": {"PASS": 20}},
            "functional_validation": {"status": "PASS", "summary": {"PASS": 10}},
            "row_counts": {"source": {"x": 1}, "target": {"x": 1}},
        }
        d_phase = E2EPhaseSummary(
            name="D", description="validate", status=STATUS_PASS,
            total_checks=106, passed_checks=106, failed_checks=0,
            details=d_details,
        )
        report = E2EConsolidatedReport(
            source_database="src", target_database="tgt",
            server="localhost,5432", migration_status="success",
            phases=[d_phase],
        )
        d = report.to_dict()
        assert d["structural_validation"]["status"] == "PASS"
        assert d["structural_validation"]["summary"]["PASS"] == 76
        assert d["source_target_comparison"]["summary"]["PASS"] == 20
        assert d["functional_validation"]["summary"]["PASS"] == 10
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
            "B", "source", STATUS_PASS, 55, 55, 0,
            details={"row_counts": {"customers": 5, "orders": 5}},
        )
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0)
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
            "D", "validate", STATUS_PASS, 109, 109, 0, details=d_details,
        )

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert report.passed is True
        assert report.source_database == "MigrationE2E_PostgreSQL_Source"
        assert report.target_database == "MigrationE2E_PostgreSQL_Target"
        assert report.migration_status == "success"
        assert report.source_row_counts == {"customers": 5, "orders": 5}
        assert report.target_row_counts == {"customers": 5}
        assert report.missing_objects == []
        assert report.unexpected_objects == []
        assert report.metadata_mismatches == []

    def test_build_detects_missing_objects(self):
        config = _load_config()
        phase_b = _make_phase_summary("B", "source", STATUS_PASS, 55, 55, 0)
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0)
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
            "D", "validate", STATUS_FAIL, 109, 108, 1, details=d_details,
        )
        phase_a = _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0)

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert report.missing_objects == ["orders"]
        assert report.passed is False

    def test_build_collects_migration_failures(self):
        config = _load_config()
        phase_a = _make_phase_summary("A", "setup", STATUS_PASS, 15, 15, 0)
        phase_b = _make_phase_summary("B", "source", STATUS_PASS, 55, 55, 0)
        phase_c1 = _make_phase_summary("C1", "pre", STATUS_PASS, 13, 13, 0)
        phase_c2 = _make_phase_summary(
            "C2", "migrate", STATUS_FAIL, 7, 0, 7,
            details={"migration_status": "partial", "failures":
                ["customers: 3 failure(s)", "orders: 2 failure(s)"]},
        )
        phase_d = _make_phase_summary("D", "validate", STATUS_PASS, 109, 109, 0)

        report = build_consolidated_report(
            config, phase_a, phase_b, phase_c1, phase_c2, phase_d
        )

        assert len(report.migration_failures) == 2
        assert "customers: 3 failure(s)" in report.migration_failures
        assert report.migration_status == "partial"
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
        config["e2e"]["source_database"] = "postgres"

        summary = _run_phase_a(config)
        assert summary.status == STATUS_FAIL

    def test_phase_a_success_with_mocked_load(self):
        config = _load_config()
        mock_result = MagicMock()
        mock_result.all_ok = True
        mock_result.results = [MagicMock(), MagicMock()]
        mock_result.failed_scripts = []

        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.postgresql.run_full_e2e.is_cleanup_safe",
                   return_value=(True, [])), \
             patch("tests.e2e.postgresql.run_full_e2e.cleanup_databases"), \
             patch("tests.e2e.postgresql.run_full_e2e.load_fixture",
                   return_value=mock_result):
            summary = _run_phase_a(config)

        assert summary.status == STATUS_PASS
        assert summary.passed_checks == 2
        assert summary.failed_checks == 0


class TestPhaseC2Runner:
    def test_phase_c2_handles_migration_failure(self):
        config = _load_config()
        with patch(
            "tests.e2e.postgresql.run_full_e2e._run_migration"
        ) as mock_migrate:
            mock_migrate.side_effect = RuntimeError("connection timeout")
            summary = _run_phase_c2(config)

            assert summary.status == STATUS_FAIL
            assert summary.failed_checks == 1
            assert "connection timeout" in summary.details["error"]

    def test_phase_c2_passes_on_success(self):
        config = _load_config()
        with patch(
            "tests.e2e.postgresql.run_full_e2e._run_migration"
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
            "tests.e2e.postgresql.run_full_e2e._run_migration"
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


class TestPhaseBRunner:
    def test_phase_b_builds_summary_from_report(self):
        config = _load_config()

        mock_report = _mock_validation_report(
            passed=True, pass_count=55, fail_count=0,
            phases=[],
        )

        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.postgresql.run_full_e2e.psycopg.connect"), \
             patch(
                 "tests.e2e.postgresql.validation.source_validator.SourceValidator"
             ) as MockSV:
            MockSV.return_value.validate.return_value = mock_report
            summary = _run_phase_b(config)

        assert summary.status == STATUS_PASS
        assert summary.total_checks == 55
        assert summary.passed_checks == 55
        assert summary.failed_checks == 0


class TestPhaseC1Runner:
    def test_phase_c1_handles_protected_target(self):
        config = _load_config()
        config["e2e"]["target_database"] = "postgres"
        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ):
            summary = _run_phase_c1(config)
        assert summary.status == STATUS_FAIL
        assert "error" in summary.details

    def test_phase_c1_aborts_on_safety_error(self):
        config = _load_config()
        config["e2e"]["target_database"] = "ProductionDB"
        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ):
            summary = _run_phase_c1(config)
        assert summary.status == STATUS_FAIL

    def test_phase_c1_passes_with_mocked_validator(self):
        config = _load_config()
        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}
        mock_report = _mock_validation_report(
            passed=True, pass_count=13, fail_count=0,
        )

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.postgresql.run_full_e2e.reset_target_database"), \
             patch("tests.e2e.postgresql.run_full_e2e.psycopg.connect"), \
             patch(
                 "tests.e2e.postgresql.validation.target_pre.TargetPreValidator"
             ) as MockPre:
            MockPre.return_value.validate.return_value = mock_report
            summary = _run_phase_c1(config)

        assert summary.status == STATUS_PASS
        assert summary.total_checks == 13
        assert summary.failed_checks == 0


class TestPhaseDRunner:
    def test_phase_d_aggregates_three_sub_reports(self):
        config = _load_config()

        mock_target_report = _mock_validation_report(pass_count=76, fail_count=0)
        mock_target_report.phases = []
        mock_comp_report = _mock_validation_report(pass_count=20, fail_count=0)

        class _MockCategory:
            def __init__(self):
                self.name = "tables"
                self.passed = True
                self.status = STATUS_PASS
                self.failed_count = 0
                self.total_count = 20
                self.checks = []
                self.difference = {}

        mock_comparator = MagicMock()
        mock_comparator.compare.return_value = None
        mock_comparator.to_report.return_value = mock_comp_report
        mock_comparator.categories = [_MockCategory()]

        mock_func_report = _mock_validation_report(pass_count=10, fail_count=0)

        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.postgresql.run_full_e2e.psycopg.connect"), \
             patch(
                 "tests.e2e.postgresql.run_full_e2e.TargetValidator"
             ) as MockTV, \
             patch(
                 "tests.e2e.postgresql.run_full_e2e.SourceTargetComparator"
             ) as MockComp, \
             patch(
                 "tests.e2e.postgresql.run_full_e2e.FunctionalValidator"
             ) as MockFunc:

            MockTV.return_value.validate.return_value = mock_target_report
            MockComp.return_value = mock_comparator
            MockFunc.return_value.validate.return_value = mock_func_report

            summary, _struct_dict, _comp_dict, _func_dict = _run_phase_d(config, {})

        assert summary.status == STATUS_PASS
        assert summary.total_checks == 76 + 20 + 10
        assert summary.passed_checks == 76 + 20 + 10
        assert summary.failed_checks == 0

    def test_phase_d_fails_when_target_validation_fails(self):
        config = _load_config()

        mock_target_report = _mock_validation_report(
            passed=False, pass_count=76, fail_count=1)
        mock_target_report.phases = []
        mock_comp_report = _mock_validation_report(pass_count=20, fail_count=0)
        mock_func_report = _mock_validation_report(pass_count=10, fail_count=0)

        mock_comparator = MagicMock()
        mock_comparator.compare.return_value = None
        mock_comparator.to_report.return_value = mock_comp_report

        mock_params = {"host": "h", "port": "p", "user": "u", "password": "pw"}

        with patch(
            "tests.e2e.postgresql.run_full_e2e._resolve_connection_params",
            return_value=mock_params,
        ), \
             patch("tests.e2e.postgresql.run_full_e2e.psycopg.connect"), \
             patch(
                 "tests.e2e.postgresql.run_full_e2e.TargetValidator"
             ) as MockTV, \
             patch(
                 "tests.e2e.postgresql.run_full_e2e.SourceTargetComparator"
             ) as MockComp, \
             patch(
                 "tests.e2e.postgresql.run_full_e2e.FunctionalValidator"
             ) as MockFunc:

            MockTV.return_value.validate.return_value = mock_target_report
            MockComp.return_value = mock_comparator
            MockFunc.return_value.validate.return_value = mock_func_report

            summary, _, _, _ = _run_phase_d(config, {})

        assert summary.status == STATUS_FAIL
        assert summary.failed_checks > 0


# ============================================================
# Credential non-leakage in report
# ============================================================

class TestCredentialSafety:
    def test_no_passwords_in_consolidated_report(self):
        config = _load_config()
        config["source"]["connection"]["password_secret"] = "MY_SECRET_PASS"

        phases = [
            _make_phase_summary("A", "setup", STATUS_PASS, 1, 1, 0),
            _make_phase_summary("B", "source", STATUS_PASS, 1, 1, 0),
            _make_phase_summary("C1", "pre", STATUS_PASS, 1, 1, 0),
            _make_phase_summary("C2", "migrate", STATUS_PASS, 1, 1, 0),
            _make_phase_summary("D", "validate", STATUS_PASS, 1, 1, 0),
        ]
        report = build_consolidated_report(config, *phases)
        d = report.to_dict()
        serialized = json.dumps(d)

        assert "MY_SECRET_PASS" not in serialized
        assert "SECRET_MY_SECRET_PASS" not in serialized
        assert "password" not in serialized.lower() or "password_secret" not in serialized


# ============================================================
# C2 count preservation
# ============================================================

class TestC2CountPreservation:
    def test_c2_object_count_preserved_in_details(self):
        config = _load_config()
        with patch(
            "tests.e2e.postgresql.run_full_e2e._run_migration"
        ) as mock_migrate:
            mock_migrate.return_value = {
                "status": "success",
                "phases": {
                    "discover": {"objects": ["customers", "orders", "products"]},
                    "create_tables": {"success": 3, "failure": 0},
                },
            }
            summary = _run_phase_c2(config)

        assert summary.details["object_count"] == 3
        assert summary.status == STATUS_PASS
