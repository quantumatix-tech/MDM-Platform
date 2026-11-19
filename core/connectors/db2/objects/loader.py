from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import Schema, UpsertResult, validate_identifier


def _quote_ident(name: str) -> str:
    if not name:
        return name
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _split_qualified(name: str, schema_name: str | None = None) -> tuple[str, str | None]:
    if isinstance(name, str) and "." in name:
        parts = name.split(".", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid qualified table name {name!r}")
        parsed_schema = parts[0].strip().strip('"')
        parsed_table = parts[1].strip().strip('"')
        if schema_name is not None and str(schema_name).strip().strip('"') != parsed_schema:
            raise ValueError(f"Conflicting schema for {name!r}: {schema_name!r} vs {parsed_schema!r}")
        return parsed_table, parsed_schema if schema_name is None else schema_name
    return name, schema_name


def upsert_batch(conn: Any, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None, commit: bool = True, override_identity: bool = False) -> UpsertResult:
    if conn is None:
        raise RuntimeError("Not connected")
    object_name, schema_name = _split_qualified(object_name, schema.schema_name if schema else None)
    validate_identifier(object_name, "table")
    if schema_name:
        validate_identifier(schema_name, "schema")
    if schema is not None:
        schema.schema_name = schema_name or schema.schema_name
    qual = f'{_quote_ident(schema_name)}.{_quote_ident(object_name)}' if schema_name else _quote_ident(object_name)
    if schema and schema.columns:
        cols = [c.name for c in schema.columns if not (c.is_computed or c.generated)]
    else:
        rows_list = []
        first = True
        cols = []
        for r in rows:
            if first and isinstance(r, dict):
                cols = list(r.keys())
                first = False
            rows_list.append(r)
        rows = iter(rows_list)
    if not cols:
        return UpsertResult(success_count=0, failure_count=0)
    always_cols = [c.name for c in schema.columns if getattr(c, "is_identity", False) and str(getattr(c, "identity_kind", None) or "").upper() == "ALWAYS"]
    if always_cols and not override_identity:
        raise RuntimeError(
            "GENERATED ALWAYS identity columns cannot be loaded via INSERT on Db2 LUW: " + ", ".join(always_cols)
        )
    col_sql = ', '.join(_quote_ident(c) for c in cols)
    placeholders = ', '.join(['?' for _ in cols])
    override_clause = " OVERRIDING SYSTEM VALUE" if (override_identity and always_cols) else ""
    sql = f"INSERT{override_clause} INTO {qual} ({col_sql}) VALUES ({placeholders})"
    inserted = 0
    cur = conn.cursor()
    try:
        for row in rows:
            if isinstance(row, dict):
                values = [row.get(c) for c in cols]
            elif isinstance(row, (list, tuple)):
                values = list(row)
            else:
                continue
            cur.execute(sql, values)
            inserted += 1
        if commit:
            try:
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return UpsertResult(success_count=inserted, failure_count=0)
