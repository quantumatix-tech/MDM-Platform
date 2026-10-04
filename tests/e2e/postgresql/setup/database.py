"""PostgreSQL database setup/reset utilities for E2E testing.

Provides the safety-gated connection, existence, drop, create, and reset
operations that Phase A relies on.  Every destructive operation is guarded by
``is_safe_e2e_database`` — only databases whose name matches the E2E regex may
be created or dropped, preventing accidental destruction of production or
shared databases on the local PostgreSQL instance.

PostgreSQL differences from the MSSQL equivalent:
  * No ``master`` database — the maintenance entry point is the ``postgres``
    database.
  * ``DROP DATABASE`` cannot run inside a transaction and requires
    ``autocommit``; ``psycopg`` connections default to autocommit=False, so we
    toggle it explicitly.
  * ``DROP DATABASE IF EXISTS`` is idempotent — no separate existence probe is
    needed on the create path.
"""
from __future__ import annotations

import os
import re
from typing import Any

import psycopg

from core.connectors.base import quote_identifier

E2E_DB_NAME_PREFIX = "MigrationE2E_PostgreSQL_"
DEFAULT_SERVER = "127.0.0.1"
DEFAULT_PORT = 55432
DEFAULT_USER = "postgres"
MAINTENANCE_DB = "postgres"


def _resolve_password(password_env: str) -> str | None:
    """Resolve a password from the environment.

    Mirrors the platform's ``EnvSecretProvider`` convention: a config value of
    ``password_secret: postgresql_e2e_source_pass`` resolves to the environment
    variable ``SECRET_postgresql_e2e_source_pass``.

    Returns ``None`` when the variable is unset, so that PostgreSQL instances
    using ``trust`` authentication (the default for local development) can
    connect without a password.
    """
    return os.environ.get(f"SECRET_{password_env}")


def _resolve_connection_params(
    host: str | None = None,
    port: int | str | None = None,
    username: str | None = None,
    password_env: str | None = None,
) -> dict[str, Any]:
    """Resolve connection parameters, pulling the password from an env var.

    When ``password_env`` is provided but the corresponding ``SECRET_*``
    environment variable is not set, ``password`` is omitted from the
    returned dict so that PostgreSQL instances with ``trust`` authentication
    can connect without one.
    """
    password = _resolve_password(password_env) if password_env else None
    params: dict[str, Any] = {
        "host": host or os.environ.get("POSTGRESQL_HOST", DEFAULT_SERVER),
        "port": port or os.environ.get("POSTGRESQL_PORT", DEFAULT_PORT),
        "user": username or os.environ.get("POSTGRESQL_USERNAME", DEFAULT_USER),
        "password": "",
        "connect_timeout": 10,
    }
    if password is not None:
        params["password"] = password
    return params


def connect_maintenance(
    host: str, port: int | str, username: str, password: str
) -> psycopg.Connection:
    """Connect to the ``postgres`` maintenance database.

    ``CREATE DATABASE`` / ``DROP DATABASE`` must run outside any transaction
    block, so the returned connection is set to ``autocommit``.
    """
    conn = psycopg.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        dbname=MAINTENANCE_DB,
        connect_timeout=10,
        autocommit=True,
    )
    return conn


def is_safe_e2e_database(db_name: str, pattern: str | None = None) -> bool:
    """Return True only when *db_name* matches the E2E naming convention.

    Never allow destructive operations (DROP DATABASE) on arbitrary names.
    """
    regex = pattern or r"^MigrationE2E_PostgreSQL_.+$"
    return bool(re.match(regex, db_name))


def database_exists(conn: psycopg.Connection, db_name: str) -> bool:
    """Check whether a database exists on the server.

    *conn* must already be in ``autocommit`` mode (i.e. connected to the
    maintenance database), because ``pg_database`` queries are not transactional
    but we need to avoid starting a transaction implicitly.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        return cur.fetchone() is not None


def drop_database(
    host: str,
    port: int | str,
    username: str,
    password: str,
    db_name: str,
    pattern: str | None = None,
) -> bool:
    """Drop a database — only permitted when the name matches the E2E pattern.

    Returns True if the database was dropped (existed).
    Raises ValueError if the name fails the safety check.
    """
    if not is_safe_e2e_database(db_name, pattern):
        raise ValueError(
            f"Refusing to drop database {db_name!r}: name does not match the "
            f"E2E safety pattern ({pattern or r'^MigrationE2E_PostgreSQL_.+$'}). "
            "This guard prevents accidental destruction of non-E2E databases."
        )

    conn = connect_maintenance(host, port, username, password)
    try:
        if not database_exists(conn, db_name):
            return False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {quote_identifier(db_name)}")
        return True
    finally:
        conn.close()


def create_database(
    host: str,
    port: int | str,
    username: str,
    password: str,
    db_name: str,
    pattern: str | None = None,
) -> bool:
    """Create a database if it does not already exist.

    The *db_name* must match the E2E safety pattern.
    Returns True if a new database was created.
    Raises ValueError if the name fails the safety check.
    """
    if not is_safe_e2e_database(db_name, pattern):
        raise ValueError(
            f"Refusing to create database {db_name!r}: name does not match the "
            f"E2E safety pattern ({pattern or r'^MigrationE2E_PostgreSQL_.+$'}). "
            "This guard ensures only designated E2E databases are managed."
        )

    conn = connect_maintenance(host, port, username, password)
    try:
        existed = database_exists(conn, db_name)
        if not existed:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE {quote_identifier(db_name)} "
                    f"OWNER {quote_identifier(username)} "
                    f"ENCODING 'UTF8' "
                    f"TEMPLATE template0"
                )
        return not existed
    finally:
        conn.close()


def reset_database(
    host: str,
    port: int | str,
    username: str,
    password: str,
    db_name: str,
    pattern: str | None = None,
) -> bool:
    """Drop and recreate an E2E database.

    Returns True if a fresh database was created.
    """
    drop_database(host, port, username, password, db_name, pattern)
    create_database(host, port, username, password, db_name, pattern)
    return True


def ensure_e2e_database(
    host: str,
    port: int | str,
    username: str,
    password: str,
    db_name: str,
    pattern: str | None = None,
) -> bool:
    """Create the database if it does not exist (idempotent, never drops)."""
    return create_database(host, port, username, password, db_name, pattern)
