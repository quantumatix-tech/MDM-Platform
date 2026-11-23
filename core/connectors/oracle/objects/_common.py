"""Shared Oracle object DDL and dependency helpers."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from core.connectors.base import quote_identifier
from core.connectors.oracle._models import validate_oracle_identifier

_SYSTEM_OWNERS = {"SYS", "SYSTEM"}


def managed_source_objects(definitions: dict[str, list[Any]]) -> set[tuple[str, str]]:
    """Return owner-local names known to be source-managed for dependency checks."""
    managed: set[tuple[str, str]] = set()
    for view in definitions.get("views", []):
        managed.add(("VIEW", view.name))
    for mv in definitions.get("materialized_views", []):
        managed.add(("MATERIALIZED VIEW", mv.name))
    for synonym in definitions.get("synonyms", []):
        managed.add(("SYNONYM", synonym.name))
    for routine in definitions.get("routines", []):
        typ = {
            "function": "FUNCTION",
            "procedure": "PROCEDURE",
            "package_spec": "PACKAGE",
            "package_body": "PACKAGE BODY",
        }.get(routine.kind)
        if typ:
            managed.add((typ, routine.name))
    managed.update(("TYPE", item.name) for item in definitions.get("types", []))
    return managed


def _assert_no_unmanaged_dependents(
    conn: Any, name: str, object_types: tuple[str, ...], managed: set[tuple[str, str]]
) -> None:
    binds = ", ".join(f":{idx + 2}" for idx in range(len(object_types)))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT name, type FROM user_dependencies WHERE referenced_name = :1 "
            f"AND referenced_type IN ({binds}) ORDER BY type, name",
            (name, *object_types),
        )
        dependents = cur.fetchall()
    unmanaged = [
        (str(typ), str(dep_name))
        for dep_name, typ in dependents
        if (str(typ), str(dep_name)) not in managed
    ]
    if unmanaged:
        kind, dep_name = unmanaged[0]
        raise RuntimeError(
            f"Cannot reconcile source-managed Oracle object {name}: "
            f"target-only dependent {kind} {dep_name} must be reviewed"
        )


def _value(value: Any) -> Any:
    return value.read() if hasattr(value, "read") and callable(value.read) else value


def _remap_owner_qualifiers(ddl: str, source_owner: str, target_owner: str | None) -> str:
    """Rewrite only qualified owner identifiers, never text or comments."""
    if not source_owner:
        return ddl
    if target_owner and source_owner.casefold() == target_owner.casefold():
        return ddl

    def replacement(token: str, quoted: bool = False) -> str:
        matches = token == source_owner if quoted else token.casefold() == source_owner.casefold()
        if not matches:
            return token
        if not target_owner:
            raise RuntimeError(
                f"Cannot remap source owner {source_owner!r} in Oracle object DDL: target owner is unknown"
            )
        validate_oracle_identifier(target_owner, "target owner")
        return quote_identifier(target_owner)

    def reject_dynamic_owner_reference(text: str) -> None:
        pattern = r'(?i)(?<![A-Z0-9_$#])(?:"?' + re.escape(source_owner) + r'"?)\s*\.'
        if re.search(pattern, text):
            raise RuntimeError(
                f"Oracle DDL contains a source-owner reference inside a string literal "
                f"for {source_owner}; dynamic SQL owner remapping is unsupported"
            )

    out: list[str] = []
    index = 0
    length = len(ddl)
    while index < length:
        char = ddl[index]
        if ddl.startswith("--", index):
            end = ddl.find("\n", index)
            if end < 0:
                out.append(ddl[index:])
                break
            out.append(ddl[index : end + 1])
            index = end + 1
            continue
        if ddl.startswith("/*", index):
            end = ddl.find("*/", index + 2)
            if end < 0:
                raise RuntimeError("Unterminated comment in Oracle object DDL")
            out.append(ddl[index : end + 2])
            index = end + 2
            continue
        if char in "qQ" and index + 1 < length and ddl[index + 1] == "'":
            if index + 2 >= length:
                raise RuntimeError("Malformed Oracle q-quoted literal in object DDL")
            opener = ddl[index + 2]
            closer = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opener, opener)
            terminator = closer + "'"
            end = ddl.find(terminator, index + 3)
            if end < 0:
                raise RuntimeError("Unterminated Oracle q-quoted literal in object DDL")
            reject_dynamic_owner_reference(ddl[index + 3 : end])
            end += len(terminator)
            out.append(ddl[index:end])
            index = end
            continue
        if char == "'":
            end = index + 1
            while end < length:
                if ddl[end] == "'":
                    if end + 1 < length and ddl[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            reject_dynamic_owner_reference(ddl[index + 1 : end - 1])
            out.append(ddl[index:end])
            index = end
            continue
        if char == '"':
            end = index + 1
            value: list[str] = []
            while end < length:
                if ddl[end] == '"':
                    if end + 1 < length and ddl[end + 1] == '"':
                        value.append('"')
                        end += 2
                        continue
                    end += 1
                    break
                value.append(ddl[end])
                end += 1
            token = ddl[index:end]
            lookahead = end
            while lookahead < length and ddl[lookahead].isspace():
                lookahead += 1
            if lookahead < length and ddl[lookahead] == ".":
                quoted_value = "".join(value)
                if quoted_value == source_owner:
                    out.append(replacement(quoted_value, quoted=True))
                else:
                    out.append(token)
            else:
                out.append(token)
            index = end
            continue
        if char.isalpha() or char in "_$#":
            end = index + 1
            while end < length and (ddl[end].isalnum() or ddl[end] in "_$#"):
                end += 1
            token = ddl[index:end]
            lookahead = end
            while lookahead < length and ddl[lookahead].isspace():
                lookahead += 1
            out.append(
                replacement(token) if lookahead < length and ddl[lookahead] == "." else token
            )
            index = end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _mask_literals_and_comments(ddl: str) -> str:
    """Keep DDL structure while hiding string and comment contents."""
    masked: list[str] = []
    index = 0
    while index < len(ddl):
        if ddl.startswith("--", index):
            end = ddl.find("\n", index)
            if end < 0:
                masked.append(" " * (len(ddl) - index))
                break
            masked.append(" " * (end - index) + "\n")
            index = end + 1
        elif ddl.startswith("/*", index):
            end = ddl.find("*/", index + 2)
            if end < 0:
                raise RuntimeError("Unterminated comment in Oracle object DDL")
            end += 2
            masked.append("".join("\n" if char == "\n" else " " for char in ddl[index:end]))
            index = end
        elif ddl[index] in "qQ" and index + 1 < len(ddl) and ddl[index + 1] == "'":
            opener = ddl[index + 2]
            closer = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opener, opener)
            end = ddl.find(closer + "'", index + 3)
            if end < 0:
                raise RuntimeError("Unterminated Oracle q-quoted literal in object DDL")
            end += 2
            masked.append("".join("\n" if char == "\n" else " " for char in ddl[index:end]))
            index = end
        elif ddl[index] == "'":
            end = index + 1
            while end < len(ddl):
                if ddl[end] == "'":
                    if end + 1 < len(ddl) and ddl[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            masked.append("".join("\n" if char == "\n" else " " for char in ddl[index:end]))
            index = end
        else:
            masked.append(ddl[index])
            index += 1
    return "".join(masked)


def _validate_single_create(ddl: str, expected_kind: str, name: str) -> str:
    clean = ddl.strip()
    if clean.splitlines() and clean.splitlines()[-1].strip() == "/":
        clean = "\n".join(clean.splitlines()[:-1]).rstrip()
    masked = _mask_literals_and_comments(clean)
    starts = list(
        re.finditer(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
            r"(?:(?:FORCE|NO\s+FORCE)\s+)?(?:EDITIONING\s+)?"
            r"(?:(?:NONEDITIONABLE|EDITIONABLE)\s+)?"
            r"(PACKAGE(?:\s+BODY)?|FUNCTION|PROCEDURE|TRIGGER|VIEW|TYPE)\b",
            masked,
            re.IGNORECASE,
        )
    )
    expected = " ".join(expected_kind.upper().split())
    found = [" ".join(match.group(1).upper().split()) for match in starts]
    if len(starts) != 1 or found[0] != expected:
        raise RuntimeError(
            f"Oracle DDL boundary/type error for {expected_kind} {name}: "
            f"expected exactly one CREATE {expected_kind}, found {found or 'none'}"
        )
    if expected == "VIEW":
        # Views contain a query, not a PL/SQL block, so any unquoted
        # semicolon must be the single final terminator. This rejects an
        # appended DROP/ALTER/second statement while allowing semicolons in
        # literals and comments.
        terminators = [match.start() for match in re.finditer(";", masked)]
        if terminators and (len(terminators) != 1 or masked[terminators[0] + 1 :].strip()):
            raise RuntimeError(
                f"Oracle DDL boundary/type error for VIEW {name}: "
                "unexpected content after the CREATE VIEW statement"
            )
        unexpected = re.search(
            r"(?im)^\s*(?:CREATE|ALTER|DROP|GRANT|INSERT|UPDATE|DELETE|TRUNCATE|BEGIN|DECLARE)\b",
            masked[starts[0].end() :],
        )
        if unexpected:
            raise RuntimeError(
                f"Oracle DDL boundary/type error for VIEW {name}: "
                "unexpected statement after the CREATE VIEW header"
            )
    if expected == "TYPE":
        terminators = [match.start() for match in re.finditer(";", masked)]
        if terminators and (len(terminators) != 1 or masked[terminators[0] + 1 :].strip()):
            raise RuntimeError(
                f"Oracle DDL boundary/type error for TYPE {name}: "
                "unexpected content after the CREATE TYPE statement"
            )
    if expected == "TRIGGER":
        unexpected = re.search(
            r"(?im)^\s*(?:ALTER|DROP|GRANT|CREATE)\b",
            masked[starts[0].end() :],
        )
        if unexpected:
            raise RuntimeError(
                f"Oracle DDL boundary/type error for TRIGGER {name}: "
                "unexpected statement after the CREATE TRIGGER body"
            )
    return clean


def _strip_trigger_metadata_state(
    ddl: str,
    name: str,
    owner: str | None = None,
) -> str:
    """Remove only DBMS_METADATA's optional trailing ALTER TRIGGER state DDL."""
    masked = _mask_literals_and_comments(ddl)
    matches = list(re.finditer(r"(?im)^[ \t]*ALTER[ \t]+TRIGGER\b", masked))
    if not matches:
        return ddl.strip()
    if len(matches) != 1:
        raise RuntimeError(f"Oracle trigger {name} DDL contains multiple ALTER TRIGGER statements")

    start = matches[0].start()
    prefix = ddl[:start].rstrip()
    if prefix.splitlines() and prefix.splitlines()[-1].strip() == "/":
        prefix = "\n".join(prefix.splitlines()[:-1]).rstrip()

    quoted = r'"((?:""|[^"])*)"'
    plain = r"([A-Za-z][A-Za-z0-9_$#]*)"
    state_statement = re.compile(
        rf"(?is)^\s*ALTER\s+TRIGGER\s+"
        rf"(?:(?:(?P<owner_q>{quoted})|(?P<owner_u>{plain}))\s*\.\s*)?"
        rf"(?:(?P<object_q>{quoted})|(?P<object_u>{plain}))"
        rf"\s+(?:ENABLE|DISABLE)\s*;?\s*(?:/\s*)?$"
    )
    parsed = state_statement.fullmatch(ddl[start:])
    if not parsed:
        raise RuntimeError(
            f"Oracle trigger {name} DDL has unsupported content after its CREATE TRIGGER body"
        )

    # Separate owner and object groups keep validation exact while accepting
    # both qualified and unqualified metadata forms and quoted names.
    owner_quoted, owner_plain = parsed.group("owner_q"), parsed.group("owner_u")
    object_quoted, object_plain = parsed.group("object_q"), parsed.group("object_u")
    owner_is_quoted = owner_quoted is not None
    object_is_quoted = object_quoted is not None
    owner_value = owner_quoted[1:-1].replace('""', '"') if owner_quoted is not None else owner_plain
    object_value = (
        object_quoted[1:-1].replace('""', '"') if object_quoted is not None else object_plain
    )

    def matches_identifier(actual: str, quoted: bool, expected: str) -> bool:
        return actual == expected if quoted else actual.upper() == expected.upper()

    if not matches_identifier(object_value, object_is_quoted, name) or (
        owner_value is not None
        and (owner is None or not matches_identifier(owner_value, owner_is_quoted, owner))
    ):
        raise RuntimeError(
            f"Oracle trigger metadata state statement does not match {owner + '.' if owner else ''}{name}"
        )
    return prefix


