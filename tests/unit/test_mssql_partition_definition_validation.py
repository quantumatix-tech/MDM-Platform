"""Regression coverage for complete SQL Server partition metadata comparison."""

from datetime import date

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from core.connectors.mssql.objects.partition import (
    PartitionDefinitionMismatch,
    inspect_partition_functions,
    inspect_partition_schemes,
    inspect_partitioned_tables,
    reconcile_partition_function,
    reconcile_partition_scheme,
)
from core.connectors.mssql._models import PartitionFunctionDef, PartitionSchemeDef
from core.orchestrator import _mssql_partition_comparison


def _inventory(boundaries: list[date], fanout: int) -> dict:
    """Build a generic managed function/scheme/index inventory."""
    function = {
        "name": "PF_Orders",
        "fanout": fanout,
        "range_right": True,
        "data_type": "date",
        "boundaries": [("date", value.isoformat()) for value in boundaries],
        "catalog_consistent": fanout == len(boundaries) + 1,
    }
    destinations = [(0, "PRIMARY")]
    scheme = {
        "name": "PS_Orders",
        "partition_function_name": "PF_Orders",
        "destinations": destinations,
    }
    table_index = {
        "schema_name": "training",
        "table_name": "Orders",
        "object_type": "U",
        "index_name": "CIX_Orders",
        "partition_scheme_name": "PS_Orders",
        "partition_function_name": "PF_Orders",
        "partition_columns": ["OrderDate"],
    }
    return {
        "functions": {"PF_Orders": function},
        "schemes": {"PS_Orders": scheme},
        "tables": {("training", "Orders", "CIX_Orders"): table_index},
    }


def test_scenario_12_extra_target_boundary_is_mismatch():
    """An extra target boundary/fanout is reported with both full definitions."""
    source = _inventory(
        [date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1)],
        fanout=5,
    )
    target = _inventory(
        [date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1), date(2026, 1, 1)],
        fanout=6,
    )

    result = _mssql_partition_comparison(source, target)

    assert result["definition_matches"] is False
    mismatch = next(item for item in result["mismatches"] if item["kind"] == "partition_function")
    assert mismatch["source"]["fanout"] == 5
    assert len(mismatch["source"]["boundaries"]) == 4
    assert mismatch["target"]["fanout"] == 6
    assert mismatch["target"]["boundaries"][-1] == ("date", "2026-01-01")


def test_scenario_13_restored_matching_definition_passes():
    """Restoring the exact source boundaries, fanout, and dependent mappings passes."""
    boundaries = [
        date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1),
    ]

    result = _mssql_partition_comparison(_inventory(boundaries, 5), _inventory(boundaries, 5))

    assert result["definition_matches"] is True
    assert result["mismatches"] == []


def test_comparison_checks_range_scheme_destinations_and_index_assignment():
    source = _inventory([date(2025, 1, 1)], fanout=2)
    target = _inventory([date(2025, 1, 1)], fanout=2)
    target["functions"]["PF_Orders"]["range_right"] = False
    target["schemes"]["PS_Orders"]["destinations"] = [(1, "PRIMARY"), (2, "FG_Archive")]
    target["tables"][("training", "Orders", "CIX_Orders")]["partition_scheme_name"] = "PS_Other"

    result = _mssql_partition_comparison(source, target)

    assert result["definition_matches"] is False
    assert {item["kind"] for item in result["mismatches"]} == {
        "partition_function", "partition_scheme", "partitioned_table_index",
    }


def test_catalog_inventory_reads_full_function_scheme_and_index_metadata():
    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [
            ("PF_Orders", 3, True, "datetime2", 8, 27, 7, date(2025, 1, 1), 1),
            ("PF_Orders", 3, True, "datetime2", 8, 27, 7, date(2025, 4, 1), 2),
        ],
        [("PS_Orders", "PF_Orders", 1, "PRIMARY", 7), ("PS_Orders", "PF_Orders", 2, "FG_Archive", 8),
         ("PS_Orders", "PF_Orders", 3, "FG_Archive", 8)],
        [("training", "Orders", "U", "CIX_Orders", "PS_Orders", "PF_Orders", 1, "OrderDate")],
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.cursor.return_value.__exit__.return_value = False

    functions = inspect_partition_functions(connection)
    schemes = inspect_partition_schemes(connection)
    tables = inspect_partitioned_tables(connection, ("training",))

    assert functions["PF_Orders"]["fanout"] == 3
    assert functions["PF_Orders"]["range_right"] is True
    assert functions["PF_Orders"]["catalog_consistent"] is True
    assert functions["PF_Orders"]["boundaries"] == [
        ("date", "2025-01-01"), ("date", "2025-04-01"),
    ]
    assert schemes["PS_Orders"]["partition_function_name"] == "PF_Orders"
    assert schemes["PS_Orders"]["destinations"] == [
        (1, "PRIMARY"), (2, "FG_Archive"), (3, "FG_Archive"),
    ]
    assert tables[("training", "Orders", "CIX_Orders")]["partition_columns"] == ["OrderDate"]
    assert all("sys.partition_schemes" in call.args[0] or "sys.partition_functions" in call.args[0]
               or "sys.objects" in call.args[0] for call in cursor.execute.call_args_list)


