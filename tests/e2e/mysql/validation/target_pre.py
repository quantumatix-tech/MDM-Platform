"""Pre-migration target validation for MySQL E2E Phase C1.

Verifies that the target database is cleanly reset and contains no
user-created objects before migration begins.  All checks are read-only
catalog queries — no data or objects are modified.

MySQL adaptations vs. the PostgreSQL ``TargetPreValidator``:
  * Connects to the ``mysql`` maintenance database (not ``postgres``).
  * System schemas: ``mysql``, ``information_schema``, ``performance_schema``,
    ``sys``.
  * Object discovery queries against ``INFORMATION_SCHEMA``.
  * No RLS policies, no sequences, no user-defined types, no extensions.
"""
from __future__ import annotations

import time

import mysql.connector

from tests.e2e.mysql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)

_SYSTEM_SCHEMAS = frozenset({
    "mysql",
    "information_schema",
    "performance_schema",
    "sys",
})


class MySQLTargetPreValidator:
    """Validates that the MySQL target database is in a clean pre-migration state."""

    def __init__(self, conn: mysql.connector.MySQLConnection, db_name: str) -> None:
        self._conn = conn
        self._db_name = db_name
        self._report = ValidationReport(database=db_name)

    def validate(self) -> ValidationReport:
        start = time.time()
        self._validate_connectivity()
        conn_phase = self._report.phases[-1] if self._report.phases else None
        conn_reachable = any(
            c.name == "database_reachable" and c.status == STATUS_PASS
            for c in conn_phase.checks
        ) if conn_phase else False
        if conn_reachable:
            self._validate_clean_state()
        self._report.total_duration_s = time.time() - start
        return self._report

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _validate_connectivity(self) -> None:
        """Phase C1 connectivity and database-existence checks."""
        phase = self._new_phase("target_connectivity")
        t0 = time.time()

        conn_ok = True
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except mysql.connector.Error:
            conn_ok = False

        phase.add_check(CheckResult(
            name="database_reachable",
            status=STATUS_PASS if conn_ok else STATUS_FAIL,
            expected=True,
            actual=conn_ok,
            message="MySQL server reachable"
            if conn_ok else
            "Cannot reach MySQL server",
        ))

        if not conn_ok:
            phase.add_check(CheckResult(
                name="correct_database",
                status=STATUS_FAIL,
                expected=self._db_name,
                actual=None,
                message="Skipped: connection not reachable",
                details={"skipped_due": "connection_failure"},
            ))
            phase.add_check(CheckResult(
                name="server_version",
                status=STATUS_FAIL,
                expected="any MySQL version",
                actual=None,
                message="Skipped: connection not reachable",
                details={"skipped_due": "connection_failure"},
            ))
            phase.duration_s = time.time() - t0
            return

        with self._conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            actual_db = cur.fetchone()[0]

        db_ok = actual_db.lower() == self._db_name.lower()
        phase.add_check(CheckResult(
            name="correct_database",
            status=STATUS_PASS if db_ok else STATUS_FAIL,
            expected=self._db_name,
            actual=actual_db,
            message=f"Connected to {actual_db}"
            if db_ok else
            f"Connected to wrong database: expected {self._db_name}, got {actual_db}",
        ))

        with self._conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version_row = cur.fetchone()
        server_version = version_row[0] if version_row else "unknown"

        phase.add_check(CheckResult(
            name="server_version",
            status=STATUS_PASS if server_version != "unknown" else STATUS_FAIL,
            expected="MySQL (any version)",
            actual=server_version,
            message=f"MySQL server version: {server_version}",
        ))

        phase.duration_s = time.time() - t0

    def _validate_clean_state(self) -> None:
        """Phase C1 clean-state checks — no user objects in fresh target."""
        phase = self._new_phase("clean_state")
        t0 = time.time()

        all_clean = True
        with self._conn.cursor() as cur:
            # --- User tables (excluding system) ---
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                (self._db_name,),
            )
            table_count = cur.fetchone()[0]
            tables_ok = table_count == 0
            phase.add_check(CheckResult(
                name="no_user_tables",
                status=STATUS_PASS if tables_ok else STATUS_FAIL,
                expected=0,
                actual=table_count,
                message="No user tables" if tables_ok else
                f"Found {table_count} user tables",
            ))
            if not tables_ok:
                all_clean = False

            # --- Views ---
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS "
                "WHERE TABLE_SCHEMA = %s",
                (self._db_name,),
            )
            view_count = cur.fetchone()[0]
            views_ok = view_count == 0
            phase.add_check(CheckResult(
                name="no_user_views",
                status=STATUS_PASS if views_ok else STATUS_FAIL,
                expected=0,
                actual=view_count,
                message="No user views" if views_ok else
                f"Found {view_count} user views",
            ))
            if not views_ok:
                all_clean = False

            # --- Routines (functions + procedures) ---
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.ROUTINES "
                "WHERE ROUTINE_SCHEMA = %s",
                (self._db_name,),
            )
            routine_count = cur.fetchone()[0]
            routines_ok = routine_count == 0
            phase.add_check(CheckResult(
                name="no_user_routines",
                status=STATUS_PASS if routines_ok else STATUS_FAIL,
                expected=0,
                actual=routine_count,
                message="No user routines" if routines_ok else
                f"Found {routine_count} user routines",
            ))
            if not routines_ok:
                all_clean = False

            # --- Triggers ---
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = %s",
                (self._db_name,),
            )
            trigger_count = cur.fetchone()[0]
            triggers_ok = trigger_count == 0
            phase.add_check(CheckResult(
                name="no_user_triggers",
                status=STATUS_PASS if triggers_ok else STATUS_FAIL,
                expected=0,
                actual=trigger_count,
                message="No user triggers" if triggers_ok else
                f"Found {trigger_count} user triggers",
            ))
            if not triggers_ok:
                all_clean = False

        # --- Events ---
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.EVENTS "
                "WHERE EVENT_SCHEMA = %s",
                (self._db_name,),
            )
            event_count = cur.fetchone()[0]
            events_ok = event_count == 0
            phase.add_check(CheckResult(
                name="no_user_events",
                status=STATUS_PASS if events_ok else STATUS_FAIL,
                expected=0,
                actual=event_count,
                message="No user events" if events_ok else
                f"Found {event_count} user events",
            ))
            if not events_ok:
                all_clean = False

        if not all_clean:
            phase.status = STATUS_FAIL

        phase.duration_s = time.time() - t0
