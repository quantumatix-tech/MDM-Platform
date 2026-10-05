#!/usr/bin/env python
"""Phase A verification: MySQL DB setup + fixture load.

Usage:
    Set environment variables first:
    $env:SECRET_mysql_e2e_source_pass = "<mysql-password>"
    $env:SECRET_mysql_e2e_target_pass = "<mysql-password>"

    python tests/e2e/mysql/run_phase_a.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import yaml

from tests.e2e.mysql.setup.database import (
    _resolve_connection_params,
    is_safe_e2e_database,
    reset_database,
)
from tests.e2e.mysql.setup.fixture_loader import LoadResult, load_fixture


def load_config() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config", "local_to_local.yaml"
    )
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_phase_a(config: dict) -> LoadResult:
    """Reset both databases and load fixtures into source.

    Reusable function called by the Phase E orchestrator and standalone runner.
    """
    params = _resolve_connection_params(
        host=config["source"]["connection"]["host"],
        port=config["source"]["connection"]["port"],
        username=config["source"]["connection"]["username"],
        password_env=config["source"]["connection"]["password_secret"],
    )

    source_db = config["e2e"]["source_database"]
    target_db = config["e2e"]["target_database"]
    pattern = config["e2e"]["safe_db_pattern"]
    fixture_path = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), config["e2e"]["fixture_path"])
    )

    print(f"[SAFETY] Checking E2E database names against pattern: {pattern}")
    if not is_safe_e2e_database(source_db, pattern):
        print(f"[!] Source DB {source_db!r} failed safety check — aborting.")
        sys.exit(1)
    if not is_safe_e2e_database(target_db, pattern):
        print(f"[!] Target DB {target_db!r} failed safety check — aborting.")
        sys.exit(1)
    print("[SAFETY] Database names pass E2E pattern check.")

    print(f"\n[SETUP] Resetting source database: {source_db}")
    start = time.time()
    reset_database(
        params["host"], params["port"], params["user"],
        params.get("password"), source_db, pattern,
    )
    print(f"  Done in {time.time() - start:.1f}s")

    print(f"\n[SETUP] Resetting target database: {target_db}")
    start = time.time()
    reset_database(
        params["host"], params["port"], params["user"],
        params.get("password"), target_db, pattern,
    )
    print(f"  Done in {time.time() - start:.1f}s")

    print(f"\n[FIXTURE] Loading fixture from: {fixture_path}")
    start = time.time()
    result = load_fixture(
        fixture_dir=fixture_path,
        host=params["host"],
        port=params["port"],
        username=params["user"],
        password_env=config["source"]["connection"]["password_secret"],
        database=source_db,
    )

    print(f"  Loaded {len(result.results)} scripts, "
          f"{'all OK' if result.all_ok else 'FAILURES'})")
    print(f"  Elapsed: {time.time() - start:.1f}s")

    return result


def main() -> None:
    config = load_config()
    result = run_phase_a(config)

    for sr in result.results:
        status = "PASS" if sr.ok else "FAIL"
        print(f"  [{status}] {sr.script} ({sr.duration_s:.2f}s)")
        if not sr.ok:
            print(f"        ERROR: {sr.error}")
            print(f"        OUTPUT: {sr.output[-500:]}")

    if result.all_ok:
        print("\n[RESULT] PHASE A: PASS")
    else:
        print("\n[RESULT] PHASE A: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
