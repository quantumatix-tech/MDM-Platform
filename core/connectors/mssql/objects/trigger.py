"""MSSQL Trigger object implementation.

Reusable Trigger-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

import re
from typing import Any

from core.connectors.base import (
    TriggerDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _qualify,
    _resolve_mssql_schemas,
)
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE TRIGGER OPERATIONS
# Called/delegated by MSSQLSourceConnector.get_all_triggers.
# Discovery of user DML triggers from the source database.
# ============================================================================

def discover_triggers(conn: Any, config: dict[str, Any]) -> list[TriggerDef]:
    """Discover user DML triggers in the configured schemas.

    Replaces ``MSSQLSourceConnector.get_all_triggers``.

    SQL Server stores trigger definitions in ``sys.sql_modules`` and
    metadata (parent table, enabled state) in ``sys.triggers``. Only DML
    triggers (``type = 'TR'``) are migrated — DDL triggers (``type = 'TA'``)
    are server-scoped and skipped.
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[TriggerDef] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT OBJECT_SCHEMA_NAME(t.object_id), "
                "t.name, OBJECT_SCHEMA_NAME(p.object_id), p.name, "
                "CAST(m.definition AS NVARCHAR(MAX)) AS definition, "
                "t.is_disabled "
                "FROM sys.triggers t "
                "JOIN sys.objects p ON t.parent_id = p.object_id "
                "JOIN sys.sql_modules m ON t.object_id = m.object_id "
                f"WHERE OBJECT_SCHEMA_NAME(t.object_id) IN ({placeholders}) "
                "AND t.type = 'TR' "
                "ORDER BY OBJECT_SCHEMA_NAME(t.object_id), p.name, t.name",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT OBJECT_SCHEMA_NAME(t.object_id), "
                "t.name, OBJECT_SCHEMA_NAME(p.object_id), p.name, "
                "CAST(m.definition AS NVARCHAR(MAX)) AS definition, "
                "t.is_disabled "
                "FROM sys.triggers t "
                "JOIN sys.objects p ON t.parent_id = p.object_id "
                "JOIN sys.sql_modules m ON t.object_id = m.object_id "
                "WHERE OBJECT_SCHEMA_NAME(t.object_id) NOT IN "
                "('sys', 'INFORMATION_SCHEMA', 'guest') "
                "AND t.type = 'TR' "
                "ORDER BY OBJECT_SCHEMA_NAME(t.object_id), p.name, t.name",
            )
        for schema_name, trig_name, table_schema, table_name, definition, is_disabled in cur.fetchall():
            validate_identifier(trig_name, "trigger")
            validate_identifier(schema_name, "schema")
            results.append(
                TriggerDef(
                    name=trig_name,
                    table=table_name,
                    schema_name=schema_name,
                    table_schema=table_schema or schema_name,
                    ddl=definition,
                    is_disabled=bool(is_disabled),
                )
            )
    return results


# ============================================================================
# INTERNAL TRIGGER HELPERS
# Used internally by the target-side trigger creation logic below.
# ============================================================================

def _normalize_trigger_ddl(ddl: str, schema_name: str, trigger_name: str) -> str:
    """Rewrite trigger DDL for CREATE OR ALTER with schema qualification.

    - ``CREATE TRIGGER <oldname>`` → ``CREATE OR ALTER TRIGGER [schema].[name]``
    - If no ``CREATE TRIGGER`` prefix is found but the DDL starts with
      ``CREATE``, prepend ``OR ALTER``.

    Returns the rewritten DDL string. This is the exact DDL-rewriting logic
    previously inlined in ``MSSQLTargetConnector.create_trigger``.
    """
    qualified_trigger = f"[{schema_name}].[{trigger_name}]"
    new_ddl, n = re.subn(
        r"CREATE\s+TRIGGER\s+\S+",
        f"CREATE OR ALTER TRIGGER {qualified_trigger}",
        ddl,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        if new_ddl.upper().startswith("CREATE "):
            new_ddl = "CREATE OR ALTER " + new_ddl[len("CREATE "):]
    return new_ddl


# ============================================================================
# TARGET-SIDE TRIGGER OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_trigger.
# Trigger creation on the target database.
# ============================================================================

def create_trigger(conn: Any, trigger: TriggerDef) -> None:
    """Create or alter a trigger on the target database.

    Replaces ``MSSQLTargetConnector.create_trigger``.

    Uses ``CREATE OR ALTER TRIGGER`` (SQL Server 2016+ SP1) for idempotency.
    The DDL from ``sys.sql_modules`` is rewritten via ``_normalize_trigger_ddl``
    so the trigger name is schema-qualified (``[schema].[name]``) — the
    original text may use an unqualified name that would resolve to the
    wrong schema on the target.

    The enabled/disabled state is re-applied after creation so the target
    matches the source regardless of whether ``CREATE OR ALTER`` preserved
    a pre-existing state.

    Supports cross-schema triggers via ``trigger.table_schema``.

    All operations (schema creation, table existence check, trigger DDL,
    enable/disable) run in a single transaction: schema creation is not
    committed separately (unlike ``create_view``) because CREATE TRIGGER
    does not need to be the first statement in its batch.
    """
    validate_identifier(trigger.name, "trigger")
    schema_name = trigger.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    trigger_qname = f"[{schema_name}].[{trigger.name}]"
    table_schema = trigger.table_schema or schema_name
    table_qname = _qualify(table_schema, trigger.table)

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

        # Ensure the target table exists — a trigger cannot be created on
        # a missing parent table.
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
            (trigger.table, table_schema),
        )
        if cur.fetchone() is None:
            audit_log(
                phase="create_trigger",
                status="skipped",
                details={
                    "trigger": trigger.name,
                    "reason": f"parent table {table_qname} not found",
                },
            )
            return

        try:
            ddl = _normalize_trigger_ddl(trigger.ddl, schema_name, trigger.name)
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_trigger",
                status="created",
                details={
                    "trigger": trigger_qname,
                    "table": table_qname,
                    "disabled": trigger.is_disabled,
                },
            )

            # Re-apply enabled/disabled state to match the source.
            if trigger.is_disabled:
                cur.execute(
                    f"ALTER TABLE {table_qname} DISABLE TRIGGER {quote_identifier(trigger.name)}"
                )
            else:
                cur.execute(
                    f"ALTER TABLE {table_qname} ENABLE TRIGGER {quote_identifier(trigger.name)}"
                )
            conn.commit()
            audit_log(
                phase="create_trigger",
                status="applied_state",
                details={"trigger": trigger_qname, "disabled": trigger.is_disabled},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_trigger",
                status="failed",
                details={
                    "trigger": trigger.name,
                    "schema": schema_name,
                    "reason": str(exc),
                },
            )
            raise
