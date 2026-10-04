#!/usr/bin/env python
"""Phase D — PostgreSQL Target Validation + Source/Target Comparison + Functional.

Validates the state produced by Phase C2 (the real migration) by running three
layers:

1. Structural validation of the target database (reusing Phase B's
   ``TargetValidator``).
2. Source-vs-target comparison (``SourceTargetComparator``) — queries both live
   databases and reports per-category deltas.
3. Functional validation (``FunctionalValidator``) — safe transactional behavioral
   checks that exercise migrated objects (views, functions, procedures, triggers,
   FKs, CHECK constraints, identities, sequences, partitions, enums, RLS).

All mutating checks use ``BEGIN / ROLLBACK`` (or autocommit with no-op changes
for procedures that contain explicit COMMIT).  No business data is permanently
modified.

Usage:
    Set environment variables first:
    $env:SECRET_postgresql_e2e_source_pass = "<postgres-password>"
    $env:SECRET_postgresql_e2e_target_pass = "<postgres-password>"

    python tests/e2e/postgresql/run_phase_d.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import psycopg
import yaml

from tests.e2e.postgresql.setup.database import _resolve_connection_params
from tests.e2e.postgresql.setup.target import is_protected_database
from tests.e2e.postgresql.validation.catalog import PostgreSQLCatalog
from tests.e2e.postgresql.validation.comparator import SourceTargetComparator
from tests.e2e.postgresql.validation.functional import FunctionalValidator
from tests.e2e.postgresql.validation.models import STATUS_PASS
from tests.e2e.postgresql.validation.target_validator import TargetValidator

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config", "local_to_local.yaml",
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


def _connect(config: dict, side: str, db_name: str) -> psycopg.Connection:
    conn_cfg = config[side]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"],
        port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )
    conn = psycopg.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password", ""),
        dbname=db_name,
        connect_timeout=10,
    )
    return conn


def main() -> None:
    config = load_config()

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]

    print("=" * 60)
    print("PHASE D: POSTGRESQL TARGET VALIDATION + S/T COMPARISON + FUNCTIONAL")
    print("=" * 60)
    print(f"Source: {source_db}")
    print(f"Target: {target_db}")
    print("=" * 60)

    # --- Safety: never validate a protected database ---
    if is_protected_database(target_db):
        print(f"[!] Target database '{target_db}' is protected — aborting.")
        sys.exit(1)

    # --- Connect to both databases ---
    src_conn = _connect(config, "source", source_db)
    tgt_conn = _connect(config, "target", target_db)

    src_cat = PostgreSQLCatalog(src_conn)
    tgt_cat = PostgreSQLCatalog(tgt_conn)

    all_passed = True

    # --- Step 1: Structural validation of target ---
    print("\n[1] TARGET STRUCTURAL VALIDATION")
    print("-" * 60)
    target_validator = TargetValidator(tgt_cat, database_name=target_db)
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
    comparator = SourceTargetComparator(src_cat, tgt_cat)
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
    func_validator = FunctionalValidator(tgt_conn, database_name=target_db)
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

    # --- Write combined JSON report ---
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
