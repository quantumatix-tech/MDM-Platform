"""Least-privilege Oracle permission discovery, preflight, application and verification."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from core.connectors.base import GrantDef, RoleDef, RoleMembershipDef, UserDef, quote_identifier
from core.connectors.oracle._models import validate_oracle_identifier

OBJECT_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
COLUMN_PRIVILEGES = frozenset({"INSERT", "UPDATE", "REFERENCES"})
SYSTEM_PRIVILEGES = frozenset({"CREATE SESSION"})
PROVENANCE_TABLE = "DMT_PERMISSION_PROVENANCE"
PROVENANCE_META_KEY = "__SCHEMA_V1__"


class LegacyPermissionAdoptionRequired(RuntimeError):
    """Raised with exact candidates when legacy grant ownership needs consent."""

    def __init__(self, owner: str, items: list[dict[str, Any]]) -> None:
        self.owner = owner
        self.items = items
        super().__init__(_legacy_adoption_diagnostic(owner, items))

# Catalog views used by the permission inventory and its target-side safety
# checks. Keep this list aligned with the SELECT statements below. A dedicated
# executor needs only SELECT on these exact views; broad dictionary privileges
# are neither required nor accepted by the onboarding path.
PERMISSION_CATALOG_VIEWS = (
    "DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS",
    "DBA_TAB_PRIVS", "DBA_COL_PRIVS",
)


def normalize_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        raise RuntimeError("Oracle permission migration requires enabled: true")
    allowed = {
        "enabled", "new_user_policy", "approve_role_creation_admin_option",
        "approve_create_session", "executor", "onboarding", "secret_provider",
        "legacy_grant_adoption",
    }
    unexpected = set(policy) - allowed
    if unexpected:
        raise RuntimeError(
            "Oracle permission migration is metadata-driven; remove per-principal "
            "mappings and per-grant allowlists: " + ", ".join(sorted(unexpected))
        )
    user_policy = str(policy.get("new_user_policy", "LOCKED")).strip().upper()
    if user_policy not in {"LOCKED", "FAIL"}:
        raise RuntimeError("oracle_permission_migration.new_user_policy must be LOCKED or FAIL")
    role_admin_approval = policy.get("approve_role_creation_admin_option", False)
    create_session_approval = policy.get("approve_create_session", False)
    onboarding = policy.get("onboarding", {})
    if not isinstance(onboarding, dict):
        raise RuntimeError("oracle_permission_migration.onboarding must be an object")  # noqa: TRY004 - Keep the setup API RuntimeError validation contract.
    unexpected_onboarding = set(onboarding) - {
        "enabled", "approve_catalog_access", "approve_create_user",
        "approve_create_role", "approve_create_session",
        "approve_role_admin_option", "admin_connection",
    }
    if unexpected_onboarding:
        raise RuntimeError("Unsupported Oracle permission onboarding setting(s): " + ", ".join(sorted(unexpected_onboarding)))
    adoption = policy.get("legacy_grant_adoption", {})
    if not isinstance(adoption, dict) or set(adoption) - {"approved_permission_keys"}:
        raise RuntimeError("oracle_permission_migration.legacy_grant_adoption accepts only approved_permission_keys")
    approved_keys = adoption.get("approved_permission_keys", [])
    if not isinstance(approved_keys, list) or any(not isinstance(key, str) or len(key) != 64 for key in approved_keys):
        raise RuntimeError("legacy_grant_adoption.approved_permission_keys must be a list of permission SHA-256 keys")
    if "enabled" in onboarding and not isinstance(onboarding["enabled"], bool):
        raise RuntimeError("Oracle permission onboarding.enabled must be boolean")
    for key in (
        "approve_catalog_access", "approve_create_user", "approve_create_role",
        "approve_create_session", "approve_role_admin_option",
    ):
        if key in onboarding and not isinstance(onboarding[key], bool):
            raise RuntimeError(f"Oracle permission onboarding.{key} must be boolean")
    admin_connection = onboarding.get("admin_connection")
    if admin_connection is not None:
        if not isinstance(admin_connection, dict) or set(admin_connection) - {"username", "password_secret"}:
            raise RuntimeError(
                "Oracle permission onboarding.admin_connection accepts only username and password_secret"
            )
        if "password" in admin_connection or not admin_connection.get("password_secret"):
            raise RuntimeError(
                "Oracle permission onboarding admin credentials must use password_secret"
            )
    executor = policy.get("executor")
    if executor is not None:
        if not isinstance(executor, dict) or set(executor) - {"username", "password_secret", "target_credentials"}:
            raise RuntimeError(
            "Oracle permission executor accepts username and password_secret, or explicit target_credentials; "
                "connection endpoint is inherited from TARGET"
            )
        if not executor.get("username") or not (
            executor.get("password_secret")
            or (executor.get("target_credentials") is True)
        ):
            raise RuntimeError(
                "Oracle permission executor requires username and password_secret, or explicit target_credentials: true"
            )
        if "target_credentials" in executor and not isinstance(executor["target_credentials"], bool):
            raise RuntimeError("Oracle permission executor target_credentials must be boolean")
        if "password" in executor:
            raise RuntimeError("Oracle permission executor plaintext passwords are not accepted")
    if not isinstance(role_admin_approval, bool) or not isinstance(create_session_approval, bool):
        raise RuntimeError(  # noqa: TRY004 - Keep the setup API RuntimeError validation contract.
            "Oracle permission migration approval fields must be boolean"
        )
    return {
        "enabled": True,
        "user_mappings": {},
        "role_mappings": {},
        "new_user_policy": user_policy,
        "approve_role_creation_admin_option": role_admin_approval,
        "approve_create_session": create_session_approval,
        "approved_object_privileges": OBJECT_PRIVILEGES,
        "approved_column_privileges": COLUMN_PRIVILEGES,
        "approved_system_privileges": (
            SYSTEM_PRIVILEGES if create_session_approval else frozenset()
        ),
        "executor": policy.get("executor"),
        "onboarding": onboarding,
        "legacy_grant_adoption": {"approved_permission_keys": approved_keys},
    }


def bootstrap_permission_executor(
    admin_conn: Any, executor_conn: Any, *, admin_user: str, executor_user: str,
    target_identity: dict[str, str], inventory: dict[str, Any], raw_policy: dict[str, Any],
) -> list[str]:
    """Apply explicitly approved, narrow one-time grants to a dedicated executor.

    The bootstrap administrator must hold grant option on each catalog view and
    ADMIN OPTION on each exact system privilege/role. No ANY or dictionary-wide
    privilege is accepted.
    """
    policy = normalize_policy(raw_policy)
    discovered_policy = inventory.get("policy", {})
    policy["user_mappings"] = dict(
        discovered_policy.get("user_mappings", inventory.get("user_mappings", {}))
    )
    policy["role_mappings"] = dict(
        discovered_policy.get("role_mappings", inventory.get("role_mappings", {}))
    )
    onboarding = policy["onboarding"]
    if onboarding.get("enabled") is not True:
        return []
    if admin_user.upper() in {"SYS", "SYSTEM"} or admin_user.casefold() == executor_user.casefold():
        raise RuntimeError("Oracle permission onboarding requires a separate non-SYS admin identity")
    expected_identity = target_identity
    actual_admin_identity = _identity(admin_conn)
    if actual_admin_identity["user"].casefold() != admin_user.casefold():
        raise RuntimeError("Oracle permission onboarding admin session does not match configured identity")
    if any(
        actual_admin_identity[key].casefold() != expected_identity[key].casefold()
        for key in ("db_unique_name", "container_name", "service_name")
    ):
        raise RuntimeError("Oracle permission onboarding admin is connected to a different database, PDB, or service")
    actual_executor_identity = _identity(executor_conn)
    if actual_executor_identity["user"].casefold() != executor_user.casefold() or any(
        actual_executor_identity[key].casefold() != expected_identity[key].casefold()
        for key in ("db_unique_name", "container_name", "service_name")
    ):
        raise RuntimeError("Oracle permission executor identity or PDB does not match its approved scope")

    gaps: list[str] = []
    if not onboarding.get("approve_catalog_access", False):
        gaps.append("explicit approval for direct SELECT on the six permission catalog views")

    # Check direct object grant option for every catalog view before issuing any
    # DDL. A role-derived catalog privilege is deliberately not treated as grant
    # authority.
    try:
        grantable_views = {
            str(row[0]).upper() for row in _fetchall(
                admin_conn,
                "SELECT table_name FROM user_tab_privs_recd WHERE owner='SYS' "
                "AND privilege='SELECT' AND grantable='YES'",
            )
        }
        for view in PERMISSION_CATALOG_VIEWS:
            if view not in grantable_views:
                gaps.append(f"direct SELECT WITH GRANT OPTION on SYS.{view} for onboarding admin")
    except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
        gaps.append(f"inspect direct catalog grant authority: {exc}")

    # Determine the minimal system privileges required for the discovered plan.
    target_users = set(policy["user_mappings"].values())
    target_roles = set(policy["role_mappings"].values())
    source_system = any(
        grant.object_type == "SYSTEM" and grant.privileges.upper() == "CREATE SESSION"
        for grant in inventory.get("grants", [])
    )
    missing_users: set[str] = set()
    missing_roles: set[str] = set()
    user_conflicts: set[str] = set()
    role_conflicts: set[str] = set()
    for name in sorted(target_users):
        try:
            if not _fetchall(admin_conn, "SELECT username FROM dba_users WHERE username=:1", (name,)):
                if _fetchall(admin_conn, "SELECT role FROM dba_roles WHERE role=:1", (name,)):
                    user_conflicts.add(name)
                else:
                    missing_users.add(name)
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"inspect mapped target user {name}: {exc}")
    for name in sorted(target_roles):
        try:
            if not _fetchall(admin_conn, "SELECT role FROM dba_roles WHERE role=:1", (name,)):
                if _fetchall(admin_conn, "SELECT username FROM dba_users WHERE username=:1", (name,)):
                    role_conflicts.add(name)
                else:
                    missing_roles.add(name)
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"inspect mapped target role {name}: {exc}")
    gaps.extend(f"mapped target user {name} conflicts with an existing role" for name in sorted(user_conflicts))
    gaps.extend(f"mapped target role {name} conflicts with an existing user" for name in sorted(role_conflicts))

    required_sys = set()
    if missing_users:
        required_sys.add("CREATE USER")
        if not onboarding.get("approve_create_user", False):
            gaps.append("explicit approval to grant CREATE USER to the executor")
    if missing_roles:
        required_sys.add("CREATE ROLE")
        if not onboarding.get("approve_create_role", False):
            gaps.append("explicit approval to grant CREATE ROLE to the executor")
    if source_system:
        required_sys.add("CREATE SESSION")
        if not onboarding.get("approve_create_session", False):
            gaps.append("explicit approval to grant CREATE SESSION WITH ADMIN OPTION to the executor")
    try:
        admin_sys = {
            str(row[0]).upper() for row in _fetchall(
                admin_conn, "SELECT privilege FROM user_sys_privs WHERE admin_option='YES'"
            )
        }
        for privilege in sorted(required_sys - admin_sys):
            gaps.append(f"direct ADMIN OPTION on {privilege} for onboarding admin")
    except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
        gaps.append(f"inspect onboarding admin system privilege authority: {exc}")

    existing_membership_roles = {
        str(policy["role_mappings"].get(item.role_name, item.role_name))
        for item in inventory.get("memberships", [])
        if policy["role_mappings"].get(item.role_name, item.role_name) in target_roles
        and policy["role_mappings"].get(item.role_name, item.role_name) not in missing_roles
    }
    if existing_membership_roles:
        if not onboarding.get("approve_role_admin_option", False):
            gaps.append("explicit approval to grant ADMIN OPTION on source-mapped existing target roles")
        try:
            admin_roles = {
                str(row[0]) for row in _fetchall(
                    admin_conn,
                    "SELECT granted_role FROM user_role_privs WHERE admin_option='YES'",
                )
            }
            for role in sorted(existing_membership_roles - admin_roles):
                gaps.append(f"ADMIN OPTION on mapped target role {role} for onboarding admin")
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"inspect onboarding admin role authority: {exc}")

    if gaps:
        raise RuntimeError(
            "Oracle permission onboarding preflight found multiple missing prerequisites: "
            + "; ".join(dict.fromkeys(gaps))
            + ". No onboarding grants were issued."
        )

    statements = [
        f"GRANT SELECT ON SYS.{view} TO {quote_identifier(executor_user)}"
        for view in PERMISSION_CATALOG_VIEWS
    ]
    statements.extend(
        f"GRANT {privilege} TO {quote_identifier(executor_user)}"
        for privilege in sorted(required_sys)
        if privilege != "CREATE SESSION"
    )
    if "CREATE SESSION" in required_sys:
        statements.append(
            f"GRANT CREATE SESSION TO {quote_identifier(executor_user)} WITH ADMIN OPTION"
        )
    statements.extend(
        f"GRANT {quote_identifier(role)} TO {quote_identifier(executor_user)} WITH ADMIN OPTION"
        for role in sorted(existing_membership_roles)
    )
    applied: list[str] = []
    for statement in statements:
        try:
            with admin_conn.cursor() as cur:
                cur.execute(statement)
            applied.append(statement)
        except Exception:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                "Oracle permission onboarding stopped after an authorized grant failure; "
                f"completed {len(applied)} of {len(statements)} approved statements. "
                "Inspect the executor grants before retrying."
            ) from None
    return applied


def _identity(conn: Any) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT USER, SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), "
            "SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
        )
        row = cur.fetchone()
    if row is None or len(row) != 4 or any(value is None for value in row):
        raise RuntimeError("Oracle permission onboarding could not verify database/PDB identity")
    return dict(zip(("user", "db_unique_name", "container_name", "service_name"), map(str, row)))


def _fetchall(conn: Any, statement: str, binds: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    try:
        with conn.cursor() as cur:
            cur.execute(statement, binds)
            return list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
        raise RuntimeError(
            f"Oracle permission catalog discovery failed for {statement.split('FROM', 1)[-1].strip()}: "
            f"{type(exc).__name__}: {exc}"
        ) from None


def verify_source_catalog_access(conn: Any) -> list[str]:
    """Probe every catalog source discovery depends on before reading any rows."""
    checks = (
        "ALL_TAB_PRIVS_MADE", "ALL_COL_PRIVS_MADE", "DBA_USERS", "DBA_ROLES",
        "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS",
    )
    missing: list[str] = []
    for view in checks:
        reference = f"SYS.{view}" if view.startswith("DBA_") else view
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM {reference} WHERE 1 = 0")
                cur.fetchall()
        except Exception:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            missing.append(f"SELECT on {reference}")
    return missing


def discover(
    conn: Any, owner: str, raw_policy: dict[str, Any], *, catalog_scope: str = "owner"
) -> dict[str, Any]:
    """Discover a permission graph from an owner session or a catalog reader.

    ``owner`` keeps the migration path limited to grants made by the connected
    source/target owner. ``dba`` is for read-only acceptance validation through
    the explicitly configured permission executor and uses the complete DBA
    grant views. Both paths require full catalog visibility before reading.
    """
    policy = normalize_policy(raw_policy)
    if catalog_scope not in {"owner", "dba"}:
        raise ValueError("Oracle permission catalog_scope must be 'owner' or 'dba'")
    catalog_gaps = (
        verify_source_catalog_access(conn)
        if catalog_scope == "owner" else verify_executor_catalog_access(conn)
    )
    if catalog_gaps:
        missing_dba_views = [
            item.removeprefix("SELECT on ")
            for item in catalog_gaps
            if item.startswith("SELECT on SYS.DBA_")
        ]
        grants = "; ".join(
            f"GRANT SELECT ON {view} TO {quote_identifier(owner)}"
            for view in missing_dba_views
        )
        grant_note = (
            f" Required least-privilege grants for the connected SOURCE owner: {grants}."
            if grants else ""
        )
        raise RuntimeError(
            "Oracle permission preflight found multiple inaccessible catalog views: "
            + ", ".join(catalog_gaps)
            + ". Arrange these exact PDB-local metadata grants once with the source DBA; "
            "no permission rows were assumed."
            + grant_note
        )
    object_groups: dict[tuple[str, str, str], set[str]] = {}
    column_groups: dict[tuple[str, str, str], set[str]] = {}
    seed_principals: set[str] = set()
    object_view = "all_tab_privs_made" if catalog_scope == "owner" else "dba_tab_privs"
    column_view = "all_col_privs_made" if catalog_scope == "owner" else "dba_col_privs"
    object_rows = _fetchall(
        conn,
        "SELECT grantee, owner, table_name, grantor, privilege, grantable, "
        f"hierarchy, common, type, inherited FROM {object_view} WHERE owner = :1 "
        "ORDER BY table_name, grantee, privilege",
        (owner,),
    )
    for row in object_rows:
        if len(row) != 10:
            raise RuntimeError("Malformed ALL_TAB_PRIVS_MADE row; refusing incomplete permission inventory")
        grantee, row_owner, name, _grantor, privilege, grantable, _hierarchy, common, object_type, inherited = row
        if str(row_owner).casefold() != owner.casefold():
            raise RuntimeError(
                f"Oracle grant catalog returned owner {row_owner} outside requested source owner {owner}"
            )
        # Oracle exposes GRANT INHERIT PRIVILEGES ON USER through this view.
        # It is a user-level grant and is not a grant on a source-owned object.
        if str(object_type).upper() == "USER" or str(privilege).upper() == "INHERIT PRIVILEGES":
            continue
        if str(common).upper() != "NO" or str(inherited).upper() != "NO":
            raise RuntimeError(
                f"Oracle common or inherited object grant on {name} is unsupported"
            )
        grantee_key = str(grantee)
        privilege_key, type_key = str(privilege).upper(), str(object_type).upper()
        if grantee_key.upper() == "PUBLIC":
            raise RuntimeError(f"Oracle PUBLIC object grant on {name} is unsupported")
        if type_key not in {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
            raise RuntimeError(f"Unsupported Oracle object grant type {object_type} on {name}")
        if privilege_key not in OBJECT_PRIVILEGES:
            raise RuntimeError(f"Unsupported Oracle object privilege {privilege_key} on {name}")
        if str(grantable).upper() != "NO":
            raise RuntimeError(f"Oracle grant option on {name} to {grantee} is unsupported")
        object_groups.setdefault((grantee_key, type_key, str(name)), set()).add(privilege_key)
        seed_principals.add(grantee_key)

    column_rows = _fetchall(
        conn,
        "SELECT grantee, owner, table_name, column_name, grantor, privilege, "
        f"grantable, common, inherited FROM {column_view} WHERE owner = :1 "
        "ORDER BY table_name, column_name, grantee, privilege",
        (owner,),
    )
    for row in column_rows:
        if len(row) != 9:
            raise RuntimeError("Malformed ALL_COL_PRIVS_MADE row; refusing incomplete permission inventory")
        grantee, row_owner, name, column, _grantor, privilege, grantable, common, inherited = row
        if str(row_owner).casefold() != owner.casefold():
            raise RuntimeError(
                f"Oracle column grant catalog returned owner {row_owner} outside requested source owner {owner}"
            )
        if str(common).upper() != "NO" or str(inherited).upper() != "NO":
            raise RuntimeError(
                f"Oracle common or inherited column grant on {name}.{column} is unsupported"
            )
        # GrantDef represents a column target as TABLE.COLUMN. Reject the
        # ambiguous case where either quoted identifier itself contains a
        # period instead of risking a grant on the wrong object.
        if "." in str(name) or "." in str(column):
            raise RuntimeError(
                f"Oracle column grant on {name}.{column} uses a period in an identifier; "
                "this permission mapping is unsupported"
            )
        grantee_key, privilege_key = str(grantee), str(privilege).upper()
        if grantee_key.upper() == "PUBLIC":
            raise RuntimeError(f"Oracle PUBLIC column grant on {name}.{column} is unsupported")
        if privilege_key not in COLUMN_PRIVILEGES:
            raise RuntimeError(f"Unsupported Oracle column privilege {privilege_key} on {name}.{column}")
        if str(grantable).upper() != "NO":
            raise RuntimeError(f"Oracle column grant option on {name}.{column} to {grantee} is unsupported")
        column_groups.setdefault((grantee_key, str(name), str(column)), set()).add(privilege_key)
        seed_principals.add(grantee_key)

    if owner in seed_principals:
        raise RuntimeError(
            "Oracle permission migration refuses source-owner self-grants; owner identity "
            "is mapped separately to the configured target owner"
        )

    # Expand only the membership component connected to grantees of objects
    # owned by this source schema. This discovers relevant users/roles without
    # migrating every account in the PDB.
    memberships_rows = _fetchall(
        conn,
        "SELECT grantee, granted_role, admin_option, default_role, common, inherited "
        "FROM dba_role_privs ORDER BY grantee, granted_role",
    )
    connected = set(seed_principals)
    connected_edges: dict[tuple[str, str], tuple[Any, ...]] = {}
    changed = True
    while changed:
        changed = False
        for row in memberships_rows:
            if len(row) != 6:
                raise RuntimeError("Malformed DBA_ROLE_PRIVS row; refusing incomplete permission inventory")
            member, granted_role = str(row[0]), str(row[1])
            if member not in connected and granted_role not in connected:
                continue
            if member.upper() == "PUBLIC" or granted_role.upper() == "PUBLIC":
                raise RuntimeError("Oracle PUBLIC role membership is unsupported")
            if _is_oracle_maintained_principal(conn, member):
                # Oracle-managed grantees can legitimately hold role grants
                # with ADMIN OPTION. They are not application membership edges
                # and must not enter the source-derived target role graph.
                continue
            if str(row[2]).upper() != "NO" or str(row[3]).upper() != "YES":
                raise RuntimeError(
                    f"Role membership {member}->{granted_role} uses unsupported ADMIN_OPTION "
                    "or non-default role state"
                )
            if str(row[4]).upper() != "NO" or str(row[5]).upper() != "NO":
                raise RuntimeError(
                    f"Common or inherited role membership {member}->{granted_role} is unsupported"
                )
            connected_edges[(member, granted_role)] = tuple(row)
            for principal in (member, granted_role):
                if principal not in connected:
                    connected.add(principal)
                    changed = True

    if owner.casefold() in {name.casefold() for name in connected}:
        raise RuntimeError(
            "Oracle source-owner role memberships are excluded from application-principal "
            "migration; owner-to-target-owner mapping is managed separately"
        )

    users: dict[str, str] = {}
    roles: dict[str, str] = {}
    user_defs: list[UserDef] = []
    role_defs: list[RoleDef] = []
    for principal in sorted(connected):
        validate_oracle_identifier(principal, "discovered Oracle principal")
        if principal.upper() in {"PUBLIC", "SYS", "SYSTEM"}:
            raise RuntimeError(f"Oracle privileged principal {principal} is outside automatic migration scope")
        user_rows = _fetchall(
            conn,
            "SELECT username, oracle_maintained, common FROM dba_users WHERE username = :1",
            (principal,),
        )
        role_rows = _fetchall(
            conn,
            "SELECT role, oracle_maintained, common, authentication_type "
            "FROM dba_roles WHERE role = :1",
            (principal,),
        )
        if user_rows and role_rows:
            raise RuntimeError(
                f"Oracle principal {principal} is both a user and a role; automatic mapping is ambiguous"
            )
        if len(user_rows) == 1:
            if str(user_rows[0][1]).upper() != "N" or str(user_rows[0][2]).upper() != "NO":
                raise RuntimeError(f"Oracle principal {principal} is common or Oracle-maintained")
            users[principal] = principal
            user_defs.append(UserDef(name=principal))
        elif len(role_rows) == 1:
            if str(role_rows[0][1]).upper() != "N" or str(role_rows[0][2]).upper() != "NO":
                raise RuntimeError(f"Oracle role {principal} is common or Oracle-maintained")
            if str(role_rows[0][3]).upper() != "NONE":
                raise RuntimeError(
                    f"Oracle role {principal} uses {role_rows[0][3]} authentication; "
                    "only local NOT IDENTIFIED roles are supported"
                )
            roles[principal] = principal
            role_defs.append(RoleDef(name=principal))
        else:
            raise RuntimeError(
                f"Oracle permission principal {principal} is missing from DBA_USERS and DBA_ROLES"
            )

    if set(users).intersection(roles):
        raise RuntimeError("Oracle source principal has ambiguous user/role identity")
    policy["user_mappings"] = users
    policy["role_mappings"] = roles
    grants = [
        GrantDef(
            privileges=", ".join(sorted(privileges)), object_type=object_type,
            object_name=name, grantee=grantee, schema_name=owner,
        )
        for (grantee, object_type, name), privileges in sorted(object_groups.items())
    ]
    grants.extend(
        GrantDef(
            privileges=", ".join(sorted(privileges)), object_type="COLUMN",
            object_name=f"{table}.{column}", grantee=grantee, schema_name=owner,
        )
        for (grantee, table, column), privileges in sorted(column_groups.items())
    )

    principals = {**users, **roles}
    system_grants: list[GrantDef] = []
    for principal in principals:
        rows = _fetchall(
            conn,
            "SELECT privilege, admin_option, common, inherited FROM dba_sys_privs "
            "WHERE grantee = :1 ORDER BY privilege",
            (principal,),
        )
        for row in rows:
            if len(row) != 4:
                raise RuntimeError("Malformed DBA_SYS_PRIVS row; refusing incomplete permission inventory")
            privilege, admin_option, common, inherited = row
            name = str(privilege).upper()
            if name != "CREATE SESSION":
                raise RuntimeError(f"Unsupported Oracle system privilege {name} for {principal}")
            if not policy["approve_create_session"]:
                raise RuntimeError(
                    f"Source principal {principal} has CREATE SESSION; set "
                    "oracle_permission_migration.approve_create_session: true to approve it"
                )
            if str(admin_option).upper() != "NO":
                raise RuntimeError(f"Oracle system privilege ADMIN OPTION for {principal}.{name} is unsupported")
            if str(common).upper() != "NO" or str(inherited).upper() != "NO":
                raise RuntimeError(
                    f"Common or inherited Oracle system privilege {name} for {principal} is unsupported"
                )
            system_grants.append(GrantDef(
                privileges=name, object_type="SYSTEM", object_name="*",
                grantee=principal, schema_name=owner,
            ))

    memberships: dict[tuple[str, str], RoleMembershipDef] = {}
    for member, granted_role in connected_edges:
        if member not in users and member not in roles:
            raise RuntimeError(f"Role member {member} is not a supported local application principal")
        if granted_role not in roles:
            raise RuntimeError(f"Granted role {granted_role} is not a supported local application role")
        memberships[(member, granted_role)] = RoleMembershipDef(
            member_name=member, role_name=granted_role, with_admin_option=False,
        )

    return {
        "policy": policy,
        "users": user_defs,
        "roles": role_defs,
        "grants": grants + system_grants,
        "memberships": list(memberships.values()),
        "user_mappings": users,
        "role_mappings": roles,
    }


def compare_inventories(
    source: dict[str, Any], target: dict[str, Any], *,
    source_owner: str, target_owner: str,
) -> dict[str, Any]:
    """Compare complete supported permission inventories with owner remapping.

    Target-only principals and grants are reported as extras, never mutated.
    An incomplete inventory must be rejected by ``discover`` before reaching
    this pure comparison function.
    """
    def norm(value: Any) -> str:
        return str(value).casefold()

    def principal_set(items: list[Any]) -> set[tuple[str, str]]:
        return {(norm(item.name), str(getattr(item, "type", ""))) for item in items}

    def membership_set(items: list[Any]) -> set[tuple[str, str, bool]]:
        return {
            (norm(item.member_name), norm(item.role_name), bool(item.with_admin_option))
            for item in items
        }

    def grant_set(items: list[Any], owner: str) -> set[tuple[Any, ...]]:
        normalized_owner = norm(owner)
        result: set[tuple[Any, ...]] = set()
        for grant in items:
            schema = norm(grant.schema_name)
            if schema == normalized_owner:
                schema = "$owner"
            result.add((
                norm(grant.grantee), norm(grant.object_type), norm(grant.object_name),
                schema, tuple(sorted(norm(part.strip()) for part in grant.privileges.split(","))),
                bool(grant.grant_option),
            ))
        return result

    components = {
        "users": (principal_set(source.get("users", [])), principal_set(target.get("users", []))),
        "roles": (principal_set(source.get("roles", [])), principal_set(target.get("roles", []))),
        "memberships": (membership_set(source.get("memberships", [])), membership_set(target.get("memberships", []))),
        "grants": (grant_set(source.get("grants", []), source_owner), grant_set(target.get("grants", []), target_owner)),
    }
    details: dict[str, Any] = {}
    for name, (expected, actual) in components.items():
        missing = sorted(expected - actual, key=repr)
        extra = sorted(actual - expected, key=repr)
        details[name] = {"match": not missing and not extra, "missing": missing, "target_only": extra}
    return {"match": all(item["match"] for item in details.values()), "components": details}


def _is_oracle_maintained_principal(conn: Any, principal: str) -> bool:
    """Identify internal membership grantees from Oracle catalog metadata."""
    users = _fetchall(
        conn,
        "SELECT username, oracle_maintained, common FROM dba_users WHERE username = :1",
        (principal,),
    )
    roles = _fetchall(
        conn,
        "SELECT role, oracle_maintained, common, authentication_type "
        "FROM dba_roles WHERE role = :1",
        (principal,),
    )
    if users and roles:
        raise RuntimeError(
            f"Oracle role-membership grantee {principal} is both a user and role"
        )
    rows = users or roles
    if not rows:
        return False
    expected_columns = 3 if users else 4
    if len(rows) != 1 or len(rows[0]) != expected_columns:
        raise RuntimeError(
            f"Malformed Oracle-maintained principal metadata for role grantee {principal}"
        )
    return str(rows[0][1]).upper() == "Y"


def _principal_map(name: str, policy: dict[str, Any]) -> str:
    for mapping_key in ("user_mappings", "role_mappings"):
        for source, target in policy[mapping_key].items():
            if str(source).casefold() == str(name).casefold():
                return target
    raise RuntimeError(f"Oracle principal {name} has no explicit target mapping")


def _mapping_value(name: str, mapping: dict[str, str]) -> str | None:
    return next(
        (target for source, target in mapping.items()
         if str(source).casefold() == str(name).casefold()),
        None,
    )


def _permission_key(item: dict[str, Any]) -> str:
    identity = [
        str(item.get("kind", "")).upper(), str(item.get("object_name", "")).upper(),
        str(item.get("column_name", "")).upper(), str(item["privilege"]).upper(),
        str(item["grantee"]).upper(),
    ]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def permission_scope_id(source_identity: dict[str, str], target_identity: dict[str, str]) -> str:
    """Stable, secret-free boundary for one Oracle source/target pairing."""
    fields = ("user", "db_unique_name", "container_name", "service_name")
    identity = {
        "source": {key: str(source_identity[key]).upper() for key in fields},
        "target": {key: str(target_identity[key]).upper() for key in fields},
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _permission_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for grant in plan.get("object_grants", []):
        items.append({**grant, "kind": "COLUMN" if grant["kind"] == "COLUMN" else "OBJECT"})
    for grant in plan.get("system_grants", []):
        items.append({**grant, "kind": "SYSTEM", "object_name": "", "column_name": ""})
    for membership in plan.get("memberships", []):
        items.append({
            "kind": "ROLE", "object_name": membership["role"], "column_name": "",
            "privilege": "MEMBERSHIP", "grantee": membership["member"],
        })
    return items


def _provenance_name(owner: str) -> str:
    validate_oracle_identifier(owner, "permission provenance owner")
    return f"{quote_identifier(owner)}.{quote_identifier(PROVENANCE_TABLE)}"


def _read_provenance(
    conn: Any, owner: str, expected_scope_id: str | None = None,
) -> dict[str, dict[str, str]]:
    exists = _fetchall(conn,
        "SELECT table_name FROM all_tables WHERE owner=:1 AND table_name=:2",
        (owner, PROVENANCE_TABLE))
    if not exists:
        return {}
    rows = _fetchall(conn,
        f"SELECT item_key, kind, object_name, column_name, privilege, grantee, state "
        f"FROM {_provenance_name(owner)}")
    if not rows or not any(str(row[0]) == PROVENANCE_META_KEY and str(row[1]) == "META" for row in rows):
        raise RuntimeError(
            f"Target object {PROVENANCE_TABLE} exists without a valid Oracle permission-provenance marker; "
            "refusing to infer ownership"
        )
    result: dict[str, dict[str, str]] = {}
    marker_scope: str | None = None
    for row in rows:
        if len(row) != 7:
            raise RuntimeError("Malformed Oracle permission-provenance row")
        key, kind = str(row[0]), str(row[1])
        obj, col = row[2], row[3]
        privilege, grantee, state = str(row[4]), str(row[5]), str(row[6])
        if key == PROVENANCE_META_KEY and kind == "META":
            marker_scope = str(obj) if obj is not None else None
            continue
        item = {"kind": kind, "object_name": str(obj or ""), "column_name": str(col or ""),
                "privilege": privilege, "grantee": grantee}
        if _permission_key(item) != key or state not in {
            "ACTIVE", "PENDING_GRANT", "PENDING_REVOKE", "APPROVED_LEGACY"
        }:
            raise RuntimeError("Invalid Oracle permission-provenance key or state; refusing reconciliation")
        result[key] = {**item, "state": state}
    if expected_scope_id is not None and marker_scope != expected_scope_id:
        if marker_scope is None and result:
            raise RuntimeError(
                "Oracle permission provenance belongs to an unscoped legacy migration; "
                "TARGET owner scope cannot be established safely. Refusing cross-migration reconciliation."
            )
        if marker_scope is not None:
            raise RuntimeError(
                "Oracle permission provenance belongs to a different SOURCE/TARGET database and PDB scope; "
                "use a dedicated TARGET owner for this migration"
            )
    return result


def _ensure_provenance(conn: Any, owner: str, scope_id: str | None = None) -> None:
    exists = _fetchall(conn,
        "SELECT table_name FROM all_tables WHERE owner=:1 AND table_name=:2",
        (owner, PROVENANCE_TABLE))
    if exists:
        ledger = _read_provenance(conn, owner, scope_id)
        if scope_id is not None and not ledger:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {_provenance_name(owner)} SET object_name=:1 WHERE item_key=:2 AND kind='META'",
                    (scope_id, PROVENANCE_META_KEY),
                )
            conn.commit()
        return
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {_provenance_name(owner)} ("
            "item_key VARCHAR2(64) PRIMARY KEY, kind VARCHAR2(12) NOT NULL, "
            "object_name VARCHAR2(128), column_name VARCHAR2(128), privilege VARCHAR2(40) NOT NULL, "
            "grantee VARCHAR2(128) NOT NULL, state VARCHAR2(20) NOT NULL)"
        )
        cur.execute(
            f"INSERT INTO {_provenance_name(owner)} "
            "(item_key, kind, object_name, column_name, privilege, grantee, state) "
            "VALUES (:1, 'META', :2, NULL, 'SCHEMA_V1', 'FRAMEWORK', 'ACTIVE')",
            (PROVENANCE_META_KEY, scope_id),
        )
    conn.commit()


def prepare_provenance(
    conn: Any, owner: str, executor: str, scope_id: str | None = None,
) -> list[str]:
    """Create the owner-scoped journal and give the executor only its DML rights."""
    _ensure_provenance(conn, owner, scope_id)
    rows = _fetchall(conn,
        "SELECT grantee, privilege, grantable, common, inherited "
        "FROM all_tab_privs_made WHERE owner=:1 AND table_name=:2 AND grantee=:3",
        (owner, PROVENANCE_TABLE, executor))
    found: set[str] = set()
    for row in rows:
        if len(row) != 5 or any(str(value).upper() != "NO" for value in row[2:]):
            raise RuntimeError("Oracle permission executor has broader or unsupported provenance-table grants")
        found.add(str(row[1]).upper())
    required = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    missing = sorted(required - found)
    if missing:
        with conn.cursor() as cur:
            cur.execute(
                f"GRANT {', '.join(missing)} ON {_provenance_name(owner)} TO {quote_identifier(executor)}"
            )
        conn.commit()
    return [f"GRANT {privilege} ON {PROVENANCE_TABLE} TO {executor}" for privilege in missing]


def _provenance_set(conn: Any, owner: str, item: dict[str, Any], state: str) -> None:
    if not _fetchall(conn,
        "SELECT table_name FROM all_tables WHERE owner=:1 AND table_name=:2",
        (owner, PROVENANCE_TABLE)):
        raise RuntimeError("Oracle permission provenance table was not prepared before permission changes")
    key = _permission_key(item)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_provenance_name(owner)} WHERE item_key=:1", (key,))
        cur.execute(
            f"INSERT INTO {_provenance_name(owner)} "
            "(item_key, kind, object_name, column_name, privilege, grantee, state) "
            "VALUES (:1, :2, :3, :4, :5, :6, :7)",
            (key, item["kind"], item.get("object_name") or None,
             item.get("column_name") or None, item["privilege"], item["grantee"], state),
        )
    conn.commit()


def _provenance_delete(conn: Any, owner: str, item: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_provenance_name(owner)} WHERE item_key=:1",
                    (_permission_key(item),))
    conn.commit()


def record_legacy_adoptions(
    conn: Any, owner: str, items: list[dict[str, Any]], principal_conn: Any | None = None,
) -> list[str]:
    """Record only live, exact permissions after the caller obtained consent."""
    recorded: list[str] = []
    for item in items:
        if item.get("kind") not in {"OBJECT", "COLUMN", "ROLE", "SYSTEM"}:
            raise RuntimeError(f"Unsupported legacy permission candidate: {item}")
        state_conn = principal_conn if item.get("kind") in {"ROLE", "SYSTEM"} else conn
        if not _validate_live_permission_state(state_conn or conn, item, owner):
            raise RuntimeError(
                f"Legacy permission changed or disappeared before approval was recorded: {item}; "
                "rerun the approval command to inspect current TARGET permissions"
            )
        _provenance_set(conn, owner, item, "APPROVED_LEGACY")
        recorded.append(_permission_key(item))
    return recorded


def _adoption_statement(owner: str, item: dict[str, Any]) -> str:
    values = [
        _permission_key(item), "ACTIVE", item["kind"], item.get("object_name", ""),
        item.get("column_name", ""), item["privilege"], item["grantee"],
    ]
    sql_values = ", ".join("'" + str(value).replace("'", "''") + "'" for value in values)
    table = quote_identifier(PROVENANCE_TABLE)
    create = (
        f"CREATE TABLE {table} (item_key VARCHAR2(64) PRIMARY KEY, kind VARCHAR2(12) NOT NULL, "
        "object_name VARCHAR2(128), column_name VARCHAR2(128), privilege VARCHAR2(40) NOT NULL, "
        "grantee VARCHAR2(128) NOT NULL, state VARCHAR2(20) NOT NULL)"
    )
    marker = (
        f"INSERT INTO {table} (item_key, kind, object_name, column_name, privilege, grantee, state) "
        "VALUES ('__SCHEMA_V1__', 'META', NULL, NULL, 'SCHEMA_V1', 'FRAMEWORK', 'ACTIVE')"
    )
    block = (
        "DECLARE v_count PLS_INTEGER; BEGIN "
        f"SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name='{PROVENANCE_TABLE}'; "
        "IF v_count=0 THEN "
        f"EXECUTE IMMEDIATE '{create.replace(chr(39), chr(39) * 2)}'; "
        f"EXECUTE IMMEDIATE '{marker.replace(chr(39), chr(39) * 2)}'; "
        "END IF; END;\n/"
    )
    adoption = (
        f"INSERT INTO {quote_identifier(owner)}.{table} "
        "(item_key, state, kind, object_name, column_name, privilege, grantee) "
        f"VALUES ({sql_values});"
    )
    return block + "\n" + adoption + "\nCOMMIT; -- only after independently confirming framework ownership"


def _build_reconciliation(conn: Any, principal_conn: Any, *, target_owner: str,
                          target_objects: set[str], target_principals: set[str],
                          target_roles: set[str], desired: list[dict[str, Any]],
                          approved_legacy_keys: set[str] | None = None,
                          permit_legacy_candidates: bool = False,
                          scope_id: str | None = None) -> dict[str, Any]:
    ledger = (
        _read_provenance(conn, target_owner)
        if scope_id is None else _read_provenance(conn, target_owner, scope_id)
    )
    current: dict[str, dict[str, Any]] = {}
    owner_rows = _fetchall(conn,
        "SELECT grantee, table_name, grantor, privilege, grantable, common, inherited, type "
        "FROM all_tab_privs_made WHERE owner=:1", (target_owner,))
    for row in owner_rows:
        if len(row) != 8:
            raise RuntimeError("Malformed ALL_TAB_PRIVS_MADE target reconciliation row")
        grantee, obj, grantor, privilege, grantable, common, inherited, obj_type = map(str, row)
        if obj.upper() in target_objects and grantee.upper() in target_principals:
            item = {"kind": "OBJECT", "object_name": obj, "column_name": "",
                    "privilege": privilege.upper(), "grantee": grantee}
            current[_permission_key(item)] = {**item, "grantor": grantor,
                "grantable": grantable, "common": common, "inherited": inherited,
                "object_type": obj_type}
    col_rows = _fetchall(conn,
        "SELECT grantee, table_name, column_name, grantor, privilege, grantable, common, inherited "
        "FROM all_col_privs_made WHERE owner=:1", (target_owner,))
    for row in col_rows:
        if len(row) != 8:
            raise RuntimeError("Malformed ALL_COL_PRIVS_MADE target reconciliation row")
        grantee, obj, column, grantor, privilege, grantable, common, inherited = map(str, row)
        if obj.upper() in target_objects and grantee.upper() in target_principals:
            item = {"kind": "COLUMN", "object_name": obj, "column_name": column,
                    "privilege": privilege.upper(), "grantee": grantee}
            current[_permission_key(item)] = {**item, "grantor": grantor,
                "grantable": grantable, "common": common, "inherited": inherited}
    role_rows = _fetchall(principal_conn,
        "SELECT grantee, granted_role, admin_option, default_role, common, inherited "
        "FROM dba_role_privs")
    for row in role_rows:
        if len(row) != 6:
            raise RuntimeError("Malformed DBA_ROLE_PRIVS target reconciliation row")
        member, role, admin_option, default_role, common, inherited = map(str, row)
        if member.upper() in target_principals and role.upper() in target_roles:
            item = {"kind": "ROLE", "object_name": role, "column_name": "",
                    "privilege": "MEMBERSHIP", "grantee": member}
            current[_permission_key(item)] = {**item, "admin_option": admin_option,
                "default_role": default_role, "common": common, "inherited": inherited}
    sys_rows = _fetchall(principal_conn,
        "SELECT grantee, privilege, admin_option, common, inherited FROM dba_sys_privs")
    for row in sys_rows:
        if len(row) != 5:
            raise RuntimeError("Malformed DBA_SYS_PRIVS target reconciliation row")
        grantee, privilege, admin_option, common, inherited = map(str, row)
        if grantee.upper() in target_principals and privilege.upper() in SYSTEM_PRIVILEGES:
            item = {"kind": "SYSTEM", "object_name": "", "column_name": "",
                    "privilege": privilege.upper(), "grantee": grantee}
            current[_permission_key(item)] = {**item, "admin_option": admin_option,
                "common": common, "inherited": inherited}
    desired_by_key = {_permission_key(item): item for item in desired}
    stale: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for key, actual in current.items():
        if key in desired_by_key:
            if any(str(actual.get(field, "NO")).upper() != "NO" for field in
                   ("grantable", "admin_option", "common", "inherited")):
                raise RuntimeError(f"Existing target permission {actual} has broader or unsupported state")
            if actual["kind"] == "ROLE" and str(actual.get("default_role", "NO")).upper() != "YES":
                raise RuntimeError(f"Existing target role membership {actual} is not DEFAULT ROLE; refusing reconciliation")
            if key not in ledger:
                preserved.append(actual)
            continue
        if actual["kind"] in {"OBJECT", "COLUMN"} and actual.get("grantor", "").upper() != target_owner.upper():
            raise RuntimeError(
                f"Target permission {actual} was issued by a different grantor; "
                "only target-owner-issued permissions can be reconciled"
            )
        tracked = ledger.get(key)
        if tracked is None:
            ambiguous.append(actual)
            continue
        if tracked.get("state") not in {
            "ACTIVE", "PENDING_GRANT", "PENDING_REVOKE", "APPROVED_LEGACY"
        }:
            raise RuntimeError(f"Unsupported provenance state for {actual}")
        if actual["kind"] == "ROLE" and str(actual.get("default_role", "NO")).upper() != "YES":
            raise RuntimeError(f"Tracked target role membership {actual} is not DEFAULT ROLE; refusing reconciliation")
        stale.append(actual)
    approved = set(approved_legacy_keys or ())
    adopted = []
    if approved and not ambiguous:
        raise RuntimeError(
            "Oracle legacy-grant approval contains stale or nonmatching permission keys; "
            "remove them after reviewing the current preflight inventory"
        )
    if ambiguous:
        current_keys = {_permission_key(item) for item in ambiguous}
        if approved - current_keys:
            raise RuntimeError(
                "Oracle legacy-grant approval contains stale or nonmatching permission keys; "
                "remove them after reviewing the current preflight inventory"
            )
        unapproved = [item for item in ambiguous if _permission_key(item) not in approved]
        if unapproved and not permit_legacy_candidates:
            raise LegacyPermissionAdoptionRequired(target_owner, unapproved)
        adopted = list(ambiguous)
        stale.extend(adopted)
    if approved - {_permission_key(item) for item in adopted}:
        raise RuntimeError(
            "Oracle legacy-grant approval contains stale or nonmatching permission keys; "
            "remove them after reviewing the current preflight inventory"
        )
    for item in stale:
        if item["kind"] == "ROLE":
            authority = _fetchall(principal_conn,
                "SELECT admin_option FROM user_role_privs WHERE granted_role=:1", (item["object_name"],))
            if not any(len(row) == 1 and str(row[0]).upper() == "YES" for row in authority):
                raise RuntimeError(
                    f"Oracle permission reconciliation cannot revoke membership in {item['object_name']}: "
                    "executor lacks direct ADMIN OPTION on that role"
                )
        elif item["kind"] == "SYSTEM":
            authority = _fetchall(principal_conn,
                "SELECT admin_option FROM user_sys_privs WHERE privilege=:1", (item["privilege"],))
            if not any(len(row) == 1 and str(row[0]).upper() == "YES" for row in authority):
                raise RuntimeError(
                    f"Oracle permission reconciliation cannot revoke system privilege {item['privilege']}: "
                    "executor lacks direct ADMIN OPTION; GRANT ANY PRIVILEGE is not accepted"
                )
    missing = [item for key, item in desired_by_key.items() if key not in current]
    return {
        "desired": desired, "to_grant": missing, "stale": stale,
        "to_revoke": stale, "preserved_independent": preserved, "ledger": ledger,
        "adopted_legacy": adopted,
    }


def _legacy_adoption_diagnostic(owner: str, items: list[dict[str, Any]]) -> str:
    """Emit one consolidated, exact manifest; names alone never imply ownership."""
    manifest = [{**item, "approval_key": _permission_key(item)} for item in items]
    return (
        "Oracle permission reconciliation found target grants absent from SOURCE with no durable "
        "framework provenance. No permission changes were made. Grantor/current state cannot prove "
        "framework ownership. Review the following exact JSON manifest and independently verify each "
        "entry using trusted audit records or an approved change record; then add only verified approval_key values to "
        "oracle_permission_migration.legacy_grant_adoption.approved_permission_keys and rerun FULL: "
        + json.dumps({"target_owner": owner, "permissions": manifest}, sort_keys=True)
    )


def _ordered_memberships(items: list[dict[str, str]]) -> list[dict[str, str]]:
    edges = {(item["member_source"], item["role_source"]): item for item in items}
    role_members = {item["member_source"] for item in items if item["member_kind"] == "ROLE"}
    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[dict[str, str]] = []

    def visit(member_role: str) -> None:
        if member_role in visiting:
            raise RuntimeError(f"Oracle role membership cycle detected at {member_role}")
        if member_role in visited:
            return
        visiting.add(member_role)
        for (member, role), item in sorted(edges.items()):
            if member == member_role and member in role_members:
                visit(role)
                result.append(item)
        visiting.remove(member_role)
        visited.add(member_role)

    for item in items:
        if item["member_kind"] == "ROLE":
            visit(item["member_source"])
    role_edges = {(item["member_source"], item["role_source"]) for item in result}
    result.extend(item for item in items if (item["member_source"], item["role_source"]) not in role_edges)
    return result


def preflight_target(
    conn: Any, raw_policy: dict[str, Any], inventory: dict[str, Any], *,
    source_owner: str, target_owner: str, target_ssl: bool = False,
    executor_conn: Any | None = None,
    approved_legacy_keys: set[str] | None = None,
    permit_legacy_candidates: bool = False,
    scope_id: str | None = None,
) -> dict[str, Any]:
    policy = normalize_policy(raw_policy)
    principal_conn = executor_conn or conn
    discovered_policy = inventory.get("policy", {})
    users = dict(discovered_policy.get("user_mappings", inventory.get("user_mappings", {})))
    roles = dict(discovered_policy.get("role_mappings", inventory.get("role_mappings", {})))
    if not users and not roles and (inventory.get("grants") or inventory.get("memberships")):
        raise RuntimeError("Oracle permission inventory is missing discovered principal mappings")
    policy.update(
        user_mappings=users,
        role_mappings=roles,
    )
    targets = [name.casefold() for name in users.values()] + [name.casefold() for name in roles.values()]
    if len(targets) != len(set(targets)):
        raise RuntimeError("Oracle same-name source principals have an ambiguous target mapping")
    if target_owner.casefold() in {name.casefold() for name in users.values()}:
        raise RuntimeError(
            "Oracle source application principal maps to target schema owner; "
            "owner identity is managed separately"
        )
    expected_objects = {
        str(name): "TABLE" for name in inventory.get("tables", set())
    }
    expected_objects.update({str(item.name): "VIEW" for item in inventory.get("views", [])})
    expected_objects.update({
        str(item.name): "MATERIALIZED VIEW"
        for item in inventory.get("materialized_views", [])
    })
    if PROVENANCE_TABLE in expected_objects:
        raise RuntimeError(
            f"Source object {PROVENANCE_TABLE} conflicts with Oracle permission provenance metadata"
        )
    resolved_grants: list[dict[str, Any]] = []
    resolved_system: list[dict[str, Any]] = []
    for grant in inventory["grants"]:
        target_grantee = _principal_map(grant.grantee, policy)
        privileges = [part.strip().upper() for part in grant.privileges.split(",")]
        if grant.object_type == "SYSTEM":
            if any(privilege not in policy["approved_system_privileges"] for privilege in privileges):
                raise RuntimeError(f"System grant {grant.privileges} is not explicitly approved")
            resolved_system.extend({"privilege": privilege, "grantee": target_grantee}
                                   for privilege in privileges)
            continue
        if grant.schema_name != source_owner:
            raise RuntimeError(f"Oracle grant on {grant.schema_name}.{grant.object_name} is outside source-owner scope")
        if grant.object_type == "COLUMN":
            table, dot, column = grant.object_name.partition(".")
            if (
                not dot or table not in expected_objects or not column
                or expected_objects[table] not in {"TABLE", "VIEW"}
            ):
                raise RuntimeError(f"Oracle column grant target {grant.object_name} is not a migrated table/view")
            supported = policy["approved_column_privileges"]
            resolved_grants.extend({
                "kind": "COLUMN", "object_name": table, "column_name": column,
                "privilege": privilege, "grantee": target_grantee,
            } for privilege in privileges if privilege in supported)
            if any(privilege not in supported for privilege in privileges):
                raise RuntimeError(f"Column grant {grant.privileges} is not explicitly approved")
        else:
            object_name = grant.object_name
            if object_name not in expected_objects:
                raise RuntimeError(f"Oracle grant target {object_name} is not a source-managed object")
            actual_kind = expected_objects[object_name]
            if grant.object_type not in {actual_kind, "TABLE" if actual_kind == "MATERIALIZED VIEW" else actual_kind}:
                raise RuntimeError(
                    f"Oracle grant type {grant.object_type} does not match source object "
                    f"{object_name} ({actual_kind})"
                )
            if any(privilege not in policy["approved_object_privileges"] for privilege in privileges):
                raise RuntimeError(f"Object grant {grant.privileges} is not explicitly approved")
            resolved_grants.extend({
                "kind": grant.object_type, "object_name": object_name,
                "privilege": privilege, "grantee": target_grantee,
            } for privilege in privileges)

    # Existing target state is inspection-only here. Broader grants are never
    # silently retained as though they matched the approved source policy.
    for item in resolved_grants:
        if item["kind"] == "COLUMN":
            rows = _fetchall(conn,
                "SELECT grantable FROM all_col_privs_made WHERE owner=:1 AND table_name=:2 "
                "AND column_name=:3 AND grantee=:4 AND privilege=:5",
                (target_owner, item["object_name"], item["column_name"],
                 item["grantee"], item["privilege"]))
        else:
            rows = _fetchall(conn,
                "SELECT grantable FROM all_tab_privs_made WHERE owner=:1 AND table_name=:2 "
                "AND grantee=:3 AND privilege=:4",
                (target_owner, item["object_name"], item["grantee"], item["privilege"]))
        if any(str(row[0]).upper() != "NO" for row in rows):
            raise RuntimeError(
                f"Existing target grant on {item['object_name']} to {item['grantee']} "
                "is broader than the approved source permission"
            )

    new_users: list[dict[str, Any]] = []
    new_roles: list[dict[str, Any]] = []
    for source, user in sorted(policy["user_mappings"].items()):
        rows = _fetchall(principal_conn,
            "SELECT username, oracle_maintained, common, account_status "
            "FROM dba_users WHERE username = :1", (user,))
        if rows:
            if len(rows) != 1 or str(rows[0][1]).upper() != "N" or str(rows[0][2]).upper() != "NO":
                raise RuntimeError(f"Mapped target user {user} is not a local application user")
            continue
        role_conflict = _fetchall(principal_conn,
            "SELECT role FROM dba_roles WHERE role = :1", (user,))
        if role_conflict:
            raise RuntimeError(
                f"Mapped target user {user} conflicts with an existing target role"
            )
        if policy["new_user_policy"] != "LOCKED":
            raise RuntimeError(
                f"Mapped target user {user} does not exist; set "
                "new_user_policy: LOCKED to authorize safe locked provisioning"
            )
        new_users.append({
            "source": source, "target": user,
            "account_state": "LOCKED",
        })

    for source, role in sorted(policy["role_mappings"].items()):
        rows = _fetchall(principal_conn,
            "SELECT role, oracle_maintained, common, authentication_type "
            "FROM dba_roles WHERE role = :1", (role,))
        if rows:
            if len(rows) != 1 or str(rows[0][1]).upper() != "N" or str(rows[0][2]).upper() != "NO":
                raise RuntimeError(f"Mapped target role {role} is not a local application role")
            if str(rows[0][3]).upper() != "NONE":
                raise RuntimeError(
                    f"Mapped target role {role} uses {rows[0][3]} authentication; "
                    "only NOT IDENTIFIED roles are supported"
                )
            continue
        user_conflict = _fetchall(principal_conn,
            "SELECT username FROM dba_users WHERE username = :1", (role,))
        if user_conflict:
            raise RuntimeError(
                f"Mapped target role {role} conflicts with an existing target user"
            )
        if not policy["approve_role_creation_admin_option"]:
            raise RuntimeError(
                f"Creating Oracle role {role} gives the creating target owner ADMIN OPTION; "
                "set approve_role_creation_admin_option: true to approve this Oracle behavior"
            )
        new_roles.append({"source": source, "target": role})

    for privilege, principals, description in (
        ("CREATE USER", new_users, "mapped user"),
        ("CREATE ROLE", new_roles, "mapped role"),
    ):
        if not principals:
            continue
        authority = _fetchall(
            principal_conn,
            "SELECT privilege FROM user_sys_privs WHERE privilege = :1",
            (privilege,),
        )
        if not authority:
            raise RuntimeError(
                f"Oracle target owner lacks direct {privilege} privilege required to "
                f"provision a {description}; no broad ANY privilege is accepted"
            )

    resolved_memberships: list[dict[str, str]] = []
    newly_assigned_existing_roles: set[str] = set()
    source_role_map = policy["role_mappings"]
    source_user_map = policy["user_mappings"]
    for membership in inventory["memberships"]:
        source_member, source_role = membership.member_name, membership.role_name
        role_target = _mapping_value(source_role, source_role_map)
        if role_target is None:
            raise RuntimeError(f"Oracle role {source_role} has no explicit target mapping")
        member_target = _mapping_value(source_member, source_role_map)
        if member_target is not None:
            member_kind = "ROLE"
        else:
            member_target = _mapping_value(source_member, source_user_map)
            member_kind = "USER"
        if member_target is None:
            raise RuntimeError(f"Oracle role member {source_member} has no explicit target mapping")
        existing = _fetchall(
            principal_conn,
            "SELECT admin_option, default_role FROM dba_role_privs "
            "WHERE grantee = :1 AND granted_role = :2",
            (member_target, role_target),
        )
        if existing and any(
            str(row[0]).upper() != "NO" or str(row[1]).upper() != "YES"
            for row in existing
        ):
            raise RuntimeError(
                f"Existing target membership {member_target}->{role_target} "
                "has broader or incompatible state"
            )
        elif not existing and role_target not in {item["target"] for item in new_roles}:
            with principal_conn.cursor() as cur:
                cur.execute(
                    "SELECT admin_option FROM user_role_privs WHERE granted_role = :1",
                    (role_target,),
                )
                admin_rows = list(cur.fetchall())
            if not any(str(row[0]).upper() == "YES" for row in admin_rows):
                raise RuntimeError(
                    f"Oracle target owner lacks ADMIN OPTION on mapped role {role_target}; "
                    "membership was not applied"
                )
            newly_assigned_existing_roles.add(role_target)
        resolved_memberships.append({
            "member_source": source_member, "member": member_target,
            "member_kind": member_kind, "role_source": source_role,
            "role": role_target,
        })
    ordered_memberships = _ordered_memberships(resolved_memberships)

    # Reusing an existing role and newly adding members can expose that role's
    # unrelated target-only privileges. Preserve them, but fail before cleanup
    # unless its direct privilege state matches the source-managed plan.
    expected_role_object_privileges = {
        (target_owner, item["object_name"], item["privilege"])
        for item in resolved_grants if item["kind"] != "COLUMN"
        for role in (item["grantee"],) if role in roles.values()
    }
    expected_role_column_privileges = {
        (target_owner, item["object_name"], item["column_name"], item["privilege"])
        for item in resolved_grants if item["kind"] == "COLUMN"
        for role in (item["grantee"],) if role in roles.values()
    }
    expected_role_system_privileges = {
        (item["grantee"], item["privilege"])
        for item in resolved_system if item["grantee"] in roles.values()
    }
    expected_nested_memberships = {
        (item["member"], item["role"])
        for item in ordered_memberships if item["member_kind"] == "ROLE"
    }
    for role in sorted(newly_assigned_existing_roles):
        rows = _fetchall(
            principal_conn,
            "SELECT owner, table_name, privilege, grantable "
            "FROM dba_tab_privs WHERE grantee = :1",
            (role,),
        )
        for row in rows:
            if len(row) != 4 or (str(row[0]), str(row[1]), str(row[2]).upper()) not in expected_role_object_privileges:
                raise RuntimeError(
                    f"Existing target role {role} has unrelated object grants; refusing to add members"
                )
            if str(row[3]).upper() != "NO":
                raise RuntimeError(
                    f"Existing target role {role} has a grant option; refusing to add members"
                )
        rows = _fetchall(
            principal_conn,
            "SELECT owner, table_name, column_name, privilege, grantable "
            "FROM dba_col_privs WHERE grantee = :1",
            (role,),
        )
        for row in rows:
            if len(row) != 5 or (
                str(row[0]), str(row[1]), str(row[2]), str(row[3]).upper()
            ) not in expected_role_column_privileges:
                raise RuntimeError(
                    f"Existing target role {role} has unrelated column grants; refusing to add members"
                )
            if str(row[4]).upper() != "NO":
                raise RuntimeError(
                    f"Existing target role {role} has a column grant option; refusing to add members"
                )
        rows = _fetchall(
            principal_conn,
            "SELECT privilege, admin_option, common, inherited "
            "FROM dba_sys_privs WHERE grantee = :1",
            (role,),
        )
        for row in rows:
            if len(row) != 4:
                raise RuntimeError("Malformed target DBA_SYS_PRIVS row for mapped role")
            if (role, str(row[0]).upper()) not in expected_role_system_privileges:
                raise RuntimeError(
                    f"Existing target role {role} has unrelated system privileges; refusing to add members"
                )
            if any(str(value).upper() != "NO" for value in row[1:]):
                raise RuntimeError(
                    f"Existing target role {role} has an ADMIN, common, or inherited system grant"
                )
        rows = _fetchall(
            principal_conn,
            "SELECT grantee, granted_role, admin_option, default_role, common, inherited "
            "FROM dba_role_privs WHERE grantee = :1",
            (role,),
        )
        for row in rows:
            if len(row) != 6:
                raise RuntimeError("Malformed target DBA_ROLE_PRIVS row for mapped role")
            if (str(row[0]), str(row[1])) not in expected_nested_memberships:
                raise RuntimeError(
                    f"Existing target role {role} has unrelated nested role memberships; refusing to add members"
                )
            if any(str(value).upper() != expected for value, expected in zip(
                row[2:], ("NO", "YES", "NO", "NO")
            )):
                raise RuntimeError(
                    f"Existing target role {role} has an unsupported nested membership state"
                )

    if resolved_system:
        for item in resolved_system:
            rows = _fetchall(principal_conn,
                "SELECT admin_option FROM dba_sys_privs WHERE grantee=:1 AND privilege=:2",
                (item["grantee"], item["privilege"].upper()))
            if any(str(row[0]).upper() != "NO" for row in rows):
                raise RuntimeError(
                    f"Existing target system grant {item['privilege']} to {item['grantee']} "
                    "is broader than approved"
                )
            if not rows:
                authority = _fetchall(principal_conn,
                    "SELECT privilege, admin_option FROM user_sys_privs WHERE privilege = :1",
                    (item["privilege"],))
                if not any(str(row[1]).upper() == "YES" for row in authority):
                    raise RuntimeError(
                        f"Oracle target owner lacks ADMIN OPTION on system privilege {item['privilege']}; "
                        "no broad GRANT ANY PRIVILEGE authority is accepted"
                    )
    permission_items = _permission_items({
        "object_grants": resolved_grants,
        "system_grants": resolved_system,
        "memberships": ordered_memberships,
    })
    will_need_provenance = bool(
        permission_items or permit_legacy_candidates or approved_legacy_keys
        or policy.get("legacy_grant_adoption", {}).get("approved_permission_keys")
    )
    if will_need_provenance and not _fetchall(
        conn, "SELECT table_name FROM all_tables WHERE owner=:1 AND table_name=:2",
        (target_owner, PROVENANCE_TABLE),
    ) and not _fetchall(
        conn, "SELECT privilege FROM session_privs WHERE privilege='CREATE TABLE'"
    ):
        raise RuntimeError(
            "Oracle target owner lacks effective CREATE TABLE authority required for the "
            "durable permission-provenance journal; refusing permission migration before cleanup"
        )
    reconciliation = _build_reconciliation(
        conn, principal_conn, target_owner=target_owner,
        target_objects={name.upper() for name in expected_objects},
        target_principals={name.upper() for name in [*users.values(), *roles.values()]},
        target_roles={name.upper() for name in roles.values()},
        desired=permission_items,
        approved_legacy_keys=(
            set(approved_legacy_keys)
            if approved_legacy_keys is not None
            else set(policy.get("legacy_grant_adoption", {}).get("approved_permission_keys", []))
        ),
        permit_legacy_candidates=permit_legacy_candidates,
        scope_id=scope_id,
    )
    return {
        "policy": policy, "object_grants": resolved_grants,
        "system_grants": resolved_system, "memberships": ordered_memberships,
        "new_users": new_users, "new_roles": new_roles,
        "target_owner": target_owner,
        "permission_reconciliation": reconciliation,
        "scope_id": scope_id,
        "roles": [{"source": name, "target": target} for name, target in sorted(policy["role_mappings"].items())],
    }


def verify_executor_catalog_access(conn: Any) -> list[str]:
    """Probe every required dictionary view and return all inaccessible views."""
    unavailable: list[str] = []
    for view in PERMISSION_CATALOG_VIEWS:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM SYS.{view} WHERE 1 = 0")
                cur.fetchall()
        except Exception:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            unavailable.append(f"SELECT on SYS.{view}")
    return unavailable


def executor_authority_gaps(
    conn: Any, policy: dict[str, Any], inventory: dict[str, Any],
) -> list[str]:
    """Collect direct executor-authority gaps in one pre-cleanup inspection."""
    normalized = normalize_policy(policy)
    discovered = inventory.get("policy", {})
    normalized["user_mappings"] = dict(
        discovered.get("user_mappings", inventory.get("user_mappings", {}))
    )
    normalized["role_mappings"] = dict(
        discovered.get("role_mappings", inventory.get("role_mappings", {}))
    )
    gaps: list[str] = []
    catalog = verify_executor_catalog_access(conn)
    gaps.extend(catalog)
    if catalog:
        return gaps

    users = dict(inventory.get("user_mappings", {}))
    roles = dict(inventory.get("role_mappings", {}))
    try:
        system_rows = _fetchall(
            conn, "SELECT privilege, admin_option FROM user_sys_privs"
        )
        direct_sys = {str(row[0]).upper(): str(row[1]).upper() for row in system_rows}
    except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
        gaps.append(f"read USER_SYS_PRIVS: {exc}")
        direct_sys = {}

    existing_users: set[str] = set()
    existing_roles: set[str] = set()
    for name in users.values():
        try:
            if _fetchall(conn, "SELECT username FROM dba_users WHERE username=:1", (name,)):
                existing_users.add(name)
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"inspect target user {name}: {exc}")
    for name in roles.values():
        try:
            if _fetchall(conn, "SELECT role FROM dba_roles WHERE role=:1", (name,)):
                existing_roles.add(name)
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"inspect target role {name}: {exc}")

    missing_users = set(users.values()) - existing_users
    missing_roles = set(roles.values()) - existing_roles
    if missing_users and "CREATE USER" not in direct_sys:
        gaps.append("direct CREATE USER on the permission executor")
    if missing_roles and "CREATE ROLE" not in direct_sys:
        gaps.append("direct CREATE ROLE on the permission executor")

    existing_membership_roles = {
        str(normalized["role_mappings"].get(item.role_name, item.role_name))
        for item in inventory.get("memberships", [])
        if normalized["role_mappings"].get(item.role_name, item.role_name) in set(roles.values())
        and normalized["role_mappings"].get(item.role_name, item.role_name) in existing_roles
    }
    if existing_membership_roles:
        try:
            role_rows = _fetchall(
                conn, "SELECT granted_role, admin_option FROM user_role_privs"
            )
            admin_roles = {
                str(row[0]) for row in role_rows if str(row[1]).upper() == "YES"
            }
            for role in sorted(existing_membership_roles - admin_roles):
                gaps.append(f"ADMIN OPTION on existing target role {role}")
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"read USER_ROLE_PRIVS for role grant authority: {exc}")

    needs_create_session = False
    for grant in inventory.get("grants", []):
        if grant.object_type != "SYSTEM" or grant.privileges.upper() != "CREATE SESSION":
            continue
        try:
            mapped_grantee = (
                normalized["user_mappings"].get(grant.grantee)
                or normalized["role_mappings"].get(grant.grantee)
            )
            already_granted = mapped_grantee and _fetchall(
                conn,
                "SELECT privilege FROM dba_sys_privs WHERE grantee=:1 AND privilege='CREATE SESSION'",
                (mapped_grantee,),
            )
            needs_create_session = needs_create_session or not already_granted
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            gaps.append(f"inspect target CREATE SESSION state for {grant.grantee}: {exc}")
    if needs_create_session and direct_sys.get("CREATE SESSION") != "YES":
        gaps.append("CREATE SESSION WITH ADMIN OPTION to apply the approved source grant")
    return gaps


def split_grants(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split owner-issued object grants from executor-issued system grants."""
    object_plan = {**plan, "system_grants": [], "reconciliation_kinds": {"OBJECT", "COLUMN"}}
    system_plan = {**plan, "object_grants": [], "reconciliation_kinds": {"SYSTEM"}}
    return object_plan, system_plan


