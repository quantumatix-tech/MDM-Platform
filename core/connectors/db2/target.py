from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import Schema, TargetConnector, UpsertResult, validate_identifier
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff

from core.connectors.db2._models import _build_db2_conn_str
from core.connectors.db2.objects import schema as db2_schema
from core.connectors.db2.objects import ddl as db2_ddl
from core.connectors.db2.objects import loader as db2_loader

def _quote_ident(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


class DB2TargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config) if config else {}
        self._conn: Any = None
        self._managed_schemas: dict[str, Schema] = {}

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pyodbc")
        import pyodbc

        conn_str = _build_db2_conn_str(self._config)
        try:
            self._conn = pyodbc.connect(conn_str)
        except Exception as exc:
            audit_log(
                phase="connect",
                status="error",
                details={"engine": "db2", "role": "target", "error": str(exc)},
            )
            raise

        audit_log(phase="connect", status="success", details={"engine": "db2", "role": "target"})

    def get_server_version(self) -> str | None:
        if self._conn is None:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT SERVICE_LEVEL FROM SYSIBMADM.ENV_INST_INFO")
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
        except Exception:
            pass
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT VERSION_NUMBER, RELEASE_NUMBER, MODIFICATION_NUMBER, FIXPACK_NUMBER FROM SYSIBMADM.ENV_INST_INFO")
                row = cur.fetchone()
                if row:
                    return ".".join(str(x) for x in row if x is not None)
        except Exception:
            pass
        return None

    def health_check(self) -> bool:
        if self._conn is None:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1")
                row = cur.fetchone()
                return row is not None and row[0] == 1
        except Exception:
            return False

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._conn.close()
        finally:
            self._conn = None

    def preflight(self) -> dict[str, Any]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        cur = self._conn.cursor()
        cur.execute("SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1")
        row = cur.fetchone()
        db = row[0] if row else None
        cur.execute("SELECT SESSION_USER FROM SYSIBM.SYSDUMMY1")
        row = cur.fetchone()
        user = row[0] if row else None
        cur.close()
        return {"current_server": db, "session_user": user}

    def ensure_database_exists(self) -> None:
        return None

    def create_schema_if_missing(self, schema_name: str) -> None:
        if self._conn is None:
            raise RuntimeError("Not connected")
        db2_schema.create_schema_if_missing(self._conn, schema_name)

    def create_object_if_missing(self, schema: Schema) -> str | None:
        if self._conn is None:
            raise RuntimeError("Not connected")
        schema_name = schema.schema_name
        if not schema_name:
            raise ValueError(f"schema_name required for object {schema.name!r}; no default schema assumed")
        try:
            self.create_schema_if_missing(schema_name)
        except Exception:
            raise
        try:
            db2_ddl.create_table_if_missing(self._conn, schema)
            self._managed_schemas[schema.name] = schema
            return None
        except Exception:
            raise

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None, commit: bool = True) -> UpsertResult:
        if self._conn is None:
            raise RuntimeError("Not connected")
        return db2_loader.upsert_batch(self._conn, object_name, rows, schema, commit=commit)

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        raise NotImplementedError("Db2 target delete() not implemented in DB2-05B")

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        from core.connectors.db2.objects import table as db2_table
        cleared: list[str] = []
        for obj in objects or []:
            if isinstance(obj, str) and "." in obj:
                sch, tbl = obj.split(".", 1)
                sch = sch.strip().strip('"')
                tbl = tbl.strip().strip('"')
            else:
                sch = None
                tbl = obj
            validate_identifier(tbl, "table")
            if sch:
                validate_identifier(sch, "schema")
            qual = f'{_quote_ident(sch)}.{_quote_ident(tbl)}' if sch else _quote_ident(tbl)
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {qual}")
            try:
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise
            cleared.append(obj if isinstance(obj, str) else str(obj))
        return cleared

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        if self._conn is None:
            raise RuntimeError("Not connected")
        from core.connectors.db2.objects import table as db2_table
        if isinstance(object_name, str) and "." in object_name and schema_name is None:
            parts = object_name.split(".", 1)
            if len(parts) == 2:
                schema_name = parts[0].strip().strip('"')
                object_name = parts[1].strip().strip('"')
        return db2_table.get_row_count(self._conn, object_name, schema_name)

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        from core.connectors.db2.objects import table as db2_table
        return db2_table.export_table_data(self._conn, object_name, schema_name)
