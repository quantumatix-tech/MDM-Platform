"""PostgreSQL custom type implementation.

Covers the three user-defined type kinds PostgreSQL exposes as ``pg_type``
entries: ENUM, DOMAIN and COMPOSITE. All three are discovered together and
each produces a complete, ready-to-execute DDL string that the target replays
verbatim — the type body is synthesised on the source rather than rebuilt on
the target.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import TypeDef


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
            "SELECT t.typname, "
            "  array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels "
            "FROM pg_type t "
            "JOIN pg_enum e ON t.oid = e.enumtypid "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE n.nspname = ANY(%s) "
            "GROUP BY t.typname ORDER BY t.typname",
            (list(schemas),),
        )
        for row in cur.fetchall():
            type_name, labels = row
            labels_sql = ", ".join(f"'{lbl}'" for lbl in labels)
            ddl = f"CREATE TYPE {type_name} AS ENUM ({labels_sql})"
            types.append(TypeDef(name=type_name, kind="enum", ddl=ddl))

        # DOMAINs
        cur.execute(
            "SELECT t.typname, "
            "  pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base, "
            "  pg_catalog.pg_get_expr(t.typdefaultbin, 0) AS dflt, "
            "  t.typnotnull, "
            "  (SELECT string_agg('CONSTRAINT ' || c.conname || ' CHECK (' || "
            "    pg_get_constraintdef(c.oid) || ')', ' ') "
            "   FROM pg_constraint c WHERE c.contypid = t.oid) AS checks "
            "FROM pg_type t "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE t.typtype = 'd' AND n.nspname = ANY(%s) "
            "ORDER BY t.typname",
            (list(schemas),),
        )
        for row in cur.fetchall():
            type_name, base, dflt, notnull, checks = row
            ddl = f"CREATE DOMAIN {type_name} AS {base}"
            if dflt:
                ddl += f" DEFAULT {dflt}"
            if notnull:
                ddl += " NOT NULL"
            if checks:
                ddl += f" {checks}"
            types.append(TypeDef(name=type_name, kind="domain", ddl=ddl))

        # COMPOSITE types
        cur.execute(
            "SELECT t.typname, "
            "  string_agg(a.attname || ' ' || pg_catalog.format_type(a.atttypid, a.atttypmod), "
            "    ', ' ORDER BY a.attnum) AS cols "
            "FROM pg_type t "
            "JOIN pg_class c ON c.oid = t.typrelid "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE t.typtype = 'c' AND n.nspname = ANY(%s) "
            "  AND c.relkind = 'c' "
            "GROUP BY t.typname ORDER BY t.typname",
            (list(schemas),),
        )
        for row in cur.fetchall():
            type_name, cols = row
            ddl = f"CREATE TYPE {type_name} AS ({cols})"
            types.append(TypeDef(name=type_name, kind="composite", ddl=ddl))

    return types


# ============================================================================
# TARGET-SIDE TYPE OPERATIONS
# ============================================================================

def create_type(conn: Any, type_def: TypeDef) -> None:
    """Create a user-defined type on the target, skipping if already present.

    Known limitation (pre-existing, deliberately unchanged): the existence
    probe only checks the ``public`` schema, so a same-named type in another
    schema is not detected and the CREATE will fail. That failure is audited
    as "skipped" rather than raised, so the migration continues.
    """
    with conn.cursor() as cur:
        try:
            # Check if type already exists
            cur.execute(
                "SELECT 1 FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE t.typname = %s AND n.nspname = 'public'",
                (type_def.name,),
            )
            if cur.fetchone() is not None:
                return
            cur.execute(type_def.ddl)
            conn.commit()
            audit_log(phase="create_type", status="created",
                      details={"type": type_def.name, "kind": type_def.kind})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_type", status="skipped",
                      details={"type": type_def.name, "reason": str(exc)})
