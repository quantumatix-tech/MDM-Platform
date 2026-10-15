"""MSSQL Partition object implementation.

Single logical home for Partition-specific behaviour: partition function
and scheme discovery/creation, and partitioned-table creation.

Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported, which
avoids circular dependencies.

# ---------------------------------------------------------------------------
# Architecture overview:
#
#   orchestrator.py
#     ┌─────────────┐
#     │  _source    │─────────  list_partition_functions() ────► partition.list_partition_functions()
#     │             │─────────  list_partition_schemes() ─────► partition.list_partition_schemes()
#     │             │─────────  get_partitioned_tables() ──────► partition.get_partitioned_tables()
#     └─────────────┘
#     ┌─────────────┐
#     │  _target    │─────────  create_partition_function() ───► partition.create_partition_function()
#     │             │─────────  create_partition_scheme() ─────► partition.create_partition_scheme()
#     │             │─────────  create_partitioned_table() ────► partition.create_partitioned_table()
#     └─────────────┘
#
# ``create_partitioned_table`` reuses the column-DDL helper
# ``table._column_ddl`` rather than re-implementing it, so plain-table and
# partitioned-table DDL generation stay consistent.
# ---------------------------------------------------------------------------
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    Schema,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    PartitionedTableDef,
    PartitionFunctionDef,
    PartitionSchemeDef,
    _mssql_column_type,
    _qualify,
    _resolve_mssql_schemas,
)
from core.connectors.mssql.objects.table import _column_ddl


class PartitionDefinitionMismatch(RuntimeError):
    """An existing SQL Server partition definition differs from source."""

# ---------------------------------------------------------------------------
# 1. SOURCE-SIDE PARTITION OPERATIONS
#    Called/delegated by MSSQLSourceConnector methods.
#    Discovery of partition functions, schemes, and partitioned tables from
#    the source database.
# ---------------------------------------------------------------------------

def list_partition_functions(conn: Any) -> list[PartitionFunctionDef]:
    """Return user partition functions with their boundaries.

    Replaces ``MSSQLSourceConnector.list_partition_functions``.
    """
    results: list[PartitionFunctionDef] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pf.name, pf.type_desc, pf.boundary_value_on_right, pf.fanout, "
            "prv.value, prv.boundary_id, "
            "tp.name AS param_type, pp.max_length, pp.precision, pp.scale "
            "FROM sys.partition_functions pf "
            "LEFT JOIN sys.partition_range_values prv "
            "  ON prv.function_id = pf.function_id "
            "LEFT JOIN sys.partition_parameters pp "
            "  ON pp.function_id = pf.function_id "
            "LEFT JOIN sys.types tp ON tp.user_type_id = pp.user_type_id "
            "ORDER BY pf.name, prv.boundary_id",
        )
        pf_map: dict = {}
        for row in cur.fetchall():
            (
                pf_name, _type_desc, bvr, fanout, value, _boundary_id,
                param_type, max_length, precision, scale,
            ) = row
            if pf_name not in pf_map:
                range_desc = "RANGE RIGHT" if bvr else "RANGE LEFT"
                data_type = _mssql_column_type(
                    (param_type or "datetime2"),
                    max_length, precision, scale,
                )
                pf_map[pf_name] = PartitionFunctionDef(
                    name=pf_name,
                    schema_name="dbo",
                    data_type=data_type,
                    boundaries=[],
                    range_desc=range_desc,
                )
            if value is not None:
                pf_map[pf_name].boundaries.append(value)
            pf_map[pf_name].fanout = int(fanout)
        results = list(pf_map.values())
    return results


def list_partition_schemes(conn: Any) -> list[PartitionSchemeDef]:
    """Return user partition schemes with their filegroup mappings.

    Replaces ``MSSQLSourceConnector.list_partition_schemes``.
    """
    results: list[PartitionSchemeDef] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ps.name, pf.name AS pf_name "
            "FROM sys.partition_schemes ps "
            "JOIN sys.partition_functions pf ON ps.function_id = pf.function_id "
            "ORDER BY ps.name",
        )
        scheme_names = [row[0] for row in cur.fetchall()]

        for ps_name in scheme_names:
            cur.execute(
                "SELECT ps.name, pf.name AS pf_name, "
                "ds.name AS data_space_name, dds.destination_id, dds.data_space_id "
                "FROM sys.partition_schemes ps "
                "JOIN sys.partition_functions pf ON ps.function_id = pf.function_id "
                "JOIN sys.destination_data_spaces dds "
                "  ON dds.partition_scheme_id = ps.data_space_id "
                "JOIN sys.data_spaces ds ON ds.data_space_id = dds.data_space_id "
                "WHERE ps.name = ? "
                "ORDER BY dds.destination_id",
                (ps_name,),
            )
            fg_list = []
            destinations: list[tuple[int, str]] = []
            pf_name_val = ""
            for row in cur.fetchall():
                _, pf_name, data_space_name, destination_id, _data_space_id = row
                pf_name_val = pf_name
                fg_list.append(data_space_name)
                destinations.append((int(destination_id), data_space_name))
            results.append(PartitionSchemeDef(
                name=ps_name,
                schema_name="dbo",
                partition_function_name=pf_name_val,
                filegroups=fg_list,
                destinations=destinations,
            ))
    return results


def get_partitioned_tables(
    conn: Any, config: dict[str, Any]
) -> list[PartitionedTableDef]:
    """Return partitioned table/index metadata.

    Replaces ``MSSQLSourceConnector.get_partitioned_tables``.
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[PartitionedTableDef] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT t.name, s.name AS schema_name, "
                "i.name AS index_name, "
                "pf.name AS pf_name, "
                "ps.name AS ps_name, "
                "c.name AS partition_column "
                "FROM sys.tables t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN sys.indexes i ON t.object_id = i.object_id "
                "JOIN sys.partition_schemes ps ON i.data_space_id = ps.data_space_id "
                "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
                "LEFT JOIN sys.index_columns ic "
                "  ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                "  AND ic.is_included_column = 0 "
                "  AND ic.partition_ordinal > 0 "
                "LEFT JOIN sys.columns c ON c.object_id = t.object_id "
                "  AND c.column_id = ic.column_id "
                f"WHERE s.name IN ({placeholders}) "
                "AND i.data_space_id IS NOT NULL "
                "ORDER BY t.name, i.name",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT t.name, s.name AS schema_name, "
                "i.name AS index_name, "
                "pf.name AS pf_name, "
                "ps.name AS ps_name, "
                "c.name AS partition_column "
                "FROM sys.tables t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN sys.indexes i ON t.object_id = i.object_id "
                "JOIN sys.partition_schemes ps ON i.data_space_id = ps.data_space_id "
                "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
                "LEFT JOIN sys.index_columns ic "
                "  ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                "  AND ic.is_included_column = 0 "
                "  AND ic.partition_ordinal > 0 "
                "LEFT JOIN sys.columns c ON c.object_id = t.object_id "
                "  AND c.column_id = ic.column_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "AND i.data_space_id IS NOT NULL "
                "ORDER BY t.name, i.name"
            )
        for row in cur.fetchall():
            table_name, schema_name, index_name, pf_name, ps_name, partition_column = row
            results.append(PartitionedTableDef(
                table_name=table_name,
                schema_name=schema_name,
                index_name=index_name,
                partition_function_name=pf_name,
                partition_scheme_name=ps_name,
                partition_column=partition_column,
            ))
    return results


