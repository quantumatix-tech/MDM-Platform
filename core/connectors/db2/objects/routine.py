from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def _retrieve_text(conn, schema_name, name, specific):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT LENGTH(TEXT) FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=? AND SPECIFICNAME=?",
            (schema_name, name, specific),
        )
        row = cur.fetchone()
        text_len = int(row[0]) if row and row[0] is not None else 0
        if text_len <= 4000:
            cur.execute(
                "SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=? AND SPECIFICNAME=?",
                (schema_name, name, specific),
            )
            r = cur.fetchone()
            return (str(r[0]) if r and r[0] is not None else ""), text_len
        chunks = []
        pos = 1
        while pos <= text_len:
            size = min(4000, text_len - pos + 1)
            cur.execute(
                f"SELECT CAST(SUBSTR(TEXT, {pos}, {size}) AS VARCHAR({size})) FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=? AND SPECIFICNAME=?",
                (schema_name, name, specific),
            )
            r = cur.fetchone()
            chunks.append(str(r[0]) if r and r[0] is not None else "")
            pos += size
        return "".join(chunks), text_len


def discover_routines(conn: Any, schema_name: str) -> list[dict]:
    validate_identifier(schema_name, "schema")
    routines = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ROUTINENAME, SPECIFICNAME, ROUTINETYPE, LANGUAGE FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND LANGUAGE='SQL' ORDER BY ROUTINENAME, SPECIFICNAME",
            (schema_name,),
        )
        rows = cur.fetchall()
    for r in rows:
        if not r:
            continue
        name = str(r[0]).strip()
        specific = str(r[1]).strip() if r[1] else name
        rtype = str(r[2]).strip().upper() if r[2] else "F"
        lang = str(r[3]).strip().upper() if r[3] else "SQL"
        text, text_len = _retrieve_text(conn, schema_name, name, specific)
        if len(text) != text_len:
            raise RuntimeError(f"Routine definition length mismatch for {schema_name}.{name}")
        routines.append({"name": name, "specific": specific, "type": rtype, "language": lang, "definition": text})
    return routines