def apply_principals(conn: Any, plan: dict[str, Any]) -> list[str]:
    """Create only explicitly approved missing principals; never alter existing ones."""
    applied: list[str] = []
    for item in plan.get("new_users", []):
        username = str(item["target"])
        if str(item.get("account_state", "LOCKED")).upper() != "LOCKED":
            raise RuntimeError(
                f"Oracle automatic user provisioning only supports locked accounts: {username}"
            )
        statement = (
            f"CREATE USER {quote_identifier(username)} IDENTIFIED EXTERNALLY ACCOUNT LOCK"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                f"Oracle target user provisioning failed for {username}: {exc}; "
                f"users created earlier in this run: {', '.join(applied) or 'none'}"
            ) from None
        applied.append(f"USER {username} (LOCKED)")

    for item in plan.get("new_roles", []):
        role = str(item["target"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE ROLE {quote_identifier(role)} NOT IDENTIFIED")
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                f"Oracle target role provisioning failed for {role}: {exc}; "
                f"principals created earlier in this run: {', '.join(applied) or 'none'}"
            ) from None
        try:
            authority = _fetchall(
                conn,
                "SELECT admin_option FROM user_role_privs WHERE granted_role=:1",
                (role,),
            )
            if not any(str(row[0]).upper() == "YES" for row in authority):
                raise RuntimeError("creator ADMIN OPTION is not visible after CREATE ROLE")
        except Exception as exc:
            raise RuntimeError(
                f"Oracle role {role} was created, but its creator ADMIN OPTION could not be verified; "
                "dependent memberships were not applied."
            ) from exc
        applied.append(f"ROLE {role} (creator ADMIN OPTION approved)")
    return applied


def _permission_exists(conn: Any, item: dict[str, Any], target_owner: str) -> bool:
    if item["kind"] == "OBJECT":
        rows = _fetchall(conn,
            "SELECT grantable FROM all_tab_privs_made WHERE owner=:1 AND table_name=:2 "
            "AND grantee=:3 AND privilege=:4",
            (target_owner, item["object_name"], item["grantee"], item["privilege"]))
    elif item["kind"] == "COLUMN":
        rows = _fetchall(conn,
            "SELECT grantable FROM all_col_privs_made WHERE owner=:1 AND table_name=:2 "
            "AND column_name=:3 AND grantee=:4 AND privilege=:5",
            (target_owner, item["object_name"], item["column_name"], item["grantee"], item["privilege"]))
    elif item["kind"] == "SYSTEM":
        rows = _fetchall(conn,
            "SELECT admin_option FROM dba_sys_privs WHERE grantee=:1 AND privilege=:2",
            (item["grantee"], item["privilege"]))
    else:
        rows = _fetchall(conn,
            "SELECT admin_option, default_role FROM dba_role_privs WHERE grantee=:1 AND granted_role=:2",
            (item["grantee"], item["object_name"]))
    return bool(rows)


def _validate_live_permission_state(conn: Any, item: dict[str, Any], target_owner: str) -> bool:
    """Recheck authority-relevant state immediately before marking or revoking a permission."""
    if item["kind"] == "OBJECT":
        rows = _fetchall(conn,
            "SELECT grantee, table_name, grantor, privilege, grantable, common, inherited, type "
            "FROM all_tab_privs_made WHERE owner=:1 AND table_name=:2 AND grantee=:3 AND privilege=:4",
            (target_owner, item["object_name"], item["grantee"], item["privilege"]))
        if not rows:
            return False
        if any(len(row) != 8 or str(row[2]).upper() != target_owner.upper()
               or any(str(row[index]).upper() != "NO" for index in (4, 5, 6)) for row in rows):
            raise RuntimeError(f"Oracle target object permission changed to an unsupported state: {item}")
    elif item["kind"] == "COLUMN":
        rows = _fetchall(conn,
            "SELECT grantee, table_name, column_name, grantor, privilege, grantable, common, inherited "
            "FROM all_col_privs_made WHERE owner=:1 AND table_name=:2 AND column_name=:3 "
            "AND grantee=:4 AND privilege=:5",
            (target_owner, item["object_name"], item["column_name"], item["grantee"], item["privilege"]))
        if not rows:
            return False
        if any(len(row) != 8 or str(row[3]).upper() != target_owner.upper()
               or any(str(row[index]).upper() != "NO" for index in (5, 6, 7)) for row in rows):
            raise RuntimeError(f"Oracle target column permission changed to an unsupported state: {item}")
    elif item["kind"] == "SYSTEM":
        rows = _fetchall(conn,
            "SELECT admin_option, common, inherited FROM dba_sys_privs WHERE grantee=:1 AND privilege=:2",
            (item["grantee"], item["privilege"]))
        if not rows:
            return False
        if any(len(row) != 3 or any(str(value).upper() != "NO" for value in row) for row in rows):
            raise RuntimeError(f"Oracle target system permission changed to an unsupported state: {item}")
    else:
        rows = _fetchall(conn,
            "SELECT admin_option, default_role, common, inherited FROM dba_role_privs "
            "WHERE grantee=:1 AND granted_role=:2", (item["grantee"], item["object_name"]))
        if not rows:
            return False
        if any(len(row) != 4 or tuple(str(value).upper() for value in row) != ("NO", "YES", "NO", "NO")
               for row in rows):
            raise RuntimeError(f"Oracle target role membership changed to an unsupported state: {item}")
    return True


def _permission_ddl(item: dict[str, Any], target_owner: str, *, revoke: bool = False) -> str:
    verb = "REVOKE" if revoke else "GRANT"
    grantee = quote_identifier(item["grantee"])
    if item["kind"] == "OBJECT":
        return f"{verb} {item['privilege']} ON {quote_identifier(target_owner)}.{quote_identifier(item['object_name'])} {('FROM' if revoke else 'TO')} {grantee}"
    if item["kind"] == "COLUMN":
        return (f"{verb} {item['privilege']} ({quote_identifier(item['column_name'])}) ON "
                f"{quote_identifier(target_owner)}.{quote_identifier(item['object_name'])} "
                f"{('FROM' if revoke else 'TO')} {grantee}")
    if item["kind"] == "SYSTEM":
        return f"{verb} {item['privilege']} {('FROM' if revoke else 'TO')} {grantee}"
    return f"{verb} {quote_identifier(item['object_name'])} {('FROM' if revoke else 'TO')} {grantee}"


def _apply_tracked_permissions(conn: Any, plan: dict[str, Any], target_owner: str,
                               kinds: set[str]) -> list[str]:
    reconciliation = plan["permission_reconciliation"]
    applied: list[str] = []
    ledger = reconciliation.get("ledger", {})
    for item in reconciliation.get("adopted_legacy", []):
        if item.get("kind") in kinds:
            if not _validate_live_permission_state(conn, item, target_owner):
                raise RuntimeError(
                    f"Oracle explicitly approved legacy permission changed or disappeared before adoption: {item}; "
                    "rerun FULL to generate a current preflight manifest"
                )
            _provenance_set(conn, target_owner, item, "ACTIVE")
            ledger[_permission_key(item)] = {**item, "state": "ACTIVE"}
            applied.append(f"ADOPTED explicitly approved legacy {item['kind']} {item['privilege']} for {item['grantee']}")
    for key, item in ledger.items():
        if (
            item.get("kind") in kinds and item.get("state") in {
            "PENDING_REVOKE", "ACTIVE", "PENDING_GRANT", "APPROVED_LEGACY"
            }
            and key not in {_permission_key(x) for x in reconciliation["desired"]}
            and not _permission_exists(conn, item, target_owner)
        ):
            _provenance_delete(conn, target_owner, item)
    desired_items = [item for item in reconciliation["desired"] if item["kind"] in kinds]
    for item in desired_items:
        key = _permission_key(item)
        exists = _validate_live_permission_state(conn, item, target_owner)
        tracked = ledger.get(key)
        if exists:
            if tracked is not None:
                _provenance_set(conn, target_owner, item, "ACTIVE")
            else:
                applied.append(f"PRESERVED independent {item['kind']} {item['privilege']} for {item['grantee']}")
            continue
        _provenance_set(conn, target_owner, item, "PENDING_GRANT")
        try:
            with conn.cursor() as cur:
                cur.execute(_permission_ddl(item, target_owner))
            conn.commit()
            if not _permission_exists(conn, item, target_owner):
                raise RuntimeError("Oracle did not expose the granted permission in its catalog")
            _provenance_set(conn, target_owner, item, "ACTIVE")
            applied.append(f"GRANT {item['privilege']} {item['kind']} TO {item['grantee']}")
        except Exception as exc:  # noqa: BLE001 - driver and verification failures must stop memberships
            raise RuntimeError(
                f"Oracle permission grant failed for {item}; earlier changes: {', '.join(applied) or 'none'}; {exc}"
            ) from None
    for item in reconciliation["stale"]:
        if item["kind"] not in kinds:
            continue
        key = _permission_key(item)
        if key not in ledger:
            raise RuntimeError(f"Oracle refuses to revoke permission without durable provenance: {item}")
        if not _validate_live_permission_state(conn, item, target_owner):
            _provenance_delete(conn, target_owner, item)
            continue
        _provenance_set(conn, target_owner, item, "PENDING_REVOKE")
        try:
            with conn.cursor() as cur:
                cur.execute(_permission_ddl(item, target_owner, revoke=True))
            conn.commit()
            if _permission_exists(conn, item, target_owner):
                raise RuntimeError("Oracle still reports the permission after REVOKE")
            _provenance_delete(conn, target_owner, item)
            applied.append(f"REVOKE {item['privilege']} {item['kind']} FROM {item['grantee']}")
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                f"Oracle permission revoke failed for {item}; earlier changes: {', '.join(applied) or 'none'}; {exc}"
            ) from None
    return applied


