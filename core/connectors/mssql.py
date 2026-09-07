from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    UnmappedTypeError,
    validate_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


class MSSQLSourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pyodbc")
        import pyodbc

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={self._config['host']},{self._config.get('port', 1433)};"
            f"DATABASE={self._config['database']};"
            f"UID={self._config['username']};"
            f"PWD={self._config.get('password', '')};"
            f"Encrypt={'yes' if self._config.get('ssl', True) else 'no'};"
            f"TrustServerCertificate={'no' if self._config.get('ssl', True) else 'yes'};"
        )

        self._conn = pyodbc.connect(conn_str)
        audit_log(phase="connect", status="success", details={"engine": "mssql", "role": "source"})

    def list_objects(self) -> list[str]:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE'"
            )
            tables = [row[0] for row in cur.fetchall()]
        for t in tables:
            validate_identifier(t, "table")
        return tables

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {object_name}")
            return cur.fetchone()[0]

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {object_name}")
            columns = [desc[0] for desc in cur.description]
            for row in cur:
                yield dict(zip(columns, row))

    def get_schema(self, object_name: str) -> Schema:
        validate_identifier(object_name, "table")
        columns: list[Column] = []
        primary_key: list[str] = []

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (object_name,),
            )
            for row in cur.fetchall():
                col_name, data_type, nullable, max_len = row
                columns.append(
                    Column(
                        name=col_name,
                        source_type=data_type,
                        target_type=None,
                        nullable=(nullable == "YES"),
                        size=max_len,
                    )
                )

            cur.execute(
                "SELECT kcu.COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
                "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                "WHERE tc.TABLE_NAME = %s AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'",
                (object_name,),
            )
            primary_key = [row[0] for row in cur.fetchall()]

        return Schema(name=object_name, columns=columns, primary_key=primary_key)


class MSSQLTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pyodbc")
        import pyodbc

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={self._config['host']},{self._config.get('port', 1433)};"
            f"DATABASE={self._config.get('database', 'master')};"
            f"UID={self._config['username']};"
            f"PWD={self._config.get('password', '')};"
            f"Encrypt={'yes' if self._config.get('ssl', True) else 'no'};"
            f"TrustServerCertificate={'no' if self._config.get('ssl', True) else 'yes'};"
        )

        self._conn = pyodbc.connect(conn_str)
        audit_log(phase="connect", status="success", details={"engine": "mssql", "role": "target"})

    def ensure_database_exists(self) -> None:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM sys.databases WHERE name = ?", (db_name,)
            )
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {db_name}")
                audit_log(phase="ensure_database", status="created", details={"database": db_name})

    def create_object_if_missing(self, schema: Schema) -> None:
        validate_identifier(schema.name, "table")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME = ?",
                (schema.name,),
            )
            if cur.fetchone() is not None:
                return

            col_defs = []
            for col in schema.columns:
                if col.target_type is None:
                    if self._config.get("source_engine") == "mssql":
                        col_type = col.source_type
                    else:
                        raise UnmappedTypeError(
                            table=schema.name,
                            column=col.name,
                            source_type=col.source_type,
                            source_engine=self._config.get("source_engine"),
                            target_engine="mssql",
                        )
                else:
                    col_type = col.target_type
                null_str = "NULL" if col.nullable else "NOT NULL"
                col_defs.append(f"{col.name} {col_type} {null_str}")

            if schema.primary_key:
                pk_cols = ", ".join(schema.primary_key)
                col_defs.append(f"PRIMARY KEY ({pk_cols})")

            ddl = f"CREATE TABLE {schema.name} ({', '.join(col_defs)})"
            cur.execute(ddl)
            self._conn.commit()
            audit_log(phase="create_table", status="created", details={"table": schema.name})

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        validate_identifier(object_name, "table")
        result = UpsertResult()
        batch = list(rows)

        if not batch:
            return result

        with self._conn.cursor() as cur:
            columns = list(batch[0].keys())
            col_names = ", ".join(columns)
            placeholders = ", ".join(["?"] * len(columns))
            update_set = ", ".join(
                f"target.{col} = source.{col}" for col in columns
            )

            pk_cols = schema.primary_key if schema else []
            if pk_cols:
                pk_clause = " AND ".join(f"target.{col} = source.{col}" for col in pk_cols)
                on_clause = pk_clause
            else:
                on_clause = "1=0"

            sql = (
                f"MERGE INTO {object_name} AS target "
                f"USING (SELECT {placeholders}) AS source ({col_names}) "
                f"ON {on_clause} "
                f"WHEN MATCHED THEN UPDATE SET {update_set} "
                f"WHEN NOT MATCHED THEN INSERT ({col_names}) VALUES (source.{col_names});"
            )

            try:
                for row in batch:
                    values = [row.get(col) for col in columns]
                    cur.execute(sql, values)
                self._conn.commit()
                result.success_count = len(batch)
                audit_log(phase="upsert_batch", status="success", details={"table": object_name, "count": len(batch)})
            except Exception as exc:
                self._conn.rollback()
                result.failure_count = len(batch)
                result.errors.append(str(exc))
                result.failed_items.extend(batch)
                audit_log(phase="upsert_batch", status="failure", details={"table": object_name, "error": str(exc)})

        return result

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {object_name}")
            return cur.fetchone()[0]

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            if schema and schema.primary_key:
                conditions = []
                values = []
                for pk_col in schema.primary_key:
                    conditions.append(f"{pk_col} = ?")
                    values.append(document.get(pk_col))
                where_clause = " AND ".join(conditions)
                cur.execute(f"DELETE FROM {object_name} WHERE {where_clause}", values)
            else:
                cur.execute(f"DELETE FROM {object_name} WHERE id = ?", (document.get("id"),))
            self._conn.commit()
            audit_log(phase="cdc_delete", status="deleted", details={"table": object_name})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {object_name}")
            columns = [desc[0] for desc in cur.description]
            for row in cur:
                yield dict(zip(columns, row))


