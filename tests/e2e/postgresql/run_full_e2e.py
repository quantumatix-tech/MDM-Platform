#!/usr/bin/env python
"""Phase E — Full E2E pipeline runner for PostgreSQL.

Executes the complete PostgreSQL end-to-end migration validation flow in order:

    A → B → C1 → C2 → D → consolidated report

Each phase is delegated to the dedicated runner module.  The runner stops
on the first required failure and returns a non-zero exit code.

Usage::

    Set environment variables first::

        $env:SECRET_postgresql_e2e_source_pass = "<postgres-password>"
        $env:SECRET_postgresql_e2e_target_pass = "<postgres-password>"

    python tests/e2e/postgresql/run_full_e2e.py          # full pipeline
    python tests/e2e/postgresql/run_full_e2e.py --clean   # also reset both DBs first
    python tests/e2e/postgresql/run_full_e2e.py --skip-cleanup  # skip Phase A reset
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

import psycopg

from tests.e2e.postgresql.run_phase_a import load_config
from tests.e2e.postgresql.run_phase_c2 import run_migration as _run_migration
from tests.e2e.postgresql.setup.cleanup import cleanup_databases, is_cleanup_safe
from tests.e2e.postgresql.setup.database import (
    _resolve_connection_params,
)
from tests.e2e.postgresql.setup.fixture_loader import load_fixture
from tests.e2e.postgresql.setup.target import reset_target_database
from tests.e2e.postgresql.validation.catalog import PostgreSQLCatalog
from tests.e2e.postgresql.validation.comparator import SourceTargetComparator
from tests.e2e.postgresql.validation.functional import FunctionalValidator
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    E2EConsolidatedReport,
    E2EPhaseSummary,
)
from tests.e2e.postgresql.validation.target_validator import TargetValidator

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def _connect_to_db(config: dict, role: str, database: str):
    """Open a psycopg connection to *database* using role (source|target)."""
    conn_cfg = config[role]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"],
        port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )
    return psycopg.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password", ""),
        dbname=database,
        connect_timeout=10,
    )


def _run_phase_a(config: dict, skip_cleanup: bool = False) -> E2EPhaseSummary:
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

    if not skip_cleanup:
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
        print("[A] Resetting databases:", source_db, target_db)
        cleanup_databases(
            params["host"], params["port"], params["user"], params["password"],
            [source_db, target_db], pattern,
        )

    # If --skip-cleanup, still ensure the source DB exists (target may be stale)
    params = _resolve_connection_params(
        host=config["source"]["connection"]["host"],
        port=config["source"]["connection"]["port"],
        username=config["source"]["connection"]["username"],
        password_env=config["source"]["connection"]["password_secret"],
    )

    fixture_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), config["e2e"]["fixture_path"])
    )
    print("[A] Loading fixture into source:", fixture_path)
    result = load_fixture(
        fixture_dir=fixture_path,
        host=params["host"],
        port=params["port"],
        username=params["user"],
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

    try:
        conn = psycopg.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params.get("password", ""),
            dbname=source_db,
            connect_timeout=10,
        )
    except psycopg.Error as e:
        summary.status = STATUS_FAIL
        summary.total_checks = 1
        summary.failed_checks = 1
        summary.duration_s = time.time() - t0
        summary.details["error"] = str(e)
        print(f"  [B] FAIL: connection error: {e}")
        return summary

    catalog = PostgreSQLCatalog(conn)
    from tests.e2e.postgresql.validation.source_validator import SourceValidator
    validator = SourceValidator(catalog, database_name=source_db)
    report = validator.validate()
    conn.close()

    summary.total_checks = report.summary["PASS"] + report.summary["FAIL"]
    summary.passed_checks = report.summary["PASS"]
    summary.failed_checks = report.summary["FAIL"]
    summary.duration_s = time.time() - t0
    summary.status = STATUS_PASS if report.passed else STATUS_FAIL

    src_rows: dict[str, int] = {}
    for phase in report.phases:
        if phase.name == "row_counts":
            for check in phase.checks:
                src_rows[check.name.replace("rows:", "")] = check.actual

    summary.details["report"] = report.to_dict()
    summary.details["row_counts"] = src_rows
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
            params["host"], params["port"], params["user"],
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
    except (psycopg.Error, OSError) as e:
        summary.status = STATUS_FAIL
        summary.total_checks = 1
        summary.failed_checks = 1
        summary.details["error"] = str(e)
        summary.duration_s = time.time() - t0
        print(f"  [C1] FAIL: reset error: {e}")
        return summary

    try:
        conn = psycopg.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params.get("password", ""),
            dbname=target_db,
            connect_timeout=10,
        )
    except psycopg.Error as e:
        summary.status = STATUS_FAIL
        summary.total_checks = 1
        summary.failed_checks = 1
        summary.duration_s = time.time() - t0
        summary.details["error"] = str(e)
        print(f"  [C1] FAIL: connection error: {e}")
        return summary

    from tests.e2e.postgresql.validation.target_pre import TargetPreValidator
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

    failures: list[str] = []
    for obj_name, obj_phase in phases.items():
        if isinstance(obj_phase, dict) and obj_phase.get("failure", 0) > 0 and obj_name != "discover":
            failures.append(f"{obj_name}: {obj_phase.get('failure')} failure(s)")

    summary.total_checks = obj_count
    summary.passed_checks = obj_count - len(failures)
    summary.failed_checks = len(failures)
    summary.duration_s = time.time() - t0
    summary.status = STATUS_PASS if status in ("success", "completed") else STATUS_FAIL
    summary.details["migration_status"] = status
    summary.details["object_count"] = obj_count
    summary.details["phase_summaries"] = {
        name: (pv if isinstance(pv, str) else "ok")
        for name, pv in phases.items()
    }
    if failures:
        summary.details["failures"] = failures

    print(f"  [C2] {'PASS' if summary.status == STATUS_PASS else 'FAIL'} — "
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

    src_cat = PostgreSQLCatalog(src_conn)
    tgt_cat = PostgreSQLCatalog(tgt_conn)

    target_validator = TargetValidator(tgt_cat, database_name=target_db)
    target_report = target_validator.validate()

    comparator = SourceTargetComparator(src_cat, tgt_cat)
    comparator.compare()
    comparison_report = comparator.to_report()

    func_validator = FunctionalValidator(tgt_conn, database_name=target_db)
    func_report = func_validator.validate()

    src_conn.close()
    tgt_conn.close()

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
    summary.status = (
        STATUS_PASS
        if target_report.passed and comparison_report.passed and func_report.passed
        else STATUS_FAIL
    )

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


def _run_phase_d_from_report(
    config: dict, src_row_counts: dict[str, int]
) -> tuple[E2EPhaseSummary, dict, dict, dict]:
    """Phase D fallback — re-run validation from a fresh connection.

    This is an alias for _run_phase_d but accepts the same signature, allowing
    report-only mode to still exercise live validation if desired.
    """
    return _run_phase_d(config, src_row_counts)


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

    src_rows = phase_b.details.get("row_counts", {})
    d_details = phase_d.details
    tgt_rows = d_details.get("row_counts", {}).get("target", {})

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

    migration_failures: list[str] = []
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


def _cleanup_after_pipeline(config: dict, cleanup_mode: str) -> None:
    """Perform post-pipeline cleanup based on the requested mode.

    Modes:
      on_success — clean only safe E2E databases if all phases passed.
      always     — always clean up safe E2E databases.
      never      — do not clean up; preserve target for debugging.
    """
    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]
    pattern = config["e2e"]["safe_db_pattern"]

    if cleanup_mode == "never":
        print("\n[CLEANUP] Cleanup disabled (--no-cleanup). Target preserved.")
        return

    do_cleanup = cleanup_mode == "always"
    if cleanup_mode == "on_success":
        # Caller (main) handles this decision and calls us only on success
        do_cleanup = True

    if do_cleanup:
        params = _resolve_connection_params(
            host=config["source"]["connection"]["host"],
            port=config["source"]["connection"]["port"],
            username=config["source"]["connection"]["username"],
            password_env=config["source"]["connection"]["password_secret"],
        )
        print("\n[CLEANUP] Resetting E2E databases (on_success mode)...")
        cleanup_databases(
            params["host"], params["port"], params["user"], params["password"],
            [source_db, target_db], pattern,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full PostgreSQL E2E pipeline: A → B → C1 → C2 → D → report."
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Reset both E2E databases before starting (runs cleanup).",
    )
    parser.add_argument(
        "--skip-cleanup", action="store_true",
        help="Skip Phase A database reset + fixture reload (assumes DBs already set up).",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Skip phases; just consolidate existing reports from disk.",
    )
    parser.add_argument(
        "--keep-target", action="store_true",
        help="Do not clean up the target database on success (for debugging).",
    )
    parser.add_argument(
        "--no-cleanup", action="store_true",
        help="Disable all cleanup; preserve databases after the run.",
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
        print("\n[PHASE A] DB Setup + Fixture Load")
        phase_a = _run_phase_a(config, skip_cleanup=args.skip_cleanup)
        if not phase_a.passed:
            print("[ABORT] Phase A failed — cannot continue.")
            _cleanup_on_failure(config, args)
            sys.exit(1)

        print("\n[PHASE B] Source Structural Validation")
        phase_b = _run_phase_b(config)
        if not phase_b.passed:
            print("[ABORT] Phase B failed — source is not in expected state.")
            _cleanup_on_failure(config, args)
            sys.exit(1)

        print("\n[PHASE C1] Target Setup + Pre-Migration Validation")
        phase_c1 = _run_phase_c1(config)
        if not phase_c1.passed:
            print("[ABORT] Phase C1 failed — target is not clean.")
            _cleanup_on_failure(config, args)
            sys.exit(1)

        print("\n[PHASE C2] Real Migration Execution")
        phase_c2 = _run_phase_c2(config)
        if not phase_c2.passed:
            print("[ABORT] Phase C2 failed — migration did not succeed.")
            _cleanup_on_failure(config, args)
            sys.exit(1)

        print("\n[PHASE D] Target Validation + S/T Comparison + Functional")
        phase_d, _, _, _ = _run_phase_d(config, phase_b.details.get("row_counts", {}))
        if not phase_d.passed:
            print("[ABORT] Phase D failed — target validation failed.")
            _cleanup_on_failure(config, args)
            sys.exit(1)

    # --- Build consolidated report ---
    report = build_consolidated_report(
        config, phase_a, phase_b, phase_c1, phase_c2, phase_d
    )

    report_path = os.path.join(REPORTS_DIR, "postgresql_e2e_report.json")
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
    print(f"  PostgreSQL Endpoint: {report.server}")
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
    print(f"  Metadata Mismatches: {report.metadata_mismatches or 'none'}")
    print(f"  Migration Failures: {report.migration_failures or 'none'}")

    print(f"\n  [REPORT] JSON: {report_path}")
    print("=" * 70)

    # --- Cleanup on success (on_success mode by default) ---
    if args.no_cleanup:
        print("\n[CLEANUP] Cleanup disabled (--no-cleanup). All databases preserved.")
    elif args.keep_target:
        print("\n[CLEANUP] Target preserved (--keep-target). Source will be cleaned.")
        _cleanup_partial(config)
    else:
        _cleanup_after_pipeline(config, "on_success")

    sys.exit(0 if report.passed else 1)


def _cleanup_on_failure(config: dict, args: argparse.Namespace) -> None:
    """Preserve target database on failure (unless --no-cleanup is also set)."""
    if args.no_cleanup:
        return
    source_db = config["e2e"]["source_database"]
    pattern = config["e2e"]["safe_db_pattern"]
    params = _resolve_connection_params(
        host=config["source"]["connection"]["host"],
        port=config["source"]["connection"]["port"],
        username=config["source"]["connection"]["username"],
        password_env=config["source"]["connection"]["password_secret"],
    )
    print("[CLEANUP] Preserving target for debugging; cleaning source only...")
    try:
        cleanup_databases(
            params["host"], params["port"], params["user"], params["password"],
            [source_db], pattern,
        )
    except ValueError as e:
        print(f"[CLEANUP] Skipped source cleanup: {e}")


def _cleanup_partial(config: dict) -> None:
    """Clean source only, preserve target (used with --keep-target)."""
    source_db = config["e2e"]["source_database"]
    pattern = config["e2e"]["safe_db_pattern"]
    params = _resolve_connection_params(
        host=config["source"]["connection"]["host"],
        port=config["source"]["connection"]["port"],
        username=config["source"]["connection"]["username"],
        password_env=config["source"]["connection"]["password_secret"],
    )
    try:
        cleanup_databases(
            params["host"], params["port"], params["user"], params["password"],
            [source_db], pattern,
        )
    except ValueError as e:
        print(f"[CLEANUP] Skipped: {e}")


if __name__ == "__main__":
    main()
