"""MSSQL Security object implementation.

Single logical home for Security-specific behaviour: grant discovery,
user/role/membership discovery, and target-side role/user/membership/grant
creation.

Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported, which
avoids circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    GrantDef,
    RoleDef,
    RoleMembershipDef,
    UserDef,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _MSSQL_FIXED_DB_ROLES,
    _resolve_mssql_schemas,
)
from core.audit_logger import audit_log


# ---------------------------------------------------------------------------
# 1. SOURCE-SIDE SECURITY OPERATIONS
#    Called/delegated by MSSQLSourceConnector methods.
#    Discovery of grants, users, roles, and role memberships.
# ---------------------------------------------------------------------------

def list_grants(conn: Any, config: dict[str, Any]) -> list[GrantDef]:
    """Discover database permissions (schema, table, column, database level).

    Replaces ``MSSQLSourceConnector.list_grants``.

    Queries sys.database_permissions, filtering to grants on objects
    within the configured schemas (or all non-system schemas), excluding
    system principals (public, dbo, fixed database roles).
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[GrantDef] = []

    _fixed_roles_sql = ", ".join(f"'{r}'" for r in _MSSQL_FIXED_DB_ROLES)

    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                f"SELECT dp.permission_name, dp.class_desc, "
                f"dp.major_id, dp.minor_id, "
                f"grantee.name AS grantee_name, "
                f"obj.name AS object_name, "
                f"col.name AS column_name, "
                f"sch.name AS schema_name, "
                f"db.name AS database_name "
                f"FROM sys.database_permissions dp "
                f"JOIN sys.database_principals grantee "
                f"  ON dp.grantee_principal_id = grantee.principal_id "
                f"LEFT JOIN sys.objects obj "
                f"  ON dp.major_id = obj.object_id "
                f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                f"LEFT JOIN sys.columns col "
                f"  ON dp.major_id = col.object_id "
                f"  AND dp.minor_id = col.column_id "
                f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                f"LEFT JOIN sys.schemas sch "
                f"  ON (dp.class_desc = 'OBJECT_OR_COLUMN' "
                f"      AND obj.schema_id = sch.schema_id) "
                f"  OR (dp.class_desc = 'SCHEMA' "
                f"      AND dp.major_id = sch.schema_id) "
                f"LEFT JOIN sys.databases db "
                f"  ON dp.major_id = db.database_id "
                f"  AND dp.class_desc = 'DATABASE' "
                f"WHERE dp.state = 'G' "
                f"  AND grantee.name NOT IN ({_fixed_roles_sql}) "
                f"  AND (dp.class_desc = 'DATABASE' "
                f"       OR sch.name IN ({placeholders})) "
                f"ORDER BY dp.class_desc, grantee.name, "
                f"ISNULL(sch.name, db.name), "
                f"ISNULL(obj.name, sch.name), col.name",
                list(schemas),
            )
        else:
            cur.execute(
                f"SELECT dp.permission_name, dp.class_desc, "
                f"dp.major_id, dp.minor_id, "
                f"grantee.name AS grantee_name, "
                f"obj.name AS object_name, "
                f"col.name AS column_name, "
                f"sch.name AS schema_name, "
                f"db.name AS database_name "
                f"FROM sys.database_permissions dp "
                f"JOIN sys.database_principals grantee "
                f"  ON dp.grantee_principal_id = grantee.principal_id "
                f"LEFT JOIN sys.objects obj "
                f"  ON dp.major_id = obj.object_id "
                f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                f"LEFT JOIN sys.columns col "
                f"  ON dp.major_id = col.object_id "
                f"  AND dp.minor_id = col.column_id "
                f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                f"LEFT JOIN sys.schemas sch "
                f"  ON (dp.class_desc = 'OBJECT_OR_COLUMN' "
                f"      AND obj.schema_id = sch.schema_id) "
                f"  OR (dp.class_desc = 'SCHEMA' "
                f"      AND dp.major_id = sch.schema_id) "
                f"LEFT JOIN sys.databases db "
                f"  ON dp.major_id = db.database_id "
                f"  AND dp.class_desc = 'DATABASE' "
                f"WHERE dp.state = 'G' "
                f"  AND grantee.name NOT IN ({_fixed_roles_sql}) "
                f"  AND (dp.class_desc = 'DATABASE' "
                f"       OR sch.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')) "
                f"ORDER BY dp.class_desc, grantee.name, "
                f"ISNULL(sch.name, db.name), "
                f"ISNULL(obj.name, sch.name), col.name"
            )
        rows = cur.fetchall()

    groups: dict = {}
    for row in rows:
        (
            permission_name,
            class_desc,
            major_id,
            minor_id,
            grantee_name,
            object_name,
            column_name,
            schema_name,
            database_name,
        ) = row

        if class_desc == "DATABASE":
            obj_type = "DATABASE"
            obj_name = database_name or ""
            sch = ""
        elif class_desc == "SCHEMA":
            obj_type = "SCHEMA"
            obj_name = schema_name or ""
            sch = schema_name or ""
        elif class_desc == "OBJECT_OR_COLUMN":
            if minor_id and minor_id > 0:
                obj_type = "COLUMN"
                obj_name = f"{object_name}.{column_name}"
                sch = schema_name or ""
            else:
                obj_type = "TABLE"
                obj_name = object_name or ""
                sch = schema_name or ""
        else:
            continue

        key = (grantee_name, obj_type, obj_name, sch)
        if key not in groups:
            groups[key] = {
                "privileges": [],
                "object_type": obj_type,
                "object_name": obj_name,
                "schema_name": sch,
                "grantee": grantee_name,
            }
        groups[key]["privileges"].append(permission_name)

    for info in groups.values():
        results.append(
            GrantDef(
                privileges=", ".join(sorted(set(info["privileges"]))),
                object_type=info["object_type"],
                object_name=info["object_name"],
                grantee=info["grantee"],
                schema_name=info["schema_name"],
            )
        )
    return results


