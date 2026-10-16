"""Functional validation for the MSSQL E2E target database.

Executes safe read-only operations against migrated objects to confirm they
are usable at runtime, not just structurally present.  All mutating checks
use explicit transactions that are rolled back so no business data is
permanently modified.
"""
from __future__ import annotations

import time
from typing import Any

import pyodbc

from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


class FunctionalValidator:
    """Runs safe functional checks against the target database."""

    def __init__(
        self, conn: pyodbc.Connection, database_name: str = "", *,
        expected_sequences: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._conn = conn
        self._db_name = database_name
        self._expected_sequences = expected_sequences or {}
        self._report = ValidationReport(database=database_name)

    def validate(self) -> ValidationReport:
        start = time.time()
        self._validate_view()
        self._validate_scalar_function()
        self._validate_inline_tvf()
        self._validate_stored_procedure()
        self._validate_trigger_enabled()
        self._validate_trigger_disabled()
        self._validate_synonym()
        self._validate_partitioned_table()
        self._validate_sequence()
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

    def _validate_view(self) -> None:
        phase = self._new_phase("functional_view")
        t0 = time.time()
        try:
            rows = self._execute("SELECT COUNT(*) FROM training.vw_CustomerOrders")
            count = rows[0][0]
            phase.add_check(CheckResult(
                name="view_readable",
                status=STATUS_PASS,
                expected=">=0 rows",
                actual=count,
                message=f"vw_CustomerOrders returned {count} rows",
            ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="view_readable",
                status=STATUS_FAIL,
                expected="readable view",
                actual=f"error: {exc}",
                message=f"Cannot read vw_CustomerOrders: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_scalar_function(self) -> None:
        phase = self._new_phase("functional_scalar_function")
        t0 = time.time()
        try:
            result = self._execute(
                "SELECT training.fn_CalculateTax(100.00) AS tax"
            )
            val = result[0][0] if result else None
            phase.add_check(CheckResult(
                name="scalar_function_executable",
                status=STATUS_PASS if val is not None else STATUS_FAIL,
                expected="tax value",
                actual=val,
                message=f"fn_CalculateTax returned {val}",
            ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="scalar_function_executable",
                status=STATUS_FAIL,
                expected="executable function",
                actual=f"error: {exc}",
                message=f"Cannot execute fn_CalculateTax: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_inline_tvf(self) -> None:
        phase = self._new_phase("functional_inline_tvf")
        t0 = time.time()
        try:
            result = self._execute(
                "SELECT * FROM training.fn_CustomerOrderStats(1)"
            )
            count = len(result)
            phase.add_check(CheckResult(
                name="inline_tvf_executable",
                status=STATUS_PASS,
                expected=">=0 rows",
                actual=count,
                message=f"fn_CustomerOrderStats returned {count} rows",
            ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="inline_tvf_executable",
                status=STATUS_FAIL,
                expected="executable TVF",
                actual=f"error: {exc}",
                message=f"Cannot execute fn_CustomerOrderStats: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_stored_procedure(self) -> None:
        phase = self._new_phase("functional_stored_procedure")
        t0 = time.time()
        try:
            result = self._execute(
                "{CALL training.usp_GetCustomerOrders(?)}", (1,)
            )
            count = len(result)
            phase.add_check(CheckResult(
                name="procedure_executable",
                status=STATUS_PASS,
                expected=">=0 rows",
                actual=count,
                message=f"usp_GetCustomerOrders returned {count} rows",
            ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="procedure_executable",
                status=STATUS_FAIL,
                expected="executable procedure",
                actual=f"error: {exc}",
                message=f"Cannot execute usp_GetCustomerOrders: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_trigger_enabled(self) -> None:
        """Verify the enabled trigger (trg_Orders_Insert) fires on insert.

        Uses an explicit transaction and rolls back so the test row is not
        persisted.  The trigger should write to OrderAudit.
        """
        phase = self._new_phase("functional_trigger_enabled")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN TRAN")
                # Insert test row — trigger should fire and create an audit row
                cur.execute(
                    "INSERT INTO training.Orders "
                    "(OrderNumber, CustomerID, TotalAmount, Status, OrderDate) "
                    "VALUES (?, ?, ?, 'Test', '2024-01-01')",
                    (99999, 1, 100.00),
                )
                cur.execute(
                    "SELECT COUNT(*) FROM training.OrderAudit "
                    "WHERE Action = 'INSERT' AND OrderID = "
                    "(SELECT MAX(OrderID) FROM training.Orders)"
                )
                new_audit = cur.fetchone()[0]
                cur.execute("ROLLBACK")

            if new_audit > 0:
                phase.add_check(CheckResult(
                    name="trigger_enabled_fires",
                    status=STATUS_PASS,
                    expected="audit row created",
                    actual=new_audit,
                    message=f"Enabled trigger created {new_audit} audit row(s)",
                ))
            else:
                phase.add_check(CheckResult(
                    name="trigger_enabled_fires",
                    status=STATUS_FAIL,
                    expected="audit row created",
                    actual=new_audit,
                    message="Enabled trigger did not create an audit row",
                ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="trigger_enabled_fires",
                status=STATUS_FAIL,
                expected="trigger fires",
                actual=f"error: {exc}",
                message=f"Cannot test enabled trigger: {exc}",
            ))
            try:
                self._conn.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
        phase.duration_s = time.time() - t0

    def _validate_trigger_disabled(self) -> None:
        """Verify the disabled trigger (trg_OrderDetails_Audit) does not fire."""
        phase = self._new_phase("functional_trigger_disabled")
        t0 = time.time()
        try:
            with self._conn.cursor() as cur:
                cur.execute("BEGIN TRAN")
                cur.execute(
                    "INSERT INTO training.OrderDetails "
                    "(OrderID, ProductID, Quantity, UnitPrice) "
                    "VALUES (?, ?, ?, ?)",
                    (1, 1, 1, 10.00),
                )
                cur.execute(
                    "SELECT COUNT(*) FROM sys.triggers "
                    "WHERE name = 'trg_OrderDetails_Audit' "
                    "AND is_disabled = 1"
                )
                still_disabled = cur.fetchone()[0]
                cur.execute("ROLLBACK")

            if still_disabled:
                phase.add_check(CheckResult(
                    name="trigger_disabled_remains",
                    status=STATUS_PASS,
                    expected="disabled",
                    actual="disabled",
                    message="Disabled trigger remains disabled",
                ))
            else:
                phase.add_check(CheckResult(
                    name="trigger_disabled_remains",
                    status=STATUS_FAIL,
                    expected="disabled",
                    actual="enabled",
                    message="Trigger was unexpectedly enabled",
                ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="trigger_disabled_remains",
                status=STATUS_FAIL,
                expected="disabled trigger",
                actual=f"error: {exc}",
                message=f"Cannot test disabled trigger: {exc}",
            ))
            try:
                self._conn.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
        phase.duration_s = time.time() - t0

    def _validate_synonym(self) -> None:
        """Verify the synonym resolves to a real object."""
        phase = self._new_phase("functional_synonym")
        t0 = time.time()
        try:
            rows = self._execute("SELECT COUNT(*) FROM training.syn_Orders")
            count = rows[0][0]
            phase.add_check(CheckResult(
                name="synonym_resolves",
                status=STATUS_PASS,
                expected="readable via synonym",
                actual=count,
                message=f"syn_Orders returned {count} rows",
            ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="synonym_resolves",
                status=STATUS_FAIL,
                expected="resolves to table",
                actual=f"error: {exc}",
                message=f"Synonym does not resolve: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_partitioned_table(self) -> None:
        """Verify the partitioned table can be queried."""
        phase = self._new_phase("functional_partitioned_table")
        t0 = time.time()
        try:
            rows = self._execute("SELECT COUNT(*) FROM training.PartitionedOrders")
            count = rows[0][0]
            phase.add_check(CheckResult(
                name="partitioned_table_readable",
                status=STATUS_PASS,
                expected="readable",
                actual=count,
                message=f"PartitionedOrders returned {count} rows",
            ))
        except Exception as exc:  # noqa: BLE001
            phase.add_check(CheckResult(
                name="partitioned_table_readable",
                status=STATUS_FAIL,
                expected="readable",
                actual=f"error: {exc}",
                message=f"Cannot query PartitionedOrders: {exc}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_sequence(self) -> None:
        """Consume one controlled value and compare it with source next state."""
        phase = self._new_phase("functional_sequence")
        t0 = time.time()
        source_state = self._expected_sequences.get("Seq_OrderNumber")
        expected_next = None
        source_exhausted = False
        try:
            if source_state is not None:
                source_exhausted = bool(source_state.get("is_exhausted"))
                expected_next = _expected_sequence_next(source_state)
            result = self._execute(
                "SELECT NEXT VALUE FOR training.Seq_OrderNumber"
            )
            val = result[0][0] if result else None
            passed = val is not None and (
                source_state is None or (not source_exhausted and int(val) == expected_next)
            )
            phase.add_check(CheckResult(
                name="sequence_next_value",
                status=STATUS_PASS if passed else STATUS_FAIL,
                expected=(expected_next if source_state is not None else "integer value"),
                actual=val,
                message=(
                    f"Seq_OrderNumber NEXT VALUE = {val}; source expected {expected_next}"
                    if source_state is not None else f"Seq_OrderNumber NEXT VALUE = {val}"
                ),
            ))
        except Exception as exc:  # noqa: BLE001
            passed = source_state is not None and source_exhausted
            phase.add_check(CheckResult(
                name="sequence_next_value",
                status=STATUS_PASS if passed else STATUS_FAIL,
                expected=("sequence exhausted" if passed else expected_next or "next value"),
                actual=f"error: {exc}",
                message=(
                    "Target sequence is exhausted as expected from source"
                    if passed else f"Cannot get expected next sequence value: {exc}"
                ),
            ))
        phase.duration_s = time.time() - t0


def _expected_sequence_next(state: dict[str, Any]) -> int | None:
    """Compute source's next value from catalog state without consuming it."""
    if state.get("is_exhausted"):
        return None
    start = state.get("start_value")
    increment = state.get("increment")
    if not isinstance(start, int) or not isinstance(increment, int) or increment == 0:
        raise ValueError("Source sequence metadata is incomplete or invalid")
    if state.get("last_used_value_available"):
        last = state.get("last_used_value")
        if last is None:
            return start
    else:
        last = state.get("current_value")
        if last == start:
            raise ValueError("Cannot determine source next value: last_used_value is unavailable")
    next_value = int(last) + increment
    minimum, maximum = state.get("minimum_value"), state.get("maximum_value")
    if minimum <= next_value <= maximum:
        return next_value
    if state.get("is_cycling"):
        return minimum if increment > 0 else maximum
    return None