def apply_memberships(conn: Any, plan: dict[str, Any]) -> list[str]:
    if "permission_reconciliation" in plan:
        return _apply_tracked_permissions(conn, plan, str(plan["target_owner"]), {"ROLE"})
    applied: list[str] = []
    for item in plan["memberships"]:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT admin_option FROM user_role_privs WHERE granted_role = :1",
                    (item["role"],),
                )
                authority_rows = list(cur.fetchall())
                if not any(str(row[0]).upper() == "YES" for row in authority_rows):
                    raise RuntimeError(
                        f"Oracle permission executor lacks verified ADMIN OPTION on role {item['role']}"
                    )
                cur.execute(
                    "SELECT admin_option, default_role FROM dba_role_privs "
                    "WHERE grantee = :1 AND granted_role = :2",
                    (item["member"], item["role"]),
                )
                existing = cur.fetchone()
                if existing is None:
                    cur.execute(
                        f"GRANT {quote_identifier(item['role'])} TO {quote_identifier(item['member'])}"
                    )
                    conn.commit()
                elif str(existing[0]).upper() != "NO" or str(existing[1]).upper() != "YES":
                    raise RuntimeError(
                        f"Existing target membership {item['member']}->{item['role']} has broader or incompatible state"
                    )
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                f"Oracle role membership {item['member']}->{item['role']} failed; "
                f"memberships applied earlier in this run: {', '.join(applied) or 'none'}; {exc}"
            ) from None
        applied.append(f"{item['role']} TO {item['member']}")
    return applied


