from __future__ import annotations

from unittest.mock import MagicMock

import psycopg

from core.connectors.base import Column, ForeignKey, Schema
from core.connectors.postgresql import PostgresTargetConnector
from core.migration_plan import (
    BLOCK,
    CREATE_AND_MIGRATE,
    MIGRATE,
    PostgresMigrationPlanner,
    SchemaComparison,
    compare_schemas,
    decide_action,
    validate_postgresql_full_config,
)
from core.orchestrator import MigrationOrchestrator
from core.reporting.report_builder import ReportBuilder


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


def _schema(name: str = "accounts", *, nullable: bool = False) -> Schema:
    return Schema(
        name=name,
        columns=[Column(name="id", source_type="integer", nullable=nullable)],
        primary_key=["id"],
    )


def _planner(source_schema: Schema | None = None, target_schema: Schema | None = None):
    source = MagicMock()
    target = MagicMock()
    source_schema = source_schema or _schema()
    source.list_objects.return_value = [source_schema.name]
    source.get_schema.return_value = source_schema
    source.get_object_count.return_value = 7
    target.list_objects.return_value = [] if target_schema is None else [("public", target_schema.name)]
    target.inspect_schema.return_value = target_schema
    target.get_object_count.return_value = 3
    return PostgresMigrationPlanner(source, target, _config()), source, target


def test_config_validation_reports_required_postgresql_connection_fields_without_secrets():
    issues = validate_postgresql_full_config({"source": {"engine": "postgresql"}})

    messages = " ".join(issue.message for issue in issues)
    assert "source.connection" in messages
    assert "Missing target configuration" in messages
    assert "provided-by-test" not in messages


def test_connectivity_failure_blocks_plan_with_actionable_source_error():
    planner, source, target = _planner()
    source.connect.side_effect = RuntimeError("connection refused")

    plan = planner.build()

    assert not plan.ready
    assert plan.blockers[0].code == "SOURCE_CONNECT_FAILED"
    assert "connection refused" in plan.blockers[0].message
    target.connect.assert_not_called()


def test_missing_target_object_is_planned_for_create_and_migrate_with_source_row_count():
    planner, _, target = _planner()

    plan = planner.build()

    object_plan = plan.objects[0]
    assert plan.ready
    assert object_plan.action == CREATE_AND_MIGRATE
    assert object_plan.schema_comparison.status == "TARGET_MISSING"
    assert object_plan.source_row_count == 7
    assert object_plan.target_row_count is None
    target.inspect_schema.assert_not_called()


def test_matching_target_schema_is_planned_for_migration_and_captures_counts():
    schema = _schema()
    planner, _, _ = _planner(schema, _schema())

    plan = planner.build()

    object_plan = plan.objects[0]
    assert plan.ready
    assert object_plan.action == MIGRATE
    assert object_plan.schema_comparison.status == "COMPATIBLE"
    assert object_plan.source_row_count == 7
    assert object_plan.target_row_count == 3
    assert object_plan.warnings


def test_schema_mismatch_blocks_object_and_reports_difference():
    planner, _, _ = _planner(_schema(nullable=False), _schema(nullable=True))

    plan = planner.build()

    object_plan = plan.objects[0]
    assert not plan.ready
    assert object_plan.action == BLOCK
    assert object_plan.schema_comparison.status == "MISMATCH"
    assert "nullability differs" in object_plan.blockers[0]


def test_action_decision_and_dependencies_are_deterministic():
    schema = _schema("orders")
    schema.foreign_keys = [
        ForeignKey("orders_account_fk", ["account_id"], "accounts", ["id"]),
        ForeignKey("orders_customer_fk", ["customer_id"], "customers", ["id"]),
    ]
    planner, _, _ = _planner(schema)

    plan = planner.build()

    assert plan.objects[0].dependencies == ["accounts", "customers"]
    assert decide_action(True, SchemaComparison("COMPATIBLE"), []) == MIGRATE
    assert decide_action(True, SchemaComparison("MISMATCH"), []) == BLOCK


