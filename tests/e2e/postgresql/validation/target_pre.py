"""Pre-migration target validation for PostgreSQL E2E Phase C1.

Verifies that the target database is cleanly reset and contains no
user-created objects before migration begins.  All checks are read-only
catalog queries — no data or objects are modified.

PostgreSQL adaptations vs. the MSSQL ``TargetPreValidator``:
  * Connects to the ``postgres`` maintenance database (not ``master``).
  * System schemas: ``pg_catalog``, ``information_schema``, ``pg_toast``,
    ``pg_temp_*``, ``pg_toast_temp_*``.
  * No ``dbo``/``guest`` schemas — ``public`` is the default user schema.
  * Object discovery queries against ``pg_class``, ``pg_proc``,
    ``pg_constraint``, ``pg_trigger`` using ``relkind`` filters.
"""
from __future__ import annotations

import time

import psycopg

from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)

_SYSTEM_SCHEMAS = frozenset({
    "pg_catalog",
    "information_schema",
    "pg_toast",
})


def _is_system_schema(schema_name: str) -> bool:
    """Return True for PostgreSQL internal schemas."""
    return schema_name in _SYSTEM_SCHEMAS or schema_name.startswith(
        ("pg_toast_temp_", "pg_temp_", "pg_toast_")
    )


class TargetPreValidator:
    """Validates that the PostgreSQL target database is in a clean pre-migration state."""

    def __init__(self, conn: psycopg.Connection, db_name: str) -> None:
        self._conn = conn
        self._db_name = db_name
        self._report = ValidationReport(database=db_name)

    def validate(self) -> ValidationReport:
        start = time.time()
        self._validate_connectivity()
        # Only run clean-state checks if the connection is alive
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
        self._conn_phase = phase
        t0 = time.time()

        conn_ok = True
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.Error:
            conn_ok = False

        phase.add_check(CheckResult(
            name="database_reachable",
            status=STATUS_PASS if conn_ok else STATUS_FAIL,
            expected=True,
            actual=conn_ok,
            message="PostgreSQL server reachable"
            if conn_ok else
            "Cannot reach PostgreSQL server",
        ))

        if not conn_ok:
            # Connection failure — skip dependent checks
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
                expected="any PostgreSQL version",
                actual=None,
                message="Skipped: connection not reachable",
                details={"skipped_due": "connection_failure"},
            ))
            phase.duration_s = time.time() - t0
            return

        db_name = self._db_name
        with self._conn.cursor() as cur:
            cur.execute("SELECT current_database()")
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

        # Server version
        with self._conn.cursor() as cur:
            cur.execute("SHOW server_version")
            version_row = cur.fetchone()
        server_version = version_row[0] if version_row else "unknown"

        phase.add_check(CheckResult(
            name="server_version",
            status=STATUS_PASS if server_version != "unknown" else STATUS_FAIL,
            expected="PostgreSQL (any version)",
            actual=server_version,
            message=f"PostgreSQL server version: {server_version}",
        ))

        phase.duration_s = time.time() - t0

    def _validate_clean_state(self) -> None:
        """Phase C1 clean-state checks — no user objects in fresh target."""
        phase = self._new_phase("clean_state")
        t0 = time.time()

        all_clean = True
        with self._conn.cursor() as cur:
            # --- Unexpected user schemas ---
            cur.execute(
                """
                SELECT nspname FROM pg_namespace
                WHERE nspname NOT LIKE 'pg_%'
                  AND nspname NOT IN ('information_schema', 'public')
                ORDER BY nspname
                """
            )
            extra_schemas = [r[0] for r in cur.fetchall()]
            schema_ok = len(extra_schemas) == 0
            phase.add_check(CheckResult(
                name="no_unexpected_schemas",
                status=STATUS_PASS if schema_ok else STATUS_FAIL,
                expected="only pg_catalog/information_schema/pg_toast/public",
                actual=extra_schemas or [],
                message=(
                    "Only system schemas present in fresh target"
                    if schema_ok else
                    f"Unexpected schemas: {extra_schemas}"
                ),
            ))
            if not schema_ok:
                all_clean = False

            # --- User tables (excluding system) ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                """
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
                """
                SELECT COUNT(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'v'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                """
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

            # --- Functions (excluding system) ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND p.prokind = 'f'
                """
            )
            func_count = cur.fetchone()[0]
            funcs_ok = func_count == 0
            phase.add_check(CheckResult(
                name="no_user_functions",
                status=STATUS_PASS if funcs_ok else STATUS_FAIL,
                expected=0,
                actual=func_count,
                message="No user functions" if funcs_ok else
                f"Found {func_count} user functions",
            ))
            if not funcs_ok:
                all_clean = False

            # --- Procedures ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND p.prokind = 'p'
                """
            )
            proc_count = cur.fetchone()[0]
            procs_ok = proc_count == 0
            phase.add_check(CheckResult(
                name="no_user_procedures",
                status=STATUS_PASS if procs_ok else STATUS_FAIL,
                expected=0,
                actual=proc_count,
                message="No user procedures" if procs_ok else
                f"Found {proc_count} user procedures",
            ))
            if not procs_ok:
                all_clean = False

            # --- Sequences ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'S'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                """
            )
            seq_count = cur.fetchone()[0]
            seqs_ok = seq_count == 0
            phase.add_check(CheckResult(
                name="no_user_sequences",
                status=STATUS_PASS if seqs_ok else STATUS_FAIL,
                expected=0,
                actual=seq_count,
                message="No user sequences" if seqs_ok else
                f"Found {seq_count} user sequences",
            ))
            if not seqs_ok:
                all_clean = False

            # --- Custom types (ENUM, composite, domain) ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND t.typtype IN ('e', 'd', 'c')
                  AND t.typname NOT LIKE '_%'
                """
            )
            type_count = cur.fetchone()[0]
            types_ok = type_count == 0
            phase.add_check(CheckResult(
                name="no_user_types",
                status=STATUS_PASS if types_ok else STATUS_FAIL,
                expected=0,
                actual=type_count,
                message="No user-defined types" if types_ok else
                f"Found {type_count} user-defined types",
            ))
            if not types_ok:
                all_clean = False

            # --- Triggers ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_trigger t
                JOIN pg_class c ON t.tgrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND NOT t.tgisinternal
                """
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

            # --- RLS policies ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_policy p
                JOIN pg_class c ON p.polrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                """
            )
            policy_count = cur.fetchone()[0]
            policies_ok = policy_count == 0
            phase.add_check(CheckResult(
                name="no_user_rls_policies",
                status=STATUS_PASS if policies_ok else STATUS_FAIL,
                expected=0,
                actual=policy_count,
                message="No user RLS policies" if policies_ok else
                f"Found {policy_count} user RLS policies",
            ))
            if not policies_ok:
                all_clean = False

            # --- Extension objects (non-system) ---
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_extension e
                JOIN pg_namespace n ON e.extnamespace = n.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                """
            )
            ext_count = cur.fetchone()[0]
            exts_ok = ext_count == 0
            phase.add_check(CheckResult(
                name="no_user_extensions",
                status=STATUS_PASS if exts_ok else STATUS_FAIL,
                expected=0,
                actual=ext_count,
                message="No user extensions" if exts_ok else
                f"Found {ext_count} user extensions",
            ))
            if not exts_ok:
                all_clean = False

            if not all_clean:
                phase.status = STATUS_FAIL

        phase.duration_s = time.time() - t0
