from __future__ import annotations

from core.connectors.base import validate_identifier


def create_role_if_missing(conn, role_name: str) -> str:
    validate_identifier(role_name, "role")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM SYSCAT.ROLES WHERE ROLENAME=?", (role_name,))
        if cur.fetchone():
            return "already_present"
        cur.execute(f"CREATE ROLE {role_name}")
    return "created"


def grant_role_membership(conn, role_name: str, grantee: str, grantee_type: str, admin: bool = False) -> str:
    validate_identifier(role_name, "role")
    validate_identifier(grantee, "principal")
    admin_clause = " WITH ADMIN OPTION" if admin else ""
    sql = f"GRANT ROLE {role_name} TO {grantee}{admin_clause}"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "granted"


def grant_role_privilege(conn, role_name: str, privilege: str, object_name: str, object_type: str = "TABLE") -> str:
    validate_identifier(role_name, "role")
    privilege = privilege.strip().upper()
    allowed = {"SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "USAGE", "CREATEIN"}
    if privilege not in allowed:
        raise ValueError(f"Unsupported privilege: {privilege}")
    with conn.cursor() as cur:
        if object_type.upper() == "TABLE":
            cur.execute(f"GRANT {privilege} ON TABLE {object_name} TO ROLE {role_name}")
        elif object_type.upper() == "ROUTINE":
            cur.execute(f"GRANT EXECUTE ON {object_name} TO ROLE {role_name}")
        else:
            cur.execute(f"GRANT {privilege} ON {object_type} {object_name} TO ROLE {role_name}")
    return "granted"
