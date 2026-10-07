"""MySQL database setup/reset utilities for E2E testing.

Provides the safety-gated connection, existence, drop, create, and reset
operations that Phase A relies on.  Every destructive operation is guarded by
``is_safe_e2e_database`` — only databases whose name matches the E2E regex may
be created or dropped, preventing accidental destruction of production or
shared databases on the local MySQL instance.

MySQL differences from the PostgreSQL equivalent:
  * No ``postgres`` maintenance database — the entry point is the ``mysql``
    system database.
  * MySQL has no superuser-reserved DROP DATABASE outside a transaction;
    MySQL's DDL is implicitly committed.
  * ``DROP DATABASE IF EXISTS`` is idempotent — no separate existence probe
    is needed on the create path.
"""
from __future__ import annotations

import os
import re
from typing import Any

import mysql.connector
from mysql.connector.connection import MySQLConnection

from core.connectors.mysql._models import _q

E2E_DB_NAME_PREFIX = "MigrationE2E_MySQL_"
DEFAULT_SERVER = "127.0.0.1"
DEFAULT_PORT = 33062
DEFAULT_USER = "root"
MAINTENANCE_DB = "mysql"


def _resolve_password(password_env: str) -> str | None:
    """Resolve a password from the environment.

    Mirrors the platform's EnvSecretProvider convention: a config value of
    ``password_secret: mysql_e2e_source_pass`` resolves to the environment
    variable ``SECRET_mysql_e2e_source_pass``.

    Returns ``None`` when the variable is unset.
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
    environment variable is not set, the ``password`` key is omitted from
    the returned dict so callers fall back to MySQL's default authentication
    (e.g. trust auth or socket auth on a local instance).
    """
    password = _resolve_password(password_env) if password_env else None
    params: dict[str, Any] = {
        "host": host or os.environ.get("MYSQL_HOST", DEFAULT_SERVER),
        "port": port or int(os.environ.get("MYSQL_PORT", DEFAULT_PORT)),
        "user": username or os.environ.get("MYSQL_USERNAME", DEFAULT_USER),
        "connect_timeout": 10,
    }
    if password is not None:
        params["password"] = password
    return params


def connect_maintenance(
    host: str,
    port: int | str,
    username: str,
    password: str,
) -> MySQLConnection:
    """Connect to the ``mysql`` system database (maintenance entry point)."""
    conn = mysql.connector.connect(
        host=host,
        port=int(port),
        user=username,
        password=password,
        database=MAINTENANCE_DB,
        connection_timeout=10,
    )
    return conn


def is_safe_e2e_database(db_name: str, pattern: str | None = None) -> bool:
    """Return True only when *db_name* matches the E2E naming convention.

    Never allow destructive operations (DROP DATABASE) on arbitrary names.
    """
    regex = pattern or r"^MigrationE2E_MySQL_.+$"
    return bool(re.match(regex, db_name))


def database_exists(conn: MySQLConnection, db_name: str) -> bool:
    """Check whether a database exists on the server."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
            (db_name,),
        )
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
            f"E2E safety pattern ({pattern or r'^MigrationE2E_MySQL_.+$'}). "
            "This guard prevents accidental destruction of non-E2E databases."
        )

    conn = connect_maintenance(host, port, username, password)
    try:
        if not database_exists(conn, db_name):
            return False
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_q(db_name)}")
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
            f"E2E safety pattern ({pattern or r'^MigrationE2E_MySQL_.+$'}). "
            "This guard ensures only designated E2E databases are managed."
        )

    conn = connect_maintenance(host, port, username, password)
    try:
        existed = database_exists(conn, db_name)
        if not existed:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE {_q(db_name)} "
                    f"DEFAULT CHARACTER SET utf8mb4 "
                    f"DEFAULT COLLATE utf8mb4_0900_ai_ci"
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
