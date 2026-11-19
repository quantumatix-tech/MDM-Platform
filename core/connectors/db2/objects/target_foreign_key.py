from __future__ import annotations

from typing import Any

from core.connectors.base import ForeignKey, validate_identifier
from core.connectors.db2.objects.ddl import _quote_ident


def _existing_fk_def(conn, child_schema, child_table, fk_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT REFTABSCHEMA, REFTABNAME, DELETERULE, UPDATERULE, REFKEYNAME
            FROM SYSCAT.REFERENCES
            WHERE TABSCHEMA=? AND TABNAME=? AND CONSTNAME=?
            """,
            (child_schema, child_table, fk_name),
        )
        row = cur.fetchone()
        if not row:
            return None
        ref_schema = str(row[0]).strip()
        ref_table = str(row[1]).strip()
        delete_rule = str(row[2]).strip().upper()
        update_rule = str(row[3]).strip().upper()
        ref_keyname = str(row[4]).strip() if row[4] else None
        cur.execute(
            """
            SELECT COLNAME
            FROM SYSCAT.KEYCOLUSE
            WHERE TABSCHEMA=? AND TABNAME=? AND CONSTNAME=?
            ORDER BY COLSEQ
            """,
            (child_schema, child_table, fk_name),
        )
        child_cols = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
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
        action_map = {"A": "NO ACTION", "C": "CASCADE", "R": "RESTRICT", "N": "SET NULL"}
        return {
            "ref_schema": ref_schema,
            "ref_table": ref_table,
            "child_cols": child_cols,
            "ref_cols": ref_cols,
            "on_delete": action_map.get(delete_rule, delete_rule),
            "on_update": action_map.get(update_rule, update_rule),
        }


def create_foreign_key_if_missing(conn: Any, child_schema: str, child_table: str, foreign_key: ForeignKey) -> str:
    validate_identifier(child_schema, "schema")
    validate_identifier(child_table, "table")
    validate_identifier(foreign_key.name, "constraint")
    for c in foreign_key.columns + foreign_key.ref_columns:
        validate_identifier(c, "column")
    existing = _existing_fk_def(conn, child_schema, child_table, foreign_key.name)
    if existing is not None:
        if (existing["ref_schema"] != foreign_key.ref_schema or
            existing["ref_table"] != foreign_key.ref_table or
            existing["child_cols"] != list(foreign_key.columns) or
            existing["ref_cols"] != list(foreign_key.ref_columns) or
            existing["on_delete"] != foreign_key.on_delete or
            existing["on_update"] != foreign_key.on_update):
            raise RuntimeError(f"Conflicting foreign key definition for {child_schema}.{foreign_key.name}")
        return "skipped-existing"
    child_cols_sql = ", ".join(_quote_ident(c) for c in foreign_key.columns)
    ref_cols_sql = ", ".join(_quote_ident(c) for c in foreign_key.ref_columns)
    sql = (
        f"ALTER TABLE {_quote_ident(child_schema)}.{_quote_ident(child_table)} "
        f"ADD CONSTRAINT {_quote_ident(foreign_key.name)} "
        f"FOREIGN KEY ({child_cols_sql}) "
        f"REFERENCES {_quote_ident(foreign_key.ref_schema)}.{_quote_ident(foreign_key.ref_table)} ({ref_cols_sql}) "
        f"ON DELETE {foreign_key.on_delete} ON UPDATE {foreign_key.on_update}"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"