def _reconciliation_inputs(*, occupied_rows: int = 0):
    source_boundaries = [date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1)]
    target_boundaries = [*source_boundaries, date(2026, 1, 1)]
    source = _inventory(source_boundaries, 5)
    target = _inventory(target_boundaries, 6)
    dependency = {
        "schema_name": "training", "object_name": "Orders", "object_type": "U",
        "index_name": "CIX_Orders", "index_id": 1, "index_type": "CLUSTERED",
        "partition_number": 6, "rows": occupied_rows, "partition_scheme_name": "PS_Orders",
    }
    filegroups = {"PRIMARY": {
        "name": "PRIMARY", "type_desc": "ROWS_FILEGROUP", "is_read_only": False,
        "file_states": ["ONLINE"],
    }}
    pf = PartitionFunctionDef(
        name="PF_Orders", data_type="date", boundaries=source_boundaries,
        range_desc="RANGE RIGHT", fanout=5,
    )
    return pf, source, target, [dependency], filegroups


def test_safe_reconciliation_merges_only_empty_trailing_date_partition_and_verifies_catalog():
    pf, source, target_before, dependencies, filegroups = _reconciliation_inputs()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = MagicMock()
    connection.cursor.return_value.__exit__.return_value = False
    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions",
               side_effect=[target_before["functions"], source["functions"], source["functions"]]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes",
               side_effect=[target_before["schemes"], source["schemes"], source["schemes"]]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy",
               return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups",
               return_value=filegroups):
        reconcile_partition_function(connection, pf, source)

    sql = [str(call.args[0]) for call in connection.cursor.return_value.__enter__.return_value.execute.call_args_list]
    assert "BEGIN TRANSACTION" in sql
    assert 'ALTER PARTITION FUNCTION "PF_Orders"() MERGE RANGE (\'2026-01-01\')' in sql
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()


def test_safe_reconciliation_adds_missing_source_next_used_destination_after_merge():
    """A source NEXT USED slot is restored when target has only partition destinations."""
    pf, source, target_before, dependencies, filegroups = _reconciliation_inputs()
    six_primary_destinations = [(destination_id, "PRIMARY") for destination_id in range(1, 7)]
    source["schemes"]["PS_Orders"]["destinations"] = list(six_primary_destinations)
    target_before["schemes"]["PS_Orders"]["destinations"] = list(six_primary_destinations[:5])
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = MagicMock()
    connection.cursor.return_value.__exit__.return_value = False

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions",
               side_effect=[target_before["functions"], source["functions"], source["functions"]]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes",
               side_effect=[target_before["schemes"], source["schemes"], source["schemes"]]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy",
               return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups",
               return_value=filegroups):
        reconcile_partition_function(connection, pf, source)

    sql = [str(call.args[0]) for call in connection.cursor.return_value.__enter__().execute.call_args_list]
    assert 'ALTER PARTITION FUNCTION "PF_Orders"() MERGE RANGE (\'2026-01-01\')' in sql
    assert 'ALTER PARTITION SCHEME "PS_Orders" NEXT USED "PRIMARY"' in sql
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()


def test_scheme_only_reconciliation_sets_missing_explicit_next_used_slot():
    """A matching PF still needs scheme reconciliation when NEXT USED is missing."""
    _, source, _, _, filegroups = _reconciliation_inputs()
    six_primary_destinations = [(destination_id, "PRIMARY") for destination_id in range(1, 7)]
    source["schemes"]["PS_Orders"]["destinations"] = list(six_primary_destinations)
    target_scheme = {
        "name": "PS_Orders", "partition_function_name": "PF_Orders",
        "destinations": list(six_primary_destinations[:5]),
    }
    target_schemes_after = {"PS_Orders": source["schemes"]["PS_Orders"]}
    scheme = PartitionSchemeDef(
        name="PS_Orders", partition_function_name="PF_Orders",
        filegroups=["PRIMARY"] * 6, destinations=six_primary_destinations,
    )
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = MagicMock()
    connection.cursor.return_value.__exit__.return_value = False

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions",
               return_value=source["functions"]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes",
               side_effect=[{"PS_Orders": target_scheme}, target_schemes_after, target_schemes_after]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups",
               return_value=filegroups):
        reconcile_partition_scheme(connection, scheme, source)

    sql = [str(call.args[0]) for call in connection.cursor.return_value.__enter__.return_value.execute.call_args_list]
    assert 'ALTER PARTITION SCHEME "PS_Orders" NEXT USED "PRIMARY"' in sql
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()


