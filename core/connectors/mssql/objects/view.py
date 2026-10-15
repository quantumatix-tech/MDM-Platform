"""MSSQL View object implementation.

Reusable View-specific logic for discovery and creation. Functions accept
an explicit *conn* parameter so that neither ``MSSQLSourceConnector`` nor
``MSSQLTargetConnector`` is imported, avoiding circular dependencies.
"""
from __future__ import annotations

import re
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    Index,
    ViewDefinition,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import _resolve_mssql_schemas

# ============================================================================
# SOURCE-SIDE VIEW OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_views.
# ============================================================================

def discover_views(conn: Any, config: dict[str, Any]) -> list[ViewDefinition]:
    """Discover user views in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_views``.

    Reads ``INFORMATION_SCHEMA.VIEWS`` (excludes system schemas). The
    ``VIEW_DEFINITION`` column is ``NVARCHAR(MAX)`` and pyodbc can choke on
    it when fetched together with other columns, so it is CAST explicitly.
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[ViewDefinition] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT TABLE_NAME, TABLE_SCHEMA, "
                "CAST(VIEW_DEFINITION AS NVARCHAR(MAX)) AS VIEW_DEFINITION, "
                "OBJECTPROPERTY(OBJECT_ID(QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME)), 'IsSchemaBound') "
                "FROM INFORMATION_SCHEMA.VIEWS "
                f"WHERE TABLE_SCHEMA IN ({placeholders}) "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_SCHEMA, "
                "CAST(VIEW_DEFINITION AS NVARCHAR(MAX)) AS VIEW_DEFINITION, "
                "OBJECTPROPERTY(OBJECT_ID(QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME)), 'IsSchemaBound') "
                "FROM INFORMATION_SCHEMA.VIEWS "
                "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
        for row in cur.fetchall():
            view_name, view_schema, view_def, is_schema_bound = row
            validate_identifier(view_name, "view")
            validate_identifier(view_schema, "schema")
            results.append(
                ViewDefinition(
                    name=view_name,
                    schema_name=view_schema,
                    definition=view_def,
                    is_schema_bound=bool(is_schema_bound),
                )
            )
    # Indexed-view metadata is captured separately because INFORMATION_SCHEMA
    # exposes neither index type nor its key definition.
    for view in results:
        if not view.is_schema_bound:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "SELECT i.name, i.is_unique, i.type, ic.key_ordinal, "
                "ic.is_descending_key, c.name "
                "FROM sys.views v "
                "JOIN sys.schemas s ON s.schema_id = v.schema_id "
                "JOIN sys.indexes i ON i.object_id = v.object_id "
                "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                "WHERE s.name = ? AND v.name = ? AND i.index_id > 0 "
                "ORDER BY i.index_id, ic.key_ordinal",
                (view.schema_name, view.name),
            )
            grouped: dict[str, dict[str, Any]] = {}
            for name, unique, index_type, _ordinal, descending, column in cur.fetchall():
                item = grouped.setdefault(name, {
                    "unique": bool(unique), "clustered": int(index_type) == 1,
                    "columns": [], "descending": [],
                })
                item["columns"].append(column)
                if descending:
                    item["descending"].append(column)
            for name, item in grouped.items():
                clustered = item["clustered"]
                if clustered and not item["unique"]:
                    raise RuntimeError(
                        f"SQL Server indexed view {view.schema_name}.{view.name} has a non-unique clustered index {name}; "
                        "source metadata is invalid for indexed-view recreation."
                    )
                if clustered and any(existing.index_type == "CLUSTERED" for existing in view.indexes):
                    raise RuntimeError(
                        f"SQL Server indexed view {view.schema_name}.{view.name} has multiple clustered indexes."
                    )
                ddl = (
                    f"CREATE {'UNIQUE ' if item['unique'] else ''}"
                    f"{'CLUSTERED' if clustered else 'NONCLUSTERED'} INDEX "
                    f"{quote_identifier(name)} ON {quote_identifier(view.schema_name)}."
                    f"{quote_identifier(view.name)} ({', '.join(quote_identifier(c) + (' DESC' if c in item['descending'] else ' ASC') for c in item['columns'])})"
                )
                view.indexes.append(Index(
                    name=name, columns=item["columns"], unique=item["unique"],
                    ddl=ddl, index_type="CLUSTERED" if clustered else "NONCLUSTERED",
                    descending_columns=item["descending"],
                ))
            if view.indexes and not any(index.index_type == "CLUSTERED" for index in view.indexes):
                raise RuntimeError(
                    f"SQL Server indexed view {view.schema_name}.{view.name} has nonclustered indexes "
                    "without a unique clustered index."
                )
    return results


# ============================================================================
# TARGET-SIDE VIEW OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_view.
# ============================================================================

def _procedure_replacement_blockers(
    cur: Any, object_id: int, schema_name: str, object_name: str,
) -> list[str]:
    """Return catalog-known consumers that make a procedure-to-view replacement unsafe."""
    blockers: list[str] = []
    cur.execute(
        "SELECT OBJECT_SCHEMA_NAME(d.referencing_id), OBJECT_NAME(d.referencing_id), "
        "COALESCE(o.type_desc, 'DATABASE_OBJECT'), d.is_schema_bound_reference "
        "FROM sys.sql_expression_dependencies d "
        "LEFT JOIN sys.objects o ON o.object_id=d.referencing_id "
        "WHERE d.referenced_id=? AND d.referencing_id<>? ORDER BY 1,2",
        (object_id, object_id),
    )
    for row in cur.fetchall():
        blockers.append(
            f"SQL dependency {row[0]}.{row[1]} ({row[2]}, schema_bound={bool(row[3])})"
        )

    qualified_names = (object_name, f"{schema_name}.{object_name}", f"[{schema_name}].[{object_name}]")
    cur.execute(
        "SELECT OBJECT_SCHEMA_NAME(m.object_id), OBJECT_NAME(m.object_id), o.type_desc "
        "FROM sys.sql_modules m JOIN sys.objects o ON o.object_id=m.object_id "
        "WHERE m.object_id<>? AND (CHARINDEX(?,m.definition)>0 OR CHARINDEX(?,m.definition)>0 "
        "OR CHARINDEX(?,m.definition)>0) ORDER BY 1,2",
        (object_id, *qualified_names),
    )
    for row in cur.fetchall():
        blockers.append(f"SQL text/dynamic reference {row[0]}.{row[1]} ({row[2]})")

    cur.execute(
        "SELECT s.name, sy.name FROM sys.synonyms sy JOIN sys.schemas s ON s.schema_id=sy.schema_id "
        "WHERE CHARINDEX(?,sy.base_object_name)>0 OR CHARINDEX(?,sy.base_object_name)>0 "
        "OR CHARINDEX(?,sy.base_object_name)>0 ORDER BY 1,2",
        qualified_names,
    )
    for row in cur.fetchall():
        blockers.append(f"synonym {row[0]}.{row[1]}")

    cur.execute(
        "SELECT grantee.name, p.state_desc, p.permission_name "
        "FROM sys.database_permissions p "
        "JOIN sys.database_principals grantee ON grantee.principal_id=p.grantee_principal_id "
        "WHERE p.class=1 AND p.major_id=? ORDER BY grantee.name,p.permission_name,p.state_desc",
        (object_id,),
    )
    for row in cur.fetchall():
        blockers.append(
            f"explicit target permission {row[0]} {row[1]} {row[2]} would be lost with the procedure"
        )

    cur.execute("SELECT 1 FROM sys.crypt_properties WHERE major_id=?", (object_id,))
    if cur.fetchone() is not None:
        blockers.append("the target procedure has a module signature")

    try:
        cur.execute(
            "SELECT j.name, st.step_name FROM msdb.dbo.sysjobsteps st "
            "JOIN msdb.dbo.sysjobs j ON j.job_id=st.job_id "
            "WHERE CHARINDEX(?,st.command)>0 OR CHARINDEX(?,st.command)>0 "
            "OR CHARINDEX(?,st.command)>0 ORDER BY 1,2",
            qualified_names,
        )
        for row in cur.fetchall():
            blockers.append(f"SQL Agent job step {row[0]}.{row[1]}")
    except Exception as exc:
        blockers.append(f"SQL Agent references could not be inspected ({exc})")
    return blockers

def create_view(conn: Any, view: ViewDefinition) -> None:
    """Create or alter a view on the target database.

    Replaces ``MSSQLTargetConnector.create_view``.

    Ensures the target schema exists (dbo is always present), then executes
    ``CREATE OR ALTER VIEW`` in a separate cursor/batch after any schema
    creation commit (CREATE VIEW must be the first statement in its batch).

    The view definition is normalised: if it starts with
    ``CREATE VIEW <name> AS``, the prefix is stripped to avoid a duplicate
    view qualification (e.g.
    ``CREATE OR ALTER VIEW [s].[v] AS s.v AS SELECT ...`` triggers SQL
    Server error 102).
    """
    validate_identifier(view.name, "view")
    schema_name = view.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    view_qname = f"[{schema_name or 'dbo'}].[{view.name}]"

    replaced_procedure: dict[str, Any] | None = None
    with conn.cursor() as cur:
        # Ensure the target schema exists (dbo always exists in SQL Server).
        if schema_name != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": schema_name},
                )
                conn.commit()

        # CREATE OR ALTER only replaces a view. Fail before executing DDL if
        # the same two-part name is occupied by a different object type.
        cur.execute(
            "SELECT o.object_id, o.type, o.type_desc FROM sys.objects o "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = ? AND o.name = ?",
            (schema_name, view.name),
        )
        existing = cur.fetchone()
        if existing is not None and str(existing[2]).upper() != "VIEW":
            target_type = str(existing[2])
            # sys.objects.type is a CHAR(2) code and pyodbc can return its
            # trailing padding (e.g. "P "). Normalize before classifying it.
            if str(existing[1]).strip().upper() not in {"P", "PC"}:
                raise RuntimeError(
                    f"Cannot reconcile source VIEW {schema_name}.{view.name}: target contains "
                    f"{target_type}; automatic replacement is supported only for a SQL procedure."
                )
            blockers = _procedure_replacement_blockers(
                cur, int(existing[0]), schema_name, view.name
            )
            if blockers:
                reason = "; ".join(blockers)
                audit_log(
                    phase="reconcile_object_type_conflict", status="rejected",
                    details={"object": f"{schema_name}.{view.name}", "source_type": "VIEW",
                             "target_type": target_type, "blockers": blockers},
                )
                raise RuntimeError(
                    f"Cannot replace target {target_type} {schema_name}.{view.name} with source VIEW; "
                    f"dependency review found: {reason}"
                )
            cur.execute(f"DROP PROCEDURE {view_qname}")
            replaced_procedure = {
                "object": f"{schema_name}.{view.name}",
                "source_type": "VIEW", "target_type": target_type,
                "dependency_blockers": [],
                "known_external_or_dynamic_references": [],
            }

    # CREATE VIEW must be the first statement in a batch.
    # Execute in a separate cursor/batch after schema creation commit.
    with conn.cursor() as cur:
        try:
            definition = view.definition
            # Strip leading whitespace and comment lines so the normal
            # ``CREATE VIEW`` prefix detection works even when SQL Server
            # embeds leading comments in the definition text.
            definition = definition.lstrip()
            while definition.startswith("--"):
                newline_pos = definition.find("\n")
                if newline_pos == -1:
                    definition = ""
                    break
                definition = definition[newline_pos + 1:].lstrip()
            if definition.upper().startswith("CREATE VIEW"):
                definition = definition[len("CREATE VIEW"):].lstrip()
                # SQL Server returns the definition with its original line
                # breaks (``name\r\nAS\r\nSELECT``), so the separator cannot be
                # matched as a literal " AS ".  Require surrounding whitespace to
                # avoid matching inside an identifier such as ``vw_OrderASAP``.
                as_match = re.search(r"\s+AS\s+", definition, re.IGNORECASE)
                if as_match:
                    definition = definition[as_match.end():].lstrip()
            schema_binding = " WITH SCHEMABINDING" if view.is_schema_bound else ""
            cur.execute(f"CREATE OR ALTER VIEW {view_qname}{schema_binding} AS {definition}")
            conn.commit()
            audit_log(
                phase="create_view",
                status="created",
                details={"view": view.name},
            )
            if replaced_procedure is not None:
                audit_log(
                    phase="reconcile_object_type_conflict", status="replaced",
                    details={**replaced_procedure,
                             "action": "dropped dependency-free procedure and created source view"},
                )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_view",
                status="failed",
                details={"view": view.name, "reason": str(exc)},
            )
            raise

    # SQL Server requires the view to be schema-bound before creating its
    # unique clustered index. Subsequent nonclustered indexes follow it.
    for index in sorted(view.indexes, key=lambda item: 0 if item.index_type == "CLUSTERED" else 1):
        if index.index_type not in {"CLUSTERED", "NONCLUSTERED"} or not index.ddl:
            raise RuntimeError(
                f"Unsupported indexed-view index metadata for {schema_name}.{view.name} index {index.name}"
            )
        with conn.cursor() as cur:
            try:
                cur.execute(
                "SELECT i.type, i.is_unique, c.name, ic.is_descending_key "
                "FROM sys.indexes i "
                "LEFT JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                "LEFT JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                    "WHERE i.object_id = OBJECT_ID(?) AND i.name = ?",
                    (view_qname, index.name),
                )
                existing_rows = cur.fetchall()
                existing = existing_rows[0] if existing_rows else None
                expected_type = 1 if index.index_type == "CLUSTERED" else 2
                if existing is not None:
                    actual_columns = [row[2] for row in existing_rows if row[2] is not None]
                    actual_descending = [row[2] for row in existing_rows if row[2] is not None and bool(row[3])]
                    actual_keys_match = (
                        actual_columns == index.columns
                        and actual_descending == index.descending_columns
                    )
                    if int(existing[0]) != expected_type or bool(existing[1]) != index.unique or not actual_keys_match:
                        if index.index_type == "CLUSTERED":
                            raise RuntimeError(
                                f"Existing clustered index {index.name} on indexed view {schema_name}.{view.name} "
                                "has incompatible keys or uniqueness; it cannot be safely replaced automatically."
                            )
                        cur.execute(f"DROP INDEX {quote_identifier(index.name)} ON {view_qname}")
                        existing = None
                if existing is None:
                    cur.execute(index.ddl)
                conn.commit()
                audit_log(
                    phase="create_view_index", status="created" if existing is None else "exists",
                    details={"view": f"{schema_name}.{view.name}", "index": index.name,
                             "index_type": index.index_type, "unique": index.unique},
                )
            except Exception as exc:
                conn.rollback()
                audit_log(
                    phase="create_view_index", status="failed",
                    details={"view": f"{schema_name}.{view.name}", "index": index.name,
                             "reason": str(exc)},
                )
                raise
