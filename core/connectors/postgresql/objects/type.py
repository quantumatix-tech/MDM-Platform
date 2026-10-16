"""PostgreSQL custom type implementation.

Covers the three user-defined type kinds PostgreSQL exposes as ``pg_type``
entries: ENUM, DOMAIN and COMPOSITE. All three are discovered together and
each produces a complete, ready-to-execute DDL string that the target replays
verbatim — the type body is synthesised on the source rather than rebuilt on
the target.
"""
from __future__ import annotations

import uuid
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import TypeDef, quote_identifier


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

# ============================================================================
# SOURCE-SIDE TYPE OPERATIONS
# ============================================================================

def discover_types(conn: Any, schemas: tuple[str, ...]) -> list[TypeDef]:
    """Return all ENUM, DOMAIN and COMPOSITE types in the configured schemas.

    Each kind is produced by its own catalog query, because their metadata
    lives in different places: ENUM labels in ``pg_enum``, DOMAIN constraints
    in ``pg_constraint.contypid``, and COMPOSITE attributes in the backing
    relation's ``pg_attribute`` rows.
    """
    types: list[TypeDef] = []
    with conn.cursor() as cur:
        # ENUMs
        cur.execute(
            "SELECT n.nspname, t.typname, "
            "  array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels "
            "FROM pg_type t "
            "JOIN pg_enum e ON t.oid = e.enumtypid "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE n.nspname = ANY(%s) "
            "GROUP BY n.nspname, t.typname ORDER BY n.nspname, t.typname",
            (list(schemas),),
        )
        for row in cur.fetchall():
            schema, type_name, labels = row
            labels_sql = ", ".join(_quote_literal(lbl) for lbl in labels)
            qualified = f"{quote_identifier(schema)}.{quote_identifier(type_name)}"
            ddl = f"CREATE TYPE {qualified} AS ENUM ({labels_sql})"
            types.append(TypeDef(
                name=type_name, kind="enum", ddl=ddl, schema=schema,
                enum_labels=list(labels),
            ))

        # DOMAINs
        cur.execute(
            "SELECT n.nspname, t.typname, "
            "  pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base, "
            "  pg_catalog.pg_get_expr(t.typdefaultbin, 0) AS dflt, "
            "  t.typnotnull, "
            "  (SELECT string_agg('CONSTRAINT ' || c.conname || ' CHECK (' || "
            "    pg_get_constraintdef(c.oid) || ')', ' ') "
            "   FROM pg_constraint c WHERE c.contypid = t.oid) AS checks "
            "FROM pg_type t "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE t.typtype = 'd' AND n.nspname = ANY(%s) "
            "ORDER BY n.nspname, t.typname",
            (list(schemas),),
        )
        for row in cur.fetchall():
            schema, type_name, base, dflt, notnull, checks = row
            qualified = f"{quote_identifier(schema)}.{quote_identifier(type_name)}"
            ddl = f"CREATE DOMAIN {qualified} AS {base}"
            if dflt:
                ddl += f" DEFAULT {dflt}"
            if notnull:
                ddl += " NOT NULL"
            if checks:
                ddl += f" {checks}"
            types.append(TypeDef(name=type_name, kind="domain", ddl=ddl, schema=schema))

        # COMPOSITE types
        cur.execute(
            "SELECT n.nspname, t.typname, "
            "  string_agg(a.attname || ' ' || pg_catalog.format_type(a.atttypid, a.atttypmod), "
            "    ', ' ORDER BY a.attnum) AS cols "
            "FROM pg_type t "
            "JOIN pg_class c ON c.oid = t.typrelid "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE t.typtype = 'c' AND n.nspname = ANY(%s) "
            "  AND c.relkind = 'c' "
            "GROUP BY n.nspname, t.typname ORDER BY n.nspname, t.typname",
            (list(schemas),),
        )
        for row in cur.fetchall():
            schema, type_name, cols = row
            qualified = f"{quote_identifier(schema)}.{quote_identifier(type_name)}"
            ddl = f"CREATE TYPE {qualified} AS ({cols})"
            types.append(TypeDef(name=type_name, kind="composite", ddl=ddl, schema=schema))

    return types


# ============================================================================
# TARGET-SIDE TYPE OPERATIONS
# ============================================================================

def create_type(conn: Any, type_def: TypeDef) -> None:
    """Create a user-defined type on the target, skipping if already present.

    Checks for existence in the type's own schema (``type_def.schema``) and
    generates schema-qualified DDL, so types defined in non-``public`` schemas
    (e.g. ``training``) are created in the correct location.
    """
    schema = type_def.schema or "public"
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT 1 FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE t.typname = %s AND n.nspname = %s",
                (type_def.name, schema),
            )
            if cur.fetchone() is not None:
                return
            cur.execute(type_def.ddl)
            conn.commit()
            audit_log(phase="create_type", status="created",
                      details={"type": type_def.name, "kind": type_def.kind, "schema": schema})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_type", status="skipped",
                      details={"type": type_def.name, "reason": str(exc)})


