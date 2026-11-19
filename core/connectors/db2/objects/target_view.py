from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier
from core.connectors.db2.objects.ddl import _quote_ident


def _existing_view(conn, schema_name, view_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT LENGTH(TEXT) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?",
            (schema_name, view_name),
        )
        row = cur.fetchone()
        if not row:
            return None
        text_len = int(row[0]) if row[0] is not None else 0
        if text_len <= 4000:
            cur.execute(
                "SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?",
                (schema_name, view_name),
            )
            r = cur.fetchone()
            return (str(r[0]).strip() if r and r[0] is not None else "")
        chunks = []
        pos = 1
        while pos <= text_len:
            size = min(4000, text_len - pos + 1)
            cur.execute(
                f"SELECT CAST(SUBSTR(TEXT, {pos}, {size}) AS VARCHAR({size})) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?",
                (schema_name, view_name),
            )
            r = cur.fetchone()
            chunks.append(str(r[0]) if r and r[0] is not None else "")
            pos += size
        return "".join(chunks).strip()


def create_view_if_missing(conn: Any, schema_name: str, view: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(view["name"], "view")
    existing = _existing_view(conn, schema_name, view["name"])
    if existing is not None:
        if existing != view["definition"].strip():
            raise RuntimeError(f"Conflicting view definition for {schema_name}.{view['name']}")
        return "skipped-existing"
    sql = view["definition"]
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"
