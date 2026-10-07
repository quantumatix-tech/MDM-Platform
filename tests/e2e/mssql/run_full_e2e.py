#!/usr/bin/env python
"""Phase E — Full E2E pipeline runner.

Executes the complete MSSQL end-to-end migration validation flow in order:

    A → B → C1 → C2 → D → consolidated report

Each phase is delegated to the dedicated runner module.  The runner stops
on the first required failure and returns a non-zero exit code.

Usage::

    Set environment variables first::

        $env:SECRET_mssql_e2e_source_pass = "<sa-password>"
        $env:SECRET_mssql_e2e_target_pass = "<sa-password>"

    python tests/e2e/mssql/run_full_e2e.py          # full pipeline
    python tests/e2e/mssql/run_full_e2e.py --clean   # also reset both DBs first
    python tests/e2e/mssql/run_full_e2e.py --skip-cleanup  # skip Phase A
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

import pyodbc  # noqa: E402

from tests.e2e.mssql.run_phase_b import load_config  # noqa: E402
from tests.e2e.mssql.run_phase_c2 import run_migration as _run_migration  # noqa: E402
from tests.e2e.mssql.run_phase_d import _connect as _connect_to_db  # noqa: E402
from tests.e2e.mssql.setup.cleanup import cleanup_databases, is_cleanup_safe  # noqa: E402
from tests.e2e.mssql.setup.database import (  # noqa: E402
    _resolve_connection_params,
    make_conn_str,
)
from tests.e2e.mssql.setup.fixture_loader import load_fixture  # noqa: E402
from tests.e2e.mssql.setup.target import reset_target_database  # noqa: E402
from tests.e2e.mssql.validation.catalog import MSSQLCatalog  # noqa: E402
from tests.e2e.mssql.validation.comparator import SourceTargetComparator  # noqa: E402
from tests.e2e.mssql.validation.functional import FunctionalValidator  # noqa: E402
from tests.e2e.mssql.validation.models import (  # noqa: E402
    STATUS_FAIL,
    STATUS_PASS,
    E2EConsolidatedReport,
    E2EPhaseSummary,
)
from tests.e2e.mssql.validation.target_pre import TargetPreValidator  # noqa: E402
from tests.e2e.mssql.validation.target_validator import TargetValidator  # noqa: E402

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


# --------------------------------------------------------------------------- #
# Phase runners (thin wrappers that return E2EPhaseSummary)
# --------------------------------------------------------------------------- #

def _run_phase_a(config: dict) -> E2EPhaseSummary:
    """Phase A — reset databases + load fixtures into source."""
    summary = E2EPhaseSummary(
        name="A",
        description="E2E DB Setup + Fixture Load",
        details={},
    )
    t0 = time.time()

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]
    pattern = config["e2e"]["safe_db_pattern"]
    fixture_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), config["e2e"]["fixture_path"])
    )
    safe, reasons = is_cleanup_safe([source_db, target_db], pattern)
    if not safe:
        summary.status = STATUS_FAIL
        summary.failed_checks = 1
        summary.total_checks = 1
        summary.details["error"] = "; ".join(reasons)
        summary.duration_s = time.time() - t0
        print(f"  [A] FAIL: safety check failed: {'; '.join(reasons)}")
        return summary

    params = _resolve_connection_params(
        host=config["source"]["connection"]["host"],
        port=config["source"]["connection"]["port"],
        username=config["source"]["connection"]["username"],
        password_env=config["source"]["connection"]["password_secret"],
    )

    print("[A] Resetting source database:", source_db)
    cleanup_databases(
        params["host"], params["port"], params["username"], params["password"],
        [source_db, target_db], pattern,
    )

    print("[A] Loading fixture into source:", fixture_path)
    result = load_fixture(
        fixture_dir=fixture_path,
        host=params["host"],
        port=params["port"],
        username=params["username"],
        password_env=config["source"]["connection"]["password_secret"],
        database=source_db,
    )

    total = len(result.results)
    failed = len(result.failed_scripts)
    summary.total_checks = total
    summary.passed_checks = total - failed
    summary.failed_checks = failed
    summary.duration_s = time.time() - t0

    if result.all_ok:
        summary.status = STATUS_PASS
        print(f"  [A] PASS — {total}/{total} scripts loaded")
    else:
        summary.status = STATUS_FAIL
        summary.details["failures"] = result.failed_scripts
        print(f"  [A] FAIL — {failed} of {total} scripts failed")

    return summary


def _run_phase_b(config: dict) -> E2EPhaseSummary:
    """Phase B — source structural validation."""
    summary = E2EPhaseSummary(
        name="B", description="Source Structural Validation"
    )
    t0 = time.time()

    conn_cfg = config["source"]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"], port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )
    source_db = config["e2e"]["source_database"]

    conn_str = make_conn_str(
        params["host"], params["port"], params["username"],
        params["password"], database=source_db,
    )
    conn = pyodbc.connect(conn_str)
    catalog = MSSQLCatalog(conn)

    from tests.e2e.mssql.validation.source_validator import SourceValidator
    validator = SourceValidator(catalog, database_name=source_db)
    report = validator.validate()
    conn.close()

    summary.total_checks = report.summary["PASS"] + report.summary["FAIL"]
    summary.passed_checks = report.summary["PASS"]
    summary.failed_checks = report.summary["FAIL"]
    summary.duration_s = report.total_duration_s
    summary.status = STATUS_PASS if report.passed else STATUS_FAIL

    src_rows = {}
    for phase in report.phases:
        if phase.name == "row_counts":
            for check in phase.checks:
                src_rows[check.name.replace("rows:", "")] = check.actual

    summary.details["report"] = report.to_dict()
    summary.details["row_counts"] = src_rows
    summary.duration_s = time.time() - t0
    print(f"  [B] {'PASS' if report.passed else 'FAIL'} — "
          f"{summary.passed_checks}/{summary.total_checks} checks")
    return summary


def _run_phase_c1(config: dict) -> E2EPhaseSummary:
    """Phase C1 — reset target + pre-migration validation."""
    summary = E2EPhaseSummary(
        name="C1", description="Target Setup + Pre-Migration Validation"
    )
    t0 = time.time()

    conn_cfg = config["target"]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"], port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )
    target_db = config["e2e"]["target_database"]
    pattern = config["e2e"]["safe_db_pattern"]

    print("[C1] Resetting target database:", target_db)
    try:
        reset_target_database(
            params["host"], params["port"], params["username"],
            params["password"], target_db, pattern,
        )
    except ValueError as e:
        summary.status = STATUS_FAIL
        summary.total_checks = 1
        summary.failed_checks = 1
        summary.details["error"] = str(e)
        summary.duration_s = time.time() - t0
        print(f"  [C1] FAIL: {e}")
        return summary

    conn_str = make_conn_str(
        params["host"], params["port"], params["username"],
        params["password"], database=target_db,
    )
    conn = pyodbc.connect(conn_str)
    validator = TargetPreValidator(conn, target_db)
    report = validator.validate()
    conn.close()

    summary.total_checks = report.summary["PASS"] + report.summary["FAIL"]
    summary.passed_checks = report.summary["PASS"]
    summary.failed_checks = report.summary["FAIL"]
    summary.duration_s = report.total_duration_s
    summary.status = STATUS_PASS if report.passed else STATUS_FAIL
    summary.details["report"] = report.to_dict()
    print(f"  [C1] {'PASS' if report.passed else 'FAIL'} — "
          f"{summary.passed_checks}/{summary.total_checks} checks")
    return summary


def _run_phase_c2(config: dict) -> E2EPhaseSummary:
    """Phase C2 — execute the real migration."""
    summary = E2EPhaseSummary(
        name="C2", description="Real Migration Execution"
    )
    t0 = time.time()

    try:
        result = _run_migration(config)
    except Exception as exc:  # noqa: BLE001
        summary.status = STATUS_FAIL
        summary.total_checks = 1
        summary.failed_checks = 1
        summary.duration_s = time.time() - t0
        summary.details["error"] = str(exc)
        summary.details["migration_status"] = "error"
        print(f"  [C2] FAIL: migration error: {exc}")
        return summary

    status = result.get("status", "unknown")
    phases = result.get("phases", {})
    objects = phases.get("discover", {}).get("objects", [])
    obj_count = len(objects) if isinstance(objects, list) else 0

    failures = []
    for obj_name, obj_phase in phases.items():
        if isinstance(obj_phase, dict):
            if obj_phase.get("success") == 0 and obj_phase.get("failure", 0) > 0:
                failures.append(f"{obj_name}: {obj_phase.get('failure')} failure(s)")

    summary.total_checks = obj_count
    summary.passed_checks = obj_count - len(failures)
    summary.failed_checks = len(failures)
    summary.duration_s = time.time() - t0
    summary.status = STATUS_PASS if status in ("success",) else STATUS_FAIL
    summary.details["migration_status"] = status
    summary.details["object_count"] = obj_count
    summary.details["phase_summaries"] = {
        name: (pv if isinstance(pv, str) else "ok")
        for name, pv in phases.items()
    }
    if failures:
        summary.details["failures"] = failures

    print(f"  [C2] {'PASS' if status in ('success',) else 'FAIL'} — "
          f"migration status: {status}, {obj_count} objects")
    if failures:
        for f in failures:
            print(f"       {f}")
    return summary


def _run_phase_d(
    config: dict, src_row_counts: dict[str, int]
) -> tuple[E2EPhaseSummary, dict, dict, dict]:
    """Phase D — structural + comparison + functional validation.

    Returns the Phase D summary plus the three sub-reports as dicts.
    """
    summary = E2EPhaseSummary(
        name="D", description="Target Validation + S/T Comparison + Functional"
    )
    t0 = time.time()

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]

    src_conn = _connect_to_db(config, "source", source_db)
    tgt_conn = _connect_to_db(config, "target", target_db)

    src_cat = MSSQLCatalog(src_conn)
    tgt_cat = MSSQLCatalog(tgt_conn)

    # --- Structural validation ---
    target_validator = TargetValidator(tgt_cat, database_name=target_db)
    target_report = target_validator.validate()

    # --- Comparison ---
    comparator = SourceTargetComparator(src_cat, tgt_cat)
    comparator.compare()
    comparison_report = comparator.to_report()

    # --- Functional validation ---
    func_validator = FunctionalValidator(tgt_conn, database_name=target_db)
    func_report = func_validator.validate()

    src_conn.close()
    tgt_conn.close()

    # Aggregate checks across the three sub-reports
    structural_total = target_report.summary["PASS"] + target_report.summary["FAIL"]
    comparison_total = comparison_report.summary["PASS"] + comparison_report.summary["FAIL"]
    functional_total = func_report.summary["PASS"] + func_report.summary["FAIL"]

    total = structural_total + comparison_total + functional_total
    passed = (
        target_report.summary["PASS"]
        + comparison_report.summary["PASS"]
        + func_report.summary["PASS"]
    )
    failed = (
        target_report.summary["FAIL"]
        + comparison_report.summary["FAIL"]
        + func_report.summary["FAIL"]
    )

    summary.total_checks = total
    summary.passed_checks = passed
    summary.failed_checks = failed
    summary.duration_s = time.time() - t0
    summary.status = STATUS_PASS if target_report.passed and comparison_report.passed \
        and func_report.passed else STATUS_FAIL

    # Extract target row counts from structural report
    tgt_rows: dict[str, int] = {}
    for phase in target_report.phases:
        if phase.name == "row_counts":
            for check in phase.checks:
                tbl = check.name.replace("rows:", "")
                tgt_rows[tbl] = check.actual

    summary.details["structural_validation"] = target_report.to_dict()
    summary.details["source_target_comparison"] = comparison_report.to_dict()
    summary.details["functional_validation"] = func_report.to_dict()
    summary.details["row_counts"] = {
        "source": src_row_counts,
        "target": tgt_rows,
    }

    print(f"  [D] {'PASS' if summary.passed else 'FAIL'} — "
          f"{passed}/{total} checks across structural/comparison/functional")
    return summary, target_report.to_dict(), comparison_report.to_dict(), func_report.to_dict()


# --------------------------------------------------------------------------- #
# Consolidated report builder
# --------------------------------------------------------------------------- #

def build_consolidated_report(
    config: dict,
    phase_a: E2EPhaseSummary,
    phase_b: E2EPhaseSummary,
    phase_c1: E2EPhaseSummary,
    phase_c2: E2EPhaseSummary,
    phase_d: E2EPhaseSummary,
) -> E2EConsolidatedReport:
    """Build the final consolidated report from per-phase summaries."""

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]
    conn_cfg = config["source"]["connection"]

    # Extract row counts
    src_rows = phase_b.details.get("row_counts", {})
    d_details = phase_d.details
    tgt_rows = d_details.get("row_counts", {}).get("target", {})

    # Collect missing/unexpected objects from Phase D comparison details
    comparison = d_details.get("source_target_comparison", {})
    missing: list[str] = []
    unexpected: list[str] = []
    mismatches: list[str] = []

    for phase in comparison.get("phases", []):
        checks = phase.get("checks", [])
        if checks:
            d = checks[0].get("details", {})
            missing.extend(d.get("missing", []))
            unexpected.extend(d.get("unexpected", []))
            mismatches.extend(d.get("mismatches", []))

    # Collect migration failures from Phase C2
    migration_failures = d_details.get("failures", [])
    # Also check C2 details
    if phase_c2.details.get("failures"):
        migration_failures.extend(phase_c2.details["failures"])

    report = E2EConsolidatedReport(
        source_database=source_db,
        target_database=target_db,
        server=f"{conn_cfg['host']},{conn_cfg['port']}",
        migration_status=phase_c2.details.get("migration_status", "unknown"),
        source_row_counts=src_rows,
        target_row_counts=tgt_rows,
        phases=[phase_a, phase_b, phase_c1, phase_c2, phase_d],
        migration_failures=migration_failures,
        missing_objects=missing,
        unexpected_objects=unexpected,
        metadata_mismatches=mismatches,
    )
    return report


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full MSSQL E2E pipeline: A → B → C1 → C2 → D → report."
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Reset both E2E databases before starting (runs cleanup).",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Skip phases; just consolidate existing reports from disk.",
    )
    args = parser.parse_args()

    config = load_config()
    print("=" * 70)
    print("FULL E2E PIPELINE: Phase A -> B -> C1 -> C2 -> D -> Consolidated Report")
    print("=" * 70)
    print(f"  Server:   {config['source']['connection']['host']},"
            f"{config['source']['connection']['port']}")
    print(f"  Source:   {config['e2e']['source_database']}")
    print(f"  Target:   {config['e2e']['target_database']}")
    print("=" * 70)

    os.makedirs(REPORTS_DIR, exist_ok=True)

    phase_a: E2EPhaseSummary
    phase_b: E2EPhaseSummary
    phase_c1: E2EPhaseSummary
    phase_c2: E2EPhaseSummary
    phase_d: E2EPhaseSummary

    if not args.report_only:
        # Phase A — setup
        print("\n[PHASE A] DB Setup + Fixture Load")
        phase_a = _run_phase_a(config)
        if not phase_a.passed:
            print("[ABORT] Phase A failed — cannot continue.")
            sys.exit(1)

        # Phase B — source validation
        print("\n[PHASE B] Source Structural Validation")
        phase_b = _run_phase_b(config)
        if not phase_b.passed:
            print("[ABORT] Phase B failed — source is not in expected state.")
            sys.exit(1)

        # Phase C1 — target reset + pre-validation
        print("\n[PHASE C1] Target Setup + Pre-Migration Validation")
        phase_c1 = _run_phase_c1(config)
        if not phase_c1.passed:
            print("[ABORT] Phase C1 failed — target is not clean.")
            sys.exit(1)

        # Phase C2 — real migration
        print("\n[PHASE C2] Real Migration Execution")
        phase_c2 = _run_phase_c2(config)
        if not phase_c2.passed:
            print("[ABORT] Phase C2 failed — migration did not succeed.")
            sys.exit(1)

        # Phase D — target validation + comparison + functional
        print("\n[PHASE D] Target Validation + S/T Comparison + Functional")
        phase_d, _, _, _ = _run_phase_d(config, phase_b.details.get("row_counts", {}))
        if not phase_d.passed:
            print("[ABORT] Phase D failed — target validation failed.")
            sys.exit(1)

    # --- Build consolidated report ---
    report = build_consolidated_report(
        config, phase_a, phase_b, phase_c1, phase_c2, phase_d
    )

    report_path = os.path.join(REPORTS_DIR, "e2e_consolidated_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            report.to_dict(), f, indent=2, ensure_ascii=False,
            default=lambda o: (
                str(o) if isinstance(o, (Decimal, datetime, date)) else o
            ),
        )

    # --- Terminal summary ---
    print("\n" + "=" * 70)
    print("CONSOLIDATED E2E REPORT")
    print("=" * 70)
    print(f"  Source Database:    {report.source_database}")
    print(f"  Target Database:    {report.target_database}")
    print(f"  SQL Server Endpoint: {report.server}")
    print(f"  Migration Status:   {report.migration_status}")
    print(f"  Source Row Counts:  {report.source_row_counts}")
    print(f"  Target Row Counts:  {report.target_row_counts}")
    print(f"  Overall Result:     {'PASS' if report.passed else 'FAIL'}")
    print(f"  Total Checks:       {report.total_checks}")
    print(f"  Passed:             {report.passed_checks}")
    print(f"  Failed:             {report.failed_checks}")

    print("\n  Phase Summary:")
    for p in report.phases:
        tag = "PASS" if p.passed else "FAIL"
        print(f"    [{tag}] Phase {p.name}: {p.description} "
              f"({p.passed_checks}/{p.total_checks} checks, {p.duration_s:.1f}s)")

    print(f"\n  Missing Objects:    {report.missing_objects or 'none'}")
    print(f"  Unexpected Objects: {report.unexpected_objects or 'none'}")
    print(f"  Metadata Mismatches:{report.metadata_mismatches or 'none'}")
    print(f"  Migration Failures: {report.migration_failures or 'none'}")

    print(f"\n  [REPORT] JSON: {report_path}")
    print("=" * 70)

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
