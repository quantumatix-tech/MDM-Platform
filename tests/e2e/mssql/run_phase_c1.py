#!/usr/bin/env python
"""Phase C1 — Target setup and pre-migration target validation."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pyodbc
import yaml

from tests.e2e.mssql.setup.database import (
    _resolve_connection_params,
    make_conn_str,
)
from tests.e2e.mssql.setup.target import reset_target_database
from tests.e2e.mssql.validation.models import STATUS_FAIL, STATUS_PASS
from tests.e2e.mssql.validation.target_pre import TargetPreValidator


def load_config() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "config", "local_to_local.yaml"
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
    print("PHASE C1: TARGET SETUP + PRE-MIGRATION VALIDATION")
    print("=" * 60)
    print(f"Target Database: {target_db}")
    print("=" * 60)

    # --- Reset target database ---
    print(f"\n[SETUP] Resetting target database: {target_db}")
    try:
        created = reset_target_database(
            params["host"], params["port"], params["username"],
            params["password"], target_db, safe_pattern,
        )
        print(f"  [{'+' if created else '-'}] Database reset complete "
              f"({'created fresh' if created else 'already existed'})")
    except ValueError as e:
        print(f"  [!] SAFETY ERROR: {e}")
        sys.exit(1)

    # --- Connect and validate ---
    conn_str = make_conn_str(
        params["host"], params["port"], params["username"],
        params["password"], database=target_db,
    )
    conn = pyodbc.connect(conn_str)

    print("\n[VALIDATION] Pre-migration target checks")
    validator = TargetPreValidator(conn, target_db)
    report = validator.validate()
    conn.close()

    for phase in report.phases:
        print(_fmt_phase(phase))
        if phase.status != STATUS_PASS:
            for check in phase.checks:
                if check.status != STATUS_FAIL:
                    continue
                print(f"        X {check.name}: {check.message or ''}")

    print("=" * 60)
    overall = "PASS" if report.passed else "FAIL"
    print(f"TARGET C1 RESULT: {overall}")
    print(f"Checks: {report.summary['PASS']} passed, "
          f"{report.summary['FAIL']} failed")
    print(f"Total:  {report.total_duration_s:.1f}s")
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
