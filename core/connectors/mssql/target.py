"""MSSQL Target Connector.

Extracted from ``core/connectors/mssql.py`` — all DDL creation, constraint
application, and write logic for the target database.

The two column-introspection helpers ``_non_computed_column_names`` and
``_variant_column_names`` are resolved through the ``core.connectors.mssql``
package namespace at call time (via local imports inside ``export_full``) so
that test patches on those names are honoured.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from core.connectors.base import (
    GrantDef,
    Schema,
    TargetConnector,
    UpsertResult,
    UnmappedTypeError,
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
    _mssql_column_type,
    _qualify,
    _target_identity_columns,
)


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
        db_name = self._config["database"]
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM sys.databases WHERE name = ?", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {quote_identifier(db_name)}")
                audit_log(phase="ensure_database", status="created", details={"database": db_name})

    def create_sequence(self, seq: "SequenceDef") -> None:
        """Create a schema-qualified SQL Server sequence with source metadata."""
        from core.connectors.base import SequenceDef  # noqa: F401

        seq_schema = seq.schema or "dbo"
        validate_identifier(seq.name, "sequence")
        validate_identifier(seq_schema, "schema")
        seq_qname = _qualify(seq_schema, seq.name)

        with self._conn.cursor() as cur:
            if seq_schema != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (seq_schema,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(seq_schema)}")
                    audit_log(
                        phase="create_schema",
                        status="created",
                        details={"schema": seq_schema},
                    )

            cur.execute(
                "SELECT 1 FROM sys.sequences "
                "WHERE name = ? AND schema_id = SCHEMA_ID(?)",
                (seq.name, seq_schema),
            )
            if cur.fetchone() is not None:
                return

            cycle_clause = "CYCLE" if seq.cycle else "NO CYCLE"
            cache_clause = (
                "NO CACHE"
                if not seq.is_cached
                else f"CACHE {int(seq.cache_size)}"
            )
            ddl = (
                f"CREATE SEQUENCE {seq_qname} "
                f"AS {(seq.data_type or 'bigint').upper()} "
                f"START WITH {int(seq.start_value)} "
                f"INCREMENT BY {int(seq.increment)} "
                f"MINVALUE {int(seq.min_value)} "
                f"MAXVALUE {int(seq.max_value)} "
                f"{cycle_clause} "
                f"{cache_clause}"
            )
            try:
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_sequence",
                    status="created",
                    details={"sequence": seq_qname, "owned_by": seq.owned_by},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_sequence", status="failed",
                    details={"sequence": seq_qname, "reason": str(exc)},
                )
                raise

    def create_type(self, type_def: "TypeDef") -> None:
        """Create a user-defined (alias) data type on the target.

        SQL Server has no ``CREATE OR ALTER TYPE``; re-runs are made idempotent
        by checking sys.types first. CREATE TYPE must be the sole statement in
        its batch, so it is executed on its own cursor.execute().
        """
        # TypeDef.name is schema-qualified ("schema.type") for MSSQL UDTs.
        name = type_def.name
        if "." in name:
            type_schema, type_name = name.split(".", 1)
        else:
            type_schema, type_name = "dbo", name
        validate_identifier(type_name, "type")
        validate_identifier(type_schema, "schema")

        with self._conn.cursor() as cur:
            # Ensure the target schema exists (dbo always exists).
            if type_schema != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (type_schema,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(type_schema)}")
                    audit_log(
                        phase="create_schema", status="created", details={"schema": type_schema}
                    )

            cur.execute(
                "SELECT 1 FROM sys.types "
                "WHERE is_user_defined = 1 "
                "AND name = ? AND schema_id = SCHEMA_ID(?)",
                (type_name, type_schema),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_type", status="exists",
                    details={"type": f"{type_schema}.{type_name}"},
                )
                return

            try:
                cur.execute(type_def.ddl)
                self._conn.commit()
                audit_log(
                    phase="create_type", status="created",
                    details={"type": f"{type_schema}.{type_name}", "kind": type_def.kind},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_type", status="failed",
                    details={"type": f"{type_schema}.{type_name}", "reason": str(exc)},
                )
                raise

    def create_object_if_missing(self, schema: "Schema") -> None:
        validate_identifier(schema.name, "table")
        schema_name = schema.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        qualified = _qualify(schema_name, schema.name)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
                (schema.name, schema_name),
            )
            if cur.fetchone() is not None:
                return

            # Ensure the target schema exists (dbo always exists in SQL Server).
            if schema_name != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                    audit_log(phase="create_schema", status="created", details={"schema": schema_name})

            col_defs = []
            for col in schema.columns:
                if col.is_computed:
                    # Computed columns: AS (<definition>). Nullability is inferred
                    # from the expression (specifying NULL/NOT NULL requires PERSISTED, error 8183)
                    # — so omit it and let SQL Server infer, matching the source.
                    col_defs.append(f"{col.name} AS ({col.computed_definition})")
                    continue
                if col.target_type is None:
                    if self._config.get("source_engine") == "mssql":
                        col_type = col.source_type
                    else:
                        raise UnmappedTypeError(
                            table=schema.name,
                            column=col.name,
                            source_type=col.source_type,
                            source_engine=self._config.get("source_engine"),
                            target_engine="mssql",
                        )
                else:
                    col_type = col.target_type
                col_type = _mssql_column_type(col_type, col.size, col.precision, col.scale)
                identity_part = ""
                if col.is_identity and col.identity_seed is not None and col.identity_increment is not None:
                    identity_part = f" IDENTITY({col.identity_seed},{col.identity_increment})"
                null_str = "NULL" if col.nullable else "NOT NULL"
                col_defs.append(f"{col.name} {col_type}{identity_part} {null_str}")

            if schema.primary_key:
                pk_cols = ", ".join(schema.primary_key)
                col_defs.append(f"PRIMARY KEY ({pk_cols})")

            ddl = f"CREATE TABLE {qualified} ({', '.join(col_defs)})"
            cur.execute(ddl)
            self._conn.commit()
            audit_log(phase="create_table", status="created", details={"table": qualified})

    def upsert_batch(
        self,
        object_name: str,
        rows: Iterator[dict[str, Any]],
        schema: "Schema | None" = None,
    ) -> "UpsertResult":
        validate_identifier(object_name, "table")
        result = UpsertResult()
        batch = list(rows)

        if not batch:
            return result

        schema_name = (schema.schema_name or "dbo") if schema else None
        qualified = _qualify(schema_name, object_name)
        # Inspect the ACTUAL target table: only enable IDENTITY_INSERT when the
        # destination column is really an IDENTITY column (preserves source ids).
        # Pre-existing Step 4 tables created as plain INT are left untouched.
        target_id_cols = set(_target_identity_columns(self._conn, object_name, schema_name))
        # Source-side identity columns cannot appear in the UPDATE SET of a MERGE
        # (SQL Server: "Cannot update identity column", error 8102), but they MUST
        # stay in the INSERT list so the explicit source identity values are kept.
        schema_id_cols = {c.name for c in (schema.columns if schema else []) if c.is_identity}
        # Identify sql_variant columns — pyodbc cannot bind them as ? parameters;
        # use CAST(? AS SQL_VARIANT) so the source string values are coerced.
        variant_cols: set[str] = set()
        if schema:
            variant_cols = {
                c.name
                for c in schema.columns
                if c.source_type and "sql_variant" in c.source_type.lower()
            }
        with self._conn.cursor() as cur:
            columns = list(batch[0].keys())
            col_names = ", ".join(columns)
            placeholders = ", ".join(
                "CAST(? AS SQL_VARIANT)" if col in variant_cols else "?"
                for col in columns
            )
            updatable_cols = [c for c in columns if c not in schema_id_cols]
            update_set = ", ".join(
                f"target.{col} = source.{col}" for col in updatable_cols
            )

            pk_cols = schema.primary_key if schema else []
            if pk_cols:
                pk_clause = " AND ".join(f"target.{col} = source.{col}" for col in pk_cols)
                on_clause = pk_clause
            else:
                on_clause = "1=0"

            sql = (
                f"MERGE INTO {qualified} AS target "
                f"USING (SELECT {placeholders}) AS source ({col_names}) "
                f"ON {on_clause} "
                f"WHEN MATCHED THEN UPDATE SET {update_set} "
                f"WHEN NOT MATCHED THEN INSERT ({col_names}) VALUES (source.{col_names});"
            )

            need_id_insert = bool(target_id_cols & set(columns))
            try:
                if need_id_insert:
                    cur.execute(f"SET IDENTITY_INSERT {qualified} ON")
                for row in batch:
                    values = [row.get(col) for col in columns]
                    cur.execute(sql, values)
                self._conn.commit()
                result.success_count = len(batch)
                audit_log(phase="upsert_batch", status="success", details={"table": qualified, "count": len(batch)})
            except Exception as exc:
                self._conn.rollback()
                result.failure_count = len(batch)
                result.errors.append(str(exc))
                result.failed_items.extend(batch)
                audit_log(phase="upsert_batch", status="failure", details={"table": qualified, "error": str(exc)})
            finally:
                if need_id_insert:
                    cur.execute(f"SET IDENTITY_INSERT {qualified} OFF")

        return result

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        qualified = _qualify(schema_name, object_name)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {qualified}")
            return cur.fetchone()[0]

    def delete(self, object_name: str, document: dict[str, Any], schema: "Schema | None" = None) -> None:
        validate_identifier(object_name, "table")
        schema_name = (schema.schema_name or "dbo") if schema else None
        qualified = _qualify(schema_name, object_name)
        with self._conn.cursor() as cur:
            if schema and schema.primary_key:
                conditions = []
                values = []
                for pk_col in schema.primary_key:
                    conditions.append(f"{pk_col} = ?")
                    values.append(document.get(pk_col))
                where_clause = " AND ".join(conditions)
                cur.execute(f"DELETE FROM {qualified} WHERE {where_clause}", values)
            else:
                cur.execute(f"DELETE FROM {qualified} WHERE id = ?", (document.get("id"),))
            self._conn.commit()
            audit_log(phase="cdc_delete", status="deleted", details={"table": qualified})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict]:
        # Resolved through the package namespace so test patches on
        # ``core.connectors.mssql._non_computed_column_names`` /
        # ``_variant_column_names`` are honoured at call time.
        from core.connectors.mssql import _non_computed_column_names, _variant_column_names

        validate_identifier(object_name, "table")
        qualified = _qualify(schema_name, object_name)
        with self._conn.cursor() as cur:
            cols = _non_computed_column_names(self._conn, object_name, schema_name)
            if cols:
                variant_cols = _variant_column_names(self._conn, object_name, schema_name)
                col_exprs = [
                    f"CAST({quote_identifier(c)} AS NVARCHAR(MAX)) AS {quote_identifier(c)}"
                    if c in variant_cols
                    else quote_identifier(c)
                    for c in cols
                ]
                col_list = ", ".join(col_exprs)
                cur.execute(f"SELECT {col_list} FROM {qualified}")
            else:
                cur.execute(f"SELECT * FROM {qualified}")
            columns = [desc[0] for desc in cur.description]
            for row in cur:
                yield dict(zip(columns, row))

    def apply_constraints(self, schema: "Schema") -> None:
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
        validate_identifier(view.name, "view")
        schema_name = view.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        view_qname = f"[{schema_name or 'dbo'}].[{view.name}]"
        with self._conn.cursor() as cur:
            # Ensure the target schema exists (dbo always exists in SQL Server).
            if schema_name != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": schema_name},
                    )
                    self._conn.commit()
        # CREATE VIEW must be the first statement in a batch.
        # Execute in a separate cursor/batch after schema creation commit.
        with self._conn.cursor() as cur:
            try:
                definition = view.definition
                if definition.upper().startswith("CREATE VIEW"):
                    definition = definition[len("CREATE VIEW"):].lstrip()
                    as_idx = definition.upper().find(" AS ")
                    if as_idx >= 0:
                        definition = definition[as_idx + 4:].lstrip()
                cur.execute(
                    f"CREATE OR ALTER VIEW {view_qname} AS {definition}"
                )
                self._conn.commit()
                audit_log(
                    phase="create_view", status="created",
                    details={"view": view.name},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_view", status="failed",
                    details={"view": view.name, "reason": str(exc)},
                )
                raise

    def create_function(self, func: "FunctionDef") -> None:
        validate_identifier(func.name, "function")
        schema_name = func.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        with self._conn.cursor() as cur:
            # Ensure the target schema exists (dbo always exists in SQL Server).
            if schema_name != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": schema_name},
                    )
            try:
                # The DDL from sys.sql_modules.definition already includes
                # "CREATE FUNCTION" or "CREATE PROCEDURE"; replace with CREATE OR ALTER
                # for idempotency across re-runs.
                ddl = func.ddl
                if ddl.upper().startswith("CREATE "):
                    ddl = "CREATE OR ALTER " + ddl[len("CREATE "):]
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_function", status="created",
                    details={"function": func.name, "schema": schema_name},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_function", status="failed",
                    details={"function": func.name, "schema": schema_name, "reason": str(exc)},
                )
                raise

    def create_trigger(self, trigger: "TriggerDef") -> None:
        """Create or alter a trigger on the target database.

        Uses ``CREATE OR ALTER TRIGGER`` (SQL Server 2016+ SP1) for idempotency.
        The DDL from ``sys.sql_modules`` is rewritten so the trigger name is
        schema-qualified (``[schema].[name]``) — the original text may use an
        unqualified name that would resolve to the wrong schema on the target.

        The enabled/disabled state is re-applied after creation so the target
        matches the source regardless of whether ``CREATE OR ALTER`` preserved
        a pre-existing state.

        Supports cross-schema triggers via trigger.table_schema.
        """
        validate_identifier(trigger.name, "trigger")
        schema_name = trigger.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        trigger_qname = f"[{schema_name}].[{trigger.name}]"
        table_schema = trigger.table_schema or schema_name
        table_qname = _qualify(table_schema, trigger.table)

        with self._conn.cursor() as cur:
            # Ensure the target schema exists (dbo always exists in SQL Server).
            if schema_name != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": schema_name},
                    )

            # Ensure the target table exists — a trigger cannot be created on
            # a missing parent table.
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
                (trigger.table, table_schema),
            )
            if cur.fetchone() is None:
                audit_log(
                    phase="create_trigger", status="skipped",
                    details={"trigger": trigger.name, "reason": f"parent table {table_qname} not found"},
                )
                return

            try:
                ddl = trigger.ddl
                # Rewrite CREATE TRIGGER <name> → CREATE OR ALTER TRIGGER [schema].[name]
                # for idempotency AND to ensure the correct schema regardless of
                # whether the source DDL used an unqualified name.
                qualified_trigger = f"[{schema_name}].[{trigger.name}]"
                new_ddl, n = re.subn(
                    r"CREATE\s+TRIGGER\s+\S+",
                    f"CREATE OR ALTER TRIGGER {qualified_trigger}",
                    ddl,
                    count=1,
                    flags=re.IGNORECASE,
                )
                if n == 0:
                    if new_ddl.upper().startswith("CREATE "):
                        new_ddl = "CREATE OR ALTER " + new_ddl[len("CREATE "):]
                ddl = new_ddl
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_trigger", status="created",
                    details={"trigger": trigger_qname, "table": table_qname,
                             "disabled": trigger.is_disabled},
                )

                # Re-apply enabled/disabled state to match the source.
                if trigger.is_disabled:
                    cur.execute(
                        f"ALTER TABLE {table_qname} DISABLE TRIGGER {quote_identifier(trigger.name)}"
                    )
                else:
                    cur.execute(
                        f"ALTER TABLE {table_qname} ENABLE TRIGGER {quote_identifier(trigger.name)}"
                    )
                self._conn.commit()
                audit_log(
                    phase="create_trigger", status="applied_state",
                    details={"trigger": trigger_qname, "disabled": trigger.is_disabled},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_trigger", status="failed",
                    details={"trigger": trigger.name, "schema": schema_name, "reason": str(exc)},
                )
                raise

    def create_synonym(self, synonym: "SynonymDef") -> None:
        """Create a synonym on the target database."""
        validate_identifier(synonym.name, "synonym")
        schema_name = synonym.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        # base_object should already be qualified like [schema].[object]
        base_object = synonym.base_object
        with self._conn.cursor() as cur:
            # Ensure the target schema exists (dbo always exists in SQL Server).
            if schema_name != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": schema_name},
                    )
            try:
                # Check if synonym already exists
                cur.execute(
                    "SELECT 1 FROM sys.synonyms WHERE name = ? AND schema_id = SCHEMA_ID(?)",
                    (synonym.name, schema_name),
                )
                if cur.fetchone() is not None:
                    audit_log(
                        phase="create_synonym", status="exists",
                        details={"synonym": f"{schema_name}.{synonym.name}"},
                    )
                    return
                # Create the synonym
                syn_qname = f"[{schema_name}].[{synonym.name}]"
                ddl = f"CREATE SYNONYM {syn_qname} FOR {base_object}"
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_synonym", status="created",
                    details={"synonym": f"{schema_name}.{synonym.name}", "base_object": base_object},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_synonym", status="failed",
                    details={"synonym": f"{schema_name}.{synonym.name}", "reason": str(exc)},
                )
                raise

    # ------------------------------------------------------------------
    # Step 12 — Partition creation
    # ------------------------------------------------------------------

    def create_partition_function(self, pf: "PartitionFunctionDef") -> None:
        """Create a partition function from metadata."""
        validate_identifier(pf.name, "partition function")
        validate_identifier(pf.schema_name, "schema")
        pf_schema = pf.schema_name or "dbo"

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.partition_functions WHERE name = ?",
                (pf.name,),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_partition_function", status="exists",
                    details={"function": f"{pf_schema}.{pf.name}"},
                )
                return

            if pf_schema != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (pf_schema,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(pf_schema)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": pf_schema},
                    )

            boundaries = ", ".join(
                f"'{b.strftime('%Y-%m-%d')}'" if isinstance(b, (datetime, date))
                else f"'{b}'" if isinstance(b, str)
                else str(b)
                for b in pf.boundaries
            )
            ddl = (
                f"CREATE PARTITION FUNCTION {quote_identifier(pf.name)} "
                f"({pf.data_type}) "
                f"AS {pf.range_desc} FOR VALUES ({boundaries})"
            )
            try:
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_partition_function", status="created",
                    details={"function": f"{pf_schema}.{pf.name}"},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_partition_function", status="failed",
                    details={"function": f"{pf_schema}.{pf.name}", "reason": str(exc)},
                )
                raise

    def create_partition_scheme(self, ps: "PartitionSchemeDef") -> None:
        """Create a partition scheme from metadata."""
        validate_identifier(ps.name, "partition scheme")
        validate_identifier(ps.schema_name, "schema")
        ps_schema = ps.schema_name or "dbo"

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.partition_schemes WHERE name = ?",
                (ps.name,),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_partition_scheme", status="exists",
                    details={"scheme": f"{ps_schema}.{ps.name}"},
                )
                return

            if ps_schema != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (ps_schema,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(ps_schema)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": ps_schema},
                    )

            filegroups = ", ".join(f"[{fg}]" for fg in ps.filegroups)
            ddl = (
                f"CREATE PARTITION SCHEME {quote_identifier(ps.name)} "
                f"AS PARTITION {quote_identifier(ps.partition_function_name)} "
                f"TO ({filegroups})"
            )
            try:
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_partition_scheme", status="created",
                    details={"scheme": f"{ps_schema}.{ps.name}"},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_partition_scheme", status="failed",
                    details={"scheme": f"{ps_schema}.{ps.name}", "reason": str(exc)},
                )
                raise

    def create_partitioned_table(
        self,
        schema: "Schema",
        partition_scheme_name: str,
        partition_column: str,
    ) -> None:
        """Create a table with partitioning applied."""
        validate_identifier(schema.name, "table")
        schema_name = schema.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        validate_identifier(partition_scheme_name, "partition scheme")
        validate_identifier(partition_column, "column")
        qualified = _qualify(schema_name, schema.name)

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?",
                (schema.name, schema_name),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_partitioned_table", status="exists",
                    details={"table": qualified},
                )
                return

            if schema_name != "dbo":
                cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                    audit_log(
                        phase="create_schema", status="created",
                        details={"schema": schema_name},
                    )

            col_defs = []
            for col in schema.columns:
                if col.is_computed:
                    col_defs.append(f"{col.name} AS ({col.computed_definition})")
                    continue
                col_type = col.source_type or col.target_type or "nvarchar"
                col_type = _mssql_column_type(col_type, col.size, col.precision, col.scale)
                identity_part = ""
                if col.is_identity and col.identity_seed is not None and col.identity_increment is not None:
                    identity_part = f" IDENTITY({col.identity_seed},{col.identity_increment})"
                null_str = "NULL" if col.nullable else "NOT NULL"
                col_defs.append(f"{col.name} {col_type}{identity_part} {null_str}")

            if schema.primary_key:
                pk_cols = ", ".join(schema.primary_key)
                col_defs.append(f"PRIMARY KEY ({pk_cols})")

            ddl = (
                f"CREATE TABLE {qualified} ({', '.join(col_defs)}) "
                f"ON {partition_scheme_name}({partition_column})"
            )
            try:
                cur.execute(ddl)
                self._conn.commit()
                audit_log(
                    phase="create_partitioned_table", status="created",
                    details={"table": qualified},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_partitioned_table", status="failed",
                    details={"table": qualified, "reason": str(exc)},
                )
                raise

    # ------------------------------------------------------------------
    # Step 14 — Security: Roles, Users & Role Memberships (target creation)
    # ------------------------------------------------------------------

    def create_role_if_not_exists(self, role_name: str) -> None:
        """Create a database role on the target if it does not already exist.

        Idempotent: if any database principal with that name already
        exists (role or user), creation is skipped.
        """
        validate_identifier(role_name, "role")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.database_principals WHERE name = ?",
                (role_name,),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_role", status="exists",
                    details={"role": role_name},
                )
                return
            try:
                cur.execute(f"CREATE ROLE [{role_name}]")
                self._conn.commit()
                audit_log(
                    phase="create_role", status="created",
                    details={"role": role_name},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_role", status="failed",
                    details={"role": role_name, "reason": str(exc)},
                )
                raise

    def create_user_if_not_exists(self, user_name: str) -> None:
        """Create a database user on the target if it does not already exist.

        Does NOT create server-level logins or migrate passwords.
        If a login with the same name exists on the server, the user is
        mapped to it; otherwise a contained user is created.
        """
        validate_identifier(user_name, "user")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.database_principals WHERE name = ?",
                (user_name,),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_user", status="exists",
                    details={"user": user_name},
                )
                return
            cur.execute(
                "SELECT 1 FROM sys.server_principals WHERE name = ?",
                (user_name,),
            )
            login_exists = cur.fetchone() is not None
            try:
                if login_exists:
                    cur.execute(f"CREATE USER [{user_name}] FOR LOGIN [{user_name}]")
                else:
                    cur.execute(f"CREATE USER [{user_name}] WITHOUT LOGIN")
                self._conn.commit()
                audit_log(
                    phase="create_user", status="created",
                    details={"user": user_name},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_user", status="failed",
                    details={"user": user_name, "reason": str(exc)},
                )
                raise

    def create_role_membership(self, member_name: str, role_name: str) -> None:
        """Add a database principal to a database role (idempotent)."""
        validate_identifier(member_name, "member")
        validate_identifier(role_name, "role")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.database_role_members drm "
                "JOIN sys.database_principals m "
                "  ON drm.member_principal_id = m.principal_id "
                "JOIN sys.database_principals r "
                "  ON drm.role_principal_id = r.principal_id "
                "WHERE m.name = ? AND r.name = ?",
                (member_name, role_name),
            )
            if cur.fetchone() is not None:
                audit_log(
                    phase="create_role_membership", status="exists",
                    details={"member": member_name, "role": role_name},
                )
                return
            try:
                cur.execute(
                    f"ALTER ROLE [{role_name}] ADD MEMBER [{member_name}]"
                )
                self._conn.commit()
                audit_log(
                    phase="create_role_membership", status="created",
                    details={"member": member_name, "role": role_name},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="create_role_membership", status="failed",
                    details={"member": member_name, "role": role_name, "reason": str(exc)},
                )
                raise

    def apply_grant(self, grant: "GrantDef") -> None:
        """Apply a GRANT statement using MSSQL-native syntax.

        Translates the cross-engine GrantDef into the appropriate
        MSSQL GRANT form based on object_type:
          - DATABASE: GRANT <privs> ON DATABASE::[db] TO [grantee]
          - SCHEMA:   GRANT <privs> ON SCHEMA::[schema] TO [grantee]
          - TABLE:    GRANT <privs> ON [schema].[table] TO [grantee]
          - COLUMN:   GRANT <privs> (<column>) ON [schema].[table] TO [grantee]
        """
        grantee_q = f"[{grant.grantee}]"
        privileges = grant.privileges
        schema_q = f"[{grant.schema_name or 'dbo'}]"
        with self._conn.cursor() as cur:
            try:
                if grant.object_type == "DATABASE":
                    db_name = grant.object_name or self._config.get("database", "master")
                    cur.execute(
                        f"GRANT {privileges} ON DATABASE::[{db_name}] TO {grantee_q}"
                    )
                elif grant.object_type == "SCHEMA":
                    cur.execute(
                        f"GRANT {privileges} ON SCHEMA::{schema_q} TO {grantee_q}"
                    )
                elif grant.object_type == "COLUMN":
                    parts = grant.object_name.split(".")
                    table_q = f"[{parts[0]}]"
                    column_q = f"[{parts[1]}]"
                    cur.execute(
                        f"GRANT {privileges} ({column_q}) "
                        f"ON {schema_q}.{table_q} TO {grantee_q}"
                    )
                else:
                    object_q = f"[{grant.object_name}]"
                    cur.execute(
                        f"GRANT {privileges} ON {schema_q}.{object_q} TO {grantee_q}"
                    )
                self._conn.commit()
                audit_log(
                    phase="apply_grant", status="applied",
                    details={"object": grant.object_name,
                             "grantee": grant.grantee,
                             "privileges": privileges},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="apply_grant", status="failed",
                    details={"object": grant.object_name,
                             "grantee": grant.grantee, "reason": str(exc)},
                )
                raise

    # ------------------------------------------------------------------
    # Step 15 — Comments / Extended Properties
    # ------------------------------------------------------------------

    def apply_comment(self, comment: "CommentDef") -> None:
        """Apply an extended property (comment) using sp_addextendedproperty / sp_updateextendedproperty.

        Idempotent: adds if missing, updates if already present.
        """
        from core.connectors.base import CommentDef

        if not comment.comment:
            audit_log(
                phase="apply_comment", status="skipped",
                details={"object": comment.object_name, "schema": comment.schema_name or "dbo",
                         "reason": "null or empty comment"},
            )
            return

        escaped = comment.comment.replace("'", "''")
        schema_name = comment.schema_name or "dbo"

        with self._conn.cursor() as cur:
            try:
                # Check if extended property already exists
                if comment.object_type == "SCHEMA":
                    cur.execute(
                        "SELECT 1 FROM sys.extended_properties ep "
                        "JOIN sys.schemas s ON ep.major_id = s.schema_id "
                        "WHERE ep.class = 3 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                        "AND s.name = ?",
                        (schema_name,),
                    )
                    exists = cur.fetchone() is not None
                    level0_type, level0_name = "SCHEMA", schema_name
                    level1_type = level1_name = level2_type = level2_name = None

                elif comment.object_type == "COLUMN":
                    parts = comment.object_name.split(".")
                    table_name, column_name = parts[0], parts[1]
                    cur.execute(
                        "SELECT 1 FROM sys.extended_properties ep "
                        "JOIN sys.objects o ON ep.major_id = o.object_id "
                        "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                        "JOIN sys.columns c ON c.object_id = o.object_id AND c.column_id = ep.minor_id "
                        "WHERE ep.class = 1 AND ep.minor_id > 0 AND ep.name = 'MS_Description' "
                        "AND s.name = ? AND o.name = ? AND c.name = ?",
                        (schema_name, table_name, column_name),
                    )
                    exists = cur.fetchone() is not None
                    level0_type, level0_name = "SCHEMA", schema_name
                    level1_type, level1_name = "TABLE", table_name
                    level2_type, level2_name = "COLUMN", column_name

                else:
                    # TABLE, VIEW, FUNCTION, PROCEDURE
                    object_name = comment.object_name
                    cur.execute(
                        "SELECT 1 FROM sys.extended_properties ep "
                        "JOIN sys.objects o ON ep.major_id = o.object_id "
                        "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                        "WHERE ep.class = 1 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                        "AND s.name = ? AND o.name = ?",
                        (schema_name, object_name),
                    )
                    exists = cur.fetchone() is not None
                    level0_type, level0_name = "SCHEMA", schema_name
                    level1_type, level1_name = comment.object_type.upper(), object_name
                    level2_type = level2_name = None

                if exists:
                    # UPDATE existing extended property
                    if comment.object_type == "SCHEMA":
                        cur.execute(
                            "EXEC sys.sp_updateextendedproperty "
                            "@name = 'MS_Description', @value = ?, "
                            "@level0type = ?, @level0name = ?",
                            (escaped, level0_type, level0_name),
                        )
                    elif comment.object_type == "COLUMN":
                        cur.execute(
                            "EXEC sys.sp_updateextendedproperty "
                            "@name = 'MS_Description', @value = ?, "
                            "@level0type = ?, @level0name = ?, "
                            "@level1type = ?, @level1name = ?, "
                            "@level2type = ?, @level2name = ?",
                            (escaped, level0_type, level0_name, level1_type, level1_name, level2_type, level2_name),
                        )
                    else:
                        cur.execute(
                            "EXEC sys.sp_updateextendedproperty "
                            "@name = 'MS_Description', @value = ?, "
                            "@level0type = ?, @level0name = ?, "
                            "@level1type = ?, @level1name = ?",
                            (escaped, level0_type, level0_name, level1_type, level1_name),
                        )
                    action = "updated"
                else:
                    # ADD new extended property
                    if comment.object_type == "SCHEMA":
                        cur.execute(
                            "EXEC sys.sp_addextendedproperty "
                            "@name = 'MS_Description', @value = ?, "
                            "@level0type = ?, @level0name = ?",
                            (escaped, level0_type, level0_name),
                        )
                    elif comment.object_type == "COLUMN":
                        cur.execute(
                            "EXEC sys.sp_addextendedproperty "
                            "@name = 'MS_Description', @value = ?, "
                            "@level0type = ?, @level0name = ?, "
                            "@level1type = ?, @level1name = ?, "
                            "@level2type = ?, @level2name = ?",
                            (escaped, level0_type, level0_name, level1_type, level1_name, level2_type, level2_name),
                        )
                    else:
                        cur.execute(
                            "EXEC sys.sp_addextendedproperty "
                            "@name = 'MS_Description', @value = ?, "
                            "@level0type = ?, @level0name = ?, "
                            "@level1type = ?, @level1name = ?",
                            (escaped, level0_type, level0_name, level1_type, level1_name),
                        )
                    action = "added"

                self._conn.commit()
                audit_log(
                    phase="apply_comment", status="applied",
                    details={"object": comment.object_name, "schema": schema_name, "action": action},
                )
            except Exception as exc:
                self._conn.rollback()
                audit_log(
                    phase="apply_comment", status="failed",
                    details={"object": comment.object_name, "schema": schema_name, "reason": str(exc)},
                )
                raise
