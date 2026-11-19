from __future__ import annotations

from typing import Any


def schema_exists(conn: Any, schema_name: str) -> bool:
    if conn is None or not schema_name:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM SYSCAT.SCHEMATA WHERE SCHEMANAME = ?", (schema_name.strip(),))
            rows = cur.fetchall()
            return bool(rows)
    except Exception:
        raise


def create_schema_if_missing(conn: Any, schema_name: str) -> None:
    if conn is None or not schema_name:
        return
    if schema_exists(conn, schema_name):
        return
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema_name.strip()}"')
    except Exception:
        raise


def list_tables_in_schema(conn: Any, schema_name: str) -> list[str]:
    if conn is None or not schema_name:
        return []
    tables = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE = 'T'", (schema_name.strip(),))
            for row in cur.fetchall():
                if row and row[0]:
                    tables.append(str(row[0]).strip())
    except Exception:
        raise
    return tables
