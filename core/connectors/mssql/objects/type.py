"""MSSQL Type object implementation.

Reusable Type-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    TypeDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _mssql_column_type,
    _resolve_mssql_schemas,
)
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE TYPE OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_types.
# Discovery of user-defined (alias) data types from the source database.
# ============================================================================

def discover_types(conn: Any, config: dict[str, Any]) -> list[TypeDef]:
    """Discover user-defined (alias) data types in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_types``.

    SQL Server stores alias/user-defined types in sys.types with
    is_user_defined = 1. INFORMATION_SCHEMA.COLUMNS only reports the base
    DATA_TYPE for columns that use such a type, so these are discovered here
    (and re-applied to columns in get_schema) to preserve schema-qualified
    UDT references on the target.
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[TypeDef] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT s.name, t.name, t.is_nullable, t.max_length, "
                "TRY_CAST(t.precision AS INT), TRY_CAST(t.scale AS INT), "
                "TYPE_NAME(t.system_type_id) AS base_name "
                "FROM sys.types t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                f"WHERE s.name IN ({placeholders}) AND t.is_user_defined = 1 "
                "ORDER BY s.name, t.name",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT s.name, t.name, t.is_nullable, t.max_length, "
                "TRY_CAST(t.precision AS INT), TRY_CAST(t.scale AS INT), "
                "TYPE_NAME(t.system_type_id) AS base_name "
                "FROM sys.types t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "AND t.is_user_defined = 1 "
                "ORDER BY s.name, t.name"
            )
        for row in cur.fetchall():
            type_schema, type_name, is_nullable, max_len, precision, scale, base_name = row
            validate_identifier(type_name, "type")
            validate_identifier(type_schema, "schema")
            base_type = _mssql_column_type(base_name, max_len, precision, scale)
            null_str = "NULL" if is_nullable else "NOT NULL"
            ddl = (
                f"CREATE TYPE [{type_schema}].[{type_name}] "
                f"FROM {base_type} {null_str}"
            )
            results.append(
                TypeDef(
                    name=f"{type_schema}.{type_name}",
                    kind="alias",
                    ddl=ddl,
                )
            )
    return results


# ============================================================================
# TARGET-SIDE TYPE OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_type.
# Type creation on the target database.
# ============================================================================

def create_type(conn: Any, type_def: TypeDef) -> None:
    """Create a user-defined (alias) data type on the target.

    Replaces ``MSSQLTargetConnector.create_type``.

    SQL Server has no ``CREATE OR ALTER TYPE``; re-runs are made idempotent
    by checking sys.types first. CREATE TYPE must be the sole statement in
    its batch, so it is executed on its own cursor.execute().
    """
    # TypeDef.name is schema-qualified ("schema.type") for MSSQL UDTs.
    name = type_def.name
    if "." in name:
        type_schema, type_name = name.split(".", 1)
    else:
        type_schema, type_name = "dbo", name
    validate_identifier(type_name, "type")
    validate_identifier(type_schema, "schema")

    with conn.cursor() as cur:
        if type_schema != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (type_schema,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(type_schema)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": type_schema},
                )

        cur.execute(
            "SELECT 1 FROM sys.types "
            "WHERE is_user_defined = 1 "
            "AND name = ? AND schema_id = SCHEMA_ID(?)",
            (type_name, type_schema),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_type", status="exists",
                details={"type": f"{type_schema}.{type_name}"},
            )
            return

        try:
            cur.execute(type_def.ddl)
            conn.commit()
            audit_log(
                phase="create_type",
                status="created",
                details={"type": f"{type_schema}.{type_name}", "kind": type_def.kind},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_type", status="failed",
                details={"type": f"{type_schema}.{type_name}", "reason": str(exc)},
            )
            raise