def _normalize_partition_value(value: Any) -> tuple[str, str]:
    """Normalize pyodbc date/numeric/string catalog values for exact comparison."""
    if isinstance(value, datetime):
        return ("datetime", value.isoformat(sep=" "))
    if isinstance(value, date):
        return ("date", value.isoformat())
    if isinstance(value, Decimal):
        return ("number", format(value.normalize(), "f"))
    if isinstance(value, (int, float)):
        return ("number", format(Decimal(str(value)).normalize(), "f"))
    return ("string", str(value).strip())


def _normalize_partition_type(value: str) -> str:
    return "".join(str(value).lower().split())


def inspect_partition_functions(conn: Any) -> dict[str, dict[str, Any]]:
    """Read complete SQL Server partition-function definitions from catalogs."""
    functions: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pf.name, pf.fanout, pf.boundary_value_on_right, "
            "tp.name, pp.max_length, pp.precision, pp.scale, prv.value, prv.boundary_id "
            "FROM sys.partition_functions pf "
            "JOIN sys.partition_parameters pp ON pp.function_id = pf.function_id "
            "JOIN sys.types tp ON tp.user_type_id = pp.user_type_id "
            "LEFT JOIN sys.partition_range_values prv ON prv.function_id = pf.function_id "
            "ORDER BY pf.name, prv.boundary_id"
        )
        for row in cur.fetchall():
            name, fanout, range_right, type_name, max_length, precision, scale, value, boundary_id = row
            entry = functions.setdefault(name, {
                "name": name,
                "fanout": int(fanout),
                "range_right": bool(range_right),
                "data_type": _normalize_partition_type(
                    _mssql_column_type(type_name, max_length, precision, scale)
                ),
                "boundaries": [],
            })
            if value is not None:
                entry["boundaries"].append((int(boundary_id), _normalize_partition_value(value)))
    for entry in functions.values():
        entry["boundaries"] = [v for _, v in sorted(entry["boundaries"])]
        if entry["fanout"] != len(entry["boundaries"]) + 1:
            entry["catalog_consistent"] = False
            entry["catalog_error"] = (
                f"fanout {entry['fanout']} does not equal boundary count + 1 "
                f"({len(entry['boundaries']) + 1})"
            )
        else:
            entry["catalog_consistent"] = True
    return functions


def inspect_partition_schemes(conn: Any) -> dict[str, dict[str, Any]]:
    """Read complete ordered destination mapping for each partition scheme."""
    schemes: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ps.name, pf.name, dds.destination_id, ds.name, dds.data_space_id "
            "FROM sys.partition_schemes ps "
            "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
            "JOIN sys.destination_data_spaces dds ON dds.partition_scheme_id = ps.data_space_id "
            "JOIN sys.data_spaces ds ON ds.data_space_id = dds.data_space_id "
            "ORDER BY ps.name, dds.destination_id"
        )
        for name, function_name, destination_id, data_space_name, _data_space_id in cur.fetchall():
            item = schemes.setdefault(name, {
                "name": name, "partition_function_name": function_name, "destinations": []
            })
            if item["partition_function_name"] != function_name:
                raise RuntimeError(f"Partition scheme {name} maps to multiple partition functions in catalog results")
            item["destinations"].append((int(destination_id), str(data_space_name)))
    for item in schemes.values():
        item["destinations"] = sorted(item["destinations"])
    return schemes


