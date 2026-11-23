from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def _existing_trigger_def(conn, schema_name, trigger_name):
    with conn.cursor() as cur:
        cur.execute("SELECT LENGTH(TEXT) FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?", (schema_name, trigger_name))
        row = cur.fetchone()
        if not row:
            return None
        text_len = int(row[0]) if row[0] is not None else 0
        if text_len <= 4000:
            cur.execute("SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?", (schema_name, trigger_name))
            r = cur.fetchone()
            return (str(r[0]) if r and r[0] is not None else "")
        chunks = []
        pos = 1
        while pos <= text_len:
            size = min(4000, text_len - pos + 1)
            cur.execute(f"SELECT CAST(SUBSTR(TEXT, {pos}, {size}) AS VARCHAR({size})) FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?", (schema_name, trigger_name))
            r = cur.fetchone()
            chunks.append(str(r[0]) if r and r[0] is not None else "")
            pos += size
        return "".join(chunks)


def create_trigger_if_missing(conn: Any, schema_name: str, trigger: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(trigger["name"], "trigger")
    existing = _existing_trigger_def(conn, schema_name, trigger["name"])
    if existing is not None:
        if existing != trigger["definition"]:
            raise RuntimeError(f"Conflicting trigger definition for {schema_name}.{trigger['name']}")
        return "skipped-existing"
    with conn.cursor() as cur:
        cur.execute(trigger["definition"])
    # validate
    with conn.cursor() as cur:
        cur.execute("SELECT VALID, ENABLED FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?", (schema_name, trigger["name"]))
        row = cur.fetchone()
        if not row or str(row[0]).upper() != "Y":
            raise RuntimeError(f"Trigger {schema_name}.{trigger['name']} is not valid after creation")
    return "created"
