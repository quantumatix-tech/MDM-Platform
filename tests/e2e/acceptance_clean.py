#!/usr/bin/env python
"""Shared acceptance E2E cleanup runner.

Destroys all E2E databases from the configured source and target for the
specified engine.  This is a convenience wrapper that delegates to the
per-engine cleanup logic without requiring a full setup run.

Usage:

    python tests/e2e/acceptance_clean.py --engine mssql  --config config/mssql_e2e_acceptance.yaml
    python tests/e2e/acceptance_clean.py --engine mysql  --config config/mysql_e2e_acceptance.yaml
    python tests/e2e/acceptance_clean.py --engine pg     --config config/postgresql_e2e_acceptance.yaml

The --engine argument selects the backend adapter.  The config determines
which databases to destroy.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from tests.e2e.acceptance import E2EConfig, load_e2e_config

ENGINE_MAP: dict[str, dict[str, str]] = {
    "mssql": {
        "cleanup": "tests.e2e.mssql.setup.cleanup",
        "database": "tests.e2e.mssql.setup.database",
        "target": "tests.e2e.mssql.setup.target",
    },
    "mysql": {
        "cleanup": "tests.e2e.mysql.setup.cleanup",
        "database": "tests.e2e.mysql.setup.database",
        "target": "tests.e2e.mysql.setup.target",
    },
    "pg": {
        "cleanup": "tests.e2e.postgresql.setup.cleanup",
        "database": "tests.e2e.postgresql.setup.database",
        "target": "tests.e2e.postgresql.setup.target",
    },
    "postgresql": {
        "cleanup": "tests.e2e.postgresql.setup.cleanup",
        "database": "tests.e2e.postgresql.setup.database",
        "target": "tests.e2e.postgresql.setup.target",
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shared acceptance E2E cleanup — destroy E2E databases across engines."
    )
    parser.add_argument(
        "--engine",
        required=True,
        choices=sorted(ENGINE_MAP.keys()),
        help="Database engine to clean up.",
    )
    parser.add_argument("--config", help="Path to acceptance YAML config.")
    args = parser.parse_args()

    cfg: E2EConfig = load_e2e_config(args.config)

    mods = {key: importlib.import_module(val) for key, val in ENGINE_MAP[args.engine].items()}
    cleanup_mod = mods["cleanup"]
    db_mod = mods["database"]
    target_mod = mods["target"]

    params = db_mod._resolve_connection_params(
        host=cfg.source_connection["host"],
        port=cfg.source_connection["port"],
        username=cfg.source_connection["username"],
        password_env=cfg.source_connection["password_secret"],
    )

    source_db = cfg.source_db
    target_db = cfg.target_db
    pattern = cfg.safe_pattern

    # Safety: refuse to clean protected databases
    if target_mod.is_protected_database(source_db) or target_mod.is_protected_database(target_db):
        print("[!] Refusing to clean a protected database.")
        sys.exit(1)

    print(f"[CLEANUP] Engine: {args.engine.upper()}")
    print(f"  Databases: {source_db}, {target_db}")
    print(f"  Pattern:   {pattern}")
    print(f"  Server:    {params['host']}:{params['port']}")
    print()

    # engine-specific keyword arguments for cleanup_databases
    if args.engine == "mssql":
        cleanup_databases = lambda: cleanup_mod.cleanup_databases(
            params["host"], params["port"],
            params["username"], params["password"],
            [source_db, target_db], pattern,
        )
    else:
        cleanup_databases = lambda: cleanup_mod.cleanup_databases(
            params["host"], params["port"],
            params["user"], params.get("password"),
            [source_db, target_db], pattern,
        )

    cleanup_databases()
    print("\n[CLEANUP] All E2E databases destroyed successfully.")


if __name__ == "__main__":
    main()
