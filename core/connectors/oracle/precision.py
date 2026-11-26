"""Lossless Oracle value transport for FULL migration and comparison."""
from __future__ import annotations

import json
import re
import tempfile
import xml.etree.ElementTree as ET
from itertools import zip_longest
from typing import Any

from core.connectors.base import quote_identifier

DATE_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS'
TIMESTAMP_FORMAT = DATE_FORMAT + ".FF9"
TIMESTAMP_TZ_FORMAT = TIMESTAMP_FORMAT + " TZH:TZM"
_NLS_CALENDAR = "NLS_DATE_LANGUAGE=American NLS_CALENDAR=GREGORIAN"

# Oracle FULL only permits source types whose DDL, extraction, insertion, and
# verification paths are implemented without changing the source type. Keep
# this allowlist deliberately explicit: a new Oracle type is unsupported until
# its complete transport path is added.
_ORACLE_FULL_SUPPORTED_TYPES = {
    "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW",
    "NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "DATE",
    "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE",
    "BLOB", "CLOB", "NCLOB", "JSON", "XMLTYPE",
}


def oracle_full_unsupported_datatypes(schemas: Any) -> list[tuple[str, str, str]]:
    """List Oracle FULL source columns without a verified native transport."""
    unsupported: list[tuple[str, str, str]] = []
    for schema in schemas:
        for column in schema.columns:
            datatype = " ".join(str(column.source_type or "").upper().split())
            base = re.sub(r"\s*\([^)]*\)", "", datatype).strip()
            supported = False
            if base == "FLOAT":
                supported = re.fullmatch(r"FLOAT(?:\s*\(\s*\d+\s*\))?", datatype) is not None
            elif base == "NUMBER":
                supported = re.fullmatch(
                    r"NUMBER(?:\s*\(\s*\d+\s*(?:,\s*-?\d+\s*)?\))?", datatype
                ) is not None
            elif base in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
                supported = re.fullmatch(
                    rf"{base}(?:\s*\(\s*\d+\s*(?:BYTE|CHAR)?\s*\))?", datatype
                ) is not None
            elif base.startswith("TIMESTAMP"):
                supported = re.fullmatch(
                    r"TIMESTAMP(?:\s*\(\s*\d+\s*\))?(?: WITH (?:TIME ZONE|LOCAL TIME ZONE))?",
                    datatype,
                ) is not None
            elif base in _ORACLE_FULL_SUPPORTED_TYPES:
                supported = datatype == base
            elif (
                re.fullmatch(r"[A-Z][A-Z0-9_$#]*", base)
                and column.name in schema.options.get("oracle_object_types", {})
            ):
                # Oracle schema-level OBJECT types are preserved as named
                # columns; their value path is validated independently using
                # catalog-resolved type metadata and the driver object API.
                supported = True
            else:
                supported = (
                    base in _ORACLE_FULL_SUPPORTED_TYPES and datatype == base
                )
            if not supported:
                unsupported.append((str(schema.name), str(column.name), datatype or "<unknown>"))
    return unsupported


def oracle_type_family(type_name: Any) -> str | None:
    """Classify Oracle numeric and temporal types without losing precision."""
    normalized = " ".join(str(type_name or "").upper().split())
    base = re.sub(r"\s*\([^)]*\)", "", normalized).strip()
    if base in {"NUMBER", "FLOAT"}:
        return "NUMBER"
    if base == "DATE":
        return "DATE"
    if base == "TIMESTAMP WITH LOCAL TIME ZONE":
        return "TIMESTAMP_LTZ"
    if base == "TIMESTAMP WITH TIME ZONE":
        return "TIMESTAMP_TZ"
    if base == "TIMESTAMP":
        return "TIMESTAMP"
    return None


def oracle_lob_kind(type_name: Any) -> str | None:
    """Return the Oracle BLOB/CLOB family handled by this transport."""
    normalized = " ".join(str(type_name or "").upper().split())
    normalized = re.sub(r"\s*\([^)]*\)", "", normalized).strip()
    if normalized == "BLOB":
        return "BLOB"
    if normalized == "NCLOB":
        return "NCLOB"
    if normalized == "CLOB":
        return "CLOB"
    if normalized == "XMLTYPE":
        return "CLOB"
    return None