def list_users(conn: Any) -> list[UserDef]:
    """Discover database users (excluding system principals).

    Replaces ``MSSQLSourceConnector.list_users``.

    Returns user-defined database users with type 'S' (SQL user) or
    'U' (Windows user), excluding system principals (dbo, guest,
    INFORMATION_SCHEMA, sys).
    """
    results: list[UserDef] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, type FROM sys.database_principals "
            "WHERE type IN ('S', 'U') "
            "AND name NOT IN ('dbo', 'guest', 'INFORMATION_SCHEMA', 'sys') "
            "ORDER BY name"
        )
        for row in cur.fetchall():
            results.append(UserDef(name=row[0], type=row[1]))
    return results


def list_roles(conn: Any) -> list[RoleDef]:
    """Discover database roles (excluding fixed/system roles).

    Replaces ``MSSQLSourceConnector.list_roles``.

    Returns user-defined database roles with type 'R' (database role)
    or 'C' (application role), excluding fixed system roles.
    """
    _fixed_roles_sql = ", ".join(f"'{r}'" for r in _MSSQL_FIXED_DB_ROLES)
    results: list[RoleDef] = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT name, type FROM sys.database_principals "
            f"WHERE type IN ('R', 'C') "
            f"AND name NOT IN ({_fixed_roles_sql}) "
            f"ORDER BY name"
        )
        for row in cur.fetchall():
            results.append(RoleDef(name=row[0], type=row[1]))
    return results


def list_role_memberships(conn: Any) -> list[RoleMembershipDef]:
    """Discover database role memberships (excluding system principals).

    Replaces ``MSSQLSourceConnector.list_role_memberships``.

    Returns mappings of member_principal -> role_principal, excluding
    memberships involving fixed/system principals (public, dbo, db_*).
    """
    _fixed_roles_sql = ", ".join(f"'{r}'" for r in _MSSQL_FIXED_DB_ROLES)
    results: list[RoleMembershipDef] = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT m.name AS member_name, r.name AS role_name "
            f"FROM sys.database_role_members drm "
            f"JOIN sys.database_principals m "
            f"  ON drm.member_principal_id = m.principal_id "
            f"JOIN sys.database_principals r "
            f"  ON drm.role_principal_id = r.principal_id "
            f"WHERE m.name NOT IN ({_fixed_roles_sql}) "
            f"  AND r.name NOT IN ({_fixed_roles_sql}) "
            f"ORDER BY r.name, m.name"
        )
        for row in cur.fetchall():
            results.append(RoleMembershipDef(
                member_name=row[0], role_name=row[1]
            ))
    return results


# ---------------------------------------------------------------------------
# 2. TARGET-SIDE SECURITY OPERATIONS
#    Called/delegated by MSSQLTargetConnector methods.
#    Role/user/membership creation and GRANT application.
# ---------------------------------------------------------------------------

def create_role_if_not_exists(conn: Any, role_name: str) -> None:
    """Create a database role on the target if it does not already exist.

    Replaces ``MSSQLTargetConnector.create_role_if_not_exists``.

    Idempotent: if any database principal with that name already
    exists (role or user), creation is skipped.
    """
    validate_identifier(role_name, "role")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sys.database_principals WHERE name = ?",
            (role_name,),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_role", status="exists",
                details={"role": role_name},
            )
            return
        try:
            cur.execute(f"CREATE ROLE [{role_name}]")
            conn.commit()
            audit_log(
                phase="create_role", status="created",
                details={"role": role_name},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_role", status="failed",
                details={"role": role_name, "reason": str(exc)},
            )
            raise


