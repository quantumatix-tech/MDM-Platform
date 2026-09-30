"""MySQL target connector — connector-facing target API.

Owns the target connection lifecycle and the connector surface.
``ensure_database_exists`` (database bootstrap) and
``apply_constraints`` (the ordered index -> foreign key -> CHECK
constraint layer) intentionally remain here rather than in an
object module. Object-specific creation is delegated to
``core.connectors.mysql.objects`` (Batch 1+).
"""

from __future__ import annotations

from typing import Any
from collections.abc import Iterator
import re

from core.connectors.base import (
    CommentDef,
    EventDef,
    FunctionDef,
    GrantDef,
    Schema,
    TargetConnector,
    TriggerDef,
    UpsertResult,
    ViewDefinition,
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
from core.connectors.mysql.objects import table as table_ops
from core.connectors.mysql._models import (
    _MYSQL_ACCOUNT_PART,
    _connection_options,
    _q,
    _qaccount,
    _qname,
    _rewrite_mysql_definer,
)


class MySQLTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        self._reconciliation_backups: list[str] = []
        self._routine_definer: str | None = config.get("routine_definer")
        self._preserve_source_definer: bool = bool(config.get("preserve_source_definer", False))
        self._available_definer_accounts: set[tuple[str, str]] = set()
        self._current_account: tuple[str, str] | None = None
        self._can_set_any_definer = False

    def _rewrite_definer(self, ddl: str) -> str:
        return _rewrite_mysql_definer(
            ddl,
            self._routine_definer,
            preserve_source_definer=self._preserve_source_definer,
            available_accounts=self._available_definer_accounts,
            can_set_any_definer=self._can_set_any_definer,
        )

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("mysql-connector-python", "mysql.connector")
        import mysql.connector

        conn_kwargs: dict[str, Any] = {
            "host": self._config["host"],
            "port": self._config.get("port", 3306),
            "database": self._config.get("database", "mysql"),
            "user": self._config["username"],
            "password": self._config.get("password", ""),
            "connection_timeout": self._config.get("connection_timeout", 10),
        }
        conn_kwargs.update(_connection_options(self._config))

        self._conn = mysql.connector.connect(**conn_kwargs)
        with self._conn.cursor() as cur:
            cur.execute("SELECT CURRENT_USER()")
            current_user = cur.fetchone()[0]
            current_match = re.fullmatch(
                rf"\s*(?P<user>{_MYSQL_ACCOUNT_PART})\s*@\s*(?P<host>{_MYSQL_ACCOUNT_PART})\s*",
                current_user,
            )
            if current_match is None:
                raise RuntimeError("MySQL CURRENT_USER() did not return a user@host identity")

            def unquote(part: str) -> str:
                return part[1:-1].replace("``", "`") if part.startswith("`") else part

            self._current_account = (
                unquote(current_match["user"]),
                unquote(current_match["host"]),
            )
            self._available_definer_accounts.add(self._current_account)
            try:
                cur.execute("SHOW GRANTS FOR CURRENT_USER()")
                grant_text = "\n".join(str(row[0]).upper() for row in cur.fetchall())
                self._can_set_any_definer = bool(
                    re.search(r"\b(?:SET_ANY_DEFINER|SET_USER_ID|SUPER)\b", grant_text)
                )
            except Exception:
                # Unknown authority must not be treated as permission to retain
                # a different source DEFINER. The current account remains the
                # safe target-side identity fallback.
                self._can_set_any_definer = False
                self._conn.rollback()

            if self._routine_definer is None or (
                not self._can_set_any_definer
                and self._routine_definer != current_user
            ):
                self._routine_definer = current_user
        audit_log(phase="connect", status="success", details={"engine": "mysql", "role": "target"})

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

    def ensure_database_exists(self) -> None:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {db_name}")
                audit_log(phase="ensure_database", status="created", details={"database": db_name})

    def _show_create(self, kind: str, name: str) -> str:
        """Return the authoritative MySQL DDL for an existing target object."""
        with self._conn.cursor() as cur:
            cur.execute(f"SHOW CREATE {kind} {_q(name)}")
            row = cur.fetchone()
            for index, column_name in enumerate(cur.column_names):
                if "create" in column_name.lower() or "original statement" in column_name.lower():
                    return row[index]
            raise RuntimeError(f"SHOW CREATE {kind} did not return a DDL column")

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        # Target-owned because permissions/version checks can refine this later.
        direct = ("tables", "columns", "defaults", "primary_keys", "auto_increment", "indexes", "unique_constraints", "check_constraints", "foreign_keys", "generated_columns", "partitions", "views", "functions", "procedures", "triggers", "events", "comments", "grants", "security_principals")
        unsupported = {"materialized_views": "MySQL has no native materialized views", "rls_policies": "MySQL has no row-level security policies", "extensions": "MySQL has no PostgreSQL extension model", "custom_types": "MySQL has no PostgreSQL domain/type model", "sequences": "AUTO_INCREMENT is table-bound", "schemas": "MySQL databases are namespaces, not PostgreSQL schemas"}
        return {x: {"supported": True, "mode": "direct"} for x in direct} | {x: {"supported": False, "mode": "unsupported", "reason": r} for x, r in unsupported.items()}

    def inspect_schema(self, object_name: str, schema_name: str | None = None) -> Schema | None:
        """Reuse MySQL catalog extraction for target partition verification."""
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
                (self._config.get("database", "mysql"), object_name),
            )
            if cur.fetchone() is None:
                return None
        return _schema.inspect_schema(self._conn, self._config["database"], object_name)

    def create_object_if_missing(self, schema: Schema) -> str:
        """Create ``schema`` as a table unless it already exists."""
        return table_ops.create_object_if_missing(self._conn, self._config, schema)


    def reconcile_mysql_table(self, schema: Schema, managed_tables: set[str]) -> str:
        """Stage and atomically swap a source-equivalent table, retaining backup until success."""
        return table_ops.reconcile_mysql_table(
            self._conn, self._config, schema, managed_tables,
            self._reconciliation_backups,
        )

    def finalize_schema_reconciliations(self) -> list[str]:
        """Drop retained backup tables once the run has succeeded."""
        return table_ops.finalize_schema_reconciliations(self._conn, self._reconciliation_backups)

    @staticmethod
    @staticmethod
    def _literal(value: str) -> str:
        return table_ops.literal(value)

    def apply_constraints(self, schema: Schema) -> None:
        """Apply post-load objects; FKs last so referenced tables always exist."""
        table = _q(schema.name)
        with self._conn.cursor() as cur:
            for index in schema.indexes:
                try:
                    cur.execute("SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), NON_UNIQUE, INDEX_TYPE FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s GROUP BY NON_UNIQUE, INDEX_TYPE", (self._config["database"], schema.name, index.name))
                    existing = cur.fetchone()
                    if existing is not None:
                        actual_columns, non_unique, actual_type = existing
                        if (
                            actual_columns == ",".join(index.columns)
                            and bool(non_unique) == (not index.unique)
                            and actual_type.upper() == (index.index_type or "BTREE").upper()
                        ):
                            audit_log(phase="create_index", status="verified_existing", details={"table": schema.name, "index": index.name})
                            continue
                        raise RuntimeError(f"existing index definition differs: {actual_columns}")
                    index_type = (index.index_type or "").upper()
                    if index_type in {"FULLTEXT", "SPATIAL"}:
                        prefix = f"{index_type} INDEX"
                    else:
                        prefix = "UNIQUE INDEX" if index.unique else "INDEX"
                    cur.execute(f"CREATE {prefix} {_q(index.name)} ON {table} ({', '.join(_q(c) for c in index.columns)})")
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(f"index {index.name}: {exc}") from exc
            for check in schema.check_constraints:
                try:
                    cur.execute("SELECT 1 FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s AND CONSTRAINT_TYPE='CHECK'", (self._config["database"], schema.name, check.name))
                    if cur.fetchone() is not None:
                        audit_log(phase="create_check", status="verified_existing", details={"table": schema.name, "constraint": check.name})
                        continue
                    cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {_q(check.name)} CHECK ({check.expression})")
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(f"check {check.name}: {exc}") from exc
            for fk in schema.foreign_keys:
                try:
                    cur.execute("SELECT 1 FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s AND CONSTRAINT_TYPE='FOREIGN KEY'", (self._config["database"], schema.name, fk.name))
                    if cur.fetchone() is not None:
                        audit_log(phase="create_fk", status="verified_existing", details={"table": schema.name, "constraint": fk.name})
                        continue
                    # MySQL's schema value is the source database namespace.
                    # A database migration maps it to the configured target
                    # database; retaining it would create cross-database FKs
                    # back to the source server/database.
                    ref = _qname(self._config["database"], fk.ref_table)
                    cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {_q(fk.name)} FOREIGN KEY ({', '.join(_q(c) for c in fk.columns)}) REFERENCES {ref} ({', '.join(_q(c) for c in fk.ref_columns)}) ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}")
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(f"foreign key {fk.name}: {exc}") from exc

    def create_view(self, view: ViewDefinition) -> None:
        """Create or replace a view on the target, rewriting database references."""
        view_ops.create_view(self._conn, self._config["database"], view)

    def create_function(self, func: FunctionDef) -> None:
        function_ops.create_function(self._conn, func, self._rewrite_definer)

    def create_trigger(self, trigger: TriggerDef) -> None:
        trigger_ops.create_trigger(self._conn, trigger, self._rewrite_definer)

    def suspend_triggers_for_data_load(self, triggers: list[TriggerDef]) -> list[TriggerDef]:
        """MySQL lacks DISABLE TRIGGER; snapshot/drop only matching triggers.

        The orchestration recreates source definitions after the load. Returning
        the target definitions provides an audit trail and permits callers to
        restore them if later trigger application is unavailable.
        """
        return trigger_ops.suspend_triggers_for_data_load(
            self._conn, self._config["database"], triggers, self._show_create
        )

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        """Delete all migrated-table rows for a deterministic MySQL full sync.

        ``TRUNCATE`` is deliberately not used: it is incompatible with tables
        referenced by foreign keys.  FK checks are disabled only in this target
        connection while deleting the known migration tables, then restored in
        a ``finally`` block.  The orchestrator suspends matching triggers first.
        """
        tables = [validate_identifier(name, "table") for name in objects]
        if not tables:
            return []
        with self._conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                for table in tables:
                    cur.execute(f"DELETE FROM {_q(table)}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
                self._conn.commit()
        audit_log(phase="clear_full_sync", status="success", details={"tables": tables})
        return tables

    def create_event(self, event: EventDef) -> None:
        event_ops.create_event(self._conn, event, self._rewrite_definer)

    def sync_auto_increment(self, table: str, column: str) -> None:
        """Advance the table AUTO_INCREMENT counter to max(column) + 1."""
        table_ops.sync_auto_increment(self._conn, table, column)

    def apply_comment(self, comment: CommentDef) -> None:
        # MySQL stores comments in table/column DDL; retain source comments for
        # existing target objects by applying native ALTER statements.
        comment_ops.apply_comment(self._conn, self._config["database"], comment)

    def apply_grant(self, grant: GrantDef) -> None:
        # Grantees are not created by the platform; target permissions decide
        # whether this direct MySQL statement can be applied.
        security_ops.apply_grant(self._conn, self._config["database"], grant)

    def create_user_if_not_exists(self, user_name: str, host: str | None = None) -> None:
        """Create a host-scoped account without copying source credentials."""
        security_ops.create_user_if_not_exists(
            self._conn,
            user_name,
            host,
            self._available_definer_accounts,
            self._current_account,
        )

    @staticmethod
    def is_authorization_error(error: Exception) -> bool:
        """Classify common MySQL privilege-denial errors for per-grant reporting."""
        return security_ops.is_authorization_error(error)

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        """Insert a batch of rows, updating duplicates by primary/unique key."""
        return table_ops.upsert_batch(self._conn, self._config, object_name, rows, schema)

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        """Count rows currently present in ``object_name`` on the target."""
        return table_ops.get_object_count(self._conn, object_name)

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        """Delete a single row addressed by primary key, or by ``id``."""
        table_ops.delete(self._conn, object_name, document, schema)

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream every row of ``object_name`` from the target."""
        return table_ops.export_full(self._conn, object_name)
