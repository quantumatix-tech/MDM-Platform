"""MSSQL Table object implementation.

Single logical home for Table-specific behaviour: discovery, DDL
generation, creation, data export, upsert, and row deletion.

Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported, which
avoids circular dependencies.

# ---------------------------------------------------------------------------
# Architecture overview:
#
#   orchestrator.py
#     ┌─────────────┐
#     │  _source    │─────────  list_objects()     ─────────► table.discover_tables()
#     │             │─────────  get_object_count() ─────────► table.get_table_row_count()
#     │             │─────────  export_full()      ─────────► table.export_table_data()
#     │             │─────────  get_schema()       ─────────► (stays in source.py — mixed concern)
#     └─────────────┘
#     ┌─────────────┐
#     │  _target    │─────────  create_object_if_missing()
#     │             │                ───────────────────────► table.create_table()
#     │             │─────────  create_partitioned_table()  ─► partition.create_partitioned_table()
#     │             │─────────  upsert_batch()              ─► table.upsert_table_data()
#     │             │─────────  get_object_count()          ─► table.get_table_row_count()
#     │             │─────────  export_full()               ─► table.export_table_data()
#     │             │─────────  delete()                    ─► table.delete_from_table()
#     │             │─────────  apply_constraints()         ─── (stays in target.py — indexes/FKs/checks/defaults)
#     └─────────────┘
#     ┌─────────────┐
#     │  _cdc        │─────────  apply()  ── calls target.delete() ──► table.delete_from_table()
#     └─────────────┘              apply()  ── calls target.upsert_batch() ─► table.upsert_table_data()
#
# The column-introspection helpers ``_non_computed_column_names`` and
# ``_variant_column_names`` are resolved through the ``core.connectors.mssql``
# package namespace at call time (via local imports inside
# ``export_table_data``) so that test patches on
# ``core.connectors.mssql._non_computed_column_names`` /
# ``core.connectors.mssql._variant_column_names`` are honoured.
# ---------------------------------------------------------------------------
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from core.connectors.base import (
    Schema,
    UnmappedTypeError,
    UpsertResult,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _mssql_column_type,
    _qualify,
    _resolve_mssql_schemas,
    _target_identity_columns,
)
from core.audit_logger import audit_log


# ---------------------------------------------------------------------------
# 1. SOURCE-SIDE TABLE OPERATIONS
#    Called/delegated by MSSQLSourceConnector methods.
#    Discovery and data-export logic for reading from a source database.
# ---------------------------------------------------------------------------

def discover_tables(conn: Any, config: dict[str, Any]) -> list[str]:
    """Discover base tables in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_objects``. Delegates the complete
    table-discovery logic (INFORMATION_SCHEMA.TABLES query, schema filtering,
    identifier validation) that previously lived inside the connector.
    """
    schemas = _resolve_mssql_schemas(config)
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT DISTINCT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' "
                f"AND TABLE_SCHEMA IN ({placeholders}) "
                "ORDER BY TABLE_NAME",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT DISTINCT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' "
                "AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "ORDER BY TABLE_NAME"
            )
        tables = [row[0] for row in cur.fetchall()]
    for t in tables:
        validate_identifier(t, "table")
    return tables


# ---------------------------------------------------------------------------
# 2. SHARED TABLE OPERATIONS
#    Called/delegated by both MSSQLSourceConnector and MSSQLTargetConnector.
#    Row-counting and data-export are identical on both sides; a single
#    implementation serves both connectors.
# ---------------------------------------------------------------------------

def get_table_row_count(
    conn: Any, object_name: str, schema_name: str | None = None
) -> int:
    """Count rows in a table.

    Replaces ``MSSQLSourceConnector.get_object_count`` and
    ``MSSQLTargetConnector.get_object_count``. Both connectors use the
    identical ``SELECT COUNT(*)`` query, so this shared function serves
    both call sites.
    """
    validate_identifier(object_name, "table")
    qualified = _qualify(schema_name, object_name)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {qualified}")
        return cur.fetchone()[0]


def export_table_data(
    conn: Any, object_name: str, schema_name: str | None = None
) -> Iterator[dict]:
    """Export all rows from a table, handling ``sql_variant`` columns.

    Replaces ``MSSQLSourceConnector.export_full`` and
    ``MSSQLTargetConnector.export_full``. Both connectors use the identical
    export logic, so this shared function serves both call sites.

    Computed columns are excluded automatically (via
    ``_non_computed_column_names``), and ``sql_variant`` columns are
    wrapped in ``CAST(... AS NVARCHAR(MAX))`` so pyodbc can read them.
    """
    # Resolved through the package namespace so test patches on
    # ``core.connectors.mssql._non_computed_column_names`` /
    # ``_variant_column_names`` are honoured at call time.
    from core.connectors.mssql import (
        _non_computed_column_names,
        _variant_column_names,
    )

    validate_identifier(object_name, "table")
    qualified = _qualify(schema_name, object_name)
    with conn.cursor() as cur:
        cols = _non_computed_column_names(conn, object_name, schema_name)
        if cols:
            variant_cols = _variant_column_names(conn, object_name, schema_name)
            # Python datetime (and therefore pyodbc's default datetime2
            # conversion) is limited to microseconds. Keep datetime2(7) in a
            # seven-digit SQL text representation until it reaches SQL Server
            # again, where the target column performs the lossless conversion.
            temporal_cols: set[str] = set()
            with conn.cursor() as metadata_cur:
                metadata_cur.execute(
                    "SELECT c.name FROM sys.columns c "
                    "JOIN sys.tables t ON t.object_id = c.object_id "
                    "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                    "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
                    "WHERE s.name = ? AND t.name = ? AND ty.name = 'datetime2'",
                    (schema_name or "dbo", object_name),
                )
                temporal_cols = {row[0] for row in metadata_cur.fetchall()}
            col_exprs = [
                f"CAST({quote_identifier(c)} AS NVARCHAR(MAX)) AS {quote_identifier(c)}"
                if c in variant_cols
                else f"CONVERT(VARCHAR(27), {quote_identifier(c)}, 126) AS {quote_identifier(c)}"
                if c in temporal_cols
                else quote_identifier(c)
                for c in cols
            ]
            col_list = ", ".join(col_exprs)
            cur.execute(f"SELECT {col_list} FROM {qualified}")
        else:
            # Falling back to SELECT * would let pyodbc materialize datetime2
            # as Python datetime (microsecond precision), silently losing the
            # seventh fractional digit. Refuse an untyped export rather than
            # report a successful but lossy migration.
            raise RuntimeError(
                f"Cannot export {qualified}: source columns could not be enumerated"
            )
        columns = [desc[0] for desc in cur.description]
        for row in cur:
            yield dict(zip(columns, row))


# ---------------------------------------------------------------------------
# 3. INTERNAL TABLE HELPERS
#    Shared utilities used by the target-side table creation functions
#    below, and by ``objects/partition.py`` for partitioned-table DDL
#    generation. These are not connector-facing; they operate on a raw *conn*.
# ---------------------------------------------------------------------------

def table_exists(
    conn: Any, object_name: str, schema_name: str | None = None
) -> bool:
    """Return ``True`` if *object_name* exists as a base table in *schema_name*.

    Used by ``create_table`` to implement idempotent creation.
    """
    sn = schema_name or "dbo"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
            (object_name, sn),
        )
        return cur.fetchone() is not None


def _ensure_schema_exists(conn: Any, schema_name: str) -> None:
    """Ensure a schema exists on the target (``dbo`` always exists).

    Shared by ``create_table`` and ``partition.create_partitioned_table``.
    """
    if schema_name == "dbo":
        return
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
            audit_log(
                phase="create_schema",
                status="created",
                details={"schema": schema_name},
            )


def _column_ddl(col: Any, col_type: str) -> str:
    """Build a single ``CREATE TABLE`` column-definition string.

    *col_type* is resolved by the caller; this helper applies only:
      - ``_mssql_column_type`` (size/precision re-attachment)
      - Identity clause (``IDENTITY(seed, increment)``)
      - NULL / NOT NULL nullability

    Computed columns are emitted as ``AS (definition)`` and bypass type
    resolution entirely.

    Shared by ``create_table`` and ``partition.create_partitioned_table``.
    """
    if col.is_computed:
        return f"{col.name} AS ({col.computed_definition})"
    resolved_type = _mssql_column_type(col_type, col.size, col.precision, col.scale)
    identity_part = ""
    if col.is_identity and col.identity_seed is not None and col.identity_increment is not None:
        identity_part = f" IDENTITY({col.identity_seed},{col.identity_increment})"
    null_str = "NULL" if col.nullable else "NOT NULL"
    return f"{col.name} {resolved_type}{identity_part} {null_str}"


# ---------------------------------------------------------------------------
# 4. TARGET-SIDE TABLE OPERATIONS
#    Called/delegated by MSSQLTargetConnector methods.
#    Table creation, data upsert, and row deletion for writing to a target
#    database. Partitioned-table creation lives in ``objects/partition.py``.
# ---------------------------------------------------------------------------

def create_table(conn: Any, schema: Schema, config: dict[str, Any]) -> None:
    """Create a table if it does not already exist.

    Replaces ``MSSQLTargetConnector.create_object_if_missing``.
    Delegates the complete table-creation logic: existence check, schema
    creation, column DDL generation (with UnmappedTypeError on unknown
    types), primary-key constraint, and CREATE TABLE execution.
    """
    validate_identifier(schema.name, "table")
    schema_name = schema.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    qualified = _qualify(schema_name, schema.name)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
            (schema.name, schema_name),
        )
        if cur.fetchone() is not None:
            return

        if schema_name != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": schema_name},
                )

        col_defs: list[str] = []
        for col in schema.columns:
            if col.target_type is None:
                if config.get("source_engine") == "mssql":
                    col_type = col.source_type
                else:
                    raise UnmappedTypeError(
                        table=schema.name,
                        column=col.name,
                        source_type=col.source_type,
                        source_engine=config.get("source_engine"),
                        target_engine="mssql",
                    )
            else:
                col_type = col.target_type
            col_defs.append(_column_ddl(col, col_type))

        if schema.primary_key:
            pk_cols = ", ".join(schema.primary_key)
            # Reproduce an explicitly named source PK constraint. The name goes
            # through the shared identifier quoting helper so uppercase,
            # hyphenated or otherwise awkward identifiers survive. Without a
            # source name the bare clause is emitted exactly as before and
            # SQL Server generates its own name.
            if schema.primary_key_name:
                pk_clause = (
                    f"CONSTRAINT {quote_identifier(schema.primary_key_name)} "
                    f"PRIMARY KEY ({pk_cols})"
                )
            else:
                pk_clause = f"PRIMARY KEY ({pk_cols})"
            col_defs.append(pk_clause)

        ddl = f"CREATE TABLE {qualified} ({', '.join(col_defs)})"
        cur.execute(ddl)
        conn.commit()
        audit_log(
            phase="create_table",
            status="created",
            details={"table": qualified},
        )


def reconcile_table_columns(conn: Any, schema: Schema) -> dict[str, list[str]]:
    """Reconcile source-managed table columns and definitions on MSSQL.

    Missing source tables are created by ``create_table`` first. This method
    deliberately inspects only the named source table and never discovers or
    changes unrelated target-only tables. Target-only columns are dropped;
    source columns are restored to source type, length and nullability. Data
    conversion failures and unsafe dependencies propagate as errors.
    """
    validate_identifier(schema.name, "table")
    schema_name = schema.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    source_columns = {column.name: column for column in schema.columns}
    for column_name in source_columns:
        validate_identifier(column_name, "column")

    dropped_columns: list[str] = []
    altered_columns: list[str] = []
    added_columns: list[str] = []
    deferred_not_null_columns: list[str] = []
    changed_columns: dict[str, Any] = {}
    table_qname = _qualify(schema_name, schema.name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.name, ty.name, c.max_length, c.precision, c.scale, "
                "c.is_nullable, c.is_computed, c.is_identity, ty.is_user_defined, "
                "SCHEMA_NAME(ty.schema_id), TRY_CAST(cc.definition AS NVARCHAR(MAX)) "
                "FROM sys.columns c "
                "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
                "JOIN sys.tables t ON t.object_id = c.object_id "
                "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "LEFT JOIN sys.computed_columns cc "
                "ON cc.object_id = c.object_id AND cc.column_id = c.column_id "
                "WHERE s.name = ? AND t.name = ? ORDER BY c.column_id",
                (schema_name, schema.name),
            )
            target_metadata = {
                row[0]: {
                    "type": row[1].lower(),
                    "max_length": int(row[2]),
                    "precision": int(row[3]),
                    "scale": int(row[4]),
                    "nullable": bool(row[5]),
                    "computed": bool(row[6]),
                    "identity": bool(row[7]),
                    "user_defined": bool(row[8]),
                    "type_schema": row[9],
                    "computed_definition": row[10] if len(row) > 10 else None,
                }
                for row in cur.fetchall()
            }
            target_columns = set(target_metadata)
            extras = sorted(target_columns - set(source_columns))
            missing = sorted(set(source_columns) - target_columns)

            for column_name in missing:
                source_column = source_columns[column_name]
                if source_column.is_computed or source_column.is_identity:
                    special_kind = "computed" if source_column.is_computed else "IDENTITY"
                    raise RuntimeError(
                        f"Cannot safely add missing {special_kind} column "
                        f"{table_qname}.{quote_identifier(column_name)} to an existing table"
                    )
                expected_type = source_column.target_type or _mssql_column_type(
                    source_column.source_type,
                    source_column.size,
                    source_column.precision,
                    source_column.scale,
                )
                # SQL Server cannot add a NOT NULL column to a populated table
                # without inventing values for the existing rows. Add it as
                # nullable (existing rows receive NULL), then FULL cleanup
                # finalizes NOT NULL in the same transaction as row deletion.
                # A failed finalization rolls back that deletion.
                cur.execute(
                    f"ALTER TABLE {table_qname} ADD "
                    f"{quote_identifier(column_name)} {expected_type} NULL"
                )
                added_columns.append(column_name)
                if not source_column.nullable:
                    deferred_not_null_columns.append(column_name)

            for column_name in sorted(set(source_columns) & target_columns):
                source_column = source_columns[column_name]
                target_column = target_metadata[column_name]
                if target_column["computed"] != bool(source_column.is_computed):
                    raise RuntimeError(
                        f"Cannot reconcile computed-column mismatch for "
                        f"{table_qname}.{quote_identifier(column_name)}"
                    )
                if target_column["identity"] != bool(source_column.is_identity):
                    raise RuntimeError(
                        f"Cannot reconcile IDENTITY mismatch for "
                        f"{table_qname}.{quote_identifier(column_name)}"
                    )
                if target_column["computed"]:
                    source_expression = _normalize_computed_expression(
                        source_column.computed_definition
                    )
                    target_expression = _normalize_computed_expression(
                        target_column["computed_definition"]
                    )
                    if source_expression is None or target_expression is None:
                        raise RuntimeError(
                            f"Cannot verify computed-column expression for "
                            f"{table_qname}.{quote_identifier(column_name)}; "
                            "source or target definition is unavailable"
                        )
                    if source_expression != target_expression:
                        raise RuntimeError(
                            f"Cannot safely reconcile computed column "
                            f"{table_qname}.{quote_identifier(column_name)}: "
                            "source and target expressions differ; automatic "
                            "computed-column recreation is unsupported; "
                            f"source={source_column.computed_definition!r}, "
                            f"target={target_column['computed_definition']!r}"
                        )
                    # The expression is the computed column's definition;
                    # SQL Server derives its result type, precision, storage
                    # length, and nullability from that expression. Catalog
                    # reports for those derived fields can vary by metadata
                    # surface without representing a separate DDL change.
                    continue
                expected_type = source_column.target_type or _mssql_column_type(
                    source_column.source_type,
                    source_column.size,
                    source_column.precision,
                    source_column.scale,
                )
                expected_base, expected_length, expected_precision, expected_scale = (
                    _mssql_type_signature(
                        expected_type,
                        source_column.size,
                        source_column.precision,
                        source_column.scale,
                    )
                )
                type_matches = False
                if target_column["user_defined"]:
                    expected_udt = expected_type.replace("[", "").replace("]", "")
                    expected_parts = expected_udt.split(".")
                    if (
                        target_column["type"] != expected_parts[-1].lower()
                        or (len(expected_parts) > 1 and target_column["type_schema"].lower()
                            != expected_parts[-2].lower())
                    ):
                        raise RuntimeError(
                            f"Cannot safely reconcile user-defined type on "
                            f"{table_qname}.{quote_identifier(column_name)}"
                        )
                    type_matches = True
                is_changed = _column_definition_differs(
                    target_column,
                    expected_base,
                    expected_length,
                    expected_precision,
                    expected_scale,
                    bool(source_column.nullable),
                    type_matches=type_matches,
                )
                if is_changed:
                    if target_column["computed"] or target_column["identity"]:
                        special_kind = (
                            "computed-column" if target_column["computed"] else "IDENTITY"
                        )
                        raise RuntimeError(
                            f"Cannot safely reconcile {special_kind} "
                            f"{table_qname}.{quote_identifier(column_name)}: "
                            "the source and target data type, declared parameters, "
                            "or nullability differ; automatic recreation is unsupported"
                        )
                    changed_columns[column_name] = (
                        expected_type,
                        bool(source_column.nullable),
                    )

            for column_name in changed_columns:
                _assert_column_definition_change_is_safe(
                    cur, schema_name, schema.name, column_name
                )

            constraint_states: dict[
                str, list[tuple[str, str, bool, bool, str | None]]
            ] = {
                "check": [], "default": []
            }
            affected_columns = set(changed_columns)
            if affected_columns:
                constraint_states = _drop_recreatable_column_constraints(
                    cur, schema_name, schema.name, affected_columns
                )

            for column_name in extras:
                cur.execute(
                    f"ALTER TABLE {table_qname} DROP COLUMN "
                    f"{quote_identifier(column_name)}"
                )
                dropped_columns.append(column_name)

            for column_name, (type_name, nullable) in changed_columns.items():
                cur.execute(
                    f"ALTER TABLE {table_qname} ALTER COLUMN "
                    f"{quote_identifier(column_name)} {type_name} "
                    f"{'NULL' if nullable else 'NOT NULL'}"
                )
                altered_columns.append(column_name)

            _restore_column_constraints(
                cur, table_qname, constraint_states
            )
            if altered_columns:
                _refresh_dependent_views(
                    cur, schema_name, schema.name, altered_columns
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    audit_log(
        phase="reconcile_table_columns",
        status="reconciled" if dropped_columns or altered_columns or added_columns else "unchanged",
        details={
            "table": table_qname,
            "dropped_columns": dropped_columns,
            "altered_columns": altered_columns,
            "added_columns": added_columns,
            "deferred_not_null_columns": deferred_not_null_columns,
        },
    )
    outcome = {"dropped_columns": dropped_columns, "altered_columns": altered_columns}
    if added_columns:
        outcome["added_columns"] = added_columns
    if deferred_not_null_columns:
        outcome["deferred_not_null_columns"] = deferred_not_null_columns
    return outcome


def finalize_required_columns(
    cur: Any, schema: Schema, column_names: list[str]
) -> None:
    """Finalize staged source NOT NULL columns after FULL cleanup emptied tables."""
    schema_name = schema.schema_name or "dbo"
    table_qname = _qualify(schema_name, schema.name)
    source_columns = {column.name: column for column in schema.columns}
    for column_name in column_names:
        source_column = source_columns.get(column_name)
        if source_column is None or source_column.nullable:
            raise RuntimeError(
                f"Invalid deferred NOT NULL column {table_qname}.{column_name}"
            )
        if source_column.is_computed or source_column.is_identity:
            raise RuntimeError(
                f"Cannot finalize unsupported special column "
                f"{table_qname}.{quote_identifier(column_name)}"
            )
        expected_type = source_column.target_type or _mssql_column_type(
            source_column.source_type,
            source_column.size,
            source_column.precision,
            source_column.scale,
        )
        cur.execute(
            f"ALTER TABLE {table_qname} ALTER COLUMN "
            f"{quote_identifier(column_name)} {expected_type} NOT NULL"
        )


def _mssql_type_signature(
    type_name: str,
    size: int | None,
    precision: int | None,
    scale: int | None,
) -> tuple[str, int, int, int]:
    """Return SQL Server sys.types/sys.columns comparable type metadata."""
    normalized = type_name.strip().lower()
    if normalized.endswith("(max)"):
        base = normalized[:-5]
        length = -1
    elif "(" in normalized and normalized.endswith(")"):
        base, args = normalized[:-1].split("(", 1)
        values = [part.strip() for part in args.split(",")]
        if len(values) == 2:
            precision, scale = int(values[0]), int(values[1])
        elif values[0].isdigit():
            if base in {"time", "datetime2", "datetimeoffset"}:
                scale = int(values[0])
            elif base == "float":
                precision = int(values[0])
            else:
                size = int(values[0])
        length = _expected_storage_length(base, size, precision, scale)
    else:
        base = normalized
        length = _expected_storage_length(base, size, precision, scale)
    if precision is None and base in {"decimal", "numeric"}:
        precision = 18
    elif precision is None and base == "float":
        precision = 53
    if scale is None and base in {"decimal", "numeric"}:
        scale = 0
    elif scale is None and base in {"time", "datetime2", "datetimeoffset"}:
        scale = 7
    return (
        base,
        length,
        int(precision or 0),
        int(scale or 0),
    )


def _canonical_mssql_type(type_name: str) -> str:
    normalized = " ".join(type_name.strip().lower().split())
    return {
        "numeric": "decimal",
        "timestamp": "rowversion",
        "integer": "int",
    }.get(normalized, normalized)


def _column_definition_differs(
    target: dict[str, Any],
    expected_base: str,
    expected_length: int,
    expected_precision: int,
    expected_scale: int,
    expected_nullable: bool,
    *,
    type_matches: bool,
) -> bool:
    """Compare actual SQL definition parameters, ignoring catalog-only fields.

    ``sys.columns.precision`` and ``max_length`` are populated for types where
    they are not independently declared (for example BIT). Comparing those
    values for every type can cause a spurious ALTER on an otherwise matching
    column. Compare only parameters that are part of that type's definition.
    """
    base = _canonical_mssql_type(expected_base)
    target_base = _canonical_mssql_type(target["type"])
    if not type_matches and target_base != base:
        return True

    if not type_matches:
        if base in {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}:
            if target["max_length"] != expected_length:
                return True
        elif base in {"decimal", "float"}:
            if target["precision"] != expected_precision:
                return True
            if base == "decimal" and target["scale"] != expected_scale:
                return True
        elif base in {"time", "datetime2", "datetimeoffset"}:
            if target["scale"] != expected_scale:
                return True

    return target["nullable"] != expected_nullable


_EXPRESSION_TOKEN = re.compile(
    r"\s+|--[^\r\n]*|/\*.*?\*/|N?'(?:''|[^'])*'|"
    r"\[(?:\]\]|[^\]])*\]|\"(?:\"\"|[^\"])*\"|"
    r"[A-Za-z_][A-Za-z0-9_@$#]*|\d+(?:\.\d+)?|<=|>=|<>|!=|.",
    re.IGNORECASE | re.DOTALL,
)
_EXPRESSION_KEYWORDS = {
    "as", "and", "or", "not", "null", "is", "case", "when", "then",
    "else", "end", "second", "minute", "hour", "day", "dayofyear",
    "weekday", "week", "month", "quarter", "year", "millisecond",
    "microsecond", "nanosecond", "iso_week", "true", "false",
}


def _normalize_computed_expression(expression: str | None) -> tuple[str, ...] | None:
    """Token-normalize SQL expressions without changing identifier/literal meaning.

    Whitespace/comments, bracket-vs-unquoted identifier spelling, keyword or
    function casing, and redundant parentheses around a single atom are
    normalized. Identifier case and literal contents are retained so a
    materially different expression is not accepted as equivalent.
    """
    if expression is None:
        return None
    raw_tokens: list[str] = []
    for match in _EXPRESSION_TOKEN.finditer(expression):
        token = match.group(0)
        if not token or token.isspace() or token.startswith("--") or token.startswith("/*"):
            continue
        if token.startswith("[") and token.endswith("]"):
            raw_tokens.append("id:" + token[1:-1].replace("]]", "]"))
        elif token.startswith('"') and token.endswith('"'):
            raw_tokens.append("id:" + token[1:-1].replace('""', '"'))
        elif token[:1].lower() == "n" and len(token) > 1 and token[1] == "'":
            raw_tokens.append("N" + token[1:])
        elif token.startswith("'"):
            raw_tokens.append(token)
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_@$#]*", token):
            raw_tokens.append(token)
        else:
            raw_tokens.append(token)

    tokens: list[str] = []
    for index, token in enumerate(raw_tokens):
        if token.startswith("id:") or token.startswith("'") or token.startswith("N'"):
            tokens.append(token)
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_@$#]*", token):
            lowered = token.lower()
            next_token = raw_tokens[index + 1] if index + 1 < len(raw_tokens) else None
            tokens.append(lowered if lowered in _EXPRESSION_KEYWORDS or next_token == "(" else f"id:{token}")
        else:
            tokens.append(token)

    # Strip only parentheses that wrap an entire expression or one atom.
    # Parenthesized compound expressions remain intact to preserve precedence.
    changed = True
    while changed:
        changed = False
        if len(tokens) >= 2 and tokens[0] == "(" and tokens[-1] == ")":
            depth = 0
            encloses_all = True
            for index, token in enumerate(tokens):
                if token == "(":
                    depth += 1
                elif token == ")":
                    depth -= 1
                    if depth == 0 and index != len(tokens) - 1:
                        encloses_all = False
                        break
            if encloses_all:
                tokens = tokens[1:-1]
                changed = True
                continue
        for index in range(len(tokens) - 2):
            atom = tokens[index + 1]
            if (
                tokens[index] == "("
                and tokens[index + 2] == ")"
                and (atom.startswith("id:") or atom.startswith("'") or atom.startswith("N'")
                     or re.fullmatch(r"\d+(?:\.\d+)?", atom) is not None)
            ):
                tokens[index:index + 3] = [atom]
                changed = True
                break
    return tuple(tokens)


def _expected_storage_length(
    type_name: str,
    size: int | None,
    precision: int | None = None,
    scale: int | None = None,
) -> int:
    normalized = type_name.strip().lower()
    if normalized in {"nvarchar", "nchar"} and size is not None and size >= 0:
        return size * 2
    if normalized in {"decimal", "numeric"}:
        p = int(precision or 18)
        return 5 if p <= 9 else 9 if p <= 19 else 13 if p <= 28 else 17
    if normalized in {"time", "datetime2", "datetimeoffset"}:
        effective_scale = 7 if scale is None else scale
        temporal_size = 3 if effective_scale <= 2 else 4 if effective_scale <= 4 else 5
        if normalized == "datetime2":
            return temporal_size + 3
        if normalized == "datetimeoffset":
            return temporal_size + 5
        return temporal_size
    return {
        "bit": 1, "tinyint": 1, "smallint": 2, "int": 4, "bigint": 8,
        "real": 4, "money": 8, "smallmoney": 4,
        "date": 3, "datetime": 8, "smalldatetime": 4,
        "uniqueidentifier": 16, "timestamp": 8, "rowversion": 8,
    }.get(
        normalized,
        (4 if int(precision or 53) <= 24 else 8)
        if normalized == "float"
        else int(size or 0),
    )


def _assert_column_definition_change_is_safe(
    cur: Any, schema_name: str, table_name: str, column_name: str
) -> None:
    """Reject dependencies SQL Server cannot safely rebuild from metadata."""
    cur.execute(
        "SELECT DISTINCT i.name, i.filter_definition, "
        "CASE WHEN EXISTS ("
        "SELECT 1 FROM sys.index_columns ic "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND c.name = ?"
        ") THEN 1 ELSE 0 END "
        "FROM sys.indexes i "
        "JOIN sys.tables t ON t.object_id = i.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name = ? AND t.name = ? AND i.index_id > 0 "
        "AND (i.filter_definition IS NOT NULL OR EXISTS ("
        "SELECT 1 FROM sys.index_columns ic "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND c.name = ?))",
        (column_name, schema_name, table_name, column_name),
    )
    indexes = []
    for row in cur.fetchall():
        index_name = row[0]
        filter_definition = row[1] if len(row) > 1 else None
        contains_column = bool(row[2]) if len(row) > 2 else True
        if contains_column or _filter_references_column(filter_definition, column_name):
            indexes.append(index_name)
    if indexes:
        raise RuntimeError(
            f"Cannot alter {schema_name}.{table_name}.{column_name}; "
            f"dependent indexes (including filtered-index predicates) require "
            f"explicit review/recreation: {indexes}"
        )

    cur.execute(
        "SELECT fk.name FROM sys.foreign_key_columns fkc "
        "JOIN sys.foreign_keys fk ON fk.object_id = fkc.constraint_object_id "
        "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
            "AND pc.column_id = fkc.parent_column_id "
        "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
            "AND rc.column_id = fkc.referenced_column_id "
        "WHERE (fkc.parent_object_id = OBJECT_ID(?) AND pc.name = ?) "
        "OR (fkc.referenced_object_id = OBJECT_ID(?) AND rc.name = ?)",
        (f"{schema_name}.{table_name}", column_name,
         f"{schema_name}.{table_name}", column_name),
    )
    foreign_keys = [row[0] for row in cur.fetchall()]
    if foreign_keys:
        raise RuntimeError(
            f"Cannot alter {schema_name}.{table_name}.{column_name}; "
            f"dependent foreign keys require explicit recreation: {foreign_keys}"
        )

    cur.execute(
        "SELECT o.name, o.type_desc FROM sys.sql_expression_dependencies d "
        "JOIN sys.objects o ON o.object_id = d.referencing_id "
        "JOIN sys.sql_modules m ON m.object_id = o.object_id "
        "JOIN sys.schemas os ON os.schema_id = o.schema_id "
        "JOIN sys.tables t ON t.object_id = d.referenced_id "
        "JOIN sys.schemas ts ON ts.schema_id = t.schema_id "
        "WHERE ts.name = ? AND t.name = ? AND d.referenced_minor_id = "
        "COLUMNPROPERTY(d.referenced_id, ?, 'ColumnId') "
        "AND m.is_schema_bound = 1",
        (schema_name, table_name, column_name),
    )
    schema_bound_modules = [row[0] for row in cur.fetchall()]
    if schema_bound_modules:
        raise RuntimeError(
            f"Cannot alter {schema_name}.{table_name}.{column_name}; "
            f"schema-bound views/functions depend on it: {schema_bound_modules}"
        )

    cur.execute(
        "SELECT c.name FROM sys.computed_columns cc "
        "JOIN sys.columns c ON c.object_id = cc.object_id AND c.column_id = cc.column_id "
        "JOIN sys.tables t ON t.object_id = c.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "JOIN sys.sql_expression_dependencies d "
        "ON d.referencing_id = cc.object_id "
        "WHERE s.name = ? AND t.name = ? AND d.referenced_id = t.object_id "
        "AND d.referencing_minor_id = cc.column_id "
        "AND d.referenced_minor_id = COLUMNPROPERTY(t.object_id, ?, 'ColumnId')",
        (schema_name, table_name, column_name),
    )
    computed_dependencies = [row[0] for row in cur.fetchall()]
    if computed_dependencies:
        raise RuntimeError(
            f"Cannot alter {schema_name}.{table_name}.{column_name}; "
            f"computed columns depend on it: {computed_dependencies}"
        )


def _filter_references_column(filter_definition: str | None, column_name: str) -> bool:
    if not filter_definition:
        return False
    escaped = re.escape(column_name)
    pattern = (
        rf"(?<![A-Za-z0-9_])(?:\[{escaped}\]|\"{escaped}\"|{escaped})"
        rf"(?![A-Za-z0-9_])"
    )
    return re.search(pattern, filter_definition, flags=re.IGNORECASE) is not None


def _drop_recreatable_column_constraints(
    cur: Any,
    schema_name: str,
    table_name: str,
    columns: set[str],
) -> dict[str, list[tuple[str, str, bool, bool, str | None]]]:
    """Snapshot/drop affected CHECK and DEFAULT constraints transactionally."""
    table_qname = _qualify(schema_name, table_name)
    states: dict[str, list[tuple[str, str, bool, bool, str | None]]] = {
        "check": [], "default": []
    }
    for column_name in sorted(columns):
        cur.execute(
            "SELECT cc.name, cc.definition, cc.is_disabled, cc.is_not_trusted "
            "FROM sys.check_constraints cc "
            "JOIN sys.sql_expression_dependencies d ON d.referencing_id = cc.object_id "
            "WHERE cc.parent_object_id = OBJECT_ID(?) AND d.referenced_minor_id = "
            "COLUMNPROPERTY(d.referenced_id, ?, 'ColumnId')",
            (f"{schema_name}.{table_name}", column_name),
        )
        for name, definition, disabled, untrusted in cur.fetchall():
            state = (name, definition, bool(disabled), bool(untrusted), None)
            if state not in states["check"]:
                states["check"].append(state)
        cur.execute(
            "SELECT dc.name, dc.definition, 0, 0, c.name FROM sys.default_constraints dc "
            "JOIN sys.columns c ON c.object_id = dc.parent_object_id "
            "AND c.column_id = dc.parent_column_id "
            "JOIN sys.tables t ON t.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ? AND t.name = ? AND c.name = ?",
            (schema_name, table_name, column_name),
        )
        for name, definition, disabled, untrusted, bound_column in cur.fetchall():
            state = (name, definition, bool(disabled), bool(untrusted), bound_column)
            if state not in states["default"]:
                states["default"].append(state)

    for name, _, _, _, _ in states["check"]:
        cur.execute(
            f"ALTER TABLE {table_qname} DROP CONSTRAINT {quote_identifier(name)}"
        )
    for name, _, _, _, _ in states["default"]:
        cur.execute(
            f"ALTER TABLE {table_qname} DROP CONSTRAINT {quote_identifier(name)}"
        )
    return states


def _restore_column_constraints(
    cur: Any,
    table_qname: str,
    states: dict[str, list[tuple[str, str, bool, bool, str | None]]],
) -> None:
    for name, definition, _, _, column_name in states["default"]:
        if column_name is None:
            raise RuntimeError(f"Cannot restore default constraint {name}: column unknown")
        cur.execute(
            f"ALTER TABLE {table_qname} ADD CONSTRAINT {quote_identifier(name)} "
            f"DEFAULT {definition} FOR {quote_identifier(column_name)}"
        )
    for name, definition, disabled, untrusted, _ in states["check"]:
        add_mode = "WITH NOCHECK" if untrusted else "WITH CHECK"
        cur.execute(
            f"ALTER TABLE {table_qname} {add_mode} ADD CONSTRAINT "
            f"{quote_identifier(name)} CHECK {definition}"
        )
        if disabled:
            cur.execute(
                f"ALTER TABLE {table_qname} NOCHECK CONSTRAINT {quote_identifier(name)}"
            )


def _refresh_dependent_views(
    cur: Any, schema_name: str, table_name: str, columns: list[str]
) -> None:
    cur.execute(
        "SELECT DISTINCT os.name, o.name FROM sys.sql_expression_dependencies d "
        "JOIN sys.objects o ON o.object_id = d.referencing_id "
        "JOIN sys.schemas os ON os.schema_id = o.schema_id "
        "JOIN sys.tables t ON t.object_id = d.referenced_id "
        "JOIN sys.schemas ts ON ts.schema_id = t.schema_id "
        "WHERE ts.name = ? AND t.name = ? AND o.type = 'V'",
        (schema_name, table_name),
    )
    views = cur.fetchall()
    for view_schema, view_name in views:
        # sp_refreshview accepts a multipart identifier as text. Use SQL
        # Server's bracket quoting inside that text (rather than the DDL
        # helper's double-quoted form) so unusual but valid names resolve.
        view_qname = (
            f"[{str(view_schema).replace(']', ']]')}]."
            f"[{str(view_name).replace(']', ']]')}]"
        )
        cur.execute(
            f"EXEC sys.sp_refreshview N'{view_qname.replace(chr(39), chr(39) * 2)}'"
        )


def upsert_table_data(
    conn: Any,
    object_name: str,
    rows: Iterator[dict[str, Any]],
    schema: Schema | None = None,
) -> UpsertResult:
    """Upsert a batch of rows into a table using ``MERGE``.

    Replaces ``MSSQLTargetConnector.upsert_batch``.
    Delegates the complete MERGE-based upsert logic including:
    IDENTITY_INSERT toggling (only when the target really has identity columns),
    sql_variant column CAST handling, identity column exclusion from UPDATE SET,
    and transaction commit/rollback.

    The CDC engine (``cdc.py``) reaches this via ``target.upsert_batch()``
    which delegates to this function.
    """
    validate_identifier(object_name, "table")
    result = UpsertResult()
    batch = list(rows)

    if not batch:
        return result

    schema_name = (schema.schema_name or "dbo") if schema else None
    qualified = _qualify(schema_name, object_name)
    target_id_cols = set(_target_identity_columns(conn, object_name, schema_name))
    schema_id_cols = {c.name for c in (schema.columns if schema else []) if c.is_identity}
    variant_cols: set[str] = set()
    datetime2_scales: dict[str, int] = {}
    if schema:
        variant_cols = {
            c.name
            for c in schema.columns
            if c.source_type and "sql_variant" in c.source_type.lower()
        }
        datetime2_scales = {
            c.name: min(7, max(0, int(c.scale if c.scale is not None else 7)))
            for c in schema.columns
            if c.source_type
            and c.source_type.strip().lower().split("(", 1)[0].strip("[] ") == "datetime2"
        }
    with conn.cursor() as cur:
        columns = list(batch[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(
            "CAST(? AS SQL_VARIANT)" if col in variant_cols
            else f"CONVERT(datetime2({datetime2_scales[col]}), ?, 126)"
            if col in datetime2_scales
            else "?"
            for col in columns
        )
        updatable_cols = [c for c in columns if c not in schema_id_cols]
        update_set = ", ".join(
            f"target.{col} = source.{col}" for col in updatable_cols
        )

        pk_cols = schema.primary_key if schema else []
        if pk_cols:
            pk_clause = " AND ".join(f"target.{col} = source.{col}" for col in pk_cols)
            on_clause = pk_clause
        else:
            on_clause = "1=0"

        sql = (
            f"MERGE INTO {qualified} AS target "
            f"USING (SELECT {placeholders}) AS source ({col_names}) "
            f"ON {on_clause} "
            f"WHEN MATCHED THEN UPDATE SET {update_set} "
            f"WHEN NOT MATCHED THEN INSERT ({col_names}) VALUES (source.{col_names});"
        )

        need_id_insert = bool(target_id_cols & set(columns))
        try:
            if need_id_insert:
                cur.execute(f"SET IDENTITY_INSERT {qualified} ON")
            for row in batch:
                values = [row.get(col) for col in columns]
                cur.execute(sql, values)
            conn.commit()
            result.success_count = len(batch)
            audit_log(
                phase="upsert_batch",
                status="success",
                details={"table": qualified, "count": len(batch)},
            )
        except Exception as exc:
            conn.rollback()
            result.failure_count = len(batch)
            result.errors.append(str(exc))
            result.failed_items.extend(batch)
            audit_log(
                phase="upsert_batch",
                status="failure",
                details={"table": qualified, "error": str(exc)},
            )
        finally:
            if need_id_insert:
                cur.execute(f"SET IDENTITY_INSERT {qualified} OFF")

    return result


def delete_from_table(
    conn: Any,
    object_name: str,
    document: dict[str, Any],
    schema: Schema | None = None,
) -> None:
    """Delete rows from a table by primary key (or ``id`` if no PK).

    Used by CDC delete operations.

    Replaces ``MSSQLTargetConnector.delete``. The CDC engine
    (``cdc.py``) reaches this via ``target.delete()`` which delegates
    to this function.
    """
    validate_identifier(object_name, "table")
    schema_name = (schema.schema_name or "dbo") if schema else None
    qualified = _qualify(schema_name, object_name)
    with conn.cursor() as cur:
        if schema and schema.primary_key:
            conditions = []
            values = []
            for pk_col in schema.primary_key:
                conditions.append(f"{pk_col} = ?")
                values.append(document.get(pk_col))
            where_clause = " AND ".join(conditions)
            cur.execute(f"DELETE FROM {qualified} WHERE {where_clause}", values)
        else:
            cur.execute(
                f"DELETE FROM {qualified} WHERE id = ?",
                (document.get("id"),),
            )
        conn.commit()
        audit_log(
            phase="cdc_delete",
            status="deleted",
            details={"table": qualified},
        )