def _get_ddl(conn: Any, object_type: str, name: str, owner: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM("
            "DBMS_METADATA.SESSION_TRANSFORM, 'EMIT_SCHEMA', FALSE); END;"
        )
        cur.execute(
            "SELECT DBMS_METADATA.GET_DDL(:1, :2, :3) FROM dual",
            (object_type, name, owner),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Oracle source DDL is unavailable for {object_type} {owner}.{name}")
    ddl = str(_value(row[0])).strip()
    if object_type.upper() == "TRIGGER":
        ddl = _strip_trigger_metadata_state(ddl, name, owner)
    if ddl.splitlines() and ddl.splitlines()[-1].strip() == "/":
        ddl = "\n".join(ddl.splitlines()[:-1]).rstrip()
    if not ddl:
        raise RuntimeError(f"Oracle source DDL is empty for {object_type} {owner}.{name}")
    return ddl


def _check_local_dependencies(conn: Any, owner: str, name: str, object_type: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT referenced_owner, referenced_name, referenced_type "
            "FROM user_dependencies WHERE name = :1 AND type = :2 "
            "AND referenced_owner IS NOT NULL AND UPPER(referenced_owner) <> :3 "
            "ORDER BY referenced_owner, referenced_name",
            (name, object_type, owner.upper()),
        )
        external = [r for r in cur.fetchall() if str(r[0]).upper() not in _SYSTEM_OWNERS]
    if external:
        owner_name, dep_name, dep_type = external[0]
        raise RuntimeError(
            f"Oracle {object_type} {owner}.{name} depends on cross-schema "
            f"{dep_type} {owner_name}.{dep_name}; provision and approve that dependency explicitly"
        )


def _dependency_order(
    conn: Any, owner: str, names_by_type: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """Topologically order same-family source dependencies, rejecting cycles."""
    nodes = {(typ, name) for typ, names in names_by_type.items() for name in names}
    dependencies: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, type, referenced_name, referenced_type "
            "FROM user_dependencies WHERE UPPER(referenced_owner) = :1",
            (owner.upper(),),
        )
        rows = cur.fetchall()
    for name, typ, dep_name, dep_type in rows:
        node, dep = (str(typ), str(name)), (str(dep_type), str(dep_name))
        if node in nodes and dep in nodes and node != dep:
            dependencies[node].add(dep)

    ordered: list[tuple[str, str]] = []
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(node: tuple[str, str]) -> None:
        if node in visited:
            return
        if node in visiting:
            raise RuntimeError(
                f"Oracle object dependency cycle prevents safe FULL ordering at {node[0]} {node[1]}"
            )
        visiting.add(node)
        for dep in sorted(dependencies[node]):
            visit(dep)
        visiting.remove(node)
        visited.add(node)
        ordered.append(node)

    for node in sorted(nodes):
        visit(node)
    return ordered


def _assert_target_type(conn: Any, name: str, expected: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT object_type FROM user_objects WHERE object_name = :1", (name,))
        types = {str(r[0]).upper() for r in cur.fetchall()}
        if expected == "MATERIALIZED VIEW" and "TABLE" in types:
            # Oracle exposes an MV's storage segment as a TABLE in USER_OBJECTS.
            # Treat that row as MV storage only when USER_MVIEWS independently
            # confirms the same owner-local materialized-view name.
            cur.execute("SELECT mview_name FROM user_mviews WHERE mview_name = :1", (name,))
            if cur.fetchone() is None:
                raise RuntimeError(
                    f"Target object {name!r} exists as TABLE without matching "
                    "USER_MVIEWS metadata; refusing materialized-view replacement"
                )
            types.remove("TABLE")
    allowed = {expected}
    if expected == "PACKAGE":
        allowed.add("PACKAGE BODY")
    if types and not types.issubset(allowed):
        raise RuntimeError(
            f"Target object {name!r} already exists as {', '.join(sorted(types))}; "
            f"source-managed {expected} was not applied"
        )


def _normalize_materialized_view_query(value: Any) -> str:
    """Normalize catalog whitespace without folding quoted identifier semantics."""
    return " ".join(str(_value(value) or "").rstrip("; ").split())


def _check_compile_status(conn: Any, name: str, object_types: Iterable[str]) -> None:
    types = tuple(object_types)
    binds = ", ".join(f":{idx + 2}" for idx in range(len(types)))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT type, line, position, text FROM user_errors "
            f"WHERE name = :1 AND type IN ({binds}) AND attribute = 'ERROR' ORDER BY sequence",
            (name, *types),
        )
        errors = cur.fetchall()
        cur.execute(
            f"SELECT object_type, status FROM user_objects WHERE object_name = :1 "
            f"AND object_type IN ({binds})",
            (name, *types),
        )
        statuses = cur.fetchall()
    invalid = [f"{typ}={status}" for typ, status in statuses if str(status).upper() != "VALID"]
    if not statuses or errors or invalid:
        details = (
            "; ".join(
                f"{typ} line {line}:{position}: {str(msg).strip()}"
                for typ, line, position, msg in errors
            )
            or ", ".join(invalid)
            or "object was not found in USER_OBJECTS"
        )
        raise RuntimeError(f"Oracle compilation/status check failed for {name}: {details}")