def test_dry_run_builds_plan_without_calling_create_or_upsert():
    source = MagicMock()
    target = MagicMock()
    schema = _schema()
    source.list_objects.return_value = ["accounts"]
    source.get_schema.return_value = schema
    source.get_object_count.return_value = 7
    target.list_objects.return_value = []

    result = MigrationOrchestrator(source, target, _config()).run_dry_run()

    assert result["status"] == "ready"
    assert result["preflight"]["objects"][0]["action"] == CREATE_AND_MIGRATE
    target.ensure_database_exists.assert_not_called()
    target.create_object_if_missing.assert_not_called()
    target.upsert_batch.assert_not_called()


def test_report_includes_the_preflight_plan_in_json_and_html():
    source = MagicMock()
    target = MagicMock()
    source.list_objects.return_value = ["accounts"]
    source.get_schema.return_value = _schema()
    source.get_object_count.return_value = 7
    target.list_objects.return_value = []
    result = MigrationOrchestrator(source, target, _config()).run_dry_run()

    report = ReportBuilder(result, 0, 1)

    report_json = report.build_json()
    report_html = report.build_html()

    assert report_json["preflight"]["ready"] is True
    assert report_json["preflight"]["objects"][0]["action"] == CREATE_AND_MIGRATE
    assert "PostgreSQL Migration Plan" in report_html
    assert "accounts" in report_html
    assert "public.accounts" not in report_html
    assert "Target Missing" in report_html
    assert "Create & Migrate" in report_html
    assert "CREATE_AND_MIGRATE" not in report_html
    assert "TARGET_MISSING" not in report_html
    assert ">0</td>" in report_html
    assert "None" not in report_html


def test_report_renders_compatible_schema_and_migrate_action_with_human_labels():
    source = MagicMock()
    target = MagicMock()
    source.list_objects.return_value = ["accounts"]
    source.get_schema.return_value = _schema()
    source.get_object_count.return_value = 7
    target.list_objects.return_value = [("public", "accounts")]
    target.inspect_schema.return_value = _schema()
    target.get_object_count.return_value = 3
    result = MigrationOrchestrator(source, target, _config()).run_dry_run()

    report_html = ReportBuilder(result, 0, 1).build_html()

    assert "Compatible" in report_html
    assert "Update & Migrate" in report_html
    assert "COMPATIBLE" not in report_html
    assert ">3</td>" in report_html


def test_report_renders_schema_mismatch_and_block_with_human_labels():
    result = {
        "run_id": "report-test",
        "mode": "dry-run",
        "status": "blocked",
        "phases": {},
        "preflight": {
            "ready": False,
            "source_connected": True,
            "target_connected": True,
            "blockers": [],
            "objects": [
                {
                    "object_name": "accounts",
                    "schema_comparison": {"status": "MISMATCH"},
                    "source_row_count": 7,
                    "target_row_count": 3,
                    "action": "BLOCK",
                    "readiness": "BLOCKED",
                    "blockers": ["Column 'id' type differs."],
                    "warnings": [],
                }
            ],
        },
    }

    report_html = ReportBuilder(result, 0, 1).build_html()

    assert "Schema Mismatch" in report_html
    assert "Migration Blocked" in report_html
    assert "MISMATCH" not in report_html
    assert ">BLOCK</span>" not in report_html


def test_target_clears_preflight_read_transaction_before_enabling_autocommit(monkeypatch):
    events: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_):
            events.append("execute")

        def fetchone(self):
            return (1,)

    class Connection:
        def __init__(self, transaction_open: bool = False):
            self.transaction_open = transaction_open
            self._autocommit = False

        def rollback(self):
            events.append("rollback")
            self.transaction_open = False

        @property
        def autocommit(self):
            return self._autocommit

        @autocommit.setter
        def autocommit(self, value):
            if value and self.transaction_open:
                raise RuntimeError("autocommit enabled while transaction is open")
            events.append(f"autocommit={value}")
            self._autocommit = value

        def cursor(self):
            return Cursor()

        def close(self):
            events.append("close")

    connector = PostgresTargetConnector(
        {
            "host": "localhost",
            "database": "migration_target",
            "username": "migration_user",
            "ssl": False,
        }
    )
    connector._conn = Connection(transaction_open=True)
    monkeypatch.setattr(psycopg, "connect", lambda **_: Connection())

    connector.ensure_database_exists()

    assert events.index("rollback") < events.index("autocommit=True")
