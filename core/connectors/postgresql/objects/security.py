"""PostgreSQL security object implementation.

Owns the three PostgreSQL authorization surfaces as one object family:

  * row-level security (RLS) policies, which gate rows inside a table,
  * object grants (privileges), and
  * role creation, which grants are applied against.

This module depends on ``sequence.py`` for exactly one thing:
``owned_sequences()``. A role holding INSERT on a table also needs USAGE on
that table's owned (SERIAL/IDENTITY) sequence, because the sequence backs the
column being inserted into. That ownership lookup lives in the sequence object
and is consumed here — the dependency is one-way.

PostgreSQL users and role memberships are deliberately not implemented: this
connector does not migrate server-level logins, and roles are created
on demand as grant grantees.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    GrantDef,
    RLSPolicy,
    quote_identifier,
    validate_identifier,
)
from core.connectors.postgresql._models import _qualify
from core.connectors.postgresql.objects.sequence import owned_sequences


# ============================================================================
# SOURCE-SIDE ROW-LEVEL SECURITY
# ============================================================================

def discover_rls_policies(
    conn: Any,
    schemas: tuple[str, ...],
    table: str,
    schema_name: str | None = None,
) -> list[RLSPolicy]:
    """Return the row-level security policies defined on *table*.

    ``polcmd`` is mapped from its single-character code to the SQL command,
    and the permissive/restrictive flag is read from ``polpermissive``. Both
    the USING and WITH CHECK expressions are decompiled with
    ``pg_get_expr`` so the policy body is replayed verbatim on the target.
    """
    policies: list[RLSPolicy] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pol.polname, "
            "  CASE pol.polcmd "
            "    WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT' "
            "    WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE' "
            "    ELSE 'ALL' END AS cmd, "
            "  CASE pol.polpermissive WHEN true THEN 'PERMISSIVE' ELSE 'RESTRICTIVE' END, "
            "  pg_get_expr(pol.polqual, pol.polrelid) AS using_expr, "
            "  pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr, "
            "  n.nspname "
            "FROM pg_policy pol "
            "JOIN pg_class c ON c.oid = pol.polrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY(%s) AND c.relname = %s",
            (list(schemas), table),
        )
        for row in cur.fetchall():
            pol_name, cmd, permissive, using_expr, check_expr, nspname = row
            policies.append(RLSPolicy(
                name=pol_name, table=table, cmd=cmd,
                permissive=permissive,
                using_expr=using_expr,
                check_expr=check_expr,
                schema_name=nspname or schema_name or "public",
            ))
    return policies


# ============================================================================
# SOURCE-SIDE GRANT DISCOVERY
# ============================================================================

def discover_grants(conn: Any, schemas: tuple[str, ...]) -> list[GrantDef]:
    """Return all object grants in the configured schemas.

    Five distinct catalog strategies are used, because PostgreSQL exposes
    privileges through different views and ACL columns depending on the object:

      1. tables       via ``information_schema.role_table_grants``
      2. columns      via ``information_schema.role_column_grants``
      3. sequences    via ``aclexplode(relacl)`` on ``pg_class``
      4. schemas      via ``aclexplode(nspacl)`` on ``pg_namespace``
      5. routines     via ``aclexplode(proacl)`` on ``pg_proc``

    In every case ``PUBLIC`` grantees are dropped, and self-granted entries
    (``grantor = grantee``) are dropped as noise, except for schema and routine
    ACLs where the exclusion is expressed against the current user.

    Ownership-derived grants: a role with INSERT on a table also needs USAGE on
    that table's owned (SERIAL/IDENTITY) sequence, because the sequence backs
    the inserted column. These are synthesized after the explicit sequence
    grants so that an explicit grant is never duplicated.
    """
    grants: list[GrantDef] = []
    with conn.cursor() as cur:
        schema_list = list(schemas)
        # Table grants
        cur.execute(
            "SELECT grantee, table_schema, table_name, "
            "  string_agg(privilege_type, ', ' ORDER BY privilege_type) "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema = ANY(%s) "
            "  AND grantee NOT IN ('PUBLIC') "
            "  AND grantor != grantee "
            "GROUP BY grantee, table_schema, table_name "
            "ORDER BY table_schema, table_name, grantee",
            (schema_list,),
        )
        table_grants: list[tuple[str, str, str, str]] = []
        for row in cur.fetchall():
            grantee, schema_name, table_name, privs = row
            table_grants.append((grantee, schema_name, table_name, privs))
            grants.append(GrantDef(
                privileges=privs, object_type="TABLE",
                object_name=table_name, grantee=grantee, schema_name=schema_name,
            ))

        # Column grants
        cur.execute(
            "SELECT grantee, table_schema, table_name, column_name, "
            "  string_agg(privilege_type, ', ' ORDER BY privilege_type) "
            "FROM information_schema.role_column_grants "
            "WHERE table_schema = ANY(%s) "
            "  AND grantee NOT IN ('PUBLIC') "
            "  AND grantor != grantee "
            "GROUP BY grantee, table_schema, table_name, column_name "
            "ORDER BY table_schema, table_name, column_name, grantee",
            (schema_list,),
        )
        for row in cur.fetchall():
            grantee, schema_name, table_name, column_name, privs = row
            grants.append(GrantDef(
                privileges=privs, object_type="COLUMN",
                object_name=f"{table_name}.{column_name}", grantee=grantee, schema_name=schema_name,
            ))

        # Sequence grants (explicit ACL entries)
        cur.execute(
            "SELECT r.rolname AS grantee, n.nspname, c.relname, "
            "  string_agg(acl.privilege_type, ', ' ORDER BY acl.privilege_type) "
            "FROM pg_class c "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "JOIN aclexplode(c.relacl) acl ON true "
            "JOIN pg_roles r ON r.oid = acl.grantee "
            "WHERE n.nspname = ANY(%s) "
            "  AND c.relkind = 'S' "
            "  AND c.relacl IS NOT NULL "
            "  AND acl.grantee != 0 "
            "  AND acl.grantor != acl.grantee "
            "GROUP BY r.rolname, n.nspname, c.relname "
            "ORDER BY n.nspname, c.relname, r.rolname",
            (schema_list,),
        )
        for row in cur.fetchall():
            grantee, schema_name, seq_name, privs = row
            grants.append(GrantDef(
                privileges=privs, object_type="SEQUENCE",
                object_name=seq_name, grantee=grantee, schema_name=schema_name,
            ))

        # Sequence grants for owned sequences (SERIAL/IDENTITY columns)
        # If a role has INSERT on a table with an owned sequence, they need USAGE on that sequence
        # This runs AFTER explicit sequence grants so we can avoid duplicates
        if table_grants:
            # (table schema, table, column) -> (sequence schema, sequence name)
            owned = owned_sequences(conn, schemas)

            for grantee, schema_name, table_name, privs in table_grants:
                if "INSERT" in privs:
                    # Check for owned sequence on any column of this table
                    for (s, t, c), (seq_schema, seq_name) in owned.items():
                        if s == schema_name and t == table_name:
                            # Avoid duplicate if explicit grant already exists
                            if not any(g.object_type == "SEQUENCE" and g.object_name == seq_name and g.grantee == grantee for g in grants):
                                grants.append(GrantDef(
                                    privileges="USAGE, SELECT", object_type="SEQUENCE",
                                    object_name=seq_name, grantee=grantee, schema_name=seq_schema,
                                ))

        # Schema grants
        cur.execute(
            "SELECT n.nspname, r.rolname AS grantee, acl.privilege_type "
            "FROM pg_namespace n "
            "JOIN aclexplode(n.nspacl) acl ON true "
            "JOIN pg_roles r ON r.oid = acl.grantee "
            "WHERE n.nspname = ANY(%s) "
            "  AND n.nspacl IS NOT NULL "
            "  AND r.rolname NOT IN ('PUBLIC') "
            "  AND acl.grantee != (SELECT oid FROM pg_roles WHERE rolname = current_user)",
            (schema_list,),
        )
        for row in cur.fetchall():
            schema_name, grantee, privilege_type = row
            grants.append(GrantDef(
                privileges=privilege_type,
                object_type="SCHEMA",
                object_name=schema_name, grantee=grantee, schema_name=schema_name,
            ))

        # Function and procedure grants
        cur.execute(
            "SELECT n.nspname, p.proname || '(' || "
            "  pg_get_function_arguments(p.oid) || ')', r.rolname AS grantee, "
            "  acl.privilege_type, p.prokind "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON p.pronamespace = n.oid "
            "JOIN aclexplode(p.proacl) acl ON true "
            "JOIN pg_roles r ON r.oid = acl.grantee "
            "WHERE n.nspname = ANY(%s) "
            "  AND p.prokind IN ('f', 'p') "
            "  AND p.proacl IS NOT NULL "
            "  AND r.rolname NOT IN ('PUBLIC') "
            "  AND acl.grantee != (SELECT oid FROM pg_roles WHERE rolname = current_user)",
            (schema_list,),
        )
        for row in cur.fetchall():
            schema_name, func_sig, grantee, privilege_type, prokind = row
            object_type = "FUNCTION" if prokind == "f" else "PROCEDURE"
            grants.append(GrantDef(
                privileges=privilege_type,
                object_type=object_type,
                object_name=func_sig, grantee=grantee, schema_name=schema_name,
            ))

    return grants


# ============================================================================
# TARGET-SIDE ROW-LEVEL SECURITY
# ============================================================================

def apply_rls_policy(conn: Any, policy: RLSPolicy) -> None:
    """Enable row-level security on the policy's table and create the policy.

    Enabling RLS and creating the policy are two separate statements: a failure
    to enable is rolled back and ignored so the policy is still attempted, while
    a failure to create the policy is audited and swallowed. Both match the
    behaviour of the rest of the object application layer.
    """
    validate_identifier(policy.table, "table")
    table_qname = _qualify(policy.schema_name, policy.table)
    with conn.cursor() as cur:
        try:
            cur.execute(f"ALTER TABLE {table_qname} ENABLE ROW LEVEL SECURITY")
            conn.commit()
        except Exception:
            conn.rollback()

        try:
            using_clause = f" USING ({policy.using_expr})" if policy.using_expr else ""
            check_clause = f" WITH CHECK ({policy.check_expr})" if policy.check_expr else ""
            cur.execute(
                f"CREATE POLICY {policy.name} ON {table_qname} "
                f"AS {policy.permissive} FOR {policy.cmd}"
                f"{using_clause}{check_clause}"
            )
            conn.commit()
            audit_log(phase="create_rls_policy", status="created",
                      details={"table": table_qname, "policy": policy.name})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_rls_policy", status="skipped",
                      details={"policy": policy.name, "reason": str(exc)})


# ============================================================================
# TARGET-SIDE GRANTS
# ============================================================================

def apply_grant(conn: Any, grant: GrantDef) -> None:
    """Apply a single grant on the target.

    Unlike most object application methods this one raises on failure: a
    privilege that silently fails to apply leaves the migrated database with
    silently wrong access control, so the failure must surface to the
    orchestrator.
    """
    with conn.cursor() as cur:
        try:
            if grant.object_type == "SCHEMA":
                qualified_name = quote_identifier(grant.schema_name)
                cur.execute(
                    f"GRANT {grant.privileges} ON SCHEMA {qualified_name} TO {grant.grantee}"
                )
            elif grant.object_type == "COLUMN":
                parts = grant.object_name.split(".")
                table_name = quote_identifier(parts[0])
                column_name = quote_identifier(parts[1])
                if grant.schema_name == "public":
                    qualified_table = table_name
                else:
                    qualified_table = f"{quote_identifier(grant.schema_name)}.{table_name}"
                cur.execute(
                    f"GRANT {grant.privileges} ({column_name}) ON TABLE {qualified_table} TO {grant.grantee}"
                )
            elif grant.object_type in ("FUNCTION", "PROCEDURE"):
                qualified_name = f"{quote_identifier(grant.schema_name)}.{grant.object_name}"
                cur.execute(
                    f"GRANT {grant.privileges} ON {grant.object_type} {qualified_name} TO {grant.grantee}"
                )
            elif grant.schema_name == "public":
                qualified_name = grant.object_name
                cur.execute(
                    f"GRANT {grant.privileges} ON {grant.object_type} {qualified_name} TO {grant.grantee}"
                )
            else:
                qualified_name = f"{quote_identifier(grant.schema_name)}.{quote_identifier(grant.object_name)}"
                cur.execute(
                    f"GRANT {grant.privileges} ON {grant.object_type} {qualified_name} TO {grant.grantee}"
                )
            conn.commit()
            audit_log(phase="apply_grant", status="applied",
                      details={"object": grant.object_name, "grantee": grant.grantee})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="apply_grant", status="failed",
                      details={"object": grant.object_name, "grantee": grant.grantee, "reason": str(exc)})
            raise


# ============================================================================
# TARGET-SIDE ROLES
# ============================================================================

def create_role_if_not_exists(conn: Any, role_name: str) -> None:
    """Create a role on the target when it does not already exist.

    Roles are created on demand as grant grantees: PostgreSQL has no source-side
    role discovery in this connector, so a role appears on the target only when
    a discovered grant references it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {quote_identifier(role_name)}")
            conn.commit()
            audit_log(phase="create_role", status="created", details={"role": role_name})
        else:
            audit_log(phase="create_role", status="exists", details={"role": role_name})