class MSSQLCDCEngine(CDCEngine):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        self._last_lsn: str | None = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pyodbc")
        import pyodbc

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={self._config['host']},{self._config.get('port', 1433)};"
            f"DATABASE={self._config['database']};"
            f"UID={self._config['username']};"
            f"PWD={self._config.get('password', '')};"
            f"Encrypt=yes;TrustServerCertificate=no;"
        )

        self._conn = pyodbc.connect(conn_str)

    def start(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM sys.databases WHERE is_cdc_enabled = 1")
            cdc_dbs = [row[0] for row in cur.fetchall()]
            if self._config["database"] not in cdc_dbs:
                cur.execute(f"EXEC sys.sp_cdc_enable_db")
        audit_log(phase="cdc_start", status="success", details={"engine": "mssql"})

    def poll_changes(self) -> list[ChangeEvent]:
        self._config["database"]
        schema_name = "dbo"

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT t.name FROM sys.tables t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN cdc.change_tables ct ON ct.object_id = t.object_id "
                "WHERE s.name = %s",
                (schema_name,),
            )
            tables = [row[0] for row in cur.fetchall()]

        start_lsn = self._last_lsn if self._last_lsn is not None else f"sys.fn_cdc_get_min_lsn('{schema_name}')"
        events = []
        for table_name in tables:
            func_name = f"cdc.fn_cdc_get_all_changes_{schema_name}_{table_name}"
            validate_identifier(table_name, "table")

            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {func_name}("
                    f"{start_lsn}, "
                    f"sys.fn_cdc_get_max_lsn(), 'all')"
                )
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

            for row in rows:
                row_dict = dict(zip(columns, row))
                operation_code = row_dict.get("__$operation", 2)
                operation_map = {1: "delete", 2: "insert", 4: "update"}
                operation = operation_map.get(operation_code)
                if operation is None:
                    continue

                document = {
                    k: v
                    for k, v in row_dict.items()
                    if not k.startswith("__$")
                }

                object_name = table_name
                events.append(
                    ChangeEvent(
                        operation=operation,
                        document=document,
                        object_name=object_name,
                        watermark=row_dict.get("__$start_lsn"),
                    )
                )

        return events

    def apply(self, events: list[ChangeEvent], target: TargetConnector) -> ApplyResult:
        result = ApplyResult()
        if not events:
            return result

        for event in events:
            try:
                if event.operation == "insert":
                    target.upsert_batch(event.object_name, iter([event.document]), event.schema)
                elif event.operation == "update":
                    target.upsert_batch(event.object_name, iter([event.document]), event.schema)
                elif event.operation == "delete":
                    target.delete(event.object_name, event.document, event.schema)
                result.success_count += 1
            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0:
            result.last_checkpoint = events[-1].watermark
            audit_log(phase="cdc_apply", status="success", details={"applied": result.success_count})
        else:
            audit_log(phase="cdc_apply", status="partial_failure", details={"success": result.success_count, "failure": result.failure_count})

        return result

    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return

        lsn = result.last_checkpoint
        self._last_lsn = (
            f"0x{lsn.hex()}" if isinstance(lsn, (bytes, bytearray)) else str(lsn)
        )
        audit_log(phase="cdc_checkpoint", status="advanced", details={"lsn": self._last_lsn})
