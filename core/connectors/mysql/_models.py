"""MySQL connector shared helpers, constants and exceptions.

Every symbol here is engine-specific and shared by more than one
connector surface (``source``, ``target``) or by a future object
module. Nothing in this module imports ``source``, ``target`` or
``cdc``, so it is safe to import from anywhere in the package.

``EventDef`` is re-exported here for MySQL-internal use. Its definition
still lives in :mod:`core.connectors.base` because the
``SourceConnector.list_events`` / ``TargetConnector.create_event`` ABC
signatures and existing tests import it from there; relocating the
definition out of the shared DTO layer requires a coordinated change to
those call sites.
"""

from __future__ import annotations

from typing import Any
import re

from core.connectors.base import (
    EventDef,
    Schema,
)

__all__ = [
    "EventDef",
    "MySQLRoutineCreationPolicyError",
    "MySQLEventTimingSafetyError",
    "MySQLSecurityMetadataNotVisible",
]


class MySQLRoutineCreationPolicyError(RuntimeError):
    """Target policy prevents creating a function or trigger under binary logging."""
class MySQLEventTimingSafetyError(RuntimeError):
    """An enabled one-time event can no longer retain its execution semantics."""
class MySQLSecurityMetadataNotVisible(RuntimeError):
    """Security catalog access is unavailable to the migration identity."""
def _mysql_routine_policy_error(error: Exception, object_type: str) -> MySQLRoutineCreationPolicyError | None:
    text = str(error)
    if "1419" not in text and "log_bin_trust_function_creators" not in text:
        return None
    return MySQLRoutineCreationPolicyError(
        f"{object_type}: BLOCKED\n"
        "Please ask a MySQL administrator to run once:\n"
        "SET PERSIST log_bin_trust_function_creators = ON;\n"
        "This server configuration persists across MySQL restarts.\n"
        "Then re-run the migration."
    )
def _q(name: str) -> str:
    """Quote a MySQL identifier. Metadata values are never interpolated bare."""
    return "`" + name.replace("`", "``") + "`"
def _qname(database: str, name: str) -> str:
    return f"{_q(database)}.{_q(name)}"
def _qaccount(user: str, host: str) -> str:
    """Quote an account as identifiers, never as interpolated SQL literals."""
    return f"{_q(user)}@{_q(host)}"
_MYSQL_ACCOUNT_PART = r"(?:`(?:``|[^`])*`|[^`@\s]+)"
_MYSQL_DEFINER_RE = re.compile(
    rf"(?i)\A\s*CREATE\s+(?P<clause>DEFINER\s*=\s*"
    rf"(?P<user>{_MYSQL_ACCOUNT_PART})\s*@\s*(?P<host>{_MYSQL_ACCOUNT_PART})"
    rf"(?=\s+(?:FUNCTION|PROCEDURE|TRIGGER|EVENT)\b))"
)
def _mysql_definer_identity(ddl: str) -> tuple[str, str] | None:
    """Extract a stored object's exact MySQL DEFINER account identity."""
    match = _MYSQL_DEFINER_RE.match(ddl)
    if match is None:
        return None

    def unquote(part: str) -> str:
        return part[1:-1].replace("``", "`") if part.startswith("`") else part

    return unquote(match["user"]), unquote(match["host"])
def _rewrite_mysql_definer(
    ddl: str,
    target_definer: str | None,
    *,
    preserve_source_definer: bool = False,
    available_accounts: set[tuple[str, str]] | None = None,
    can_set_any_definer: bool = False,
) -> str:
    """Preserve only available source accounts the target may set as DEFINER."""
    match = _MYSQL_DEFINER_RE.match(ddl)
    if match is None:
        return ddl

    source_account = _mysql_definer_identity(ddl)
    if source_account is None:
        return ddl
    if target_definer is None:
        return ddl

    account = re.fullmatch(
        rf"\s*(?P<user>{_MYSQL_ACCOUNT_PART})\s*@\s*(?P<host>{_MYSQL_ACCOUNT_PART})\s*",
        target_definer,
    )
    if account is None:
        raise ValueError("MySQL routine_definer must use the format user@host")

    def unquote(part: str) -> str:
        return part[1:-1].replace("``", "`") if part.startswith("`") else part

    target_user, target_host = unquote(account["user"]), unquote(account["host"])
    target_identity = (target_user, target_host)
    if (
        preserve_source_definer
        and available_accounts is not None
        and source_account in available_accounts
        and (can_set_any_definer or source_account == target_identity)
    ):
        return ddl
    if source_account == (target_user, target_host):
        return ddl

    replacement = f"DEFINER={_q(target_user)}@{_q(target_host)}"
    return ddl[:match.start("clause")] + replacement + ddl[match.end("clause"):]
