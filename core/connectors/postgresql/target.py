"""PostgreSQL target connector — the connector-facing target API.

Owns the connection and exposes the application API the orchestrator calls.
Object-specific application is delegated to
``core.connectors.postgresql.objects``; this module routes, it does not
reimplement. The nine object modules are ``table``, ``sequence``, ``view``,
``type``, ``function``, ``security``, ``comment``, ``trigger`` and
``partition``.

Retained here by design:

  * ``__init__`` / ``connect`` — connection lifecycle. The target connects to
    the maintenance database first, which is what lets ``ensure_database_exists``
    bootstrap the real one.
  * ``ensure_database_exists`` — database bootstrap, which must run before any
    object is applied.
  * ``create_extension`` / ``create_schema`` — small bootstrap object
    application steps that every other object depends on.
  * ``apply_constraints`` — the combined ordered constraint layer (indexes,
    then CHECK constraints, then foreign keys). Foreign keys must come last
    because every referenced table has to exist first, and all three families
    share the same probe/commit/audit/rollback scaffolding. Splitting them
    would duplicate that scaffolding and still require this method to sequence
    them. It is the target-side counterpart of
    ``PostgresSourceConnector.get_schema()``.

Object application methods keep their existing, deliberately distinct failure
policies — some raise, most are swallowed and audited. Those policies are part
of the contract and are preserved per method.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    CommentDef,
    ExtensionDef,
    FunctionDef,
    GrantDef,
    MaterializedViewDef,
    PartitionDef,
    RLSPolicy,
    Schema,
    SchemaDef,
    SequenceDef,
    TargetConnector,
    TriggerDef,
    TypeDef,
    UpsertResult,
    ViewDefinition,
    validate_identifier,
)
from core.connectors.postgresql._models import (
    _make_conn_kwargs,
    _qualify,
)
from core.connectors.postgresql.objects import table as _postgres_table
from core.connectors.postgresql.objects import sequence as _postgres_sequence
from core.connectors.postgresql.objects import view as _postgres_view
from core.connectors.postgresql.objects import type as _postgres_type
from core.connectors.postgresql.objects import function as _postgres_function
from core.connectors.postgresql.objects import security as _postgres_security
from core.connectors.postgresql.objects import comment as _postgres_comment
from core.connectors.postgresql.objects import trigger as _postgres_trigger
from core.connectors.postgresql.objects import partition as _postgres_partition
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff


class PostgresTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("psycopg")
        import psycopg
        # Connect to 'postgres' DB first (needed for ensure_database_exists)
        cfg = dict(self._config)
        cfg["database"] = cfg.get("database", "postgres")
        self._conn = psycopg.connect(**_make_conn_kwargs(cfg))
        audit_log(phase="connect", status="success", details={"engine": "postgresql", "role": "target"})

    def ensure_database_exists(self) -> None:
        """Create the target database if it is missing, then connect to it.

        Bootstrap step that must run before any object is applied. PostgreSQL
        refuses ``CREATE DATABASE`` inside a transaction, so ``autocommit`` is
        toggled on for the existence probe and the create, and toggled back off
        afterwards. The connection is then closed and reopened against the
        target database, because the original session was established against
        the maintenance database (see ``connect``).
        """
        dbname = self._config["database"]
        validate_identifier(dbname, "database")
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {dbname}")
                audit_log(phase="ensure_database", status="created", details={"database": dbname})
        self._conn.autocommit = False
        # Reconnect to the target database
        import psycopg
        self._conn.close()
        self._conn = psycopg.connect(**_make_conn_kwargs(self._config))

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------

    def create_extension(self, ext: ExtensionDef) -> None:
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext.name}")
                self._conn.commit()
                audit_log(phase="create_extension", status="created", details={"extension": ext.name})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_extension", status="skipped",
                          details={"extension": ext.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Schemas
    # ------------------------------------------------------------------

    def create_schema(self, schema_def: SchemaDef) -> None:
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_def.name}")
                self._conn.commit()
                audit_log(phase="create_schema", status="created", details={"schema": schema_def.name})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_schema", status="skipped",
                          details={"schema": schema_def.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Custom Types
    # ------------------------------------------------------------------

    def create_type(self, type_def: TypeDef) -> None:
        return _postgres_type.create_type(self._conn, type_def)

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def create_object_if_missing(self, schema: Schema) -> None:
        return _postgres_table.create_table(self._conn, schema)

    # ------------------------------------------------------------------
    # Sequences
    # ------------------------------------------------------------------

    def create_sequence(self, seq: SequenceDef) -> None:
        return _postgres_sequence.create_sequence(self._conn, seq)

    def advance_sequence(self, seq_name: str, table: str, column: str, owned_by: str | None = None) -> None:
        return _postgres_sequence.advance_sequence(
            self._conn, seq_name, table, column, owned_by
        )

    def sync_sequence(self, table: str, column: str, schema_name: str | None = None) -> None:
        return _postgres_sequence.sync_sequence(self._conn, table, column, schema_name)

    def apply_sequence_ownership(self, seq: SequenceDef) -> None:
        return _postgres_sequence.apply_sequence_ownership(self._conn, seq)

    # ------------------------------------------------------------------
    # Partitions
    # ------------------------------------------------------------------

    def create_partition(self, partition: PartitionDef) -> None:
        return _postgres_partition.create_partition(self._conn, partition)

    # ------------------------------------------------------------------
    # Data load
    # ------------------------------------------------------------------

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        return _postgres_table.upsert_table_data(self._conn, object_name, rows, schema)

    # ------------------------------------------------------------------
    # Indexes, CHECK constraints, Foreign keys
    # ------------------------------------------------------------------

    def apply_constraints(self, schema: Schema) -> None:
        """Apply indexes, CHECK constraints and foreign keys.

        Retained in ``target.py`` as a single combined constraint layer. The
        three families are applied in dependency order — indexes first, then
        CHECK constraints, then foreign keys last, because every referenced
        table must already exist.

        All three share the same shape: execute, commit and audit, rolling back
        and auditing a skip on failure. Splitting them into separate modules
        would duplicate that scaffolding and still require this method to
        orchestrate the ordering.

        This is the target-side counterpart of
        ``PostgresSourceConnector.get_schema()``, which discovers the same
        families in the same order.
        """
        validate_identifier(schema.name, "table")
        table_qname = _qualify(schema.schema_name, schema.name)
        with self._conn.cursor() as cur:
            # Indexes (use full DDL from pg_get_indexdef if available)
            for idx in schema.indexes:
                try:
                    if idx.ddl:
                        # Replace CREATE INDEX with CREATE INDEX IF NOT EXISTS
                        ddl = idx.ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
                        ddl = ddl.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
                        cur.execute(ddl)
                    else:
                        idx_type = "UNIQUE INDEX" if idx.unique else "INDEX"
                        col_list = ", ".join(idx.columns)
                        cur.execute(
                            f"CREATE {idx_type} IF NOT EXISTS {idx.name} "
                            f"ON {table_qname} ({col_list})"
                        )
                    self._conn.commit()
                    audit_log(phase="create_index", status="created",
                              details={"table": schema.name, "index": idx.name, "unique": idx.unique})
                except Exception as exc:
                    self._conn.rollback()
                    audit_log(phase="create_index", status="skipped",
                              details={"index": idx.name, "reason": str(exc)})

            # Check Constraints
            for chk in schema.check_constraints:
                try:
                    cur.execute(
                        f"ALTER TABLE {table_qname} "
                        f"ADD CONSTRAINT {chk.name} CHECK ({chk.expression})"
                    )
                    self._conn.commit()
                    audit_log(phase="create_check", status="created",
                              details={"table": schema.name, "constraint": chk.name})
                except Exception as exc:
                    self._conn.rollback()
                    audit_log(phase="create_check", status="skipped",
                              details={"constraint": chk.name, "reason": str(exc)})

            # Foreign Keys (applied last — all tables must exist first)
            for fk in schema.foreign_keys:
                col_list = ", ".join(fk.columns)
                ref_col_list = ", ".join(fk.ref_columns)
                ref_table_qname = _qualify(fk.ref_schema, fk.ref_table)
                try:
                    cur.execute(
                        f"ALTER TABLE {table_qname} "
                        f"ADD CONSTRAINT {fk.name} "
                        f"FOREIGN KEY ({col_list}) "
                        f"REFERENCES {ref_table_qname} ({ref_col_list}) "
                        f"ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}"
                    )
                    self._conn.commit()
                    audit_log(phase="create_fk", status="created",
                              details={"table": schema.name, "fk": fk.name, "ref_table": ref_table_qname})
                except Exception as exc:
                    self._conn.rollback()
                    audit_log(phase="create_fk", status="skipped",
                              details={"fk": fk.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Row-Level Security
    # ------------------------------------------------------------------

    def apply_rls_policy(self, policy: RLSPolicy) -> None:
        return _postgres_security.apply_rls_policy(self._conn, policy)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def create_view(self, view: ViewDefinition) -> None:
        return _postgres_view.create_view(self._conn, view)

    def create_materialized_view(self, mv: MaterializedViewDef) -> None:
        return _postgres_view.create_materialized_view(self._conn, mv)

    def refresh_materialized_view(self, name: str, schema_name: str | None = None) -> None:
        return _postgres_view.refresh_materialized_view(self._conn, name, schema_name)

    # ------------------------------------------------------------------
    # Functions & Stored Procedures
    # ------------------------------------------------------------------

    def create_function(self, func: FunctionDef) -> None:
        return _postgres_function.create_function(self._conn, func)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    def create_trigger(self, trigger: TriggerDef) -> None:
        return _postgres_trigger.create_trigger(self._conn, trigger)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def apply_comment(self, comment: CommentDef) -> None:
        return _postgres_comment.apply_comment(self._conn, comment)

    # ------------------------------------------------------------------
    # Grants
    # ------------------------------------------------------------------

    def apply_grant(self, grant: GrantDef) -> None:
        return _postgres_security.apply_grant(self._conn, grant)

    def create_role_if_not_exists(self, role_name: str) -> None:
        return _postgres_security.create_role_if_not_exists(self._conn, role_name)

    # ------------------------------------------------------------------
    # Table read / row maintenance API
    # ------------------------------------------------------------------

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        return _postgres_table.get_row_count(self._conn, object_name, schema_name)

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        return _postgres_table.delete_row(self._conn, object_name, document, schema)

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        yield from _postgres_table.export_target_data(self._conn, object_name, schema_name)