def apply_grants(conn: Any, plan: dict[str, Any], target_owner: str) -> list[str]:
    if "permission_reconciliation" in plan:
        kinds = set(plan.get("reconciliation_kinds", {"OBJECT", "COLUMN"}))
        return _apply_tracked_permissions(conn, plan, target_owner, kinds)
    applied: list[str] = []
    owner = quote_identifier(target_owner)
    for item in plan["object_grants"]:
        privilege, grantee = item["privilege"], quote_identifier(item["grantee"])
        object_name = quote_identifier(item["object_name"])
        if item["kind"] == "COLUMN":
            target = f"{owner}.{object_name}.{quote_identifier(item['column_name'])}"
            check = (
                (
                    "SELECT grantable FROM all_col_privs_made WHERE owner=:1 AND table_name=:2 "
                    "AND column_name=:3 AND grantee=:4 AND privilege=:5"
                ),
                (target_owner, item["object_name"], item["column_name"], item["grantee"], privilege),
            )
            statement = f"GRANT {privilege} ({quote_identifier(item['column_name'])}) ON {owner}.{object_name} TO {grantee}"
        else:
            target = f"{owner}.{object_name}"
            check = (
                (
                    "SELECT grantable FROM all_tab_privs_made WHERE owner=:1 AND table_name=:2 "
                    "AND grantee=:3 AND privilege=:4"
                ),
                (target_owner, item["object_name"], item["grantee"], privilege),
            )
            statement = f"GRANT {privilege} ON {target} TO {grantee}"
        try:
            with conn.cursor() as cur:
                cur.execute(check[0], check[1])
                existing = cur.fetchone()
                if existing is not None:
                    if str(existing[0]).upper() != "NO":
                        raise RuntimeError(f"Existing target grant on {item['object_name']} is broader than approved")
                else:
                    cur.execute(statement)
                    conn.commit()
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                f"Oracle permission grant {privilege} on {target} to {item['grantee']} failed; "
                f"grants applied earlier in this run: {', '.join(applied) or 'none'}; {exc}"
            ) from None
        applied.append(f"{privilege} ON {target} TO {item['grantee']}")

    for item in plan["system_grants"]:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT admin_option FROM dba_sys_privs WHERE grantee=:1 AND privilege=:2",
                    (item["grantee"], item["privilege"].upper()),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if str(existing[0]).upper() != "NO":
                        raise RuntimeError(
                            f"Existing system grant {item['privilege']} to {item['grantee']} is broader than approved"
                        )
                else:
                    cur.execute(
                        f"GRANT {item['privilege']} TO {quote_identifier(item['grantee'])}"
                    )
                    conn.commit()
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                f"Oracle system grant {item['privilege']} to {item['grantee']} failed; "
                f"system grants applied earlier in this run: {', '.join(applied) or 'none'}; {exc}"
            ) from None
        applied.append(f"{item['privilege']} TO {item['grantee']}")
    return applied


