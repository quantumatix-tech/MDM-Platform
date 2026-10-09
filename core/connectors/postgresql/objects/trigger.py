"""PostgreSQL trigger object implementation.

Triggers are transported as the complete ``pg_get_triggerdef`` statement, so
timing (BEFORE/AFTER), event (INSERT/UPDATE/DELETE/TRUNCATE), level
(ROW/STATEMENT), the WHEN clause, the referenced function and the FOR EACH
ROW ordering are all preserved exactly as PostgreSQL decompiles them. This
module neither builds nor parses trigger bodies — it moves the ready-to-execute
definition produced on the source.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    TriggerDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.postgresql._models import _qualify


# ============================================================================
# SOURCE-SIDE TRIGGER OPERATIONS
# ============================================================================

def discover_triggers(conn: Any, schemas: tuple[str, ...]) -> list[TriggerDef]:
    """Return every user-defined trigger in the configured schemas.

    ``tgisinternal`` is filtered out so constraint-backed triggers that
    PostgreSQL generates for foreign keys and CHECK constraints are not
    migrated as standalone triggers — they are recreated with their
    constraints in the constraint phase.

    On failure the connection is rolled back before re-raising, so a discovery
    error cannot leave the source connection in a failed transaction for the
    later migration phases.
    """
    triggers: list[TriggerDef] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.tgname, c.relname, n.nspname, pg_get_triggerdef(t.oid) "
                "FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = ANY(%s) AND NOT t.tgisinternal "
                "ORDER BY c.relname, t.tgname",
                (list(schemas),),
            )
            for row in cur.fetchall():
                trig_name, table_name, schema_name, ddl = row
                triggers.append(TriggerDef(name=trig_name, table=table_name, schema_name=schema_name, ddl=ddl))
    except Exception:
        conn.rollback()   # keep source connection clean for subsequent phases
        raise
    return triggers


# ============================================================================
# TARGET-SIDE TRIGGER OPERATIONS
# ============================================================================

def create_trigger(conn: Any, trigger: TriggerDef) -> None:
    """Create a trigger on the target.

    An existing trigger of the same name is dropped first so that re-running a
    migration is idempotent — PostgreSQL has no ``CREATE OR REPLACE TRIGGER``.
    A failure is audited and swallowed.
    """
    validate_identifier(trigger.name, "trigger")
    trigger_schema = trigger.schema_name or "public"
    table_qname = _qualify(trigger_schema, trigger.table)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"DROP TRIGGER IF EXISTS {quote_identifier(trigger.name)} ON {table_qname}"
            )
            conn.commit()
            cur.execute(trigger.ddl)
            conn.commit()
            audit_log(phase="create_trigger", status="created",
                      details={"trigger": trigger.name, "table": table_qname})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_trigger", status="skipped",
                      details={"trigger": trigger.name, "reason": str(exc)})


def suspend_triggers_for_data_load(
    conn: Any, triggers: list[TriggerDef]
) -> list[TriggerDef]:
    """Disable matching existing user triggers before a FULL data reload.

    The orchestrator recreates source trigger definitions after loading. A
    target trigger left enabled during the reload would create side effects
    (for example, duplicate audit rows) on top of the source rows being copied.
    """
    suspended: list[TriggerDef] = []
    try:
        with conn.cursor() as cur:
            for trigger in triggers:
                validate_identifier(trigger.name, "trigger")
                validate_identifier(trigger.table, "table")
                schema = trigger.schema_name or "public"
                table_qname = _qualify(schema, trigger.table)
                cur.execute(
                    "SELECT t.tgenabled "
                    "FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s "
                    "AND t.tgname = %s AND NOT t.tgisinternal",
                    (schema, trigger.table, trigger.name),
                )
                row = cur.fetchone()
                if row is None or row[0] == "D":
                    continue
                cur.execute(
                    f"ALTER TABLE {table_qname} DISABLE TRIGGER "
                    f"{quote_identifier(trigger.name)}"
                )
                suspended.append(trigger)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if suspended:
        audit_log(
            phase="suspend_triggers",
            status="success",
            details={"triggers": [trigger.name for trigger in suspended]},
        )
    return suspended
