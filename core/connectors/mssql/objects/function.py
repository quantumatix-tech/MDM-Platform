"""MSSQL Function object implementation.

Reusable Function-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    FunctionDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _resolve_mssql_schemas,
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
                "SELECT s.name, o.name, m.definition "
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
                "SELECT s.name, o.name, m.definition "
                "FROM sys.objects o "
                "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                "JOIN sys.sql_modules m ON o.object_id = m.object_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "AND o.type IN ('FN', 'TF', 'IF', 'P') "
                "ORDER BY s.name, o.name"
            )
        for schema_name, obj_name, definition in cur.fetchall():
            validate_identifier(obj_name, "function")
            validate_identifier(schema_name, "schema")
            results.append(
                FunctionDef(
                    name=obj_name,
                    schema_name=schema_name,
                    ddl=definition,
                )
            )
    return results


# ============================================================================
# TARGET-SIDE FUNCTION OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_function.
# Function creation on the target database.
# ============================================================================

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
            ddl = func.ddl
            # The DDL from sys.sql_modules.definition already includes
            # "CREATE FUNCTION" or "CREATE PROCEDURE"; replace with CREATE OR ALTER
            # for idempotency across re-runs.
            if ddl.upper().startswith("CREATE "):
                ddl = "CREATE OR ALTER " + ddl[len("CREATE "):]
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
