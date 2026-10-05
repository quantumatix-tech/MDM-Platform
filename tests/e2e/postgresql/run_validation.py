#!/usr/bin/env python
"""Acceptance E2E — PostgreSQL Validation.

Post-migration validation for the real-migration acceptance workflow (Mode 2).

Workflow:

    python tests/e2e/postgresql/run_setup.py [--config CONFIG]
    # → user runs actual migration CLI/UI
    python tests/e2e/postgresql/run_validation.py [--config CONFIG]

This script does NOT perform migration.  It connects to the already-migrated
source and target databases and validates:

  1. Source structural validation (tables, columns, PKs, FKs, indexes, views,
     functions, procedures, triggers, sequences, partitions, synonyms, types,
     comments, security, row counts).
  2. Target structural validation (same checks against the target).
  3. Source-vs-target comparison (catalog deltas).
  4. Functional validation (views, functions, procedures, triggers, FKs,
     CHECKs, identities, synonyms, partitions, sequence behavior).

Reports are written to tests/e2e/postgresql/reports/.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tests.e2e.acceptance import (
    E2EConfig,
    fmt_phase,
    load_e2e_config,
    write_acceptance_report,
)
from tests.e2e.postgresql.setup.database import (
    _resolve_connection_params,
    is_safe_e2e_database,
)
from tests.e2e.postgresql.setup.target import is_protected_database
from tests.e2e.postgresql.validation.catalog import PostgreSQLCatalog
from tests.e2e.postgresql.validation.comparator import SourceTargetComparator
from tests.e2e.postgresql.validation.functional import FunctionalValidator
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
)
from tests.e2e.postgresql.validation.source_validator import SourceValidator
from tests.e2e.postgresql.validation.target_validator import TargetValidator

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def _connect(cfg: E2EConfig, side: str, db_name: str):
    import psycopg

    conn_cfg = cfg.raw[side]["connection"]
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
        password=params.get("password"),
        dbname=db_name,
        connect_timeout=10,
    )


def _run_source_validation(cfg: E2EConfig) -> dict:
    """Phase B — validate source database against expected fixtures."""
    conn = _connect(cfg, "source", cfg.source_db)
    try:
        catalog = PostgreSQLCatalog(conn)
        validator = SourceValidator(catalog, database_name=cfg.source_db)
        report = validator.validate()
    finally:
        conn.close()

    print("\n[1] SOURCE STRUCTURAL VALIDATION")
    print("-" * 60)
    for phase in report.phases:
        print(fmt_phase(phase))
        if phase.status != STATUS_PASS:
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")

    src_rows = {}
    for phase in report.phases:
        if phase.name == "row_counts":
            for check in phase.checks:
                tbl = check.name.replace("rows:", "")
                src_rows[tbl] = check.actual

    passed = report.summary.get(STATUS_PASS, 0)
    failed = report.summary.get(STATUS_FAIL, 0)
    print(f"\n  Source: {'PASS' if report.passed else 'FAIL'}"
          f"  ({passed} passed, {failed} failed, {report.total_duration_s:.1f}s)")

    return {
        "status": STATUS_PASS if report.passed else STATUS_FAIL,
        "passed": passed,
        "failed": failed,
        "duration_s": round(report.total_duration_s, 1),
        "row_counts": src_rows,
        "report": report.to_dict(),
    }


def _run_target_validation(cfg: E2EConfig, src_conn, tgt_conn) -> dict:
    """Phase D — target validation + comparison + functional."""
    src_cat = PostgreSQLCatalog(src_conn)
    tgt_cat = PostgreSQLCatalog(tgt_conn)

    all_passed = True
    results: dict = {}

    # Step 1: Structural validation of target
    print("\n[2] TARGET STRUCTURAL VALIDATION")
    print("-" * 60)
    target_validator = TargetValidator(tgt_cat, database_name=cfg.target_db)
    target_report = target_validator.validate()
    for phase in target_report.phases:
        print(fmt_phase(phase))
        if phase.status != STATUS_PASS:
            all_passed = False
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")
    tgt_rows = {}
    for phase in target_report.phases:
        if phase.name == "row_counts":
            for check in phase.checks:
                tbl = check.name.replace("rows:", "")
                tgt_rows[tbl] = check.actual
    results["structural"] = {
        "status": STATUS_PASS if target_report.passed else STATUS_FAIL,
        "report": target_report.to_dict(),
        "row_counts": tgt_rows,
    }
    if not target_report.passed:
        all_passed = False
    print(f"\n  Structural: {'PASS' if target_report.passed else 'FAIL'}")

    # Step 2: Source vs Target comparison
    print("\n[3] SOURCE/TARGET COMPARISON")
    print("-" * 60)
    comparator = SourceTargetComparator(src_cat, tgt_cat)
    comparator.compare()
    comparison_report = comparator.to_report()
    for phase in comparison_report.phases:
        print(fmt_phase(phase))
        if phase.status != STATUS_PASS:
            all_passed = False
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")
    results["comparison"] = {
        "status": STATUS_PASS if comparison_report.passed else STATUS_FAIL,
        "report": comparison_report.to_dict(),
    }
    print(f"\n  Comparison: {'PASS' if comparison_report.passed else 'FAIL'}")

    # Step 3: Functional validation
    print("\n[4] FUNCTIONAL VALIDATION")
    print("-" * 60)
    with tgt_conn.cursor() as cur:
        cur.execute("SET SESSION AUTHORIZATION DEFAULT;")
    func_validator = FunctionalValidator(tgt_conn, database_name=cfg.target_db)
    func_report = func_validator.validate()
    for phase in func_report.phases:
        print(fmt_phase(phase))
        if phase.status != STATUS_PASS:
            all_passed = False
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")
    results["functional"] = {
        "status": STATUS_PASS if func_report.passed else STATUS_FAIL,
        "report": func_report.to_dict(),
    }
    print(f"\n  Functional: {'PASS' if func_report.passed else 'FAIL'}")

    return results, all_passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PostgreSQL acceptance E2E — post-migration validation."
    )
    parser.add_argument("--config", help="Path to acceptance YAML config.")
    args = parser.parse_args()
    cfg: E2EConfig = load_e2e_config(args.config)

    source_db = cfg.source_db
    target_db = cfg.target_db
    pattern = cfg.safe_pattern

    if is_protected_database(target_db):
        print(f"[!] Target database '{target_db}' is protected — aborting.")
        sys.exit(1)
    if not is_safe_e2e_database(target_db, pattern):
        print(f"[!] Target DB '{target_db}' failed E2E safety check — aborting.")
        sys.exit(1)

    print("=" * 60)
    print(f"[E2E VALIDATION] Engine: {cfg.engine.upper()}")
    print(f"  Source: {source_db}")
    print(f"  Target: {target_db}")
    print("=" * 60)

    # --- Connect to both databases ---
    src_conn = _connect(cfg, "source", source_db)
    tgt_conn = _connect(cfg, "target", target_db)

    try:
        t0 = time.time()

        # --- Phase B: Source structural validation ---
        src_result = _run_source_validation(cfg)

        # --- Phase D: Target validation + comparison + functional ---
        d_results, d_all_passed = _run_target_validation(cfg, src_conn, tgt_conn)

    finally:
        src_conn.close()
        tgt_conn.close()

    elapsed = time.time() - t0
    overall_passed = src_result["status"] == STATUS_PASS and d_all_passed

    # --- Build consolidated report ---
    report = {
        "mode": "acceptance_validation",
        "engine": "postgresql",
        "timestamp": datetime.now(UTC).isoformat(),
        "source_database": source_db,
        "target_database": target_db,
        "schema": cfg.schema,
        "migration_status": "externally_executed",
        "duration_s": round(elapsed, 1),
        "source": src_result,
        "target_validation": d_results["structural"],
        "comparison": d_results["comparison"],
        "functional": d_results["functional"],
        "result": "PASS" if overall_passed else "FAIL",
    }

    # --- Print summary ---
    print("\n" + "=" * 60)
    print("ACCEPTANCE VALIDATION RESULT")
    print("=" * 60)
    print(f"  Source:      {source_db}")
    print(f"  Target:      {target_db}")
    print(f"  Overall:     {'PASS' if overall_passed else 'FAIL'}")
    print(f"  Duration:    {elapsed:.1f}s")

    src_rc = src_result["row_counts"]
    tgt_rc = d_results["structural"]["row_counts"]
    print("\n  SOURCE -> TARGET row counts:")
    all_tables = sorted(set(src_rc) | set(tgt_rc))
    for tbl in all_tables:
        s = src_rc.get(tbl, "?")
        t = tgt_rc.get(tbl, "?")
        match = "PASS" if s == t else "FAIL"
        print(f"    [{match}] {tbl}: src={s}  tgt={t}")

    print(f"\n  Structural:  {d_results['structural']['status']}")
    print(f"  Comparison:  {d_results['comparison']['status']}")
    print(f"  Functional:  {d_results['functional']['status']}")

    report_path = os.path.join(
        REPORTS_DIR, f"acceptance_validation_{target_db}.json"
    )
    write_acceptance_report(report_path, report)
    print(f"\n  Report: {report_path}")
    print("=" * 60)

    sys.exit(0 if overall_passed else 1)


if __name__ == "__main__":
    main()
