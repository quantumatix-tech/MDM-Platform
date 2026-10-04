#!/usr/bin/env python
"""Phase D — MySQL Target Validation + Source/Target Comparison + Functional.

Validates the state produced by Phase C2 (the real migration) by running three
layers:

1. Structural validation of the target database (reusing Phase B's
   ``MySQLTargetValidator``).
2. Source-vs-target comparison (``MySQLSourceTargetComparator``) — queries both
   live databases and reports per-category deltas.
3. Functional validation (``MySQLFunctionalValidator``) — safe transactional
   behavioral checks that exercise migrated objects (views, functions,
   procedures, triggers, FKs, CHECK constraints, AUTO_INCREMENT,
   partitions, ENUMs).

All mutating checks use ``START TRANSACTION / ROLLBACK`` so no business data is
permanently modified.

Usage:
    Set environment variables first:
    $env:SECRET_mysql_e2e_source_pass = "<mysql-password>"
    $env:SECRET_mysql_e2e_target_pass = "<mysql-password>"

    python tests/e2e/mysql/run_phase_d.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import mysql.connector
import yaml

from tests.e2e.mysql.setup.database import _resolve_connection_params
from tests.e2e.mysql.setup.target import is_protected_database
from tests.e2e.mysql.validation.catalog import MySQLCatalog
from tests.e2e.mysql.validation.comparator import MySQLSourceTargetComparator
from tests.e2e.mysql.validation.functional import MySQLFunctionalValidator
from tests.e2e.mysql.validation.models import STATUS_PASS
from tests.e2e.mysql.validation.target_validator import MySQLTargetValidator

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config", "local_to_local.yaml",
)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fmt_phase(phase) -> str:
    failed = sum(1 for c in phase.checks if c.status != STATUS_PASS)
    total = len(phase.checks)
    if failed == 0:
        return f"  [+] {phase.name:<32} PASS  {total}/{total}"
    else:
        return f"  [!] {phase.name:<32} FAIL  {total - failed}/{total}"


def _connect(config: dict, side: str, db_name: str) -> mysql.connector.MySQLConnection:
    conn_cfg = config[side]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"],
        port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )
    conn = mysql.connector.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password"),
        database=db_name,
        connection_timeout=10,
    )
    conn.autocommit = True
    return conn


def main() -> None:
    config = load_config()

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]

    print("=" * 60)
    print("PHASE D: MySQL TARGET VALIDATION + S/T COMPARISON + FUNCTIONAL")
    print("=" * 60)
    print(f"Source: {source_db}")
    print(f"Target: {target_db}")
    print("=" * 60)

    if is_protected_database(target_db):
        print(f"[!] Target database '{target_db}' is protected — aborting.")
        sys.exit(1)

    src_conn = _connect(config, "source", source_db)
    tgt_conn = _connect(config, "target", target_db)
    tgt_conn.autocommit = False

    src_cat = MySQLCatalog(src_conn)
    tgt_cat = MySQLCatalog(tgt_conn)

    all_passed = True

    # --- Step 1: Structural validation of target ---
    print("\n[1] TARGET STRUCTURAL VALIDATION")
    print("-" * 60)
    target_validator = MySQLTargetValidator(tgt_cat, database_name=target_db)
    target_report = target_validator.validate()
    for phase in target_report.phases:
        print(_fmt_phase(phase))
        if phase.status != STATUS_PASS:
            all_passed = False
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")
                    if check.details:
                        for k, v in check.details.items():
                            if v:
                                print(f"          {k}: {v}")

    print(f"\n  Structural: {'PASS' if target_report.passed else 'FAIL'}")

    # --- Step 2: Source vs Target comparison ---
    print("\n[2] SOURCE/TARGET COMPARISON")
    print("-" * 60)
    comparator = MySQLSourceTargetComparator(src_cat, tgt_cat)
    comparator.compare()
    comparison_report = comparator.to_report()
    for phase in comparison_report.phases:
        print(_fmt_phase(phase))
        if phase.status != STATUS_PASS:
            all_passed = False
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")

    print(f"\n  Comparison: {'PASS' if comparison_report.passed else 'FAIL'}")

    # --- Step 3: Functional validation ---
    print("\n[3] FUNCTIONAL VALIDATION")
    print("-" * 60)
    tgt_conn.autocommit = True
    func_validator = MySQLFunctionalValidator(tgt_conn, database_name=target_db)
    func_report = func_validator.validate()
    for phase in func_report.phases:
        print(_fmt_phase(phase))
        if phase.status != STATUS_PASS:
            all_passed = False
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")

    print(f"\n  Functional: {'PASS' if func_report.passed else 'FAIL'}")

    src_conn.close()
    tgt_conn.close()

    report_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports",
        f"phase_d_validation_{target_db}.json",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    combined = {
        "phase": "D",
        "status": "PASS" if all_passed else "FAIL",
        "source_database": source_db,
        "target_database": target_db,
        "structural_validation": target_report.to_dict(),
        "source_target_comparison": comparison_report.to_dict(),
        "functional_validation": func_report.to_dict(),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            combined, f, indent=2, ensure_ascii=False,
            default=lambda o: (
                str(o) if isinstance(o, (Decimal, datetime, date)) else o
            ),
        )

    print(f"\n[REPORT] JSON: {report_path}")
    print("=" * 60)
    overall = "PASS" if all_passed else "FAIL"
    print(f"PHASE D RESULT: {overall}")
    print(f"  Structural:  {'PASS' if target_report.passed else 'FAIL'}")
    print(f"  Comparison:  {'PASS' if comparison_report.passed else 'FAIL'}")
    print(f"  Functional:  {'PASS' if func_report.passed else 'FAIL'}")
    print("=" * 60)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
