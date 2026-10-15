from __future__ import annotations

from unittest.mock import MagicMock

from core.connectors.base import (
    Column,
    Schema,
    SourceConnector,
    TargetConnector,
    TriggerDef,
    UpsertResult,
)
from core.connectors.postgresql.target import PostgresTargetConnector
from core.orchestrator import MigrationOrchestrator


def _audit_trigger() -> TriggerDef:
    return TriggerDef(
        name="tr_audit_order",
        table="orders",
        schema_name="training",
        ddl=(
            'CREATE TRIGGER "tr_audit_order" AFTER INSERT ON '
            'training.orders FOR EACH ROW EXECUTE FUNCTION training.audit_order()'
        ),
    )


def test_postgresql_target_disables_matching_existing_trigger_before_load():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    target._conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("O",)

    suspended = target.suspend_triggers_for_data_load([_audit_trigger()])

    cursor = target._conn.cursor.return_value.__enter__.return_value
    assert cursor.execute.call_args_list[0].args[0].startswith("SELECT t.tgenabled")
    assert cursor.execute.call_args_list[0].args[1] == (
        "training", "orders", "tr_audit_order"
    )
    assert cursor.execute.call_args_list[1].args[0] == (
        'ALTER TABLE "training"."orders" DISABLE TRIGGER "tr_audit_order"'
    )
    assert suspended == [_audit_trigger()]
    target._conn.commit.assert_called_once_with()


def test_full_migration_twice_keeps_audit_rows_and_table_counts_in_sync():
    source_rows = {
        "orders": [{"orderid": order_id} for order_id in range(1, 6)],
        "orderaudit": [{"auditid": audit_id} for audit_id in range(1, 6)],
    }
    target_state = {"trigger_enabled": False, "rows": {name: [] for name in source_rows}}
    trigger = _audit_trigger()

    source = MagicMock(spec=SourceConnector)
    source.list_objects.return_value = list(source_rows)
    source.get_schema.side_effect = lambda name: Schema(
        name=name,
        schema_name="training",
        columns=[Column(name="id", source_type="integer")],
    )
    source.get_object_count.side_effect = lambda name, schema_name=None: len(source_rows[name])
    source.export_full.side_effect = lambda name, schema_name=None: iter(source_rows[name])
    source.list_extensions.return_value = []
    source.list_schemas.return_value = []
    source.list_types.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.get_all_triggers.return_value = [trigger]

    target = MagicMock(spec=TargetConnector)
    target.inspect_schema.return_value = None

    def suspend_existing_triggers(triggers):
        if target_state["trigger_enabled"]:
            target_state["trigger_enabled"] = False
            return list(triggers)
        return []

    def clear_managed_tables(objects):
        for name in objects:
            target_state["rows"][name] = []
        return list(objects)

    def upsert_rows(name, rows, schema=None):
        batch = list(rows)
        target_state["rows"][name].extend(batch)
        if name == "orders" and target_state["trigger_enabled"]:
            target_state["rows"]["orderaudit"].extend(
                [{"auditid": f"trigger-{row['orderid']}"} for row in batch]
            )
        return UpsertResult(success_count=len(batch))

    target.suspend_triggers_for_data_load.side_effect = suspend_existing_triggers
    target.clear_objects_for_full_sync.side_effect = clear_managed_tables
    target.upsert_batch.side_effect = upsert_rows
    target.create_trigger.side_effect = lambda _trigger: target_state.__setitem__(
        "trigger_enabled", True
    )

    orchestrator = MigrationOrchestrator(
        source,
        target,
        {"source": {"engine": "postgresql"}, "target": {"engine": "postgresql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    orchestrator.run_full()
    assert len(target_state["rows"]["orderaudit"]) == 5

    orchestrator.run_full()

    assert len(source_rows["orderaudit"]) == 5
    assert len(target_state["rows"]["orderaudit"]) == 5
    for table_name, rows in source_rows.items():
        assert len(target_state["rows"][table_name]) == len(rows)
    assert target.suspend_triggers_for_data_load.call_count == 2
