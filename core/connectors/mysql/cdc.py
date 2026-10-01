"""MySQL CDC engine — binary-log change capture.

Verbatim move of ``MySQLCDCEngine``. Behaviour is intentionally
unchanged in this structural batch.

Note: ``apply`` takes ``(events, target)`` rather than the ABC's
``apply(events)``. The extra target argument is required to dispatch
changes and is passed explicitly by the orchestrator.
"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import json

from core.connectors.base import (
    ApplyResult,
    CDCEngine,
    ChangeEvent,
    TargetConnector,
)
from core.audit_logger import audit_log
from core.retry import retry_with_backoff


class MySQLCDCEngine(CDCEngine):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

        self._last_binlog_file: str | None = None
        self._last_binlog_pos: int | None = None

        # Persistent CDC checkpoint
        self._checkpoint_dir = Path("state") / "cdc"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

        safe_name = (
            f"{self._config['host']}_"
            f"{self._config.get('port', 3306)}_"
            f"{self._config['database']}"
        ).replace(":", "_").replace("/", "_").replace("\\", "_")

        self._checkpoint_path = (
            self._checkpoint_dir / f"mysql_{safe_name}.json"
        )

        self._load_checkpoint()

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        import mysql.connector

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "database": self._config["database"],
            "user": self._config["username"],
            "password": self._config.get("password", ""),
            "ssl_disabled": not self._config.get("ssl", True),
        }

        self._conn = mysql.connector.connect(**conn_kwargs)
    def _load_checkpoint(self) -> None:
        if not self._checkpoint_path.exists():
            return

        try:
            with self._checkpoint_path.open("r", encoding="utf-8") as f:
                state = json.load(f)

            self._last_binlog_file = state.get("binlog_file")
            self._last_binlog_pos = state.get("binlog_pos")

            audit_log(
                phase="cdc_checkpoint",
                status="loaded",
                details={
                    "binlog_file": self._last_binlog_file,
                    "binlog_pos": self._last_binlog_pos,
                },
            )

        except Exception as exc:
            audit_log(
                phase="cdc_checkpoint",
                status="load_failed",
                details={"error": str(exc)},
            )

    def start(self) -> None:
    # Only initialize a new CDC position if no checkpoint exists.
    # Never overwrite a persisted checkpoint.
        if self._last_binlog_file is None:
            with self._conn.cursor() as cur:
                cur.execute("SHOW BINARY LOG STATUS")
                row = cur.fetchone()
                if row:
                    self._last_binlog_file = row[0]
                    self._last_binlog_pos = row[1]

        audit_log(
            phase="cdc_start",
            status="success",
            details={
                "engine": "mysql",
                "binlog_file": self._last_binlog_file,
                "binlog_pos": self._last_binlog_pos,
            },
        )

    def poll_changes(self) -> list[ChangeEvent]:
        from pymysqlreplication import BinLogStreamReader
        from pymysqlreplication.row_event import (
            WriteRowsEvent,
            UpdateRowsEvent,
            DeleteRowsEvent,
        )

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "user": self._config["username"],
            "passwd": self._config.get("password", ""),
            "connect_timeout": 5,
            "read_timeout": 5,
        }

        if self._config.get("ssl", False):
            conn_kwargs["ssl"] = {}

        stream = BinLogStreamReader(
            connection_settings=conn_kwargs,
            server_id=self._config.get("server_id", 100),
            blocking=False,
            resume_stream=self._last_binlog_file is not None,
            log_file=self._last_binlog_file,
            log_pos=self._last_binlog_pos or 4,
            only_events=[
                WriteRowsEvent,
                UpdateRowsEvent,
                DeleteRowsEvent,
            ],
            only_schemas=[self._config["database"]],
        )

        events: list[ChangeEvent] = []
        try:
            for binlogevent in stream:
                if isinstance(binlogevent, WriteRowsEvent):
                    operation = "insert"
                    for row in binlogevent.rows:
                        document = row["values"]
                        events.append(
                            ChangeEvent(
                                operation=operation,
                                document=document,
                                object_name=binlogevent.table,
                                watermark={
                                    "file": binlogevent.log_file,
                                    "pos": binlogevent.log_pos,
                                },
                            )
                        )
                elif isinstance(binlogevent, UpdateRowsEvent):
                    operation = "update"
                    for row in binlogevent.rows:
                        document = row["after_values"]
                        events.append(
                            ChangeEvent(
                                operation=operation,
                                document=document,
                                object_name=binlogevent.table,
                                watermark={
                                    "file": binlogevent.log_file,
                                    "pos": binlogevent.log_pos,
                                },
                            )
                        )
                elif isinstance(binlogevent, DeleteRowsEvent):
                    operation = "delete"
                    for row in binlogevent.rows:
                        document = row["values"]
                        events.append(
                            ChangeEvent(
                                operation=operation,
                                document=document,
                                object_name=binlogevent.table,
                                watermark={
                                    "file": binlogevent.log_file,
                                    "pos": binlogevent.log_pos,
                                },
                            )
                        )
        finally:
            stream.close()

        return events

    def apply(
        self,
        events: list[ChangeEvent],
        target: TargetConnector,
    ) -> ApplyResult:
        result = ApplyResult()

        if not events:
            return result

        for event in events:
            try:
                if event.operation in ("insert", "update"):
                    apply_result = target.upsert_batch(
                        event.object_name,
                        iter([event.document]),
                        event.schema,
                    )

                    if apply_result.failure_count > 0:
                        result.failure_count += apply_result.failure_count
                        result.errors.extend(apply_result.errors)
                        continue

                    result.success_count += apply_result.success_count

                elif event.operation == "delete":
                    target.delete(
                        event.object_name,
                        event.document,
                        event.schema,
                    )
                    result.success_count += 1

            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0 and result.success_count > 0:
            result.last_checkpoint = events[-1].watermark

            audit_log(
                phase="cdc_apply",
                status="success",
                details={
                    "applied": result.success_count,
                },
            )
        else:
            audit_log(
                phase="cdc_apply",
                status="partial_failure",
                details={
                    "success": result.success_count,
                    "failure": result.failure_count,
                },
            )

        return result

    
    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return

        watermark = result.last_checkpoint

        if isinstance(watermark, dict):
            self._last_binlog_file = watermark.get("file")
            self._last_binlog_pos = watermark.get("pos")
        else:
            self._last_binlog_file = str(watermark)
            self._last_binlog_pos = None

        with self._checkpoint_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "binlog_file": self._last_binlog_file,
                    "binlog_pos": self._last_binlog_pos,
                },
                f,
                indent=2,
            )

        audit_log(
            phase="cdc_checkpoint",
            status="advanced",
            details={
                "binlog_file": self._last_binlog_file,
                "binlog_pos": self._last_binlog_pos,
            },
        )
