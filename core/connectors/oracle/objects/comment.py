"""Oracle table and column comment discovery."""

from __future__ import annotations

from typing import Any

from core.connectors.base import CommentDef


def discover_comments(conn: Any, owner: str) -> list[CommentDef]:
    """Discover comments only on ordinary owner tables and their columns."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.table_name, c.comments, o.object_type "
            "FROM user_tab_comments c JOIN user_objects o "
            "ON o.object_name = c.table_name AND o.object_type IN "
            "('TABLE','VIEW','MATERIALIZED VIEW') "
            "WHERE c.comments IS NOT NULL ORDER BY c.table_name"
        )
        table_rows = cur.fetchall()
        cur.execute(
            "SELECT c.table_name, c.column_name, c.comments, o.object_type "
            "FROM user_col_comments c JOIN user_objects o "
            "ON o.object_name = c.table_name AND o.object_type IN "
            "('TABLE','VIEW','MATERIALIZED VIEW') "
            "WHERE c.comments IS NOT NULL ORDER BY c.table_name, c.column_name"
        )
        column_rows = cur.fetchall()
    unsupported = sorted(
        {f"{kind} {table}" for table, _, kind in table_rows if str(kind).upper() != "TABLE"}
        | {
            f"{kind} {table}.{column}"
            for table, column, _, kind in column_rows
            if str(kind).upper() != "TABLE"
        }
    )
    if unsupported:
        raise RuntimeError(
            "Oracle FULL cannot preserve comments on unsupported target(s): "
            + ", ".join(unsupported)
        )
    return [
        CommentDef("TABLE", str(table), str(comment), owner) for table, comment, _ in table_rows
    ] + [
        CommentDef("COLUMN", str(table), str(comment), owner, column_name=str(column))
        for table, column, comment, _ in column_rows
    ]
