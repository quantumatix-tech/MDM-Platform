#!/usr/bin/env python
"""Phase C2 — Execute the real migration flow.

Calls the existing MigrationOrchestrator with the E2E local-to-local
configuration — the exact same code path as ``python -m migration_platform``.
Does not duplicate any migration logic.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import yaml

from core.audit_logger import configure_file_logging
from core.connectors import (
    MSSQLSourceConnector,
    MSSQLTargetConnector,
)
from core.orchestrator import MigrationOrchestrator
from tests.e2e.mssql.setup.database import _resolve_connection_params
from tests.e2e.mssql.setup.target import is_protected_database
from tests.e2e.mssql.validation.models import STATUS_PASS
from tests.e2e.mssql.validation.target_pre import TargetPreValidator

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config", "local_to_local.yaml",
)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _verify_target_clean(
    params: dict[str, str], db_name: str
) -> bool:
    """Use the Phase C1 validator to confirm the target is still clean.

    Does NOT reset the target — C1 owns that.
    """
    import pyodbc

    from tests.e2e.mssql.setup.database import make_conn_str

    conn_str = make_conn_str(
        params["host"], params["port"], params["username"],
        params["password"], database=db_name,
    )
    conn = pyodbc.connect(conn_str)
    validator = TargetPreValidator(conn, db_name)
    report = validator.validate()
    conn.close()

    if not report.passed:
        print("[C2] Target is NOT clean — aborting migration.")
        for phase in report.phases:
            if phase.status != STATUS_PASS:
                print(f"  Phase '{phase.name}' FAILED")
        return False
    print("[C2] Target clean-state verified (Phase C1 validator).")
    return True


def run_migration(config: dict) -> dict:
    """Execute the migration using the existing orchestrator.

    This mirrors ``python -m migration_platform --config <cfg> --mode full``
    but bypasses the blocking HTML report server / Rich dashboard.
    """
    source_cfg = config["source"]["connection"]
    target_cfg = config["target"]["connection"]

    source = MSSQLSourceConnector(dict(source_cfg))
    target_params = dict(target_cfg)
    target_params["source_engine"] = source_cfg.get("engine", "mssql")
    target = MSSQLTargetConnector(target_params)

    # Configure audit log (same as __main__.py)
    configure_file_logging(log_dir="logs", suppress_stdout=True)

    orchestrator = MigrationOrchestrator(
        source, target, config, status_server=None
    )
    result = orchestrator.run_full()
    return result


def main() -> None:
    config = load_config()
    target_cfg = config["target"]["connection"]

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]

    print("=" * 60)
    print("PHASE C2: REAL MIGRATION EXECUTION")
    print("=" * 60)
    print(f"Source: {source_db}")
    print(f"Target: {target_db}")
    print("=" * 60)

    if is_protected_database(target_db):
        print(f"[!] Target database '{target_db}' is protected — aborting.")
        sys.exit(1)

    # --- Step 1: Verify target is clean (Phase C1, no reset) ---
    params = _resolve_connection_params(
        host=target_cfg["host"],
        port=target_cfg["port"],
        username=target_cfg["username"],
        password_env=target_cfg["password_secret"],
    )
    if not _verify_target_clean(params, target_db):
        sys.exit(1)

    # --- Step 2: Execute migration ---
    print("\n[MIGRATION] Starting migration via MigrationOrchestrator...")
    start = time.time()
    error_info: str | None = None
    result: dict = {}
    try:
        result = run_migration(config)
    except Exception as exc:
        error_info = str(exc)
        traceback.print_exc()
        result = {"status": "error", "error": error_info}

    duration = time.time() - start
    status = result.get("status", "unknown")

    # --- Step 3: Extract summary ---
    phases = result.get("phases", {})
    objects = phases.get("discover", {}).get("objects", [])
    if isinstance(objects, list):
        obj_count = len(objects)
    else:
        obj_count = 0

    print(f"\n[MIGRATION] Completed in {duration:.1f}s")
    print(f"[MIGRATION] Status: {status.upper()}")
    if objects:
        if isinstance(objects, list) and len(objects) > 0:
            print("[MIGRATION] Objects migrated:")
            for obj_name in objects:
                obj_phase = phases.get(obj_name, {})
                src_rows = obj_phase.get("source_rows", "n/a")
                success = obj_phase.get("success", "n/a")
                failure = obj_phase.get("failure", "n/a")
                if isinstance(src_rows, int) or isinstance(success, int):
                    print(f"  {obj_name}: {src_rows} source rows, "
                          f"{success} ok, {failure} failed")

    if error_info:
        print(f"[MIGRATION] Error: {error_info}")

    # --- Step 4: Write JSON Phase C2 report ---
    report_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports",
        f"phase_c2_migration_result_{target_db}.json",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    c2_report = {
        "phase": "C2",
        "status": "PASS" if status in ("success",) else "FAIL",
        "migration_status": status,
        "source_database": source_db,
        "target_database": target_db,
        "duration_s": round(duration, 1),
        "object_count": obj_count,
        "objects": objects if isinstance(objects, list) else [],
        "phase_summaries": {
            name: (phase_val if isinstance(phase_val, str)
                   else phase_val.get("status", "ok")
                   if isinstance(phase_val, dict) else str(phase_val))
            for name, phase_val in phases.items()
        },
        "error": error_info,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(c2_report, f, indent=2, ensure_ascii=False)

    print(f"\n[REPORT] JSON: {report_path}")
    print("=" * 60)
    overall = "PASS" if status in ("success",) else "FAIL"
    print(f"PHASE C2 RESULT: {overall}  (migration status: {status})")
    print("=" * 60)

    sys.exit(0 if overall == "PASS" else 1)


if __name__ == "__main__":
    main()
