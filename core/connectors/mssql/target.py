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

    def apply_constraints(self, schema: "Schema") -> None:
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
        with self._conn.cursor() as cur:
            for idx in schema.indexes:
                try:
                    ddl = idx.ddl
                    if ddl:
                        cur.execute(ddl)
                    self._conn.commit()
                    audit_log(
                        phase="create_index", status="created",
                        details={"table": schema.name, "index": idx.name, "unique": idx.unique},
                    )
                except Exception as exc:
                    self._conn.rollback()
                    audit_log(
                        phase="create_index", status="skipped",
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
            except Exception:
                pass
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
                    audit_log(
                        phase="create_unique", status="skipped",
                        details={"unique": uq.name, "reason": str(exc)},
                    )

            # --- Foreign Keys (cross-schema aware, idempotent) ---
            cur.execute(
                "SELECT name FROM sys.foreign_keys "
                "WHERE parent_object_id = OBJECT_ID(?)",
                (table_qname,),
            )
            existing_fks = {row[0] for row in cur.fetchall()}
            for fk in schema.foreign_keys:
                if fk.name in existing_fks:
                    audit_log(
                        phase="create_fk", status="skipped",
                        details={"fk": fk.name, "reason": "already exists"},
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
                    audit_log(
                        phase="create_fk", status="skipped",
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
            except Exception:
                pass
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
                    audit_log(
                        phase="create_check", status="skipped",
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
            except Exception:
                pass
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
                    audit_log(
                        phase="create_default", status="skipped",
                        details={"default": dfl.name, "reason": str(exc)},
                    )

    def create_view(self, view: "ViewDefinition") -> None:
        """Create or alter a view. Delegates to ``view.create_view``."""
        _mssql_view.create_view(self._conn, view)

    def create_function(self, func: "FunctionDef") -> None:
        """Create or alter a function on the target database.

        Delegates to ``core.connectors.mssql.objects.function.create_function``.
        """
        _mssql_function.create_function(self._conn, func)

    def create_trigger(self, trigger: "TriggerDef") -> None:
        """Create or alter a trigger on the target database.

        Delegates to ``core.connectors.mssql.objects.trigger.create_trigger``.
        """
        _mssql_trigger.create_trigger(self._conn, trigger)

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
