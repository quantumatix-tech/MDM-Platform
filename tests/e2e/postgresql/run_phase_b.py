#!/usr/bin/env python
"""Phase B verification: PostgreSQL source structural validation.

Connects to the source database (MigrationE2E_PostgreSQL_Source), queries the
live catalog through PostgreSQLCatalog, and compares every object against the
expected fixture state in ``expected.py``.

Usage:
    Set environment variables first:
    $env:SECRET_postgresql_e2e_source_pass = "<postgres-password>"

    python tests/e2e/postgresql/run_phase_b.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import psycopg
import yaml

from tests.e2e.postgresql.setup.database import _resolve_connection_params
from tests.e2e.postgresql.validation.catalog import PostgreSQLCatalog
from tests.e2e.postgresql.validation.source_validator import SourceValidator


def load_config() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config", "local_to_local.yaml"
    )
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    config = load_config()
    params = _resolve_connection_params(
        host=config["source"]["connection"]["host"],
        port=config["source"]["connection"]["port"],
        username=config["source"]["connection"]["username"],
        password_env=config["source"]["connection"]["password_secret"],
    )

    source_db = config["e2e"]["source_database"]

    print("=" * 60)
    print("PHASE B: PostgreSQL Source Structural Validation")
    print("=" * 60)

    conn = psycopg.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password", ""),
        dbname=source_db,
        connect_timeout=10,
    )
    catalog = PostgreSQLCatalog(conn)
    validator = SourceValidator(catalog, database_name=source_db)

    start = time.time()
    report = validator.validate()
    elapsed = time.time() - start

    conn.close()

    summary = report.summary
    print(f"\n  Phases executed: {len(report.phases)}")
    for phase in report.phases:
        checks = len(phase.checks)
        passed = sum(1 for c in phase.checks if c.passed)
        status = "PASS" if phase.status == "PASS" else "FAIL"
        print(f"  [{status}] {phase.name}: {passed}/{checks} checks "
              f"({phase.duration_s:.2f}s)")

    failed = summary.get("FAIL", 0)
    print(f"\n  Total checks: {sum(len(p.checks) for p in report.phases)}")
    print(f"  Passed: {summary.get('PASS', 0)}, Failed: {failed}")
    print(f"  Elapsed: {elapsed:.1f}s")

    if "--json" in sys.argv:
        print("\n--- JSON Report ---")
        print(json.dumps(report.to_dict(), indent=2))

    if failed > 0:
        print("\n[RESULT] PHASE B: FAIL")
        # Print failed checks
        for phase in report.phases:
            for check in phase.checks:
                if check.status == "FAIL":
                    print(f"  {phase.name}:{check.name}: {check.message}")
        sys.exit(1)
    else:
        print("\n[RESULT] PHASE B: PASS")


if __name__ == "__main__":
    main()
