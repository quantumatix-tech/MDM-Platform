from __future__ import annotations

from collections.abc import Iterator
from typing import Any
import json
from pathlib import Path
import re
from datetime import datetime, timedelta
import hashlib

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    Index,
    ForeignKey,
    CheckConstraint,
    ViewDefinition,
    FunctionDef,
    TriggerDef,
    MySQLPartitionDef,
    EventDef,
    CommentDef,
    GrantDef,
    SecurityPrincipalDef,
    RoleMembershipDef,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    UnmappedTypeError,
    validate_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


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


def _grantee_key(grantee: str) -> tuple[str, str] | None:
    """Parse INFORMATION_SCHEMA's quoted user@host form without SQL execution."""
    match = re.fullmatch(r"['`](.*?)['`]@['`](.*?)['`]", grantee.strip())
    return (match.group(1), match.group(2)) if match else None


_MYSQL_SYSTEM_USERS = {"root", "mysql.sys", "mysql.session", "mysql.infoschema", "mysqlxsys"}


def _is_system_account(account: tuple[str, str] | None) -> bool:
    return account is None or account[0].casefold() in _MYSQL_SYSTEM_USERS


_MYSQL_ACCOUNT_PART = r"(?:`(?:``|[^`])*`|[^`@\s]+)"
_MYSQL_DEFINER_RE = re.compile(
    rf"(?i)\A\s*CREATE\s+(?P<clause>DEFINER\s*=\s*"
    rf"(?P<user>{_MYSQL_ACCOUNT_PART})\s*@\s*(?P<host>{_MYSQL_ACCOUNT_PART})"
    rf"(?=\s+(?:FUNCTION|PROCEDURE|TRIGGER|EVENT)\b))"
)


def _rewrite_mysql_definer(ddl: str, target_definer: str) -> str:
    """Replace only a MySQL CREATE statement's existing DEFINER clause."""
    match = _MYSQL_DEFINER_RE.match(ddl)
    if match is None:
        return ddl

    account = re.fullmatch(
        rf"\s*(?P<user>{_MYSQL_ACCOUNT_PART})\s*@\s*(?P<host>{_MYSQL_ACCOUNT_PART})\s*",
        target_definer,
    )
    if account is None:
        raise ValueError("MySQL routine_definer must use the format user@host")

    def unquote(part: str) -> str:
        return part[1:-1].replace("``", "`") if part.startswith("`") else part

    source_user, source_host = unquote(match["user"]), unquote(match["host"])
    target_user, target_host = unquote(account["user"]), unquote(account["host"])
    if (source_user, source_host) == (target_user, target_host):
        return ddl

    replacement = f"DEFINER={_q(target_user)}@{_q(target_host)}"
    return ddl[:match.start("clause")] + replacement + ddl[match.end("clause"):]


def _mysql_partition_boundary(description: str | None) -> str:
    boundary = (description or "").strip()
    if boundary.upper() == "MAXVALUE":
        return "(MAXVALUE)"
    if boundary.startswith("(") and boundary.endswith(")"):
        return boundary
    return f"({boundary})"


def _mysql_partition_clause(schema: Schema) -> str:
    method = (schema.mysql_partition_method or "").upper()
    expression = (schema.mysql_partition_expression or "").strip()
    partitions = schema.mysql_partitions
    if not method:
        return ""
    if method not in {"RANGE", "RANGE COLUMNS", "LIST", "LIST COLUMNS", "HASH", "KEY"}:
        raise ValueError(f"Unsupported MySQL partition method: {method}")
    if not expression:
        raise ValueError(f"MySQL {method} partitioning has no expression")

    if method in {"HASH", "KEY"}:
        return f"PARTITION BY {method} ({expression}) PARTITIONS {len(partitions)}"

    boundary_keyword = "VALUES LESS THAN" if method.startswith("RANGE") else "VALUES IN"
    definitions = ", ".join(
        f"PARTITION {_q(partition.name)} {boundary_keyword} "
        f"{_mysql_partition_boundary(partition.description)}"
        for partition in partitions
    )
    if not definitions:
        raise ValueError(f"MySQL {method} partitioning has no partitions")
    return f"PARTITION BY {method} ({expression}) ({definitions})"


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


class MySQLSourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("mysql-connector-python", "mysql.connector")
        import mysql.connector

        # ssl_disabled (plus CA/verification options) is supplied below by
        # _connection_options for legacy and provider-neutral TLS configs.

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "database": self._config["database"],
            "user": self._config["username"],
            "password": self._config.get("password", ""),
            "connection_timeout": self._config.get("connection_timeout", 10),
        }
        conn_kwargs.update(_connection_options(self._config))

        self._conn = mysql.connector.connect(**conn_kwargs)
        # Source access is read-only. Autocommit prevents metadata locks from
        # surviving catalog reads or streaming exports until run shutdown.
        self._conn.autocommit = True
        audit_log(phase="connect", status="success", details={"engine": "mysql", "role": "source"})

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._conn.close()
        finally:
                self._conn = None

    def list_objects(self) -> list[str]:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                (db_name,),
            )
            tables = [row[0] for row in cur.fetchall()]
        for t in tables:
            validate_identifier(t, "table")
        return tables

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {object_name}")
            return cur.fetchone()[0]

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        with self._conn.cursor(dictionary=True) as cur:
            cur.execute(f"SELECT * FROM {object_name}")
            for row in cur:
                yield dict(row)

    def get_schema(self, object_name: str) -> Schema:
        validate_identifier(object_name, "table")
        db = self._config["database"]
        with self._conn.cursor(buffered=True) as cur:
            cur.execute("SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,CHARACTER_MAXIMUM_LENGTH,COLUMN_DEFAULT,EXTRA,GENERATION_EXPRESSION,COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (db, object_name))
            columns = [Column(name=n, source_type=t, nullable=(nullable == "YES"), size=size,
                default=default, generated=generated or None,
                generated_kind=("STORED" if "STORED GENERATED" in (extra or "") else "VIRTUAL") if generated else None,
                auto_increment="auto_increment" in (extra or "").lower(), comment=comment or None)
                for n, t, nullable, size, default, extra, generated, comment in cur.fetchall()]
            cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION", (db, object_name))
            primary_key = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT INDEX_NAME,NON_UNIQUE,COLUMN_NAME,INDEX_TYPE FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME<>'PRIMARY' ORDER BY INDEX_NAME,SEQ_IN_INDEX", (db, object_name))
            index_map: dict[str, Index] = {}
            for name, non_unique, column, index_type in cur.fetchall():
                index_map.setdefault(
                    name,
                    Index(name=name, columns=[], unique=not bool(non_unique), index_type=index_type),
                ).columns.append(column)
            cur.execute("SELECT k.CONSTRAINT_NAME,k.COLUMN_NAME,k.REFERENCED_TABLE_SCHEMA,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,r.UPDATE_RULE,r.DELETE_RULE FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA=%s AND k.TABLE_NAME=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL ORDER BY k.CONSTRAINT_NAME,k.ORDINAL_POSITION", (db, object_name))
            fk_map: dict[str, ForeignKey] = {}
            for name, col, ref_schema, ref_table, ref_col, on_update, on_delete in cur.fetchall():
                fk = fk_map.setdefault(name, ForeignKey(name=name, columns=[], ref_table=ref_table, ref_columns=[], ref_schema=ref_schema, on_update=on_update, on_delete=on_delete))
                fk.columns.append(col); fk.ref_columns.append(ref_col)
            cur.execute("SELECT tc.CONSTRAINT_NAME,cc.CHECK_CLAUSE FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA=%s AND tc.TABLE_NAME=%s AND tc.CONSTRAINT_TYPE='CHECK'", (db, object_name))
            # INFORMATION_SCHEMA returns escaped character-set string literals
            # on this MySQL build; DDL requires the unescaped form.
            checks = [CheckConstraint(name=n, expression=e.replace("\\'", "'")) for n, e in cur.fetchall()]
            cur.execute("SELECT TABLE_COMMENT,ENGINE,TABLE_COLLATION FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, object_name))
            comment, engine, collation = cur.fetchone()
            cur.execute("SELECT PARTITION_METHOD,PARTITION_EXPRESSION,PARTITION_NAME,PARTITION_DESCRIPTION FROM INFORMATION_SCHEMA.PARTITIONS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND PARTITION_NAME IS NOT NULL ORDER BY PARTITION_ORDINAL_POSITION", (db, object_name))
            partition_rows = cur.fetchall()

        partition_method = partition_rows[0][0] if partition_rows else None
        partition_expression = partition_rows[0][1] if partition_rows else None
        mysql_partitions = [
            MySQLPartitionDef(name=name, description=description)
            for _, _, name, description in partition_rows
        ]
        return Schema(name=object_name, columns=columns, primary_key=primary_key, indexes=list(index_map.values()), foreign_keys=list(fk_map.values()), check_constraints=checks, mysql_partition_method=partition_method, mysql_partition_expression=partition_expression, mysql_partitions=mysql_partitions, comment=comment or None, options={"engine": engine, "collation": collation})

    def _show_create(self, kind: str, name: str) -> str:
        with self._conn.cursor() as cur:
            cur.execute(f"SHOW CREATE {kind} {_q(name)}")
            row = cur.fetchone()
            for index, column_name in enumerate(cur.column_names):
                if "create" in column_name.lower() or "original statement" in column_name.lower():
                    return row[index]
            raise RuntimeError(f"SHOW CREATE {kind} did not return a DDL column")

    def list_views(self) -> list[ViewDefinition]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME,VIEW_DEFINITION FROM INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA=%s", (self._config["database"],))
            return [ViewDefinition(name=n, definition=d, schema_name=self._config["database"]) for n, d in cur.fetchall()]

    def list_functions(self) -> list[FunctionDef]:
        result: list[FunctionDef] = []
        with self._conn.cursor() as cur:
            for kind in ("FUNCTION", "PROCEDURE"):
                cur.execute("SELECT ROUTINE_NAME FROM INFORMATION_SCHEMA.ROUTINES WHERE ROUTINE_SCHEMA=%s AND ROUTINE_TYPE=%s", (self._config["database"], kind))
                result.extend(FunctionDef(name=n, ddl=self._show_create(kind, n), schema_name=self._config["database"], kind=kind.lower()) for (n,) in cur.fetchall())
        return result

    def get_all_triggers(self) -> list[TriggerDef]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=%s", (self._config["database"],))
            return [TriggerDef(name=n, table=t, ddl=self._show_create("TRIGGER", n), schema_name=self._config["database"]) for n, t in cur.fetchall()]

    def list_events(self) -> list[EventDef]:
        with self._conn.cursor() as cur:
            # Snapshot event metadata at migration start.  In particular, a
            # one-time event must not be rediscovered after its schedule passes.
            cur.execute("SELECT NOW()")
            snapshot_at = cur.fetchone()[0]
            cur.execute(
                "SELECT EVENT_NAME,EVENT_TYPE,STATUS,EXECUTE_AT,INTERVAL_VALUE,"
                "INTERVAL_FIELD,STARTS,ENDS,ON_COMPLETION,TIME_ZONE,DEFINER "
                "FROM INFORMATION_SCHEMA.EVENTS WHERE EVENT_SCHEMA=%s",
                (self._config["database"],),
            )
            rows = cur.fetchall()
        return [
            EventDef(
                name=name,
                ddl=self._show_create("EVENT", name),
                schema_name=self._config["database"],
                event_type=event_type,
                status=status,
                execute_at=execute_at,
                interval_value=str(interval_value) if interval_value is not None else None,
                interval_field=interval_field,
                starts=starts,
                ends=ends,
                on_completion=on_completion,
                time_zone=time_zone,
                definer=definer,
                snapshot_at=snapshot_at,
            )
            for (
                name, event_type, status, execute_at, interval_value,
                interval_field, starts, ends, on_completion, time_zone, definer,
            ) in rows
        ]

    def list_comments(self) -> list[CommentDef]:
        db = self._config["database"]; result: list[CommentDef] = []
        with self._conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME,TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' AND TABLE_COMMENT<>''", (db,))
            result.extend(CommentDef("TABLE", n, c, db) for n, c in cur.fetchall())
            cur.execute("SELECT c.TABLE_NAME,c.COLUMN_NAME,c.COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME WHERE c.TABLE_SCHEMA=%s AND t.TABLE_TYPE='BASE TABLE' AND c.COLUMN_COMMENT<>''", (db,))
            result.extend(CommentDef("COLUMN", f"{t}.{n}", c, db) for t, n, c in cur.fetchall())
        return result

    def _security_allowlist(self) -> dict[str, set[tuple[str, str]]] | None:
        """Return configured user/role identities, or None for the safe default."""
        configured = self._config.get("security_principals")
        if not configured:
            return None

        def identities(kind: str) -> set[tuple[str, str]]:
            result: set[tuple[str, str]] = set()
            for item in configured.get(kind, []):
                if isinstance(item, str):
                    result.add((item, "%"))
                elif isinstance(item, dict) and item.get("user"):
                    result.add((str(item["user"]), str(item.get("host", "%"))))
                else:
                    raise ValueError(f"migration.security_principals.{kind} entries require user and optional host")
            return result

        return {"USER": identities("users"), "ROLE": identities("roles")}

    def _allowed_principal_keys(self) -> set[tuple[str, str]]:
        allowlist = self._security_allowlist()
        return set() if allowlist is None else allowlist["USER"] | allowlist["ROLE"]

    def security_scope_status(self) -> str | None:
        if self._security_allowlist() is None:
            return "AUTOMATIC_DISCOVERY"
        return "ALLOWLIST_ENFORCED: principals and grants outside migration.security_principals are SKIPPED/OUT_OF_SCOPE"

    def _filter_security_grants(self, grants: list[GrantDef]) -> list[GrantDef]:
        allowlist = self._security_allowlist()
        if allowlist is None:
            return [grant for grant in grants if not _is_system_account(_grantee_key(grant.grantee))]
        allowed = self._allowed_principal_keys()
        return [grant for grant in grants if _grantee_key(grant.grantee) in allowed]

    def list_grants(self) -> list[GrantDef]:
        db = self._config["database"]; result: list[GrantDef] = []
        with self._conn.cursor() as cur:
            cur.execute("SELECT GRANTEE,TABLE_SCHEMA,TABLE_NAME,PRIVILEGE_TYPE,IS_GRANTABLE FROM INFORMATION_SCHEMA.TABLE_PRIVILEGES WHERE TABLE_SCHEMA=%s", (db,))
            result.extend(GrantDef(privileges=row[3], object_type="TABLE", object_name=row[2], grantee=row[0], schema_name=row[1], grant_option=len(row) > 4 and str(row[4]).upper() == "YES") for row in cur.fetchall())
            try:
                cur.execute("SELECT GRANTEE,ROUTINE_NAME,ROUTINE_TYPE,PRIVILEGE_TYPE,IS_GRANTABLE FROM INFORMATION_SCHEMA.ROUTINE_PRIVILEGES WHERE ROUTINE_SCHEMA=%s", (db,))
                result.extend(GrantDef(privileges=row[3], object_type=row[2], object_name=row[1], grantee=row[0], schema_name=db, grant_option=len(row) > 4 and str(row[4]).upper() == "YES") for row in cur.fetchall())
            except Exception:
                # Some compatible servers do not expose ROUTINE_PRIVILEGES.
                # Preserve table grants and leave routine grants unreported.
                self._conn.rollback()
        return self._filter_security_grants(result)

    def list_security_principals(self) -> list[SecurityPrincipalDef]:
        """Read accounts without selecting password authentication material."""
        allowlist = self._security_allowlist()
        if allowlist is None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT FROM_USER,FROM_HOST,TO_USER,TO_HOST,WITH_ADMIN_OPTION FROM mysql.role_edges")
                    edges = cur.fetchall()
                    role_keys = {(row[0], row[1]) for row in edges if not _is_system_account((row[0], row[1]))}
                    cur.execute("SELECT User,Host,plugin,account_locked,password_expired FROM mysql.user WHERE User NOT IN ('root','mysql.sys','mysql.session','mysql.infoschema','mysqlxsys') AND User<>'' ORDER BY User,Host")
                    rows = cur.fetchall()
            except Exception as exc:
                raise MySQLSecurityMetadataNotVisible("BLOCKED: MySQL account/role metadata is not visible to the migration account") from exc
            return [SecurityPrincipalDef(user, host, "ROLE" if (user, host) in role_keys else "USER", plugin, str(locked).upper() == "Y", str(expired).upper() == "Y") for user, host, plugin, locked, expired in rows if not _is_system_account((user, host))]
        requested = self._allowed_principal_keys()
        if not requested:
            return []
        predicates = " OR ".join("(User=%s AND Host=%s)" for _ in requested)
        params = tuple(value for identity in sorted(requested) for value in identity)
        try:
            with self._conn.cursor() as cur:
                # MySQL 26.7 no longer exposes mysql.user.is_role.  The
                # explicitly configured allowlist is the authority for the
                # requested principal type, so no version-specific role flag
                # or unsafe full catalog scan is needed.
                cur.execute(
                    "SELECT User,Host,plugin,account_locked,password_expired "
                    f"FROM mysql.user WHERE {predicates} ORDER BY User,Host",
                    params,
                )
                rows = cur.fetchall()
        except Exception as exc:
            raise MySQLSecurityMetadataNotVisible(
                "BLOCKED: MySQL account/role metadata is not visible to the migration account"
            ) from exc
        found = {(row[0], row[1]) for row in rows}
        missing = requested - found
        if missing:
            labels = ", ".join(f"{user}@{host}" for user, host in sorted(missing))
            raise RuntimeError(f"BLOCKED: requested security principal is missing or not visible: {labels}")
        principals = [SecurityPrincipalDef(
            user=user, host=host,
            principal_type="ROLE" if (user, host) in allowlist["ROLE"] else "USER",
            authentication_plugin=plugin, account_locked=str(locked).upper() == "Y",
            password_expired=str(expired).upper() == "Y",
        ) for user, host, plugin, locked, expired in rows]
        wrong_type = [p for p in principals if (p.user, p.host) not in allowlist[p.principal_type]]
        if wrong_type:
            labels = ", ".join(f"{p.user}@{p.host}" for p in wrong_type)
            raise RuntimeError(f"BLOCKED: requested principal has a different user/role type: {labels}")
        return principals

    def list_role_memberships(self) -> list[RoleMembershipDef]:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT FROM_USER,FROM_HOST,TO_USER,TO_HOST,WITH_ADMIN_OPTION FROM mysql.role_edges ORDER BY FROM_USER,FROM_HOST,TO_USER,TO_HOST")
                rows = cur.fetchall()
        except Exception as exc:
            raise MySQLSecurityMetadataNotVisible(
                "BLOCKED: MySQL role-edge metadata is not visible or unsupported by this server"
            ) from exc
        allowed = self._allowed_principal_keys()
        configured = self._security_allowlist() is not None
        return [RoleMembershipDef(*row[:4], with_admin_option=str(row[4]).upper() == "Y") for row in rows if (
            ((row[0], row[1]) in allowed and (row[2], row[3]) in allowed)
            if configured else
            (not _is_system_account((row[0], row[1])) and not _is_system_account((row[2], row[3])))
        )]

    def list_security_grants(self) -> list[GrantDef]:
        """Discover global, database, and column grants with grant option."""
        db = self._config["database"]
        queries = (
            ("SELECT GRANTEE,TABLE_SCHEMA,PRIVILEGE_TYPE,IS_GRANTABLE FROM INFORMATION_SCHEMA.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA=%s", "DATABASE"),
            ("SELECT GRANTEE,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,PRIVILEGE_TYPE,IS_GRANTABLE FROM INFORMATION_SCHEMA.COLUMN_PRIVILEGES WHERE TABLE_SCHEMA=%s", "COLUMN"),
        )
        result: list[GrantDef] = []
        try:
            with self._conn.cursor() as cur:
                for sql, scope in queries:
                    cur.execute(sql, (db,) if "%s" in sql else ())
                    for row in cur.fetchall():
                        if scope == "DATABASE":
                            grantee, schema, privilege, grantable = row
                            result.append(GrantDef(privilege, "DATABASE", schema, grantee, schema, str(grantable).upper() == "YES"))
                        else:
                            grantee, schema, table, column, privilege, grantable = row
                            result.append(GrantDef(privilege, "COLUMN", f"{table}.{column}", grantee, schema, str(grantable).upper() == "YES"))
        except Exception as exc:
            raise MySQLSecurityMetadataNotVisible("BLOCKED: MySQL privilege metadata is not visible to the migration account") from exc
        return self._filter_security_grants(result)

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        direct = ("tables", "columns", "defaults", "primary_keys", "auto_increment", "indexes", "unique_constraints", "check_constraints", "foreign_keys", "generated_columns", "partitions", "views", "functions", "procedures", "triggers", "events", "comments", "grants", "security_principals")
        unsupported = {"materialized_views": "MySQL has no native materialized views", "rls_policies": "MySQL has no row-level security policies", "extensions": "MySQL has no PostgreSQL extension model", "custom_types": "MySQL has no PostgreSQL domain/type model", "sequences": "AUTO_INCREMENT is table-bound", "schemas": "MySQL databases are namespaces, not PostgreSQL schemas"}
        return {x: {"supported": True, "mode": "direct"} for x in direct} | {x: {"supported": False, "mode": "unsupported", "reason": r} for x, r in unsupported.items()}


class MySQLTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        self._reconciliation_backups: list[str] = []
        self._routine_definer: str | None = config.get("routine_definer")

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("mysql-connector-python", "mysql.connector")
        import mysql.connector

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "database": self._config.get("database", "mysql"),
            "user": self._config["username"],
            "password": self._config.get("password", ""),
            "connection_timeout": self._config.get("connection_timeout", 10),
        }
        conn_kwargs.update(_connection_options(self._config))

        self._conn = mysql.connector.connect(**conn_kwargs)
        if self._routine_definer is None:
            with self._conn.cursor() as cur:
                cur.execute("SELECT CURRENT_USER()")
                self._routine_definer = cur.fetchone()[0]
        audit_log(phase="connect", status="success", details={"engine": "mysql", "role": "target"})

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._conn.close()
        finally:
            self._conn = None

    def ensure_database_exists(self) -> None:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {db_name}")
                audit_log(phase="ensure_database", status="created", details={"database": db_name})

    def _show_create(self, kind: str, name: str) -> str:
        """Return the authoritative MySQL DDL for an existing target object."""
        with self._conn.cursor() as cur:
            cur.execute(f"SHOW CREATE {kind} {_q(name)}")
            row = cur.fetchone()
            for index, column_name in enumerate(cur.column_names):
                if "create" in column_name.lower() or "original statement" in column_name.lower():
                    return row[index]
            raise RuntimeError(f"SHOW CREATE {kind} did not return a DDL column")

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        # Target-owned because permissions/version checks can refine this later.
        direct = ("tables", "columns", "defaults", "primary_keys", "auto_increment", "indexes", "unique_constraints", "check_constraints", "foreign_keys", "generated_columns", "partitions", "views", "functions", "procedures", "triggers", "events", "comments", "grants", "security_principals")
        unsupported = {"materialized_views": "MySQL has no native materialized views", "rls_policies": "MySQL has no row-level security policies", "extensions": "MySQL has no PostgreSQL extension model", "custom_types": "MySQL has no PostgreSQL domain/type model", "sequences": "AUTO_INCREMENT is table-bound", "schemas": "MySQL databases are namespaces, not PostgreSQL schemas"}
        return {x: {"supported": True, "mode": "direct"} for x in direct} | {x: {"supported": False, "mode": "unsupported", "reason": r} for x, r in unsupported.items()}

    def inspect_schema(self, object_name: str, schema_name: str | None = None) -> Schema | None:
        """Reuse MySQL catalog extraction for target partition verification."""
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
                (self._config.get("database", "mysql"), object_name),
            )
            if cur.fetchone() is None:
                return None
        return MySQLSourceConnector.get_schema(self, object_name)

    def create_object_if_missing(self, schema: Schema) -> str:
        validate_identifier(schema.name, "table")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._config.get("database", "mysql"), schema.name),
            )
            if cur.fetchone() is not None:
                return "already_exists"
            self._create_table(cur, schema, schema.name)
            self._conn.commit()
            audit_log(phase="create_table", status="created", details={"table": schema.name})
            return "created"

    def _create_table(self, cur: Any, schema: Schema, table_name: str) -> None:
            """Emit the same source-derived DDL for a final or staged table."""
            validate_identifier(table_name, "table")
            col_defs = []
            for col in schema.columns:
                if col.target_type is None:
                    if self._config.get("source_engine") == "mysql":
                        col_type = col.source_type
                    else:
                        raise UnmappedTypeError(
                            table=schema.name,
                            column=col.name,
                            source_type=col.source_type,
                            source_engine=self._config.get("source_engine"),
                            target_engine="mysql",
                        )
                else:
                    col_type = col.target_type
                null_str = "NULL" if col.nullable else "NOT NULL"
                generated = f" GENERATED ALWAYS AS ({col.generated}) {col.generated_kind or 'VIRTUAL'}" if col.generated else ""
                default = f" DEFAULT {_default_sql(col.default, col_type)}" if col.default is not None and not col.generated else ""
                increment = " AUTO_INCREMENT" if col.auto_increment else ""
                comment = f" COMMENT {self._literal(col.comment)}" if col.comment else ""
                col_defs.append(f"{_q(col.name)} {col_type}{generated} {null_str}{default}{increment}{comment}")

            if schema.primary_key:
                pk_cols = ", ".join(_q(c) for c in schema.primary_key)
                col_defs.append(f"PRIMARY KEY ({pk_cols})")

            suffix = ""
            if schema.options.get("engine"):
                suffix += f" ENGINE={schema.options['engine']}"
            if schema.options.get("collation"):
                suffix += f" COLLATE={schema.options['collation']}"
            if schema.comment:
                suffix += f" COMMENT={self._literal(schema.comment)}"
            partition_clause = _mysql_partition_clause(schema)
            if partition_clause:
                suffix += f" {partition_clause}"
            ddl = f"CREATE TABLE {_q(table_name)} ({', '.join(col_defs)}){suffix}"
            cur.execute(ddl)

    def reconcile_mysql_table(self, schema: Schema, managed_tables: set[str]) -> str:
        """Stage and atomically swap a source-equivalent table, retaining backup until success."""
        token = hashlib.sha1(schema.name.encode()).hexdigest()[:10]
        staged = f"__dms_stage_{token}"
        backup = f"__dms_backup_{token}"
        validate_identifier(staged, "table"); validate_identifier(backup, "table")
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"DROP TABLE IF EXISTS {_q(staged)}")
                self._create_table(cur, schema, staged)
                self._conn.commit()
                staged_schema = MySQLSourceConnector.get_schema(self, staged)
                if (
                    staged_schema.mysql_partition_method != schema.mysql_partition_method
                    or staged_schema.mysql_partition_expression != schema.mysql_partition_expression
                    or [(p.name, p.description) for p in staged_schema.mysql_partitions] != [(p.name, p.description) for p in schema.mysql_partitions]
                ):
                    raise RuntimeError("staged table partition metadata does not match source")
                cur.execute(
                    "SELECT TABLE_NAME,CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s "
                    "AND CONSTRAINT_NAME<>'PRIMARY'",
                    (self._config["database"], self._config["database"], schema.name),
                )
                incoming = cur.fetchall()
                external = [table for table, _ in incoming if table not in managed_tables]
                if external:
                    raise RuntimeError(f"cannot safely reconcile {schema.name}: referenced by unmanaged target tables {external}")
                for child, constraint in incoming:
                    if child != schema.name:
                        cur.execute(f"ALTER TABLE {_q(child)} DROP FOREIGN KEY {_q(constraint)}")
                cur.execute(f"DROP TABLE IF EXISTS {_q(backup)}")
                cur.execute(f"RENAME TABLE {_q(schema.name)} TO {_q(backup)}, {_q(staged)} TO {_q(schema.name)}")
                self._conn.commit()
                self._reconciliation_backups.append(backup)
                audit_log(phase="reconcile_table", status="recreated", details={"table": schema.name, "backup": backup})
                return "reconciled"
            except Exception:
                self._conn.rollback()
                try:
                    cur.execute(f"DROP TABLE IF EXISTS {_q(staged)}")
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                raise

    def finalize_schema_reconciliations(self) -> list[str]:
        removed: list[str] = []
        with self._conn.cursor() as cur:
            for backup in self._reconciliation_backups:
                cur.execute(f"DROP TABLE {_q(backup)}")
                removed.append(backup)
            self._conn.commit()
        self._reconciliation_backups.clear()
        return removed

    @staticmethod
    def _literal(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def apply_constraints(self, schema: Schema) -> None:
        """Apply post-load objects; FKs last so referenced tables always exist."""
        table = _q(schema.name)
        with self._conn.cursor() as cur:
            for index in schema.indexes:
                try:
                    cur.execute("SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), NON_UNIQUE, INDEX_TYPE FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s GROUP BY NON_UNIQUE, INDEX_TYPE", (self._config["database"], schema.name, index.name))
                    existing = cur.fetchone()
                    if existing is not None:
                        actual_columns, non_unique, actual_type = existing
                        if (
                            actual_columns == ",".join(index.columns)
                            and bool(non_unique) == (not index.unique)
                            and actual_type.upper() == (index.index_type or "BTREE").upper()
                        ):
                            audit_log(phase="create_index", status="verified_existing", details={"table": schema.name, "index": index.name})
                            continue
                        raise RuntimeError(f"existing index definition differs: {actual_columns}")
                    index_type = (index.index_type or "").upper()
                    if index_type in {"FULLTEXT", "SPATIAL"}:
                        prefix = f"{index_type} INDEX"
                    else:
                        prefix = "UNIQUE INDEX" if index.unique else "INDEX"
                    cur.execute(f"CREATE {prefix} {_q(index.name)} ON {table} ({', '.join(_q(c) for c in index.columns)})")
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(f"index {index.name}: {exc}") from exc
            for check in schema.check_constraints:
                try:
                    cur.execute("SELECT 1 FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s AND CONSTRAINT_TYPE='CHECK'", (self._config["database"], schema.name, check.name))
                    if cur.fetchone() is not None:
                        audit_log(phase="create_check", status="verified_existing", details={"table": schema.name, "constraint": check.name})
                        continue
                    cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {_q(check.name)} CHECK ({check.expression})")
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(f"check {check.name}: {exc}") from exc
            for fk in schema.foreign_keys:
                try:
                    cur.execute("SELECT 1 FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s AND CONSTRAINT_TYPE='FOREIGN KEY'", (self._config["database"], schema.name, fk.name))
                    if cur.fetchone() is not None:
                        audit_log(phase="create_fk", status="verified_existing", details={"table": schema.name, "constraint": fk.name})
                        continue
                    # MySQL's schema value is the source database namespace.
                    # A database migration maps it to the configured target
                    # database; retaining it would create cross-database FKs
                    # back to the source server/database.
                    ref = _qname(self._config["database"], fk.ref_table)
                    cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {_q(fk.name)} FOREIGN KEY ({', '.join(_q(c) for c in fk.columns)}) REFERENCES {ref} ({', '.join(_q(c) for c in fk.ref_columns)}) ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}")
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(f"foreign key {fk.name}: {exc}") from exc

    def create_view(self, view: ViewDefinition) -> None:
        definition = _rewrite_view_database_references(
            view.definition,
            source_database=view.schema_name,
            target_database=self._config["database"],
        )
        with self._conn.cursor() as cur:
            cur.execute(f"CREATE OR REPLACE VIEW {_q(view.name)} AS {definition.rstrip().rstrip(';')}")
            self._conn.commit()

    def create_function(self, func: FunctionDef) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"DROP {func.kind.upper()} IF EXISTS {_q(func.name)}")
                cur.execute(_rewrite_mysql_definer(func.ddl, self._routine_definer or ""))
                self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            policy_error = _mysql_routine_policy_error(exc, "FUNCTION")
            if policy_error is not None:
                raise policy_error from exc
            raise

    def create_trigger(self, trigger: TriggerDef) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {_q(trigger.name)}")
                cur.execute(_rewrite_mysql_definer(trigger.ddl, self._routine_definer or ""))
                self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            policy_error = _mysql_routine_policy_error(exc, "TRIGGER")
            if policy_error is not None:
                raise policy_error from exc
            raise

    def suspend_triggers_for_data_load(self, triggers: list[TriggerDef]) -> list[TriggerDef]:
        """MySQL lacks DISABLE TRIGGER; snapshot/drop only matching triggers.

        The orchestration recreates source definitions after the load. Returning
        the target definitions provides an audit trail and permits callers to
        restore them if later trigger application is unavailable.
        """
        requested = {trigger.name for trigger in triggers}
        suspended: list[TriggerDef] = []
        with self._conn.cursor() as cur:
            cur.execute("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=%s", (self._config["database"],))
            for name, table in cur.fetchall():
                if name not in requested:
                    continue
                ddl = self._show_create("TRIGGER", name)
                cur.execute(f"DROP TRIGGER {_q(name)}")
                suspended.append(TriggerDef(name=name, table=table, ddl=ddl, schema_name=self._config["database"]))
            self._conn.commit()
        if suspended:
            audit_log(phase="suspend_triggers", status="success", details={"triggers": [t.name for t in suspended]})
        return suspended

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        """Delete all migrated-table rows for a deterministic MySQL full sync.

        ``TRUNCATE`` is deliberately not used: it is incompatible with tables
        referenced by foreign keys.  FK checks are disabled only in this target
        connection while deleting the known migration tables, then restored in
        a ``finally`` block.  The orchestrator suspends matching triggers first.
        """
        tables = [validate_identifier(name, "table") for name in objects]
        if not tables:
            return []
        with self._conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                for table in tables:
                    cur.execute(f"DELETE FROM {_q(table)}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
                self._conn.commit()
        audit_log(phase="clear_full_sync", status="success", details={"tables": tables})
        return tables

    def create_event(self, event: EventDef) -> None:
        safety_lead_seconds = event.safety_lead_seconds
        if (
            event.event_type == "ONE TIME"
            and event.status == "ENABLED"
            and event.execute_at is not None
        ):
            snapshot_deadline = event.snapshot_at + timedelta(seconds=safety_lead_seconds) if event.snapshot_at else None
            if snapshot_deadline is not None and event.execute_at <= snapshot_deadline:
                raise MySQLEventTimingSafetyError(
                    f"EVENT: BLOCKED {event.name}: enabled one-time event executes at "
                    f"{event.execute_at}; it was due or within the {safety_lead_seconds}-second "
                    "safety window at source snapshot. Reschedule it farther into the future and rerun."
                )
        with self._conn.cursor() as cur:
            original_time_zone: str | None = None
            try:
                # CREATE EVENT interprets literal schedule values in the session
                # time zone.  Reuse the source event's recorded time zone.
                if event.time_zone:
                    cur.execute("SELECT @@session.time_zone")
                    original_time_zone = cur.fetchone()[0]
                    cur.execute("SET time_zone = %s", (event.time_zone,))
                if event.event_type == "ONE TIME" and event.status == "ENABLED" and event.execute_at is not None:
                    cur.execute("SELECT NOW()")
                    target_now = cur.fetchone()[0]
                    if event.execute_at <= target_now + timedelta(seconds=safety_lead_seconds):
                        raise MySQLEventTimingSafetyError(
                            f"EVENT: BLOCKED {event.name}: enabled one-time event executes at "
                            f"{event.execute_at}; it is due or within the {safety_lead_seconds}-second "
                            "safety window at target creation. It was not replaced."
                        )
                cur.execute(f"DROP EVENT IF EXISTS {_q(event.name)}")
                cur.execute(_rewrite_mysql_definer(event.ddl, self._routine_definer or ""))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                if original_time_zone is not None:
                    cur.execute("SET time_zone = %s", (original_time_zone,))

    def sync_auto_increment(self, table: str, column: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COALESCE(MAX({_q(column)}), 0) + 1 FROM {_q(table)}")
            next_value = int(cur.fetchone()[0])
            cur.execute(f"ALTER TABLE {_q(table)} AUTO_INCREMENT = {next_value}")
            self._conn.commit()

    def apply_comment(self, comment: CommentDef) -> None:
        # MySQL stores comments in table/column DDL; retain source comments for
        # existing target objects by applying native ALTER statements.
        if comment.object_type not in {"TABLE", "COLUMN"}:
            raise ValueError(f"Unsupported MySQL comment object type: {comment.object_type}")

        try:
            table_name, column_name = (
                comment.object_name.rsplit(".", 1)
                if comment.object_type == "COLUMN"
                else (comment.object_name, None)
            )
        except ValueError as exc:
            raise ValueError(f"Invalid MySQL column comment name: {comment.object_name}") from exc
        validate_identifier(table_name, "table")
        if column_name is not None:
            validate_identifier(column_name, "column")

        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                    (self._config["database"], table_name),
                )
                row = cur.fetchone()
                if row is None or row[0] != "BASE TABLE":
                    raise RuntimeError(f"Target object is not a BASE TABLE: {table_name}")

                if comment.object_type == "TABLE":
                    cur.execute(
                        f"ALTER TABLE {_q(table_name)} "
                        f"COMMENT = {self._literal(comment.comment)}"
                    )
                    self._conn.commit()
                    return

                cur.execute(
                    "SELECT COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,"
                    "GENERATION_EXPRESSION,CHARACTER_SET_NAME,COLLATION_NAME "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                    (self._config["database"], table_name, column_name),
                )
                metadata = cur.fetchone()
                if metadata is None:
                    raise RuntimeError(f"Target column not found: {table_name}.{column_name}")
                (
                    column_type,
                    is_nullable,
                    default,
                    extra,
                    generation_expression,
                    character_set,
                    collation,
                ) = metadata
                extra_text = extra or ""
                definition = str(column_type)
                if character_set:
                    definition += f" CHARACTER SET {_q(str(character_set))}"
                if collation:
                    definition += f" COLLATE {_q(str(collation))}"
                if generation_expression:
                    generated_kind = "STORED" if "STORED" in extra_text.upper() else "VIRTUAL"
                    definition += f" GENERATED ALWAYS AS ({generation_expression}) {generated_kind}"
                    definition += " NULL" if is_nullable == "YES" else " NOT NULL"
                else:
                    definition += " NULL" if is_nullable == "YES" else " NOT NULL"
                    if default is not None:
                        definition += f" DEFAULT {_default_sql(default, str(column_type))}"
                    if "AUTO_INCREMENT" in extra_text.upper():
                        definition += " AUTO_INCREMENT"
                    on_update = re.search(r"\bon update\s+(.+)$", extra_text, re.IGNORECASE)
                    if on_update:
                        definition += f" ON UPDATE {on_update.group(1)}"
                definition += f" COMMENT {self._literal(comment.comment)}"
                cur.execute(
                    f"ALTER TABLE {_q(table_name)} MODIFY COLUMN {_q(column_name)} {definition}"
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def apply_grant(self, grant: GrantDef) -> None:
        # Grantees are not created by the platform; target permissions decide
        # whether this direct MySQL statement can be applied.
        target_database = validate_identifier(self._config["database"], "database")
        object_name = grant.object_name.rsplit(".", 1)[-1]
        with self._conn.cursor() as cur:
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
                cur.execute(f"GRANT {grant.privileges} ON {scope}{target} TO {grant.grantee}{grant_option}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def create_security_principal(self, principal: SecurityPrincipalDef) -> None:
        """Create an account/role without copying a password or password hash.

        Accounts use the target server's default authentication policy; callers
        must provision a credential separately before behavioral login testing.
        """
        account = _qaccount(principal.user, principal.host)
        statement = f"CREATE ROLE IF NOT EXISTS {account}" if principal.principal_type == "ROLE" else f"CREATE USER IF NOT EXISTS {account}"
        with self._conn.cursor() as cur:
            try:
                cur.execute(statement)
                self._conn.commit()
            except Exception as exc:
                self._conn.rollback()
                if principal.principal_type == "USER":
                    raise RuntimeError(
                        f"Cannot create {account}; target authentication/account policy differs. "
                        "No source password or authentication hash is migrated."
                    ) from exc
                raise

    def apply_role_membership(self, membership: RoleMembershipDef) -> None:
        role = _qaccount(membership.role_user, membership.role_host)
        grantee = _qaccount(membership.grantee_user, membership.grantee_host)
        admin = " WITH ADMIN OPTION" if membership.with_admin_option else ""
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"GRANT {role} TO {grantee}{admin}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def security_migration_authorization(self) -> tuple[bool, str]:
        """Preflight effective target grants so known-denied writes are skipped."""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SHOW GRANTS FOR CURRENT_USER()")
                grants = "\n".join(str(row[0]).upper() for row in cur.fetchall())
        except Exception:
            return False, "Target authorization could not be verified; grant CREATE USER, CREATE ROLE, and GRANT OPTION, then rerun the full migration."
        full_admin = "ALL PRIVILEGES ON *.*" in grants and "GRANT OPTION" in grants
        required = ("CREATE USER", "CREATE ROLE", "GRANT OPTION")
        if full_admin or all(item in grants for item in required):
            return True, ""
        return False, "Target migration account lacks authorization to create users/roles or apply permissions. Grant CREATE USER, CREATE ROLE, and GRANT OPTION, then rerun the full migration."

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        validate_identifier(object_name, "table")
        result = UpsertResult()
        batch = list(rows)

        if not batch:
            return result

        with self._conn.cursor() as cur:
            generated_columns = {col.name for col in (schema.columns if schema else []) if col.generated}
            set_columns = {}
            if self._config.get("source_engine") == "mysql":
                set_columns = {
                    col.name: col.source_type
                    for col in (schema.columns if schema else [])
                    if col.source_type.strip().lower().startswith("set(")
                }
            columns = [column for column in batch[0].keys() if column not in generated_columns]
            col_names = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            update_set = ", ".join(
                f"{col} = VALUES({col})" for col in columns
            )

            sql = (
                f"INSERT INTO {object_name} ({col_names}) "
                f"VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_set}"
            )

            try:
                for row in batch:
                    values = [
                        _normalize_mysql_set_value(row.get(col), set_columns[col])
                        if col in set_columns
                        else row.get(col)
                        for col in columns
                    ]
                    cur.execute(sql, values)
                self._conn.commit()
                result.success_count = len(batch)
                audit_log(phase="upsert_batch", status="success", details={"table": object_name, "count": len(batch)})
            except Exception as exc:
                self._conn.rollback()
                result.failure_count = len(batch)
                result.errors.append(str(exc))
                result.failed_items.extend(batch)
                audit_log(phase="upsert_batch", status="failure", details={"table": object_name, "error": str(exc)})

        return result

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {object_name}")
            return cur.fetchone()[0]

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            if schema and schema.primary_key:
                conditions = []
                values = []
                for pk_col in schema.primary_key:
                    conditions.append(f"{pk_col} = %s")
                    values.append(document.get(pk_col))
                where_clause = " AND ".join(conditions)
                cur.execute(f"DELETE FROM {object_name} WHERE {where_clause}", values)
            else:
                cur.execute(f"DELETE FROM {object_name} WHERE id = %s", (document.get("id"),))
            self._conn.commit()
            audit_log(phase="cdc_delete", status="deleted", details={"table": object_name})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        with self._conn.cursor(dictionary=True) as cur:
            cur.execute(f"SELECT * FROM {object_name}")
            for row in cur:
                yield dict(row)


class MySQLCDCEngine(CDCEngine):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

        self._last_binlog_file: str | None = None
        self._last_binlog_pos: int | None = None

        # Persistent CDC checkpoint
        self._checkpoint_dir = Path("state") / "cdc"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

        safe_name = (
            f"{self._config['host']}_"
            f"{self._config.get('port', 3306)}_"
            f"{self._config['database']}"
        ).replace(":", "_").replace("/", "_").replace("\\", "_")

        self._checkpoint_path = (
            self._checkpoint_dir / f"mysql_{safe_name}.json"
        )

        self._load_checkpoint()

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        import mysql.connector

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "database": self._config["database"],
            "user": self._config["username"],
            "password": self._config.get("password", ""),
            "ssl_disabled": not self._config.get("ssl", True),
        }

        self._conn = mysql.connector.connect(**conn_kwargs)
    def _load_checkpoint(self) -> None:
        if not self._checkpoint_path.exists():
            return

        try:
            with self._checkpoint_path.open("r", encoding="utf-8") as f:
                state = json.load(f)

            self._last_binlog_file = state.get("binlog_file")
            self._last_binlog_pos = state.get("binlog_pos")

            audit_log(
                phase="cdc_checkpoint",
                status="loaded",
                details={
                    "binlog_file": self._last_binlog_file,
                    "binlog_pos": self._last_binlog_pos,
                },
            )

        except Exception as exc:
            audit_log(
                phase="cdc_checkpoint",
                status="load_failed",
                details={"error": str(exc)},
            )

    def start(self) -> None:
    # Only initialize a new CDC position if no checkpoint exists.
    # Never overwrite a persisted checkpoint.
        if self._last_binlog_file is None:
            with self._conn.cursor() as cur:
                cur.execute("SHOW BINARY LOG STATUS")
                row = cur.fetchone()
                if row:
                    self._last_binlog_file = row[0]
                    self._last_binlog_pos = row[1]

        audit_log(
            phase="cdc_start",
            status="success",
            details={
                "engine": "mysql",
                "binlog_file": self._last_binlog_file,
                "binlog_pos": self._last_binlog_pos,
            },
        )

    def poll_changes(self) -> list[ChangeEvent]:
        from pymysqlreplication import BinLogStreamReader
        from pymysqlreplication.row_event import (
            WriteRowsEvent,
            UpdateRowsEvent,
            DeleteRowsEvent,
        )

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "user": self._config["username"],
            "passwd": self._config.get("password", ""),
            "connect_timeout": 5,
            "read_timeout": 5,
        }

        if self._config.get("ssl", False):
            conn_kwargs["ssl"] = {}

        stream = BinLogStreamReader(
            connection_settings=conn_kwargs,
            server_id=self._config.get("server_id", 100),
            blocking=False,
            resume_stream=self._last_binlog_file is not None,
            log_file=self._last_binlog_file,
            log_pos=self._last_binlog_pos or 4,
            only_events=[
                WriteRowsEvent,
                UpdateRowsEvent,
                DeleteRowsEvent,
            ],
            only_schemas=[self._config["database"]],
        )

        events: list[ChangeEvent] = []
        try:
            for binlogevent in stream:
                if isinstance(binlogevent, WriteRowsEvent):
                    operation = "insert"
                    for row in binlogevent.rows:
                        document = row["values"]
                        events.append(
                            ChangeEvent(
                                operation=operation,
                                document=document,
                                object_name=binlogevent.table,
                                watermark={
                                    "file": binlogevent.log_file,
                                    "pos": binlogevent.log_pos,
                                },
                            )
                        )
                elif isinstance(binlogevent, UpdateRowsEvent):
                    operation = "update"
                    for row in binlogevent.rows:
                        document = row["after_values"]
                        events.append(
                            ChangeEvent(
                                operation=operation,
                                document=document,
                                object_name=binlogevent.table,
                                watermark={
                                    "file": binlogevent.log_file,
                                    "pos": binlogevent.log_pos,
                                },
                            )
                        )
                elif isinstance(binlogevent, DeleteRowsEvent):
                    operation = "delete"
                    for row in binlogevent.rows:
                        document = row["values"]
                        events.append(
                            ChangeEvent(
                                operation=operation,
                                document=document,
                                object_name=binlogevent.table,
                                watermark={
                                    "file": binlogevent.log_file,
                                    "pos": binlogevent.log_pos,
                                },
                            )
                        )
        finally:
            stream.close()

        return events

    def apply(
        self,
        events: list[ChangeEvent],
        target: TargetConnector,
    ) -> ApplyResult:
        result = ApplyResult()

        if not events:
            return result

        for event in events:
            try:
                if event.operation in ("insert", "update"):
                    apply_result = target.upsert_batch(
                        event.object_name,
                        iter([event.document]),
                        event.schema,
                    )

                    if apply_result.failure_count > 0:
                        result.failure_count += apply_result.failure_count
                        result.errors.extend(apply_result.errors)
                        continue

                    result.success_count += apply_result.success_count

                elif event.operation == "delete":
                    target.delete(
                        event.object_name,
                        event.document,
                        event.schema,
                    )
                    result.success_count += 1

            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0 and result.success_count > 0:
            result.last_checkpoint = events[-1].watermark

            audit_log(
                phase="cdc_apply",
                status="success",
                details={
                    "applied": result.success_count,
                },
            )
        else:
            audit_log(
                phase="cdc_apply",
                status="partial_failure",
                details={
                    "success": result.success_count,
                    "failure": result.failure_count,
                },
            )

        return result

    
    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return

        watermark = result.last_checkpoint

        if isinstance(watermark, dict):
            self._last_binlog_file = watermark.get("file")
            self._last_binlog_pos = watermark.get("pos")
        else:
            self._last_binlog_file = str(watermark)
            self._last_binlog_pos = None

        with self._checkpoint_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "binlog_file": self._last_binlog_file,
                    "binlog_pos": self._last_binlog_pos,
                },
                f,
                indent=2,
            )

        audit_log(
            phase="cdc_checkpoint",
            status="advanced",
            details={
                "binlog_file": self._last_binlog_file,
                "binlog_pos": self._last_binlog_pos,
            },
        )