def oracle_xml_storage_supported(metadata: dict[str, Any] | None) -> bool:
    """Allow untyped XML storage modes transportable through XMLSerialize/XMLType."""
    if not metadata or metadata.get("xmlschema") is not None:
        return False
    storage = " ".join(str(metadata.get("storage_type", "")).upper().split())
    if storage == "CLOB":
        return True
    return (
        storage in {"BINARY", "TRANSPORTABLE BINARY"}
        and str(metadata.get("nonschema", "")).upper() == "YES"
    )


def _json_values_equal(left: Any, right: Any) -> bool:
    """Ignore object-key order while preserving JSON container/scalar types."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _oracle_object_values_equal(left: Any, right: Any) -> bool:
    """Compare recursive Oracle object values, ignoring nested-table ordering."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, dict) and isinstance(right, dict):
        left_collection = set(left) == {"__oracle_collection__", "kind", "items"}
        right_collection = set(right) == {"__oracle_collection__", "kind", "items"}
        if left_collection or right_collection:
            if not left_collection or not right_collection:
                return False
            if (
                str(left["__oracle_collection__"]).casefold()
                != str(right["__oracle_collection__"]).casefold()
                or left["kind"] != right["kind"]
            ):
                return False
            left_items, right_items = left["items"], right["items"]
            if len(left_items) != len(right_items):
                return False
            if left["kind"] == "varray":
                return all(
                    _oracle_object_values_equal(a, b)
                    for a, b in zip(left_items, right_items)
                )
            unmatched = list(right_items)
            for item in left_items:
                match_index = next(
                    (index for index, candidate in enumerate(unmatched)
                     if _oracle_object_values_equal(item, candidate)),
                    None,
                )
                if match_index is None:
                    return False
                unmatched.pop(match_index)
            return True
        left_object = set(left) == {"__oracle_object__", "attributes"}
        right_object = set(right) == {"__oracle_object__", "attributes"}
        if left_object or right_object:
            if not left_object or not right_object:
                return False
            return (
                str(left["__oracle_object__"]).casefold()
                == str(right["__oracle_object__"]).casefold()
                and left["attributes"].keys() == right["attributes"].keys()
                and all(
                    _oracle_object_values_equal(left["attributes"][name], right["attributes"][name])
                    for name in left["attributes"]
                )
            )
        return left.keys() == right.keys() and all(
            _oracle_object_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list) and isinstance(right, list)
            and len(left) == len(right)
            and all(_oracle_object_values_equal(a, b) for a, b in zip(left, right))
        )
    return type(left) is type(right) and left == right


def oracle_lob_units(value: bytes | str, kind: str) -> int:
    """Oracle counts CLOB/NCLOB offsets in UCS-2 code points."""
    if kind == "BLOB":
        return len(value)
    return len(value.encode("utf-16-be")) // 2


class OracleLobPayload:
    """A spooled LOB value; large contents spill to disk instead of RAM."""

    def __init__(self, kind: str, spool: Any, length: int) -> None:
        self.kind = kind
        self.spool = spool
        self.length = length

    @property
    def empty(self) -> bool:
        return self.length == 0

    @classmethod
    def from_lob(cls, lob: Any, kind: str, chunk_size: int = 64 * 1024) -> OracleLobPayload:
        is_binary = kind == "BLOB"
        # Ownership transfers to OracleLobPayload, so this temporary file must
        # stay open after the factory returns until the caller closes the value.
        spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=64 * 1024,
            mode="w+b" if is_binary else "w+t",
            encoding=None if is_binary else "utf-8",
            newline=None if is_binary else "",
        )
        try:
            total = int(lob.size())
            offset = 1
            while offset <= total:
                chunk = lob.read(offset, min(chunk_size, total - offset + 1))
                if not chunk:
                    break
                spool.write(chunk)
                offset += oracle_lob_units(chunk, kind)
            spool.seek(0)
            return cls(kind, spool, offset - 1)
        except Exception:
            spool.close()
            raise

    @classmethod
    def from_value(cls, value: Any, kind: str) -> OracleLobPayload:
        is_binary = kind == "BLOB"
        # Ownership transfers to OracleLobPayload, so this temporary file must
        # stay open after the factory returns until the caller closes the value.
        spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=64 * 1024,
            mode="w+b" if is_binary else "w+t",
            encoding=None if is_binary else "utf-8",
            newline=None if is_binary else "",
        )
        if value is not None:
            spool.write(value)
        length = oracle_lob_units(value, kind) if value is not None else 0
        spool.seek(0)
        return cls(kind, spool, length)

    def chunks(self, chunk_size: int = 64 * 1024):
        self.spool.seek(0)
        while True:
            chunk = self.spool.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def matches(self, other: Any) -> bool:
        if not isinstance(other, OracleLobPayload) or self.kind != other.kind:
            return False
        if self.length != other.length:
            return False
        left = self.chunks()
        right = other.chunks()
        return all(a == b for a, b in zip_longest(left, right, fillvalue=None))

    def close(self) -> None:
        self.spool.close()


