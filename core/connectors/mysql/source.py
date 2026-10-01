"""MySQL source connector — connector-facing source API.

Owns the source connection lifecycle and the connector surface.
``get_schema`` is intentionally retained here: it is the schema
composition point that assembles the cross-engine ``Schema`` DTO
from several metadata families in a single pass, so it belongs to
no single object type. Object-specific discovery is delegated to
``core.connectors.mysql.objects`` (Batch 1+).
"""

from __future__ import annotations

from typing import Any
from collections.abc import Iterator

from core.connectors.base import (
    EventDef,
    FunctionDef,
    GrantDef,
    Schema,
    SourceConnector,
    TriggerDef,
    UserDef,
    validate_identifier,
)
from core.audit_logger import audit_log
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff

from core.connectors.mysql import _schema
from core.connectors.mysql.objects import comment as comment_ops
from core.connectors.mysql.objects import event as event_ops
from core.connectors.mysql.objects import function as function_ops
from core.connectors.mysql.objects import security as security_ops
from core.connectors.mysql.objects import trigger as trigger_ops
from core.connectors.mysql.objects import view as view_ops
from core.connectors.mysql._models import (
    _connection_options,
    _q,
)


class MySQLSourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("mysql-connector-python", "mysql.connector")
        import mysql.connector

        # ssl_disabled (plus CA/verification options) is supplied below by
        # _connection_options for legacy and provider-neutral TLS configs.

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "database": self._config["database"],
            "user": self._config["username"],
            "password": self._config.get("password", ""),
            "connection_timeout": self._config.get("connection_timeout", 10),
        }
        conn_kwargs.update(_connection_options(self._config))

        self._conn = mysql.connector.connect(**conn_kwargs)
        # Source access is read-only. Autocommit prevents metadata locks from
        # surviving catalog reads or streaming exports until run shutdown.
        self._conn.autocommit = True
        audit_log(phase="connect", status="success", details={"engine": "mysql", "role": "source"})

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._conn.close()
        finally:
                self._conn = None

    def list_objects(self) -> list[str]:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                (db_name,),
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
        with self._conn.cursor(dictionary=True) as cur:
            cur.execute(f"SELECT * FROM {object_name}")
            for row in cur:
                yield dict(row)

    def get_schema(self, object_name: str) -> Schema:
        """Compose a Schema for ``object_name`` from MySQL catalog metadata.

        The catalog extraction itself lives in ``_schema`` so the target
        connector can verify target-side metadata without importing this
        module. Identifier validation stays here, at the public API boundary.
        """
        validate_identifier(object_name, "table")
        return _schema.inspect_schema(self._conn, self._config["database"], object_name)

    def _show_create(self, kind: str, name: str) -> str:
        with self._conn.cursor() as cur:
            cur.execute(f"SHOW CREATE {kind} {_q(name)}")
            row = cur.fetchone()
            for index, column_name in enumerate(cur.column_names):
                if "create" in column_name.lower() or "original statement" in column_name.lower():
                    return row[index]
            raise RuntimeError(f"SHOW CREATE {kind} did not return a DDL column")

    def list_views(self) -> list[ViewDefinition]:
        """Discover every view defined in the source database."""
        return view_ops.list_views(self._conn, self._config["database"])

    def list_functions(self) -> list[FunctionDef]:
        """Discover every stored FUNCTION and PROCEDURE in the source database."""
        return function_ops.list_functions(
            self._conn, self._config["database"], self._show_create
        )

    def get_all_triggers(self) -> list[TriggerDef]:
        """Discover every trigger defined in the source database."""
        return trigger_ops.get_all_triggers(
            self._conn, self._config["database"], self._show_create
        )

    def list_events(self) -> list[EventDef]:
        """Discover every event defined in the source database."""
        return event_ops.list_events(
            self._conn, self._config["database"], self._show_create
        )

    def list_comments(self) -> list[CommentDef]:
        """Discover table and column comments in the source database."""
        return comment_ops.list_comments(self._conn, self._config["database"])

    def list_users(self) -> list[UserDef]:
        """Return unlocked MySQL accounts selected by the user allowlist policy.

        Locked accounts are excluded. Authentication plugins and password
        material are deliberately omitted.
        """
        return security_ops.list_users(self._conn, self._config)

    def list_grants(self) -> list[GrantDef]:
        """Return the directly granted MySQL privileges for selected accounts."""
        return security_ops.list_grants(self._conn, self._config)

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        direct = ("tables", "columns", "defaults", "primary_keys", "auto_increment", "indexes", "unique_constraints", "check_constraints", "foreign_keys", "generated_columns", "partitions", "views", "functions", "procedures", "triggers", "events", "comments", "grants")
        unsupported = {"materialized_views": "MySQL has no native materialized views", "rls_policies": "MySQL has no row-level security policies", "extensions": "MySQL has no PostgreSQL extension model", "custom_types": "MySQL has no PostgreSQL domain/type model", "sequences": "AUTO_INCREMENT is table-bound", "schemas": "MySQL databases are namespaces, not PostgreSQL schemas", "security_principals": "MySQL exposes accounts and grants, not a separate principal object model"}
        return {x: {"supported": True, "mode": "direct"} for x in direct} | {x: {"supported": False, "mode": "unsupported", "reason": r} for x, r in unsupported.items()}
