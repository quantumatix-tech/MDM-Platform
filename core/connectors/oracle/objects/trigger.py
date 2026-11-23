"""Oracle row/table trigger discovery and application."""

from __future__ import annotations

from typing import Any

from core.connectors.base import TriggerDef, quote_identifier
from core.connectors.oracle._models import validate_oracle_identifier
from core.connectors.oracle.objects._common import (
    _assert_target_type,
    _check_compile_status,
    _check_local_dependencies,
    _get_ddl,
    _remap_owner_qualifiers,
    _strip_trigger_metadata_state,
    _validate_single_create,
)


def discover_triggers(
    conn: Any, owner: str, selected_tables: set[str] | None = None
) -> list[TriggerDef]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trigger_name, table_name, status, trigger_type "
            "FROM user_triggers ORDER BY table_name, trigger_name"
        )
        rows = cur.fetchall()
    result = []
    for name, table, status, trigger_type in rows:
        # Scope ordinary table triggers before validation and metadata reads;
        # unselected tables do not participate in this FULL migration.
        if (
            selected_tables is not None
            and table is not None
            and str(table).upper() not in selected_tables
        ):
            continue
        name = str(name)
        if table is None or "EVENT" in str(trigger_type).upper():
            raise RuntimeError(
                f"Oracle database-level trigger {owner}.{name} is unsupported by table FULL migration"
            )
        table = str(table)
        validate_oracle_identifier(name, "trigger")
        validate_oracle_identifier(table, "trigger table")
        if str(trigger_type).upper().startswith("INSTEAD OF"):
            raise RuntimeError(
                f"Oracle INSTEAD OF trigger {owner}.{name} is unsupported by table FULL migration"
            )
        _check_local_dependencies(conn, owner, name, "TRIGGER")
        result.append(
            TriggerDef(
                name=name,
                table=table,
                schema_name=owner,
                ddl=_get_ddl(conn, "TRIGGER", name, owner),
                is_disabled=str(status).upper() == "DISABLED",
            )
        )
    return result


def create_trigger(
    conn: Any,
    trigger: TriggerDef,
    target_owner: str | None = None,
    managed_tables: set[str] | None = None,
) -> None:
    if managed_tables is not None and trigger.table.casefold() not in {
        table.casefold() for table in managed_tables
    }:
        raise RuntimeError(
            f"Oracle trigger {trigger.name} references {trigger.table}, which is not a source-managed table"
        )
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM user_tables WHERE table_name = :1", (trigger.table,))
        if cur.fetchone() is None:
            raise RuntimeError(
                f"Oracle trigger {trigger.name} parent table {trigger.table} is missing on target"
            )
    _assert_target_type(conn, trigger.name, "TRIGGER")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM user_triggers WHERE trigger_name = :1",
            (trigger.name,),
        )
        existing = cur.fetchone()
    if existing is not None and str(existing[0]) != trigger.table:
        raise RuntimeError(
            f"Target trigger {trigger.name} belongs to {existing[0]}, source trigger belongs to {trigger.table}"
        )
    # Create disabled first. The orchestrator invokes this during preflight,
    # before data clearing, then reapplies the source state after loading.
    ddl = _strip_trigger_metadata_state(trigger.ddl, trigger.name, trigger.schema_name)
    ddl = _remap_owner_qualifiers(ddl, trigger.schema_name, target_owner)
    ddl = _validate_single_create(ddl, "TRIGGER", trigger.name)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    _check_compile_status(conn, trigger.name, ("TRIGGER",))
    # Apply state only after Oracle has compiled the trigger. Keep the
    # migration-time disabled window explicit, then restore the source state.
    with conn.cursor() as cur:
        cur.execute(f"ALTER TRIGGER {quote_identifier(trigger.name)} DISABLE")
    conn.commit()
    if not trigger.is_disabled:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TRIGGER {quote_identifier(trigger.name)} ENABLE")
            cur.execute(
                "SELECT status FROM user_triggers WHERE trigger_name = :1",
                (trigger.name,),
            )
            state = cur.fetchone()
            if state is None or str(state[0]).upper() != "ENABLED":
                raise RuntimeError(f"Oracle trigger {trigger.name} did not become ENABLED")
        conn.commit()
    else:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM user_triggers WHERE trigger_name = :1",
                (trigger.name,),
            )
            state = cur.fetchone()
            if state is None or str(state[0]).upper() != "DISABLED":
                raise RuntimeError(f"Oracle trigger {trigger.name} did not remain DISABLED")


def suspend_triggers_for_data_load(conn: Any, triggers: list[TriggerDef]) -> list[TriggerDef]:
    suspended = []
    with conn.cursor() as cur:
        for trigger in triggers:
            cur.execute(
                "SELECT table_name, status FROM user_triggers WHERE trigger_name = :1",
                (trigger.name,),
            )
            row = cur.fetchone()
            if row is None:
                continue
            if str(row[0]) != trigger.table:
                raise RuntimeError(
                    f"Target trigger {trigger.name} belongs to {row[0]}, source trigger belongs to {trigger.table}"
                )
            if str(row[1]).upper() == "ENABLED":
                cur.execute(f"ALTER TRIGGER {quote_identifier(trigger.name)} DISABLE")
                suspended.append(trigger)
    conn.commit()
    return suspended
