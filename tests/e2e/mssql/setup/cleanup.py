"""Safe E2E database cleanup for MSSQL.

Drops and recreates the E2E source and target databases so the full
E2E pipeline can be re-run from a clean state.  Reuses the safety
guards from ``database.py`` — every name is checked against the E2E
pattern and the protected-database set before any DROP is issued.
"""
from __future__ import annotations

import time

from tests.e2e.mssql.setup.database import (
    is_safe_e2e_database,
    reset_database,
)
from tests.e2e.mssql.setup.target import PROTECTED_DATABASES, is_protected_database


def cleanup_databases(
    host: str,
    port: str,
    username: str,
    password: str,
    db_names: list[str],
    pattern: str | None = None,
) -> dict[str, bool]:
    """Reset each E2E database (drop + recreate).

    Parameters
    ----------
    db_names
        Database names to reset.  Each must pass both the E2E safety
        pattern and the protected-database guard.

    Returns
    -------
    ``{db_name: True}`` for each database that was successfully reset.
    """
    results: dict[str, bool] = {}
    for db_name in db_names:
        if is_protected_database(db_name):
            raise ValueError(
                f"Refusing to clean up protected database {db_name!r}. "
                f"Protected databases: {sorted(PROTECTED_DATABASES)}"
            )
        if not is_safe_e2e_database(db_name, pattern):
            raise ValueError(
                f"Refusing to clean up {db_name!r}: does not match E2E "
                f"safety pattern ({pattern or r'^MigrationE2E_MSSQL_.+$'})."
            )
        start = time.time()
        reset_database(host, port, username, password, db_name, pattern)
        elapsed = time.time() - start
        results[db_name] = True
        print(f"  [CLEANUP] {db_name} reset ({elapsed:.1f}s)")
    return results


def is_cleanup_safe(
    db_names: list[str], pattern: str | None = None
) -> tuple[bool, list[str]]:
    """Return ``(True, [])`` if all names are safe to clean up,
    otherwise ``(False, [reasons])``."""
    reasons: list[str] = []
    for db_name in db_names:
        if is_protected_database(db_name):
            reasons.append(
                f"{db_name!r} is in the protected list "
                f"({sorted(PROTECTED_DATABASES)})"
            )
        if not is_safe_e2e_database(db_name, pattern):
            reasons.append(
                f"{db_name!r} does not match E2E safety pattern "
                f"({pattern or r'^MigrationE2E_MSSQL_.+$'})"
            )
    return (len(reasons) == 0, reasons)
