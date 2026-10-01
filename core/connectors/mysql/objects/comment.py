"""MySQL comment object operations.

Owns table and column comment discovery and application. This module must
not import ``source``, ``target`` or ``cdc``: it depends only on
``_models``, the cross-engine DTOs and the standard library.

MySQL stores comments in the table/column DDL, so applying a comment to an
existing target object requires re-issuing the native ``ALTER TABLE``
statement while preserving the column's current definition.

Every SQL statement, parameter order, validation, error type, commit and
rollback boundary is carried over unchanged from the former
``MySQLSourceConnector.list_comments`` and
``MySQLTargetConnector.apply_comment`` implementations.
"""
from __future__ import annotations

import re
from typing import Any

from core.connectors.base import (
    CommentDef,
    validate_identifier,
)

from core.connectors.mysql._models import (
    _default_sql,
    _q,
    literal,
)


def list_comments(conn: Any, database: str) -> list[CommentDef]:
    """Discover table and column comments in ``database``.

    Table comments are returned first, then column comments, each in
    catalog order; views and empty comments are excluded.
    """
    db = database; result: list[CommentDef] = []
    with conn.cursor() as cur:
        cur.execute("SELECT TABLE_NAME,TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' AND TABLE_COMMENT<>''", (db,))
        result.extend(CommentDef("TABLE", n, c, db) for n, c in cur.fetchall())
        cur.execute("SELECT c.TABLE_NAME,c.COLUMN_NAME,c.COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME WHERE c.TABLE_SCHEMA=%s AND t.TABLE_TYPE='BASE TABLE' AND c.COLUMN_COMMENT<>''", (db,))
        result.extend(CommentDef("COLUMN", f"{t}.{n}", c, db) for t, n, c in cur.fetchall())
    return result


def apply_comment(conn: Any, database: str, comment: CommentDef) -> None:
    # MySQL stores comments in table/column DDL; retain source comments for
    # existing target objects by applying native ALTER statements.
    if comment.object_type not in {"TABLE", "COLUMN"}:
        raise ValueError(f"Unsupported MySQL comment object type: {comment.object_type}")

    try:
        table_name, column_name = (
            comment.object_name.rsplit(".", 1)
            if comment.object_type == "COLUMN"
            else (comment.object_name, None)
        )
    except ValueError as exc:
        raise ValueError(f"Invalid MySQL column comment name: {comment.object_name}") from exc
    validate_identifier(table_name, "table")
    if column_name is not None:
        validate_identifier(column_name, "column")

    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (database, table_name),
            )
            row = cur.fetchone()
            if row is None or row[0] != "BASE TABLE":
                raise RuntimeError(f"Target object is not a BASE TABLE: {table_name}")

            if comment.object_type == "TABLE":
                cur.execute(
                    f"ALTER TABLE {_q(table_name)} "
                    f"COMMENT = {literal(comment.comment)}"
                )
                conn.commit()
                return

            cur.execute(
                "SELECT COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,"
                "GENERATION_EXPRESSION,CHARACTER_SET_NAME,COLLATION_NAME "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (database, table_name, column_name),
            )
            metadata = cur.fetchone()
            if metadata is None:
                raise RuntimeError(f"Target column not found: {table_name}.{column_name}")
            (
                column_type,
                is_nullable,
                default,
                extra,
                generation_expression,
                character_set,
                collation,
            ) = metadata
            extra_text = extra or ""
            definition = str(column_type)
            if character_set:
                definition += f" CHARACTER SET {_q(str(character_set))}"
            if collation:
                definition += f" COLLATE {_q(str(collation))}"
            if generation_expression:
                generated_kind = "STORED" if "STORED" in extra_text.upper() else "VIRTUAL"
                definition += f" GENERATED ALWAYS AS ({generation_expression}) {generated_kind}"
                definition += " NULL" if is_nullable == "YES" else " NOT NULL"
            else:
                definition += " NULL" if is_nullable == "YES" else " NOT NULL"
                if default is not None:
                    definition += f" DEFAULT {_default_sql(default, str(column_type))}"
                if "AUTO_INCREMENT" in extra_text.upper():
                    definition += " AUTO_INCREMENT"
                on_update = re.search(r"\bon update\s+(.+)$", extra_text, re.IGNORECASE)
                if on_update:
                    definition += f" ON UPDATE {on_update.group(1)}"
            definition += f" COMMENT {literal(comment.comment)}"
            cur.execute(
                f"ALTER TABLE {_q(table_name)} MODIFY COLUMN {_q(column_name)} {definition}"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
