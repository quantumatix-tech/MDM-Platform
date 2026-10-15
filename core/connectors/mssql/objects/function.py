"""MSSQL Function object implementation.

Reusable Function-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

import re
from typing import Any

from core.connectors.base import (
    FunctionDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _resolve_mssql_schemas,
    _read_mssql_qualified_identifier,
    _skip_mssql_sql_trivia,
)
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE FUNCTION OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_functions.
# Discovery of user functions and stored procedures from the source database.
# ============================================================================

def discover_functions(conn: Any, config: dict[str, Any]) -> list[FunctionDef]:
    """Discover user functions and stored procedures in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_functions``.

    SQL Server stores the full CREATE definition in ``sys.sql_modules.definition``
    (types: 'FN' scalar, 'TF' table-valued, 'IF' inline table-valued, 'P' procedure).
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[FunctionDef] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT s.name, o.name, m.definition, o.type "
                "FROM sys.objects o "
                "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                "JOIN sys.sql_modules m ON o.object_id = m.object_id "
                f"WHERE s.name IN ({placeholders}) "
                "AND o.type IN ('FN', 'TF', 'IF', 'P') "
                "ORDER BY s.name, o.name",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT s.name, o.name, m.definition, o.type "
                "FROM sys.objects o "
                "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                "JOIN sys.sql_modules m ON o.object_id = m.object_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "AND o.type IN ('FN', 'TF', 'IF', 'P') "
                "ORDER BY s.name, o.name"
            )
        for schema_name, obj_name, definition, object_type in cur.fetchall():
            validate_identifier(obj_name, "function")
            validate_identifier(schema_name, "schema")
            object_type_code = str(object_type or "").strip().upper()
            results.append(
                FunctionDef(
                    name=obj_name,
                    schema_name=schema_name,
                    ddl=definition,
                    kind="procedure" if object_type_code == "P" else "function",
                )
            )
    return results


# ============================================================================
# TARGET-SIDE FUNCTION OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_function.
# Function creation on the target database.
# ============================================================================

_ROUTINE_TYPES = {
    "function": "FUNCTION",
    "procedure": "PROCEDURE",
    "p": "PROCEDURE",
    "fn": "FUNCTION",
    "if": "FUNCTION",
    "tf": "FUNCTION",
}


def _normalize_routine_ddl(ddl: str, schema_name: str, name: str, kind: str) -> str:
    """Return CREATE OR ALTER DDL bound to the catalogued schema and object name."""
    object_type = _ROUTINE_TYPES.get((kind or "function").strip().lower())
    if object_type is None:
        raise ValueError(f"Unsupported MSSQL routine type: {kind!r}")
    qualified_name = f"[{schema_name}].[{name}]"
    header_start = _skip_mssql_sql_trivia(ddl)
    header = re.match(
        r"CREATE\s+(?:(?:OR\s+ALTER|OR\s+REPLACE)\s+)?(FUNCTION|PROCEDURE|PROC)\b",
        ddl[header_start:],
        flags=re.IGNORECASE,
    )
    expected_type = "PROCEDURE" if object_type == "PROCEDURE" else "FUNCTION"
    header_type = header.group(1).upper() if header else None
    observed_type = "PROCEDURE" if header_type in {"PROCEDURE", "PROC"} else header_type
    name_start = (
        _skip_mssql_sql_trivia(ddl, header_start + header.end())
        if header else len(ddl)
    )
    qualified_identifier = (
        _read_mssql_qualified_identifier(ddl, name_start)
        if observed_type == expected_type else None
    )
    if qualified_identifier is None:
        raise ValueError(
            f"Expected CREATE {expected_type} DDL for {schema_name}.{name}; "
            "refusing to execute unrecognized module text"
        )
    header_end, _ = qualified_identifier
    return (
        ddl[:header_start]
        + f"CREATE OR ALTER {expected_type} {qualified_name}"
        + ddl[header_end:]
    )

def create_function(conn: Any, func: FunctionDef) -> None:
    """Create or alter a function on the target database.

    Replaces ``MSSQLTargetConnector.create_function``.

    Uses ``CREATE OR ALTER`` (SQL Server 2016+ SP1) for idempotency.
    The DDL from ``sys.sql_modules.definition`` already includes
    ``CREATE FUNCTION`` or ``CREATE PROCEDURE``; the leading ``CREATE``
    is replaced with ``CREATE OR ALTER`` for idempotency across re-runs.

    Schema creation is not committed separately: CREATE SCHEMA is not
    the start of a new batch and can be followed by additional statements
    in the same transaction.
    """
    validate_identifier(func.name, "function")
    schema_name = func.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    with conn.cursor() as cur:
        # Ensure the target schema exists (dbo always exists in SQL Server).
        if schema_name != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                audit_log(
                    phase="create_schema", status="created",
                    details={"schema": schema_name},
                )
        try:
            cur.execute(
                "SELECT o.type_desc FROM sys.objects o "
                "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                "WHERE s.name = ? AND o.name = ?",
                (schema_name, func.name),
            )
            existing = cur.fetchone()
            expected_type = "SQL_STORED_PROCEDURE" if func.kind.lower() == "procedure" else None
            if existing is not None:
                target_type = str(existing[0]).upper()
                if expected_type is None:
                    compatible = target_type in {
                        "SQL_SCALAR_FUNCTION", "SQL_TABLE_VALUED_FUNCTION",
                        "SQL_INLINE_TABLE_VALUED_FUNCTION", "CLR_SCALAR_FUNCTION",
                        "CLR_TABLE_VALUED_FUNCTION",
                    }
                    source_type = "FUNCTION"
                else:
                    compatible = target_type == expected_type
                    source_type = "PROCEDURE"
                if not compatible:
                    message = (
                        f"Cannot reconcile source {source_type} {schema_name}.{func.name}: "
                        f"target contains {target_type}. The target object was preserved. "
                        "Remove it explicitly after checking dependencies, then retry the migration."
                    )
                    audit_log(
                        phase="create_function", status="conflict",
                        details={"object": f"{schema_name}.{func.name}", "source_type": source_type,
                                 "target_type": target_type, "action": "preserved; manual dependency review required"},
                    )
                    raise RuntimeError(message)
            ddl = _normalize_routine_ddl(
                func.ddl, schema_name, func.name, func.kind
            )
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_function", status="created",
                details={"function": func.name, "schema": schema_name},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_function", status="failed",
                details={"function": func.name, "schema": schema_name, "reason": str(exc)},
            )
            raise
