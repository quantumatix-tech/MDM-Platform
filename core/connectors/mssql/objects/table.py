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
#     │             │─────────  create_partitioned_table()  ─► table.create_partitioned_table()
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
            col_exprs = [
                f"CAST({quote_identifier(c)} AS NVARCHAR(MAX)) AS {quote_identifier(c)}"
                if c in variant_cols
                else quote_identifier(c)
                for c in cols
            ]
            col_list = ", ".join(col_exprs)
            cur.execute(f"SELECT {col_list} FROM {qualified}")
        else:
            cur.execute(f"SELECT * FROM {qualified}")
        columns = [desc[0] for desc in cur.description]
        for row in cur:
            yield dict(zip(columns, row))


# ---------------------------------------------------------------------------
# 3. INTERNAL TABLE HELPERS
#    Shared utilities used by the target-side table creation functions
#    below (and potentially by future object modules for indexes, triggers,
#    etc.). These are not connector-facing; they operate on a raw *conn*.
# ---------------------------------------------------------------------------

def table_exists(
    conn: Any, object_name: str, schema_name: str | None = None
) -> bool:
    """Return ``True`` if *object_name* exists as a base table in *schema_name*.

    Used by ``create_table`` and ``create_partitioned_table`` to implement
    idempotent creation.
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

    Shared by ``create_table`` and ``create_partitioned_table`` to avoid
    duplicate schema-creation logic. Future object modules (indexes, triggers,
    etc.) can also import and reuse this helper.
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

    Shared by ``create_table`` and ``create_partitioned_table``.
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
#    Table creation, partitioned table creation, data upsert, and row
#    deletion for writing to a target database.
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
            col_defs.append(f"PRIMARY KEY ({pk_cols})")

        ddl = f"CREATE TABLE {qualified} ({', '.join(col_defs)})"
        cur.execute(ddl)
        conn.commit()
        audit_log(
            phase="create_table",
            status="created",
            details={"table": qualified},
        )


def create_partitioned_table(
    conn: Any,
    schema: Schema,
    partition_scheme_name: str,
    partition_column: str,
) -> None:
    """Create a table with partition-scheme binding.

    Replaces ``MSSQLTargetConnector.create_partitioned_table``.
    Delegates the complete partitioned-table creation logic: existence
    check, schema creation, column DDL generation, primary-key constraint,
    and CREATE TABLE ... ON partition_scheme(column) execution.
    """
    validate_identifier(schema.name, "table")
    schema_name = schema.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    validate_identifier(partition_scheme_name, "partition scheme")
    validate_identifier(partition_column, "column")
    qualified = _qualify(schema_name, schema.name)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
            (schema.name, schema_name),
        )
        if cur.fetchone() is not None:
            audit_log(
                phase="create_partitioned_table",
                status="exists",
                details={"table": qualified},
            )
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
            col_type = col.source_type or col.target_type or "nvarchar"
            col_defs.append(_column_ddl(col, col_type))

        if schema.primary_key:
            pk_cols = ", ".join(schema.primary_key)
            col_defs.append(f"PRIMARY KEY ({pk_cols})")

        ddl = (
            f"CREATE TABLE {qualified} ({', '.join(col_defs)}) "
            f"ON {partition_scheme_name}({partition_column})"
        )
        try:
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_partitioned_table",
                status="created",
                details={"table": qualified},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_partitioned_table",
                status="failed",
                details={"table": qualified, "reason": str(exc)},
            )
            raise


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
    if schema:
        variant_cols = {
            c.name
            for c in schema.columns
            if c.source_type and "sql_variant" in c.source_type.lower()
        }
    with conn.cursor() as cur:
        columns = list(batch[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(
            "CAST(? AS SQL_VARIANT)" if col in variant_cols else "?"
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
