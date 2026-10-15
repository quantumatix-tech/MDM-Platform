"""FULL-mode coverage for partition reconciliation, status, and data loading."""

from datetime import date
from unittest.mock import MagicMock

from core.connectors.base import Column, Schema, UpsertResult
from core.connectors.mssql._models import PartitionFunctionDef, PartitionSchemeDef, PartitionedTableDef
from core.connectors.mssql.objects.partition import PartitionDefinitionMismatch
from core.orchestrator import MigrationOrchestrator


def _metadata(*, extra_target_boundary: bool, missing_next_used: bool = False) -> dict:
    boundaries = [date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1)]
    if extra_target_boundary:
        boundaries.append(date(2026, 1, 1))
    fanout = len(boundaries) + 1
    function = {
        "name": "PF_Orders", "fanout": fanout, "range_right": True,
        "data_type": "date", "boundaries": [("date", item.isoformat()) for item in boundaries],
        "catalog_consistent": True,
    }
    scheme_destinations = [
        (destination_id, "PRIMARY")
        for destination_id in range(
            1, 6 if extra_target_boundary or missing_next_used else 7
        )
    ]
    scheme = {
        "name": "PS_Orders", "partition_function_name": "PF_Orders",
        "destinations": scheme_destinations,
    }
    index = {
        "schema_name": "training", "table_name": "Orders", "object_type": "U",
        "index_name": "CIX_Orders", "partition_scheme_name": "PS_Orders",
        "partition_function_name": "PF_Orders", "partition_columns": ["OrderDate"],
    }
    return {
        "functions": {"PF_Orders": function}, "schemes": {"PS_Orders": scheme},
        "tables": {("training", "Orders", "CIX_Orders"): index},
    }


def _orchestrator(
    *, reconcile_enabled: bool, reconciliation_fails: bool = False,
    scheme_only_mismatch: bool = False,
):
    source = MagicMock()
    target = MagicMock()
    source_inventory = _metadata(extra_target_boundary=False)
    stale_inventory = _metadata(
        extra_target_boundary=not scheme_only_mismatch,
        missing_next_used=scheme_only_mismatch,
    )
    pf = PartitionFunctionDef(
        name="PF_Orders", schema_name="dbo", data_type="date",
        boundaries=[date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1)],
        range_desc="RANGE RIGHT", fanout=5,
    )
    ps = PartitionSchemeDef(
        name="PS_Orders", schema_name="dbo", partition_function_name="PF_Orders",
        filegroups=["PRIMARY"] * 6,
        destinations=[(destination_id, "PRIMARY") for destination_id in range(1, 7)],
    )
    pt = PartitionedTableDef(
        table_name="Orders", schema_name="training", index_name="CIX_Orders",
        partition_function_name="PF_Orders", partition_scheme_name="PS_Orders",
        partition_column="OrderDate",
    )
    schema = Schema(
        name="Orders", schema_name="training",
        columns=[Column(name="OrderDate", source_type="date")],
    )

    source.list_objects.return_value = ["Orders"]
    source.get_partitioned_tables.return_value = [pt]
    source.list_partitions.side_effect = AttributeError("partition child listing unsupported")
    source.list_partition_functions.return_value = [pf]
    source.list_partition_schemes.return_value = [ps]
    source.get_schema.return_value = schema
    source.inspect_partition_metadata.return_value = source_inventory
    source.get_object_count.return_value = 1
    source.export_full.side_effect = lambda *args, **kwargs: iter([{"OrderDate": date(2025, 2, 1)}])
    source.list_extensions.return_value = []
    source.list_schemas.return_value = []
    source.list_types.return_value = []
    source.list_all_sequences.return_value = []
    source.get_all_triggers.return_value = []
    source.list_views.return_value = []
    source.list_materialized_views.return_value = []
    source.list_functions.return_value = []
    source.list_synonyms.return_value = []
    source.list_grants.return_value = []
    source.list_roles.return_value = []
    source.list_users.return_value = []

    target.create_partition_function.side_effect = (
        None if scheme_only_mismatch else PartitionDefinitionMismatch("definition differs")
    )
    target.inspect_partition_metadata.return_value = (
        stale_inventory if reconciliation_fails or not reconcile_enabled else source_inventory
    )
    target.reconcile_partition_function.side_effect = (
        PartitionDefinitionMismatch("rightmost partition is occupied")
        if reconciliation_fails else None
    )
    target.reconcile_partition_scheme.side_effect = (
        PartitionDefinitionMismatch("scheme destinations are incompatible")
        if reconciliation_fails else None
    )
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    target.get_object_count.return_value = 1
    target.clear_objects_for_full_sync.return_value = ["Orders"]
    target.apply_constraints.return_value = None
    target.suspend_triggers_for_data_load.return_value = []

    orchestrator = MigrationOrchestrator(
        source, target,
        {
            "source": {"engine": "mssql"}, "target": {"engine": "mssql"},
            "migration": {"mode": "full", "reconcile_mssql_partition_functions": reconcile_enabled},
        },
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})
    return orchestrator, source, target


def test_successful_partition_reconciliation_allows_full_data_load_and_success_status():
    orchestrator, _, target = _orchestrator(reconcile_enabled=True)
    target.create_partition_function.side_effect = [
        PartitionDefinitionMismatch("definition differs"), None,
    ]

    first_result = orchestrator.run_full()
    second_result = orchestrator.run_full()

    target.reconcile_partition_function.assert_called_once()
    for result in (first_result, second_result):
        assert result["phases"]["partition_definition_validation"]["definition_matches"] is True
        assert result["phases"]["full_target_sync"]["status"] == "success"
        assert result["phases"]["Orders"]["success"] == 1
        assert result["phases"]["object_failures"] == []
        assert result["status"] == "success"
    assert target.create_partition_function.call_count == 2


def test_scheme_only_next_used_reconciliation_allows_full_sync_and_is_idempotent():
    orchestrator, _, target = _orchestrator(
        reconcile_enabled=True, scheme_only_mismatch=True,
    )
    target.create_partition_scheme.side_effect = [
        PartitionDefinitionMismatch("missing NEXT USED destination"), None, None,
    ]

    first_result = orchestrator.run_full()
    second_result = orchestrator.run_full()

    target.reconcile_partition_scheme.assert_called_once()
    target.reconcile_partition_function.assert_not_called()
    for result in (first_result, second_result):
        assert result["phases"]["partition_definition_validation"]["definition_matches"] is True
        assert result["phases"]["full_target_sync"]["status"] == "success"
        assert result["phases"]["Orders"]["success"] == 1
        assert result["phases"]["object_failures"] == []
        assert result["status"] == "success"


def test_disabled_or_rejected_reconciliation_reports_one_error_and_skips_full_data_load():
    for enabled, rejected in ((False, False), (True, True)):
        orchestrator, _, target = _orchestrator(
            reconcile_enabled=enabled, reconciliation_fails=rejected,
        )

        result = orchestrator.run_full()

        assert result["status"] == "partial_success"
        assert result["phases"]["partition_definition_validation"]["definition_matches"] is False
        failures = [
            failure for failure in result["phases"]["object_failures"]
            if failure["phase"] == "partition_definition_validation"
        ]
        assert len(failures) == 1
        assert len(result["phases"]["object_failures"]) == 1
        assert result["phases"]["full_target_sync"]["status"] == "skipped"
        assert result["phases"]["data_load"]["status"] == "skipped"
        target.upsert_batch.assert_not_called()
