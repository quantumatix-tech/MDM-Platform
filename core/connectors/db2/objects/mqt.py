from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def discover_mqts(conn: Any, schema_name: str) -> list[dict]:
    validate_identifier(schema_name, "schema")
    mqts = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TYPE='S' ORDER BY TABNAME",
            (schema_name,),
        )
        names = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
        for name in names:
            # retrieve DDL from SYSCAT.VIEWS.TEXT
            cur.execute("SELECT LENGTH(TEXT) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?", (schema_name, name))
            row = cur.fetchone()
            text_len = int(row[0]) if row and row[0] is not None else 0
            if text_len <= 4000:
                cur.execute("SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?", (schema_name, name))
                r = cur.fetchone()
                ddl = str(r[0]).strip() if r and r[0] is not None else ""
            else:
                chunks = []
                pos = 1
                while pos <= text_len:
                    size = min(4000, text_len - pos + 1)
                    cur.execute(f"SELECT CAST(SUBSTR(TEXT, {pos}, {size}) AS VARCHAR({size})) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?", (schema_name, name))
                    r = cur.fetchone()
                    chunks.append(str(r[0]) if r and r[0] is not None else "")
                    pos += size
                ddl = "".join(chunks).strip()
            if len(ddl) != text_len:
                raise RuntimeError(f"MQT definition length mismatch for {schema_name}.{name}")
            mqts.append({"name": name, "definition": ddl})
    return mqts
