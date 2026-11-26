"""Oracle index catalog normalization for supported column-key indexes.

Oracle reports indexes on ``TIMESTAMP WITH TIME ZONE`` columns as
function-based indexes. Their dictionary expression is an intrinsic
``SYS_EXTRACT_UTC(column)`` expression, while the reproducible DDL remains an
ordinary index on the original column. Oracle similarly represents descending
keys as function-based indexes. This module recognizes only those exact
intrinsic forms; arbitrary expression indexes remain unsupported.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

_IDENTIFIER = r'"((?:[^"]|"")+)"|([A-Z][A-Z0-9_$#]*)'
_COLUMN_EXPRESSION = re.compile(rf"^\s*(?:{_IDENTIFIER})\s*$", re.IGNORECASE)
_SYS_EXTRACT_UTC = re.compile(
    rf"^\s*SYS_EXTRACT_UTC\s*\(\s*(?:{_IDENTIFIER})\s*\)\s*$",
    re.IGNORECASE,
)
_SYS_OP_DESCEND = re.compile(r"^\s*SYS_OP_DESCEND\s*\((.*)\)\s*$", re.IGNORECASE)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "read") and callable(value.read):
        value = value.read()
    return str(value or "").strip()


def _identifier_value(match: re.Match[str]) -> str:
    quoted, unquoted = match.group(1), match.group(2)
    return quoted.replace('""', '"') if quoted is not None else unquoted.upper()


def _intrinsic_expression(expression: Any) -> tuple[str, str, bool] | None:
    """Return ``(column, kind, descending_wrapper)`` for exact native forms."""
    text = _text(expression)
    if not text:
        return None
    descending_wrapper = False
    match = _SYS_OP_DESCEND.fullmatch(text)
    if match:
        descending_wrapper = True
        text = match.group(1).strip()

    match = _SYS_EXTRACT_UTC.fullmatch(text)
    if match:
        return _identifier_value(match), "timestamp_tz", descending_wrapper

    match = _COLUMN_EXPRESSION.fullmatch(text)
    if match and descending_wrapper:
        return _identifier_value(match), "descending", True
    return None


def _is_timestamp_with_time_zone(data_type: Any) -> bool:
    normalized = re.sub(r"\(\s*\d+\s*\)", "", _text(data_type).upper())
    return " ".join(normalized.split()) == "TIMESTAMP WITH TIME ZONE"


def normalize_index_rows(
    rows: Iterable[tuple[Any, ...]],
    column_types: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Normalize rows shaped as USER_INDEXES + USER_IND_COLUMNS + expressions.

    Expected columns are ``name, uniqueness, index_type, status, partitioned,
    column_name, position, descend, expression``. Unrecognized metadata is
    returned with ``supported=False`` so callers can fail closed and report the
    original catalog classification.
    """
    types = {str(name).casefold(): value for name, value in (column_types or {}).items()}
    grouped: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        if len(row) < 9:
            raise ValueError("Oracle index catalog row is incomplete")
        grouped[str(row[0])].append(tuple(row[:9]))

    result: dict[str, dict[str, Any]] = {}
    for name, index_rows in grouped.items():
        first = index_rows[0]
        uniqueness, raw_type, status, partitioned = first[1], _text(first[2]).upper(), _text(first[3]).upper(), _text(first[4]).upper()
        normalized_columns: list[tuple[int, str, bool]] = []
        expressions_seen = False
        reason: str | None = None
        for row in index_rows:
            _, _, _, _, _, catalog_column, position, descend, raw_expression = row
            position = int(position)
            direction = _text(descend).upper() or "ASC"
            if direction not in {"ASC", "DESC"}:
                reason = f"unknown key direction {direction!r}"
                break
            expression = _text(raw_expression)
            if expression:
                expressions_seen = True
                parsed = _intrinsic_expression(expression)
                # Oracle 26ai can expose an ordinary DESC key as the hidden
                # SYS_NC* index column plus the original bare identifier in
                # USER_IND_EXPRESSIONS (for example, "COL"), rather than
                # SYS_OP_DESCEND("COL"). Accept that exact catalog shape only
                # when it is a DESC FUNCTION-BASED NORMAL key and the named
                # visible table column exists. This does not admit arbitrary
                # function/expression indexes.
                if (
                    parsed is None
                    and raw_type == "FUNCTION-BASED NORMAL"
                    and direction == "DESC"
                    and _text(catalog_column).upper().startswith("SYS_NC")
                ):
                    bare_column = _COLUMN_EXPRESSION.fullmatch(expression)
                    if bare_column:
                        candidate = _identifier_value(bare_column)
                        if candidate.casefold() in types:
                            parsed = (candidate, "descending", False)
                if parsed is None:
                    reason = f"unsupported key expression at position {position}"
                    break
                column, expression_kind, descending_wrapper = parsed
                if expression_kind == "timestamp_tz":
                    if not _is_timestamp_with_time_zone(types.get(column.casefold())):
                        reason = (
                            f"SYS_EXTRACT_UTC expression at position {position} does not "
                            "match a TIMESTAMP WITH TIME ZONE column"
                        )
                        break
                elif direction != "DESC":
                    reason = f"descending expression at position {position} is not marked DESC"
                    break
                if descending_wrapper and direction != "DESC":
                    reason = f"SYS_OP_DESCEND expression at position {position} is not marked DESC"
                    break
                normalized_column = column
            else:
                normalized_column = _text(catalog_column)
                if not normalized_column or normalized_column.upper().startswith("SYS_NC"):
                    reason = f"unmapped generated key column at position {position}"
                    break
            normalized_columns.append((position, normalized_column, direction == "DESC"))

        positions = [item[0] for item in normalized_columns]
        if reason is None and (
            not normalized_columns
            or sorted(positions) != list(range(1, len(positions) + 1))
            or len(set(positions)) != len(positions)
        ):
            reason = "index key positions are incomplete or duplicated"
        if reason is None and raw_type not in {"NORMAL", "FUNCTION-BASED NORMAL"}:
            reason = f"unsupported Oracle index type {raw_type or 'UNKNOWN'}"
        if reason is None and status != "VALID":
            reason = f"index status is {status or 'UNKNOWN'}"
        if reason is None and partitioned != "NO":
            reason = f"index partitioned state is {partitioned or 'UNKNOWN'}"
        if reason is None and raw_type == "FUNCTION-BASED NORMAL" and not expressions_seen:
            reason = "function-based index has no recognized intrinsic expression"

        ordered = sorted(normalized_columns)
        result[name] = {
            "name": name,
            "unique": _text(uniqueness).upper() == "UNIQUE",
            "index_type": "NORMAL" if reason is None else raw_type,
            "catalog_index_type": raw_type,
            "status": status,
            "partitioned": partitioned,
            "columns": [column for _, column, _ in ordered],
            "descending_columns": [column for _, column, descending in ordered if descending],
            "functional": expressions_seen and reason is not None,
            "supported": reason is None,
            "reason": reason,
        }
    return result
