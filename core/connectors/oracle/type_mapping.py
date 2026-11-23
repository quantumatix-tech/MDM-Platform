"""Conservative Oracle source mappings for supported relational targets."""
from __future__ import annotations

from typing import Any

from core.connectors.base import UnmappedTypeError


def map_oracle_source_column(column: Any, target_engine: str, table_name: str) -> str:
    """Return a target SQL type for a supported Oracle scalar column."""
    source_type = str(column.source_type or "").upper().strip()
    if source_type == "NUMBER":
        precision = column.precision
        scale = int(column.scale or 0)
        if precision is None or scale:
            canonical = "numeric"
        elif precision <= 4:
            canonical = "smallint"
        elif precision <= 9:
            canonical = "integer"
        elif precision <= 18:
            canonical = "bigint"
        else:
            canonical = "numeric"
    elif source_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"}:
        canonical = "char" if source_type in {"CHAR", "NCHAR"} else "varchar"
    elif source_type in {"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"}:
        canonical = "timestamp"
    elif source_type == "BINARY_FLOAT":
        canonical = "real"
    elif source_type in {"BINARY_DOUBLE", "FLOAT"}:
        canonical = "double"
    elif source_type in {"CLOB", "NCLOB", "LONG"}:
        canonical = "text"
    elif source_type in {"BLOB", "RAW", "LONG RAW"}:
        canonical = "binary"
    else:
        raise UnmappedTypeError(
            table=table_name,
            column=column.name,
            source_type=source_type,
            source_engine="oracle",
            target_engine=target_engine,
        )

    size = int(column.size) if column.size else None
    precision = int(column.precision) if column.precision is not None else None
    scale = int(column.scale or 0)
    if target_engine == "postgresql":
        base = {
            "smallint": "smallint", "integer": "integer", "bigint": "bigint",
            "numeric": "numeric", "varchar": "varchar", "char": "char",
            "timestamp": "timestamp", "real": "real", "double": "double precision",
            "text": "text", "binary": "bytea",
        }[canonical]
        if canonical in {"varchar", "char"} and size:
            return f"{base}({size})"
        if canonical == "numeric" and precision:
            return f"numeric({precision},{scale})"
        return base
    if target_engine == "mysql":
        base = {
            "smallint": "SMALLINT", "integer": "INT", "bigint": "BIGINT",
            "numeric": "DECIMAL", "varchar": "VARCHAR", "char": "CHAR",
            "timestamp": "DATETIME", "real": "FLOAT", "double": "DOUBLE",
            "text": "LONGTEXT", "binary": "LONGBLOB",
        }[canonical]
        if canonical in {"varchar", "char"} and size:
            return f"{base}({size})"
        if canonical == "numeric" and precision:
            return f"DECIMAL({precision},{scale})"
        return base
    if target_engine == "mssql":
        base = {
            "smallint": "SMALLINT", "integer": "INT", "bigint": "BIGINT",
            "numeric": "DECIMAL", "varchar": "NVARCHAR", "char": "NCHAR",
            "timestamp": "DATETIME2", "real": "REAL", "double": "FLOAT",
            "text": "NVARCHAR(MAX)", "binary": "VARBINARY(MAX)",
        }[canonical]
        if canonical in {"varchar", "char"} and size:
            return f"{base}({size})"
        if canonical == "numeric" and precision:
            return f"DECIMAL({precision},{scale})"
        return base
    raise UnmappedTypeError(
        table=table_name,
        column=column.name,
        source_type=source_type,
        source_engine="oracle",
        target_engine=target_engine,
    )
