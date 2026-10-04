#!/usr/bin/env python
"""Phase B — MSSQL SOURCE STRUCTURAL VALIDATION."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pyodbc
import yaml

from tests.e2e.mssql.setup.database import _resolve_connection_params
from tests.e2e.mssql.validation.catalog import MSSQLCatalog
from tests.e2e.mssql.validation.models import STATUS_FAIL, STATUS_PASS
from tests.e2e.mssql.validation.source_validator import SourceValidator


def load_config() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "config", "local_to_local.yaml"
    )
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fmt_phase(phase) -> str:
    """Format a PhaseResult for terminal output."""
    icon = "+" if phase.status == STATUS_PASS else "!"
    failed = sum(1 for c in phase.checks if c.status == STATUS_FAIL)
    total = len(phase.checks)
    if failed == 0:
        return f"  [{icon}] {phase.name:<32} PASS  {total}/{total}"
    else:
        return f"  [{icon}] {phase.name:<32} FAIL  {total - failed}/{total}"


def main() -> None:
    config = load_config()
    conn_cfg = config["source"]["connection"]
    params = _resolve_connection_params(
        host=conn_cfg["host"],
        port=conn_cfg["port"],
        username=conn_cfg["username"],
        password_env=conn_cfg["password_secret"],
    )

    source_db = config["e2e"]["source_database"]

    from tests.e2e.mssql.setup.database import make_conn_str
    conn_str = make_conn_str(
        params["host"], params["port"], params["username"],
        params["password"], database=source_db,
    )

    conn = pyodbc.connect(conn_str)
    catalog = MSSQLCatalog(conn)

    print("=" * 60)
    print("MSSQL SOURCE STRUCTURAL VALIDATION")
    print("=" * 60)
    print(f"Database: {source_db}")
    print("Schema:   (implicit in checks)")
    print("=" * 60)

    validator = SourceValidator(catalog, database_name=source_db)
    report = validator.validate()
    conn.close()

    for phase in report.phases:
        print(_fmt_phase(phase))
        if phase.status != STATUS_PASS:
            for check in phase.checks:
                if check.status != STATUS_PASS:
                    print(f"        X {check.name}: {check.message or ''}")
                    if check.details:
                        for k, v in check.details.items():
                            if v:
                                print(f"          {k}: {v}")

    print("=" * 60)
    overall = "PASS" if report.passed else "FAIL"
    print(f"SOURCE VALIDATION: {overall}")
    print(f"Checks: {report.summary['PASS']} passed, "
          f"{report.summary['FAIL']} failed")
    print(f"Total:  {report.total_duration_s:.1f}s")
    print("=" * 60)

    # Write JSON report
    json_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports", f"source_validation_{source_db}.json"
    )
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
    print(f"JSON report: {json_path}")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
