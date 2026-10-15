from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column,
    ForeignKey,
    Schema,
    SourceConnector,
    TargetConnector,
    TriggerDef,
    UpsertResult,
)
from core.orchestrator import MigrationOrchestrator
from core.connectors.mssql.target import MSSQLTargetConnector
from core.connectors.mssql.objects.table import upsert_table_data


def _target() -> tuple[MSSQLTargetConnector, MagicMock, MagicMock]:
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector({"database": "target"})
    target._conn = conn
    return target, conn, cursor


def test_full_sync_clears_modified_extra_and_missing_source_rows_before_reload():
    """FULL replacement removes stale target state before source rows reload."""
    target, _, cursor = _target()
    target._managed_schemas = {
        "customers": Schema(
            name="customers",
            schema_name="training",
            columns=[Column(name="customer_id", source_type="int")],
        )
    }

    # Covers the cleanup required when rows were modified, added, or deleted
    # on the target: every existing source-managed row is removed first.
    assert target.clear_objects_for_full_sync(["customers"]) == ["customers"]
    cursor.execute.assert_called_once_with('DELETE FROM "training"."customers"')


def test_full_sync_repeat_clears_same_managed_table_each_run():
    target, conn, cursor = _target()
    target._managed_schemas = {
        "customers": Schema(name="customers", schema_name="training")
    }

    first = target.clear_objects_for_full_sync(["customers"])
    second = target.clear_objects_for_full_sync(["customers"])

    assert first == second == ["customers"]
    assert cursor.execute.call_count == 2
    assert conn.commit.call_count == 2


def test_full_sync_preserves_target_only_tables_and_data():
    target, conn, cursor = _target()
    target._managed_schemas = {
        "customers": Schema(name="customers", schema_name="training")
    }

    cleared = target.clear_objects_for_full_sync(
        ["customers", "target_only_test"]
    )

    assert cleared == ["customers"]
    assert cursor.execute.call_count == 1
    assert "target_only_test" not in cursor.execute.call_args.args[0]
    conn.commit.assert_called_once_with()


def test_mssql_reconciliation_drops_extra_columns_only_from_managed_table():
    target, conn, cursor = _target()
    cursor.fetchall.return_value = [
        ("CustomerID", "int", 4, 10, 0, True, False, False, False, "sys"),
        ("Name", "nvarchar", 80, 0, 0, True, False, False, False, "sys"),
        ("ExtraTargetColumn", "nvarchar", 200, 0, 0, True, False, False, False, "sys"),
    ]
    schema = Schema(
        name="Customers",
        schema_name="training",
            columns=[
                Column(name="CustomerID", source_type="int", precision=10),
                Column(name="Name", source_type="nvarchar", size=40),
            ],
    )

    dropped = target.reconcile_mssql_table(schema)

    assert dropped == {
        "dropped_columns": ["ExtraTargetColumn"],
        "altered_columns": [],
    }
    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert any(
        sql == 'ALTER TABLE "training"."Customers" DROP COLUMN "ExtraTargetColumn"'
        for sql in executed
    )
    assert all("TargetOnly_Test" not in sql for sql in executed)
    conn.commit.assert_called()


def test_mssql_reconciliation_failure_propagates_without_cascade():
    target, conn, cursor = _target()
    cursor.fetchall.return_value = [("CustomerID",), ("ExtraTargetColumn",)]
    cursor.execute.side_effect = [None, RuntimeError("dependent index blocks drop")]
    schema = Schema(
        name="Customers", schema_name="training",
        columns=[Column(name="CustomerID", source_type="int")],
    )

    with pytest.raises(RuntimeError, match="dependent index blocks drop"):
        target.reconcile_mssql_table(schema)

    assert all("CASCADE" not in str(call.args[0]).upper() for call in cursor.execute.call_args_list)
    conn.rollback.assert_called_once_with()


