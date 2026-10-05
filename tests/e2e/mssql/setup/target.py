"""Target database setup/reset for MSSQL E2E.

Reuses ``database.py`` for the safety guard and reset logic — no
duplication of the name-matching or DROP/CREATE pattern.
"""
from __future__ import annotations

from tests.e2e.mssql.setup.database import (
    connect_master,
    database_exists,
    is_safe_e2e_database,
    reset_database,
)

# Databases that must NEVER be dropped, even if they happen to live on the
# same SQL Server instance.
PROTECTED_DATABASES: frozenset[str] = frozenset({
    "master",
    "model",
    "msdb",
    "tempdb",
    "MigrationSource_MSSQL",
    "MigrationTarget_MSSQL",
})


def is_protected_database(db_name: str) -> bool:
    """Return True if *db_name* must never be dropped by E2E tooling."""
    return db_name in PROTECTED_DATABASES


def reset_target_database(
    host: str,
    port: str,
    username: str,
    password: str,
    db_name: str,
    pattern: str | None = None,
) -> bool:
    """Drop and recreate the E2E target database.

    Delegates to ``database.reset_database`` after confirming the name is
    both E2E-safe and not protected.
    """
    if is_protected_database(db_name):
        raise ValueError(
            f"Refusing to reset protected database {db_name!r}. "
            "This database is used by the production migration flow."
        )
    if not is_safe_e2e_database(db_name, pattern):
        raise ValueError(
            f"Refusing to reset {db_name!r}: does not match E2E pattern."
        )
    return reset_database(host, port, username, password, db_name, pattern)


def target_database_exists(host: str, port: str, username: str, password: str,
                           db_name: str) -> bool:
    """Check whether the target database exists on the server."""
    if is_protected_database(db_name):
        return False
    conn = connect_master(host, port, username, password)
    try:
        return database_exists(conn, db_name)
    finally:
        conn.close()


def get_database_state(conn, db_name: str) -> str | None:
    """Return the state_desc of *db_name* (e.g. 'ONLINE'), or None if missing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state_desc FROM sys.databases WHERE name = ?", (db_name,)
        )
        row = cur.fetchone()
        return row[0] if row else None
