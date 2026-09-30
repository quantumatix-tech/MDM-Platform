"""MSSQL CDC (Change Data Capture) Engine.

Extracted from ``core/connectors/mssql.py`` — captures change events from a
SQL Server source database via CDC and applies them to a target connector.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    CDCEngine,
    ChangeEvent,
    ApplyResult,
    TargetConnector,
    validate_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


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
        schema_name = "dbo"

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT t.name FROM sys.tables t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN cdc.change_tables ct ON ct.object_id = t.object_id "
                "WHERE s.name = ?",
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
