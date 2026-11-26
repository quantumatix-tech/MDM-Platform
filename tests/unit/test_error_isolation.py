from __future__ import annotations

from unittest.mock import MagicMock

from core.connectors.base import Column, ForeignKey, Schema, SourceConnector, TargetConnector, UpsertResult
from core.orchestrator import MigrationOrchestrator


def _schema(name: str) -> Schema:
    return Schema(name=name, columns=[Column(name="id", source_type="integer")])


def _orchestrator(stop_on_error: bool = False):
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    source._config = {}
    target._config = {}
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
        {"migration": {"stop_on_error": stop_on_error}},
    )
    return orchestrator, source, target


def test_creation_failure_isolated_and_reported_while_other_objects_continue():
    orchestrator, _, target = _orchestrator()

    def create_object(schema):
        if schema.name == "broken":
            raise RuntimeError("unsupported table definition")

    target.create_object_if_missing.side_effect = create_object

    result = orchestrator.run_full()

    assert result["status"] == "partial_success"
    assert [call.args[0].name for call in target.create_object_if_missing.call_args_list] == [
        "good_first",
        "broken",
        "good_last",
    ]
    assert result["phases"]["broken"]["status"] == "creation_failed"
    assert result["phases"]["good_first"]["failure"] == 0
    assert result["phases"]["good_last"]["failure"] == 0
    assert result["phases"]["object_failures"] == [
        {
            "phase": "create_table",
            "object": "broken",
            "error": "unsupported table definition",
        }
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
    assert [call.args[0] for call in target.upsert_batch.call_args_list] == [
        "good_first",
        "broken",
        "good_last",
    ]
    assert result["phases"]["broken"]["failure"] == 1
    assert "data_load failed for object 'broken': target write failed" in result["phases"]["broken"]["errors"]
    assert result["phases"]["good_first"]["failure"] == 0
    assert result["phases"]["good_last"]["failure"] == 0


def test_stop_on_error_stops_after_first_object_failure():
    orchestrator, _, target = _orchestrator(stop_on_error=True)

    def create_object(schema):
        if schema.name == "broken":
            raise RuntimeError("unsupported table definition")

    target.create_object_if_missing.side_effect = create_object

    result = orchestrator.run_full()

    assert result["status"] == "failed"
    assert [call.args[0].name for call in target.create_object_if_missing.call_args_list] == [
        "good_first",
        "broken",
    ]
    assert target.upsert_batch.call_count == 0
    assert result["phases"]["object_failures"][0]["object"] == "broken"


def _dependency_orchestrator(objects, foreign_keys, stop_on_error=False):
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    source._config = {}
    target._config = {}
    schemas = {
        name: Schema(
            name=name,
            schema_name="APP",
            columns=[Column(name="id", source_type="integer")],
            foreign_keys=foreign_keys.get(name, []),
        )
        for name in objects
    }
    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas[name]
    source.get_object_count.return_value = 1
    source.export_full.side_effect = lambda name, **kw: iter([{"id": 1}])
    target.get_object_count.return_value = 1
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    orchestrator = MigrationOrchestrator(
        source, target, {"migration": {"stop_on_error": stop_on_error}}
    )
    orchestrator.validate = MagicMock(return_value={"status": "success", "checks": {}})
    return orchestrator, target


def test_failed_parent_skips_transitive_children_but_continues_independent_tables():
    objects = ["child", "grandchild", "independent", "parent"]
    orchestrator, target = _dependency_orchestrator(
        objects,
        {
            "child": [ForeignKey("fk_child_parent", ["parent_id"], "parent", ["id"], "APP")],
            "grandchild": [ForeignKey("fk_grandchild_child", ["child_id"], "child", ["id"], "APP")],
        },
    )

    def upsert(name, rows, schema):
        if name == "parent":
            return UpsertResult(failure_count=1, errors=["parent row rejected"])
        return UpsertResult(success_count=1)

    target.upsert_batch.side_effect = upsert
    result = orchestrator.run_full()
    phases = result["phases"]

    assert phases["data_load_order"] == ["independent", "parent", "child", "grandchild"]
    assert [call.args[0] for call in target.upsert_batch.call_args_list] == ["independent", "parent"]
    assert phases["independent"]["status"] == "SUCCESS"
    assert phases["parent"]["status"] == "FAILED"
    assert phases["child"]["status"] == "SKIPPED"
    assert "parent" in phases["child"]["reason"]
    assert phases["grandchild"]["status"] == "SKIPPED"
    assert "child" in phases["grandchild"]["reason"]
    assert phases["data"]["counts"] == {"SUCCESS": 1, "FAILED": 1, "SKIPPED": 2}
    assert orchestrator.validate.call_args.args[0] == ["independent"]


def test_stop_on_error_records_failure_and_marks_all_unattempted_tables_skipped():
    objects = ["child", "independent", "parent"]
    orchestrator, target = _dependency_orchestrator(
        objects,
        {"child": [ForeignKey("fk_child_parent", ["parent_id"], "parent", ["id"], "APP")]},
        stop_on_error=True,
    )

    def upsert(name, rows, schema):
        if name == "parent":
            return UpsertResult(failure_count=1, errors=["parent row rejected"])
        return UpsertResult(success_count=1)

    target.upsert_batch.side_effect = upsert
    result = orchestrator.run_full()
    phases = result["phases"]

    assert phases["data_load_order"] == ["independent", "parent", "child"]
    assert [call.args[0] for call in target.upsert_batch.call_args_list] == ["independent", "parent"]
    assert phases["parent"]["status"] == "FAILED"
    assert phases["child"]["status"] == "SKIPPED"
    assert "stop_on_error=true" in phases["child"]["reason"]
    assert phases["data"]["counts"] == {"SUCCESS": 1, "FAILED": 1, "SKIPPED": 1}
    assert all(error.get("object") != "child" for error in phases["object_failures"])


def test_successful_foreign_key_tables_load_parent_before_child():
    objects = ["child", "parent"]
    orchestrator, target = _dependency_orchestrator(
        objects,
        {"child": [ForeignKey("fk_child_parent", ["parent_id"], "parent", ["id"], "APP")]},
    )

    result = orchestrator.run_full()

    assert [call.args[0] for call in target.upsert_batch.call_args_list] == ["parent", "child"]
    assert result["phases"]["parent"]["status"] == "SUCCESS"
    assert result["phases"]["child"]["status"] == "SUCCESS"
