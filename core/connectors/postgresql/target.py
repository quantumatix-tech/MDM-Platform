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
    quote_identifier,
    validate_identifier,
)
from core.dependency_order import order_data_load_objects
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
        self._managed_schemas: dict[str, Schema] = {}
        self._pending_not_null_columns: list[tuple[str, str, str]] = []

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("psycopg")
        import psycopg
        # Connect to 'postgres' DB first (needed for ensure_database_exists)
        cfg = dict(self._config)
        cfg["database"] = cfg.get("database", "postgres")
        self._conn = psycopg.connect(**_make_conn_kwargs(cfg))
        audit_log(phase="connect", status="success", details={"engine": "postgresql", "role": "target"})

    def close(self) -> None:
        """Roll back any unfinished transaction and release the connection."""
        conn = self._conn
        if conn is None:
            return
        try:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
        finally:
            self._conn = None

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

    def reconcile_postgresql_types(
        self, type_defs: list[TypeDef], managed_schemas: list[Schema],
        managed_views: list[ViewDefinition] | None = None,
    ) -> list[str]:
        return _postgres_type.reconcile_enum_types(
            self._conn, type_defs, managed_schemas, managed_views
        )

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def create_object_if_missing(self, schema: Schema) -> None:
        result = _postgres_table.create_table(self._conn, schema)
        self._managed_schemas[schema.name] = schema
        return result

    def reconcile_postgresql_table(
        self, schema: Schema, managed_views: list[ViewDefinition] | None = None
    ) -> str:
        """Reconcile columns and table-local objects for a managed table.

        Target-only columns, constraints and indexes are removed. Missing
        source columns are added and source column types are restored. The
        operation never drops or enumerates unrelated target tables.
        """
        self.create_object_if_missing(schema)
        table_schema = schema.schema_name or "public"
        validate_identifier(schema.name, "table")
        source_columns = {column.name: column for column in schema.columns}
        for column_name in source_columns:
            validate_identifier(column_name, "column")

        pending_not_null_columns: list[tuple[str, str, str]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT c.column_name, "
                    "pg_catalog.format_type(a.atttypid, a.atttypmod) "
                    "FROM information_schema.columns c "
                    "JOIN pg_class pc ON pc.relname = c.table_name "
                    "JOIN pg_namespace pn ON pn.oid = pc.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid = pc.oid "
                    "AND a.attname = c.column_name AND a.attnum > 0 "
                    "AND NOT a.attisdropped "
                    "WHERE pn.nspname = %s AND c.table_name = %s",
                    (table_schema, schema.name),
                )
                target_column_types = dict(cur.fetchall())
                target_columns = set(target_column_types)
                source_column_names = set(source_columns)
                extra_columns = sorted(target_columns - source_column_names)
                missing_columns = sorted(source_column_names - target_columns)
                changed_types = [
                    column_name
                    for column_name in sorted(source_column_names & target_columns)
                    if (source_columns[column_name].target_type or source_columns[column_name].source_type).strip().lower()
                    != str(target_column_types[column_name]).strip().lower()
                ]
                table_qname = _qualify(table_schema, schema.name)

                if changed_types:
                    self._drop_managed_views_before_type_change(
                        cur,
                        [
                            view for view in (managed_views or [])
                            if (view.schema_name or "public") == table_schema
                        ],
                    )

                for column_name in missing_columns:
                    column = source_columns[column_name]
                    col_type = column.target_type or column.source_type
                    definition = f"{quote_identifier(column_name)} {col_type}"
                    if column.generated:
                        definition += f" GENERATED ALWAYS AS ({column.generated}) STORED"
                    elif column.is_identity:
                        identity_kind = column.identity_kind if column.identity_kind in {"ALWAYS", "BY DEFAULT"} else "BY DEFAULT"
                        identity_options = []
                        if column.identity_seed is not None:
                            identity_options.append(f"START WITH {column.identity_seed}")
                        if column.identity_increment is not None:
                            identity_options.append(f"INCREMENT BY {column.identity_increment}")
                        options = f" ({' '.join(identity_options)})" if identity_options else ""
                        definition += f" GENERATED {identity_kind} AS IDENTITY{options}"
                    else:
                        if column.default is not None:
                            definition += f" DEFAULT {column.default}"
                        if column.nullable:
                            definition += " NULL"
                        elif column.default is None:
                            # FULL sync clears managed rows after schema
                            # reconciliation. Enforce NOT NULL immediately
                            # after that clear, before source rows are loaded.
                            pending_not_null_columns.append(
                                (table_schema, schema.name, column_name)
                            )
                        else:
                            definition += " NOT NULL"
                    if column.generated and not column.nullable:
                        definition += " NOT NULL"
                    cur.execute(f"ALTER TABLE {table_qname} ADD COLUMN {definition}")

                for column_name in changed_types:
                    source_type = source_columns[column_name].target_type or source_columns[column_name].source_type
                    column_qname = quote_identifier(column_name)
                    cur.execute(
                        f"ALTER TABLE {table_qname} ALTER COLUMN {column_qname} "
                        f"TYPE {source_type} USING {column_qname}::{source_type}"
                    )

                for column_name in extra_columns:
                    cur.execute(
                        f"ALTER TABLE {table_qname} DROP COLUMN "
                        f"{quote_identifier(column_name)}"
                    )

                # Reconciliation must also remove target-only table-local
                # constraints and indexes. Source definitions are reapplied
                # after the data load by apply_constraints().
                cur.execute(
                    "SELECT conname, contype FROM pg_constraint "
                    "WHERE conrelid = to_regclass(%s)",
                    (f'"{table_schema}"."{schema.name}"',),
                )
                target_constraints = cur.fetchall()
                dropped_constraints: list[str] = []
                source_constraint_names = {
                    name
                    for name in (
                        schema.primary_key_name,
                        *(constraint.name for constraint in schema.unique_constraints),
                        *(constraint.name for constraint in schema.check_constraints),
                        *(constraint.name for constraint in schema.foreign_keys),
                    )
                    if name
                }
                for constraint_name, constraint_type in target_constraints:
                    is_source_primary_key = bool(schema.primary_key) and constraint_type == "p"
                    if (
                        constraint_name not in source_constraint_names
                        and not is_source_primary_key
                    ):
                        cur.execute(
                            f"ALTER TABLE {table_qname} DROP CONSTRAINT "
                            f"{quote_identifier(constraint_name)}"
                        )
                        dropped_constraints.append(constraint_name)

                cur.execute(
                    "SELECT index_class.relname "
                    "FROM pg_index i "
                    "JOIN pg_class table_class ON table_class.oid = i.indrelid "
                    "JOIN pg_namespace table_ns ON table_ns.oid = table_class.relnamespace "
                    "JOIN pg_class index_class ON index_class.oid = i.indexrelid "
                    "WHERE table_ns.nspname = %s AND table_class.relname = %s",
                    (table_schema, schema.name),
                )
                target_indexes = {row[0] for row in cur.fetchall()}
                source_index_names = {index.name for index in schema.indexes}
                source_index_names.update(source_constraint_names)
                dropped_indexes = sorted(target_indexes - source_index_names)
                for index_name in dropped_indexes:
                    cur.execute(
                        f"DROP INDEX {quote_identifier(table_schema)}."
                        f"{quote_identifier(index_name)}"
                    )
            self._conn.commit()
            self._pending_not_null_columns.extend(pending_not_null_columns)
        except Exception:
            self._conn.rollback()
            raise

        audit_log(
            phase="reconcile_table",
            status="reconciled"
            if extra_columns or missing_columns or changed_types or dropped_constraints or dropped_indexes
            else "unchanged",
            details={
                "table": schema.name,
                "dropped_columns": extra_columns,
                "added_columns": missing_columns,
                "altered_types": changed_types,
                "dropped_constraints": dropped_constraints,
                "dropped_indexes": dropped_indexes,
            },
        )
        return (
            "reconciled"
            if extra_columns or missing_columns or changed_types or dropped_constraints or dropped_indexes
            else "unchanged"
        )

    @staticmethod
    def _drop_managed_views_before_type_change(cur: Any, views: list[ViewDefinition]) -> None:
        """Drop only source-managed views, dependents first, without CASCADE.

        Savepoints let the method retry a blocked view after dropping its
        dependent source-managed view. A target-only dependent keeps the drop
        blocked; in that case type reconciliation fails safely and rolls back.
        """
        pending = list(views)
        while pending:
            remaining: list[ViewDefinition] = []
            dropped_any = False
            for view in pending:
                schema_name = view.schema_name or "public"
                view_qname = _qualify(schema_name, view.name)
                cur.execute("SAVEPOINT reconcile_managed_view")
                try:
                    cur.execute(f"DROP VIEW IF EXISTS {view_qname}")
                    cur.execute("RELEASE SAVEPOINT reconcile_managed_view")
                    dropped_any = True
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT reconcile_managed_view")
                    cur.execute("RELEASE SAVEPOINT reconcile_managed_view")
                    remaining.append(view)
            if remaining and not dropped_any:
                names = [f"{view.schema_name}.{view.name}" for view in remaining]
                raise RuntimeError(
                    "Could not safely detach dependent source-managed views before "
                    f"column type reconciliation: {names}. Check target-only dependencies."
                )
            pending = remaining

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

    def restore_standalone_sequence_state(self, seq: SequenceDef) -> None:
        return _postgres_sequence.restore_standalone_sequence_state(self._conn, seq)

    # ------------------------------------------------------------------
    # Partitions
    # ------------------------------------------------------------------

    def create_partition(self, partition: PartitionDef) -> None:
        result = _postgres_partition.create_partition(self._conn, partition)
        self._managed_schemas.setdefault(
            partition.parent_table,
            Schema(name=partition.parent_table, schema_name=partition.schema or "public"),
        )
        return result

    def reconcile_partitions(
        self,
        source_partitions: list[PartitionDef],
        managed_schemas: list[Schema],
    ) -> list[str]:
        """Drop extra child partitions for source-managed partitioned tables.

        Only parents represented in the source partition inventory are
        reconciled. PostgreSQL refuses DROP TABLE on an attached partition;
        DETACH PARTITION followed by DROP TABLE removes the child without
        cascading to unrelated dependent objects.
        """
        source_by_parent: dict[tuple[str, str], set[str]] = {
            (schema.schema_name or "public", schema.name): set()
            for schema in managed_schemas
            if schema.partition_key
        }
        for partition in source_partitions:
            key = (partition.schema or "public", partition.parent_table)
            source_by_parent.setdefault(key, set()).add(partition.name)

        removed: list[str] = []
        try:
            with self._conn.cursor() as cur:
                for (schema_name, parent_name), source_names in source_by_parent.items():
                    cur.execute(
                        "SELECT child.relname FROM pg_inherits i "
                        "JOIN pg_class parent ON parent.oid = i.inhparent "
                        "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
                        "JOIN pg_class child ON child.oid = i.inhrelid "
                        "WHERE parent_ns.nspname = %s AND parent.relname = %s "
                        "AND child.relispartition",
                        (schema_name, parent_name),
                    )
                    target_names = {row[0] for row in cur.fetchall()}
                    parent_qname = _qualify(schema_name, parent_name)
                    for partition_name in sorted(target_names - source_names):
                        part_qname = _qualify(schema_name, partition_name)
                        cur.execute(
                            f"ALTER TABLE {parent_qname} DETACH PARTITION {part_qname}"
                        )
                        cur.execute(f"DROP TABLE {part_qname}")
                        removed.append(partition_name)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return removed

    # ------------------------------------------------------------------
    # Data load
    # ------------------------------------------------------------------

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        return _postgres_table.upsert_table_data(self._conn, object_name, rows, schema)

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        """Delete rows from source-managed tables before a FULL replacement load.

        Children are deleted before their referenced parents to satisfy
        PostgreSQL foreign keys. Only object names supplied by the orchestrator
        are considered, so target-only tables are never added to the clear set.
        """
        managed = {
            name: self._managed_schemas[name]
            for name in objects
            if name in self._managed_schemas
        }
        if not managed:
            return []

        ordered_tables = order_data_load_objects(managed, list(managed))
        delete_order = list(reversed(ordered_tables))
        try:
            with self._conn.cursor() as cur:
                for table in delete_order:
                    schema = managed[table]
                    table_name = _qualify(schema.schema_name, schema.name)
                    cur.execute(f"DELETE FROM {table_name}")
                for schema_name, table_name, column_name in self._pending_not_null_columns:
                    if table_name in managed:
                        cur.execute(
                            f"ALTER TABLE {_qualify(schema_name, table_name)} "
                            f"ALTER COLUMN {quote_identifier(column_name)} SET NOT NULL"
                        )
            self._conn.commit()
            self._pending_not_null_columns = [
                pending for pending in self._pending_not_null_columns
                if pending[1] not in managed
            ]
        except Exception:
            self._conn.rollback()
            raise

        audit_log(
            phase="clear_full_sync",
            status="success",
            details={"tables": delete_order},
        )
        return delete_order

    # ------------------------------------------------------------------
    # Indexes, CHECK constraints, Foreign keys
    # ------------------------------------------------------------------

    def apply_constraints(self, schema: Schema) -> None:
        """Apply unique constraints, CHECK constraints, indexes and foreign keys.

        Unique constraints are applied **before** indexes. A UNIQUE constraint
        makes PostgreSQL create its own backing index carrying the constraint
        name, so creating that index first makes the subsequent
        ``ADD CONSTRAINT`` fail on the name collision. Unique constraints are
        therefore created first, and the source marks indexes that back a
        constraint (``Index.constraint_backed``, resolved from
        ``pg_constraint.conindid`` on the source side) so they are not created a
        second time as standalone indexes. A manually-created UNIQUE INDEX has
        no backing constraint and is still migrated as an ordinary index.

        Remaining order is dependency order — CHECK constraints, then indexes,
        then foreign keys last, because every referenced table must already
        exist.

        Every object is applied inside its own savepoint. A failure rolls back
        only that savepoint, so one bad constraint cannot abort the surrounding
        transaction and cascade into the CHECK/index/FK objects applied after
        it. Genuine creation errors are audited as failures; only the
        already-exists race (same object recreated by a concurrent/earlier run)
        is downgraded to a skip.
        """
        validate_identifier(schema.name, "table")
        table_qname = _qualify(schema.schema_name, schema.name)
        with self._conn.cursor() as cur:
            # Unique Constraints (first — see method docstring for ordering)
            for uq in schema.unique_constraints:
                self._apply_one_constraint(
                    cur,
                    f"ALTER TABLE {table_qname} "
                    f"ADD CONSTRAINT {uq.name} UNIQUE ({', '.join(uq.columns)})",
                    phase="create_unique",
                    label=uq.name,
                    table=schema.name,
                )

            # Check Constraints
            for chk in schema.check_constraints:
                self._apply_one_constraint(
                    cur,
                    f"ALTER TABLE {table_qname} "
                    f"ADD CONSTRAINT {chk.name} CHECK ({chk.expression})",
                    phase="create_check",
                    label=chk.name,
                    table=schema.name,
                )

            # Indexes (use full DDL from pg_get_indexdef if available)
            for idx in schema.indexes:
                if idx.constraint_backed:
                    # Recreated automatically by the unique/PK constraint that
                    # owns it — creating it again here would collide on name.
                    audit_log(phase="create_index", status="skipped",
                              details={"table": schema.name, "index": idx.name,
                                       "reason": "backs constraint"})
                    continue
                try:
                    cur.execute("SAVEPOINT apply_index")
                except Exception:
                    pass
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

            # Foreign Keys (applied last — all tables must exist first)
            for fk in schema.foreign_keys:
                col_list = ", ".join(fk.columns)
                ref_col_list = ", ".join(fk.ref_columns)
                ref_table_qname = _qualify(fk.ref_schema, fk.ref_table)
                try:
                    cur.execute("SAVEPOINT apply_fk")
                except Exception:
                    pass
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

    def _apply_one_constraint(
        self,
        cur: Any,
        sql: str,
        *,
        phase: str,
        label: str,
        table: str,
    ) -> None:
        """Apply a single ALTER TABLE ... ADD CONSTRAINT in its own savepoint.

        Isolating each constraint keeps a failure local: the savepoint rollback
        clears the aborted-transaction state so the next CHECK/index/FK object
        still runs. The existing per-method contract is preserved — failures are
        swallowed and audited rather than raised — but an object that genuinely
        already exists is reported as a skip, and every other error is reported
        with its real reason instead of being mislabelled.
        """
        try:
            cur.execute("SAVEPOINT apply_constraint")
        except Exception:
            pass
        try:
            cur.execute(sql)
            self._conn.commit()
            audit_log(phase=phase, status="created",
                      details={"table": table, "constraint": label})
        except Exception as exc:
            self._conn.rollback()
            reason = str(exc)
            if "already exists" in reason.lower():
                audit_log(phase=phase, status="skipped",
                          details={"constraint": label, "reason": reason})
            else:
                audit_log(phase=phase, status="failed",
                          details={"constraint": label, "reason": reason})

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

    def reconcile_materialized_view(self, mv: MaterializedViewDef) -> None:
        return _postgres_view.reconcile_materialized_view(self._conn, mv)

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

    def suspend_triggers_for_data_load(
        self, triggers: list[TriggerDef]
    ) -> list[TriggerDef]:
        return _postgres_trigger.suspend_triggers_for_data_load(self._conn, triggers)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def apply_comment(self, comment: CommentDef) -> None:
        return _postgres_comment.apply_comment(self._conn, comment)

    def reconcile_postgresql_column_comments(
        self, managed_schemas: list[Schema], source_comments: list[CommentDef]
    ) -> list[str]:
        return _postgres_comment.reconcile_column_comments(
            self._conn, managed_schemas, source_comments
        )

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
