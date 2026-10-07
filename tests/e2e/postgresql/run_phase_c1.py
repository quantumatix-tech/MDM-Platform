#!/usr/bin/env python
"""Phase C1 — Target setup and pre-migration target validation for PostgreSQL E2E.

Resets the PostgreSQL target database (drop + recreate) and validates that it
is reachable and in a clean pre-migration state.

Usage:
    Set environment variables first:
    $env:SECRET_postgresql_e2e_target_pass = "<postgres-password>"

    python tests/e2e/postgresql/run_phase_c1.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import psycopg
import yaml

from tests.e2e.postgresql.setup.database import (
    _resolve_connection_params,
)
from tests.e2e.postgresql.setup.target import reset_target_database
from tests.e2e.postgresql.validation.models import STATUS_FAIL, STATUS_PASS
from tests.e2e.postgresql.validation.target_pre import TargetPreValidator


def load_config() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config", "local_to_local.yaml"
    )
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fmt_phase(phase) -> str:
    icon = "+" if phase.status == STATUS_PASS else "!"
    failed = sum(1 for c in phase.checks if c.status == STATUS_FAIL)
    total = len(phase.checks)
    if failed == 0:
        return f"  [{icon}] {phase.name:<32} PASS  {total}/{total}"
    return f"  [{icon}] {phase.name:<32} FAIL  {total - failed}/{total}"


def main() -> None:
    config = load_config()
    conn_cfg = config["target"]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"],
        port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )

    target_db = config["e2e"]["target_database"]
    safe_pattern = config["e2e"]["safe_db_pattern"]

    print("=" * 60)
    print("PHASE C1: PostgreSQL Target Setup + Pre-Migration Validation")
    print("=" * 60)
    print(f"Target Database: {target_db}")
    print("=" * 60)

    # --- Reset target database (drop + recreate) ---
    print(f"\n[SETUP] Resetting target database: {target_db}")
    try:
        start = time.time()
        created = reset_target_database(
            params["host"], params["port"], params["user"],
            params["password"], target_db, safe_pattern,
        )
        elapsed = time.time() - start
        tag = "created fresh" if created else "already existed"
        print(f"  [DONE] Database reset ({tag}) in {elapsed:.1f}s")
    except ValueError as e:
        print(f"  [!] SAFETY ERROR: {e}")
        sys.exit(1)
    except (psycopg.Error, OSError) as e:
        print(f"  [!] ERROR during reset: {e}")
        sys.exit(1)

    # --- Connect to target and validate ---
    print("\n[VALIDATION] Pre-migration target checks")
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
        print(f"  [!] Connection failed: {e}")
        sys.exit(1)

    try:
        validator = TargetPreValidator(conn, target_db)
        start = time.time()
        report = validator.validate()
        elapsed = time.time() - start
    finally:
        conn.close()

    print()
    for phase in report.phases:
        print(_fmt_phase(phase))
        if phase.status != STATUS_PASS:
            for check in phase.checks:
                if check.status == STATUS_FAIL:
                    print(f"        X {check.name}: {check.message or ''}")

    total_checks = sum(len(p.checks) for p in report.phases)
    failed_checks = sum(
        1 for p in report.phases for c in p.checks if c.status == STATUS_FAIL
    )
    passed_checks = total_checks - failed_checks

    print("=" * 60)
    overall = "PASS" if report.passed else "FAIL"
    print(f"[RESULT] PHASE C1: {overall}")
    print(f"Checks: {passed_checks} passed, {failed_checks} failed / {total_checks} total")
    print(f"Elapsed: {elapsed:.1f}s")
    print("=" * 60)

    # Write JSON report
    reports_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports",
    )
    os.makedirs(reports_dir, exist_ok=True)
    json_path = os.path.join(
        reports_dir, f"target_pre_validation_{target_db}.json"
    )
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
    print(f"JSON report: {json_path}")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
