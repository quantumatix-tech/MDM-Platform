"""Oracle table partition discovery and safety validation."""

from __future__ import annotations

import re
from typing import Any

from core.connectors.oracle._models import validate_oracle_identifier


def discover_table_partitioning(conn: Any, table_name: str) -> dict[str, Any] | None:
    """Read and validate supported single-level RANGE/LIST partition metadata."""
    validate_oracle_identifier(table_name, "table")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT partitioning_type, subpartitioning_type, interval, autolist, "
            "auto, ref_ptn_constraint_name, status, partition_count "
            "FROM user_part_tables WHERE table_name = :1",
            (table_name,),
        )
        table_row = cur.fetchone()
        if table_row is None:
            return None
        if not isinstance(table_row, (tuple, list)) or len(table_row) != 8:
            raise RuntimeError(
                f"Oracle partition catalog returned malformed USER_PART_TABLES metadata "
                f"for {table_name}: expected 8 fields, received "
                f"{len(table_row) if isinstance(table_row, (tuple, list)) else 'a non-row value'}"
            )
        method, submethod, interval, autolist, auto, ref_constraint, status, expected_count = (
            table_row
        )
        method = str(method or "").upper()
        unsupported = []
        if method not in {"RANGE", "LIST"}:
            unsupported.append(f"partitioning_type={method or 'UNKNOWN'}")
        if str(submethod or "").upper() != "NONE":
            unsupported.append(f"subpartitioning_type={submethod}")
        if interval not in (None, ""):
            unsupported.append("interval partitioning")
        if str(autolist or "NO").upper() != "NO":
            unsupported.append("automatic list partitioning")
        if str(auto or "NO").upper() != "NO":
            unsupported.append("automatic table partitioning")
        if ref_constraint:
            unsupported.append("reference partitioning")
        if str(status or "").upper() != "VALID":
            unsupported.append(f"status={status}")
        cur.execute(
            "SELECT column_name FROM user_part_key_columns "
            "WHERE name = :1 AND object_type = 'TABLE' ORDER BY column_position",
            (table_name,),
        )
        keys = [str(row[0]) for row in cur.fetchall()]
        for key in keys:
            validate_oracle_identifier(key, "partition key")
        cur.execute(
            "SELECT partition_name, partition_position, high_value_clob, interval, "
            "composite, is_nested, read_only, indexing "
            "FROM user_tab_partitions WHERE table_name = :1 "
            "ORDER BY partition_position",
            (table_name,),
        )
        rows = cur.fetchall()
    if not keys:
        unsupported.append("missing partition key metadata")
    elif len(keys) != 1:
        unsupported.append(
            "multi-column partition keys require explicit HIGH_VALUE_CLOB format verification"
        )
    partitions = []
    for (
        name,
        position,
        high_value,
        interval_partition,
        composite,
        nested,
        read_only,
        indexing,
    ) in rows:
        validate_oracle_identifier(str(name), "partition")
        if str(interval_partition or "NO").upper() == "YES":
            unsupported.append(f"interval partition {name}")
        if str(composite or "NO").upper() != "NO":
            unsupported.append(f"composite partition {name}")
        if str(nested or "NO").upper() == "YES":
            unsupported.append(f"nested-table partition {name}")
        if str(read_only or "NO").upper() == "YES":
            unsupported.append(f"read-only partition {name}")
        if str(indexing or "ON").upper() not in {"ON", "NONE"}:
            unsupported.append(f"partition indexing={indexing} on {name}")
        value = str(high_value.read() if hasattr(high_value, "read") else high_value or "").strip()
        if not value:
            unsupported.append(f"partition {name} has no HIGH_VALUE_CLOB metadata")
        else:
            _validate_partition_bound_expression(value, table_name, str(name))
        partitions.append(
            {
                "name": str(name),
                "position": int(position),
                "high_value": value,
            }
        )
    if expected_count is not None and int(expected_count) != len(partitions):
        unsupported.append(
            f"partition catalog count mismatch ({len(partitions)} of {expected_count})"
        )
    if [part["position"] for part in partitions] != list(range(1, len(partitions) + 1)):
        unsupported.append("partition positions are incomplete or out of order")
    if len({part["name"].casefold() for part in partitions}) != len(partitions):
        unsupported.append("duplicate partition names")
    if unsupported:
        raise RuntimeError(
            f"Oracle FULL cannot safely migrate partitioned table {table_name}: "
            + ", ".join(unsupported)
        )
    return {"method": method, "keys": keys, "partitions": partitions}


