"""MySQL security object operations.

Owns account discovery, direct-privilege discovery, grant application, user
creation and authorization-error classification. This module must not import
``source``, ``target`` or ``cdc``, and it must not import any sibling object
module: it depends only on ``_models``, the cross-engine DTOs and the
standard library.

DEFINER rewriting is deliberately *not* here. ``_rewrite_mysql_definer``
stays in ``core.connectors.mysql._models``; this module only supplies the
account metadata that the caller already tracks on the target connector
(``_available_definer_accounts`` / ``_current_account``), and
:func:`create_user_if_not_exists` mutates that set in place so the existing
DEFINER flow is unaffected.

:func:`security_user_allowlist`, :func:`list_users`,
:func:`filter_direct_user_grants` and :func:`list_grants` replace the former
private connector helpers of the same purpose.
:func:`is_authorization_error` replaces the former static connector method.

Every SQL statement, parameter order, ordering guarantee, commit/rollback
boundary, audit-free error path and message of the former
``MySQLSourceConnector``/``MySQLTargetConnector`` implementations is carried
over unchanged.
"""
from __future__ import annotations

import re
from typing import Any

from core.connectors.base import (
    GrantDef,
    UserDef,
    validate_identifier,
)

from core.connectors.mysql._models import (
    MySQLSecurityMetadataNotVisible,
    _q,
    _qaccount,
    _qname,
)


_MYSQL_SYSTEM_USERS = {"root", "mysql.sys", "mysql.session", "mysql.infoschema", "mysqlxsys"}


def _is_system_account(account: tuple[str, str] | None) -> bool:
    return account is None or account[0].casefold() in _MYSQL_SYSTEM_USERS


def _grantee_key(grantee: str) -> tuple[str, str] | None:
    """Parse INFORMATION_SCHEMA's quoted user@host form without SQL execution."""
    match = re.fullmatch(r"['`](.*?)['`]@['`](.*?)['`]", grantee.strip())
    return (match.group(1), match.group(2)) if match else None


def security_user_allowlist(config: dict[str, Any]) -> set[tuple[str, str]] | None:
    """Return configured user identities, or None for automatic discovery."""
    configured = config.get("security_users")
    if not configured:
        return None
    result: set[tuple[str, str]] = set()
    for item in configured:
        if isinstance(item, str):
            result.add((item, "%"))
        elif isinstance(item, dict) and item.get("user"):
            result.add((str(item["user"]), str(item.get("host", "%"))))
        else:
            raise ValueError("migration.security_users entries require user and optional host")
    return result


def list_users(conn: Any, config: dict[str, Any]) -> list[UserDef]:
    """Return unlocked MySQL accounts selected by the user allowlist policy.

    Locked accounts are excluded. Authentication plugins and password
    material are deliberately omitted.
    """
    requested = security_user_allowlist(config)
    if requested is not None and not requested:
        return []
    try:
        with conn.cursor() as cur:
            if requested is None:
                cur.execute(
                    "SELECT User,Host FROM mysql.user "
                    "WHERE User NOT IN ('root','mysql.sys','mysql.session','mysql.infoschema','mysqlxsys') "
                    "AND User<>'' AND account_locked='N' ORDER BY User,Host"
                )
            else:
                predicates = " OR ".join("(User=%s AND Host=%s)" for _ in requested)
                params = tuple(value for identity in sorted(requested) for value in identity)
                cur.execute(
                    f"SELECT User,Host FROM mysql.user WHERE ({predicates}) AND account_locked='N' ORDER BY User,Host",
                    params,
                )
            rows = cur.fetchall()
    except Exception as exc:
        raise MySQLSecurityMetadataNotVisible(
            "MySQL account metadata is not visible to the migration account"
        ) from exc

    found = {(row[0], row[1]) for row in rows}
    if requested is not None:
        missing = requested - found
        if missing:
            labels = ", ".join(f"{user}@{host}" for user, host in sorted(missing))
            raise RuntimeError(f"Configured MySQL user is missing or not visible: {labels}")

    return [
        UserDef(name=user, host=host)
        for user, host in rows
        if not _is_system_account((user, host))
    ]


def filter_direct_user_grants(
    conn: Any,
    config: dict[str, Any],
    grants: list[GrantDef],
) -> list[GrantDef]:
    allowed_users = security_user_allowlist(config)
    user_identities = {
        (user.name, user.host or "%") for user in list_users(conn, config)
    }
    # Global privileges span every database and are migrated only for
    # accounts explicitly selected by the operator.
    supported_global = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    return [
        grant for grant in grants
        if not _is_system_account((grant.grantee, grant.grantee_host or "%"))
        and grant.privileges.upper() not in {"USAGE", "ROLE_ADMIN", "PROXY"}
        and (grant.grantee, grant.grantee_host or "%") in user_identities
        and (
            grant.object_type != "GLOBAL"
            or (
                allowed_users is not None
                and (grant.grantee, grant.grantee_host or "%") in allowed_users
                and grant.privileges.upper() in supported_global
            )
        )
    ]


