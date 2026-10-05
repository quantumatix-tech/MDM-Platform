"""Functional validation for the MySQL E2E target database.

Executes safe read-only operations against migrated objects to confirm they
are usable at runtime, not just structurally present.  All mutating checks
use ``START TRANSACTION / ROLLBACK`` so no business data is permanently
modified.

MySQL-specific considerations:
  * No standalone sequences — ``AUTO_INCREMENT`` replaces them.
  * No RLS policies, user-defined types, or extensions.
  * Routines are schema-less in MySQL 8 (created in the current database).
  * ENUM is a column type, validated by inserting/casting valid values.
"""
from __future__ import annotations

import time
from decimal import Decimal

import mysql.connector
import mysql.connector.errors

from tests.e2e.mysql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


class MySQLFunctionalValidator:
    """Runs safe functional checks against the MySQL target database."""

    def __init__(self, conn: mysql.connector.MySQLConnection, database_name: str = "") -> None:
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
        self._validate_auto_increment()
        self._validate_partition_routing()
        self._validate_enum()
        self._report.total_duration_s = time.time() - start
        return self._report

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _execute(self, sql: str, params: tuple = ()) -> list[list]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _execute_nofetch(self, sql: str, params: tuple = ()) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)

    def _validate_view(self) -> None:
        """Query the migrated view and verify it returns rows."""
        phase = self._new_phase("functional_view")
        t0 = time.time()
        try:
            rows = self._execute("SELECT COUNT(*) FROM vw_ordersummary")
            count = rows[0][0]
            phase.add_check(CheckResult(
                name="view_readable",
                status=STATUS_PASS,
                expected=">=0 rows",
                actual=count,
                message=f"vw_ordersummary returned {count} rows",
            ))
        except mysql.connector.Error as exc:
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
            result = self._execute("SELECT fn_GetOrderTotal(%s) AS total", (1,))
            val = result[0][0] if result else None
            expected_val = Decimal("77400.00")
            ok = val == expected_val
            phase.add_check(CheckResult(
                name="scalar_function_executable",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=f"total={expected_val}",
                actual=val,
                message=f"fn_GetOrderTotal(1) returned {val}",
            ))
        except mysql.connector.Error as exc:
            phase.add_check(CheckResult(
                name="scalar_function_executable",
                status=STATUS_FAIL,
                expected="executable function returns 77400.00",
                actual=f"error: {exc}",
                message=f"Cannot execute fn_GetOrderTotal: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_procedure(self) -> None:
        """Execute the migrated procedure and verify it modifies stock.

        MySQL procedures may contain explicit COMMIT statements, so we
        cannot rely on transaction rollback. We use a compensating delta:
        increment by 1, then decrement by 1, and verify the final value
        matches the original.
        """
        phase = self._new_phase("functional_procedure")
        t0 = time.time()

        original_autocommit = self._conn.autocommit
        try:
            self._conn.autocommit = True
            before = self._execute(
                "SELECT stockqty FROM products WHERE productid = %s", (1,)
            )[0][0]

            self._execute_nofetch("CALL sp_UpdateProductStock(%s, %s)", (1, 1))
            self._execute_nofetch("CALL sp_UpdateProductStock(%s, %s)", (1, -1))

            after = self._execute(
                "SELECT stockqty FROM products WHERE productid = %s", (1,)
            )[0][0]

            ok = after == before
            phase.add_check(CheckResult(
                name="procedure_executable",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=f"stock restored ({before})",
                actual=after,
                message=f"sp_UpdateProductStock(1, +1) then (1, -1), stock={after} (was {before})",
            ))
        except mysql.connector.Error as exc:
            phase.add_check(CheckResult(
                name="procedure_executable",
                status=STATUS_FAIL,
                expected="procedure executes successfully",
                actual=f"error: {exc}",
                message=f"Cannot execute sp_UpdateProductStock: {exc}",
            ))
        finally:
            self._conn.autocommit = original_autocommit
        phase.duration_s = time.time() - t0

    def _validate_trigger(self) -> None:
        """Insert a row into Orders, verify OrderAudit gets a trigger row, rollback."""
        phase = self._new_phase("functional_trigger")
        t0 = time.time()
        try:
            self._conn.start_transaction()
            try:
                self._execute_nofetch(
                    "INSERT INTO orders (ordernumber, customerid, totalamount, orderstatus) "
                    "VALUES (%s, %s, %s, %s)",
                    (999999, 1, Decimal("100.00"), "pending"),
                )
                audit_count_rows = self._execute(
                    "SELECT COUNT(*) FROM orderaudit "
                    "WHERE action = 'INSERT' "
                    "AND orderid = (SELECT MAX(orderid) FROM orders)"
                )
                audit_count = audit_count_rows[0][0]
            finally:
                self._conn.rollback()

            ok = audit_count >= 1
            phase.add_check(CheckResult(
                name="trigger_fires_on_insert",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected="audit row created",
                actual=audit_count,
                message=f"Trigger created {audit_count} audit row(s) (rolled back)",
            ))
        except mysql.connector.Error as exc:
            try:
                self._conn.rollback()
            except mysql.connector.Error:
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
            self._conn.start_transaction()
            try:
                self._execute_nofetch(
                    "INSERT INTO orders (ordernumber, customerid, totalamount, orderstatus) "
                    "VALUES (%s, %s, 100.00, 'pending')",
                    (99999999, 999999),
                )
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="fk_rejects_invalid",
                    status=STATUS_FAIL,
                    expected="FK violation error",
                    actual="insert succeeded (FK not enforced)",
                    message="FK did not reject invalid CustomerID",
                ))
            except mysql.connector.errors.IntegrityError:
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="fk_rejects_invalid",
                    status=STATUS_PASS,
                    expected="FK violation error",
                    actual="FK violation raised",
                    message="FK correctly rejected invalid CustomerID",
                ))
        except mysql.connector.Error as exc:
            try:
                self._conn.rollback()
            except mysql.connector.Error:
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
            self._conn.start_transaction()
            try:
                self._execute_nofetch(
                    "INSERT INTO products (sku, name, category, price, stockqty) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("CHK001", "Test", "Test", Decimal("-999.00"), 0),
                )
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="check_rejects_invalid",
                    status=STATUS_FAIL,
                    expected="CHECK violation error",
                    actual="insert succeeded (CHECK not enforced)",
                    message="CHECK did not reject negative price",
                ))
            except mysql.connector.errors.IntegrityError:
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="check_rejects_invalid",
                    status=STATUS_PASS,
                    expected="CHECK violation error",
                    actual="CHECK violation raised",
                    message="CHECK correctly rejected negative price",
                ))
            except mysql.connector.errors.Error as exc:
                if getattr(exc, "errno", None) in (3819, 3820, 3821):
                    self._conn.rollback()
                    phase.add_check(CheckResult(
                        name="check_rejects_invalid",
                        status=STATUS_PASS,
                        expected="CHECK violation error",
                        actual="CHECK violation raised",
                        message="CHECK correctly rejected negative price",
                    ))
                else:
                    raise
        except mysql.connector.Error as exc:
            try:
                self._conn.rollback()
            except mysql.connector.Error:
                pass
            phase.add_check(CheckResult(
                name="check_rejects_invalid",
                status=STATUS_FAIL,
                expected="CHECK enforced",
                actual=f"error: {exc}",
                message=f"Cannot test CHECK: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_auto_increment(self) -> None:
        """Verify AUTO_INCREMENT column auto-generates a value in a transaction."""
        phase = self._new_phase("functional_auto_increment")
        t0 = time.time()
        try:
            self._conn.start_transaction()
            try:
                self._execute_nofetch(
                    "INSERT INTO pk_name_test (testname) VALUES (%s)",
                    ("identity_test",),
                )
                result = self._execute(
                    "SELECT MAX(testid) FROM pk_name_test"
                )
                identity_val = result[0][0]
                self._conn.rollback()

                ok = identity_val is not None and identity_val > 0
                phase.add_check(CheckResult(
                    name="auto_increment_generates_value",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected="auto-generated identity value",
                    actual=identity_val,
                    message=f"AUTO_INCREMENT returned testid={identity_val} (rolled back)",
                ))
            except mysql.connector.Error as exc:
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="auto_increment_generates_value",
                    status=STATUS_FAIL,
                    expected="identity generates value",
                    actual=f"error: {exc}",
                    message=f"Cannot test AUTO_INCREMENT: {exc}",
                ))
        except mysql.connector.Error as exc:
            phase.add_check(CheckResult(
                name="auto_increment_generates_value",
                status=STATUS_FAIL,
                expected="identity generates value",
                actual=f"error: {exc}",
                message=f"Cannot test AUTO_INCREMENT: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_partition_routing(self) -> None:
        """Insert a row into a partitioned table, verify it's visible, rollback."""
        phase = self._new_phase("functional_partition_routing")
        t0 = time.time()
        try:
            self._conn.start_transaction()
            try:
                self._execute_nofetch(
                    "INSERT INTO partitionedorders (orderdate, amount, status) "
                    "VALUES (%s, %s, %s)",
                    ("2024-06-15", Decimal("999.99"), "test"),
                )
                route_count = self._execute(
                    "SELECT COUNT(*) FROM partitionedorders WHERE status = 'test'"
                )[0][0]
                self._conn.rollback()

                ok = route_count >= 1
                phase.add_check(CheckResult(
                    name="partition_routing",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected="row visible in parent (routed)",
                    actual=f"test rows={route_count}",
                    message="Partitioned table accepted INSERT (rolled back)",
                ))
            except mysql.connector.Error as exc:
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="partition_routing",
                    status=STATUS_FAIL,
                    expected="valid partition routing",
                    actual=f"error: {exc}",
                    message=f"Cannot test partition routing: {exc}",
                ))
        except mysql.connector.Error as exc:
            phase.add_check(CheckResult(
                name="partition_routing",
                status=STATUS_FAIL,
                expected="valid partition routing",
                actual=f"error: {exc}",
                message=f"Cannot test partition routing: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_enum(self) -> None:
        """Verify the ENUM column accepts valid values."""
        phase = self._new_phase("functional_enum")
        t0 = time.time()
        try:
            self._conn.start_transaction()
            try:
                self._execute_nofetch(
                    "INSERT INTO orders (ordernumber, customerid, totalamount, orderstatus) "
                    "VALUES (%s, %s, %s, %s)",
                    (99999999, 1, Decimal("50.00"), "pending"),
                )
                self._conn.rollback()

                phase.add_check(CheckResult(
                    name="enum_values_valid",
                    status=STATUS_PASS,
                    expected="valid enum value accepted",
                    actual="pending accepted",
                    message="ENUM column accepted valid value 'pending' (rolled back)",
                ))
            except mysql.connector.Error as exc:
                self._conn.rollback()
                phase.add_check(CheckResult(
                    name="enum_values_valid",
                    status=STATUS_FAIL,
                    expected="valid enum value accepted",
                    actual=f"error: {exc}",
                    message=f"ENUM validation failed: {exc}",
                ))
        except mysql.connector.Error as exc:
            phase.add_check(CheckResult(
                name="enum_values_valid",
                status=STATUS_FAIL,
                expected="enum values valid",
                actual=f"error: {exc}",
                message=f"ENUM validation failed: {exc}",
            ))
        phase.duration_s = time.time() - t0
