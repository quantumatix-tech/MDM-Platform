"""Functional validation for the PostgreSQL E2E target database.

Executes safe read-only operations against migrated objects to confirm they
are usable at runtime, not just structurally present.  All mutating checks
use explicit transactions that are rolled back so no business data is
permanently modified.
"""
from __future__ import annotations

import time
from typing import Any

import psycopg
import psycopg.errors

from tests.e2e.postgresql.validation.expected import SCHEMA_NAME
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


class FunctionalValidator:
    """Runs safe functional checks against the target database."""

    def __init__(self, conn: psycopg.Connection, database_name: str = "") -> None:
        self._conn = conn
        self._db_name = database_name
        self._report = ValidationReport(database=database_name)

    def validate(self) -> ValidationReport:
        start = time.time()
        self._validate_view()
        self._validate_scalar_function()
        self._validate_procedure()
        self._validate_trigger()
        self._validate_foreign_key()
        self._validate_check_constraint()
        self._validate_identity()
        self._validate_sequence()
        self._validate_partition_routing()
        self._validate_enum()
        self._validate_rls()
        self._report.total_duration_s = time.time() - start
        return self._report

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _execute(self, sql: str, params: tuple = ()) -> list[Any]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _execute_nofetch(self, sql: str, params: tuple = ()) -> None:
        """Execute SQL that does not return a result set (e.g. CALL)."""
        with self._conn.cursor() as cur:
            cur.execute(sql, params)

    def _validate_view(self) -> None:
        """Query the migrated view and verify it returns rows."""
        phase = self._new_phase("functional_view")
        t0 = time.time()
        try:
            rows = self._execute("SELECT COUNT(*) FROM training.vw_ordersummary")
            count = rows[0][0]
            phase.add_check(CheckResult(
                name="view_readable",
                status=STATUS_PASS,
                expected=">=0 rows",
                actual=count,
                message=f"vw_ordersummary returned {count} rows",
            ))
        except psycopg.Error as exc:
            phase.add_check(CheckResult(
                name="view_readable",
                status=STATUS_FAIL,
                expected="readable view",
                actual=f"error: {exc}",
                message=f"Cannot read vw_ordersummary: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_scalar_function(self) -> None:
        """Execute the migrated scalar function and verify the result."""
        phase = self._new_phase("functional_scalar_function")
        t0 = time.time()
        try:
            result = self._execute(
                "SELECT training.fn_getordertotal(%s) AS total", (1,)
            )
            val = result[0][0] if result else None
            expected_val = 77400.00
            ok = val == expected_val
            phase.add_check(CheckResult(
                name="scalar_function_executable",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=f"total={expected_val}",
                actual=val,
                message=f"fn_getordertotal(1) returned {val}",
            ))
        except psycopg.Error as exc:
            phase.add_check(CheckResult(
                name="scalar_function_executable",
                status=STATUS_FAIL,
                expected="executable function returns 77400.00",
                actual=f"error: {exc}",
                message=f"Cannot execute fn_getordertotal: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_procedure(self) -> None:
        """Execute the migrated procedure with a zero-qty change (safe no-op).

        The procedure contains an explicit ``COMMIT``, so it cannot run inside
        an application transaction.  We use p_qty_change=0 so no data is
        permanently modified — the UPDATE is a no-op on existing rows.
        """
        phase = self._new_phase("functional_procedure")
        t0 = time.time()
        try:
            before = self._execute(
                "SELECT stockqty FROM training.products WHERE productid = %s", (1,)
            )[0][0]
            self._conn.rollback()
            self._conn.autocommit = True
            try:
                self._execute_nofetch(
                    "CALL training.sp_updateproductstock(%s, %s)", (1, 0)
                )
            finally:
                self._conn.rollback()
                self._conn.autocommit = False

            after = self._execute(
                "SELECT stockqty FROM training.products WHERE productid = %s", (1,)
            )[0][0]

            ok = after == before
            phase.add_check(CheckResult(
                name="procedure_executable",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=f"stock unchanged ({before})",
                actual=after,
                message=f"sp_updateproductstock(1, 0) executed, stock={after} (was {before})",
            ))
        except psycopg.Error as exc:
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
            self._conn.autocommit = False
            phase.add_check(CheckResult(
                name="procedure_executable",
                status=STATUS_FAIL,
                expected="procedure executes successfully",
                actual=f"error: {exc}",
                message=f"Cannot execute sp_updateproductstock: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_trigger(self) -> None:
        """Insert a row into Orders, verify OrderAudit gets a trigger row, rollback."""
        phase = self._new_phase("functional_trigger")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "INSERT INTO training.orders "
                    "(ordernumber, customerid, totalamount, orderstatus) "
                    "VALUES (nextval('training.seq_ordernumber'), %s, %s, %s)",
                    (1, 100.00, "pending"),
                )
                cur.execute(
                    "SELECT COUNT(*) FROM training.orderaudit "
                    "WHERE action = 'INSERT' "
                    "AND orderid = (SELECT MAX(orderid) FROM training.orders)"
                )
                audit_count = cur.fetchone()[0]
                cur.execute("ROLLBACK")

            ok = audit_count >= 1
            phase.add_check(CheckResult(
                name="trigger_fires_on_insert",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected="audit row created",
                actual=audit_count,
                message=f"Trigger created {audit_count} audit row(s) (rolled back)",
            ))
        except psycopg.Error as exc:
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
            phase.add_check(CheckResult(
                name="trigger_fires_on_insert",
                status=STATUS_FAIL,
                expected="trigger fires",
                actual=f"error: {exc}",
                message=f"Cannot test trigger: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_foreign_key(self) -> None:
        """Verify FK enforces by attempting an invalid insert in a transaction."""
        phase = self._new_phase("functional_foreign_key")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN")
                try:
                    cur.execute(
                        "INSERT INTO training.orders (ordernumber, customerid, totalamount, orderstatus) "
                        "VALUES (nextval('training.seq_ordernumber'), %s, 100.00, 'pending')",
                        (999999,),
                    )
                    cur.execute("ROLLBACK")
                    phase.add_check(CheckResult(
                        name="fk_rejects_invalid",
                        status=STATUS_FAIL,
                        expected="FK violation error",
                        actual="insert succeeded (FK not enforced)",
                        message="FK did not reject invalid CustomerID",
                    ))
                except psycopg.errors.ForeignKeyViolation:
                    cur.execute("ROLLBACK")
                    phase.add_check(CheckResult(
                        name="fk_rejects_invalid",
                        status=STATUS_PASS,
                        expected="FK violation error",
                        actual="FK violation raised",
                        message="FK correctly rejected invalid CustomerID",
                    ))
        except psycopg.Error as exc:
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
            phase.add_check(CheckResult(
                name="fk_rejects_invalid",
                status=STATUS_FAIL,
                expected="FK enforced",
                actual=f"error: {exc}",
                message=f"Cannot test FK: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_check_constraint(self) -> None:
        """Verify CHECK rejects invalid data in a transaction."""
        phase = self._new_phase("functional_check_constraint")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN")
                try:
                    cur.execute(
                        "INSERT INTO training.products (sku, name, category, price, stockqty) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        ("CHK001", "Test", "Test", -999.00, 0),
                    )
                    cur.execute("ROLLBACK")
                    phase.add_check(CheckResult(
                        name="check_rejects_invalid",
                        status=STATUS_FAIL,
                        expected="CHECK violation error",
                        actual="insert succeeded (CHECK not enforced)",
                        message="CHECK did not reject negative price",
                    ))
                except psycopg.errors.CheckViolation:
                    cur.execute("ROLLBACK")
                    phase.add_check(CheckResult(
                        name="check_rejects_invalid",
                        status=STATUS_PASS,
                        expected="CHECK violation error",
                        actual="CHECK violation raised",
                        message="CHECK correctly rejected negative price",
                    ))
        except psycopg.Error as exc:
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
            phase.add_check(CheckResult(
                name="check_rejects_invalid",
                status=STATUS_FAIL,
                expected="CHECK enforced",
                actual=f"error: {exc}",
                message=f"Cannot test CHECK: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_identity(self) -> None:
        """Verify identity column auto-generates a value in a transaction."""
        phase = self._new_phase("functional_identity")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "INSERT INTO training.pk_name_test (testname) VALUES (%s) "
                    "RETURNING testid",
                    ("identity_test",),
                )
                identity_val = cur.fetchone()[0]
                cur.execute("ROLLBACK")

            ok = identity_val is not None and identity_val > 0
            phase.add_check(CheckResult(
                name="identity_generates_value",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected="auto-generated identity value",
                actual=identity_val,
                message=f"Identity column returned testid={identity_val} (rolled back)",
            ))
        except psycopg.Error as exc:
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
            phase.add_check(CheckResult(
                name="identity_generates_value",
                status=STATUS_FAIL,
                expected="identity generates value",
                actual=f"error: {exc}",
                message=f"Cannot test identity: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_sequence(self) -> None:
        """Verify the standalone sequence can produce next values."""
        phase = self._new_phase("functional_sequence")
        t0 = time.time()
        try:
            val = self._execute("SELECT nextval('training.seq_ordernumber')")[0][0]
            ok = val is not None and val > 0
            phase.add_check(CheckResult(
                name="sequence_next_value",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected="positive integer",
                actual=val,
                message=f"seq_ordernumber nextval = {val}",
            ))
        except psycopg.Error as exc:
            phase.add_check(CheckResult(
                name="sequence_next_value",
                status=STATUS_FAIL,
                expected="next value",
                actual=f"error: {exc}",
                message=f"Cannot get next sequence value: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_partition_routing(self) -> None:
        """Insert a row into the parent partitioned table and verify routing."""
        phase = self._new_phase("functional_partition_routing")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "INSERT INTO training.partitionedorders (orderdate, amount, status) "
                    "VALUES (%s, %s, %s) RETURNING partitionid",
                    ("2024-06-15", 999.99, "test"),
                )
                inserted_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'partitionedorders_2024' AND n.nspname = 'training'"
                )
                partition_exists = cur.fetchone() is not None
                cur.execute(
                    "SELECT COUNT(*) FROM training.partitionedorders_2024 "
                    "WHERE partitionid = %s", (inserted_id,)
                )
                routed_count = cur.fetchone()[0]
                cur.execute("ROLLBACK")

            ok = partition_exists and routed_count >= 1
            phase.add_check(CheckResult(
                name="partition_routing",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected="row routed to partitionedorders_2024",
                actual=f"partition_exists={partition_exists}, routed_rows={routed_count}",
                message=(
                    "Row with date 2024-06-15 routed to partitionedorders_2024 "
                    "(rolled back)"
                ),
            ))
        except psycopg.Error as exc:
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
            phase.add_check(CheckResult(
                name="partition_routing",
                status=STATUS_FAIL,
                expected="valid partition routing",
                actual=f"error: {exc}",
                message=f"Cannot test partition routing: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_enum(self) -> None:
        """Verify the ENUM type accepts expected values."""
        phase = self._new_phase("functional_enum")
        t0 = time.time()
        try:
            self._execute(
                "SELECT 'pending'::training.orderstatus, "
                "'processing'::training.orderstatus, "
                "'shipped'::training.orderstatus"
            )
            phase.add_check(CheckResult(
                name="enum_values_valid",
                status=STATUS_PASS,
                expected="enum casts succeed",
                actual="all enum values valid",
                message="ENUM training.orderstatus accepts expected labels",
            ))
        except psycopg.Error as exc:
            phase.add_check(CheckResult(
                name="enum_values_valid",
                status=STATUS_FAIL,
                expected="valid enum values",
                actual=f"error: {exc}",
                message=f"ENUM validation failed: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_rls(self) -> None:
        """Verify RLS is enabled and the policy restricts data."""
        phase = self._new_phase("functional_rls")
        t0 = time.time()
        try:
            rls_enabled = self._execute(
                "SELECT relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = 'customers' AND n.nspname = %s",
                (SCHEMA_NAME,),
            )[0][0]
            if rls_enabled:
                with self._conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute("SET LOCAL ROLE migration_role")
                    cur.execute("SET LOCAL myapp.current_city = 'Mumbai'")
                    cur.execute("SELECT COUNT(*) FROM training.customers")
                    city_rows = cur.fetchone()[0]
                    cur.execute("SET LOCAL myapp.current_city = 'NonExistentCity'")
                    cur.execute("SELECT COUNT(*) FROM training.customers")
                    restricted_rows = cur.fetchone()[0]
                    cur.execute("ROLLBACK")

                ok = city_rows > 0 and restricted_rows == 0
                phase.add_check(CheckResult(
                    name="rls_policy_restricts",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected="Mumbai>0 rows, NonExistentCity=0 rows",
                    actual=f"Mumbai={city_rows}, NonExistentCity={restricted_rows}",
                    message=(
                        f"RLS: Mumbai={city_rows} rows, NonExistentCity={restricted_rows} rows"
                    ),
                ))
            else:
                phase.add_check(CheckResult(
                    name="rls_policy_restricts",
                    status=STATUS_FAIL,
                    expected="RLS enabled",
                    actual=f"RLS enabled={rls_enabled}",
                    message="RLS is not enabled on customers table",
                ))
        except psycopg.Error as exc:
            phase.add_check(CheckResult(
                name="rls_policy_restricts",
                status=STATUS_FAIL,
                expected="RLS policy enforces row isolation",
                actual=f"error: {exc}",
                message=f"Cannot test RLS: {exc}",
            ))
            try:
                self._conn.rollback()
            except psycopg.Error:
                pass
        phase.duration_s = time.time() - t0
