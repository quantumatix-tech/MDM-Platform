from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column, FunctionDef, Schema, SynonymDef, TriggerDef,
    UpsertResult,
)
from core.orchestrator import MigrationOrchestrator


def _orchestrator() -> tuple[MigrationOrchestrator, MagicMock, MagicMock]:
    source = MagicMock()
    target = MagicMock()
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = Schema(
        name="customers", columns=[Column(name="id", source_type="int")]
    )
    source.get_object_count.return_value = 1
    source.export_full.return_value = iter([{"id": 1}])
    source.get_partitioned_tables.return_value = []
    source.list_functions.return_value = []
    source.get_all_triggers.return_value = []
    source.list_views.return_value = []
    source.list_materialized_views.return_value = []
    source.list_synonyms.return_value = []
    source.list_comments.return_value = []
    source.list_grants.return_value = []
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    target.apply_constraints.return_value = []
    orchestrator = MigrationOrchestrator(
        source, target,
        {"source": {"engine": "mysql"}, "target": {"engine": "mysql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})
    return orchestrator, source, target


@pytest.mark.parametrize(
    "failure_kind", [
        "constraint", "function", "trigger", "trigger_missing_parent", "synonym"
    ]
)
def test_required_object_failure_prevents_overall_success(failure_kind: str):
    orchestrator, source, target = _orchestrator()
    if failure_kind == "constraint":
        target.apply_constraints.return_value = ["index IX_customers failed: SQL error"]
    elif failure_kind == "function":
        source.list_functions.return_value = [
            FunctionDef(name="calculate", ddl="CREATE FUNCTION calculate()"),
        ]
        target.create_function.side_effect = RuntimeError("routine DDL rejected")
    elif failure_kind in {"trigger", "trigger_missing_parent"}:
        source.get_all_triggers.return_value = [
            TriggerDef(name="customers_insert", table="customers", ddl="CREATE TRIGGER ..."),
        ]
        if failure_kind == "trigger":
            target.create_trigger.side_effect = RuntimeError("trigger DDL rejected")
        else:
            target.create_trigger.return_value = False
    else:
        source.list_synonyms.return_value = [
            SynonymDef(name="customer_alias", schema_name="sales", base_object="dbo.customers"),
        ]
        target.create_synonym.side_effect = RuntimeError("synonym DDL rejected")

    result = orchestrator.run_full()

    assert result["status"] in {"partial_success", "failed"}
    assert result["phases"]["validation"]["status"] == "success"
    failures = result["phases"]["object_failures"]
    if failure_kind != "synonym":
        expected_phase = "trigger" if failure_kind == "trigger_missing_parent" else failure_kind
        assert any(expected_phase in item["phase"] for item in failures)
    else:
        assert result["phases"]["synonyms"]["sales.customer_alias"].startswith("skipped:")
    if failure_kind != "synonym":
        assert any(item["error"] for item in failures)
