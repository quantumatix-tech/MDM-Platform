from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def _existing_routine_def(conn, schema_name, routine_name):
    with conn.cursor() as cur:
        cur.execute("SELECT LENGTH(TEXT) FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?", (schema_name, routine_name))
        row = cur.fetchone()
        if not row:
            return None
        text_len = int(row[0]) if row[0] is not None else 0
        if text_len <= 4000:
            cur.execute("SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?", (schema_name, routine_name))
            r = cur.fetchone()
            return (str(r[0]) if r and r[0] is not None else "")
        chunks = []
        pos = 1
        while pos <= text_len:
            size = min(4000, text_len - pos + 1)
            cur.execute(f"SELECT CAST(SUBSTR(TEXT, {pos}, {size}) AS VARCHAR({size})) FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?", (schema_name, routine_name))
            r = cur.fetchone()
            chunks.append(str(r[0]) if r and r[0] is not None else "")
            pos += size
        return "".join(chunks)


def create_routine_if_missing(conn: Any, schema_name: str, routine: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(routine["name"], "routine")
    existing = _existing_routine_def(conn, schema_name, routine["name"])
    if existing is not None:
        if existing != routine["definition"]:
            raise RuntimeError(f"Conflicting routine definition for {schema_name}.{routine['name']}")
        return "skipped-existing"
    with conn.cursor() as cur:
        cur.execute(routine["definition"])
    return "created"
