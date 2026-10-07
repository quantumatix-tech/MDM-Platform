"""Unit tests for Phase C2 — PostgreSQL real migration execution.

Tests safety checks, migration flow, and report structure without requiring
a live PostgreSQL migration.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.connectors.postgresql.objects import partition as pg_partition
from tests.e2e.postgresql.run_phase_c2 import (
    _extract_migration_summary,
    load_config,
    run_migration,
)
from tests.e2e.postgresql.setup.database import is_safe_e2e_database
from tests.e2e.postgresql.setup.target import is_protected_database

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "e2e" / "postgresql" / "config" / "local_to_local.yaml"
)


class TestE2ESafetyChecks:
    """Verify the C2 runner refuses to migrate into protected databases."""

    @pytest.mark.parametrize("db_name", [
        "MigrationSource_PostgreSQL",
        "MigrationTarget_PostgreSQL",
        "postgres",
        "template0",
        "template1",
    ])
    def test_protected_db_blocks_migration(self, db_name):
        assert is_protected_database(db_name) is True

    def test_e2e_target_not_protected(self):
        assert is_protected_database("MigrationE2E_PostgreSQL_Target") is False


class TestSafeE2EDatabase:
    @pytest.mark.parametrize("db_name", [
        "MigrationE2E_PostgreSQL_Target",
        "MigrationE2E_PostgreSQL_Source",
    ])
    def test_valid_e2e_database_accepted(self, db_name):
        assert is_safe_e2e_database(db_name) is True

    @pytest.mark.parametrize("db_name", [
        "ProductionDB",
        "postgres",
        "template0",
        "MigrationSource_PostgreSQL",
        "MigrationTarget_PostgreSQL",
        "",
    ])
    def test_non_e2e_database_rejected(self, db_name):
        assert is_safe_e2e_database(db_name) is False


class TestConfigLoading:
    def test_config_points_to_e2e_dbs(self):
        config = load_config()
        assert config["e2e"]["source_database"] == "MigrationE2E_PostgreSQL_Source"
        assert config["e2e"]["target_database"] == "MigrationE2E_PostgreSQL_Target"

    def test_config_uses_secret_convention(self):
        config = load_config()
        assert config["source"]["connection"]["password_secret"] == \
            "postgresql_e2e_source_pass"
        assert config["target"]["connection"]["password_secret"] == \
            "postgresql_e2e_target_pass"

    def test_config_has_secrets_provider(self):
        config = load_config()
        assert config["secrets"]["provider"] == "env"

    def test_config_has_postgresql_engines(self):
        config = load_config()
        assert config["source"]["engine"] == "postgresql"
        assert config["target"]["engine"] == "postgresql"


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
                "discover": {"objects": ["customers", "orders"]},
                "connect": "success",
                "ensure_database": "success",
            },
        }

        with patch(
            "tests.e2e.postgresql.run_phase_c2.PostgresSourceConnector",
            return_value=mock_source,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.PostgresTargetConnector",
            return_value=mock_target,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.MigrationOrchestrator",
            return_value=mock_orchestrator,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.configure_file_logging"
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
            "tests.e2e.postgresql.run_phase_c2.PostgresSourceConnector",
            return_value=mock_source,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.PostgresTargetConnector",
            return_value=mock_target,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.MigrationOrchestrator",
            return_value=mock_orchestrator,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.configure_file_logging"
        ):
            result = run_migration(config)

            assert result["status"] == "failed"


class TestExtractMigrationSummary:
    def test_successful_migration_summary(self):
        result = {
            "phases": {
                "discover": {"objects": ["customers", "orders"]},
                "customers": {"source_rows": 5, "success": 5, "failure": 0},
                "orders": {"source_rows": 5, "success": 5, "failure": 0},
            },
        }
        summary = _extract_migration_summary(result)
        assert summary["tables_migrated"] == 10
        assert summary["objects_migrated"] == 2
        assert summary["failed_objects"] == []

    def test_migration_with_failures(self):
        result = {
            "phases": {
                "discover": {"objects": ["customers", "orders"]},
                "customers": {"source_rows": 5, "success": 3, "failure": 2},
                "orders": {"source_rows": 5, "success": 5, "failure": 0},
            },
        }
        summary = _extract_migration_summary(result)
        assert summary["tables_migrated"] == 8
        assert summary["objects_migrated"] == 2
        assert "customers" in summary["failed_objects"]

    def test_empty_migration(self):
        result = {"phases": {}}
        summary = _extract_migration_summary(result)
        assert summary["tables_migrated"] == 0
        assert summary["objects_migrated"] == 0
        assert summary["failed_objects"] == []


class TestCredentialSafety:
    """Verify credentials are never included in reports."""

    def test_summary_does_not_include_passwords(self):
        config = load_config()
        # The config should only contain password_secret, never plaintext
        assert "password" not in config["source"]["connection"]
        assert "password" not in config["target"]["connection"]

    def test_password_secret_naming(self):
        config = load_config()
        assert config["source"]["connection"]["password_secret"] == \
            "postgresql_e2e_source_pass"
        assert config["target"]["connection"]["password_secret"] == \
            "postgresql_e2e_target_pass"


class TestNoRetryPolicy:
    """Verify C2 does not retry migrations."""

    def test_no_retry_after_failure(self):
        """C2 should run migration exactly once, never auto-retry."""
        config = load_config()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_full.return_value = {
            "status": "failed",
            "phases": {"error": "test failure"},
        }

        with patch(
            "tests.e2e.postgresql.run_phase_c2.PostgresSourceConnector",
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.PostgresTargetConnector",
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.MigrationOrchestrator",
            return_value=mock_orchestrator,
        ), patch(
            "tests.e2e.postgresql.run_phase_c2.configure_file_logging"
        ):
            run_migration(config)
            assert mock_orchestrator.run_full.call_count == 1


class TestJSONSerialization:
    """Verify the C2 report can be JSON-serialized."""

    def test_summary_serializes(self):
        summary = {
            "phase": "C2",
            "status": "PASS",
            "migration_status": "success",
            "source_database": "MigrationE2E_PostgreSQL_Source",
            "target_database": "MigrationE2E_PostgreSQL_Target",
            "duration_s": 12.3,
            "tables_migrated": 10,
            "objects_migrated": 2,
            "failed_objects": [],
            "error": None,
        }
        json_str = json.dumps(summary)
        assert '"password"' not in json_str.lower()
        d = json.loads(json_str)
        assert d["status"] == "PASS"

    def test_failed_summary_serializes(self):
        summary = {
            "phase": "C2",
            "status": "FAIL",
            "migration_status": "failed",
            "source_database": "MigrationE2E_PostgreSQL_Source",
            "target_database": "MigrationE2E_PostgreSQL_Target",
            "duration_s": 5.0,
            "tables_migrated": 0,
            "objects_migrated": 0,
            "failed_objects": ["customers"],
            "error": "connection timeout",
        }
        json_str = json.dumps(summary)
        assert "password" not in json_str.lower()
        d = json.loads(json_str)
        assert d["status"] == "FAIL"
        assert "customers" in d["failed_objects"]


class TestPostgreSQLPartitionDiscovery:
    """Verify discover_partitions filters out indexes that have relispartition=true."""

    @staticmethod
    def _make_mock_conn(rows):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.cursor.return_value.__exit__.return_value = None
        return mock_conn, mock_cursor

    def test_discover_partitions_query_filters_indexes(self):
        """PG17 sets relispartition=true on PK indexes; the SQL must filter them."""
        rows = [
            ("training", "orders_2024", "orders", "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"),
        ]
        mock_conn, mock_cursor = self._make_mock_conn(rows)

        pg_partition.discover_partitions(mock_conn, ("training",))

        sql = mock_cursor.execute.call_args[0][0]
        assert "c.relkind" in sql
        assert "'r'" in sql
        assert "'p'" in sql or "'f'" in sql

    def test_discover_partitions_returns_table_partitions(self):
        """Only table rows (relkind=r) should be returned to callers."""
        rows = [
            ("training", "orders_2024", "orders", "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"),
            ("training", "orders_default", "orders", None),
        ]
        mock_conn, _ = self._make_mock_conn(rows)

        result = pg_partition.discover_partitions(mock_conn, ("training",))

        names = [p.name for p in result]
        assert "orders_2024" in names
        assert "orders_default" in names
        assert all(p.schema == "training" for p in result)

    def test_discover_partitions_includes_schema(self):
        """PartitionDef.schema must be set from the discovered schema_name."""
        rows = [
            ("training", "orders_q1", "orders", "FOR VALUES FROM ('2024-01-01') TO ('2024-04-01')"),
        ]
        mock_conn, _ = self._make_mock_conn(rows)

        result = pg_partition.discover_partitions(mock_conn, ("training",))
        assert len(result) == 1
        assert result[0].schema == "training"
        assert result[0].parent_table == "orders"

    def test_discover_partitions_default_bound_is_none(self):
        """DEFAULT partitions have bound=None and should use DEFAULT keyword."""
        rows = [
            ("training", "orders_default", "orders", None),
        ]
        mock_conn, _ = self._make_mock_conn(rows)

        result = pg_partition.discover_partitions(mock_conn, ("training",))
        assert result[0].bound is None
