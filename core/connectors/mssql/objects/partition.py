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
from typing import Any

from core.connectors.base import (
    Schema,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    PartitionFunctionDef,
    PartitionSchemeDef,
    PartitionedTableDef,
    _qualify,
    _resolve_mssql_schemas,
)
from core.connectors.mssql.objects.table import _column_ddl
from core.audit_logger import audit_log


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
            "SELECT pf.name, pf.type_desc, pf.boundary_value_on_right, "
            "prv.value, prv.boundary_id "
            "FROM sys.partition_functions pf "
            "LEFT JOIN sys.partition_range_values prv "
            "  ON prv.function_id = pf.function_id "
            "ORDER BY pf.name, prv.boundary_id",
        )
        pf_map: dict = {}
        for row in cur.fetchall():
            pf_name, type_desc, bvr, value, boundary_id = row
            if pf_name not in pf_map:
                range_desc = "RANGE RIGHT" if bvr else "RANGE LEFT"
                pf_map[pf_name] = PartitionFunctionDef(
                    name=pf_name,
                    schema_name="dbo",
                    data_type="datetime2",
                    boundaries=[],
                    range_desc=range_desc,
                )
            if value is not None:
                pf_map[pf_name].boundaries.append(value)
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
                "fg.name AS fg_name "
                "FROM sys.partition_schemes ps "
                "JOIN sys.partition_functions pf ON ps.function_id = pf.function_id "
                "JOIN sys.destination_data_spaces dds "
                "  ON dds.partition_scheme_id = ps.data_space_id "
                "JOIN sys.filegroups fg ON fg.data_space_id = dds.data_space_id "
                "WHERE ps.name = ? "
                "ORDER BY dds.destination_id",
                (ps_name,),
            )
            fg_list = []
            pf_name_val = ""
            for row in cur.fetchall():
                _, pf_name, fg_name = row
                pf_name_val = pf_name
                fg_list.append(fg_name)
            results.append(PartitionSchemeDef(
                name=ps_name,
                schema_name="dbo",
                partition_function_name=pf_name_val,
                filegroups=fg_list,
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
            "SELECT 1 FROM sys.partition_functions WHERE name = ?",
            (pf.name,),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_partition_function", status="exists",
                details={"function": f"{pf_schema}.{pf.name}"},
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
            "SELECT 1 FROM sys.partition_schemes WHERE name = ?",
            (ps.name,),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_partition_scheme", status="exists",
                details={"scheme": f"{ps_schema}.{ps.name}"},
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
            audit_log(
                phase="create_partitioned_table",
                status="exists",
                details={"table": qualified},
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
