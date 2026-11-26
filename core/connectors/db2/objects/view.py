from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def discover_views(conn: Any, schema_name: str) -> list[dict]:
    validate_identifier(schema_name, "schema")
    views = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT V.VIEWNAME, LENGTH(V.TEXT) FROM SYSCAT.VIEWS V WHERE V.VIEWSCHEMA=? AND EXISTS (SELECT 1 FROM SYSCAT.TABLES T WHERE T.TABSCHEMA=V.VIEWSCHEMA AND T.TABNAME=V.VIEWNAME AND T.TYPE='V') ORDER BY V.VIEWNAME",
            (schema_name,),
        )
        names_lens = cur.fetchall()
        for row in names_lens:
            if not row:
                continue
            view_name = str(row[0]).strip()
            text_len = int(row[1]) if row[1] is not None else 0
            validate_identifier(view_name, "view")
            # retrieve text via cast to avoid CLOB HY106
            if text_len <= 4000:
                cur.execute(
                    "SELECT CAST(TEXT AS VARCHAR(4000)) FROM SYSCAT.VIEWS WHERE VIEWSCHEMA=? AND VIEWNAME=?",
                    (schema_name, view_name),
                )
                r = cur.fetchone()
                definition = str(r[0]).strip() if r and r[0] is not None else ""
            else:
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
                definition = "".join(chunks)
            if len(definition) != text_len:
                raise RuntimeError(f"View definition length mismatch for {schema_name}.{view_name}: expected {text_len}, got {len(definition)}")
            views.append({"name": view_name, "definition": definition})
    return views
