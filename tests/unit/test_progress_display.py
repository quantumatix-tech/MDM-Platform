from __future__ import annotations

from unittest.mock import MagicMock

from core.connectors.base import Column, Schema
from core.orchestrator import MigrationOrchestrator
from core.progress_display import NoopProgressDisplay, _PHASE_LABELS, _preflight_rows


def _config() -> dict:
    connection = {
        "host": "localhost",
        "port": 5432,
        "database": "migration_test",
        "username": "migration_user",
        "password": "provided-by-test",
    }
    return {
        "source": {"engine": "postgresql", "connection": dict(connection)},
        "target": {"engine": "postgresql", "connection": dict(connection)},
    }


def test_preflight_rows_use_human_labels_and_zero_for_missing_target_rows():
    rows = _preflight_rows(
        {
            "objects": [
                {
                    "object_name": "customers",
                    "schema_comparison": {"status": "COMPATIBLE"},
                    "source_row_count": 5,
                    "target_row_count": 4,
                    "action": "MIGRATE",
                },
                {
                    "object_name": "orders",
                    "schema_comparison": {"status": "TARGET_MISSING"},
                    "source_row_count": 4,
                    "target_row_count": None,
                    "action": "CREATE_AND_MIGRATE",
                },
                {
                    "object_name": "products",
                    "schema_comparison": {"status": "MISMATCH"},
                    "source_row_count": 4,
                    "target_row_count": 0,
                    "action": "BLOCK",
                },
            ]
        }
    )

    assert rows == [
        ("customers", "Compatible", 5, 4, "Update & Migrate"),
        ("orders", "Target Missing", 4, 0, "Create & Migrate"),
        ("products", "Schema Mismatch", 4, 0, "Blocked"),
    ]
    assert ("preflight", "PostgreSQL Preflight") in _PHASE_LABELS


def test_noop_terminal_preflight_uses_human_labels(capsys):
    display = NoopProgressDisplay()
    display.record_preflight(
        {
            "objects": [
                {
                    "object_name": "orders",
                    "schema_comparison": {"status": "TARGET_MISSING"},
                    "source_row_count": 4,
                    "target_row_count": None,
                    "action": "CREATE_AND_MIGRATE",
                    "blockers": [],
                }
            ],
            "blockers": [],
        }
    )

    output = capsys.readouterr().out
    assert "Target Missing" in output
    assert "Create & Migrate" in output
    assert "target=0" in output
    assert "TARGET_MISSING" not in output
    assert "CREATE_AND_MIGRATE" not in output
    assert "None" not in output


def test_full_run_reports_blocked_preflight_before_database_work():
    source = MagicMock()
    target = MagicMock()
    status = MagicMock()
    source.list_objects.return_value = ["accounts"]
    source.get_schema.return_value = Schema(
        name="accounts",
        columns=[Column(name="id", source_type="integer", nullable=False)],
        primary_key=["id"],
    )
    source.get_object_count.return_value = 1
    target.list_objects.return_value = [("public", "accounts")]
    target.inspect_schema.return_value = Schema(
        name="accounts",
        columns=[Column(name="id", source_type="text", nullable=False)],
        primary_key=["id"],
    )
    target.get_object_count.return_value = 1

    result = MigrationOrchestrator(source, target, _config(), status_server=status).run_full()

    assert result["status"] == "blocked"
    status.record_preflight.assert_called_once_with(result["preflight"])
    target.ensure_database_exists.assert_not_called()
