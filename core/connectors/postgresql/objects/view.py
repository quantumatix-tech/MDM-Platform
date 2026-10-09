"""PostgreSQL View object implementation.

Views and materialized views are handled as one object family: both are
definition-driven, schema-scoped relations whose body is produced by
``pg_get_viewdef``/``information_schema`` on the source and replayed on the
target. Materialized views are created ``WITH NO DATA`` so the (potentially
expensive) population is deferred to an explicit refresh after all
dependencies exist.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    MaterializedViewDef,
    ViewDefinition,
    validate_identifier,
)
from core.connectors.postgresql._models import _qualify


# ============================================================================
# SOURCE-SIDE VIEW OPERATIONS
# ============================================================================

def discover_views(conn: Any, schemas: tuple[str, ...]) -> list[ViewDefinition]:
    """Return ordinary SQL views in the configured schemas."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, table_schema, view_definition "
            "FROM information_schema.views "
            "WHERE table_schema = ANY(%s) "
            "ORDER BY table_name",
            (list(schemas),),
        )
        return [ViewDefinition(name=row[0], schema_name=row[1], definition=row[2]) for row in cur.fetchall()]


def discover_materialized_views(conn: Any, schemas: tuple[str, ...]) -> list[MaterializedViewDef]:
    """Return materialized views in the configured schemas."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.matviewname, m.schemaname, pg_get_viewdef(c.oid) "
            "FROM pg_matviews m "
            "JOIN pg_class c ON c.relname = m.matviewname "
            "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = m.schemaname "
            "WHERE m.schemaname = ANY(%s) "
            "ORDER BY m.matviewname",
            (list(schemas),),
        )
        return [MaterializedViewDef(name=row[0], schema_name=row[1], definition=row[2]) for row in cur.fetchall()]


# ============================================================================
# TARGET-SIDE VIEW OPERATIONS
# ============================================================================

def create_view(conn: Any, view: ViewDefinition) -> None:
    """Create or replace a view on the target.

    Unlike most object application methods this one raises on failure: a view
    that silently fails to create leaves dependent objects and application
    queries broken, so the failure must surface to the orchestrator.
    """
    validate_identifier(view.name, "view")
    view_name = _qualify(view.schema_name, view.name)
    with conn.cursor() as cur:
        try:
            cur.execute(f"CREATE OR REPLACE VIEW {view_name} AS {view.definition}")
            conn.commit()
            audit_log(phase="create_view", status="created", details={"view": view.name})
            return
        except Exception:
            conn.rollback()

        # PostgreSQL cannot replace a view when its output column types or
        # order changed. Rebuild only this source-managed view, and use the
        # default RESTRICT behavior so dependent objects are never cascaded.
        try:
            cur.execute(f"DROP VIEW {view_name}")
            cur.execute(f"CREATE VIEW {view_name} AS {view.definition}")
            conn.commit()
            audit_log(phase="create_view", status="recreated", details={"view": view.name})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_view", status="failed",
                      details={"view": view.name, "reason": str(exc)})
            raise


def create_materialized_view(conn: Any, mv: MaterializedViewDef) -> None:
    """Create a materialized view on the target, unpopulated.

    Created ``WITH NO DATA`` so the first ``REFRESH MATERIALIZED VIEW`` can run
    after every dependent table and view exists. A failure is raised, matching
    ``create_view``.
    """
    validate_identifier(mv.name, "materialized view")
    mv_schema = mv.schema_name or "public"
    mv_qname = _qualify(mv_schema, mv.name)
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT 1 FROM pg_matviews WHERE matviewname = %s AND schemaname = %s",
                (mv.name, mv_schema),
            )
            if cur.fetchone() is not None:
                return
            # pg_get_viewdef may include a trailing semicolon — strip it before
            # appending WITH NO DATA (which must be the last clause)
            clean_def = mv.definition.rstrip().rstrip(";")
            cur.execute(
                f"CREATE MATERIALIZED VIEW {mv_qname} AS {clean_def} WITH NO DATA"
            )
            conn.commit()
            audit_log(phase="create_matview", status="created", details={"matview": mv_qname})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_matview", status="failed",
                      details={"matview": mv_qname, "reason": str(exc)})
            raise


def reconcile_materialized_view(conn: Any, mv: MaterializedViewDef) -> None:
    """Restore one source-managed materialized view definition safely.

    PostgreSQL has no CREATE OR REPLACE MATERIALIZED VIEW. A changed
    definition is rebuilt with DROP's default RESTRICT behavior, so dependent
    target-only objects prevent the replacement instead of being cascaded.
    """
    validate_identifier(mv.name, "materialized view")
    mv_schema = mv.schema_name or "public"
    mv_qname = _qualify(mv_schema, mv.name)
    source_definition = mv.definition.rstrip().rstrip(";").strip()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_catalog.pg_get_viewdef(c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'm'",
                (mv_schema, mv.name),
            )
            row = cur.fetchone()
            if row is not None and row[0].rstrip().rstrip(";").strip() == source_definition:
                conn.commit()
                return
            if row is not None:
                cur.execute(f"DROP MATERIALIZED VIEW {mv_qname}")
            cur.execute(
                f"CREATE MATERIALIZED VIEW {mv_qname} AS {source_definition} WITH NO DATA"
            )
        conn.commit()
        audit_log(
            phase="reconcile_matview",
            status="recreated" if row is not None else "created",
            details={"matview": mv_qname},
        )
    except Exception as exc:
        conn.rollback()
        audit_log(phase="reconcile_matview", status="failed",
                  details={"matview": mv_qname, "reason": str(exc)})
        raise


def refresh_materialized_view(conn: Any, name: str, schema_name: str | None = None) -> None:
    """Populate a materialized view.

    Runs after creation and after its dependencies exist. A failure is
    swallowed and audited, so one unrefreshable matview does not fail the run.
    """
    validate_identifier(name, "materialized view")
    mv_qname = _qualify(schema_name, name)
    with conn.cursor() as cur:
        try:
            cur.execute(f"REFRESH MATERIALIZED VIEW {mv_qname}")
            conn.commit()
            audit_log(phase="refresh_matview", status="refreshed", details={"matview": mv_qname})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="refresh_matview", status="failed",
                      details={"matview": mv_qname, "reason": str(exc)})
