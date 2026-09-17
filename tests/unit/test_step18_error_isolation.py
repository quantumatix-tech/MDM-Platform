from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column,
    ForeignKey,
    Schema,
    SourceConnector,
    TargetConnector,
    UpsertResult,
)
from core.orchestrator import MigrationOrchestrator


def _schema(name: str) -> Schema:
    return Schema(name=name, columns=[Column(name="id", source_type="integer")])


def _orchestrator():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    objects = ["good_first", "broken", "good_last"]
    schemas = {name: _schema(name) for name in objects}

    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas[name]
    source.get_object_count.return_value = 1
    source.export_full.side_effect = lambda name, **kw: iter([{"id": 1}])
    target.get_object_count.return_value = 1
    target.upsert_batch.return_value = UpsertResult(success_count=1)

    orchestrator = MigrationOrchestrator(
        source,
        target,
        {"migration": {"stop_on_error": False}},
    )
    return orchestrator, source, target


def test_creation_failure_isolated_and_reported():
    orchestrator, _, target = _orchestrator()

    def create_object(schema):
        if schema.name == "broken":
            raise RuntimeError("unsupported table definition")

    target.create_object_if_missing.side_effect = create_object

    result = orchestrator.run_full()

    assert result["status"] == "partial_success"
    assert [c.args[0].name for c in target.create_object_if_missing.call_args_list] == [
        "good_first", "broken", "good_last",
    ]
    assert result["phases"]["broken"]["status"] == "creation_failed"
    assert result["phases"]["good_first"]["failure"] == 0
    assert result["phases"]["good_last"]["failure"] == 0
    assert result["phases"]["object_failures"] == [
        {"phase": "create_table", "object": "broken", "error": "unsupported table definition"},
    ]


def test_data_load_failure_isolated_and_good_objects_continue():
    orchestrator, _, target = _orchestrator()

    def upsert(object_name, rows, schema):
        if object_name == "broken":
            raise RuntimeError("target write failed")
        return UpsertResult(success_count=1)

    target.upsert_batch.side_effect = upsert

    result = orchestrator.run_full()

    assert result["status"] == "partial_success"
    assert [c.args[0] for c in target.upsert_batch.call_args_list] == [
        "good_first", "broken", "good_last",
    ]
    assert result["phases"]["broken"]["failure"] == 1
    assert "data_load failed for object 'broken': target write failed" in result["phases"]["broken"]["errors"]
    assert result["phases"]["good_first"]["failure"] == 0
    assert result["phases"]["good_last"]["failure"] == 0


def test_constraint_failure_is_controlled_error_not_crash():
    orchestrator, _, target = _orchestrator()

    def apply_constraints(schema):
        if schema.name == "broken":
            raise RuntimeError("invalid foreign key reference")

    target.apply_constraints.side_effect = apply_constraints

    result = orchestrator.run_full()

    # Constraint failure must be captured as a controlled per-object error,
    # not crash the migration or corrupt unrelated state.
    assert "apply_constraints" in result["phases"]
    assert result["phases"]["apply_constraints"]["broken"] == "error: invalid foreign key reference"
    assert result["phases"]["apply_constraints"]["good_first"] == "success"
    assert result["phases"]["apply_constraints"]["good_last"] == "success"
    assert "invalid foreign key reference" in result["phases"]["apply_constraints"]["broken"]


def test_invalid_unsupported_ddl_produces_controlled_error():
    orchestrator, _, target = _orchestrator()

    def create_object(schema):
        if schema.name == "broken":
            raise ValueError("unsupported DDL: unknown column type")

    target.create_object_if_missing.side_effect = create_object

    result = orchestrator.run_full()

    assert result["status"] == "partial_success"
    assert result["phases"]["object_failures"][0]["phase"] == "create_table"
    assert result["phases"]["object_failures"][0]["object"] == "broken"
    assert "unknown column type" in result["phases"]["object_failures"][0]["error"]


def test_error_reporting_identifies_phase_and_object():
    orchestrator, _, target = _orchestrator()

    def create_object(schema):
        if schema.name == "broken":
            raise RuntimeError("table creation rejected")

    target.create_object_if_missing.side_effect = create_object

    result = orchestrator.run_full()

    failure = result["phases"]["object_failures"][0]
    assert failure["phase"] == "create_table"
    assert failure["object"] == "broken"
    assert "table creation rejected" in failure["error"]


def test_missing_object_handled_without_corrupting_other_state():
    orchestrator, source, target = _orchestrator()

    source.list_objects.return_value = ["only_one"]
    source.get_schema.side_effect = lambda name, **kw: _schema(name)
    target.create_object_if_missing.side_effect = RuntimeError("object not found")

    result = orchestrator.run_full()

    assert result["status"] == "failed"
    assert result["phases"]["only_one"]["status"] == "creation_failed"
    assert result["phases"]["object_failures"][0]["object"] == "only_one"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
