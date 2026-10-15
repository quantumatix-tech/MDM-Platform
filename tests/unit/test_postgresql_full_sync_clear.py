from __future__ import annotations

from unittest.mock import MagicMock

from core.connectors.base import (
    Column,
    ForeignKey,
    Schema,
    SourceConnector,
    TargetConnector,
    UpsertResult,
)
from core.connectors.postgresql.target import PostgresTargetConnector
from core.orchestrator import MigrationOrchestrator


def test_postgresql_full_sync_invokes_clear_for_source_managed_tables():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    source.get_partitioned_tables = MagicMock(return_value=[])
    source.list_objects.return_value = ["customers", "orders"]
    source.get_schema.side_effect = lambda name: Schema(
        name=name, columns=[Column(name="id", source_type="integer")]
    )
    source.get_object_count.return_value = 0
    source.export_full.side_effect = lambda *args, **kwargs: iter([])
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.get_all_triggers.return_value = []
    target.upsert_batch.return_value = UpsertResult()
    target.inspect_schema.return_value = None
    target.apply_constraints.return_value = None
    target.create_view.return_value = None

    orchestrator = MigrationOrchestrator(
        source,
        target,
        {"source": {"engine": "postgresql"}, "target": {"engine": "postgresql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    orchestrator.run_full()

    target.clear_objects_for_full_sync.assert_called_once_with(["customers", "orders"])


def test_postgresql_full_sync_deletes_fk_children_before_parents():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    target._managed_schemas = {
        "customers": Schema(name="customers", schema_name="training"),
        "orders": Schema(
            name="orders",
            schema_name="training",
            foreign_keys=[
                ForeignKey(
                    name="fk_orders_customers",
                    columns=["customerid"],
                    ref_table="customers",
                    ref_columns=["customerid"],
                    ref_schema="training",
                )
            ],
        ),
    }

    cleared = target.clear_objects_for_full_sync(["customers", "orders"])

    cursor = target._conn.cursor.return_value.__enter__.return_value
    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        'DELETE FROM "training"."orders"',
        'DELETE FROM "training"."customers"',
    ]
    assert cleared == ["orders", "customers"]
    target._conn.commit.assert_called_once_with()
    target._conn.rollback.assert_not_called()


def test_postgresql_full_sync_does_not_clear_target_only_tables():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    target._managed_schemas = {
        "source_table": Schema(name="source_table", schema_name="training"),
    }

    cleared = target.clear_objects_for_full_sync(["source_table"])

    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.execute.assert_called_once_with('DELETE FROM "training"."source_table"')
    assert "manual_table" not in str(cursor.execute.call_args_list)
    assert cleared == ["source_table"]


def test_postgresql_full_sync_clear_is_noop_for_empty_managed_set():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()

    assert target.clear_objects_for_full_sync(["target_only_table"]) == []
    target._conn.cursor.assert_not_called()
