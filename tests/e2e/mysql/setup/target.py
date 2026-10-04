"""Target database setup/reset for MySQL E2E.

Reuses ``database.py`` for the safety guard and reset logic — no duplication
of the name-matching or DROP/CREATE pattern.

MySQL-specific protected databases include system schemas and the production
migration databases.
"""
from __future__ import annotations

from tests.e2e.mysql.setup.database import (
    connect_maintenance,
    database_exists,
    is_safe_e2e_database,
    reset_database,
)

# Databases that must NEVER be dropped, even if they happen to live on the
# same MySQL instance.  This includes MySQL system schemas and databases used
# by the production migration flow.
PROTECTED_DATABASES: frozenset[str] = frozenset({
    "mysql",
    "information_schema",
    "performance_schema",
    "sys",
    "migrationsource_mysql",
    "migrationtarget_mysql",
    "MigrationSource_MySQL",
    "MigrationTarget_MySQL",
})


def is_protected_database(db_name: str) -> bool:
    """Return True if *db_name* must never be dropped by E2E tooling."""
    return db_name in PROTECTED_DATABASES


def reset_target_database(
    host: str,
    port: int | str,
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
            f"Protected databases: {sorted(PROTECTED_DATABASES)}"
        )
    if not is_safe_e2e_database(db_name, pattern):
        raise ValueError(
            f"Refusing to reset {db_name!r}: does not match E2E pattern."
        )
    return reset_database(host, port, username, password, db_name, pattern)


def target_database_exists(
    host: str, port: int | str, username: str, password: str, db_name: str
) -> bool:
    """Check whether the target database exists on the server."""
    if is_protected_database(db_name):
        return False
    conn = connect_maintenance(host, port, username, password)
    try:
        return database_exists(conn, db_name)
    finally:
        conn.close()