def reconcile_enum_types(
    conn: Any,
    type_defs: list[TypeDef],
    managed_schemas: list[Any],
    managed_views: list[Any] | None = None,
) -> list[str]:
    """Replace changed source-managed ENUM definitions without CASCADE.

    PostgreSQL cannot remove an ENUM label with ALTER TYPE. The old type is
    therefore renamed, the source definition is created under its original
    name, and only source-managed table columns are converted. The final
    restrictive DROP TYPE is an additional guard against untracked
    dependencies (including target-only routines and expressions).
    """
    schema_by_table = {
        (schema.schema_name or "public", schema.name): schema
        for schema in managed_schemas
    }
    changed: list[str] = []
    try:
        with conn.cursor() as cur:
            for type_def in type_defs:
                if type_def.kind != "enum" or type_def.enum_labels is None:
                    continue
                schema = type_def.schema or "public"
                cur.execute(
                    "SELECT e.enumlabel FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "JOIN pg_enum e ON e.enumtypid = t.oid "
                    "WHERE n.nspname = %s AND t.typname = %s "
                    "ORDER BY e.enumsortorder",
                    (schema, type_def.name),
                )
                current_labels = [row[0] for row in cur.fetchall()]
                if not current_labels or current_labels == type_def.enum_labels:
                    continue

                cur.execute(
                    "SELECT n.nspname, c.relname, a.attname, "
                    "a.atttypid = t.typarray "
                    "FROM pg_type t "
                    "JOIN pg_namespace tn ON tn.oid = t.typnamespace "
                    "JOIN pg_attribute a ON a.atttypid IN (t.oid, t.typarray) "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE tn.nspname = %s AND t.typname = %s "
                    "AND a.attnum > 0 AND NOT a.attisdropped "
                    "AND a.attinhcount = 0 AND c.relkind IN ('r', 'p', 'f')",
                    (schema, type_def.name),
                )
                dependencies = list(cur.fetchall())
                managed_dependencies = []
                for table_schema, table_name, column_name, is_array in dependencies:
                    table = schema_by_table.get((table_schema, table_name))
                    if table is None or column_name not in {c.name for c in table.columns}:
                        raise RuntimeError(
                            "Cannot safely reconcile ENUM "
                            f"{schema}.{type_def.name}: dependent column "
                            f"{table_schema}.{table_name}.{column_name} is not source-managed."
                        )
                    managed_dependencies.append(
                        (table_schema, table_name, column_name, is_array, table)
                    )

                _drop_managed_views_before_enum_change(cur, managed_views or [])
                for table_schema, table_name, column_name, _is_array, _table in managed_dependencies:
                    cur.execute(
                        f"ALTER TABLE {quote_identifier(table_schema)}.{quote_identifier(table_name)} "
                        f"ALTER COLUMN {quote_identifier(column_name)} DROP DEFAULT"
                    )

                old_type = f"_codex_old_enum_{uuid.uuid4().hex[:12]}"
                qualified_old = f"{quote_identifier(schema)}.{quote_identifier(old_type)}"
                qualified_type = f"{quote_identifier(schema)}.{quote_identifier(type_def.name)}"
                cur.execute(
                    f"ALTER TYPE {qualified_type} RENAME TO {quote_identifier(old_type)}"
                )
                cur.execute(type_def.ddl)

                for table_schema, table_name, column_name, is_array, table in managed_dependencies:
                    table_qname = (
                        f"{quote_identifier(table_schema)}.{quote_identifier(table_name)}"
                    )
                    column_qname = quote_identifier(column_name)
                    target_type = f"{qualified_type}[]" if is_array else qualified_type
                    cast = f"{column_qname}::text[]::{target_type}" if is_array else f"{column_qname}::text::{target_type}"
                    cur.execute(
                        f"ALTER TABLE {table_qname} ALTER COLUMN {column_qname} "
                        f"TYPE {target_type} USING {cast}"
                    )
                    source_column = next(c for c in table.columns if c.name == column_name)
                    if source_column.default is not None:
                        cur.execute(
                            f"ALTER TABLE {table_qname} ALTER COLUMN {column_qname} "
                            f"SET DEFAULT {source_column.default}"
                        )

                # RESTRICT is PostgreSQL's default; keeping it explicit makes
                # the target-only dependency safety guarantee clear.
                cur.execute(f"DROP TYPE {qualified_old} RESTRICT")
                changed.append(f"{schema}.{type_def.name}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return changed


def _drop_managed_views_before_enum_change(cur: Any, views: list[Any]) -> None:
    """Drop source-managed views only, with restrictive dependency checks."""
    pending = list(views)
    while pending:
        remaining = []
        dropped_any = False
        for view in pending:
            view_schema = view.schema_name or "public"
            cur.execute("SAVEPOINT reconcile_managed_enum_view")
            try:
                cur.execute(
                    f"DROP VIEW IF EXISTS {quote_identifier(view_schema)}."
                    f"{quote_identifier(view.name)}"
                )
                cur.execute("RELEASE SAVEPOINT reconcile_managed_enum_view")
                dropped_any = True
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT reconcile_managed_enum_view")
                cur.execute("RELEASE SAVEPOINT reconcile_managed_enum_view")
                remaining.append(view)
        if remaining and not dropped_any:
            names = [f"{view.schema_name}.{view.name}" for view in remaining]
            raise RuntimeError(
                "Could not safely detach source-managed views before ENUM "
                f"reconciliation: {names}. Check target-only dependencies."
            )
        pending = remaining
