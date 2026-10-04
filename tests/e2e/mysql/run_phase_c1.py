#!/usr/bin/env python
"""Phase C1 — MySQL target pre-migration validation.

Verifies that the target database is cleanly reset and contains no
user-created objects before migration begins.  All checks are read-only
catalog queries — no data or objects are modified.

Usage:
    Set environment variables first:
    $env:SECRET_mysql_e2e_target_pass = "<mysql-password>"

    python tests/e2e/mysql/run_phase_c1.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import mysql.connector
import yaml

from tests.e2e.mysql.setup.database import _resolve_connection_params
from tests.e2e.mysql.validation.models import STATUS_PASS
from tests.e2e.mysql.validation.target_pre import MySQLTargetPreValidator


def load_config() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config", "local_to_local.yaml"
    )
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    config = load_config()
    target_cfg = config["target"]["connection"]
    target_db = config["e2e"]["target_database"]

    print("=" * 60)
    print("PHASE C1: MySQL Target Pre-Migration Validation")
    print("=" * 60)
    print(f"Target database: {target_db}")
    print(f"Target host: {target_cfg['host']}:{target_cfg['port']}")
    print("=" * 60)

    params = _resolve_connection_params(
        host=target_cfg["host"],
        port=target_cfg["port"],
        username=target_cfg["username"],
        password_env=target_cfg["password_secret"],
    )

    conn = mysql.connector.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password"),
        database=target_db,
        connection_timeout=10,
    )

    try:
        validator = MySQLTargetPreValidator(conn, db_name=target_db)
        report = validator.validate()
    finally:
        conn.close()

    print(f"\n  Phases executed: {len(report.phases)}")
    for phase in report.phases:
        checks = len(phase.checks)
        passed = sum(1 for c in phase.checks if c.passed)
        status = "PASS" if phase.status == STATUS_PASS else "FAIL"
        print(f"  [{status}] {phase.name}: {passed}/{checks} checks "
              f"({phase.duration_s:.2f}s)")

    failed = sum(1 for p in report.phases for c in p.checks if c.status == "FAIL")
    total = sum(len(p.checks) for p in report.phases)
    passed = total - failed
    print(f"\n  Total checks: {total}")
    print(f"  Passed: {passed}, Failed: {failed}")

    if "--json" in sys.argv:
        print("\n--- JSON Report ---")
        print(json.dumps(report.to_dict(), indent=2))

    if report.passed:
        print("\n[RESULT] PHASE C1: PASS")
        sys.exit(0)
    else:
        print("\n[RESULT] PHASE C1: FAIL")
        for phase in report.phases:
            for check in phase.checks:
                if check.status == "FAIL":
                    print(f"  {phase.name}:{check.name}: {check.message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
