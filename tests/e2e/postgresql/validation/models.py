"""Validation result models for the PostgreSQL E2E framework.

Re-exports the generic dataclasses from the MSSQL validation layer — these
models are engine-neutral (CheckResult, PhaseResult, ValidationReport, etc.)
so the PostgreSQL validator can reuse the same result shape without
duplicating the dataclass definitions.
"""
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_SUPPORTED,
    STATUS_PASS,
    STATUS_SKIPPED,
    CheckResult,
    E2EConsolidatedReport,
    E2EPhaseSummary,
    PhaseResult,
    ValidationReport,
)

__all__ = [
    "STATUS_FAIL",
    "STATUS_NOT_APPLICABLE",
    "STATUS_NOT_SUPPORTED",
    "STATUS_PASS",
    "STATUS_SKIPPED",
    "CheckResult",
    "E2EConsolidatedReport",
    "E2EPhaseSummary",
    "PhaseResult",
    "ValidationReport",
]
