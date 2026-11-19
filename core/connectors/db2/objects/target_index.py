from __future__ import annotations

from typing import Any

from core.connectors.base import Index, validate_identifier
from core.connectors.db2.objects.ddl import _quote_ident


def _existing_index_def(conn, schema_name, table_name, index_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT INDNAME, UNIQUERULE
            FROM SYSCAT.INDEXES
            WHERE TABSCHEMA=? AND TABNAME=? AND INDSCHEMA=? AND INDNAME=?
            """,
            (schema_name, table_name, schema_name, index_name),
        )
        row = cur.fetchone()
        if not row:
            return None
        unique = str(row[1]).strip().upper() == "U"
        cur.execute(
            """
            SELECT COLNAME, COLORDER
            FROM SYSCAT.INDEXCOLUSE
            WHERE INDSCHEMA=? AND INDNAME=?
            ORDER BY COLSEQ
            """,
            (schema_name, index_name),
        )
        cols = [(str(r[0]).strip(), str(r[1]).strip().upper() if r[1] else "A") for r in cur.fetchall()]
        return {"unique": unique, "columns": cols}


def create_index_if_missing(conn: Any, schema_name: str, table_name: str, index: Index) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(table_name, "table")
    validate_identifier(index.name, "index")
    desired_cols = [(c, "D" if c in index.descending_columns else "A") for c in index.columns]
    desired_unique = bool(index.unique)
    existing = _existing_index_def(conn, schema_name, table_name, index.name)
    if existing is not None:
        if existing["unique"] != desired_unique or existing["columns"] != desired_cols:
            raise RuntimeError(f"Conflicting index definition for {schema_name}.{index.name}")
        return "skipped-existing"
    cols = []
    for col in index.columns:
        validate_identifier(col, "column")
        cols.append(_quote_ident(col) + (" DESC" if col in index.descending_columns else " ASC"))
    unique = "UNIQUE " if index.unique else ""
    sql = f"CREATE {unique}INDEX {_quote_ident(schema_name)}.{_quote_ident(index.name)} ON {_quote_ident(schema_name)}.{_quote_ident(table_name)} ({', '.join(cols)})"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"