def list_grants(conn: Any, config: dict[str, Any]) -> list[GrantDef]:
    database = config["database"]
    grants: list[GrantDef] = []

    def append_grant(
        privileges: str,
        object_type: str,
        object_name: str,
        schema_name: str,
        grantee_text: str,
        grant_option: bool = False,
    ) -> None:
        account = _grantee_key(grantee_text)
        if account is None:
            return
        user, host = account
        grants.append(GrantDef(
            privileges=privileges,
            object_type=object_type,
            object_name=object_name,
            grantee=user,
            schema_name=schema_name,
            grant_option=grant_option,
            grantee_host=host,
        ))

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT GRANTEE,TABLE_SCHEMA,TABLE_NAME,PRIVILEGE_TYPE,IS_GRANTABLE "
                "FROM INFORMATION_SCHEMA.TABLE_PRIVILEGES WHERE TABLE_SCHEMA=%s",
                (database,),
            )
            for row in cur.fetchall():
                append_grant(row[3], "TABLE", row[2], row[1], row[0], str(row[4]).upper() == "YES")

            try:
                cur.execute(
                    "SELECT GRANTEE,ROUTINE_NAME,ROUTINE_TYPE,PRIVILEGE_TYPE,IS_GRANTABLE "
                    "FROM INFORMATION_SCHEMA.ROUTINE_PRIVILEGES WHERE ROUTINE_SCHEMA=%s",
                    (database,),
                )
                for row in cur.fetchall():
                    append_grant(row[3], row[2], row[1], database, row[0], str(row[4]).upper() == "YES")
            except Exception:
                # Compatible servers may not expose routine privilege metadata.
                conn.rollback()

            cur.execute(
                "SELECT GRANTEE,TABLE_SCHEMA,PRIVILEGE_TYPE,IS_GRANTABLE "
                "FROM INFORMATION_SCHEMA.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA=%s",
                (database,),
            )
            for row in cur.fetchall():
                append_grant(row[2], "DATABASE", row[1], row[1], row[0], str(row[3]).upper() == "YES")

            cur.execute(
                "SELECT GRANTEE,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,PRIVILEGE_TYPE,IS_GRANTABLE "
                "FROM INFORMATION_SCHEMA.COLUMN_PRIVILEGES WHERE TABLE_SCHEMA=%s",
                (database,),
            )
            for row in cur.fetchall():
                append_grant(row[4], "COLUMN", f"{row[2]}.{row[3]}", row[1], row[0], str(row[5]).upper() == "YES")

            try:
                cur.execute(
                    "SELECT GRANTEE,TABLE_CATALOG,PRIVILEGE_TYPE,IS_GRANTABLE "
                    "FROM INFORMATION_SCHEMA.USER_PRIVILEGES"
                )
                for row in cur.fetchall():
                    append_grant(row[2], "GLOBAL", "*", "*", row[0], str(row[3]).upper() == "YES")
            except Exception:
                # USER_PRIVILEGES is standard MySQL metadata but may be hidden
                # or absent on compatible services; retain narrower grants.
                conn.rollback()
    except Exception as exc:
        raise MySQLSecurityMetadataNotVisible(
            "MySQL direct privilege metadata is not visible to the migration account"
        ) from exc

    return filter_direct_user_grants(conn, config, grants)


def apply_grant(conn: Any, database: str, grant: GrantDef) -> None:
    # Grantees are not created by the platform; target permissions decide
    # whether this direct MySQL statement can be applied.
    target_database = validate_identifier(database, "database")
    object_name = grant.object_name.rsplit(".", 1)[-1]
    grantee = (
        _qaccount(grant.grantee, grant.grantee_host)
        if grant.grantee_host is not None
        else grant.grantee
    )
    with conn.cursor() as cur:
        if grant.object_type == "GLOBAL":
            object_type, target = "", "*.*"
        elif grant.object_type == "DATABASE":
            object_type, target = "", f"{_q(target_database)}.*"
        elif grant.object_type == "COLUMN":
            table, column = grant.object_name.rsplit(".", 1)
            validate_identifier(table, "table")
            validate_identifier(column, "column")
            object_type, target = "", f"{_qname(target_database, table)} ({_q(column)})"
        else:
            validate_identifier(object_name, "object")
            object_type = "PROCEDURE" if grant.object_type == "PROCEDURE" else ("FUNCTION" if grant.object_type == "FUNCTION" else "TABLE")
            target = _qname(target_database, object_name)
        try:
            grant_option = " WITH GRANT OPTION" if grant.grant_option else ""
            scope = f"{object_type} " if object_type else ""
            cur.execute(f"GRANT {grant.privileges} ON {scope}{target} TO {grantee}{grant_option}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def create_user_if_not_exists(
    conn: Any,
    user_name: str,
    host: str | None,
    available_accounts: set[tuple[str, str]],
    current_account: tuple[str, str] | None,
) -> None:
    """Create a host-scoped account without copying source credentials.

    ``available_accounts`` is the target connector's DEFINER account set and
    is mutated in place, which is what lets ``_rewrite_mysql_definer`` see
    accounts created through this path. An account that is already available,
    or that is the migration account itself, short-circuits without issuing
    any SQL.
    """
    identity = (user_name, host or "%")
    if identity in available_accounts:
        return
    if identity == current_account:
        available_accounts.add(identity)
        return
    account = _qaccount(*identity)
    with conn.cursor() as cur:
        try:
            cur.execute(f"CREATE USER IF NOT EXISTS {account}")
            conn.commit()
            available_accounts.add(identity)
        except Exception:
            conn.rollback()
            raise


def is_authorization_error(error: Exception) -> bool:
    """Classify common MySQL privilege-denial errors for per-grant reporting."""
    codes = {1044, 1045, 1142, 1143, 1227, 1370, 1410, 1698}
    args = getattr(error, "args", ())
    if args:
        try:
            if int(args[0]) in codes:
                return True
        except (TypeError, ValueError):
            pass
    message = str(error).casefold()
    return any(token in message for token in (
        "access denied",
        "not authorized",
        "not authorised",
        "permission denied",
        "grant command denied",
        "you are not allowed",
        "insufficient privilege",
        "need (at least one of) the",
    ))