def _validate_partition_bound_expression(value: str, table: str, partition: str) -> None:
    """Permit catalog-rendered literal bounds without allowing executable SQL."""
    if "--" in value or "/*" in value or "*/" in value or "\n" in value or "\r" in value:
        raise RuntimeError(
            f"Oracle partition bound for {table}.{partition} contains a comment or line break"
        )

    def skip_space(position: int) -> int:
        while position < len(value) and value[position].isspace():
            position += 1
        return position

    def read_string(position: int) -> tuple[str, int] | None:
        """Read one SQL string literal, honoring Oracle's doubled quote escape."""
        if position >= len(value) or value[position] != "'":
            return None
        position += 1
        content: list[str] = []
        while position < len(value):
            char = value[position]
            if char == "'":
                if position + 1 < len(value) and value[position + 1] == "'":
                    content.append("'")
                    position += 2
                    continue
                return "".join(content), position + 1
            content.append(char)
            position += 1
        return None

    def read_word(position: int) -> tuple[str, int] | None:
        match = re.match(r"[A-Za-z_$#][A-Za-z0-9_$#]*", value[position:])
        return (match.group(0), position + len(match.group(0))) if match else None

    def read_scalar(position: int) -> int | None:
        position = skip_space(position)
        if position >= len(value):
            return None
        string = read_string(position)
        if string is not None:
            return string[1]

        number = re.match(
            r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?",
            value[position:],
        )
        if number:
            return position + len(number.group(0))

        word = read_word(position)
        if word is None:
            return None
        token, end = word
        token = token.upper()
        if token in {"MAXVALUE", "DEFAULT", "NULL"}:
            return end
        if token in {"DATE", "TIMESTAMP"}:
            literal = read_string(skip_space(end))
            if literal is None:
                return None
            content, literal_end = literal
            if token == "DATE" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", content):
                return None
            if token == "TIMESTAMP" and not re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?(?: [+-]\d{2}:\d{2})?",
                content,
            ):
                return None
            return literal_end
        if token not in {"TO_DATE", "TO_TIMESTAMP", "TO_TIMESTAMP_TZ"}:
            return None

        cursor = skip_space(end)
        if cursor >= len(value) or value[cursor] != "(":
            return None
        cursor = skip_space(cursor + 1)
        arguments: list[str] = []
        while True:
            literal = read_string(cursor)
            if literal is None:
                return None
            content, cursor = literal
            arguments.append(content)
            cursor = skip_space(cursor)
            if cursor < len(value) and value[cursor] == ",":
                if len(arguments) == 3:
                    return None
                cursor = skip_space(cursor + 1)
                continue
            if cursor >= len(value) or value[cursor] != ")":
                return None
            cursor += 1
            break
        if not arguments or not arguments[0]:
            return None
        if len(arguments) == 3 and not re.fullmatch(
            r"NLS_CALENDAR\s*=\s*[A-Za-z0-9_ -]+", arguments[2], re.IGNORECASE
        ):
            return None
        return cursor

    # Oracle's HIGH_VALUE_CLOB can render date bounds as, for example,
    # TO_DATE(' 2025-01-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS',
    #         'NLS_CALENDAR=GREGORIAN'). Parse a narrow literal-only grammar:
    # bare scalar values or a flat LIST tuple; date conversion functions may
    # contain only string literal arguments. Never pass arbitrary SQL through.
    cursor = skip_space(0)
    if cursor < len(value) and value[cursor] == "(":
        cursor = skip_space(cursor + 1)
        cursor = read_scalar(cursor) or -1
        if cursor >= 0:
            while True:
                cursor = skip_space(cursor)
                if cursor < len(value) and value[cursor] == ",":
                    cursor = read_scalar(cursor + 1) or -1
                    if cursor < 0:
                        break
                    continue
                if cursor < len(value) and value[cursor] == ")":
                    cursor += 1
                else:
                    cursor = -1
                break
    else:
        cursor = read_scalar(cursor) or -1

    if cursor < 0 or skip_space(cursor) != len(value):
        raise RuntimeError(
            f"Oracle partition bound for {table}.{partition} contains an unsupported expression"
        )
