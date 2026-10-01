"""MySQL table object operations.

Owns table creation, target reconciliation, row-level data operations and
``AUTO_INCREMENT`` synchronisation. This module must not import ``source``,
``target`` or ``cdc``: it depends only on ``_models``, the shared schema
inspection helper in ``_schema``, the cross-engine DTOs and the standard
library.

Every SQL statement, parameter order, commit/rollback boundary, audit call
and ordering constraint is carried over unchanged from the former
``MySQLTargetConnector`` methods.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import hashlib

from core.connectors.base import (
    Schema,
    UnmappedTypeError,
    UpsertResult,
    validate_identifier,
)
from core.audit_logger import audit_log

from core.connectors.mysql import _schema
from core.connectors.mysql.objects.partition import _mysql_partition_clause
from core.connectors.mysql._models import (
    _default_sql,
    _normalize_mysql_set_value,
    _q,
    literal,
)


def create_object_if_missing(conn: Any, config: dict[str, Any], schema: Schema) -> str:
    """Create ``schema`` as a table when it does not already exist."""
    validate_identifier(schema.name, "table")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (config.get("database", "mysql"), schema.name),
        )
        if cur.fetchone() is not None:
            return "already_exists"
        _create_table(cur, config, schema, schema.name)
        conn.commit()
        audit_log(phase="create_table", status="created", details={"table": schema.name})
        return "created"


def _create_table(cur: Any, config: dict[str, Any], schema: Schema, table_name: str) -> None:
        """Emit the same source-derived DDL for a final or staged table."""
        validate_identifier(table_name, "table")
        col_defs = []
        for col in schema.columns:
            if col.target_type is None:
                if config.get("source_engine") == "mysql":
                    col_type = col.source_type
                else:
                    raise UnmappedTypeError(
                        table=schema.name,
                        column=col.name,
                        source_type=col.source_type,
                        source_engine=config.get("source_engine"),
                        target_engine="mysql",
                    )
            else:
                col_type = col.target_type
            null_str = "NULL" if col.nullable else "NOT NULL"
            generated = f" GENERATED ALWAYS AS ({col.generated}) {col.generated_kind or 'VIRTUAL'}" if col.generated else ""
            default = f" DEFAULT {_default_sql(col.default, col_type)}" if col.default is not None and not col.generated else ""
            increment = " AUTO_INCREMENT" if col.auto_increment else ""
            comment = f" COMMENT {literal(col.comment)}" if col.comment else ""
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
            suffix += f" COMMENT={literal(schema.comment)}"
        partition_clause = _mysql_partition_clause(schema)
        if partition_clause:
            suffix += f" {partition_clause}"
        ddl = f"CREATE TABLE {_q(table_name)} ({', '.join(col_defs)}){suffix}"
        cur.execute(ddl)


def reconcile_mysql_table(conn: Any, config: dict[str, Any], schema: Schema,
                          managed_tables: set[str], backups: list[str]) -> str:
    """Stage and atomically swap a source-equivalent table, retaining backup until success."""
    token = hashlib.sha1(schema.name.encode()).hexdigest()[:10]
    staged = f"__dms_stage_{token}"
    backup = f"__dms_backup_{token}"
    validate_identifier(staged, "table"); validate_identifier(backup, "table")
    with conn.cursor() as cur:
        try:
            cur.execute(f"DROP TABLE IF EXISTS {_q(staged)}")
            _create_table(cur, config, schema, staged)
            conn.commit()
            staged_schema = _schema.inspect_schema(conn, config["database"], staged)
            if (
                staged_schema.partition_method != schema.partition_method
                or staged_schema.partition_expression != schema.partition_expression
                or [(p.name, p.description) for p in staged_schema.partitions] != [(p.name, p.description) for p in schema.partitions]
            ):
                raise RuntimeError("staged table partition metadata does not match source")
            cur.execute(
                "SELECT TABLE_NAME,CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s "
                "AND CONSTRAINT_NAME<>'PRIMARY'",
                (config["database"], config["database"], schema.name),
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
            conn.commit()
            backups.append(backup)
            audit_log(phase="reconcile_table", status="recreated", details={"table": schema.name, "backup": backup})
            return "reconciled"
        except Exception:
            conn.rollback()
            try:
                cur.execute(f"DROP TABLE IF EXISTS {_q(staged)}")
                conn.commit()
            except Exception:
                conn.rollback()
            raise


def finalize_schema_reconciliations(conn: Any, backups: list[str]) -> list[str]:
    """Drop the retained backup tables once the run has succeeded."""
    removed: list[str] = []
    with conn.cursor() as cur:
        for backup in backups:
            cur.execute(f"DROP TABLE {_q(backup)}")
            removed.append(backup)
        conn.commit()
    backups.clear()
    return removed


def sync_auto_increment(conn: Any, table: str, column: str) -> None:
    """Advance a table's AUTO_INCREMENT counter to max(column) + 1."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX({_q(column)}), 0) + 1 FROM {_q(table)}")
        next_value = int(cur.fetchone()[0])
        cur.execute(f"ALTER TABLE {_q(table)} AUTO_INCREMENT = {next_value}")
        conn.commit()


def upsert_batch(conn: Any, config: dict[str, Any], object_name: str,
                 rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
    """Insert a batch of rows, updating duplicates by primary/unique key."""
    validate_identifier(object_name, "table")
    result = UpsertResult()
    batch = list(rows)

    if not batch:
        return result

    with conn.cursor() as cur:
        generated_columns = {col.name for col in (schema.columns if schema else []) if col.generated}
        set_columns = {}
        if config.get("source_engine") == "mysql":
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
            conn.commit()
            result.success_count = len(batch)
            audit_log(phase="upsert_batch", status="success", details={"table": object_name, "count": len(batch)})
        except Exception as exc:
            conn.rollback()
            result.failure_count = len(batch)
            result.errors.append(str(exc))
            result.failed_items.extend(batch)
            audit_log(phase="upsert_batch", status="failure", details={"table": object_name, "error": str(exc)})

    return result


def get_object_count(conn: Any, object_name: str) -> int:
    """Count rows currently present in ``object_name``."""
    validate_identifier(object_name, "table")
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {object_name}")
        return cur.fetchone()[0]


def delete(conn: Any, object_name: str, document: dict[str, Any],
           schema: Schema | None = None) -> None:
    """Delete a single row addressed by primary key, or by ``id``."""
    validate_identifier(object_name, "table")
    with conn.cursor() as cur:
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
        conn.commit()
        audit_log(phase="cdc_delete", status="deleted", details={"table": object_name})


def export_full(conn: Any, object_name: str) -> Iterator[dict[str, Any]]:
    """Stream every row of ``object_name`` from the target."""
    validate_identifier(object_name, "table")
    with conn.cursor(dictionary=True) as cur:
        cur.execute(f"SELECT * FROM {object_name}")
        for row in cur:
            yield dict(row)