def inspect_partitioned_tables(conn: Any, schemas: tuple[str, ...] | None = None) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Read every partitioned table or indexed-view index and its binding."""
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    with conn.cursor() as cur:
        query = (
            "SELECT s.name, o.name, o.type, COALESCE(i.name, ''), ps.name, pf.name, "
            "ic.partition_ordinal, c.name "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "JOIN sys.indexes i ON i.object_id = o.object_id "
            "JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id "
            "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
            "LEFT JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "AND ic.partition_ordinal > 0 "
            "LEFT JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
            "WHERE o.type IN ('U', 'V') "
        )
        params: tuple[Any, ...] = ()
        if schemas:
            query += "AND s.name IN (" + ",".join("?" for _ in schemas) + ") "
            params = tuple(schemas)
        query += "ORDER BY s.name, o.name, i.name, ic.partition_ordinal"
        cur.execute(query, params)
        for schema_name, table_name, object_type, index_name, scheme_name, function_name, ordinal, column_name in cur.fetchall():
            key = (schema_name, table_name, index_name)
            item = result.setdefault(key, {
                "schema_name": schema_name, "table_name": table_name,
                "object_type": object_type,
                "index_name": index_name or None, "partition_scheme_name": scheme_name,
                "partition_function_name": function_name, "partition_columns": [],
            })
            if ordinal is not None and column_name is not None:
                item["partition_columns"].append((int(ordinal), column_name))
    for item in result.values():
        item["partition_columns"] = [name for _, name in sorted(item["partition_columns"])]
    return result


def _inspect_partition_tail_occupancy(conn: Any, function_name: str, partition_number: int) -> list[dict[str, Any]]:
    """Return every dependent index and its row count in a candidate tail partition."""
    dependencies: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.name, o.name, o.type, COALESCE(i.name, ''), i.index_id, "
            "i.type_desc, p.partition_number, p.rows, ps.name "
            "FROM sys.partition_functions pf "
            "JOIN sys.partition_schemes ps ON ps.function_id = pf.function_id "
            "JOIN sys.indexes i ON i.data_space_id = ps.data_space_id "
            "JOIN sys.objects o ON o.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "JOIN sys.partitions p ON p.object_id = i.object_id AND p.index_id = i.index_id "
            "WHERE pf.name = ? AND p.partition_number = ? "
            "AND o.type IN ('U', 'V') "
            "ORDER BY s.name, o.name, i.index_id",
            (function_name, partition_number),
        )
        for row in cur.fetchall():
            schema_name, object_name, object_type, index_name, index_id, index_type, part_no, rows, scheme = row
            dependencies.append({
                "schema_name": schema_name, "object_name": object_name,
                "object_type": object_type, "index_name": index_name or "",
                "index_id": int(index_id), "index_type": index_type,
                "partition_number": int(part_no), "rows": int(rows),
                "partition_scheme_name": scheme,
            })
    return dependencies


def _inspect_partition_filegroups(conn: Any, names: set[str]) -> dict[str, dict[str, Any]]:
    """Inspect filegroup read-only and online state for affected partition schemes."""
    if not names:
        return {}
    result: dict[str, dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in names)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ds.name, ds.type_desc, fg.is_read_only, df.state_desc "
            "FROM sys.data_spaces ds "
            "LEFT JOIN sys.filegroups fg ON fg.data_space_id = ds.data_space_id "
            "LEFT JOIN sys.database_files df ON df.data_space_id = ds.data_space_id "
            f"WHERE ds.name IN ({placeholders}) ORDER BY ds.name, df.file_id",
            tuple(sorted(names)),
        )
        for name, type_desc, is_read_only, state_desc in cur.fetchall():
            entry = result.setdefault(name, {
                "name": name, "type_desc": type_desc,
                "is_read_only": bool(is_read_only) if is_read_only is not None else None,
                "file_states": [],
            })
            if state_desc is not None:
                entry["file_states"].append(str(state_desc).upper())
    return result


def _expected_scheme_destinations(scheme: dict[str, Any]) -> list[tuple[int, str]]:
    destinations = scheme.get("destinations") or []
    if destinations:
        return [(int(destination_id), str(name)) for destination_id, name in destinations]
    return list(enumerate(scheme.get("filegroups", []), start=1))


def _plan_safe_trailing_date_merge(
    pf: PartitionFunctionDef,
    source_metadata: dict[str, Any],
    target_functions: dict[str, dict[str, Any]],
    target_schemes: dict[str, dict[str, Any]],
    dependencies: list[dict[str, Any]],
    filegroups: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
    """Approve only an empty, single, trailing RANGE RIGHT date partition merge."""
    name = pf.name
    source = source_metadata.get("functions", {}).get(name)
    target = target_functions.get(name)
    if source is None or target is None:
        raise PartitionDefinitionMismatch(f"partition function {name} source or target metadata is unavailable")
    if not source.get("catalog_consistent") or not target.get("catalog_consistent"):
        raise PartitionDefinitionMismatch(f"partition function {name} has inconsistent fanout/boundary metadata")
    if source.get("data_type") != "date" or target.get("data_type") != "date":
        raise PartitionDefinitionMismatch("safe automatic reconciliation currently supports only SQL Server date functions")
    if not source.get("range_right") or not target.get("range_right"):
        raise PartitionDefinitionMismatch("safe trailing-boundary reconciliation requires RANGE RIGHT")
    source_boundaries = list(source.get("boundaries", []))
    target_boundaries = list(target.get("boundaries", []))
    if len(target_boundaries) != len(source_boundaries) + 1 or target_boundaries[:-1] != source_boundaries:
        raise PartitionDefinitionMismatch(
            "only one extra trailing target boundary is eligible; missing, middle, multiple, type, or range differences need manual reconciliation"
        )
    if target.get("fanout") != source.get("fanout") + 1:
        raise PartitionDefinitionMismatch("target fanout is not exactly one greater than the source fanout")
    if len(pf.boundaries) != len(source_boundaries) or [
        _normalize_partition_value(value) for value in pf.boundaries
    ] != source_boundaries:
        raise PartitionDefinitionMismatch("partition function DTO does not match the inspected source definition")

    source_schemes = source_metadata.get("schemes", {})
    expected_schemes = {
        scheme_name: scheme for scheme_name, scheme in source_schemes.items()
        if scheme.get("partition_function_name") == name
    }
    dependent_schemes = {
        scheme_name: scheme for scheme_name, scheme in target_schemes.items()
        if scheme.get("partition_function_name") == name
    }
    if not dependent_schemes:
        raise PartitionDefinitionMismatch("target partition function has no inspectable dependent partition scheme")

    target_destinations_to_check: set[str] = set()
    scheme_next_used: dict[str, str] = {}
    for scheme_name, target_scheme in dependent_schemes.items():
        source_scheme = expected_schemes.get(scheme_name)
        if source_scheme is None:
            raise PartitionDefinitionMismatch(
                f"target-only partition scheme {scheme_name} depends on {name}; preserving unrelated dependencies requires manual reconciliation"
            )
        target_destinations = [
            (int(destination_id), str(filegroup_name))
            for destination_id, filegroup_name in target_scheme.get("destinations", [])
        ]
        expected_destinations = _expected_scheme_destinations(source_scheme)
        all_to_mapping = (
            len(target_destinations) == 1 and target_destinations[0][0] == 0
            and target_destinations == expected_destinations
        )
        # SQL Server can retain one destination beyond the source fanout as
        # its NEXT USED filegroup. MERGE RANGE removes the boundary partition,
        # but does not create that extra destination slot for a target scheme
        # that lacks one. In that case we explicitly restore the source's
        # NEXT USED designation after the merge using ALTER PARTITION SCHEME.
        target_fanout = target.get("fanout")
        source_fanout = source.get("fanout")
        source_partition_destinations = expected_destinations[:source_fanout]
        source_has_next_used = (
            source_fanout is not None
            and len(expected_destinations) == source_fanout + 1
            and [destination_id for destination_id, _ in expected_destinations]
            == list(range(1, source_fanout + 2))
        )
        target_partition_destinations_match = (
            source_fanout is not None
            and len(target_destinations) in (source_fanout, target_fanout)
            and [destination_id for destination_id, _ in target_destinations[:source_fanout]]
            == list(range(1, source_fanout + 1))
            and target_destinations[:source_fanout] == source_partition_destinations
        )
        if target_partition_destinations_match and source_has_next_used:
            # If target has no trailing NEXT USED destination (the live
            # five-vs-six case), adding this designation is an explicit,
            # supported metadata operation with no data movement. If it
            # already has a trailing slot, reassert the source designation
            # after MERGE so the persisted catalog is deterministic.
            scheme_next_used[scheme_name] = expected_destinations[-1][1]
            target_destinations_to_check.add(expected_destinations[-1][1])
        partition_mapping = (
            target_partition_destinations_match
            and (source_has_next_used or len(expected_destinations) == source_fanout)
        )
        if not (all_to_mapping or partition_mapping):
            predicted = (
                [*source_partition_destinations,
                 (source_fanout + 1, scheme_next_used[scheme_name])]
                if scheme_name in scheme_next_used else target_destinations[:source_fanout]
            )
            raise PartitionDefinitionMismatch(
                f"partition scheme {scheme_name} mapping is incompatible with the source after removing the empty tail partition; "
                f"retained target destinations are {target_destinations[:source_fanout]!r}, "
                f"expected source partition destinations are {source_partition_destinations!r}, "
                f"predicted post-merge destinations are {predicted!r}, "
                f"source destinations are {expected_destinations!r}"
            )
        target_destinations_to_check.update(name for _, name in target_destinations)

    source_dependency_keys = {
        (definition["schema_name"], definition["table_name"], key[2], definition.get("object_type"))
        for key, definition in source_metadata.get("tables", {}).items()
        if definition.get("partition_function_name") == name
    }
    for dependency in dependencies:
        dependency_key = (
            dependency["schema_name"], dependency["object_name"],
            dependency["index_name"], dependency["object_type"],
        )
        if dependency_key not in source_dependency_keys:
            raise PartitionDefinitionMismatch(
                f"target-only dependent index {dependency['schema_name']}.{dependency['object_name']}"
                f".{dependency['index_name'] or dependency['index_id']} uses {name}; preserving it requires manual reconciliation"
            )
        if dependency["rows"] != 0:
            raise PartitionDefinitionMismatch(
                f"dependent index {dependency['schema_name']}.{dependency['object_name']}"
                f".{dependency['index_name'] or dependency['index_id']} has {dependency['rows']} row(s) "
                f"in rightmost partition {dependency['partition_number']}; no merge was attempted"
            )

    for filegroup_name in target_destinations_to_check:
        state = filegroups.get(filegroup_name)
        if (state is None or state.get("type_desc") != "ROWS_FILEGROUP"
                or state.get("is_read_only") not in (False, 0) or not state.get("file_states")
                or any(file_state != "ONLINE" for file_state in state["file_states"])):
            raise PartitionDefinitionMismatch(
                f"partition filegroup {filegroup_name} is missing, read-only, offline, or not a row filegroup"
            )

    boundary_type, boundary_text = target_boundaries[-1]
    if boundary_type != "date":
        raise PartitionDefinitionMismatch("extra target boundary is not a normalized date value")
    try:
        boundary_value = date.fromisoformat(boundary_text)
    except (TypeError, ValueError) as exc:
        raise PartitionDefinitionMismatch(f"could not normalize extra date boundary {boundary_text!r}") from exc
    return boundary_value.isoformat(), dependencies, scheme_next_used


def reconcile_partition_function(
    conn: Any,
    pf: PartitionFunctionDef,
    source_metadata: dict[str, Any],
) -> None:
    """Safely remove one empty trailing RANGE RIGHT date partition boundary."""
    validate_identifier(pf.name, "partition function")
    source_definition = source_metadata.get("functions", {}).get(pf.name)
    try:
        target_functions = inspect_partition_functions(conn)
        target_schemes = inspect_partition_schemes(conn)
        target_definition = target_functions.get(pf.name)
        if target_definition is None:
            raise PartitionDefinitionMismatch(f"target partition function {pf.name} is missing")
        dependent_schemes = {
            scheme for scheme, definition in target_schemes.items()
            if definition.get("partition_function_name") == pf.name
        }
        tail_rows = _inspect_partition_tail_occupancy(conn, pf.name, target_definition["fanout"])
        filegroup_names = {
            filegroup for scheme_name in dependent_schemes
            for _, filegroup in target_schemes[scheme_name].get("destinations", [])
        }
        for scheme in source_metadata.get("schemes", {}).values():
            if scheme.get("partition_function_name") == pf.name:
                filegroup_names.update(
                    filegroup for _, filegroup in _expected_scheme_destinations(scheme)
                )
        filegroup_status = _inspect_partition_filegroups(conn, filegroup_names)
        boundary, dependency_details, scheme_next_used = _plan_safe_trailing_date_merge(
            pf, source_metadata, target_functions, target_schemes,
            tail_rows, filegroup_status,
        )
        target_schemes_expected = source_metadata.get("schemes", {})

        def verify_current_catalog() -> tuple[dict[str, Any], dict[str, Any]]:
            functions_after = inspect_partition_functions(conn)
            schemes_after = inspect_partition_schemes(conn)
            if functions_after.get(pf.name) != source_definition:
                raise PartitionDefinitionMismatch(
                    f"post-merge partition function {pf.name} does not match the source definition"
                )
            for scheme_name in dependent_schemes:
                if schemes_after.get(scheme_name) != target_schemes_expected.get(scheme_name):
                    raise PartitionDefinitionMismatch(
                        f"post-merge partition scheme {scheme_name} does not match the source definition"
                    )
            return functions_after[pf.name], {
                scheme_name: schemes_after[scheme_name] for scheme_name in dependent_schemes
            }

        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION")
            cur.execute(
                f"ALTER PARTITION FUNCTION {quote_identifier(pf.name)}() "
                f"MERGE RANGE ('{boundary}')"
            )
            for scheme_name, filegroup_name in scheme_next_used.items():
                cur.execute(
                    f"ALTER PARTITION SCHEME {quote_identifier(scheme_name)} "
                    f"NEXT USED {quote_identifier(filegroup_name)}"
                )
        verify_current_catalog()
        conn.commit()
        # Read once more after commit so a successful status reflects persisted catalog state.
        persisted_function, persisted_schemes = verify_current_catalog()
        audit_log(
            phase="partition_function_reconciliation", status="reconciled",
            details={
                "function": pf.name, "boundary_removed": boundary,
                "definition_matches": True, "source": source_definition,
                "target_before": target_definition, "target_after": persisted_function,
                "schemes_after": persisted_schemes,
                "next_used_filegroups_set": scheme_next_used,
                "dependent_indexes_checked": dependency_details,
                "data_movement_expected": False,
            },
        )
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        audit_log(
            phase="partition_function_reconciliation", status="rejected",
            details={"function": pf.name, "definition_matches": False,
                     "source": source_definition, "reason": str(exc)},
        )
        if isinstance(exc, PartitionDefinitionMismatch):
            raise
        raise PartitionDefinitionMismatch(
            f"Safe reconciliation of partition function {pf.name} failed: {exc}"
        ) from exc


def reconcile_partition_scheme(
    conn: Any,
    ps: PartitionSchemeDef,
    source_metadata: dict[str, Any],
) -> None:
    """Restore one missing SQL Server NEXT USED destination without rebuilding objects."""
    validate_identifier(ps.name, "partition scheme")
    source_scheme = source_metadata.get("schemes", {}).get(ps.name)
    source_function = source_metadata.get("functions", {}).get(ps.partition_function_name)
    try:
        target_functions = inspect_partition_functions(conn)
        target_schemes = inspect_partition_schemes(conn)
        target_function = target_functions.get(ps.partition_function_name)
        target_scheme = target_schemes.get(ps.name)
        if source_scheme is None or source_function is None:
            raise PartitionDefinitionMismatch(
                f"source metadata for partition scheme {ps.name} or function "
                f"{ps.partition_function_name} is unavailable"
            )
        if target_function is None or target_function != source_function:
            raise PartitionDefinitionMismatch(
                f"partition scheme {ps.name} cannot be reconciled because function "
                f"{ps.partition_function_name} does not exactly match source metadata"
            )
        if target_scheme is None:
            raise PartitionDefinitionMismatch(
                f"target partition scheme {ps.name} is missing; scheme creation is handled separately"
            )
        if (source_scheme.get("partition_function_name") != ps.partition_function_name
                or target_scheme.get("partition_function_name") != ps.partition_function_name):
            raise PartitionDefinitionMismatch(
                f"partition scheme {ps.name} is not associated with source function "
                f"{ps.partition_function_name} on both databases"
            )

        expected = _expected_scheme_destinations(source_scheme)
        actual = _expected_scheme_destinations(target_scheme)
        fanout = int(source_function["fanout"])
        if (not source_function.get("catalog_consistent")
                or len(expected) != fanout + 1
                or [destination_id for destination_id, _ in expected] != list(range(1, fanout + 2))):
            raise PartitionDefinitionMismatch(
                f"source partition scheme {ps.name} does not have exactly one explicit NEXT USED "
                f"destination after its {fanout} partition destinations"
            )
        if actual == expected:
            return
        eligible = (
            len(actual) == fanout
            and [destination_id for destination_id, _ in actual] == list(range(1, fanout + 1))
            and actual == expected[:fanout]
        )
        if not eligible:
            raise PartitionDefinitionMismatch(
                f"partition scheme {ps.name} has incompatible destinations: "
                f"target retained destinations are {actual!r}; source partition destinations are "
                f"{expected[:fanout]!r}; source NEXT USED destination is {expected[-1]!r}"
            )

        next_used_filegroup = expected[-1][1]
        filegroup_status = _inspect_partition_filegroups(conn, {next_used_filegroup})
        state = filegroup_status.get(next_used_filegroup)
        if (state is None or state.get("type_desc") != "ROWS_FILEGROUP"
                or state.get("is_read_only") not in (False, 0) or not state.get("file_states")
                or any(file_state != "ONLINE" for file_state in state["file_states"])):
            raise PartitionDefinitionMismatch(
                f"NEXT USED filegroup {next_used_filegroup} for partition scheme {ps.name} "
                "is missing, read-only, offline, or not a row filegroup"
            )

        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION")
            cur.execute(
                f"ALTER PARTITION SCHEME {quote_identifier(ps.name)} "
                f"NEXT USED {quote_identifier(next_used_filegroup)}"
            )
        target_after = inspect_partition_schemes(conn).get(ps.name)
        if target_after != source_scheme:
            raise PartitionDefinitionMismatch(
                f"post-reconciliation partition scheme {ps.name} still differs from source: "
                f"target destinations are {_expected_scheme_destinations(target_after or {})!r}; "
                f"source destinations are {expected!r}"
            )
        conn.commit()
        persisted = inspect_partition_schemes(conn).get(ps.name)
        if persisted != source_scheme:
            raise PartitionDefinitionMismatch(
                f"persisted partition scheme {ps.name} does not match source after setting NEXT USED"
            )
        audit_log(
            phase="partition_scheme_reconciliation", status="reconciled",
            details={
                "scheme": ps.name, "partition_function": ps.partition_function_name,
                "definition_matches": True, "source": source_scheme,
                "target_before": target_scheme, "target_after": persisted,
                "next_used_filegroup": next_used_filegroup,
                "data_movement_expected": False,
            },
        )
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        audit_log(
            phase="partition_scheme_reconciliation", status="rejected",
            details={
                "scheme": ps.name, "definition_matches": False,
                "source": source_scheme, "target": target_scheme if "target_scheme" in locals() else None,
                "reason": str(exc),
            },
        )
        if isinstance(exc, PartitionDefinitionMismatch):
            raise
        raise PartitionDefinitionMismatch(
            f"Safe reconciliation of partition scheme {ps.name} failed: {exc}"
        ) from exc


def compare_partition_metadata(source_conn: Any, target_conn: Any, schemas: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Compare source-managed SQL Server partition functions, schemes and indexes."""
    source_functions = inspect_partition_functions(source_conn)
    target_functions = inspect_partition_functions(target_conn)
    source_schemes = inspect_partition_schemes(source_conn)
    target_schemes = inspect_partition_schemes(target_conn)
    source_tables = inspect_partitioned_tables(source_conn, schemas)
    target_tables = inspect_partitioned_tables(target_conn, schemas)
    mismatches: list[dict[str, Any]] = []

    def compare_map(kind: str, source: dict, target: dict) -> None:
        for name in sorted(source):
            if name not in target:
                mismatches.append({"object": name, "kind": kind, "source": source[name], "target": None,
                                   "reason": f"target {kind} is missing"})
            elif source[name] != target[name]:
                mismatches.append({"object": name, "kind": kind, "source": source[name], "target": target[name],
                                   "reason": f"source and target {kind} definitions differ"})

    compare_map("partition_function", source_functions, target_functions)
    compare_map("partition_scheme", source_schemes, target_schemes)
    compare_map("partitioned_table_index", source_tables, target_tables)
    return {
        "definition_matches": not mismatches,
        "source": {"functions": source_functions, "schemes": source_schemes, "tables": source_tables},
        "target": {"functions": target_functions, "schemes": target_schemes, "tables": target_tables},
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# 2. TARGET-SIDE PARTITION OPERATIONS
#    Called/delegated by MSSQLTargetConnector methods.
#    Partition function/scheme creation and partitioned-table creation for
#    writing to a target database.
# ---------------------------------------------------------------------------

def create_partition_function(conn: Any, pf: PartitionFunctionDef) -> None:
    """Create a partition function from metadata.

    Replaces ``MSSQLTargetConnector.create_partition_function``.
    """
    validate_identifier(pf.name, "partition function")
    validate_identifier(pf.schema_name, "schema")
    pf_schema = pf.schema_name or "dbo"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pf.boundary_value_on_right, t.name, pp.max_length, "
            "pp.precision, pp.scale, prv.value, pf.fanout, prv.boundary_id "
            "FROM sys.partition_functions pf "
            "JOIN sys.partition_parameters pp ON pp.function_id = pf.function_id "
            "JOIN sys.types t ON t.user_type_id = pp.user_type_id "
            "LEFT JOIN sys.partition_range_values prv ON prv.function_id = pf.function_id "
            "WHERE pf.name = ? ORDER BY prv.boundary_id",
            (pf.name,),
        )
        existing = list(cur.fetchall() or [])
        if existing:
            target_boundaries = [row[5] for row in existing if row[5] is not None]
            source_boundaries_normalized = [_normalize_partition_value(value) for value in pf.boundaries]
            target_boundaries_normalized = [_normalize_partition_value(value) for value in target_boundaries]
            target_type = _mssql_column_type(existing[0][1], existing[0][2], existing[0][3], existing[0][4])
            requested_type = " ".join(str(pf.data_type).lower().split())
            target_type_normalized = " ".join(str(target_type).lower().replace(" ", "").split())
            requested_type_normalized = " ".join(requested_type.replace(" ", "").split())
            matches = (
                target_boundaries_normalized == source_boundaries_normalized
                and int(existing[0][6]) == len(pf.boundaries) + 1
                and (pf.fanout is None or int(existing[0][6]) == pf.fanout)
                and bool(existing[0][0]) == (pf.range_desc.upper() == "RANGE RIGHT")
                and target_type_normalized == requested_type_normalized
            )
            if not matches:
                source_definition = {
                    "name": pf.name,
                    "data_type": pf.data_type,
                    "range": pf.range_desc,
                    "fanout": pf.fanout if pf.fanout is not None else len(pf.boundaries) + 1,
                    "boundaries": source_boundaries_normalized,
                }
                target_definition = {
                    "name": pf.name,
                    "data_type": target_type,
                    "range": "RANGE RIGHT" if existing[0][0] else "RANGE LEFT",
                    "fanout": int(existing[0][6]),
                    "boundaries": target_boundaries_normalized,
                }
                audit_log(
                    phase="create_partition_function", status="mismatch",
                    details={"function": f"{pf_schema}.{pf.name}",
                             "definition_matches": False, "source": source_definition,
                             "target": target_definition,
                             "reason": "existing definition requires explicit reconciliation"},
                )
                raise PartitionDefinitionMismatch(
                    f"Partition function {pf.name} differs from source definition "
                    f"(source boundaries={list(pf.boundaries)!r}, target boundaries={target_boundaries!r}, "
                    f"source type={pf.data_type!r}, target type={target_type!r}, "
                    f"source range={pf.range_desc!r}, target range={'RANGE RIGHT' if existing[0][0] else 'RANGE LEFT'}). "
                    "The create step did not alter the existing definition. Enable the narrowly supported "
                    "empty trailing DATE partition reconciliation when eligible, or use an approved "
                    "maintenance plan for other differences."
                )
            audit_log(
                phase="create_partition_function", status="exists",
                details={"function": f"{pf_schema}.{pf.name}", "definition_matches": matches},
            )
            return

        if pf_schema != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (pf_schema,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(pf_schema)}")
                audit_log(
                    phase="create_schema", status="created",
                    details={"schema": pf_schema},
                )

        boundaries = ", ".join(
            f"'{b.strftime('%Y-%m-%d')}'" if isinstance(b, (datetime, date))
            else f"'{b}'" if isinstance(b, str)
            else str(b)
            for b in pf.boundaries
        )
        ddl = (
            f"CREATE PARTITION FUNCTION {quote_identifier(pf.name)} "
            f"({pf.data_type}) "
            f"AS {pf.range_desc} FOR VALUES ({boundaries})"
        )
        try:
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_partition_function", status="created",
                details={"function": f"{pf_schema}.{pf.name}"},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_partition_function", status="failed",
                details={"function": f"{pf_schema}.{pf.name}", "reason": str(exc)},
            )
            raise


