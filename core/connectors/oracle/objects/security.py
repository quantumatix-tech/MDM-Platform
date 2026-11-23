"""Oracle object-owner DDL privilege inspection and preflight."""

from __future__ import annotations

from typing import Any

from core.connectors.base import (
    FunctionDef,
    SequenceDef,
    SynonymDef,
    TriggerDef,
    TypeDef,
    ViewDefinition,
    quote_identifier,
)

ORACLE_BOOTSTRAP_PRIVILEGES = frozenset(
    {
        "CREATE TABLE",
        "CREATE TYPE",
        "CREATE VIEW",
        "CREATE MATERIALIZED VIEW",
        "CREATE PROCEDURE",
        "CREATE TRIGGER",
        "CREATE SEQUENCE",
        "CREATE SYNONYM",
    }
)

_DIRECT_OWNER_PRIVILEGES = frozenset(
    {
        "CREATE MATERIALIZED VIEW",
        "CREATE PROCEDURE",
        "CREATE TRIGGER",
    }
)


def required_owner_privileges(
    *,
    tables: set[str] | list[str] | tuple[str, ...] | None = None,
    views: list[ViewDefinition] | None = None,
    sequences: list[SequenceDef] | None = None,
    routines: list[FunctionDef] | None = None,
    triggers: list[TriggerDef] | None = None,
    materialized_views: list[Any] | None = None,
    synonyms: list[SynonymDef] | None = None,
    types: list[TypeDef] | None = None,
) -> dict[str, bool]:
    """Map discovered object kinds to required target system privileges.

    Values are true when Oracle requires the privilege to be directly granted
    to the owner for this supported DDL/compilation path.
    """
    required: dict[str, bool] = {}

    def require(privilege: str, direct: bool = False) -> None:
        required[privilege] = required.get(privilege, False) or direct

    if tables:
        require("CREATE TABLE")
    if views:
        require("CREATE VIEW", direct="CREATE VIEW" in _DIRECT_OWNER_PRIVILEGES)
    if materialized_views:
        require("CREATE MATERIALIZED VIEW", direct=True)
        require("CREATE TABLE", direct=True)
    if routines:
        require("CREATE PROCEDURE", direct="CREATE PROCEDURE" in _DIRECT_OWNER_PRIVILEGES)
    if triggers:
        require("CREATE TRIGGER", direct="CREATE TRIGGER" in _DIRECT_OWNER_PRIVILEGES)
    if sequences:
        require("CREATE SEQUENCE")
    if synonyms:
        require("CREATE SYNONYM")
    if types:
        require("CREATE TYPE")
    return dict(sorted(required.items()))


def inspect_privileges(conn: Any, requirements: dict[str, bool]) -> dict[str, Any]:
    """Inspect direct and currently enabled privileges without changing grants."""
    unsupported = sorted(set(requirements) - ORACLE_BOOTSTRAP_PRIVILEGES)
    if unsupported:
        raise RuntimeError(
            "Oracle privilege policy refused unsupported privilege(s): " + ", ".join(unsupported)
        )
    with conn.cursor() as cur:
        cur.execute("SELECT privilege FROM user_sys_privs")
        direct = {str(row[0]).upper() for row in cur.fetchall()}
        cur.execute("SELECT privilege FROM session_privs")
        effective = {str(row[0]).upper() for row in cur.fetchall()}
        cur.execute("SELECT role FROM session_roles")
        roles = sorted({str(row[0]).upper() for row in cur.fetchall()})
    missing = sorted(
        privilege
        for privilege, direct_required in requirements.items()
        if (privilege not in direct if direct_required else privilege not in effective)
    )
    return {
        "required": [
            {"privilege": privilege, "direct_required": direct_required}
            for privilege, direct_required in requirements.items()
        ],
        "direct_privileges": sorted(direct),
        "effective_privileges": sorted(effective),
        "enabled_roles": roles,
        "missing": missing,
        "proposed_grants": [],
    }


def validate_privileges(
    conn: Any,
    *,
    tables: set[str] | list[str] | tuple[str, ...] | None = None,
    views: list[ViewDefinition] | None = None,
    sequences: list[SequenceDef] | None = None,
    routines: list[FunctionDef] | None = None,
    triggers: list[TriggerDef] | None = None,
    materialized_views: list[Any] | None = None,
    synonyms: list[SynonymDef] | None = None,
    types: list[TypeDef] | None = None,
    target_owner: str | None = None,
) -> dict[str, Any]:
    requirements = required_owner_privileges(
        tables=tables,
        views=views,
        sequences=sequences,
        routines=routines,
        triggers=triggers,
        materialized_views=materialized_views,
        synonyms=synonyms,
        types=types,
    )
    report = inspect_privileges(conn, requirements)
    if report["missing"]:
        report["proposed_grants"] = [
            f"GRANT {privilege} TO {quote_identifier(target_owner)};"
            if target_owner
            else f"GRANT {privilege} TO <TARGET_OWNER>;"
            for privilege in report["missing"]
        ]
        direct_missing = sorted(
            privilege for privilege in report["missing"] if requirements[privilege]
        )
        detail = ", ".join(report["missing"])
        if direct_missing:
            detail += "; direct grants required for " + ", ".join(direct_missing)
        commands = "\n".join(report["proposed_grants"])
        raise RuntimeError(
            "Oracle target account lacks required owner-level object privileges: "
            + detail
            + f"\nAsk the target DBA to run:\n{commands}"
        )
    return report
