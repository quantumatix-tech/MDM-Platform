"""MSSQL Target Connector — connector-facing target API / routing layer.

Every object-specific creation and application implementation lives under
``core.connectors.mssql.objects``; this module owns the connector surface
and forwards to it:

  table     : table creation, upsert, export, delete, row counts
  view      : view creation
  trigger   : trigger creation
  function  : function/procedure creation
  sequence  : sequence creation
  synonym   : synonym creation
  type      : user-defined (alias) type creation
  comment   : extended property (comment) application
  partition : partition function/scheme and partitioned-table creation
  security  : role, user, role-membership and GRANT application

Three responsibilities intentionally remain here rather than in an object
module:

``connect()``
    Shared connector infrastructure (ODBC connection string, driver
    setup, retry, audit).

``ensure_database_exists()``
    Database bootstrap infrastructure, not an object operation. It must
    run before any schema/table/object deployment.

``apply_constraints()``
    Deliberate combined constraint layer for indexes, foreign keys, CHECK
    and DEFAULT constraints, applied in dependency order and sharing one
    existence/idempotency/audit/rollback pattern. See its docstring.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import (
    GrantDef,
    Schema,
    TargetConnector,
    UpsertResult,
    ViewDefinition,
    FunctionDef,
    SynonymDef,
    TypeDef,
    TriggerDef,
    validate_identifier,
    quote_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log
from core.dependency_order import order_data_load_objects

from core.connectors.mssql._models import (
    PartitionFunctionDef,
    PartitionSchemeDef,
    _qualify,
)
from core.connectors.mssql.objects import table as _mssql_table
from core.connectors.mssql.objects import view as _mssql_view
from core.connectors.mssql.objects import trigger as _mssql_trigger
from core.connectors.mssql.objects import function as _mssql_function
from core.connectors.mssql.objects import sequence as _mssql_sequence
from core.connectors.mssql.objects import synonym as _mssql_synonym
from core.connectors.mssql.objects import type as _mssql_type
from core.connectors.mssql.objects import comment as _mssql_comment
from core.connectors.mssql.objects import partition as _mssql_partition
from core.connectors.mssql.objects import security as _mssql_security


class MSSQLTargetConnector(TargetConnector):

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        # Keep the source schema metadata supplied by the orchestrator. FULL
        # sync cleanup must be limited to these tables; catalog-wide cleanup
        # could otherwise delete unrelated target-owned data.
        self._managed_schemas: dict[str, Schema] = {}
        self._suspended_triggers: list[TriggerDef] = []
        self._deferred_not_null_columns: dict[str, list[str]] = {}

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

    def get_server_version(self) -> str | None:
        """Return MSSQL server version string."""
        if self._conn is None:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT @@VERSION")
                row = cur.fetchone()
                if row:
                    return row[0]
        except Exception:
            pass
        return None

    def ensure_database_exists(self) -> None:
        """Create the target database if it does not exist.

        Database bootstrap infrastructure, not an object-level operation:
        it must complete before any schema, table or object deployment.

        Intentionally does not call ``validate_identifier`` — SQL Server
        database names may legitimately contain hyphens, and the name must
        still reach ``CREATE DATABASE`` correctly.
        """
        db_name = self._config["database"]
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM sys.databases WHERE name = ?", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {quote_identifier(db_name)}")
                audit_log(phase="ensure_database", status="created", details={"database": db_name})

    def create_sequence(self, seq: "SequenceDef") -> None:
        """Create a schema-qualified SQL Server sequence with source metadata.

        Delegates to ``core.connectors.mssql.objects.sequence.create_sequence``.
        """
        _mssql_sequence.create_sequence(self._conn, seq)

    def advance_sequence(
        self, sequence_name: str, table: str, column: str,
        owned_by: str | None = None,
    ) -> None:
        """Advance an owned sequence beyond loaded table data without rewinding it."""
        _mssql_sequence.advance_sequence(
            self._conn, sequence_name, table, column, owned_by
        )

    def create_type(self, type_def: "TypeDef") -> None:
        """Create a user-defined (alias) data type on the target.

        SQL Server has no ``CREATE OR ALTER TYPE``; re-runs are made idempotent
        by checking sys.types first. CREATE TYPE must be the sole statement in
        its batch, so it is executed on its own cursor.execute().

        Delegates to ``core.connectors.mssql.objects.type.create_type``.
        """
        _mssql_type.create_type(self._conn, type_def)

    def create_object_if_missing(self, schema: "Schema") -> None:
        """Create a table if it doesn't exist. Delegates to ``table.create_table``."""
        _mssql_table.create_table(self._conn, schema, self._config)
        self._managed_schemas[schema.name] = schema

    def reconcile_mssql_table(self, schema: Schema) -> dict[str, list[str]]:
        """Create a source table if missing and reconcile its definitions."""
        _mssql_table.create_table(self._conn, schema, self._config)
        self._managed_schemas[schema.name] = schema
        outcome = _mssql_table.reconcile_table_columns(self._conn, schema)
        # FULL reconciliation only visits source-managed tables. Remove
        # standalone target indexes that have no corresponding source
        # definition, while leaving PK/UNIQUE constraint indexes alone.
        source_index_names = {index.name for index in schema.indexes}
        source_index_names.update(
            name for name in (
                schema.primary_key_name,
                *(constraint.name for constraint in schema.unique_constraints),
            ) if name
        )
        table_qname = _qualify(schema.schema_name or "dbo", schema.name)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT i.name FROM sys.indexes i "
                "WHERE i.object_id = OBJECT_ID(?) AND i.index_id > 0 "
                "AND i.is_primary_key = 0 AND i.is_unique_constraint = 0",
                (table_qname,),
            )
            extra_indexes = sorted(
                row[0] for row in cur.fetchall()
                if row[0] not in source_index_names
            )
            for index_name in extra_indexes:
                cur.execute(
                    f"DROP INDEX {quote_identifier(index_name)} ON {table_qname}"
                )
        if extra_indexes:
            self._conn.commit()
            audit_log(
                phase="reconcile_table",
                status="reconciled",
                details={"table": schema.name, "dropped_indexes": extra_indexes},
            )
        if outcome.get("deferred_not_null_columns"):
            self._deferred_not_null_columns[schema.name] = outcome[
                "deferred_not_null_columns"
            ]
        return outcome

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        """Delete rows from known source-managed tables before a FULL load.

        Tables are deleted in reverse FK dependency order so SQL Server
        constraints remain enabled. Names absent from ``_managed_schemas``
        are ignored, preserving target-only tables and their data.
        """
        managed = {
            name: self._managed_schemas[name]
            for name in objects
            if name in self._managed_schemas
        }
        if not managed:
            return []

        delete_order = list(reversed(order_data_load_objects(managed, list(managed))))
        try:
            with self._conn.cursor() as cur:
                for name in delete_order:
                    schema = managed[name]
                    qualified = _qualify(schema.schema_name or "dbo", schema.name)
                    cur.execute(f"DELETE FROM {qualified}")
                # Finalize staged NOT NULL additions within the same
                # transaction as cleanup. If any ALTER fails, rollback also
                # restores all source-managed rows deleted above.
                for name in delete_order:
                    columns = self._deferred_not_null_columns.get(name, [])
                    if columns:
                        _mssql_table.finalize_required_columns(
                            cur, managed[name], columns
                        )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        for name in delete_order:
            self._deferred_not_null_columns.pop(name, None)

        audit_log(
            phase="clear_full_sync",
            status="success",
            details={"tables": delete_order},
        )
        return delete_order

    def upsert_batch(
        self,
        object_name: str,
        rows: Iterator[dict[str, Any]],
        schema: "Schema | None" = None,
    ) -> UpsertResult:
        """Upsert a batch of rows. Delegates to ``table.upsert_table_data``."""
        return _mssql_table.upsert_table_data(self._conn, object_name, rows, schema)

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        """Count rows in a table. Delegates to ``table.get_table_row_count``."""
        return _mssql_table.get_table_row_count(self._conn, object_name, schema_name)

    def delete(self, object_name: str, document: dict[str, Any], schema: "Schema | None" = None) -> None:
        """Delete rows from a table. Delegates to ``table.delete_from_table``."""
        _mssql_table.delete_from_table(self._conn, object_name, document, schema)

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict]:
        """Stream all rows from a table. Delegates to ``table.export_table_data``."""
        yield from _mssql_table.export_table_data(self._conn, object_name, schema_name)

    def apply_constraints(self, schema: "Schema") -> list[str]:
        """Apply indexes, unique constraints, foreign keys, CHECK and DEFAULT constraints.

        Intentionally retained in ``target.py`` as a single combined
        constraint layer. The five families are applied in dependency
        order — indexes, then unique constraints, then foreign keys, then
        CHECK, then DEFAULT — because each stage can depend on the objects
        created by the previous one.

        All five families share the same shape: probe for an existing
        object, skip with an audit entry when present, otherwise execute,
        commit and audit, rolling back and auditing on failure. Splitting
        them into separate modules would duplicate that scaffolding and
        still require this method to orchestrate the ordering.

        This is the target-side counterpart of
        ``MSSQLSourceConnector.get_schema()``, which discovers the same
        five families in the same order.
        """
        validate_identifier(schema.name, "table")
        schema_name = schema.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        table_qname = _qualify(schema_name, schema.name)
        errors: list[str] = []
        with self._conn.cursor() as cur:
            for idx in schema.indexes:
                if idx.constraint_backed:
                    audit_log(
                        phase="create_index", status="skipped",
                        details={"index": idx.name, "reason": "backed by a source constraint"},
                    )
                    continue
                try:
                    ddl = idx.ddl
                    if not ddl:
                        raise RuntimeError(f"Source DDL is unavailable for index {idx.name}")
                    cur.execute(
                        "SELECT i.is_unique, i.filter_definition, c.name, "
                        "ic.key_ordinal, ic.is_included_column, ic.is_descending_key, "
                        "i.is_primary_key, i.is_unique_constraint "
                        "FROM sys.indexes i "
                        "LEFT JOIN sys.index_columns ic "
                        "ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                        "LEFT JOIN sys.columns c "
                        "ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                        "WHERE i.object_id = OBJECT_ID(?) AND i.name = ? "
                        "ORDER BY ic.is_included_column, ic.key_ordinal, ic.index_column_id",
                        (table_qname, idx.name),
                    )
                    existing_rows = cur.fetchall()
                    if existing_rows:
                        actual_keys = [
                            row[2] for row in existing_rows
                            if row[2] is not None and not bool(row[4])
                        ]
                        actual_includes = [
                            row[2] for row in existing_rows
                            if row[2] is not None and bool(row[4])
                        ]
                        actual_descending = [
                            row[2] for row in existing_rows
                            if row[2] is not None and not bool(row[4]) and bool(row[5])
                        ]
                        actual_unique = bool(existing_rows[0][0])
                        actual_filter = existing_rows[0][1]
                        is_constraint_backed = any(
                            bool(row[6]) or bool(row[7]) for row in existing_rows
                        )
                        matches = (
                            actual_keys == list(idx.columns)
                            and actual_descending == list(idx.descending_columns)
                            and set(actual_includes) == set(idx.included_columns)
                            and actual_unique == bool(idx.unique)
                            and (actual_filter or None) == (idx.filter_definition or None)
                        )
                        if matches:
                            audit_log(
                                phase="create_index", status="skipped",
                                details={"index": idx.name, "reason": "definition already matches"},
                            )
                            continue
                        if is_constraint_backed:
                            raise RuntimeError(
                                f"Cannot reconcile mismatched index {idx.name}: "
                                "target index backs a primary-key or unique constraint"
                            )
                        cur.execute(f"DROP INDEX {quote_identifier(idx.name)} ON {table_qname}")
                    cur.execute(ddl)
                    self._conn.commit()
                    audit_log(
                        phase="create_index", status="created",
                        details={"table": schema.name, "index": idx.name, "unique": idx.unique},
                    )
                except Exception as exc:
                    self._conn.rollback()
                    errors.append(f"index {idx.name}: {exc}")
                    audit_log(
                        phase="create_index", status="failed",
                        details={"index": idx.name, "reason": str(exc)},
                    )

            # --- Unique Constraints (idempotent) ---
            existing_uqs = set()
            try:
                cur.execute(
                    "SELECT i.name FROM sys.indexes i "
                    "WHERE i.object_id = OBJECT_ID(?) AND i.is_unique_constraint = 1",
                    (table_qname,),
                )
                existing_uqs = {row[0] for row in cur.fetchall()}
            except Exception as exc:
                errors.append(f"unique constraint catalog query for {schema.name}: {exc}")
            for uq in schema.unique_constraints:
                if uq.name in existing_uqs:
                    audit_log(
                        phase="create_unique", status="skipped",
                        details={"unique": uq.name, "reason": "already exists"},
                    )
                    continue
                col_list = ", ".join(quote_identifier(c) for c in uq.columns)
                try:
                    cur.execute(
                        f"ALTER TABLE {table_qname} "
                        f"ADD CONSTRAINT {quote_identifier(uq.name)} "
                        f"UNIQUE ({col_list})"
                    )
                    self._conn.commit()
                    audit_log(
                        phase="create_unique", status="created",
                        details={"table": schema.name, "unique": uq.name,
                                 "columns": uq.columns},
                    )
                except Exception as exc:
                    self._conn.rollback()
                    errors.append(f"unique constraint {uq.name}: {exc}")
                    audit_log(
                        phase="create_unique", status="failed",
                        details={"unique": uq.name, "reason": str(exc)},
                    )

            # --- Foreign Keys (cross-schema aware, idempotent) ---
            try:
                cur.execute(
                    "SELECT name, is_disabled, is_not_trusted FROM sys.foreign_keys "
                    "WHERE parent_object_id = OBJECT_ID(?)",
                    (table_qname,),
                )
                existing_fks = {
                    row[0]: (bool(row[1]), bool(row[2])) for row in cur.fetchall()
                }
            except Exception as exc:
                errors.append(f"foreign key catalog query for {schema.name}: {exc}")
                existing_fks = {}
            for fk in schema.foreign_keys:
                if fk.name in existing_fks:
                    target_disabled, target_untrusted = existing_fks[fk.name]
                    source_state = (fk.is_disabled, fk.is_not_trusted)
                    target_state = (target_disabled, target_untrusted)
                    if source_state != target_state:
                        try:
                            if fk.is_disabled:
                                cur.execute(
                                    f"ALTER TABLE {table_qname} NOCHECK CONSTRAINT "
                                    f"{quote_identifier(fk.name)}"
                                )
                            elif fk.is_not_trusted:
                                cur.execute(
                                    f"ALTER TABLE {table_qname} WITH NOCHECK CHECK CONSTRAINT "
                                    f"{quote_identifier(fk.name)}"
                                )
                            else:
                                # WITH CHECK validates existing rows before
                                # enabling the FK and marks it trusted on success.
                                cur.execute(
                                    f"ALTER TABLE {table_qname} WITH CHECK CHECK CONSTRAINT "
                                    f"{quote_identifier(fk.name)}"
                                )
                            self._conn.commit()
                        except Exception as exc:
                            self._conn.rollback()
                            errors.append(f"foreign key {fk.name}: {exc}")
                            audit_log(
                                phase="create_fk", status="failed",
                                details={"fk": fk.name, "reason": str(exc)},
                            )
                            continue
                    audit_log(
                        phase="create_fk",
                        status="reconciled" if target_disabled or target_untrusted else "skipped",
                        details={
                            "fk": fk.name,
                            "source_disabled": fk.is_disabled,
                            "source_untrusted": fk.is_not_trusted,
                            "target_disabled": target_disabled,
                            "target_untrusted": target_untrusted,
                        },
                    )
                    continue
                col_list = ", ".join(quote_identifier(c) for c in fk.columns)
                ref_col_list = ", ".join(quote_identifier(c) for c in fk.ref_columns)
                ref_schema_q = quote_identifier(fk.ref_schema or "dbo")
                ref_table_q = quote_identifier(fk.ref_table)
                ref_qname = f"{ref_schema_q}.{ref_table_q}"
                try:
                    cur.execute(
                        f"ALTER TABLE {table_qname} "
                        f"ADD CONSTRAINT {quote_identifier(fk.name)} "
                        f"FOREIGN KEY ({col_list}) "
                        f"REFERENCES {ref_qname} ({ref_col_list})",
                    )
                    self._conn.commit()
                    audit_log(
                        phase="create_fk", status="created",
                        details={"table": schema.name, "fk": fk.name,
                                 "ref_table": ref_qname},
                    )
                except Exception as exc:
                    self._conn.rollback()
                    errors.append(f"foreign key {fk.name}: {exc}")
                    audit_log(
                        phase="create_fk", status="failed",
                        details={"fk": fk.name, "reason": str(exc)},
                    )

            # --- CHECK Constraints (idempotent) ---
            existing_checks = set()
            try:
                cur.execute(
                    "SELECT name FROM sys.check_constraints "
                    "WHERE parent_object_id = OBJECT_ID(?)",
                    (table_qname,),
                )
                existing_checks = {row[0] for row in cur.fetchall()}
            except Exception as exc:
                errors.append(f"check constraint catalog query for {schema.name}: {exc}")
            for chk in schema.check_constraints:
                if chk.name in existing_checks:
                    audit_log(
                        phase="create_check", status="skipped",
                        details={"check": chk.name, "reason": "already exists"},
                    )
                    continue
                try:
                    cur.execute(
                        f"ALTER TABLE {table_qname} "
                        f"ADD CONSTRAINT {quote_identifier(chk.name)} "
                        f"CHECK {chk.expression}"
                    )
                    self._conn.commit()
                    audit_log(
                        phase="create_check", status="created",
                        details={"table": schema.name, "check": chk.name},
                    )
                except Exception as exc:
                    self._conn.rollback()
                    errors.append(f"check constraint {chk.name}: {exc}")
                    audit_log(
                        phase="create_check", status="failed",
                        details={"check": chk.name, "reason": str(exc)},
                    )

            # --- DEFAULT Constraints (idempotent) ---
            existing_defaults = set()
            try:
                cur.execute(
                    "SELECT name FROM sys.default_constraints "
                    "WHERE parent_object_id = OBJECT_ID(?)",
                    (table_qname,),
                )
                existing_defaults = {row[0] for row in cur.fetchall()}
            except Exception as exc:
                errors.append(f"default constraint catalog query for {schema.name}: {exc}")
            for dfl in schema.default_constraints:
                if dfl.name in existing_defaults:
                    audit_log(
                        phase="create_default", status="skipped",
                        details={"default": dfl.name, "reason": "already exists"},
                    )
                    continue
                try:
                    cur.execute(
                        f"ALTER TABLE {table_qname} "
                        f"ADD CONSTRAINT {quote_identifier(dfl.name)} "
                        f"DEFAULT {dfl.definition} FOR {quote_identifier(dfl.column)}"
                    )
                    self._conn.commit()
                    audit_log(
                        phase="create_default", status="created",
                        details={"table": schema.name, "default": dfl.name},
                    )
                except Exception as exc:
                    self._conn.rollback()
                    errors.append(f"default constraint {dfl.name}: {exc}")
                    audit_log(
                        phase="create_default", status="failed",
                        details={"default": dfl.name, "reason": str(exc)},
                    )
        return errors

    def create_view(self, view: "ViewDefinition") -> None:
        """Create or alter a view. Delegates to ``view.create_view``."""
        _mssql_view.create_view(self._conn, view)

    def create_function(self, func: "FunctionDef") -> None:
        """Create or alter a function on the target database.

        Delegates to ``core.connectors.mssql.objects.function.create_function``.
        """
        _mssql_function.create_function(self._conn, func)

    def create_trigger(self, trigger: "TriggerDef") -> bool | None:
        """Create or alter a trigger on the target database.

        Delegates to ``core.connectors.mssql.objects.trigger.create_trigger``.
        """
        return _mssql_trigger.create_trigger(self._conn, trigger)

    def suspend_triggers_for_data_load(
        self, triggers: list["TriggerDef"]
    ) -> list["TriggerDef"]:
        """Disable matching existing triggers during a FULL data reload."""
        self._suspended_triggers = _mssql_trigger.suspend_triggers_for_data_load(
            self._conn, triggers
        )
        return list(self._suspended_triggers)

    def restore_suspended_triggers(self) -> None:
        """Restore suspended triggers to their pre-load state after failure."""
        if self._suspended_triggers:
            suspended = self._suspended_triggers
            self._suspended_triggers = []
            _mssql_trigger.restore_suspended_triggers(self._conn, suspended)

    def complete_trigger_data_load(self) -> None:
        """Forget the snapshot after Phase 15 applies source trigger state."""
        self._suspended_triggers = []

    def create_synonym(self, synonym: "SynonymDef") -> None:
        """Create a synonym on the target database.

        Delegates to ``core.connectors.mssql.objects.synonym.create_synonym``.
        """
        _mssql_synonym.create_synonym(self._conn, synonym)

    # ------------------------------------------------------------------
    # Step 12 — Partition creation
    # ------------------------------------------------------------------

    def create_partition_function(self, pf: "PartitionFunctionDef") -> None:
        """Create a partition function from metadata.

        Delegates to ``objects.partition.create_partition_function``.
        """
        _mssql_partition.create_partition_function(self._conn, pf)

    def reconcile_partition_function(
        self, pf: "PartitionFunctionDef", source_metadata: dict[str, Any]
    ) -> None:
        """Apply only the explicitly supported non-destructive PF reconciliation."""
        _mssql_partition.reconcile_partition_function(self._conn, pf, source_metadata)

    def reconcile_partition_scheme(
        self, ps: "PartitionSchemeDef", source_metadata: dict[str, Any]
    ) -> None:
        """Restore an eligible explicit NEXT USED mapping without rebuilding the scheme."""
        _mssql_partition.reconcile_partition_scheme(self._conn, ps, source_metadata)

    def create_partition_scheme(self, ps: "PartitionSchemeDef") -> None:
        """Create a partition scheme from metadata.

        Delegates to ``objects.partition.create_partition_scheme``.
        """
        _mssql_partition.create_partition_scheme(self._conn, ps)

    def create_partitioned_table(
        self,
        schema: "Schema",
        partition_scheme_name: str,
        partition_column: str,
    ) -> None:
        """Create a table with partitioning applied.

        Delegates to ``objects.partition.create_partitioned_table``.
        """
        _mssql_partition.create_partitioned_table(
            self._conn, schema, partition_scheme_name, partition_column
        )
        self._managed_schemas[schema.name] = schema

    def inspect_partition_metadata(self) -> dict[str, Any]:
        """Return complete SQL Server partition metadata for validation."""
        return {
            "functions": _mssql_partition.inspect_partition_functions(self._conn),
            "schemes": _mssql_partition.inspect_partition_schemes(self._conn),
            "tables": _mssql_partition.inspect_partitioned_tables(self._conn),
        }

    # ------------------------------------------------------------------
    # Step 14 — Security: Roles, Users & Role Memberships (target creation)
    # ------------------------------------------------------------------

    def create_role_if_not_exists(self, role_name: str) -> None:
        """Create a database role on the target if it does not already exist.

        Delegates to
        ``core.connectors.mssql.objects.security.create_role_if_not_exists``.
        """
        _mssql_security.create_role_if_not_exists(self._conn, role_name)

    def create_user_if_not_exists(self, user_name: str) -> None:
        """Create a database user on the target if it does not already exist.

        Delegates to
        ``core.connectors.mssql.objects.security.create_user_if_not_exists``.
        """
        _mssql_security.create_user_if_not_exists(self._conn, user_name)

    def create_role_membership(self, member_name: str, role_name: str) -> None:
        """Add a database principal to a database role (idempotent).

        Delegates to
        ``core.connectors.mssql.objects.security.create_role_membership``.
        """
        _mssql_security.create_role_membership(
            self._conn, member_name, role_name
        )

    def apply_grant(self, grant: "GrantDef") -> None:
        """Apply a GRANT statement using MSSQL-native syntax.

        Delegates to ``core.connectors.mssql.objects.security.apply_grant``.
        """
        _mssql_security.apply_grant(self._conn, self._config, grant)

    # ------------------------------------------------------------------
    # Step 15 — Comments / Extended Properties
    # ------------------------------------------------------------------

    def apply_comment(self, comment: "CommentDef") -> None:
        """Apply an extended property (comment) using sp_addextendedproperty / sp_updateextendedproperty.

        Idempotent: adds if missing, updates if already present.

        Delegates to ``core.connectors.mssql.objects.comment.apply_comment``.
        """
        _mssql_comment.apply_comment(self._conn, comment)