def transport_select_expression(column: Any) -> str:
    """Produce exact text for temporal values; leave other columns native."""
    quoted = quote_identifier(column.name)
    family = oracle_type_family(column.source_type)
    if str(column.source_type).upper() == "XMLTYPE":
        expr = f"XMLSERIALIZE(DOCUMENT {quoted} AS CLOB NO INDENT)"
    elif family == "DATE":
        expr = f"TO_CHAR({quoted}, '{DATE_FORMAT}', '{_NLS_CALENDAR}')"
    elif family == "TIMESTAMP":
        expr = f"TO_CHAR({quoted}, '{TIMESTAMP_FORMAT}', '{_NLS_CALENDAR}')"
    elif family == "TIMESTAMP_TZ":
        expr = f"TO_CHAR({quoted}, '{TIMESTAMP_TZ_FORMAT}', '{_NLS_CALENDAR}')"
    elif family == "TIMESTAMP_LTZ":
        utc_value = f"SYS_EXTRACT_UTC(FROM_TZ(CAST({quoted} AS TIMESTAMP), SESSIONTIMEZONE))"
        expr = f"TO_CHAR({utc_value}, '{TIMESTAMP_FORMAT}', '{_NLS_CALENDAR}')"
    else:
        return quoted
    return f'{expr} AS {quoted}'


def transport_bind_expression(column: Any, bind_name: str) -> str:
    """Convert lossless temporal text into a typed Oracle insert expression."""
    family = oracle_type_family(column.source_type)
    if str(column.source_type).upper() == "XMLTYPE":
        return f"XMLTYPE({bind_name})"
    if family == "DATE":
        return f"TO_DATE({bind_name}, '{DATE_FORMAT}', '{_NLS_CALENDAR}')"
    if family == "TIMESTAMP":
        return f"TO_TIMESTAMP({bind_name}, '{TIMESTAMP_FORMAT}', '{_NLS_CALENDAR}')"
    if family == "TIMESTAMP_TZ":
        return f"TO_TIMESTAMP_TZ({bind_name}, '{TIMESTAMP_TZ_FORMAT}', '{_NLS_CALENDAR}')"
    if family == "TIMESTAMP_LTZ":
        return (
            f"FROM_TZ(TO_TIMESTAMP({bind_name}, '{TIMESTAMP_FORMAT}', "
            f"'{_NLS_CALENDAR}'), '+00:00')"
        )
    return bind_name


def decimal_output_type_handler(oracledb: Any):
    """Return NUMBER fetch values as Decimal, supported by driver >= 2.5."""
    def handler(cursor: Any, metadata: Any) -> Any:
        if metadata.type_code == oracledb.DB_TYPE_NUMBER:
            from decimal import Decimal

            return cursor.var(Decimal, arraysize=cursor.arraysize)
        return None

    return handler


