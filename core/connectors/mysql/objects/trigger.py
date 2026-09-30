"""MySQL trigger object operations.

Owns trigger discovery, trigger creation and the pre-data-load trigger
suspension snapshot. This module must not import ``source``, ``target`` or
``cdc``: it depends only on ``_models``, the cross-engine DTOs and the
standard library.

DEFINER rewriting is deliberately *not* implemented here. It already lives
in ``core.connectors.mysql._models`` as ``_rewrite_mysql_definer`` and is
supplied by the caller as the ``rewrite_definer`` callable, so the original
identity handling, the fallback behaviour and the SQL rewriting are
unchanged.

MySQL has no ``DISABLE TRIGGER`` statement, so suspension is a
snapshot/drop of the requested triggers only; the caller is responsible for
recreating them and for preserving any connector state, exactly as before.

Every SQL statement, parameter order, commit/rollback boundary, audit call
and the discovery/drop ordering of the former
``MySQLSourceConnector.get_all_triggers``,
``MySQLTargetConnector.create_trigger`` and
``MySQLTargetConnector.suspend_triggers_for_data_load`` implementations is
carried over unchanged.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.connectors.base import TriggerDef
from core.audit_logger import audit_log

from core.connectors.mysql._models import (
    _mysql_routine_policy_error,
    _q,
)


def get_all_triggers(
    conn: Any,
    database: str,
    show_create: Callable[[str, str], str],
) -> list[TriggerDef]:
    """Discover every trigger defined in ``database``, in catalog order."""
    with conn.cursor() as cur:
        cur.execute("SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=%s", (database,))
        return [TriggerDef(name=n, table=t, ddl=show_create("TRIGGER", n), schema_name=database) for n, t in cur.fetchall()]


def create_trigger(
    conn: Any,
    trigger: TriggerDef,
    rewrite_definer: Callable[[str], str],
) -> None:
    """Create ``trigger`` on the target, dropping any existing trigger first.

    The trigger DDL has its DEFINER rewritten by the caller's
    ``rewrite_definer`` before execution; a routine creation policy failure
    raised by the server is translated into
    :class:`MySQLRoutineCreationPolicyError` and re-raised from the
    original error.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TRIGGER IF EXISTS {_q(trigger.name)}")
            cur.execute(rewrite_definer(trigger.ddl))
            conn.commit()
    except Exception as exc:
        conn.rollback()
        policy_error = _mysql_routine_policy_error(exc, "TRIGGER")
        if policy_error is not None:
            raise policy_error from exc
        raise


def suspend_triggers_for_data_load(
    conn: Any,
    database: str,
    triggers: list[TriggerDef],
    show_create: Callable[[str, str], str],
) -> list[TriggerDef]:
    """Snapshot and drop the requested triggers ahead of a data load.

    MySQL lacks DISABLE TRIGGER; snapshot/drop only matching triggers.

    The orchestration recreates source definitions after the load. Returning
    the target definitions provides an audit trail and permits callers to
    restore them if later trigger application is unavailable.
    """
    requested = {trigger.name for trigger in triggers}
    suspended: list[TriggerDef] = []
    with conn.cursor() as cur:
        cur.execute("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=%s", (database,))
        for name, table in cur.fetchall():
            if name not in requested:
                continue
            ddl = show_create("TRIGGER", name)
            cur.execute(f"DROP TRIGGER {_q(name)}")
            suspended.append(TriggerDef(name=name, table=table, ddl=ddl, schema_name=database))
        conn.commit()
    if suspended:
        audit_log(phase="suspend_triggers", status="success", details={"triggers": [t.name for t in suspended]})
    return suspended
