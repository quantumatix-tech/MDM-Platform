"""PostgreSQL function / procedure implementation.

Functions and procedures are intentionally one object family: PostgreSQL
stores both in ``pg_proc`` and distinguishes them only by ``prokind``. Both
are transported as the complete ``pg_get_functiondef`` body, which preserves
the signature, volatility, language, security and body verbatim.
"""
from __future__ import annotations

import re
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
                "SELECT p.proname, n.nspname, pg_get_functiondef(p.oid), p.prokind, "
                "pg_get_function_identity_arguments(p.oid) "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "WHERE n.nspname = ANY(%s) "
                "  AND p.prokind IN ('f', 'p') "
                "ORDER BY p.proname",
                (list(schemas),),
            )
            for row in cur.fetchall():
                func_name, schema_name, ddl, prokind, identity_arguments = row
                funcs.append(FunctionDef(
                    name=func_name,
                    schema_name=schema_name,
                    ddl=ddl,
                    kind="procedure" if prokind == "p" else "function",
                    identity_arguments=identity_arguments,
                ))
    except Exception:
        conn.rollback()   # keep source connection clean for subsequent phases
        raise
    return funcs


# ============================================================================
# TARGET-SIDE FUNCTION OPERATIONS
# ============================================================================

def create_function(conn: Any, func: FunctionDef) -> None:
    """Create a function or procedure on the target.

    Source-managed routines are replaced in place. PostgreSQL cannot use
    CREATE OR REPLACE when a function's return type changes, so a failed
    replacement falls back to dropping and recreating only the exact source
    identity signature. DROP uses the default RESTRICT behavior; dependencies
    are never cascaded. Failures are audited and re-raised so the orchestrator
    reports a skipped routine instead of falsely reporting it as created.
    """
    validate_identifier(func.name, "function")
    func_qname = _qualify(func.schema_name, func.name)
    replace_ddl = re.sub(
        r"^(\s*CREATE\s+)(FUNCTION|PROCEDURE)\b",
        r"\1OR REPLACE \2",
        func.ddl,
        count=1,
        flags=re.IGNORECASE,
    )
    with conn.cursor() as cur:
        try:
            cur.execute(replace_ddl)
            conn.commit()
            audit_log(phase="create_function", status="created", details={"function": func_qname})
        except Exception as exc:
            conn.rollback()
            if func.identity_arguments is None:
                audit_log(phase="create_function", status="skipped",
                          details={"function": func_qname, "reason": str(exc)})
                raise
            routine_kind = "PROCEDURE" if func.kind == "procedure" else "FUNCTION"
            routine_signature = (
                f"{func_qname}({func.identity_arguments})"
            )
            try:
                cur.execute(f"DROP {routine_kind} IF EXISTS {routine_signature}")
                cur.execute(replace_ddl)
                conn.commit()
                audit_log(
                    phase="create_function",
                    status="recreated",
                    details={"function": func_qname, "kind": func.kind},
                )
            except Exception as recreate_exc:
                conn.rollback()
                audit_log(
                    phase="create_function",
                    status="skipped",
                    details={"function": func_qname, "reason": str(recreate_exc)},
                )
                raise recreate_exc from exc
