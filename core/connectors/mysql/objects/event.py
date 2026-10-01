"""MySQL event object operations.

Owns event discovery and event creation. This module must not import
``source``, ``target`` or ``cdc``: it depends only on ``_models``, the
cross-engine DTOs and the standard library.

DEFINER rewriting is deliberately *not* implemented here. It already lives
in ``core.connectors.mysql._models`` as ``_rewrite_mysql_definer`` and is
supplied by the caller as the ``rewrite_definer`` callable, so the
connector's DEFINER state (``_routine_definer``,
``_preserve_source_definer``, ``_available_definer_accounts`` and
``_can_set_any_definer``) stays private to ``target``.

The two-stage timing safety check is event-specific, so it lives here rather
than in ``_models``: the first stage compares the event against the source
snapshot taken by :func:`list_events`, the second compares it against the
target's current clock after the session time zone has been aligned. Both
stages raise :class:`MySQLEventTimingSafetyError` with their original,
distinct messages.

Every SQL statement, parameter order, commit/rollback boundary, session time
zone save/restore and the discovery ordering of the former
``MySQLSourceConnector.list_events`` and ``MySQLTargetConnector.create_event``
implementations is carried over unchanged.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from core.connectors.base import EventDef

from core.connectors.mysql._models import (
    MySQLEventTimingSafetyError,
    _q,
)


def list_events(
    conn: Any,
    database: str,
    show_create: Callable[[str, str], str],
) -> list[EventDef]:
    """Discover every event defined in ``database``, in catalog order.

    The target ``NOW()`` is read first and attached to every discovered event
    as ``snapshot_at``, so a one-time event cannot be rediscovered after its
    schedule passes.
    """
    with conn.cursor() as cur:
        # Snapshot event metadata at migration start.  In particular, a
        # one-time event must not be rediscovered after its schedule passes.
        cur.execute("SELECT NOW()")
        snapshot_at = cur.fetchone()[0]
        cur.execute(
            "SELECT EVENT_NAME,EVENT_TYPE,STATUS,EXECUTE_AT,INTERVAL_VALUE,"
            "INTERVAL_FIELD,STARTS,ENDS,ON_COMPLETION,TIME_ZONE,DEFINER "
            "FROM INFORMATION_SCHEMA.EVENTS WHERE EVENT_SCHEMA=%s",
            (database,),
        )
        rows = cur.fetchall()
    return [
        EventDef(
            name=name,
            ddl=show_create("EVENT", name),
            schema_name=database,
            event_type=event_type,
            status=status,
            execute_at=execute_at,
            interval_value=str(interval_value) if interval_value is not None else None,
            interval_field=interval_field,
            starts=starts,
            ends=ends,
            on_completion=on_completion,
            time_zone=time_zone,
            definer=definer,
            snapshot_at=snapshot_at,
        )
        for (
            name, event_type, status, execute_at, interval_value,
            interval_field, starts, ends, on_completion, time_zone, definer,
        ) in rows
    ]


def create_event(
    conn: Any,
    event: EventDef,
    rewrite_definer: Callable[[str], str],
) -> None:
    """Create ``event`` on the target, dropping any existing event first.

    ``CREATE EVENT`` interprets literal schedule values in the session time
    zone, so the source event's recorded time zone is applied first and the
    original session time zone is always restored afterwards, including when
    a timing safety check aborts the creation.
    """
    safety_lead_seconds = event.safety_lead_seconds
    if (
        event.event_type == "ONE TIME"
        and event.status == "ENABLED"
        and event.execute_at is not None
    ):
        snapshot_deadline = event.snapshot_at + timedelta(seconds=safety_lead_seconds) if event.snapshot_at else None
        if snapshot_deadline is not None and event.execute_at <= snapshot_deadline:
            raise MySQLEventTimingSafetyError(
                f"EVENT: BLOCKED {event.name}: enabled one-time event executes at "
                f"{event.execute_at}; it was due or within the {safety_lead_seconds}-second "
                "safety window at source snapshot. Reschedule it farther into the future and rerun."
            )
    with conn.cursor() as cur:
        original_time_zone: str | None = None
        try:
            # CREATE EVENT interprets literal schedule values in the session
            # time zone.  Reuse the source event's recorded time zone.
            if event.time_zone:
                cur.execute("SELECT @@session.time_zone")
                original_time_zone = cur.fetchone()[0]
                cur.execute("SET time_zone = %s", (event.time_zone,))
            if event.event_type == "ONE TIME" and event.status == "ENABLED" and event.execute_at is not None:
                cur.execute("SELECT NOW()")
                target_now = cur.fetchone()[0]
                if event.execute_at <= target_now + timedelta(seconds=safety_lead_seconds):
                    raise MySQLEventTimingSafetyError(
                        f"EVENT: BLOCKED {event.name}: enabled one-time event executes at "
                        f"{event.execute_at}; it is due or within the {safety_lead_seconds}-second "
                        "safety window at target creation. It was not replaced."
                    )
            cur.execute(f"DROP EVENT IF EXISTS {_q(event.name)}")
            cur.execute(rewrite_definer(event.ddl))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if original_time_zone is not None:
                cur.execute("SET time_zone = %s", (original_time_zone,))
