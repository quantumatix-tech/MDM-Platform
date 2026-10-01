"""MSSQL Comment / Extended Properties object implementation.

Reusable Comment-specific logic for source discovery and target application.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    CommentDef,
    validate_identifier,
)
from core.connectors.mssql._models import _resolve_mssql_schemas
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE COMMENT OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_comments.
# Discovery of extended properties (comments) from the source database.
# ============================================================================

def discover_comments(conn: Any, config: dict[str, Any]) -> list[CommentDef]:
    """Return extended properties (comments) for tables, columns, views, functions, schemas.

    Replaces ``MSSQLSourceConnector.list_comments``.

    Queries sys.extended_properties where name = 'MS_Description'.
    Supports: TABLE, VIEW, FUNCTION, PROCEDURE, SCHEMA, COLUMN.
    """
    schemas = _resolve_mssql_schemas(config)
    comments: list[CommentDef] = []

    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            schema_filter = f"AND s.name IN ({placeholders})"
            params = list(schemas)
        else:
            schema_filter = "AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')"
            params = []

        # Table, View, Function, Procedure comments (class=1, minor_id=0)
        cur.execute(
            f"SELECT ep.value, s.name AS schema_name, o.name AS object_name, "
            f"o.type_desc, "
            f"CASE o.type "
            f"  WHEN 'U' THEN 'TABLE' "
            f"  WHEN 'V' THEN 'VIEW' "
            f"  WHEN 'FN' THEN 'FUNCTION' "
            f"  WHEN 'TF' THEN 'FUNCTION' "
            f"  WHEN 'IF' THEN 'FUNCTION' "
            f"  WHEN 'P' THEN 'PROCEDURE' "
            f"  ELSE 'OBJECT' END AS obj_type "
            f"FROM sys.extended_properties ep "
            f"JOIN sys.objects o ON ep.major_id = o.object_id "
            f"JOIN sys.schemas s ON o.schema_id = s.schema_id "
            f"WHERE ep.class = 1 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
            f"{schema_filter} "
            f"ORDER BY s.name, o.name",
            params,
        )
        for row in cur.fetchall():
            value, schema_name, object_name, type_desc, obj_type = row
            comments.append(CommentDef(
                object_type=obj_type,
                object_name=object_name,
                comment=value,
                schema_name=schema_name,
            ))

        # Column comments (class=1, minor_id=column_id)
        cur.execute(
            f"SELECT ep.value, s.name AS schema_name, o.name AS table_name, c.name AS column_name "
            f"FROM sys.extended_properties ep "
            f"JOIN sys.objects o ON ep.major_id = o.object_id "
            f"JOIN sys.schemas s ON o.schema_id = s.schema_id "
            f"JOIN sys.columns c ON c.object_id = o.object_id AND c.column_id = ep.minor_id "
            f"WHERE ep.class = 1 AND ep.minor_id > 0 AND ep.name = 'MS_Description' "
            f"{schema_filter} "
            f"ORDER BY s.name, o.name, c.column_id",
            params,
        )
        for row in cur.fetchall():
            value, schema_name, table_name, column_name = row
            comments.append(CommentDef(
                object_type="COLUMN",
                object_name=f"{table_name}.{column_name}",
                comment=value,
                schema_name=schema_name,
            ))

        # Schema comments (class=3, major_id=schema_id, minor_id=0)
        cur.execute(
            f"SELECT ep.value, s.name AS schema_name "
            f"FROM sys.extended_properties ep "
            f"JOIN sys.schemas s ON ep.major_id = s.schema_id "
            f"WHERE ep.class = 3 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
            f"{schema_filter} "
            f"ORDER BY s.name",
            params,
        )
        for row in cur.fetchall():
            value, schema_name = row
            comments.append(CommentDef(
                object_type="SCHEMA",
                object_name=schema_name,
                comment=value,
                schema_name=schema_name,
            ))

    return comments


# ============================================================================
# TARGET-SIDE COMMENT OPERATIONS
# Called/delegated by MSSQLTargetConnector.apply_comment.
# Extended property application on the target database.
# ============================================================================

def apply_comment(conn: Any, comment: CommentDef) -> None:
    """Apply an extended property (comment) using sp_addextendedproperty / sp_updateextendedproperty.

    Replaces ``MSSQLTargetConnector.apply_comment``.

    Idempotent: adds if missing, updates if already present.
    """
    if not comment.comment:
        audit_log(
            phase="apply_comment", status="skipped",
            details={"object": comment.object_name, "schema": comment.schema_name or "dbo",
                     "reason": "null or empty comment"},
        )
        return

    escaped = comment.comment.replace("'", "''")
    schema_name = comment.schema_name or "dbo"

    with conn.cursor() as cur:
        try:
            # Check if extended property already exists
            if comment.object_type == "SCHEMA":
                cur.execute(
                    "SELECT 1 FROM sys.extended_properties ep "
                    "JOIN sys.schemas s ON ep.major_id = s.schema_id "
                    "WHERE ep.class = 3 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                    "AND s.name = ?",
                    (schema_name,),
                )
                exists = cur.fetchone() is not None
                level0_type, level0_name = "SCHEMA", schema_name
                level1_type = level1_name = level2_type = level2_name = None

            elif comment.object_type == "COLUMN":
                parts = comment.object_name.split(".")
                table_name, column_name = parts[0], parts[1]
                cur.execute(
                    "SELECT 1 FROM sys.extended_properties ep "
                    "JOIN sys.objects o ON ep.major_id = o.object_id "
                    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                    "JOIN sys.columns c ON c.object_id = o.object_id AND c.column_id = ep.minor_id "
                    "WHERE ep.class = 1 AND ep.minor_id > 0 AND ep.name = 'MS_Description' "
                    "AND s.name = ? AND o.name = ? AND c.name = ?",
                    (schema_name, table_name, column_name),
                )
                exists = cur.fetchone() is not None
                level0_type, level0_name = "SCHEMA", schema_name
                level1_type, level1_name = "TABLE", table_name
                level2_type, level2_name = "COLUMN", column_name

            else:
                # TABLE, VIEW, FUNCTION, PROCEDURE
                object_name = comment.object_name
                cur.execute(
                    "SELECT 1 FROM sys.extended_properties ep "
                    "JOIN sys.objects o ON ep.major_id = o.object_id "
                    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                    "WHERE ep.class = 1 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                    "AND s.name = ? AND o.name = ?",
                    (schema_name, object_name),
                )
                exists = cur.fetchone() is not None
                level0_type, level0_name = "SCHEMA", schema_name
                level1_type, level1_name = comment.object_type.upper(), object_name
                level2_type = level2_name = None

            if exists:
                # UPDATE existing extended property
                if comment.object_type == "SCHEMA":
                    cur.execute(
                        "EXEC sys.sp_updateextendedproperty "
                        "@name = 'MS_Description', @value = ?, "
                        "@level0type = ?, @level0name = ?",
                        (escaped, level0_type, level0_name),
                    )
                elif comment.object_type == "COLUMN":
                    cur.execute(
                        "EXEC sys.sp_updateextendedproperty "
                        "@name = 'MS_Description', @value = ?, "
                        "@level0type = ?, @level0name = ?, "
                        "@level1type = ?, @level1name = ?, "
                        "@level2type = ?, @level2name = ?",
                        (escaped, level0_type, level0_name, level1_type, level1_name, level2_type, level2_name),
                    )
                else:
                    cur.execute(
                        "EXEC sys.sp_updateextendedproperty "
                        "@name = 'MS_Description', @value = ?, "
                        "@level0type = ?, @level0name = ?, "
                        "@level1type = ?, @level1name = ?",
                        (escaped, level0_type, level0_name, level1_type, level1_name),
                    )
                action = "updated"
            else:
                # ADD new extended property
                if comment.object_type == "SCHEMA":
                    cur.execute(
                        "EXEC sys.sp_addextendedproperty "
                        "@name = 'MS_Description', @value = ?, "
                        "@level0type = ?, @level0name = ?",
                        (escaped, level0_type, level0_name),
                    )
                elif comment.object_type == "COLUMN":
                    cur.execute(
                        "EXEC sys.sp_addextendedproperty "
                        "@name = 'MS_Description', @value = ?, "
                        "@level0type = ?, @level0name = ?, "
                        "@level1type = ?, @level1name = ?, "
                        "@level2type = ?, @level2name = ?",
                        (escaped, level0_type, level0_name, level1_type, level1_name, level2_type, level2_name),
                    )
                else:
                    cur.execute(
                        "EXEC sys.sp_addextendedproperty "
                        "@name = 'MS_Description', @value = ?, "
                        "@level0type = ?, @level0name = ?, "
                        "@level1type = ?, @level1name = ?",
                        (escaped, level0_type, level0_name, level1_type, level1_name),
                    )
                action = "added"

            conn.commit()
            audit_log(
                phase="apply_comment", status="applied",
                details={"object": comment.object_name, "schema": schema_name, "action": action},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="apply_comment", status="failed",
                details={"object": comment.object_name, "schema": schema_name, "reason": str(exc)},
            )
            raise