def create_partition_scheme(conn: Any, ps: PartitionSchemeDef) -> None:
    """Create a partition scheme from metadata.

    Replaces ``MSSQLTargetConnector.create_partition_scheme``.
    """
    validate_identifier(ps.name, "partition scheme")
    validate_identifier(ps.schema_name, "schema")
    ps_schema = ps.schema_name or "dbo"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ps.name, pf.name, ds.name, dds.destination_id, dds.data_space_id "
            "FROM sys.partition_schemes ps "
            "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
            "JOIN sys.destination_data_spaces dds ON dds.partition_scheme_id = ps.data_space_id "
            "JOIN sys.data_spaces ds ON ds.data_space_id = dds.data_space_id "
            "WHERE ps.name = ? ORDER BY dds.destination_id",
            (ps.name,),
        )
        existing = list(cur.fetchall() or [])
        if existing:
            target_function = str(existing[0][1])
            target_filegroups = [row[2] for row in existing]
            target_destinations = [(int(row[3]), str(row[2])) for row in existing]
            expected_destinations = ps.destinations or list(enumerate(ps.filegroups, start=1))
            if (target_function != ps.partition_function_name
                    or target_filegroups != list(ps.filegroups)
                    or target_destinations != expected_destinations):
                source_definition = {
                    "name": ps.name,
                    "partition_function_name": ps.partition_function_name,
                    "destinations": expected_destinations,
                }
                target_definition = {
                    "name": ps.name,
                    "partition_function_name": target_function,
                    "destinations": target_destinations,
                }
                audit_log(
                    phase="create_partition_scheme", status="mismatch",
                    details={"scheme": f"{ps_schema}.{ps.name}",
                             "definition_matches": False, "source": source_definition,
                             "target": target_definition,
                             "reason": "automatic partition scheme replacement is disabled"},
                )
                raise PartitionDefinitionMismatch(
                    f"Partition scheme {ps.name} differs from source definition "
                    f"(source function={ps.partition_function_name!r}, filegroups={list(ps.filegroups)!r}; "
                    f"target function={target_function!r}, filegroups={target_filegroups!r}, "
                    f"destinations={target_destinations!r}). "
                    "Automatic replacement is disabled because dependent tables/indexes may use this scheme."
                )
            audit_log(
                phase="create_partition_scheme", status="exists",
                details={"scheme": f"{ps_schema}.{ps.name}", "definition_matches": True},
            )
            return

        if ps_schema != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (ps_schema,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(ps_schema)}")
                audit_log(
                    phase="create_schema", status="created",
                    details={"schema": ps_schema},
                )

        filegroups = ", ".join(f"[{fg}]" for fg in ps.filegroups)
        ddl = (
            f"CREATE PARTITION SCHEME {quote_identifier(ps.name)} "
            f"AS PARTITION {quote_identifier(ps.partition_function_name)} "
            f"TO ({filegroups})"
        )
        try:
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_partition_scheme", status="created",
                details={"scheme": f"{ps_schema}.{ps.name}"},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_partition_scheme", status="failed",
                details={"scheme": f"{ps_schema}.{ps.name}", "reason": str(exc)},
            )
            raise