def test_scheme_only_reconciliation_rejects_incompatible_retained_filegroups():
    _, source, _, _, filegroups = _reconciliation_inputs()
    source["schemes"]["PS_Orders"]["destinations"] = [
        (destination_id, "PRIMARY") for destination_id in range(1, 7)
    ]
    target_scheme = {
        "name": "PS_Orders", "partition_function_name": "PF_Orders",
        "destinations": [(1, "PRIMARY"), (2, "FG_Archive"), (3, "PRIMARY"),
                         (4, "PRIMARY"), (5, "PRIMARY")],
    }
    scheme = PartitionSchemeDef(
        name="PS_Orders", partition_function_name="PF_Orders",
        filegroups=["PRIMARY"] * 6,
        destinations=source["schemes"]["PS_Orders"]["destinations"],
    )
    connection = MagicMock()

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions",
               return_value=source["functions"]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes",
               return_value={"PS_Orders": target_scheme}), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups",
               return_value=filegroups):
        with pytest.raises(PartitionDefinitionMismatch, match="incompatible destinations"):
            reconcile_partition_scheme(connection, scheme, source)

    connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    connection.commit.assert_not_called()


@pytest.mark.parametrize("unsafe_change", ["middle-boundary", "missing-boundary", "wrong-range"])
def test_unsafe_boundary_shapes_are_rejected_without_partition_ddl(unsafe_change):
    pf, source, target, dependencies, filegroups = _reconciliation_inputs()
    target_functions = target["functions"]
    if unsafe_change == "middle-boundary":
        target_functions["PF_Orders"]["boundaries"][2] = ("date", "2025-08-01")
    elif unsafe_change == "missing-boundary":
        target_functions = source["functions"]
    else:
        target_functions["PF_Orders"]["range_right"] = False
    connection = MagicMock()

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions", return_value=target_functions), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes", return_value=target["schemes"]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy", return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups", return_value=filegroups):
        with pytest.raises(PartitionDefinitionMismatch):
            reconcile_partition_function(connection, pf, source)

    connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    connection.commit.assert_not_called()


def test_occupied_tail_partition_is_preserved_and_blocks_merge():
    pf, source, target, dependencies, filegroups = _reconciliation_inputs(occupied_rows=3)
    connection = MagicMock()

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions", return_value=target["functions"]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes", return_value=target["schemes"]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy", return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups", return_value=filegroups):
        with pytest.raises(PartitionDefinitionMismatch, match=r"has 3 row\(s\)"):
            reconcile_partition_function(connection, pf, source)

    connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()


def test_target_only_partition_dependency_blocks_merge():
    pf, source, target, dependencies, filegroups = _reconciliation_inputs()
    dependencies[0]["object_name"] = "ManualArchive"
    connection = MagicMock()

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions", return_value=target["functions"]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes", return_value=target["schemes"]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy", return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups", return_value=filegroups):
        with pytest.raises(PartitionDefinitionMismatch, match="target-only dependent index"):
            reconcile_partition_function(connection, pf, source)

    connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    connection.commit.assert_not_called()


def test_mismatched_scheme_destinations_block_merge():
    pf, source, target, dependencies, filegroups = _reconciliation_inputs()
    target["schemes"]["PS_Orders"]["destinations"] = [(1, "PRIMARY")] * 6
    connection = MagicMock()

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions", return_value=target["functions"]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes", return_value=target["schemes"]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy", return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups", return_value=filegroups):
        with pytest.raises(PartitionDefinitionMismatch, match="partition scheme PS_Orders mapping"):
            reconcile_partition_function(connection, pf, source)

    connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    connection.commit.assert_not_called()


def test_incompatible_next_used_destination_blocks_merge_with_mapping_diagnostic():
    pf, source, target, dependencies, filegroups = _reconciliation_inputs()
    source["schemes"]["PS_Orders"]["destinations"] = [
        (destination_id, "FG_Archive" if destination_id == 3 else "PRIMARY")
        for destination_id in range(1, 7)
    ]
    target["schemes"]["PS_Orders"]["destinations"] = [
        (destination_id, "PRIMARY") for destination_id in range(1, 6)
    ]
    connection = MagicMock()

    with patch("core.connectors.mssql.objects.partition.inspect_partition_functions", return_value=target["functions"]), \
         patch("core.connectors.mssql.objects.partition.inspect_partition_schemes", return_value=target["schemes"]), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_tail_occupancy", return_value=dependencies), \
         patch("core.connectors.mssql.objects.partition._inspect_partition_filegroups", return_value=filegroups):
        with pytest.raises(PartitionDefinitionMismatch, match="mapping is incompatible.*retained target destinations"):
            reconcile_partition_function(connection, pf, source)

    connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    connection.commit.assert_not_called()