def input_type(column: Any, oracledb: Any) -> Any:
    """Bind numeric values as NUMBER and precision-safe temporal text as VARCHAR."""
    family = oracle_type_family(column.source_type)
    if family == "NUMBER":
        return oracledb.DB_TYPE_NUMBER
    if family in {"DATE", "TIMESTAMP", "TIMESTAMP_TZ", "TIMESTAMP_LTZ"}:
        return oracledb.DB_TYPE_VARCHAR
    if str(column.source_type).upper() == "JSON":
        return oracledb.DB_TYPE_JSON
    lob_kind = oracle_lob_kind(column.source_type)
    if lob_kind:
        return getattr(oracledb, f"DB_TYPE_{lob_kind}")
    return None


def compare_precision_rows(
    source_rows: list[dict[str, Any]], target_rows: list[dict[str, Any]], schema: Any
) -> dict[str, Any]:
    """Compare all NUMBER/temporal values and identify each changed cell."""
    sensitive = [
        column.name for column in schema.columns
        if oracle_type_family(column.source_type) is not None
    ]
    primary_key = list(getattr(schema, "primary_key", []) or [])

    def order_key(row: dict[str, Any]) -> str:
        values = [row.get(name) for name in primary_key] if primary_key else row
        return json.dumps(values, sort_keys=True, default=str, ensure_ascii=True)

    source_sorted = sorted(source_rows, key=order_key)
    target_sorted = sorted(target_rows, key=order_key)
    mismatches: list[dict[str, Any]] = []
    shared_count = min(len(source_sorted), len(target_sorted))
    for index in range(shared_count):
        source_row = source_sorted[index]
        target_row = target_sorted[index]
        row_label: Any = (
            {
                name: (
                    str(source_row.get(name))
                    if source_row.get(name) is not None else None
                )
                for name in primary_key
            }
            if primary_key else index + 1
        )
        for column in sensitive:
            source_value = source_row.get(column)
            target_value = target_row.get(column)
            if source_value != target_value:
                mismatches.append({
                    "row": row_label,
                    "column": column,
                    "source": str(source_value) if source_value is not None else None,
                    "target": str(target_value) if target_value is not None else None,
                })
    if len(source_sorted) != len(target_sorted):
        mismatches.append({
            "row": shared_count + 1,
            "column": "<row_count>",
            "source": len(source_sorted),
            "target": len(target_sorted),
        })
    return {
        "match": not mismatches,
        "source_rows": len(source_sorted),
        "target_rows": len(target_sorted),
        "columns": sensitive,
        "mismatches": mismatches,
    }


