"""PostgreSQL comment object implementation.

Comments are stored in the shared ``pg_description`` catalog and keyed by
(object oid, sub-object id), so a single object family covers every comment
PostgreSQL supports in this connector. Four families are discovered, each with
its own catalog query because they join through different relations:

  * table / view / materialized view comments  (``relkind`` mapped to a type)
  * column comments                           (keyed by ``attnum``)
  * function comments                         (keyed by the routine signature)
  * schema comments                           (keyed by namespace oid)
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    CommentDef,
    quote_identifier,
)


# ============================================================================
# SOURCE-SIDE COMMENT OPERATIONS
# ============================================================================

def discover_comments(conn: Any, schemas: tuple[str, ...]) -> list[CommentDef]:
    """Return every comment in the configured schemas.

    The relation-kind mapping collapses the table/view/materialized view
    distinction onto the three ``COMMENT ON`` object types.
    """
    comments: list[CommentDef] = []
    with conn.cursor() as cur:
        schema_list = list(schemas)
        # Table / view / matview comments
        cur.execute(
            "SELECT CASE c.relkind "
            "  WHEN 'r' THEN 'TABLE' WHEN 'v' THEN 'VIEW' "
            "  WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, "
            "  n.nspname, c.relname, d.description "
            "FROM pg_description d "
            "JOIN pg_class c ON d.objoid = c.oid "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "WHERE n.nspname = ANY(%s) AND d.objsubid = 0 "
            "  AND c.relkind IN ('r', 'v', 'm') "
            "ORDER BY c.relname",
            (schema_list,),
        )
        for row in cur.fetchall():
            obj_type, schema_name, obj_name, comment = row
            comments.append(CommentDef(object_type=obj_type, object_name=obj_name, comment=comment, schema_name=schema_name))

        # Column comments
        cur.execute(
            "SELECT n.nspname, c.relname, a.attname, d.description "
            "FROM pg_description d "
            "JOIN pg_attribute a ON d.objoid = a.attrelid AND d.objsubid = a.attnum "
            "JOIN pg_class c ON a.attrelid = c.oid "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "WHERE n.nspname = ANY(%s) AND a.attnum > 0 "
            "ORDER BY c.relname, a.attnum",
            (schema_list,),
        )
        for row in cur.fetchall():
            schema_name, table_name, col_name, comment = row
            comments.append(CommentDef(
                object_type="COLUMN",
                object_name=f"{table_name}.{col_name}",
                comment=comment,
                schema_name=schema_name,
            ))

        # Function comments
        cur.execute(
            "SELECT n.nspname, p.proname || '(' || "
            "  pg_get_function_arguments(p.oid) || ')', d.description "
            "FROM pg_description d "
            "JOIN pg_proc p ON d.objoid = p.oid "
            "JOIN pg_namespace n ON p.pronamespace = n.oid "
            "WHERE n.nspname = ANY(%s) "
            "ORDER BY p.proname",
            (schema_list,),
        )
        for row in cur.fetchall():
            schema_name, func_sig, comment = row
            comments.append(CommentDef(object_type="FUNCTION", object_name=func_sig, comment=comment, schema_name=schema_name))

        # Schema comments
        cur.execute(
            "SELECT n.nspname, d.description "
            "FROM pg_description d "
            "JOIN pg_namespace n ON d.objoid = n.oid "
            "WHERE d.classoid = 'pg_namespace'::regclass "
            "  AND n.nspname = ANY(%s) "
            "ORDER BY n.nspname",
            (schema_list,),
        )
        for row in cur.fetchall():
            schema_name, comment = row
            comments.append(CommentDef(object_type="SCHEMA", object_name=schema_name, comment=comment, schema_name=schema_name))

    return comments


# ============================================================================
# TARGET-SIDE COMMENT OPERATIONS
# ============================================================================

def apply_comment(conn: Any, comment: CommentDef) -> None:
    """Apply a single ``COMMENT ON`` statement on the target.

    The target object name is resolved per comment type, because PostgreSQL
    addresses schemas, columns and routine signatures differently. The comment
    text is embedded in the statement, so single quotes are escaped by
    doubling.
    """
    with conn.cursor() as cur:
        try:
            escaped = comment.comment.replace("'", "''")
            if comment.object_type == "SCHEMA":
                qualified_name = quote_identifier(comment.schema_name)
            elif comment.schema_name == "public":
                qualified_name = comment.object_name
            elif comment.object_type == "COLUMN":
                parts = comment.object_name.split(".")
                qualified_name = (
                    f"{quote_identifier(comment.schema_name)}.{quote_identifier(parts[0])}"
                    f".{quote_identifier(parts[1])}"
                )
            elif comment.object_type == "FUNCTION":
                qualified_name = f"{quote_identifier(comment.schema_name)}.{comment.object_name}"
            else:
                qualified_name = f"{quote_identifier(comment.schema_name)}.{quote_identifier(comment.object_name)}"
            cur.execute(
                f"COMMENT ON {comment.object_type} {qualified_name} IS '{escaped}'"
            )
            conn.commit()
            audit_log(phase="apply_comment", status="applied",
                      details={"object": qualified_name})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="apply_comment", status="skipped",
                      details={"object": comment.object_name, "reason": str(exc)})