def create_partitioned_table(
    conn: Any,
    schema: Schema,
    partition_scheme_name: str,
    partition_column: str,
) -> None:
    """Create a table with partition-scheme binding.

    Replaces ``MSSQLTargetConnector.create_partitioned_table``.
    Delegates the complete partitioned-table creation logic: existence
    check, schema creation, column DDL generation, primary-key constraint,
    and CREATE TABLE ... ON partition_scheme(column) execution.
    """
    validate_identifier(schema.name, "table")
    schema_name = schema.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    validate_identifier(partition_scheme_name, "partition scheme")
    validate_identifier(partition_column, "column")
    qualified = _qualify(schema_name, schema.name)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
            (schema.name, schema_name),
        )
        if cur.fetchone() is not None:
            cur.execute(
                "SELECT i.name, ps.name, pf.name, ic.partition_ordinal, c.name "
                "FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "JOIN sys.indexes i ON i.object_id = t.object_id "
                "LEFT JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id "
                "LEFT JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
                "LEFT JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                "AND ic.partition_ordinal > 0 "
                "LEFT JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                "WHERE s.name = ? AND t.name = ? ORDER BY i.name, ic.partition_ordinal",
                (schema_name, schema.name),
            )
            rows = list(cur.fetchall() or [])
            expected = (partition_scheme_name, None, [partition_column])
            actual = {
                (row[1], row[2], row[4]) for row in rows
                if row[1] is not None or row[2] is not None or row[4] is not None
            }
            matches = bool(actual) and all(
                scheme == expected[0] and column == partition_column
                for scheme, _function, column in actual
            )
            if not matches:
                details = {
                    "table": qualified,
                    "source": {"partition_scheme": partition_scheme_name,
                               "partition_function": None,
                               "partition_column": partition_column},
                    "target": [{"index": row[0], "partition_scheme": row[1],
                                "partition_function": row[2], "partition_column": row[4]}
                               for row in rows],
                    "definition_matches": False,
                }
                audit_log(phase="create_partitioned_table", status="mismatch", details=details)
                raise RuntimeError(
                    f"Existing table {qualified} is not bound to partition scheme "
                    f"{partition_scheme_name} on column {partition_column}; target table was preserved."
                )
            audit_log(
                phase="create_partitioned_table",
                status="exists",
                details={"table": qualified, "definition_matches": True},
            )
            return

        if schema_name != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": schema_name},
                )

        col_defs: list[str] = []
        for col in schema.columns:
            col_type = col.source_type or col.target_type or "nvarchar"
            col_defs.append(_column_ddl(col, col_type))

        if schema.primary_key:
            pk_cols = ", ".join(schema.primary_key)
            # Mirror the non-partitioned path: an explicitly named source PK keeps its
            # name; an unnamed/auto-named one still emits a bare PRIMARY KEY so SQL
            # Server generates its own name, exactly as before.
            if schema.primary_key_name:
                col_defs.append(
                    f"CONSTRAINT {quote_identifier(schema.primary_key_name)} "
                    f"PRIMARY KEY ({pk_cols})"
                )
            else:
                col_defs.append(f"PRIMARY KEY ({pk_cols})")

        ddl = (
            f"CREATE TABLE {qualified} ({', '.join(col_defs)}) "
            f"ON {partition_scheme_name}({partition_column})"
        )
        try:
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_partitioned_table",
                status="created",
                details={"table": qualified},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_partitioned_table",
                status="failed",
                details={"table": qualified, "reason": str(exc)},
            )
            raise
