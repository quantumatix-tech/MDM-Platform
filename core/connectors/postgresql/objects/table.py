"""PostgreSQL Table object implementation.

Reusable table-specific logic for source discovery and target creation.
Every function takes an explicit ``conn`` parameter so that neither
``PostgresSourceConnector`` nor ``PostgresTargetConnector`` is imported,
which keeps the object layer free of connector dependencies.

Responsibilities:
  - table discovery (excluding partition children)
  - row counts
  - streaming data export (source and target variants)
  - psycopg3 value coercion
  - CREATE TABLE generation
  - data load (upsert) and CDC delete
"""
from __future__ import annotations

import decimal
from collections.abc import Iterator
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    Schema,
    UpsertResult,
    quote_identifier,
    validate_identifier,
)
from core.connectors.postgresql._models import _qualify


# ============================================================================
# SOURCE-SIDE TABLE OPERATIONS
# ============================================================================

def discover_tables(conn: Any, schemas: tuple[str, ...]) -> list[str]:
    """Return user base tables in the configured schemas.

    Partition child tables are excluded (``relispartition = true``) so only
    partition parents and regular tables are returned; the children are
    discovered separately by ``partition.discover_partitions``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.table_name "
            "FROM information_schema.tables t "
            "LEFT JOIN pg_class c "
            "  ON c.relname = t.table_name "
            "  AND c.relnamespace = ANY(SELECT oid FROM pg_namespace WHERE nspname = ANY(%s)) "
            "WHERE t.table_schema = ANY(%s) "
            "  AND t.table_type = 'BASE TABLE' "
            "  AND (c.relispartition IS NULL OR c.relispartition = false) "
            "ORDER BY t.table_name",
            (list(schemas), list(schemas)),
        )
        tables = [row[0] for row in cur.fetchall()]
    for t in tables:
        validate_identifier(t, "table")
    return tables


def get_row_count(conn: Any, object_name: str, schema_name: str | None = None) -> int:
    """Return the number of rows in a table.

    Used by both the source and the target connector: the orchestrator reads
    the source count before migrating and the target count afterwards to
    validate the transfer.
    """
    validate_identifier(object_name, "table")
    table_name = f"{schema_name}.{object_name}" if schema_name else object_name
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table_name}")
        return cur.fetchone()[0]


def coerce_value(v: Any) -> Any:
    """Coerce psycopg3 types that don't pass cleanly through upsert_batch.

    Recurses into lists so that PostgreSQL array values are coerced element by
    element.
    """
    if v is None:
        return None
    if isinstance(v, memoryview):
        return bytes(v)          # bytea -> bytes (psycopg3 can INSERT bytes as bytea)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, decimal.Decimal):
        return float(v)          # numeric / money coercion
    if isinstance(v, list):
        return [coerce_value(i) for i in v]  # PG arrays
    return v


def export_table_data(
    conn: Any,
    object_name: str,
    schemas: tuple[str, ...],
    statement_timeout_ms: int = 0,
    schema_name: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream all rows from *object_name* using a server-side cursor.

    A 5 second ``lock_timeout`` is set so the SELECT never queues behind a
    long-running DDL lock — it fails fast rather than waiting forever.
    ``statement_timeout`` is configurable (default 0 = disabled) so callers can
    cap run-away exports.

    ``schemas`` is currently unused by this query but is part of the object
    function signature so the export honours the same discovery scope as the
    rest of the object layer.
    """
    validate_identifier(object_name, "table")
    table_name = f"{schema_name}.{object_name}" if schema_name else object_name
    with conn.cursor() as setup_cur:
        setup_cur.execute("SET LOCAL lock_timeout = '5s'")
        if statement_timeout_ms:
            setup_cur.execute(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")
    with conn.cursor(name=f"export_{object_name}") as cur:
        cur.execute(f"SELECT * FROM {table_name}")
        columns = [desc.name for desc in cur.description]
        for row in cur:
            yield {k: coerce_value(v) for k, v in zip(columns, row)}


# ============================================================================
# TARGET-SIDE TABLE OPERATIONS
# ============================================================================

def create_table(conn: Any, schema: Schema) -> None:
    """Create the target table when it does not already exist.

    Emits columns (including ``GENERATED ALWAYS AS (expr) STORED``, inline
    ``DEFAULT`` and inline ``PRIMARY KEY``) and appends ``PARTITION BY`` when
    the source table was partitioned.

    A DDL failure is rolled back and re-raised, because a table that cannot be
    created makes every later phase for that table meaningless. Rolling back
    first is what keeps a single bad DDL from poisoning the connection for
    subsequent tables.
    """
    validate_identifier(schema.name, "table")
    table_schema = schema.schema_name or "public"
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = %s AND table_schema = %s",
                (schema.name, table_schema),
            )
            if cur.fetchone() is not None:
                return

            col_defs = []
            for col in schema.columns:
                col_type = col.target_type or col.source_type
                if col.generated:
                    # GENERATED ALWAYS AS (expr) STORED — do NOT include NULL/DEFAULT
                    col_defs.append(
                        f"{col.name} {col_type} GENERATED ALWAYS AS ({col.generated}) STORED"
                    )
                elif col.is_identity:
                    # GENERATED ALWAYS/BY DEFAULT AS IDENTITY
                    kind = col.identity_kind if col.identity_kind in ("ALWAYS", "BY DEFAULT") else "BY DEFAULT"
                    seed = col.identity_seed
                    inc = col.identity_increment
                    opts = []
                    if seed is not None:
                        opts.append(f"START WITH {seed}")
                    if inc is not None:
                        opts.append(f"INCREMENT BY {inc}")
                    opt_str = f" ({' '.join(opts)})" if opts else ""
                    null_str = "NOT NULL"  # identity columns are implicitly NOT NULL in PG
                    col_defs.append(
                        f"{col.name} {col_type} GENERATED {kind} AS IDENTITY{opt_str} {null_str}"
                    )
                else:
                    null_str = "NULL" if col.nullable else "NOT NULL"
                    default_str = f" DEFAULT {col.default}" if col.default else ""
                    col_defs.append(f"{col.name} {col_type}{default_str} {null_str}")

            if schema.primary_key:
                pk_cols = ", ".join(schema.primary_key)
                # Reproduce an explicitly named source PK constraint. The name is
                # quoted via the shared helper so uppercase, hyphenated or
                # otherwise awkward identifiers survive; the columns keep their
                # existing unquoted form. When the source had no explicit name,
                # the bare clause is emitted exactly as before and PostgreSQL
                # generates its own name.
                if schema.primary_key_name:
                    pk_clause = (
                        f"CONSTRAINT {quote_identifier(schema.primary_key_name)} "
                        f"PRIMARY KEY ({pk_cols})"
                    )
                else:
                    pk_clause = f"PRIMARY KEY ({pk_cols})"
                col_defs.append(pk_clause)

            table_qname = _qualify(table_schema, schema.name)
            ddl = f"CREATE TABLE {table_qname} ({', '.join(col_defs)})"
            if schema.partition_key:
                ddl += f" PARTITION BY {schema.partition_key}"
            cur.execute(ddl)
            conn.commit()
            audit_log(phase="create_table", status="created", details={"table": schema.name})
        except Exception:
            # Always roll back so a single bad DDL does not poison the
            # connection for subsequent tables / phases.
            try:
                conn.rollback()
            except Exception:
                pass
            raise


