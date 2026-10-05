"""Validation result models for the MSSQL E2E framework.

These dataclasses are generic (not MSSQL-specific) so they can be reused
by future PostgreSQL / MySQL E2E validators.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"
STATUS_NOT_SUPPORTED = "NOT_SUPPORTED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

_OK = {STATUS_PASS, STATUS_SKIPPED, STATUS_NOT_SUPPORTED, STATUS_NOT_APPLICABLE}


@dataclass
class CheckResult:
    """Result of a single validation check."""

    name: str
    status: str
    expected: Any = None
    actual: Any = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in _OK


@dataclass
class PhaseResult:
    """Result of a validation phase (e.g. 'tables', 'views')."""

    name: str
    status: str = STATUS_PASS
    checks: list[CheckResult] = field(default_factory=list)
    duration_s: float = 0.0

    def add_check(self, result: CheckResult) -> None:
        self.checks.append(result)
        if result.status == STATUS_FAIL:
            self.status = STATUS_FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_s": round(self.duration_s, 3),
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "expected": c.expected,
                    "actual": c.actual,
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


@dataclass
class ValidationReport:
    """Full validation report across all phases."""

    database: str = ""
    phases: list[PhaseResult] = field(default_factory=list)
    total_duration_s: float = 0.0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def passed(self) -> bool:
        return all(p.status != STATUS_FAIL for p in self.phases)

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {"PASS": 0, "FAIL": 0, "SKIPPED": 0,
                                  "NOT_SUPPORTED": 0, "NOT_APPLICABLE": 0}
        for phase in self.phases:
            for check in phase.checks:
                counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "PASS" if self.passed else "FAIL",
            "database": self.database,
            "created_at": self.created_at,
            "total_duration_s": round(self.total_duration_s, 3),
            "summary": self.summary,
            "phases": [p.to_dict() for p in self.phases],
        }


# ---------------------------------------------------------------------------
# E2E consolidation models
# ---------------------------------------------------------------------------

@dataclass
class E2EPhaseSummary:
    """Summary of one E2E phase (A, B, C1, C2, D) for the consolidated report."""

    name: str           # e.g. "A", "B", "C1", "C2", "D"
    description: str     # human-readable phase label
    status: str = STATUS_PASS
    duration_s: float = 0.0
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in _OK


@dataclass
class E2EConsolidatedReport:
    """Consolidated E2E report covering phases A through D.

    Aggregates results from every E2E phase into a single authoritative
    artefact.  Reuses ``ValidationReport`` / ``ComparisonCategory`` data
    where possible rather than duplicating their internal structure.
    """

    source_database: str = ""
    target_database: str = ""
    server: str = ""
    migration_status: str = ""
    source_row_counts: dict[str, int] = field(default_factory=dict)
    target_row_counts: dict[str, int] = field(default_factory=dict)
    phases: list[E2EPhaseSummary] = field(default_factory=list)
    migration_failures: list[str] = field(default_factory=list)
    missing_objects: list[str] = field(default_factory=list)
    unexpected_objects: list[str] = field(default_factory=list)
    metadata_mismatches: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def passed(self) -> bool:
        return all(p.passed for p in self.phases)

    @property
    def total_checks(self) -> int:
        return sum(p.total_checks for p in self.phases)

    @property
    def passed_checks(self) -> int:
        return sum(p.passed_checks for p in self.phases)

    @property
    def failed_checks(self) -> int:
        return sum(p.failed_checks for p in self.phases)

    @property
    def total_duration_s(self) -> float:
        return sum(p.duration_s for p in self.phases)

    def to_dict(self) -> dict[str, Any]:
        d_details = self._d_phase_details()
        return {
            "status": "PASS" if self.passed else "FAIL",
            "source_database": self.source_database,
            "target_database": self.target_database,
            "server": self.server,
            "migration_status": self.migration_status,
            "source_row_counts": self.source_row_counts,
            "target_row_counts": self.target_row_counts,
            "created_at": self.created_at,
            "total_duration_s": round(self.total_duration_s, 3),
            "overall": {
                "total_checks": self.total_checks,
                "passed_checks": self.passed_checks,
                "failed_checks": self.failed_checks,
            },
            "structural_validation": {
                "status": self._d_phase_status(),
                **(d_details.get("structural_validation") or {}),
            },
            "source_target_comparison": {
                "status": self._d_phase_status(),
                **(d_details.get("source_target_comparison") or {}),
            },
            "functional_validation": {
                "status": self._d_phase_status(),
                **(d_details.get("functional_validation") or {}),
            },
            "row_counts": d_details.get("row_counts", {}),
            "migration_failures": self.migration_failures,
            "missing_objects": self.missing_objects,
            "unexpected_objects": self.unexpected_objects,
            "metadata_mismatches": self.metadata_mismatches,
            "phases": [
                {
                    "name": p.name,
                    "description": p.description,
                    "status": p.status,
                    "duration_s": round(p.duration_s, 3),
                    "checks": {
                        "total": p.total_checks,
                        "passed": p.passed_checks,
                        "failed": p.failed_checks,
                    },
                    "details": p.details,
                }
                for p in self.phases
            ],
        }

    def _d_phase_status(self) -> str:
        d_phase = next((p for p in self.phases if p.name == "D"), None)
        return d_phase.status if d_phase else "NOT RUN"

    def _d_phase_details(self) -> dict[str, Any]:
        d_phase = next((p for p in self.phases if p.name == "D"), None)
        return d_phase.details if d_phase else {}
