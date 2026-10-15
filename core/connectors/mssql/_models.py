"""MSSQL connector internal models, constants, and helper functions.

Holds four kinds of shared, connector-independent building blocks used by
``source.py``, ``target.py`` and ``objects/*``:

  - constants          (system-schema and fixed-role sets, type sets)
  - dataclasses        (partition function/scheme/table metadata)
  - schema, type and qualification helpers
                        (``_resolve_mssql_schemas``, ``_qualify``,
                        ``_build_mssql_index_ddl``, ``_mssql_column_type``)
  - catalog introspection helpers
                        (``_non_computed_column_names``,
                        ``_target_identity_columns``,
                        ``_variant_column_names``)

The introspection helpers are re-exported from the package ``__init__`` and
resolved there at call time by ``objects/table.py``, which is what lets tests
patch ``core.connectors.mssql._non_computed_column_names``. Keep them here.

Public import path remains: ``from core.connectors.mssql import ...``
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.connectors.base import quote_identifier


_MSSQL_SYSTEM_SCHEMAS = frozenset({"sys", "INFORMATION_SCHEMA", "guest"})

_MSSQL_FIXED_DB_ROLES = frozenset({
    "public", "dbo", "guest", "INFORMATION_SCHEMA", "sys",
    "db_owner", "db_securityadmin", "db_accessadmin", "db_ddladmin",
    "db_datareader", "db_datawriter", "db_backupoperator",
    "db_denydatareader", "db_denydatawriter",
})


# ---------------------------------------------------------------------------
# Step 12 — Partitioning dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PartitionFunctionDef:
    """MSSQL partition function metadata."""
    name: str
    schema_name: str = "dbo"
    data_type: str = "datetime2"
    boundaries: list[Any] = field(default_factory=list)
    range_desc: str = "RANGE RIGHT"
    fanout: int | None = None


@dataclass
class PartitionSchemeDef:
    """MSSQL partition scheme metadata."""
    name: str
    schema_name: str = "dbo"
    partition_function_name: str = ""
    filegroups: list[str] = field(default_factory=list)
    destinations: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class PartitionedTableDef:
    """MSSQL partitioned table/index metadata."""
    table_name: str
    schema_name: str = "dbo"
    index_name: str | None = None
    partition_function_name: str = ""
    partition_scheme_name: str = ""
    partition_column: str = ""


# ---------------------------------------------------------------------------
# Schema / identifier helpers
# ---------------------------------------------------------------------------

def _resolve_mssql_schemas(config: dict[str, Any]) -> tuple[str, ...] | None:
    raw = config.get("include_schemas")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    cleaned = [
        s for s in raw
        if isinstance(s, str) and s and s not in _MSSQL_SYSTEM_SCHEMAS
    ]
    return tuple(cleaned) if cleaned else None


def _qualify(schema_name: str | None, object_name: str) -> str:
    schema = schema_name or "dbo"
    return f"{quote_identifier(schema)}.{quote_identifier(object_name)}"


def _skip_mssql_sql_trivia(sql: str, offset: int = 0) -> int:
    """Skip SQL Server whitespace and leading line/block comments.

    T-SQL block comments may nest, so a small scanner is more reliable than a
    regular expression when finding the first DDL token in catalog text.
    """
    length = len(sql)
    while offset < length:
        if sql[offset].isspace():
            offset += 1
        elif sql.startswith("--", offset):
            newline = sql.find("\n", offset + 2)
            offset = length if newline < 0 else newline + 1
        elif sql.startswith("/*", offset):
            offset += 2
            depth = 1
            while offset < length and depth:
                if sql.startswith("/*", offset):
                    depth += 1
                    offset += 2
                elif sql.startswith("*/", offset):
                    depth -= 1
                    offset += 2
                else:
                    offset += 1
        else:
            break
    return offset


def _read_mssql_identifier(sql: str, offset: int) -> int | None:
    """Return the end offset of one quoted or unquoted T-SQL identifier."""
    if offset >= len(sql):
        return None
    if sql[offset] == "[":
        offset += 1
        while offset < len(sql):
            if sql[offset] == "]":
                if offset + 1 < len(sql) and sql[offset + 1] == "]":
                    offset += 2
                    continue
                return offset + 1
            offset += 1
        return None
    if sql[offset] == '"':
        offset += 1
        while offset < len(sql):
            if sql[offset] == '"':
                if offset + 1 < len(sql) and sql[offset + 1] == '"':
                    offset += 2
                    continue
                return offset + 1
            offset += 1
        return None
    match = re.match(r"(?:[^\W\d]|[_#@])(?:[\w$#@])*", sql[offset:], re.UNICODE)
    return offset + match.end() if match else None


def _read_mssql_qualified_identifier(
    sql: str, offset: int, *, max_parts: int = 2
) -> tuple[int, int] | None:
    """Return (end offset, part count) for a quoted T-SQL multipart name."""
    end = _read_mssql_identifier(sql, offset)
    if end is None:
        return None
    parts = 1
    while parts < max_parts:
        dot_offset = _skip_mssql_sql_trivia(sql, end)
        if dot_offset >= len(sql) or sql[dot_offset] != ".":
            break
        next_offset = _skip_mssql_sql_trivia(sql, dot_offset + 1)
        end = _read_mssql_identifier(sql, next_offset)
        if end is None:
            return None
        parts += 1
    return end, parts


# ---------------------------------------------------------------------------
# DDL builders
# ---------------------------------------------------------------------------

def _build_mssql_index_ddl(
    idx_name: str,
    is_unique: bool,
    schema_name: str | None,
    table_name: str,
    key_cols: list[tuple[str, bool]],   # (column_name, is_descending)
    included_cols: list[str] = None,
    filter_def: str | None = None,
    *,
    on_view: bool = False,
    clustered: bool = False,
) -> str:
    """Build a complete CREATE [UNIQUE] [CLUSTERED] INDEX DDL for MSSQL."""
    schema = schema_name or "dbo"
    schema_q = quote_identifier(schema)
    table_q = quote_identifier(table_name)
    unique_str = "UNIQUE " if is_unique else ""
    clustered_str = "CLUSTERED " if clustered else "NONCLUSTERED " if on_view else ""
    cols = ", ".join(
        f"{quote_identifier(col)} {'DESC' if desc else 'ASC'}"
        for col, desc in key_cols
    )
    ddl = f"CREATE {unique_str}{clustered_str}INDEX {quote_identifier(idx_name)} ON {schema_q}.{table_q}({cols})"
    if included_cols:
        inc = ", ".join(quote_identifier(c) for c in included_cols)
        ddl += f" INCLUDE ({inc})"
    if filter_def:
        ddl += f" WHERE {filter_def}"
    return ddl


_MSSQL_SIZE_TYPES = frozenset({
    "nvarchar", "varchar", "char", "nchar", "varbinary", "binary",
})
_MSSQL_PRECISION_TYPES = frozenset({"decimal", "numeric"})


def _mssql_column_type(
    base_type: str,
    size: Any,
    precision: int | None = None,
    scale: int | None = None,
) -> str:
    """Re-attach length/precision to MSSQL types.

    INFORMATION_SCHEMA.COLUMNS reports only the base DATA_TYPE (e.g.
    ``nvarchar``). Emitting it bare makes SQL Server default to
    ``nvarchar(1)`` (truncating data) or ``decimal`` -> ``decimal(18,0)``
    (rounding away fractional values). Re-attach length/precision so the
    target DDL matches the source (e.g. ``nvarchar(100)``, ``decimal(10,2)``).
    """
    if not base_type or "(" in base_type:
        return base_type
    bt = base_type.lower()
    if bt in _MSSQL_PRECISION_TYPES and precision is not None and scale is not None:
        return f"{base_type}({precision},{scale})"
    if bt in _MSSQL_SIZE_TYPES:
        if size == -1:
            return f"{base_type}(MAX)"
        if size is not None:
            return f"{base_type}({size})"
    return base_type


# ---------------------------------------------------------------------------
# Column introspection helpers
# ---------------------------------------------------------------------------

def _non_computed_column_names(conn: Any, object_name: str, schema_name: str | None) -> list[str]:
    """Return insertable (non-computed) column names for a table, in ordinal order."""
    sn = schema_name or "dbo"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM sys.columns c "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE t.name = ? AND s.name = ? AND c.is_computed = 0 "
            "ORDER BY c.column_id",
            (object_name, sn),
        )
        return [r[0] for r in cur.fetchall()]


def _target_identity_columns(conn: Any, object_name: str, schema_name: str | None) -> list[str]:
    """Return the columns that are IDENTITY columns on the *target* table.

    Used to decide whether ``SET IDENTITY_INSERT`` is required — we inspect the
    actual target table (not the source schema), so pre-existing Step 4 tables
    that were created as plain ``INT`` are left untouched.
    """
    sn = schema_name or "dbo"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM sys.columns c "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE t.name = ? AND s.name = ? AND c.is_identity = 1",
            (object_name, sn),
        )
        return [r[0] for r in cur.fetchall()]


def _variant_column_names(conn: Any, object_name: str, schema_name: str | None) -> set[str]:
    """Return names of ``sql_variant`` columns in a table.

    pyodbc cannot natively read ODBC SQL type -16 (``SQL_VARIANT``), so
    callers must ``CAST`` these columns to a readable type before SELECT.
    """
    sn = schema_name or "dbo"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM sys.columns c "
            "JOIN sys.types t ON c.user_type_id = t.user_type_id "
            "JOIN sys.tables tbl ON c.object_id = tbl.object_id "
            "JOIN sys.schemas s ON tbl.schema_id = s.schema_id "
            "WHERE tbl.name = ? AND s.name = ? AND t.name = 'sql_variant' "
            "AND c.is_computed = 0",
            (object_name, sn),
        )
        return {r[0] for r in cur.fetchall()}
