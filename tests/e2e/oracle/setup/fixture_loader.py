"""Restricted SQL fixture loader for Oracle acceptance owners.

Only explicitly selected, allowlisted DDL/DML is accepted. SQL statements
terminate with semicolons; stored function/procedure/trigger definitions may
contain internal semicolons and use a SQL*Plus slash line as their delimiter.
Reset scripts and destructive statements are rejected.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.connectors.oracle._models import oracle_error_code

_ALLOWED_START = re.compile(
    r"(?is)^\s*(?:CREATE\s+(?:(?:OR\s+REPLACE\s+)?"
    r"(?:FUNCTION|PROCEDURE|TRIGGER|VIEW|MATERIALIZED\s+VIEW)|"
    r"TYPE|TABLE|SEQUENCE|INDEX|SYNONYM)\b|"
    r"ALTER\s+TABLE\b|COMMENT\s+ON\s+(?:TABLE|COLUMN|VIEW|"
    r"MATERIALIZED\s+VIEW|SEQUENCE)\b|INSERT\s+INTO\b)"
)
_SCHEMA_QUALIFIED = re.compile(r"(?i)\b[A-Z][A-Z0-9_$#]*\s*\.")
_QUALIFIED_OBJECT_REFERENCE = re.compile(
    r"(?is)\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TYPE|TABLE|SEQUENCE|INDEX|"
    r"SYNONYM|VIEW|MATERIALIZED\s+VIEW|FUNCTION|PROCEDURE|TRIGGER)|"
    r"ALTER\s+TABLE|FROM|JOIN|REFERENCES|UPDATE|INTO|FOR|OF)\s+"
    r"[\w\"$#]+\s*\."
)
_FORBIDDEN = re.compile(
    r"(?i)\b(?:DROP|TRUNCATE|DELETE|MERGE|GRANT|REVOKE|COMMIT|ROLLBACK|"
    r"EXECUTE\s+IMMEDIATE|DBMS_SQL|CREATE\s+(?:USER|ROLE|DATABASE|"
    r"PLUGGABLE|TABLESPACE|TYPE\s+BODY))\b"
)
_PLSQL_DDL = re.compile(
    r"(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TRIGGER)\b"
)
_PLSQL_TERMINATOR = re.compile(r"(?is)\bEND\s*;\s*$")
_ALTER_ADD_CONSTRAINT = re.compile(
    r"(?is)^\s*ALTER\s+TABLE\s+[\w\"$#]+\s+ADD\s+CONSTRAINT\b"
)


@dataclass
class FixtureResult:
    """Outcome of executing a sequence of reviewed fixture files."""

    fixture_dir: str
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(result["ok"] for result in self.results)


def _split_fixture_sql(sql: str, *, filename: str) -> list[str]:
    """Split SQL statements without treating PL/SQL internal semicolons as delimiters."""
    statements: list[str] = []
    current: list[str] = []
    single_quoted = False
    double_quoted = False
    block_comment = False
    line_comment = False
    q_quote_end: str | None = None
    plsql = False
    i = 0

    def finish() -> None:
        nonlocal current, plsql
        statement = "".join(current).strip()
        if statement:
            statements.append(statement)
        current = []
        plsql = False

    while i < len(sql):
        if (
            (i == 0 or sql[i - 1] == "\n")
            and not single_quoted and not double_quoted
            and not block_comment and not line_comment and q_quote_end is None
        ):
            line_end = sql.find("\n", i)
            if line_end < 0:
                line_end = len(sql)
            if sql[i:line_end].strip() == "/":
                if not plsql:
                    raise ValueError(
                        f"Fixture {filename!r} uses a PL/SQL delimiter outside a routine definition"
                    )
                finish()
                i = line_end + (line_end < len(sql))
                continue

        char = sql[i]
        if line_comment:
            if char == "\n":
                line_comment = False
                if current:
                    current.append(char)
            i += 1
            continue
        if block_comment:
            if sql.startswith("*/", i):
                block_comment = False
                i += 2
            else:
                if char == "\n" and current:
                    current.append(char)
                i += 1
            continue
        if q_quote_end is not None:
            if sql.startswith(q_quote_end, i):
                current.extend(q_quote_end)
                i += len(q_quote_end)
                q_quote_end = None
            else:
                current.append(char)
                i += 1
            continue
        if single_quoted:
            current.append(char)
            if char == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                single_quoted = False
            i += 1
            continue
        if double_quoted:
            current.append(char)
            if char == '"':
                if i + 1 < len(sql) and sql[i + 1] == '"':
                    current.append('"')
                    i += 2
                    continue
                double_quoted = False
            i += 1
            continue
        if sql.startswith("--", i):
            if current:
                current.append(" ")
            line_comment = True
            i += 2
            continue
        if sql.startswith("/*", i):
            if current:
                current.append(" ")
            block_comment = True
            i += 2
            continue
        if (
            char in "qQ" and i + 2 < len(sql) and sql[i + 1] == "'"
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            opening = sql[i + 2]
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            q_quote_end = closing + "'"
            current.extend(sql[i:i + 3])
            i += 3
            continue
        if char == "'":
            single_quoted = True
            current.append(char)
            i += 1
            continue
        if char == '"':
            double_quoted = True
            current.append(char)
            i += 1
            continue
        if char == ";" and not plsql:
            finish()
            i += 1
            continue
        current.append(char)
        if not plsql and _PLSQL_DDL.match("".join(current).lstrip()):
            plsql = True
        i += 1

    if single_quoted or double_quoted or block_comment or q_quote_end is not None:
        raise ValueError(f"Fixture {filename!r} contains an unterminated quote or comment")
    finish()
    if not statements:
        raise ValueError(f"Fixture {filename!r} must contain at least one SQL statement")
    return statements


def _validate_single_fixture_statement(statement: str, *, filename: str) -> str:
    """Validate one allowlisted Oracle statement without a reset/write escape."""
    statement = statement.strip()
    if not statement or statement.startswith(("--", "/*", "/", "@", "!")):
        raise ValueError(f"Fixture {filename!r} must contain one SQL statement")
    if _FORBIDDEN.search(statement) or not _ALLOWED_START.match(statement):
        raise ValueError(f"Fixture {filename!r} contains an unsupported SQL operation")
    is_plsql = bool(_PLSQL_DDL.match(statement))
    if is_plsql:
        # CREATE routine/trigger DDL contains internal PL/SQL semicolons. It
        # must be one complete definition and cannot have trailing SQL.
        if not _PLSQL_TERMINATOR.search(statement):
            raise ValueError(f"Fixture {filename!r} has an incomplete PL/SQL definition")
    if statement.upper().startswith("ALTER TABLE") and not _ALTER_ADD_CONSTRAINT.match(statement):
        raise ValueError(f"Fixture {filename!r} contains an unsupported ALTER TABLE operation")
    statement_start = statement.lstrip().upper()
    owner_check_statement = re.sub(
        r"(?i):(NEW|OLD)\s*\.\s*[A-Z][A-Z0-9_$#]*", "", statement
    )
    owner_check_statement = re.sub(
        r"(?i)\b[A-Z][A-Z0-9_$#]*\s*\.\s*(?:NEXTVAL|CURRVAL)\b",
        "", owner_check_statement,
    )
    if statement_start.startswith(("CREATE VIEW", "CREATE MATERIALIZED VIEW")):
        qualified_reference = _QUALIFIED_OBJECT_REFERENCE.search(owner_check_statement)
    elif statement_start.startswith("COMMENT ON COLUMN"):
        qualified_reference = _QUALIFIED_OBJECT_REFERENCE.search(owner_check_statement)
    else:
        qualified_reference = _SCHEMA_QUALIFIED.search(owner_check_statement)
    if qualified_reference:
        raise ValueError(f"Fixture {filename!r} must use only the connected owner schema")
    return statement


def parse_fixture_sql(sql: str, *, filename: str = "<fixture>") -> list[str]:
    """Parse and validate every statement in one explicitly selected fixture file."""
    return [
        _validate_single_fixture_statement(statement, filename=filename)
        for statement in _split_fixture_sql(sql, filename=filename)
    ]


def validate_fixture_sql(sql: str, *, filename: str = "<fixture>") -> str:
    """Validate and return exactly one allowlisted SQL statement."""
    statements = parse_fixture_sql(sql, filename=filename)
    if len(statements) != 1:
        raise ValueError(f"Fixture {filename!r} must contain exactly one SQL statement")
    return _validate_single_fixture_statement(statements[0], filename=filename)


def resolve_fixture_files(
    fixture_dir: str | Path, fixture_names: Sequence[str]
) -> list[Path]:
    """Resolve a configured manifest, rejecting missing or unsafe filenames."""
    root = Path(fixture_dir)
    if not root.is_dir():
        raise ValueError(f"Oracle fixture directory does not exist: {root}")
    if not fixture_names:
        raise ValueError("Oracle fixture file list must not be empty")
    if len(set(fixture_names)) != len(fixture_names):
        raise ValueError("Oracle fixture file list contains duplicate names")
    files: list[Path] = []
    for name in fixture_names:
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or Path(name).suffix.casefold() != ".sql"
        ):
            raise ValueError("Oracle fixture names must be plain .sql filenames")
        path = root / name
        if not path.is_file():
            raise ValueError(f"Configured Oracle fixture file is missing: {name}")
        files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


def load_fixture(
    connection: Any,
    fixture_dir: str | Path,
    fixture_names: Sequence[str],
) -> FixtureResult:
    """Apply only the explicitly configured SQL files to the source owner."""
    root = Path(fixture_dir)
    files = resolve_fixture_files(root, fixture_names)
    statements = [
        (
            path,
            parse_fixture_sql(path.read_text(encoding="utf-8"), filename=path.name),
        )
        for path in files
    ]
    result = FixtureResult(fixture_dir=str(root))
    for path, file_statements in statements:
        cursor = connection.cursor()
        try:
            for statement_index, statement in enumerate(file_statements, 1):
                cursor.execute(statement)
                connection.commit()
            result.results.append({
                "script": path.name, "ok": True,
                "statement_count": len(file_statements),
            })
        except Exception as exc:  # noqa: BLE001 - driver messages may expose connection details
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001,S110 - preserve safe fixture failure report
                pass
            # Driver text can contain connection details; retain only the
            # script identity and failure status in reports.
            result.results.append(
                {
                    "script": path.name,
                    "ok": False,
                    "error_code": oracle_error_code(exc),
                    "statement_index": statement_index,
                }
            )
            break
        finally:
            cursor.close()
    return result
