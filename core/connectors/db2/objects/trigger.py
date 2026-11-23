from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def _retrieve_text(conn, schema_name, trigger_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT LENGTH(TEXT) FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?",
            (schema_name, trigger_name),
        )
        row = cur.fetchone()
        text_len = int(row[0]) if row and row[0] is not None else 0
        if text_len <= 4000:
            cur.execute(
                "SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?",
                (schema_name, trigger_name),
            )
            r = cur.fetchone()
            return (str(r[0]) if r and r[0] is not None else ""), text_len
        chunks = []
        pos = 1
        while pos <= text_len:
            size = min(4000, text_len - pos + 1)
            cur.execute(
                f"SELECT CAST(SUBSTR(TEXT, {pos}, {size}) AS VARCHAR({size})) FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? AND TRIGNAME=?",
                (schema_name, trigger_name),
            )
            r = cur.fetchone()
            chunks.append(str(r[0]) if r and r[0] is not None else "")
            pos += size
        return "".join(chunks), text_len


def discover_triggers(conn: Any, schema_name: str, include_invalid: bool = False) -> list[dict]:
    validate_identifier(schema_name, "schema")
    triggers = []
    with conn.cursor() as cur:
        where_valid = "" if include_invalid else "AND VALID='Y'"
        cur.execute(
            f"SELECT TRIGNAME, VALID, ENABLED FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA=? {where_valid} ORDER BY TRIGNAME",
            (schema_name,),
        )
        rows = cur.fetchall()
    for row in rows:
        name = str(row[0]).strip()
        valid = str(row[1]).strip() if row[1] else "N"
        enabled = str(row[2]).strip() if row[2] else "N"
        validate_identifier(name, "trigger")
        text, text_len = _retrieve_text(conn, schema_name, name)
        if len(text) != text_len:
            raise RuntimeError(f"Trigger definition length mismatch for {schema_name}.{name}")
        triggers.append({"name": name, "definition": text, "valid": valid, "enabled": enabled})
    return triggers
