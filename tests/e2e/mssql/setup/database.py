from __future__ import annotations

import os
import re

import pyodbc

E2E_DB_NAME_PREFIX = "MigrationE2E_MSSQL_"
DEFAULT_SERVER = "localhost"
DEFAULT_PORT = "1533"
DEFAULT_USER = "sa"
MASTER_DB = "master"


def _resolve_connection_params(
    host: str | None = None,
    port: str | None = None,
    username: str | None = None,
    password_env: str | None = None,
) -> dict[str, str]:
    """Resolve connection parameters, pulling the password from an env var."""
    password = os.environ.get(f"SECRET_{password_env}") if password_env else None
    if not password:
        env_vars = [f"SECRET_{password_env}"] if password_env else []
        raise SystemExit(
            "MSSQL E2E password not found.\n"
            f"Set the environment variable(s): {env_vars}\n"
            "Example (PowerShell):\n"
            "  $env:SECRET_mssql_e2e_source_pass = Read-Host -AsSecureString | "
            "ConvertFrom-SecureString -AsPlainText"
        )
    return {
        "host": host or os.environ.get("MSSQL_HOST", DEFAULT_SERVER),
        "port": port or os.environ.get("MSSQL_PORT", DEFAULT_PORT),
        "username": username or os.environ.get("MSSQL_USERNAME", DEFAULT_USER),
        "password": password,
    }


def make_conn_str(
    host: str,
    port: str,
    username: str,
    password: str,
    database: str | None = None,
    trust_server_cert: bool = True,
) -> str:
    parts = [
        "DRIVER={ODBC Driver 18 for SQL Server}",
        f"SERVER={host},{port}",
    ]
    if database:
        parts.append(f"DATABASE={database}")
    parts.append(f"UID={username}")
    parts.append(f"PWD={password}")
    parts.append("Encrypt=no")
    parts.append(f"TrustServerCertificate={'yes' if trust_server_cert else 'no'}")
    return ";".join(parts) + ";"


def connect_master(
    host: str, port: str, username: str, password: str
) -> pyodbc.Connection:
    conn_str = make_conn_str(host, port, username, password, database=MASTER_DB)
    return pyodbc.connect(conn_str, timeout=5)


def is_safe_e2e_database(db_name: str, pattern: str | None = None) -> bool:
    """Return True only when *db_name* matches the E2E naming convention.

    Never allow destructive operations (DROP DATABASE) on arbitrary names.
    """
    regex = pattern or r"^MigrationE2E_MSSQL_.+$"
    return bool(re.match(regex, db_name))


def database_exists(conn: pyodbc.Connection, db_name: str) -> bool:
    """Check whether a database exists on the server (querying from master)."""
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM sys.databases WHERE name = ?", (db_name,))
        return cur.fetchone() is not None


def drop_database(
    host: str, port: str, username: str, password: str, db_name: str,
    pattern: str | None = None,
) -> bool:
    """Drop a database — only permitted when the name matches the E2E pattern.

    Returns True if the database was dropped (existed).
    """
    if not is_safe_e2e_database(db_name, pattern):
        raise ValueError(
            f"Refusing to drop database {db_name!r}: name does not match the "
            f"E2E safety pattern ({pattern or r'^MigrationE2E_MSSQL_.+$'}). "
            "This guard prevents accidental destruction of non-E2E databases."
        )

    conn = connect_master(host, port, username, password)
    try:
        if not database_exists(conn, db_name):
            return False
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER DATABASE [{db_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
            )
            cur.execute(f"DROP DATABASE [{db_name}]")
        return True
    finally:
        conn.close()


def create_database(
    host: str, port: str, username: str, password: str, db_name: str,
    pattern: str | None = None,
) -> bool:
    """Create a database if it does not already exist.

    The *db_name* must match the E2E safety pattern.
    Returns True if a new database was created.
    """
    if not is_safe_e2e_database(db_name, pattern):
        raise ValueError(
            f"Refusing to create database {db_name!r}: name does not match the "
            f"E2E safety pattern ({pattern or r'^MigrationE2E_MSSQL_.+$'}). "
            "This guard ensures only designated E2E databases are managed."
        )

    conn = connect_master(host, port, username, password)
    try:
        existed = database_exists(conn, db_name)
        if not existed:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE [{db_name}]")
        return not existed
    finally:
        conn.close()


def reset_database(
    host: str, port: str, username: str, password: str, db_name: str,
    pattern: str | None = None,
) -> bool:
    """Drop and recreate an E2E database.

    Returns True if a fresh database was created.
    """
    drop_database(host, port, username, password, db_name, pattern)
    create_database(host, port, username, password, db_name, pattern)
    return True


def ensure_e2e_database(
    host: str, port: str, username: str, password: str, db_name: str,
    pattern: str | None = None,
) -> bool:
    """Create the database if it does not exist (idempotent, never drops)."""
    return create_database(host, port, username, password, db_name, pattern)