def compare_oracle_rows(
    source_rows: Any, target_rows: Any, schema: Any, *, all_columns: bool = False
) -> dict[str, Any]:
    """Compare exact Oracle values row by row, streaming LOB content."""
    sensitive = [
        column.name for column in schema.columns
        if all_columns or oracle_type_family(column.source_type) is not None
        or oracle_lob_kind(column.source_type) is not None
        or str(column.source_type).upper() in {"JSON", "RAW"}
        or column.name in schema.options.get("oracle_object_types", {})
    ]
    lob_kinds = {
        column.name: oracle_lob_kind(column.source_type)
        for column in schema.columns if oracle_lob_kind(column.source_type) is not None
    }
    primary_key = list(getattr(schema, "primary_key", []) or [])
    mismatches: list[dict[str, Any]] = []
    mismatch_count = 0
    source_count = target_count = 0

    def record_mismatch(item: dict[str, Any]) -> None:
        nonlocal mismatch_count
        mismatch_count += 1
        if len(mismatches) < 100:
            mismatches.append(item)

    def row_label(row: dict[str, Any] | None, ordinal: int) -> Any:
        if row is None:
            return ordinal
        if primary_key:
            return {
                name: str(row.get(name)) if row.get(name) is not None else None
                for name in primary_key
            }
        return ordinal

    sentinel = object()
    try:
        for ordinal, pair in enumerate(zip_longest(source_rows, target_rows, fillvalue=sentinel), 1):
            source_row, target_row = pair
            if source_row is sentinel:
                target_count += 1
                record_mismatch({
                    "row": row_label(target_row, ordinal), "column": "<row_count>",
                    "source": None, "target": "extra row",
                })
                _close_row_lobs(target_row, lob_kinds)
                continue
            if target_row is sentinel:
                source_count += 1
                record_mismatch({
                    "row": row_label(source_row, ordinal), "column": "<row_count>",
                    "source": "missing target row", "target": None,
                })
                _close_row_lobs(source_row, lob_kinds)
                continue
            source_count += 1
            target_count += 1
            label = row_label(source_row, ordinal)
            try:
                for column in sensitive:
                    left, right = source_row.get(column), target_row.get(column)
                    source_type = str(next(
                        item.source_type for item in schema.columns if item.name == column
                    )).upper()
                    if source_type == "XMLTYPE":
                        equal = _xml_values_equal(left, right)
                        left_report = _lob_report_value(left)
                        right_report = _lob_report_value(right)
                    elif column in lob_kinds:
                        equal = _lob_values_equal(left, right, lob_kinds[column])
                        left_report = _lob_report_value(left)
                        right_report = _lob_report_value(right)
                    elif column in schema.options.get("oracle_object_types", {}):
                        equal = _oracle_object_values_equal(left, right)
                        left_report = str(left) if left is not None else None
                        right_report = str(right) if right is not None else None
                    else:
                        if source_type == "JSON" and left is not None and right is not None:
                            try:
                                left_json = json.loads(left) if isinstance(left, (str, bytes, bytearray)) else left
                                right_json = json.loads(right) if isinstance(right, (str, bytes, bytearray)) else right
                                equal = _json_values_equal(left_json, right_json)
                            except (TypeError, json.JSONDecodeError):
                                equal = False
                        else:
                            equal = left == right
                        left_report = str(left) if left is not None else None
                        right_report = str(right) if right is not None else None
                    if not equal:
                        record_mismatch({
                            "row": label, "column": column,
                            "source": left_report, "target": right_report,
                        })
            finally:
                _close_row_lobs(source_row, lob_kinds)
                _close_row_lobs(target_row, lob_kinds)
    finally:
        _close_remaining_lobs(source_rows, lob_kinds)
        _close_remaining_lobs(target_rows, lob_kinds)
    return {
        "match": mismatch_count == 0,
        "source_rows": source_count,
        "target_rows": target_count,
        "columns": sensitive,
        "mismatches": mismatches,
        "mismatch_count": mismatch_count,
    }


def _lob_report_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, OracleLobPayload):
        if value.empty:
            return "EMPTY"
        return f"{value.kind} payload ({value.length} units)"
    if len(value) == 0:
        return "EMPTY"
    return f"LOB payload ({len(value)} units)"


def _xml_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, OracleLobPayload):
        return "".join(value.chunks())
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _xml_values_equal(left: Any, right: Any) -> bool:
    """Compare XML infosets with C14N; retain text and attribute values."""
    if left is None or right is None:
        return left is None and right is None
    try:
        left_text = ET.canonicalize(
            xml_data=_xml_text(left), strip_text=False, with_comments=True
        )
        right_text = ET.canonicalize(
            xml_data=_xml_text(right), strip_text=False, with_comments=True
        )
        return left_text == right_text
    except (ET.ParseError, ValueError, TypeError):
        return False


def _lob_values_equal(left: Any, right: Any, kind: str) -> bool:
    if left is None or right is None:
        return left is None and right is None
    left_payload = left if isinstance(left, OracleLobPayload) else OracleLobPayload.from_value(left, kind)
    right_payload = right if isinstance(right, OracleLobPayload) else OracleLobPayload.from_value(right, kind)
    try:
        return left_payload.matches(right_payload)
    finally:
        if left_payload is not left:
            left_payload.close()
        if right_payload is not right:
            right_payload.close()


def _close_row_lobs(row: Any, lob_kinds: dict[str, str | None]) -> None:
    if not isinstance(row, dict):
        return
    for column in lob_kinds:
        value = row.get(column)
        if isinstance(value, OracleLobPayload):
            value.close()


def _close_remaining_lobs(rows: Any, lob_kinds: dict[str, str | None]) -> None:
    close = getattr(rows, "close", None)
    if callable(close):
        close()
