from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from core.connectors.base import EventDef
from core.connectors.mysql import (
    MySQLEventTimingSafetyError,
    MySQLSourceConnector,
    MySQLTargetConnector,
)


NOW = datetime(2026, 9, 21, 12, 0, 0)
SOURCE_DEFINER = "`mysql_test`@`%`"
TARGET_DEFINER = "mysql_admin@%"


def _target():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    connection = MagicMock()
    connection.cursor.return_value = cursor
    target = MySQLTargetConnector({"database": "target", "routine_definer": TARGET_DEFINER})
    target._conn = connection
    return target, cursor, connection


def _event(*, name="evt", event_type="RECURRING", status="ENABLED", execute_at=None, time_zone=None):
    return EventDef(
        name=name,
        ddl=(
            f"CREATE DEFINER={SOURCE_DEFINER} EVENT `{name}` ON SCHEDULE EVERY 1 MINUTE "
            "STARTS '2026-09-21 13:06:28' ON COMPLETION PRESERVE "
            f"{status} DO INSERT INTO migration_log(message) VALUES ('event test')"
        ),
        event_type=event_type,
        status=status,
        execute_at=execute_at,
        snapshot_at=NOW,
        time_zone=time_zone,
        safety_lead_seconds=300,
    )


def test_source_event_snapshot_captures_status_schedule_and_definer(monkeypatch):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = (NOW,)
    cursor.fetchall.return_value = [
        (
            "evt_one_time", "ONE TIME", "ENABLED", NOW + timedelta(hours=1), None,
            None, None, None, "PRESERVE", "SYSTEM", "mysql_test@%",
        )
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    source = MySQLSourceConnector({"database": "source"})
    source._conn = connection
    monkeypatch.setattr(source, "_show_create", lambda kind, name: f"CREATE EVENT `{name}` ENABLE DO SELECT 1")

    events = source.list_events()

    assert len(events) == 1
    event = events[0]
    assert event.name == "evt_one_time"
    assert event.event_type == "ONE TIME"
    assert event.status == "ENABLED"
    assert event.execute_at == NOW + timedelta(hours=1)
    assert event.on_completion == "PRESERVE"
    assert event.time_zone == "SYSTEM"
    assert event.definer == "mysql_test@%"
    assert event.snapshot_at == NOW


@pytest.mark.parametrize("status", ["ENABLED", "DISABLED"])
def test_recurring_event_preserves_status_schedule_and_rewrites_definer(status):
    target, cursor, connection = _target()
    event = _event(status=status)

    target.create_event(event)

    ddl = cursor.execute.call_args_list[1].args[0]
    assert f"ON COMPLETION PRESERVE {status}" in ddl
    assert "ON SCHEDULE EVERY 1 MINUTE" in ddl
    assert "STARTS '2026-09-21 13:06:28'" in ddl
    assert "DEFINER=`mysql_admin`@`%`" in ddl
    assert connection.commit.called


def test_future_enabled_one_time_event_is_created_with_rewritten_definer():
    target, cursor, _ = _target()
    execute_at = NOW + timedelta(hours=1)
    event = _event(event_type="ONE TIME", execute_at=execute_at, time_zone="SYSTEM")
    event.ddl = (
        f"CREATE DEFINER={SOURCE_DEFINER} EVENT `evt` ON SCHEDULE AT '2026-09-21 13:00:00' "
        "ON COMPLETION PRESERVE ENABLE DO SELECT 1"
    )
    cursor.fetchone.side_effect = [("SYSTEM",), (NOW,)]

    target.create_event(event)

    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert "SET time_zone = %s" in executed
    assert "DROP EVENT IF EXISTS `evt`" in executed
    assert any("ON SCHEDULE AT '2026-09-21 13:00:00'" in sql for sql in executed)
    assert any("DEFINER=`mysql_admin`@`%`" in sql for sql in executed)


@pytest.mark.parametrize("execute_at", [NOW, NOW + timedelta(seconds=299)])
def test_due_or_too_close_enabled_one_time_event_is_blocked_without_target_change(execute_at):
    target, cursor, connection = _target()
    event = _event(event_type="ONE TIME", execute_at=execute_at)

    with pytest.raises(MySQLEventTimingSafetyError, match="EVENT: BLOCKED"):
        target.create_event(event)

    assert not cursor.execute.called
    assert not connection.commit.called


def test_one_time_event_that_becomes_due_before_target_creation_is_not_replaced():
    target, cursor, connection = _target()
    event = _event(event_type="ONE TIME", execute_at=NOW + timedelta(minutes=10), time_zone="SYSTEM")
    cursor.fetchone.side_effect = [("SYSTEM",), (NOW + timedelta(minutes=6),)]

    with pytest.raises(MySQLEventTimingSafetyError, match="target creation"):
        target.create_event(event)

    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert "DROP EVENT IF EXISTS `evt`" not in executed
    assert not connection.commit.called


def test_event_replacement_is_scoped_to_the_snapshotted_source_event_only():
    """FULL migration has no target-wide Event pruning or ownership registry."""
    target, cursor, _ = _target()
    event = _event(name="evt_migration_e2e_final", status="DISABLED")

    target.create_event(event)

    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert executed[0] == "DROP EVENT IF EXISTS `evt_migration_e2e_final`"
    assert all("evt_live_event_test" not in sql for sql in executed)
    assert all("evt_task9_one_time_future" not in sql for sql in executed)
