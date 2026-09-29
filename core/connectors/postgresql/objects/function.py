"""PostgreSQL function / procedure implementation.

Functions and procedures are intentionally one object family: PostgreSQL
stores both in ``pg_proc`` and distinguishes them only by ``prokind``. Both
are transported as the complete ``pg_get_functiondef`` body, which preserves
the signature, volatility, language, security and body verbatim.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    FunctionDef,
    validate_identifier,
)
from core.connectors.postgresql._models import _qualify


# ============================================================================
# SOURCE-SIDE FUNCTION OPERATIONS
# ============================================================================

def discover_functions(conn: Any, schemas: tuple[str, ...]) -> list[FunctionDef]:
    """Return user functions and stored procedures in the configured schemas.

    ``prokind IN ('f', 'p')`` selects functions and procedures while already
    excluding aggregates and window functions, so no ``proisagg`` check is
    needed (that column only exists on PG10 and earlier).

    On failure the connection is rolled back before re-raising, so a discovery
    error cannot leave the source connection in a failed transaction for the
    later migration phases.
    """
    funcs: list[FunctionDef] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.proname, n.nspname, pg_get_functiondef(p.oid) "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "WHERE n.nspname = ANY(%s) "
                "  AND p.prokind IN ('f', 'p') "
                "ORDER BY p.proname",
                (list(schemas),),
            )
            for row in cur.fetchall():
                func_name, schema_name, ddl = row
                funcs.append(FunctionDef(name=func_name, schema_name=schema_name, ddl=ddl))
    except Exception:
        conn.rollback()   # keep source connection clean for subsequent phases
        raise
    return funcs


# ============================================================================
# TARGET-SIDE FUNCTION OPERATIONS
# ============================================================================

def create_function(conn: Any, func: FunctionDef) -> None:
    """Create a function or procedure on the target.

    The pre-computed DDL is executed as-is. A failure is audited as "skipped"
    rather than raised, so one unsupported routine does not stop the migration.
    """
    validate_identifier(func.name, "function")
    func_qname = _qualify(func.schema_name, func.name)
    with conn.cursor() as cur:
        try:
            cur.execute(func.ddl)
            conn.commit()
            audit_log(phase="create_function", status="created", details={"function": func_qname})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_function", status="skipped",
                      details={"function": func_qname, "reason": str(exc)})
