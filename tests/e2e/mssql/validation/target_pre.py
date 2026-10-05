"""Pre-migration target validation for MSSQL E2E.

Verifies that the target database is cleanly reset and contains no
user-created objects before migration begins.  All checks are read-only
catalog queries — no data or objects are modified.
"""
from __future__ import annotations

import time

import pyodbc

from tests.e2e.mssql.setup.target import (
    get_database_state,
    is_protected_database,
)
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)

# System schemas that are expected to exist in a fresh database.
# Fixed database roles (db_owner, db_datareader, etc.) also appear as
# schemas in sys.schemas and must be excluded from the "unexpected schemas"
# check.
_SYSTEM_SCHEMAS = frozenset({
    "sys", "INFORMATION_SCHEMA", "guest", "dbo",
})
_FIXED_DB_ROLES = frozenset({
    "db_owner", "db_accessadmin", "db_backupoperator",
    "db_datareader", "db_datawriter", "db_ddladmin",
    "db_denydatareader", "db_denydatawriter", "db_securityadmin",
})


class TargetPreValidator:
    """Validates that the target database is in a clean pre-migration state."""

    def __init__(self, conn: pyodbc.Connection, db_name: str) -> None:
        self._conn = conn
        self._db_name = db_name
        self._report = ValidationReport(database=db_name)

    def validate(self) -> ValidationReport:
        start = time.time()
        self._validate_connectivity()
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

        # 1. Database reachable / connection alive
        conn_ok = True
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except Exception:
            conn_ok = False

        phase.add_check(CheckResult(
            name="database_reachable",
            status=STATUS_PASS if conn_ok else STATUS_FAIL,
            expected=True,
            actual=conn_ok,
            message="SQL Server connection established"
            if conn_ok else
            "Cannot reach SQL Server",
        ))

        # 2. Correct database is being used
        db_name = self._db_name
        with self._conn.cursor() as cur:
            cur.execute("SELECT DB_NAME()")
            actual_db = cur.fetchone()[0]

        db_ok = actual_db == db_name
        phase.add_check(CheckResult(
            name="correct_database",
            status=STATUS_PASS if db_ok else STATUS_FAIL,
            expected=db_name,
            actual=actual_db,
            message=f"Connected to {actual_db}"
            if db_ok else
            f"Connected to wrong database: expected {db_name}, got {actual_db}",
        ))

        # 3. Database is ONLINE
        state = get_database_state(
            self._conn, db_name
        ) if not is_protected_database(db_name) else None
        # get_database_state needs a master connection context;
        # here we just check state via sys.databases on the current connection
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT state_desc FROM sys.databases WHERE name = ?",
                (db_name,),
            )
            row = cur.fetchone()
            state = row[0] if row else None

        online_ok = state == "ONLINE"
        phase.add_check(CheckResult(
            name="database_online",
            status=STATUS_PASS if online_ok else STATUS_FAIL,
            expected="ONLINE",
            actual=state or "NOT FOUND",
            message=f"Database state: {state or 'unknown'}"
            if online_ok else
            f"Database is not ONLINE (state={state})",
        ))

        phase.duration_s = time.time() - t0

    def _validate_clean_state(self) -> None:
        """Phase C1 clean-state checks — no user objects in fresh target."""
        phase = self._new_phase("clean_state")
        t0 = time.time()

        checks: list[tuple[str, str, int]] = [
            ("no_user_tables",
             "SELECT COUNT(*) FROM sys.tables",
             0),
            ("no_user_views",
             "SELECT COUNT(*) FROM sys.views",
             0),
            ("no_user_procedures",
             "SELECT COUNT(*) FROM sys.objects WHERE type = 'P'",
             0),
            ("no_user_functions",
             "SELECT COUNT(*) FROM sys.objects WHERE type IN ('FN','IF','TF')",
             0),
            ("no_user_triggers",
             "SELECT COUNT(*) FROM sys.triggers",
             0),
            ("no_user_sequences",
             "SELECT COUNT(*) FROM sys.sequences",
             0),
            ("no_user_synonyms",
             "SELECT COUNT(*) FROM sys.synonyms",
             0),
            ("no_user_defined_types",
             "SELECT COUNT(*) FROM sys.types WHERE is_user_defined = 1",
             0),
        ]

        all_clean = True
        with self._conn.cursor() as cur:
            for check_name, sql, expected in checks:
                cur.execute(sql)
                actual = cur.fetchone()[0]
                ok = actual == expected
                if not ok:
                    all_clean = False
                phase.add_check(CheckResult(
                    name=check_name,
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected=expected,
                    actual=actual,
                    message=(
                        f"{check_name}: expected {expected}, got {actual}"
                    ),
                ))

        # Check for unexpected schemas
        cur.execute(
            "SELECT name FROM sys.schemas "
            "WHERE name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'dbo') "
            "AND name NOT IN ("
            "'db_owner', 'db_accessadmin', 'db_backupoperator', "
            "'db_datareader', 'db_datawriter', 'db_ddladmin', "
            "'db_denydatareader', 'db_denydatawriter', 'db_securityadmin'"
            ")"
        )
        extra_schemas = [r[0] for r in cur.fetchall()]
        schema_ok = len(extra_schemas) == 0
        phase.add_check(CheckResult(
            name="no_unexpected_schemas",
            status=STATUS_PASS if schema_ok else STATUS_FAIL,
            expected="empty",
            actual=extra_schemas or [],
            message=(
                "Only system schemas present in fresh target"
                if schema_ok else
                f"Unexpected schemas: {extra_schemas}"
            ),
        ))
        if not schema_ok:
            all_clean = False

        if not all_clean:
            phase.status = STATUS_FAIL

        phase.duration_s = time.time() - t0
