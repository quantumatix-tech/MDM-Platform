"""MSSQL View object implementation.

Reusable View-specific logic for discovery and creation. Functions accept
an explicit *conn* parameter so that neither ``MSSQLSourceConnector`` nor
``MSSQLTargetConnector`` is imported, avoiding circular dependencies.
"""
from __future__ import annotations

import re
from typing import Any

from core.connectors.base import (
    ViewDefinition,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import _resolve_mssql_schemas
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE VIEW OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_views.
# ============================================================================

def discover_views(conn: Any, config: dict[str, Any]) -> list[ViewDefinition]:
    """Discover user views in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_views``.

    Reads ``INFORMATION_SCHEMA.VIEWS`` (excludes system schemas). The
    ``VIEW_DEFINITION`` column is ``NVARCHAR(MAX)`` and pyodbc can choke on
    it when fetched together with other columns, so it is CAST explicitly.
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[ViewDefinition] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT TABLE_NAME, TABLE_SCHEMA, "
                "CAST(VIEW_DEFINITION AS NVARCHAR(MAX)) AS VIEW_DEFINITION "
                "FROM INFORMATION_SCHEMA.VIEWS "
                f"WHERE TABLE_SCHEMA IN ({placeholders}) "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_SCHEMA, "
                "CAST(VIEW_DEFINITION AS NVARCHAR(MAX)) AS VIEW_DEFINITION "
                "FROM INFORMATION_SCHEMA.VIEWS "
                "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
        for row in cur.fetchall():
            view_name, view_schema, view_def = row
            validate_identifier(view_name, "view")
            validate_identifier(view_schema, "schema")
            results.append(
                ViewDefinition(
                    name=view_name,
                    schema_name=view_schema,
                    definition=view_def,
                )
            )
    return results


# ============================================================================
# TARGET-SIDE VIEW OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_view.
# ============================================================================

def create_view(conn: Any, view: ViewDefinition) -> None:
    """Create or alter a view on the target database.

    Replaces ``MSSQLTargetConnector.create_view``.

    Ensures the target schema exists (dbo is always present), then executes
    ``CREATE OR ALTER VIEW`` in a separate cursor/batch after any schema
    creation commit (CREATE VIEW must be the first statement in its batch).

    The view definition is normalised: if it starts with
    ``CREATE VIEW <name> AS``, the prefix is stripped to avoid a duplicate
    view qualification (e.g.
    ``CREATE OR ALTER VIEW [s].[v] AS s.v AS SELECT ...`` triggers SQL
    Server error 102).
    """
    validate_identifier(view.name, "view")
    schema_name = view.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    view_qname = f"[{schema_name or 'dbo'}].[{view.name}]"

    with conn.cursor() as cur:
        # Ensure the target schema exists (dbo always exists in SQL Server).
        if schema_name != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": schema_name},
                )
                conn.commit()

    # CREATE VIEW must be the first statement in a batch.
    # Execute in a separate cursor/batch after schema creation commit.
    with conn.cursor() as cur:
        try:
            definition = view.definition
            if definition.upper().startswith("CREATE VIEW"):
                definition = definition[len("CREATE VIEW"):].lstrip()
                # SQL Server returns the definition with its original line
                # breaks (``name\r\nAS\r\nSELECT``), so the separator cannot be
                # matched as a literal " AS ".  Require surrounding whitespace to
                # avoid matching inside an identifier such as ``vw_OrderASAP``.
                as_match = re.search(r"\s+AS\s+", definition, re.IGNORECASE)
                if as_match:
                    definition = definition[as_match.end():].lstrip()
            cur.execute(
                f"CREATE OR ALTER VIEW {view_qname} AS {definition}"
            )
            conn.commit()
            audit_log(
                phase="create_view",
                status="created",
                details={"view": view.name},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_view",
                status="failed",
                details={"view": view.name, "reason": str(exc)},
            )
            raise
