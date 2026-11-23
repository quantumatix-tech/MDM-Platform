from __future__ import annotations

from typing import Any


def discover_identity_columns(conn: Any, schema_name: str, table_name: str) -> list[dict]:
    cols = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLNAME, TYPENAME, IDENTITY, GENERATED
            FROM SYSCAT.COLUMNS
            WHERE TABSCHEMA=? AND TABNAME=? AND IDENTITY='Y'
            ORDER BY COLNAME
            """,
            (schema_name, table_name),
        )
        for r in cur.fetchall():
            if not r:
                continue
            colname = str(r[0]).strip()
            typename = str(r[1]).strip() if r[1] else ""
            generated = str(r[3]).strip() if len(r) > 3 and r[3] else "D"
            # colidentattributes
            cur.execute(
                """
                SELECT INCREMENT, MINVALUE, MAXVALUE, CYCLE, CACHE, "ORDER", NEXTCACHEFIRSTVALUE
                FROM SYSCAT.COLIDENTATTRIBUTES
                WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=?
                """,
                (schema_name, table_name, colname),
            )
            a = cur.fetchone()
            attr = {}
            if a:
                attr = {
                    "increment": a[0], "minvalue": a[1], "maxvalue": a[2],
                    "cycle": str(a[3]).strip() if a[3] is not None else "N",
                    "cache": a[4], "order": str(a[5]).strip() if a[5] is not None else "N",
                    "nextcache": a[6],
                }
            cols.append({"column": colname, "type": typename, "generated": generated, "attributes": attr})
    return cols
