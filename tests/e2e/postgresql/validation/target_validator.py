"""Post-migration target structural validation for PostgreSQL E2E.

Validates that the target database contains the expected migrated structure
after Phase C2 has executed the real migration.  Reuses the
``PostgreSQLCatalog`` / ``expected`` infrastructure from Phase B so that source
and target are validated against the *same* fixture contract.

Adds target-specific checks:
  - database connectivity
  - correct database name
  - database is not a protected/non-E2E database
"""
from __future__ import annotations

import time

import psycopg

from tests.e2e.postgresql.setup.target import is_protected_database
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)
from tests.e2e.postgresql.validation.source_validator import SourceValidator


class TargetValidator(SourceValidator):
    """Validates a PostgreSQL target database against the E2E expected fixture.

    Inherits all structural checks from ``SourceValidator`` (tables, columns,
    PKs, FKs, indexes, views, functions, procedures, triggers, sequences,
    UDTs, partitioning, RLS, grants, comments, row counts) and prepends
    target-specific safety / connectivity phases.
    """

    def __init__(self, catalog, database_name: str = "", *,
                 skip_connectivity: bool = False) -> None:
        super().__init__(catalog, database_name)
        self._skip_connectivity = skip_connectivity

    def validate(self) -> ValidationReport:
        start = time.time()
        if not self._skip_connectivity:
            self._validate_target_safety()
            self._validate_connectivity()
        super().validate()
        self._report.total_duration_s = time.time() - start
        return self._report

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _validate_target_safety(self) -> None:
        """Verify the target database name matches the E2E safety pattern."""
        phase = self._new_phase("target_safety")
        t0 = time.time()
        safe = not is_protected_database(self._db_name)
        phase.add_check(CheckResult(
            name="database_is_e2e_safe",
            status=STATUS_PASS if safe else STATUS_FAIL,
            expected=True,
            actual=safe,
            message=(
                f"Database '{self._db_name}' is safe for E2E validation"
                if safe else
                f"Database '{self._db_name}' is protected — aborting validation"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_connectivity(self) -> None:
        """Validate target database connectivity and correct database."""
        phase = self._new_phase("target_connectivity")
        t0 = time.time()
        conn = self._cat._conn

        conn_ok = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.Error:
            conn_ok = False

        phase.add_check(CheckResult(
            name="database_reachable",
            status=STATUS_PASS if conn_ok else STATUS_FAIL,
            expected=True,
            actual=conn_ok,
            message="PostgreSQL connection established"
            if conn_ok else "Cannot reach PostgreSQL",
        ))

        db_name = "unknown"
        if conn_ok:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                db_name = cur.fetchone()[0]

        db_ok = db_name == self._db_name
        phase.add_check(CheckResult(
            name="correct_database",
            status=STATUS_PASS if db_ok else STATUS_FAIL,
            expected=self._db_name,
            actual=db_name,
            message=f"Connected to {db_name}"
            if db_ok else
            f"Connected to wrong database: expected {self._db_name}, got {db_name}",
        ))

        phase.duration_s = time.time() - t0
