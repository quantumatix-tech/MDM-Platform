#!/usr/bin/env python
"""Acceptance E2E — MySQL Setup.

Prepares a deterministic source database and a clean target database for the
real-migration acceptance workflow (Mode 2).

Workflow:

    python tests/e2e/mysql/run_setup.py [--config CONFIG] [--cleanup]

This script:
  1. Validates E2E database names against strict safety patterns.
  2. Protects non-E2E databases.
  3. Resets source and target databases (drop + recreate).
  4. Loads deterministic fixtures into source.
  5. Verifies source fixture loaded successfully.
  6. Verifies target is clean (no user objects).
  7. Writes a JSON setup report.

It does NOT perform migration.  The user runs the migration platform CLI after
this script completes.

Use --cleanup to destroy both E2E databases after verification.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tests.e2e.acceptance import E2EConfig, load_e2e_config, write_acceptance_report
from tests.e2e.mysql.setup.cleanup import cleanup_databases
from tests.e2e.mysql.setup.database import (
    _resolve_connection_params,
    is_safe_e2e_database,
    reset_database,
)
from tests.e2e.mysql.setup.fixture_loader import load_fixture
from tests.e2e.mysql.setup.target import PROTECTED_DATABASES, is_protected_database

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def _connect(cfg: E2EConfig, db_name: str):
    import mysql.connector

    conn_cfg = cfg.source_connection
    params = _resolve_connection_params(
        host=conn_cfg["host"],
        port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )
    return mysql.connector.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password"),
        database=db_name,
        connection_timeout=10,
    )


def _count_user_tables(cfg: E2EConfig, db_name: str) -> int:
    conn = _connect(cfg, db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                (db_name,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _count_rows(cfg: E2EConfig, db_name: str, table: str) -> int:
    conn = _connect(cfg, db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            return cur.fetchone()[0]
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MySQL acceptance E2E — setup source and target databases."
    )
    parser.add_argument("--config", help="Path to acceptance YAML config.")
    parser.add_argument(
        "--cleanup", action="store_true",
        help="Destroy both E2E databases after setup verification (destructive).",
    )
    args = parser.parse_args()
    cfg: E2EConfig = load_e2e_config(args.config)

    source_db = cfg.source_db
    target_db = cfg.target_db
    pattern = cfg.safe_pattern

    if is_protected_database(source_db):
        print(f"[!] Source database {source_db!r} is protected — aborting.")
        sys.exit(1)
    if is_protected_database(target_db):
        print(f"[!] Target database {target_db!r} is protected — aborting.")
        sys.exit(1)
    if not is_safe_e2e_database(source_db, pattern):
        print(f"[!] Source DB '{source_db}' failed E2E safety check — aborting.")
        sys.exit(1)
    if not is_safe_e2e_database(target_db, pattern):
        print(f"[!] Target DB '{target_db}' failed E2E safety check — aborting.")
        sys.exit(1)

    print("=" * 60)
    print(f"[E2E SETUP] Engine: {cfg.engine.upper()}")
    print(f"  Source:        {source_db}")
    print(f"  Target:        {target_db}")
    print(f"  Safe pattern:  {pattern}")
    print(f"  Protected DBs: {sorted(PROTECTED_DATABASES)}")
    print("=" * 60)

    params = _resolve_connection_params(
        host=cfg.source_connection["host"],
        port=cfg.source_connection["port"],
        username=cfg.source_connection["username"],
        password_env=cfg.source_connection["password_secret"],
    )

    report: dict = {
        "mode": "acceptance_setup",
        "engine": "mysql",
        "timestamp": datetime.now(UTC).isoformat(),
        "source_database": source_db,
        "target_database": target_db,
        "safe_db_pattern": pattern,
        "protected_databases": sorted(PROTECTED_DATABASES),
        "checks": [],
        "setup_complete": False,
    }

    # --- Optional cleanup ---
    if args.cleanup:
        print("\n[CLEANUP] Destroying E2E databases...")
        cleanup_databases(
            params["host"], params["port"], params["user"],
            params.get("password"), [source_db, target_db], pattern,
        )
        report["checks"].append({
            "name": "cleanup", "status": "PASS",
            "message": "Both databases destroyed (acceptance cleanup).",
        })
        report["setup_complete"] = True
        report["cleaned_up"] = True
        _write_report(cfg, report)
        print("\n[CLEANUP] Done.")
        return

    # --- Reset both databases ---
    print(f"\n[SETUP] Resetting source database: {source_db}")
    t0 = time.time()
    reset_database(
        params["host"], params["port"], params["user"],
        params.get("password"), source_db, pattern,
    )
    elapsed = time.time() - t0
    report["checks"].append({
        "name": "source_reset", "status": "PASS",
        "message": f"Reset in {elapsed:.1f}s",
    })
    print(f"  Done in {elapsed:.1f}s")

    print(f"\n[SETUP] Resetting target database: {target_db}")
    t0 = time.time()
    reset_database(
        params["host"], params["port"], params["user"],
        params.get("password"), target_db, pattern,
    )
    elapsed = time.time() - t0
    report["checks"].append({
        "name": "target_reset", "status": "PASS",
        "message": f"Reset in {elapsed:.1f}s",
    })
    print(f"  Done in {elapsed:.1f}s")

    # --- Load fixture into source ---
    fixture_path = cfg.to_abs_fixture_path(
        os.path.dirname(os.path.abspath(__file__))
    )
    print(f"\n[FIXTURE] Loading fixtures from: {fixture_path}")
    t0 = time.time()
    result = load_fixture(
        fixture_dir=fixture_path,
        host=params["host"],
        port=params["port"],
        username=params["user"],
        password_env=cfg.source_connection["password_secret"],
        database=source_db,
    )
    elapsed = time.time() - t0

    all_ok = result.all_ok
    for sr in result.results:
        status = "PASS" if sr.ok else "FAIL"
        print(f"  [{status}] {sr.script} ({sr.duration_s:.2f}s)")
        if not sr.ok:
            print(f"        ERROR: {sr.error}")
    report["checks"].append({
        "name": "fixture_load", "status": "PASS" if all_ok else "FAIL",
        "message": f"{len(result.results)} scripts, "
                   f"{'all OK' if all_ok else 'FAILURES'} ({elapsed:.1f}s)",
    })

    if not all_ok:
        print("\n[SETUP] FAIL — fixture load had errors.")
        report["setup_complete"] = False
        _write_report(cfg, report)
        sys.exit(1)

    # --- Verify source has expected tables ---
    src_table_count = _count_user_tables(cfg, source_db)
    report["checks"].append({
        "name": "source_tables", "status": "PASS" if src_table_count > 0 else "FAIL",
        "message": f"{src_table_count} base tables found in source",
    })
    if src_table_count == 0:
        all_ok = False

    expected = cfg.raw.get("e2e", {}).get("expected", {})
    expected_rows = expected.get("row_counts", {})
    row_summary: dict[str, int] = {}
    if expected_rows:
        for tbl, expected_count in expected_rows.items():
            try:
                actual = _count_rows(cfg, source_db, tbl)
                row_summary[tbl] = actual
                ok = actual == expected_count
                report["checks"].append({
                    "name": f"row_count:{tbl}",
                    "status": "PASS" if ok else "FAIL",
                    "message": f"expected={expected_count} actual={actual}",
                })
                if not ok:
                    all_ok = False
            except Exception as exc:  # noqa: BLE001
                report["checks"].append({
                    "name": f"row_count:{tbl}",
                    "status": "FAIL",
                    "message": str(exc),
                })
                all_ok = False

    # --- Verify target is clean ---
    tgt_table_count = _count_user_tables(cfg, target_db)
    target_clean = tgt_table_count == 0
    report["checks"].append({
        "name": "target_clean",
        "status": "PASS" if target_clean else "FAIL",
        "message": f"Target has {tgt_table_count} user tables "
                   f"({'clean' if target_clean else 'NOT clean'})",
    })
    if not target_clean:
        all_ok = False

    report["source_row_counts"] = row_summary
    report["source_table_count"] = src_table_count
    report["target_table_count"] = tgt_table_count
    report["setup_complete"] = all_ok

    # --- Summary ---
    print("\n" + "=" * 60)
    print(f"  Source tables:   {src_table_count}")
    print(f"  Target tables:   {tgt_table_count} (should be 0)")
    print(f"  Fixture scripts: {len(result.results)} loaded")
    if row_summary:
        print("  Source row counts:")
        for tbl, cnt in sorted(row_summary.items()):
            print(f"    {tbl}: {cnt}")
    print(f"\n  SETUP: {'COMPLETE' if all_ok else 'FAILED'}")
    print("=" * 60)

    config_name = args.config or "config/mysql_e2e_acceptance.yaml"
    print("\n  Next step — run the actual migration:")
    print(f"    python -m migration_platform --config {config_name} --mode full")

    _write_report(cfg, report)
    sys.exit(0 if all_ok else 1)


def _write_report(cfg: E2EConfig, report: dict) -> None:
    report_path = os.path.join(
        REPORTS_DIR, f"acceptance_setup_{cfg.target_db}.json"
    )
    write_acceptance_report(report_path, report)
    print(f"\n  Report: {report_path}")


if __name__ == "__main__":
    main()
