from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import GrantDef, Schema, SourceConnector, validate_identifier
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff

from core.connectors.db2._models import _build_db2_conn_str
from core.connectors.db2.objects import table as db2_table
from core.connectors.db2.preflight import validate_source_schemas


class DB2SourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config) if config else {}
        self._conn: Any = None
        self._selected_schemas: list[str] = []

    def _get_selected_schemas(self) -> list[str]:
        schemas = self._config.get("include_schemas") or []
        if not schemas:
            migration_cfg = self._config.get("migration", {}) if isinstance(self._config.get("migration"), dict) else {}
            schemas = migration_cfg.get("include_schemas") or migration_cfg.get("schemas") or []
        if not schemas:
            src = self._config.get("source", {})
            if isinstance(src, dict):
                schemas = src.get("schemas") or src.get("include_schemas") or []
        if isinstance(schemas, list):
            self._selected_schemas = schemas
            return schemas
        self._selected_schemas = []
        return []

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
                details={"engine": "db2", "role": "source", "error": str(exc)},
            )
            raise

        audit_log(phase="connect", status="success", details={"engine": "db2", "role": "source"})

    def preflight_validate_schemas(self) -> dict[str, Any]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        schemas = self._get_selected_schemas()
        return validate_source_schemas(self._conn, schemas)

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

    def list_schemas(self) -> list[str]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        return db2_table.discover_schemas(self._conn)

    def list_objects(self) -> list[str]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        pairs = db2_table.discover_tables(self._conn)
        schemas = self._get_selected_schemas()
        if schemas:
            schema_set = set(str(s).strip().upper() for s in schemas)
            pairs = [(p[0], p[1]) for p in pairs if p[0].upper() in schema_set]
        tables = [p[1] for p in pairs]
        seen = set()
        result = []
        for t in tables:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    def list_objects_with_schema(self) -> list[tuple[str, str]]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        pairs = db2_table.discover_tables(self._conn)
        schemas = self._get_selected_schemas()
        if schemas:
            schema_set = set(str(s).strip().upper() for s in schemas)
            pairs = [(p[0], p[1]) for p in pairs if p[0].upper() in schema_set]
        return pairs

    def list_grants(self) -> list[GrantDef]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        from core.connectors.db2.objects import grant as db2_grant

        object_type_map = {
            "SCHEMA": "SCHEMA",
            "TABLE": "TABLE",
            "COLUMN": "COLUMN",
            "SEQUENCE": "SEQUENCE",
            "ROUTINE": "FUNCTION",
        }
        grants: list[GrantDef] = []
        for schema in self._get_selected_schemas():
            for item in db2_grant.discover_grants(self._conn, schema):
                ot = str(item.get("object_type", "")).upper()
                if ot not in object_type_map:
                    continue
                grants.append(
                    GrantDef(
                        privileges=str(item["privilege"]),
                        object_type=object_type_map[ot],
                        object_name=str(item["object_name"]),
                        grantee=str(item["grantee"]),
                        schema_name=schema,
                        grant_option=bool(item.get("grantable", False)),
                        grantee_host=None,
                    )
                )
        return grants

    def list_db2_grants(self) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        from core.connectors.db2.objects import grant as db2_grant

        grants: list[dict[str, Any]] = []
        for schema in self._get_selected_schemas():
            for item in db2_grant.discover_grants(self._conn, schema):
                grants.append(dict(item, schema_name=schema))
        return grants

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        if self._conn is None:
            raise RuntimeError("Not connected")
        if schema_name is None:
            schema_name = self._config.get("schema") or self._config.get("schema_name")
        schemas = self._get_selected_schemas()
        if schema_name and schemas:
            schema_set = set(str(s).strip().upper() for s in schemas)
            if str(schema_name).strip().upper() not in schema_set:
                raise ValueError(f"Schema {schema_name!r} not in configured source schemas")
        if isinstance(object_name, str) and "." in object_name and schema_name is None:
            parts = object_name.split(".", 1)
            if len(parts) == 2:
                schema_name = parts[0].strip().strip('"')
                object_name = parts[1].strip().strip('"')
        return db2_table.get_row_count(self._conn, object_name, schema_name)

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Not connected")
        if schema_name is None:
            schema_name = self._config.get("schema") or self._config.get("schema_name")
        schemas = self._get_selected_schemas()
        if schema_name and schemas:
            schema_set = set(str(s).strip().upper() for s in schemas)
            if str(schema_name).strip().upper() not in schema_set:
                raise ValueError(f"Schema {schema_name!r} not in configured source schemas")
        return db2_table.export_table_data(self._conn, object_name, schema_name)

    def get_schema(self, object_name: str, schema_name: str | None = None) -> Schema:
        if self._conn is None:
            raise RuntimeError("Not connected")
        if schema_name is None:
            schema_name = self._config.get("schema") or self._config.get("schema_name")
        schemas = self._get_selected_schemas()
        if schema_name and schemas:
            schema_set = set(str(s).strip().upper() for s in schemas)
            if str(schema_name).strip().upper() not in schema_set:
                raise ValueError(f"Schema {schema_name!r} not in configured source schemas")
        return db2_table.get_schema_for_table(self._conn, object_name, schema_name)
