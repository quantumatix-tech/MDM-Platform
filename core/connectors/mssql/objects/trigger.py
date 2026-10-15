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
    _read_mssql_qualified_identifier,
    _resolve_mssql_schemas,
    _skip_mssql_sql_trivia,
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

def _normalize_trigger_ddl(
    ddl: str,
    schema_name: str,
    trigger_name: str,
    table_schema: str,
    table_name: str,
) -> str:
    """Rewrite trigger DDL for CREATE OR ALTER with schema qualification.

    Rewrites both the trigger header and parent table reference using catalog
    names, independent of the qualification and quoting in source module text.

    Returns the rewritten DDL string. This is the exact DDL-rewriting logic
    previously inlined in ``MSSQLTargetConnector.create_trigger``.
    """
    qualified_trigger = f"[{schema_name}].[{trigger_name}]"
    header_start = _skip_mssql_sql_trivia(ddl)
    header = re.match(
        r"CREATE\s+(?:(?:OR\s+ALTER|OR\s+REPLACE)\s+)?TRIGGER\b",
        ddl[header_start:],
        flags=re.IGNORECASE,
    )
    trigger_name_start = (
        _skip_mssql_sql_trivia(ddl, header_start + header.end())
        if header else len(ddl)
    )
    trigger_identifier = (
        _read_mssql_qualified_identifier(ddl, trigger_name_start) if header else None
    )
    if trigger_identifier is None:
        raise ValueError(
            f"Expected CREATE TRIGGER DDL for {schema_name}.{trigger_name}; "
            "refusing to execute unrecognized module text"
        )
    header_end, _ = trigger_identifier

    on_start = _skip_mssql_sql_trivia(ddl, header_end)
    on_keyword = re.match(r"ON\b", ddl[on_start:], flags=re.IGNORECASE)
    table_start = _skip_mssql_sql_trivia(
        ddl, on_start + on_keyword.end()
    ) if on_keyword else len(ddl)
    table_identifier = (
        _read_mssql_qualified_identifier(ddl, table_start, max_parts=3)
        if on_keyword else None
    )
    if table_identifier is None:
        raise ValueError(
            f"Expected ON table reference for trigger {schema_name}.{trigger_name}; "
            "refusing to execute unrecognized module text"
        )
    table_end, _ = table_identifier
    table_qname = f"[{table_schema}].[{table_name}]"
    return (
        ddl[:header_start]
        + f"CREATE OR ALTER TRIGGER {qualified_trigger}"
        + ddl[header_end:on_start]
        + ddl[on_start:table_start]
        + table_qname
        + ddl[table_end:]
    )


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
    validate_identifier(table_schema, "schema")
    validate_identifier(trigger.table, "table")
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
            return False

        try:
            ddl = _normalize_trigger_ddl(
                trigger.ddl, schema_name, trigger.name, table_schema, trigger.table
            )
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
            return True
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


def suspend_triggers_for_data_load(
    conn: Any, triggers: list[TriggerDef]
) -> list[TriggerDef]:
    """Disable matching enabled target triggers and snapshot their state.

    Trigger definitions remain in place in SQL Server. Only source-managed
    trigger names are considered, and a trigger disabled before the run is
    left disabled. ``create_trigger`` later installs the source definition and
    source enabled state on successful migrations.
    """
    suspended: list[TriggerDef] = []
    try:
        with conn.cursor() as cur:
            for trigger in triggers:
                validate_identifier(trigger.name, "trigger")
                validate_identifier(trigger.table, "table")
                schema = trigger.schema_name or "dbo"
                table_schema = trigger.table_schema or schema
                validate_identifier(schema, "schema")
                validate_identifier(table_schema, "schema")
                table_qname = _qualify(table_schema, trigger.table)
                cur.execute(
                    "SELECT t.is_disabled FROM sys.triggers t "
                    "JOIN sys.objects p ON p.object_id = t.parent_id "
                    "JOIN sys.schemas s ON s.schema_id = p.schema_id "
                    "WHERE s.name = ? AND p.name = ? AND t.name = ? AND t.type = 'TR'",
                    (table_schema, trigger.table, trigger.name),
                )
                row = cur.fetchone()
                if row is None or bool(row[0]):
                    continue
                cur.execute(
                    f"DISABLE TRIGGER {quote_identifier(trigger.name)} ON {table_qname}"
                )
                suspended.append(
                    TriggerDef(
                        name=trigger.name,
                        table=trigger.table,
                        ddl=trigger.ddl,
                        schema_name=schema,
                        table_schema=table_schema,
                        is_disabled=bool(row[0]),
                    )
                )
        conn.commit()
    except Exception:
        conn.rollback()
        if suspended:
            restore_suspended_triggers(conn, suspended)
        raise

    if suspended:
        audit_log(
            phase="suspend_triggers",
            status="success",
            details={"triggers": [trigger.name for trigger in suspended]},
        )
    return suspended


def restore_suspended_triggers(conn: Any, triggers: list[TriggerDef]) -> None:
    """Re-enable triggers suspended by this FULL run after an early failure."""
    try:
        with conn.cursor() as cur:
            for trigger in triggers:
                table_schema = trigger.table_schema or trigger.schema_name or "dbo"
                table_qname = _qualify(table_schema, trigger.table)
                cur.execute(
                    f"ENABLE TRIGGER {quote_identifier(trigger.name)} ON {table_qname}"
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
