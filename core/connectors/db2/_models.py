from __future__ import annotations

from typing import Any


DEFAULT_DB2_PORT = 25000
DEFAULT_DB2_DRIVER = "IBM DB2 ODBC DRIVER - DB2COPY1"


def _build_db2_conn_str(config: dict[str, Any]) -> str:
    """Build a DSN-less IBM Db2 ODBC connection string.

    Uses the format verified in the environment:
      DRIVER={IBM DB2 ODBC DRIVER - DB2COPY1};DATABASE=...;HOSTNAME=...;PORT=...;PROTOCOL=TCPIP;UID=...;PWD=...
    """
    driver = config.get("driver") or config.get("odbc_driver") or DEFAULT_DB2_DRIVER
    database = config.get("database")
    host = config.get("host") or config.get("hostname") or config.get("server")
    port = config.get("port", DEFAULT_DB2_PORT)
    username = config.get("username") or config.get("user") or config.get("uid")
    password = config.get("password") or ""

    if not database:
        raise ValueError("Db2 connection requires 'database'")
    if not host:
        raise ValueError("Db2 connection requires 'host' (or 'hostname'/'server')")
    if not username:
        raise ValueError("Db2 connection requires 'username' (or 'user'/'uid')")

    # Build without extra quoting around password; rely on pyodbc/ODBC to handle
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"DATABASE={database};"
        f"HOSTNAME={host};"
        f"PORT={port};"
        f"PROTOCOL=TCPIP;"
        f"UID={username};"
        f"PWD={password};"
    )

    # Optional tuning knobs (if provided)
    if config.get("autocommit") is not None:
        conn_str += f"AUTOCOMMIT={'YES' if config.get('autocommit') else 'NO'};"
    if config.get("connection_timeout") is not None:
        conn_str += f"CONNECTION_TIMEOUT={config['connection_timeout']};"

    return conn_str
