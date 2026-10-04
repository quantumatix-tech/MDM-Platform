#!/usr/bin/env python
"""Phase C2 — Execute the real MySQL migration flow.

Calls the existing ``MigrationOrchestrator`` with the E2E local-to-local
configuration — the exact same code path as ``python -m migration_platform``.

The orchestrator resolves passwords from environment variables via the
``secrets.provider: env`` config and the ``SECRET_<name>`` convention, so
no plaintext passwords appear in this file or the YAML config.
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
from core.connectors.mysql import (
    MySQLSourceConnector,
    MySQLTargetConnector,
)
from core.orchestrator import MigrationOrchestrator
from tests.e2e.mysql.setup.database import _resolve_connection_params
from tests.e2e.mysql.setup.target import is_protected_database
from tests.e2e.mysql.validation.models import STATUS_PASS
from tests.e2e.mysql.validation.target_pre import MySQLTargetPreValidator

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config", "local_to_local.yaml",
)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _verify_target_clean(
    params: dict, db_name: str
) -> bool:
    """Use the Phase C1 validator to confirm the target is still clean.

    Does NOT reset the target — C1 owns that.
    """
    import mysql.connector

    conn = mysql.connector.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params.get("password"),
        database=db_name,
        connection_timeout=10,
    )
    try:
        validator = MySQLTargetPreValidator(conn, db_name=db_name)
        report = validator.validate()
    finally:
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
    run_config = json.loads(json.dumps(config))

    for section in ("source", "target"):
        conn_cfg = run_config[section]["connection"]
        pw_secret = conn_cfg.pop("password_secret", None)
        if pw_secret:
            conn_cfg["password"] = os.environ.get(f"SECRET_{pw_secret}", "")

    source_cfg = dict(run_config["source"]["connection"])
    target_cfg = dict(run_config["target"]["connection"])
    target_cfg["source_engine"] = run_config["source"].get("engine", "mysql")

    source = MySQLSourceConnector(source_cfg)
    target = MySQLTargetConnector(target_cfg)

    configure_file_logging(log_dir="logs", suppress_stdout=True)

    orchestrator = MigrationOrchestrator(
        source, target, run_config, status_server=None
    )
    result = orchestrator.run_full()
    return result


def _extract_migration_summary(result: dict) -> dict:
    """Extract migration counts from the orchestrator result."""
    phases = result.get("phases", {})
    objects = phases.get("discover", {}).get("objects", []) or []

    tables_migrated = 0
    objects_migrated = 0
    failed_objects: list[str] = []

    for obj in objects:
        obj_phase = phases.get(obj, {})
        if isinstance(obj_phase, dict):
            success = obj_phase.get("success", 0)
            failure = obj_phase.get("failure", 0)
            tables_migrated += success
            objects_migrated += 1
            if failure > 0:
                failed_objects.append(obj)

    return {
        "tables_migrated": tables_migrated,
        "objects_migrated": objects_migrated,
        "failed_objects": failed_objects,
    }


def main() -> None:
    config = load_config()
    target_cfg = config["target"]["connection"]

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]
    pattern = config["e2e"]["safe_db_pattern"]

    print("=" * 60)
    print("PHASE C2: Real MySQL Migration Execution")
    print("=" * 60)
    print(f"Source: {source_db}")
    print(f"Target: {target_db}")
    print("=" * 60)

    from tests.e2e.mysql.setup.database import is_safe_e2e_database

    if not is_safe_e2e_database(source_db, pattern):
        print(f"[!] Source database '{source_db}' failed E2E safety check — aborting.")
        sys.exit(1)
    if not is_safe_e2e_database(target_db, pattern):
        print(f"[!] Target database '{target_db}' failed E2E safety check — aborting.")
        sys.exit(1)
    if is_protected_database(target_db):
        print(f"[!] Target database '{target_db}' is protected — aborting.")
        sys.exit(1)

    print(f"[SAFETY] Database names pass E2E pattern check: {pattern}")

    params = _resolve_connection_params(
        host=target_cfg["host"],
        port=target_cfg["port"],
        username=target_cfg["username"],
        password_env=target_cfg["password_secret"],
    )
    print("\n[VERIFY] Checking target clean state before migration...")
    if not _verify_target_clean(params, target_db):
        sys.exit(1)

    print("\n[MIGRATION] Starting migration via MigrationOrchestrator...")
    start = time.time()
    error_info: str | None = None
    result: dict = {}
    try:
        result = run_migration(config)
    except Exception as exc:  # noqa: BLE001
        error_info = str(exc)
        traceback.print_exc()
        result = {"status": "error", "error": error_info, "phases": {}}

    duration = time.time() - start
    status = result.get("status", "unknown")

    if not error_info:
        error_info = result.get("error")

    summary = _extract_migration_summary(result)
    phases = result.get("phases", {})

    print(f"\n[MIGRATION] Completed in {duration:.1f}s")
    print(f"[MIGRATION] Status: {status.upper()}")
    print(f"[MIGRATION] Objects discovered: {summary['objects_migrated']}")
    print(f"[MIGRATION] Rows migrated (success): {summary['tables_migrated']}")
    if summary["failed_objects"]:
        print(f"[MIGRATION] Failed objects: {summary['failed_objects']}")

    if error_info:
        print(f"[MIGRATION] Error: {error_info}")

    reports_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports",
    )
    os.makedirs(reports_dir, exist_ok=True)
    json_path = os.path.join(
        reports_dir, f"phase_c2_migration_result_{target_db}.json"
    )

    c2_report = {
        "phase": "C2",
        "status": "PASS" if status in ("success", "completed", "partial_success") else "FAIL",
        "migration_status": status,
        "source_database": source_db,
        "target_database": target_db,
        "duration_s": round(duration, 1),
        "tables_migrated": summary["tables_migrated"],
        "objects_migrated": summary["objects_migrated"],
        "failed_objects": summary["failed_objects"],
        "phase_summaries": {
            name: (phase_val if isinstance(phase_val, str)
                   else phase_val.get("status", "ok")
                   if isinstance(phase_val, dict) else str(phase_val))
            for name, phase_val in phases.items()
        },
        "error": error_info,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(c2_report, f, indent=2, ensure_ascii=False)

    print(f"\n[REPORT] JSON: {json_path}")
    print("=" * 60)
    overall = "PASS" if status in ("success", "completed", "partial_success") else "FAIL"
    print(f"PHASE C2 RESULT: {overall}  (migration status: {status})")
    print("=" * 60)

    sys.exit(0 if overall == "PASS" else 1)


if __name__ == "__main__":
    main()