def create_user_if_not_exists(conn: Any, user_name: str) -> None:
    """Create a database user on the target if it does not already exist.

    Replaces ``MSSQLTargetConnector.create_user_if_not_exists``.

    Does NOT create server-level logins or migrate passwords.
    If a login with the same name exists on the server, the user is
    mapped to it; otherwise a contained user is created.
    """
    validate_identifier(user_name, "user")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sys.database_principals WHERE name = ?",
            (user_name,),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_user", status="exists",
                details={"user": user_name},
            )
            return
        cur.execute(
            "SELECT 1 FROM sys.server_principals WHERE name = ?",
            (user_name,),
        )
        login_exists = cur.fetchone() is not None
        try:
            if login_exists:
                cur.execute(f"CREATE USER [{user_name}] FOR LOGIN [{user_name}]")
            else:
                cur.execute(f"CREATE USER [{user_name}] WITHOUT LOGIN")
            conn.commit()
            audit_log(
                phase="create_user", status="created",
                details={"user": user_name},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_user", status="failed",
                details={"user": user_name, "reason": str(exc)},
            )
            raise


def create_role_membership(
    conn: Any, member_name: str, role_name: str
) -> None:
    """Add a database principal to a database role (idempotent).

    Replaces ``MSSQLTargetConnector.create_role_membership``.
    """
    validate_identifier(member_name, "member")
    validate_identifier(role_name, "role")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sys.database_role_members drm "
            "JOIN sys.database_principals m "
            "  ON drm.member_principal_id = m.principal_id "
            "JOIN sys.database_principals r "
            "  ON drm.role_principal_id = r.principal_id "
            "WHERE m.name = ? AND r.name = ?",
            (member_name, role_name),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_role_membership", status="exists",
                details={"member": member_name, "role": role_name},
            )
            return
        try:
            cur.execute(
                f"ALTER ROLE [{role_name}] ADD MEMBER [{member_name}]"
            )
            conn.commit()
            audit_log(
                phase="create_role_membership", status="created",
                details={"member": member_name, "role": role_name},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_role_membership", status="failed",
                details={"member": member_name, "role": role_name, "reason": str(exc)},
            )
            raise


def apply_grant(
    conn: Any, config: dict[str, Any], grant: GrantDef
) -> None:
    """Apply a GRANT statement using MSSQL-native syntax.

    Replaces ``MSSQLTargetConnector.apply_grant``.

    Translates the cross-engine GrantDef into the appropriate
    MSSQL GRANT form based on object_type:
      - DATABASE: GRANT <privs> ON DATABASE::[db] TO [grantee]
      - SCHEMA:   GRANT <privs> ON SCHEMA::[schema] TO [grantee]
      - TABLE:    GRANT <privs> ON [schema].[table] TO [grantee]
      - COLUMN:   GRANT <privs> (<column>) ON [schema].[table] TO [grantee]
    """
    grantee_q = f"[{grant.grantee}]"
    privileges = grant.privileges
    schema_q = f"[{grant.schema_name or 'dbo'}]"
    with conn.cursor() as cur:
        try:
            if grant.object_type == "DATABASE":
                db_name = grant.object_name or config.get("database", "master")
                cur.execute(
                    f"GRANT {privileges} ON DATABASE::[{db_name}] TO {grantee_q}"
                )
            elif grant.object_type == "SCHEMA":
                cur.execute(
                    f"GRANT {privileges} ON SCHEMA::{schema_q} TO {grantee_q}"
                )
            elif grant.object_type == "COLUMN":
                parts = grant.object_name.split(".")
                table_q = f"[{parts[0]}]"
                column_q = f"[{parts[1]}]"
                cur.execute(
                    f"GRANT {privileges} ({column_q}) "
                    f"ON {schema_q}.{table_q} TO {grantee_q}"
                )
            else:
                object_q = f"[{grant.object_name}]"
                cur.execute(
                    f"GRANT {privileges} ON {schema_q}.{object_q} TO {grantee_q}"
                )
            conn.commit()
            audit_log(
                phase="apply_grant", status="applied",
                details={"object": grant.object_name,
                         "grantee": grant.grantee,
                         "privileges": privileges},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="apply_grant", status="failed",
                details={"object": grant.object_name,
                         "grantee": grant.grantee, "reason": str(exc)},
            )
            raise
