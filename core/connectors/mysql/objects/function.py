"""MySQL routine (FUNCTION/PROCEDURE) object operations.

Owns routine discovery and routine creation. This module must not import
``source``, ``target`` or ``cdc``: it depends only on ``_models``, the
cross-engine DTOs and the standard library.

DEFINER rewriting and the routine creation policy are deliberately *not*
implemented here. Both already live in ``core.connectors.mysql._models``
(``_rewrite_mysql_definer`` and ``_mysql_routine_policy_error`` /
``MySQLRoutineCreationPolicyError``) and are supplied by the caller as the
``rewrite_definer`` callable, so the call order stays
``DROP -> rewrite DEFINER -> CREATE -> commit`` and the policy error
classification stays in the ``except`` branch exactly as before.

Every SQL statement, parameter order, commit/rollback boundary and the
discovery ordering of the former
``MySQLSourceConnector.list_functions`` and
``MySQLTargetConnector.create_function`` implementations is carried over
unchanged.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.connectors.base import FunctionDef

from core.connectors.mysql._models import (
    _mysql_routine_policy_error,
    _q,
)


def list_functions(
    conn: Any,
    database: str,
    show_create: Callable[[str, str], str],
) -> list[FunctionDef]:
    """Discover every stored FUNCTION and PROCEDURE in ``database``.

    Functions are listed first, then procedures, each in catalog order;
    ``show_create`` supplies the authoritative DDL for each routine.
    """
    result: list[FunctionDef] = []
    with conn.cursor() as cur:
        for kind in ("FUNCTION", "PROCEDURE"):
            cur.execute("SELECT ROUTINE_NAME FROM INFORMATION_SCHEMA.ROUTINES WHERE ROUTINE_SCHEMA=%s AND ROUTINE_TYPE=%s", (database, kind))
            result.extend(FunctionDef(name=n, ddl=show_create(kind, n), schema_name=database, kind=kind.lower()) for (n,) in cur.fetchall())
    return result


def create_function(
    conn: Any,
    function: FunctionDef,
    rewrite_definer: Callable[[str], str],
) -> None:
    """Create ``function`` on the target, dropping any existing routine first.

    The routine DDL has its DEFINER rewritten by the caller's
    ``rewrite_definer`` before execution; a routine creation policy failure
    raised by the server is translated into
    :class:`MySQLRoutineCreationPolicyError` and re-raised from the
    original error.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP {function.kind.upper()} IF EXISTS {_q(function.name)}")
            cur.execute(rewrite_definer(function.ddl))
            conn.commit()
    except Exception as exc:
        conn.rollback()
        policy_error = _mysql_routine_policy_error(exc, "FUNCTION")
        if policy_error is not None:
            raise policy_error from exc
        raise