def test_full_migration_reconciles_managed_columns_and_preserves_target_only_table():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock()
    source.list_objects.return_value = ["Customers"]
    source.get_schema.return_value = Schema(
        name="Customers", schema_name="training",
        columns=[Column(name="CustomerID", source_type="int")],
        primary_key=["CustomerID"],
    )
    source.get_object_count.return_value = 2
    source.export_full.return_value = [{"CustomerID": 1}, {"CustomerID": 2}]
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.get_all_triggers.return_value = []

    target_tables = {
        "Customers": {"columns": {"CustomerID", "ExtraTargetColumn"}, "rows": [1, 99]},
        "TargetOnly_Test": {"columns": {"id"}, "rows": ["keep"]},
    }
    reconciliation_order = []

    def reconcile(schema):
        reconciliation_order.append(schema.name)
        target_tables[schema.name]["columns"].intersection_update(
            {column.name for column in schema.columns}
        )
        return ["ExtraTargetColumn"]

    target.reconcile_mssql_table.side_effect = reconcile
    target.suspend_triggers_for_data_load.return_value = []
    target.clear_objects_for_full_sync.side_effect = lambda names: [
        name for name in names if name in target_tables
    ]

    def load(name, rows, _schema):
        batch = list(rows)
        target_tables[name]["rows"] = [row["CustomerID"] for row in batch]
        return UpsertResult(success_count=len(batch))

    target.upsert_batch.side_effect = load
    target.inspect_schema.return_value = None
    orchestrator = MigrationOrchestrator(
        source, target,
        {"source": {"engine": "mssql"}, "target": {"engine": "mssql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    result = orchestrator.run_full()

    assert result["status"] == "success"
    assert reconciliation_order == ["Customers"]
    assert target_tables["Customers"] == {
        "columns": {"CustomerID"}, "rows": [1, 2]
    }
    assert target_tables["TargetOnly_Test"] == {
        "columns": {"id"}, "rows": ["keep"]
    }


def test_full_sync_deletes_fk_children_before_parents():
    target, _, cursor = _target()
    target._managed_schemas = {
        "customers": Schema(name="customers", schema_name="training"),
        "orders": Schema(
            name="orders",
            schema_name="training",
            foreign_keys=[ForeignKey(
                name="fk_orders_customers",
                columns=["customer_id"],
                ref_table="customers",
                ref_columns=["customer_id"],
                ref_schema="training",
            )],
        ),
    }

    cleared = target.clear_objects_for_full_sync(["customers", "orders"])

    assert cleared == ["orders", "customers"]
    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        'DELETE FROM "training"."orders"',
        'DELETE FROM "training"."customers"',
    ]


def test_full_sync_skips_clear_and_load_when_managed_child_schema_fails():
    """A failed child schema must not let FULL delete its referenced parent."""
    source = MagicMock(spec=SourceConnector)
    target = MagicMock()
    target_rows = {
        "Customers": [{"CustomerID": 7, "FullName": "Existing customer"}],
        "Orders": [{"OrderID": 91, "CustomerID": 7}],
    }
    rows_before = {name: list(rows) for name, rows in target_rows.items()}
    fk_state = {"name": "FK_Orders_Customers", "enabled": True, "trusted": True}
    attempted_deletes = []

    def simulated_clear(names):
        attempted_deletes.extend(names)
        for name in names:
            if name == "Customers" and target_rows["Orders"]:
                raise RuntimeError(
                    "DELETE Customers blocked by FK_Orders_Customers"
                )
            target_rows[name] = []
        return list(names)

    source.list_objects.return_value = ["Customers", "Orders"]
    source.get_schema.side_effect = [
        Schema(
            name="Customers", schema_name="training",
            columns=[Column(name="CustomerID", source_type="int")],
            primary_key=["CustomerID"],
        ),
        RuntimeError("Orders computed-column reconciliation failed"),
    ]
    source.get_all_triggers.return_value = []
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    target.reconcile_mssql_table.return_value = {
        "dropped_columns": [], "altered_columns": []
    }
    target.suspend_triggers_for_data_load.return_value = []
    target.inspect_schema.return_value = None
    target.clear_objects_for_full_sync.side_effect = simulated_clear

    orchestrator = MigrationOrchestrator(
        source, target,
        {"source": {"engine": "mssql"}, "target": {"engine": "mssql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    result = orchestrator.run_full()

    assert result["phases"]["full_target_sync"]["status"] == "skipped"
    assert result["phases"]["full_target_sync"]["missing_schemas"] == ["Orders"]
    assert "schema preparation was incomplete" in result["phases"]["full_target_sync"]["reason"]
    assert result["phases"]["data_load"]["status"] == "skipped"
    assert "schema preparation was incomplete" in result["phases"]["data_load"]["reason"]
    assert result["phases"]["object_failures"] == [
        {
            "phase": "create_table",
            "object": "Orders",
            "error": "Orders computed-column reconciliation failed",
        }
    ]
    target.clear_objects_for_full_sync.assert_not_called()
    target.upsert_batch.assert_not_called()
    assert attempted_deletes == []
    assert target_rows == rows_before
    assert fk_state == {
        "name": "FK_Orders_Customers", "enabled": True, "trusted": True
    }


def test_full_sync_clear_rolls_back_and_propagates_delete_error():
    target, conn, cursor = _target()
    target._managed_schemas = {
        "customers": Schema(name="customers", schema_name="training")
    }
    cursor.execute.side_effect = RuntimeError("delete failed")

    with pytest.raises(RuntimeError, match="delete failed"):
        target.clear_objects_for_full_sync(["customers"])

    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_full_sync_clear_is_noop_without_registered_source_tables():
    target, conn, _ = _target()

    assert target.clear_objects_for_full_sync(["target_only_test"]) == []

    conn.cursor.assert_not_called()


def test_full_sync_finalizes_staged_not_null_column_inside_clear_transaction():
    target, conn, cursor = _target()
    target._managed_schemas = {
        "Customers": Schema(
            name="Customers",
            schema_name="training",
            columns=[Column(name="Status", source_type="nvarchar", size=20, nullable=False)],
        )
    }
    target._deferred_not_null_columns = {"Customers": ["Status"]}

    cleared = target.clear_objects_for_full_sync(["Customers"])

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    delete_index = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM"))
    alter_index = next(i for i, sql in enumerate(statements) if sql.startswith("ALTER TABLE"))
    assert cleared == ["Customers"]
    assert delete_index < alter_index
    assert statements[alter_index].endswith('"Status" nvarchar(20) NOT NULL')
    conn.commit.assert_called_once_with()
    assert target._deferred_not_null_columns == {}


def test_full_sync_rolls_back_cleanup_if_staged_not_null_finalization_fails():
    target, conn, cursor = _target()
    target._managed_schemas = {
        "Customers": Schema(
            name="Customers",
            schema_name="training",
            columns=[Column(name="Status", source_type="nvarchar", size=20, nullable=False)],
        )
    }
    target._deferred_not_null_columns = {"Customers": ["Status"]}

    def fail_finalization(sql):
        if str(sql).startswith("ALTER TABLE"):
            raise RuntimeError("cannot finalize target column")

    cursor.execute.side_effect = fail_finalization
    with pytest.raises(RuntimeError, match="cannot finalize target column"):
        target.clear_objects_for_full_sync(["Customers"])

    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()
    assert target._deferred_not_null_columns == {"Customers": ["Status"]}


def test_mssql_full_sync_disables_only_existing_enabled_source_trigger():
    target, conn, cursor = _target()
    cursor.fetchone.return_value = (False,)
    trigger = TriggerDef(
        name="trg_orders_insert",
        table="Orders",
        table_schema="training",
        schema_name="training",
        ddl="CREATE TRIGGER training.trg_orders_insert ON training.Orders AFTER INSERT AS BEGIN RETURN; END",
    )

    suspended = target.suspend_triggers_for_data_load([trigger])

    assert len(suspended) == 1
    assert suspended[0].name == trigger.name
    assert suspended[0].table_schema == trigger.table_schema
    assert cursor.execute.call_args_list[0].args[1] == (
        "training", "Orders", "trg_orders_insert"
    )
    assert cursor.execute.call_args_list[1].args[0] == (
        'DISABLE TRIGGER "trg_orders_insert" ON "training"."Orders"'
    )
    conn.commit.assert_called_once_with()


def test_mssql_full_sync_leaves_absent_or_pre_disabled_triggers_untouched():
    target, _, cursor = _target()
    cursor.fetchone.side_effect = [None, (True,)]
    triggers = [
        TriggerDef(name="missing", table="Orders", ddl="CREATE TRIGGER missing"),
        TriggerDef(name="disabled", table="Orders", ddl="CREATE TRIGGER disabled"),
    ]

    assert target.suspend_triggers_for_data_load(triggers) == []
    assert cursor.execute.call_count == 2


def test_mssql_full_sync_restores_suspended_trigger_after_failed_run():
    target, conn, cursor = _target()
    trigger = TriggerDef(
        name="trg_orders_insert", table="Orders", table_schema="training",
        schema_name="training", ddl="CREATE TRIGGER training.trg_orders_insert",
    )
    target._suspended_triggers = [trigger]

    target.restore_suspended_triggers()

    assert cursor.execute.call_args.args[0] == (
        'ENABLE TRIGGER "trg_orders_insert" ON "training"."Orders"'
    )
    conn.commit.assert_called_once_with()
    assert target._suspended_triggers == []


def test_mssql_trigger_suspension_prevents_duplicate_audit_rows_and_is_repeatable():
    """The FULL lifecycle suspends before load and reinstalls source trigger."""
    source = MagicMock(spec=SourceConnector)
    target = MagicMock()
    trigger = TriggerDef(
        name="trg_Orders_Insert", table="Orders", schema_name="training",
        table_schema="training", ddl="CREATE TRIGGER training.trg_Orders_Insert",
    )
    source.list_objects.return_value = ["Orders", "OrderAudit"]
    source.get_schema.side_effect = lambda name: Schema(
        name=name, schema_name="training",
        columns=[Column(name="id", source_type="int")],
        foreign_keys=(
            [ForeignKey("fk_audit_order", ["orderid"], "Orders", ["id"], "training")]
            if name == "OrderAudit" else []
        ),
    )
    source.get_object_count.return_value = 5
    source.export_full.side_effect = lambda *_args, **_kwargs: iter(
        [{"id": i} for i in range(1, 6)]
    )
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.get_all_triggers.return_value = [trigger]

    lifecycle = []
    trigger_state = {"enabled": True}
    target.inspect_schema.return_value = None
    target.suspend_triggers_for_data_load.side_effect = lambda items: (
        lifecycle.append("suspend"), trigger_state.update(enabled=False), list(items)
    )[-1]
    target.clear_objects_for_full_sync.side_effect = lambda names: (
        lifecycle.append("clear"), list(names)
    )[-1]
    target.upsert_batch.side_effect = lambda name, rows, _schema: (
        lifecycle.append(f"load:{name}"),
        trigger_state["enabled"] and lifecycle.append("trigger_audit_side_effect"),
        UpsertResult(success_count=len(list(rows))),
    )[-1]
    target.create_trigger.side_effect = lambda _trigger: (
        lifecycle.append("restore_source_trigger"), trigger_state.update(enabled=True), True
    )[-1]
    target.list_roles.return_value = []

    orchestrator = MigrationOrchestrator(
        source, target,
        {"source": {"engine": "mssql"}, "target": {"engine": "mssql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    result = orchestrator.run_full()

    assert result["status"] == "success"
    assert lifecycle.index("suspend") < lifecycle.index("load:Orders")
    assert "trigger_audit_side_effect" not in lifecycle
    assert lifecycle.index("restore_source_trigger") > lifecycle.index("load:OrderAudit")
    assert trigger_state["enabled"] is True


def test_mssql_suspended_trigger_snapshot_is_cleared_after_trigger_phase():
    target, _, _ = _target()
    target._suspended_triggers = [
        TriggerDef(name="trigger", table="orders", ddl="CREATE TRIGGER trigger")
    ]

    target.complete_trigger_data_load()

    assert target._suspended_triggers == []


def test_mssql_full_sync_leaves_extra_target_column_unpopulated_by_contract():
    """MSSQL retains extra columns but only sends source columns in MERGE."""
    target, _, cursor = _target()
    cursor.fetchone.return_value = None
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[Column(name="CustomerID", source_type="int")],
        primary_key=["CustomerID"],
    )

    result = upsert_table_data(
        target._conn,
        "Customers",
        iter([{"CustomerID": 1}]),
        schema,
    )

    merge = next(
        call.args[0] for call in cursor.execute.call_args_list
        if call.args and str(call.args[0]).startswith("MERGE INTO")
    )
    assert result.success_count == 1
    assert "ExtraTargetColumn" not in merge
    assert "CustomerID" in merge


def test_full_migration_replaces_extra_and_modified_rows_on_every_run():
    """Each FULL run leaves managed rows equal to source and retains target-only data."""
    source = MagicMock(spec=SourceConnector)
    target = MagicMock()
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = Schema(
        name="customers",
        schema_name="training",
        columns=[
            Column(name="id", source_type="int"),
            Column(name="name", source_type="nvarchar"),
            Column(name="city", source_type="nvarchar", nullable=True),
        ],
        primary_key=["id"],
    )
    source.get_object_count.return_value = 2
    source.export_full.return_value = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob Smith", "city": "Delhi"},
    ]
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.get_all_triggers.return_value = []
    target.upsert_batch.return_value = UpsertResult(success_count=2)
    target.suspend_triggers_for_data_load.return_value = []
    target.inspect_schema.return_value = None

    source_rows = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob Smith", "city": "Delhi"},
    ]
    target_tables = {
        "customers": {
            "columns": {"id", "name", "city"},
            "rows": {
                1: {"id": 1, "name": "Alice"},
                2: {"id": 2, "name": "TARGET MODIFIED", "city": "Pune"},
                99: {"id": 99, "name": "Extra Test Customer"},
            },
        },
        "target_only_test": {
            "columns": {"id", "value"},
            "rows": {1: {"id": 1, "value": "Keep this target-only row"}},
        },
    }
    target_only_before = {
        key: {**table, "rows": dict(table["rows"])}
        for key, table in target_tables.items()
        if key != "customers"
    }

    def clear_managed(names):
        for name in names:
            if name in target_tables and name != "target_only_test":
                target_tables[name]["rows"].clear()
        return list(names)

    def load_source_rows(_name, rows, _schema):
        batch = list(rows)
        target_tables["customers"]["rows"].update(
            {row["id"]: row for row in batch}
        )
        return UpsertResult(success_count=len(batch))

    def assert_source_equal():
        actual_rows = target_tables["customers"]["rows"]
        assert set(actual_rows) == {row["id"] for row in source_rows}
        assert [actual_rows[row["id"]] for row in source_rows] == source_rows
        assert target_tables["customers"]["columns"] == {"id", "name", "city"}
        assert {
            key: {**table, "rows": dict(table["rows"])}
            for key, table in target_tables.items()
            if key != "customers"
        } == target_only_before

    target.clear_objects_for_full_sync.side_effect = clear_managed
    target.reconcile_mssql_table.return_value = {
        "dropped_columns": [], "altered_columns": []
    }
    target.suspend_triggers_for_data_load.return_value = []
    target.upsert_batch.side_effect = load_source_rows
    orchestrator = MigrationOrchestrator(
        source,
        target,
        {"source": {"engine": "mssql"}, "target": {"engine": "mssql"}},
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    first = orchestrator.run_full()
    assert first["phases"]["full_target_sync"]["cleared"] == ["customers"]
    assert_source_equal()

    # Reintroduce both forms of stale target data. A second FULL run must
    # replace them again and must not accumulate duplicate source rows.
    target_tables["customers"]["rows"][2] = {
        "id": 2, "name": "Changed between runs", "city": "Mumbai"
    }
    target_tables["customers"]["rows"][99] = {
        "id": 99, "name": "Second extra row"
    }
    second = orchestrator.run_full()

    assert second["phases"]["full_target_sync"]["cleared"] == ["customers"]
    assert_source_equal()
    assert target.suspend_triggers_for_data_load.call_count == 2
    assert target.reconcile_mssql_table.call_count == 2
    assert target.upsert_batch.call_count == 2
    assert len(target_tables["customers"]["rows"]) == len(source_rows)
