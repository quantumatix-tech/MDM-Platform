"""Db2 role discovery, planning and security parity verification.

Read-only discovery and planning only. No DDL execution.
Roles and memberships are planned, not applied, unless explicitly approved.
"""

from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def discover_roles(conn: Any, schema_name: str | None = None, include_system: bool = False) -> list[dict]:
    """Discover database roles and their memberships from SYSCAT.

    Returns a list of role dicts with:
    - name: role name
    - grantee: who holds the role
    - grantee_type: 'U' (user), 'G' (group), 'R' (role), 'P' (public)
    - admin: whether WITH ADMIN OPTION is granted
    """
    roles = []
    with conn.cursor() as cur:
        # Discover role definitions
        cur.execute(
            "SELECT ROLENAME FROM SYSCAT.ROLES ORDER BY ROLENAME",
        )
        role_names = {str(r[0]).strip() for r in cur.fetchall() if r and r[0]}

        # Discover role grants (memberships)
        cur.execute(
            "SELECT GRANTEE, GRANTEETYPE, ROLENAME, ADMIN FROM SYSCAT.ROLEAUTH ORDER BY ROLENAME, GRANTEE",
        )
        for r in cur.fetchall():
            if not r:
                continue
            grantee = str(r[0]).strip()
            gtype = str(r[1]).strip().upper() if r[1] else "U"
            role_name = str(r[2]).strip() if r[2] else ""
            admin = str(r[3]).strip().upper() in ("Y", "G") if r[3] else False
            if not role_name:
                continue
            if not include_system and role_name.upper().startswith("SYS"):
                continue
            roles.append({
                "role": role_name,
                "grantee": grantee,
                "grantee_type": gtype,
                "admin": admin,
                "is_defined_role": role_name in role_names,
            })
    return roles


def discover_role_definitions(conn: Any, include_system: bool = False) -> list[str]:
    """Discover defined role names from SYSCAT.ROLES.

    By default, Db2-provided system roles (names starting with SYS) are excluded.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT ROLENAME FROM SYSCAT.ROLES ORDER BY ROLENAME")
        names = [str(r[0]).strip() for r in cur.fetchall() if r and r[0]]
    if not include_system:
        names = [n for n in names if not n.upper().startswith("SYS")]
    return names


def plan_role_migration(source_roles: list[dict], target_roles: list[dict]) -> dict:
    """Plan role migration (CREATE ROLE + GRANT ROLE).

    Default mode: discovery/planning only. No apply.
    Returns a plan dict with:
    - roles_to_create: roles that exist in source but not target
    - memberships_to_grant: memberships that exist in source but not target
    - conflicts: roles/memberships that differ
    - existing: roles/memberships already present on target
    """
    # Build sets for comparison
    source_role_defs = {r["role"] for r in source_roles}
    target_role_defs = {r["role"] for r in target_roles}

    source_memberships = {(r["role"], r["grantee"], r["grantee_type"]) for r in source_roles}
    target_memberships = {(r["role"], r["grantee"], r["grantee_type"]) for r in target_roles}

    plan = {
        "roles_to_create": sorted(source_role_defs - target_role_defs),
        "roles_existing": sorted(source_role_defs & target_role_defs),
        "roles_excess_on_target": sorted(target_role_defs - source_role_defs),
        "memberships_to_grant": sorted(source_memberships - target_memberships),
        "memberships_existing": sorted(source_memberships & target_memberships),
        "memberships_excess_on_target": sorted(target_memberships - source_memberships),
        "admin_option_diffs": [],
        "conflicts": [],
    }

    # Check admin option differences
    source_admin = {(r["role"], r["grantee"], r["grantee_type"]): r["admin"] for r in source_roles}
    target_admin = {(r["role"], r["grantee"], r["grantee_type"]): r["admin"] for r in target_roles}
    for key in source_memberships & target_memberships:
        if source_admin.get(key) != target_admin.get(key):
            plan["admin_option_diffs"].append({
                "role": key[0],
                "grantee": key[1],
                "grantee_type": key[2],
                "source_admin": source_admin.get(key),
                "target_admin": target_admin.get(key),
            })

    # Detect cycles (role granted to itself)
    for r in source_roles:
        if r["role"] == r["grantee"] and r["grantee_type"] == "R":
            plan["conflicts"].append({
                "type": "self_grant",
                "role": r["role"],
                "detail": "Role granted to itself (cyclic)",
            })

    # Detect invalid grantees (PUBLIC is valid but flagged for review)
    for r in source_roles:
        if r["grantee_type"] == "P":
            plan["conflicts"].append({
                "type": "public_grant",
                "role": r["role"],
                "detail": "Role granted to PUBLIC (requires security approval)",
            })

    return plan


def compare_role_security(source_roles, target_roles, result):
    """Compare role security between source and target.

    Adds mismatches/warnings to result (VerificationResult).
    """
    plan = plan_role_migration(source_roles, target_roles)

    if plan["roles_to_create"]:
        for r in plan["roles_to_create"]:
            result.add_mismatch("ROLE_MISSING", f"Role {r} exists in source but not target")

    if plan["memberships_to_grant"]:
        for m in plan["memberships_to_grant"]:
            result.add_mismatch("ROLE_MEMBERSHIP_MISSING", f"Role {m[0]} not granted to {m[1]} ({m[2]}) on target")

    if plan["memberships_excess_on_target"]:
        for m in plan["memberships_excess_on_target"]:
            result.add_warning("ROLE_MEMBERSHIP_EXCESS", f"Role {m[0]} granted to {m[1]} ({m[2]}) on target but not source")

    if plan["admin_option_diffs"]:
        for d in plan["admin_option_diffs"]:
            result.add_warning("ROLE_ADMIN_OPTION_DIFF", f"Role {d['role']} to {d['grantee']}: source admin={d['source_admin']}, target admin={d['target_admin']}")

    if plan["conflicts"]:
        for c in plan["conflicts"]:
            result.add_warning("ROLE_CONFLICT", f"{c['type']}: {c['detail']}")


def validate_role_identifier(name: str) -> bool:
    """Validate a role name is a safe Db2 identifier."""
    try:
        validate_identifier(name, "role")
        return True
    except (ValueError, TypeError):
        return False


# Minimum privilege analysis
MINIMUM_PRIVILEGES = {
    "source": {
        "verified": [
            "CONNECT on database",
            "SELECT on SYSCAT.* views (granted to PUBLIC by default)",
            "SELECT on source schema tables",
        ],
        "proposed_not_tested": [
            "CONNECT on database",
            "SELECT on specific schema tables only",
        ],
    },
    "target": {
        "verified": [
            "DBADM on database (broad convenience, tested in E2E)",
            "SECADM on database (tested, required for grant operations)",
            "DATAACCESS on database (tested)",
            "ACCESSCTRL on database (tested)",
            "CREATE_EXTERNAL_ROUTINE on database (tested)",
        ],
        "proposed_not_tested": [
            "CREATEIN on target schema",
            "SELECT/INSERT/UPDATE/DELETE on target tables",
            "USAGE on sequences",
            "EXECUTE on routines",
            "CREATE_EXTERNAL_ROUTINE on database",
        ],
    },
}