def _rewrite_view_database_references(
    definition: str, source_database: str, target_database: str
) -> str:
    """Map source-database qualifiers in a MySQL view definition to the target.

    ``INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`` commonly contains fully
    qualified MySQL object names.  Replaying it unchanged on a different
    target database makes the target view continue to read from the source.
    This deliberately scans SQL rather than using ``str.replace`` so text in
    string literals and comments, and references to other databases, remain
    untouched.
    """
    if source_database == target_database:
        return definition

    def has_qualifier_after(position: int) -> bool:
        while position < len(definition) and definition[position].isspace():
            position += 1
        return position < len(definition) and definition[position] == "."

    output: list[str] = []
    index = 0
    source_folded = source_database.casefold()
    while index < len(definition):
        char = definition[index]

        # Preserve line and block comments verbatim.
        if char == "#" or (
            char == "-"
            and definition[index:index + 2] == "--"
            and index + 2 < len(definition)
            and definition[index + 2].isspace()
        ):
            end = definition.find("\n", index)
            if end == -1:
                return "".join(output) + definition[index:]
            output.append(definition[index:end + 1])
            index = end + 1
            continue
        if definition[index:index + 2] == "/*":
            end = definition.find("*/", index + 2)
            if end == -1:
                return "".join(output) + definition[index:]
            output.append(definition[index:end + 2])
            index = end + 2
            continue

        # MySQL supports both doubled quotes and backslash escaping in string
        # literals. Double quotes can also quote identifiers with ANSI_QUOTES.
        if char == "'":
            quote = char
            end = index + 1
            while end < len(definition):
                if definition[end] == "\\":
                    end += 2
                    continue
                if definition[end] == quote:
                    if end + 1 < len(definition) and definition[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            output.append(definition[index:end])
            index = end
            continue

        if char == '"':
            end = index + 1
            while end < len(definition):
                if definition[end] == '"':
                    if end + 1 < len(definition) and definition[end + 1] == '"':
                        end += 2
                        continue
                    break
                end += 1
            if end < len(definition):
                identifier = definition[index + 1:end].replace('""', '"')
                if identifier.casefold() == source_folded and has_qualifier_after(end + 1):
                    output.append('"' + target_database.replace('"', '""') + '"')
                else:
                    output.append(definition[index:end + 1])
                index = end + 1
                continue

        # Backticks quote identifiers in MySQL.  A doubled backtick represents
        # a literal backtick in the identifier.
        if char == "`":
            end = index + 1
            while end < len(definition):
                if definition[end] == "`":
                    if end + 1 < len(definition) and definition[end + 1] == "`":
                        end += 2
                        continue
                    break
                end += 1
            if end < len(definition):
                identifier = definition[index + 1:end].replace("``", "`")
                if identifier.casefold() == source_folded and has_qualifier_after(end + 1):
                    output.append(_q(target_database))
                else:
                    output.append(definition[index:end + 1])
                index = end + 1
                continue

        # Bare MySQL identifiers.  Only a source-database token immediately
        # followed by a qualifier dot is mapped; table aliases and unrelated
        # identifiers are not candidates.
        if char.isalpha() or char in {"_", "$"}:
            end = index + 1
            while end < len(definition) and (definition[end].isalnum() or definition[end] in {"_", "$"}):
                end += 1
            identifier = definition[index:end]
            if identifier.casefold() == source_folded and has_qualifier_after(end):
                output.append(target_database)
            else:
                output.append(identifier)
            index = end
            continue

        output.append(char)
        index += 1
    return "".join(output)
def literal(value: str) -> str:
    """Render a Python string as a MySQL string literal.

    Shared by the table and comment object modules, which both need the
    same escaping when emitting DDL.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _connection_options(config: dict[str, Any]) -> dict[str, Any]:
    """Provider-neutral MySQL TLS options; legacy ``ssl: bool`` still works."""
    tls = config.get("tls", {})
    enabled = tls.get("enabled", config.get("ssl", True))
    options: dict[str, Any] = {"ssl_disabled": not enabled}
    if enabled:
        if tls.get("ca_path"): options["ssl_ca"] = tls["ca_path"]
        if tls.get("cert_path"): options["ssl_cert"] = tls["cert_path"]
        if tls.get("key_path"): options["ssl_key"] = tls["key_path"]
        if "verify_cert" in tls: options["ssl_verify_cert"] = bool(tls["verify_cert"])
        if "verify_identity" in tls: options["ssl_verify_identity"] = bool(tls["verify_identity"])
    return options
def _default_sql(value: Any, column_type: str) -> str:
    """Render INFORMATION_SCHEMA defaults as MySQL DDL, preserving expressions."""
    if value is None:
        return "NULL"
    text = str(value)
    if re.fullmatch(r"CURRENT_TIMESTAMP(?:\(\d*\))?", text, re.IGNORECASE) or text.upper() == "NULL" or re.match(r"^-?(?:\d+|\d+\.\d+)$", text) or text.startswith("("):
        return text
    if text.startswith(("'", '"', "b'", "B'")):
        return text
    return "'" + text.replace("'", "''") + "'"
def _mysql_set_members(column_type: str) -> list[str]:
    """Return SET members in the order declared by MySQL metadata."""
    text = column_type.strip()
    if not text.lower().startswith("set(") or not text.endswith(")"):
        return []

    members: list[str] = []
    index = text.find("(") + 1
    end = len(text) - 1
    while index < end:
        while index < end and (text[index].isspace() or text[index] == ","):
            index += 1
        if index >= end or text[index] != "'":
            break
        index += 1
        chars: list[str] = []
        while index < end:
            char = text[index]
            if char == "\\" and index + 1 < end:
                chars.append(text[index + 1])
                index += 2
            elif char == "'":
                index += 1
                break
            else:
                chars.append(char)
                index += 1
        members.append("".join(chars))
    return members
def _normalize_mysql_set_value(value: Any, column_type: str) -> Any:
    """Convert a connector SET collection to MySQL's comma-separated value."""
    if not column_type.strip().lower().startswith("set(") or not isinstance(value, (set, frozenset)):
        return value
    members = _mysql_set_members(column_type)
    declared_order = {member: position for position, member in enumerate(members)}
    ordered = sorted(value, key=lambda member: (declared_order.get(member, len(members)), member))
    return ",".join(ordered)
