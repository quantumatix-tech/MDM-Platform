from __future__ import annotations

from typing import Any

from core.connectors.base import UniqueConstraint, validate_identifier


def discover_unique_constraints(conn: Any, schema_name: str, table_name: str) -> list[UniqueConstraint]:
    validate_identifier(table_name, "table")
    validate_identifier(schema_name, "schema")
    constraints: list[UniqueConstraint] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT CONSTNAME
            FROM SYSCAT.TABCONST
            WHERE TABSCHEMA = ? AND TABNAME = ? AND TYPE = 'U'
            ORDER BY CONSTNAME
            """,
            (schema_name, table_name),
        )
        names = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
        for cname in names:
            cur.execute(
                """
                SELECT COLNAME
                FROM SYSCAT.KEYCOLUSE
                WHERE TABSCHEMA = ? AND TABNAME = ? AND CONSTNAME = ?
                ORDER BY COLSEQ
                """,
                (schema_name, table_name, cname),
            )
            cols = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
            constraints.append(UniqueConstraint(name=cname, columns=cols))
    return constraints