def verify(conn: Any, plan: dict[str, Any], *, principal_conn: Any | None = None) -> None:
    principal_conn = principal_conn or conn
    for source, target in plan["policy"]["user_mappings"].items():
        rows = _fetchall(principal_conn,
            "SELECT username, account_status FROM dba_users "
            "WHERE username=:1 AND oracle_maintained='N' AND common='NO'",
            (target,))
        if len(rows) != 1:
            raise RuntimeError(f"Oracle permission verification cannot find mapped target user {target} for {source}")
    for item in plan.get("new_users", []):
        rows = _fetchall(principal_conn,
            "SELECT account_status FROM dba_users WHERE username=:1 "
            "AND oracle_maintained='N' AND common='NO'",
            (item["target"],))
        status = str(rows[0][0]).upper() if len(rows) == 1 else "MISSING"
        expected = str(item["account_state"]).upper()
        if (expected == "LOCKED" and not status.startswith("LOCKED")) or (
            expected == "UNLOCKED" and status != "OPEN"
        ):
            raise RuntimeError(
                f"Oracle provisioned user {item['target']} status is {status}; expected {expected}"
            )
    for source, target in plan["policy"]["role_mappings"].items():
        rows = _fetchall(principal_conn,
            "SELECT role, authentication_type FROM dba_roles WHERE role=:1 "
            "AND oracle_maintained='N' AND common='NO'",
            (target,))
        if len(rows) != 1 or str(rows[0][1]).upper() != "NONE":
            raise RuntimeError(f"Oracle permission verification cannot find mapped target role {target} for {source}")
    for item in plan["object_grants"]:
        if item["kind"] == "COLUMN":
            query = (
                (
                    "SELECT grantable FROM all_col_privs_made WHERE owner=:1 AND table_name=:2 "
                    "AND column_name=:3 AND grantee=:4 AND privilege=:5"
                ),
                (plan["target_owner"], item["object_name"], item["column_name"], item["grantee"], item["privilege"]),
            )
        else:
            query = (
                (
                    "SELECT grantable FROM all_tab_privs_made WHERE owner=:1 AND table_name=:2 "
                    "AND grantee=:3 AND privilege=:4"
                ),
                (plan["target_owner"], item["object_name"], item["grantee"], item["privilege"]),
            )
        rows = _fetchall(conn, query[0], query[1])
        if not rows or any(str(row[0]).upper() != "NO" for row in rows):
            raise RuntimeError(f"Oracle target verification failed for grant {item}")
    for item in plan["system_grants"]:
        rows = _fetchall(principal_conn,
            "SELECT admin_option FROM dba_sys_privs WHERE grantee=:1 AND privilege=:2",
            (item["grantee"], item["privilege"].upper()))
        if not rows or any(str(row[0]).upper() != "NO" for row in rows):
            raise RuntimeError(f"Oracle target verification failed for system privilege {item}")
    for item in plan["memberships"]:
        rows = _fetchall(principal_conn,
            "SELECT admin_option, default_role FROM dba_role_privs WHERE grantee=:1 AND granted_role=:2",
            (item["member"], item["role"]))
        if not rows or any(str(row[0]).upper() != "NO" or str(row[1]).upper() != "YES" for row in rows):
            raise RuntimeError(f"Oracle target verification failed for membership {item}")
    reconciliation = plan.get("permission_reconciliation")
    if reconciliation:
        for item in reconciliation.get("stale", []):
            permission_conn = conn if item["kind"] in {"OBJECT", "COLUMN"} else principal_conn
            if _permission_exists(permission_conn, item, str(plan["target_owner"])):
                raise RuntimeError(f"Oracle target verification found stale framework-managed permission {item}")
