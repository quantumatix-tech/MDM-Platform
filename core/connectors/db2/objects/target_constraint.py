from __future__ import annotations

from typing import Any

from core.connectors.base import UniqueConstraint, validate_identifier
from core.connectors.db2.objects.ddl import _quote_ident


def _existing_constraint_def(conn, schema_name, table_name, constraint_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM SYSCAT.TABCONST WHERE TABSCHEMA=? AND TABNAME=? AND CONSTNAME=? AND TYPE='U'",
            (schema_name, table_name, constraint_name),
        )
        if cur.fetchone() is None:
            return None
        cur.execute(
            """
            SELECT COLNAME
            FROM SYSCAT.KEYCOLUSE
            WHERE TABSCHEMA=? AND TABNAME=? AND CONSTNAME=?
            ORDER BY COLSEQ
            """,
            (schema_name, table_name, constraint_name),
        )
        return [str(r[0]).strip() for r in cur.fetchall()]


def create_unique_constraint_if_missing(conn: Any, schema_name: str, table_name: str, constraint: UniqueConstraint) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(table_name, "table")
    validate_identifier(constraint.name, "constraint")
    for c in constraint.columns:
        validate_identifier(c, "column")
    existing = _existing_constraint_def(conn, schema_name, table_name, constraint.name)
    if existing is not None:
        if existing != list(constraint.columns):
            raise RuntimeError(f"Conflicting unique constraint definition for {schema_name}.{constraint.name}")
        return "skipped-existing"
    cols = ", ".join(_quote_ident(c) for c in constraint.columns)
    sql = f"ALTER TABLE {_quote_ident(schema_name)}.{_quote_ident(table_name)} ADD CONSTRAINT {_quote_ident(constraint.name)} UNIQUE ({cols})"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"
