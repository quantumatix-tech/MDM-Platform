from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def discover_table_comments(conn: Any, schema_name: str) -> list[dict]:
    validate_identifier(schema_name, "schema")
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT TABNAME, REMARKS FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TYPE IN ('T','V')", (schema_name,))
        for r in cur.fetchall():
            out.append({"object_type": "TABLE", "table": str(r[0]).strip(), "comment": (None if r[1] is None else str(r[1]))})
    return out


def discover_column_comments(conn: Any, schema_name: str) -> list[dict]:
    validate_identifier(schema_name, "schema")
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT TABNAME, COLNAME, REMARKS FROM SYSCAT.COLUMNS WHERE TABSCHEMA=?", (schema_name,))
        for r in cur.fetchall():
            out.append({"object_type": "COLUMN", "table": str(r[0]).strip(), "column": str(r[1]).strip(), "comment": (None if r[2] is None else str(r[2]))})
    return out


def _existing_table_comment(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute("SELECT REMARKS FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (schema, table))
        row = cur.fetchone()
    return None if not row or row[0] is None else str(row[0])


def _existing_column_comment(conn, schema, table, column):
    with conn.cursor() as cur:
        cur.execute("SELECT REMARKS FROM SYSCAT.COLUMNS WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=?", (schema, table, column))
        row = cur.fetchone()
    return None if not row or row[0] is None else str(row[0])


def _escape(s):
    return s.replace("'", "''")


def apply_table_comment(conn, schema_name, table, comment):
    validate_identifier(schema_name, "schema")
    validate_identifier(table, "table")
    existing = _existing_table_comment(conn, schema_name, table)
    if existing is None and (comment is None or comment == ""):
        return "already_present"
    if existing == comment:
        return "already_present"
    if existing not in (None, "") and existing != comment:
        return "conflict"
    if comment is None or comment == "":
        return "skipped"
    sql = f"COMMENT ON TABLE {schema_name}.{table} IS '{_escape(comment)}'"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "applied"


def apply_column_comment(conn, schema_name, table, column, comment):
    validate_identifier(schema_name, "schema")
    validate_identifier(table, "table")
    validate_identifier(column, "column")
    existing = _existing_column_comment(conn, schema_name, table, column)
    if existing is None and (comment is None or comment == ""):
        return "already_present"
    if existing == comment:
        return "already_present"
    if existing not in (None, "") and existing != comment:
        return "conflict"
    if comment is None or comment == "":
        return "skipped"
    sql = f"COMMENT ON COLUMN {schema_name}.{table}.{column} IS '{_escape(comment)}'"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "applied"
