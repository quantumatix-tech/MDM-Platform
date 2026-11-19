from __future__ import annotations

from typing import Any

from core.connectors.base import ForeignKey, validate_identifier


def discover_foreign_keys(conn: Any, schema_name: str, table_name: str) -> list[ForeignKey]:
    validate_identifier(schema_name, "schema")
    validate_identifier(table_name, "table")
    fks: list[ForeignKey] = []
    action_map = {"A": "NO ACTION", "C": "CASCADE", "R": "RESTRICT", "N": "SET NULL"}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT CONSTNAME, REFTABSCHEMA, REFTABNAME, REFKEYNAME, DELETERULE, UPDATERULE, COLCOUNT
            FROM SYSCAT.REFERENCES
            WHERE TABSCHEMA=? AND TABNAME=?
            ORDER BY CONSTNAME
            """,
            (schema_name, table_name),
        )
        rows = cur.fetchall()
        for row in rows:
            if not row:
                continue
            constname = str(row[0]).strip()
            ref_schema = str(row[1]).strip()
            ref_table = str(row[2]).strip()
            ref_keyname = str(row[3]).strip() if row[3] else None
            delete_rule = str(row[4]).strip().upper() if row[4] else "A"
            update_rule = str(row[5]).strip().upper() if row[5] else "A"
            colcount = int(row[6]) if len(row) > 6 and row[6] is not None else None
            if delete_rule not in action_map or update_rule not in action_map:
                raise ValueError(f"Unsupported FK action code for {schema_name}.{table_name}.{constname}")
            cur.execute(
                """
                SELECT COLNAME
                FROM SYSCAT.KEYCOLUSE
                WHERE TABSCHEMA=? AND TABNAME=? AND CONSTNAME=?
                ORDER BY COLSEQ
                """,
                (schema_name, table_name, constname),
            )
            child_cols = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
            if colcount is not None and colcount != len(child_cols):
                raise ValueError(f"Child column count mismatch for {schema_name}.{table_name}.{constname}")
            ref_cols = []
            if ref_keyname:
                cur.execute(
                    """
                    SELECT COLNAME
                    FROM SYSCAT.KEYCOLUSE
                    WHERE TABSCHEMA=? AND TABNAME=? AND CONSTNAME=?
                    ORDER BY COLSEQ
                    """,
                    (ref_schema, ref_table, ref_keyname),
                )
                ref_cols = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
            if not ref_cols:
                raise ValueError(f"Could not resolve referenced key columns for {schema_name}.{table_name}.{constname}")
            if colcount is not None and colcount != len(ref_cols):
                raise ValueError(f"Referenced column count mismatch for {schema_name}.{table_name}.{constname}")
            fks.append(ForeignKey(
                name=constname,
                columns=child_cols,
                ref_table=ref_table,
                ref_columns=ref_cols,
                ref_schema=ref_schema,
                on_delete=action_map[delete_rule],
                on_update=action_map[update_rule],
            ))
    return fks
