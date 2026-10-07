"""Unit tests for Phase C2 migration runner.

Tests safety checks and report structure without requiring live MSSQL.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.mssql.run_phase_c2 import load_config, run_migration
from tests.e2e.mssql.setup.target import is_protected_database

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "e2e" / "mssql" / "config" / "local_to_local.yaml"
)


class TestLoadBalancerSafety:
    """Verify the C2 runner refuses to migrate into protected databases."""

    @pytest.mark.parametrize("db_name", [
        "MigrationSource_MSSQL",
        "MigrationTarget_MSSQL",
        "master",
        "tempdb",
    ])
    def test_protected_db_blocks_migration(self, db_name):
        assert is_protected_database(db_name) is True

    def test_e2e_target_not_protected(self):
        assert is_protected_database("MigrationE2E_MSSQL_Target") is False


class TestConfigLoading:
    def test_config_points_to_e2e_dbs(self):
        config = load_config()
        assert config["e2e"]["source_database"] == "MigrationE2E_MSSQL_Source"
        assert config["e2e"]["target_database"] == "MigrationE2E_MSSQL_Target"

    def test_config_uses_secret_convention(self):
        config = load_config()
        assert config["source"]["connection"]["password_secret"] == \
            "mssql_e2e_source_pass"
        assert config["target"]["connection"]["password_secret"] == \
            "mssql_e2e_target_pass"

    def test_config_has_secrets_provider(self):
        config = load_config()
        assert config["secrets"]["provider"] == "env"

    def test_config_has_mssql_engines(self):
        config = load_config()
        assert config["source"]["engine"] == "mssql"
        assert config["target"]["engine"] == "mssql"


class TestRunMigrationMocked:
    """Test that run_migration delegates to MigrationOrchestrator."""

    def test_run_migration_calls_orchestrator(self):
        config = load_config()
        mock_source = MagicMock()
        mock_target = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_full.return_value = {
            "run_id": "test-run",
            "mode": "full",
            "status": "success",
            "phases": {
                "discover": {"objects": ["Customers", "Orders"]},
                "connect": "success",
                "ensure_database": "success",
            },
        }

        with patch(
            "tests.e2e.mssql.run_phase_c2.MSSQLSourceConnector",
            return_value=mock_source,
        ), patch(
            "tests.e2e.mssql.run_phase_c2.MSSQLTargetConnector",
            return_value=mock_target,
        ), patch(
            "tests.e2e.mssql.run_phase_c2.MigrationOrchestrator",
            return_value=mock_orchestrator,
        ), patch(
            "tests.e2e.mssql.run_phase_c2.configure_file_logging"
        ):

            result = run_migration(config)

            assert result["status"] == "success"
            mock_orchestrator.run_full.assert_called_once()

    def test_run_migration_handles_failure(self):
        config = load_config()
        mock_source = MagicMock()
        mock_target = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_full.return_value = {
            "run_id": "test-run",
            "mode": "full",
            "status": "failed",
            "phases": {"connect": "error: timeout"},
            "error": "timeout",
        }

        with patch(
            "tests.e2e.mssql.run_phase_c2.MSSQLSourceConnector",
            return_value=mock_source,
        ), patch(
            "tests.e2e.mssql.run_phase_c2.MSSQLTargetConnector",
            return_value=mock_target,
        ), patch(
            "tests.e2e.mssql.run_phase_c2.MigrationOrchestrator",
            return_value=mock_orchestrator,
        ), patch(
            "tests.e2e.mssql.run_phase_c2.configure_file_logging"
        ):

            result = run_migration(config)

            assert result["status"] == "failed"