def upsert_table_data(
    conn: Any,
    object_name: str,
    rows: Iterator[dict[str, Any]],
    schema: Schema | None = None,
) -> UpsertResult:
    """Load a batch of rows into the target table.

    Uses ``ON CONFLICT (pk) DO UPDATE`` when the schema carries a primary key,
    otherwise ``ON CONFLICT DO NOTHING``. A failure is reported through the
    returned ``UpsertResult`` rather than raised, so a bad batch does not abort
    the transfer.
    """
    validate_identifier(object_name, "table")
    result = UpsertResult()
    batch = list(rows)
    if not batch:
        return result

    # Strip GENERATED ALWAYS columns — PostgreSQL rejects explicit inserts into them
    if schema:
        generated_cols = {col.name for col in schema.columns if col.generated}
        if generated_cols:
            batch = [{k: v for k, v in row.items() if k not in generated_cols} for row in batch]

    all_columns: list[str] = []
    seen: set[str] = set()
    for row in batch:
        for col in row:
            if col not in seen:
                all_columns.append(col)
                seen.add(col)

    col_names = ", ".join(all_columns)
    placeholders = ", ".join("%s" for _ in all_columns)
    update_set = ", ".join(f"{col} = EXCLUDED.{col}" for col in all_columns)

    pk_cols = schema.primary_key if schema else []
    if pk_cols:
        pk_clause = ", ".join(pk_cols)
        conflict_clause = f"ON CONFLICT ({pk_clause}) DO UPDATE"
    else:
        conflict_clause = "ON CONFLICT DO NOTHING"

    table_name = _qualify(schema.schema_name if schema else None, object_name)
    sql = (
        f"INSERT INTO {table_name} ({col_names}) "
        f"VALUES ({placeholders}) "
        f"{conflict_clause} SET {update_set}"
        if pk_cols else
        f"INSERT INTO {table_name} ({col_names}) "
        f"VALUES ({placeholders}) "
        f"{conflict_clause}"
    )

    values_list = [[row.get(col) for col in all_columns] for row in batch]

    with conn.cursor() as cur:
        try:
            cur.executemany(sql, values_list)
            conn.commit()
            result.success_count = len(batch)
            audit_log(phase="upsert_batch", status="success",
                      details={"table": object_name, "count": len(batch)})
        except Exception as exc:
            conn.rollback()
            result.failure_count = len(batch)
            result.errors.append(str(exc))
            result.failed_items.extend(batch)
            audit_log(phase="upsert_batch", status="failure",
                      details={"table": object_name, "error": str(exc)})
    return result


def delete_row(
    conn: Any,
    object_name: str,
    document: dict[str, Any],
    schema: Schema | None = None,
) -> None:
    """Delete a single row by primary key.

    Falls back to ``WHERE id = %s`` when no primary key is available. Used by
    the CDC engine, which supplies a minimal Schema built from the pgoutput
    relation message.
    """
    validate_identifier(object_name, "table")
    with conn.cursor() as cur:
        if schema and schema.primary_key:
            conditions = [f"{pk_col} = %s" for pk_col in schema.primary_key]
            values = [document.get(pk_col) for pk_col in schema.primary_key]
            where_clause = " AND ".join(conditions)
            cur.execute(f"DELETE FROM {object_name} WHERE {where_clause}", values)
        else:
            cur.execute(f"DELETE FROM {object_name} WHERE id = %s", (document.get("id"),))
        conn.commit()
        audit_log(phase="cdc_delete", status="deleted", details={"table": object_name})


def export_target_data(
    conn: Any,
    object_name: str,
    schema_name: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream all rows from the target table.

    Used by the validator to re-read migrated data. Unlike the source export
    this variant applies no timeouts and no value coercion — it reads values
    exactly as the target stored them.
    """
    validate_identifier(object_name, "table")
    table_name = f"{schema_name}.{object_name}" if schema_name else object_name
    with conn.cursor(name=f"export_target_{object_name}") as cur:
        cur.execute(f"SELECT * FROM {table_name}")
        columns = [desc.name for desc in cur.description]
        for row in cur:
            yield dict(zip(columns, row))
