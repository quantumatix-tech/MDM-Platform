"""PostgreSQL source connector — the connector-facing source API.

Owns the connection and exposes the discovery API the orchestrator calls.
Object-specific discovery is delegated to ``core.connectors.postgresql.objects``;
this module routes, it does not reimplement. The nine object modules are
``table``, ``sequence``, ``view``, ``type``, ``function``, ``security``,
``comment``, ``trigger`` and ``partition``.

Retained here by design:

  * ``__init__`` / ``connect`` / ``_include_schemas`` — connection lifecycle
    and the discovery scope every object query is filtered by.
  * ``get_schema`` — the cross-engine ``Schema`` DTO composition point. It
    intentionally aggregates several metadata families in one pass: resolved
    schema, columns (with defaults and GENERATED ALWAYS expressions), primary
    key, indexes, foreign keys, CHECK constraints, the RLS-enabled flag and the
    partition key. Splitting it across object modules would scatter that
    contract without simplifying the caller. It is the source-side counterpart
    of ``PostgresTargetConnector.apply_constraints()``.
  * ``list_schemas`` / ``list_extensions`` — small bootstrap discovery
    responsibilities that every other object depends on, and for which a
    separate object module would be pure ceremony.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    CheckConstraint,
    Column,
    CommentDef,
    ExtensionDef,
    ForeignKey,
    FunctionDef,
    GrantDef,
    Index,
    MaterializedViewDef,
    PartitionDef,
    RLSPolicy,
    Schema,
    SchemaDef,
    SequenceDef,
    UniqueConstraint,
    SourceConnector,
    TriggerDef,
    TypeDef,
    ViewDefinition,
    validate_identifier,
)
from core.connectors.postgresql._models import (
    _make_conn_kwargs,
    _resolve_include_schemas,
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


class PostgresSourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None

    @property
    def _include_schemas(self) -> tuple[str, ...]:
        return _resolve_include_schemas(self._config)

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("psycopg")
        import psycopg
        self._conn = psycopg.connect(**_make_conn_kwargs(self._config))
        audit_log(phase="connect", status="success", details={"engine": "postgresql", "role": "source"})

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def list_objects(self) -> list[str]:
        """List non-partition-child tables in the configured schemas."""
        validate_identifier(self._config.get("database", ""), "database")
        return _postgres_table.discover_tables(self._conn, self._include_schemas)

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        return _postgres_table.get_row_count(self._conn, object_name, schema_name)

    def export_full(self, object_name: str, statement_timeout_ms: int = 0, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream all rows from *object_name* using a server-side cursor."""
        yield from _postgres_table.export_table_data(
            self._conn,
            object_name,
            self._include_schemas,
            statement_timeout_ms,
            schema_name,
        )

    def get_schema(self, object_name: str) -> Schema:
        """Build the cross-engine ``Schema`` DTO for a base table.

        Intentionally retained here rather than moved into an object module.
        This is a *composition point*, not a single object implementation: one
        pass builds columns (including defaults and GENERATED ALWAYS
        expressions), primary key, indexes, foreign keys, CHECK constraints,
        the RLS flag and the partition key into the single ``Schema`` contract
        the target consumes. It is the source-side counterpart of
        ``PostgresTargetConnector.apply_constraints()``, which applies the
        same constraint families in the same order.
        """
        validate_identifier(object_name, "table")
        columns: list[Column] = []
        primary_key: list[str] = []
        primary_key_name: str | None = None
        indexes: list[Index] = []
        foreign_keys: list[ForeignKey] = []
        check_constraints: list[CheckConstraint] = []
        unique_constraints: list[UniqueConstraint] = []
        sequences: list[str] = []

        with self._conn.cursor() as cur:
            # --- Resolve actual source schema for this table ---
            cur.execute(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name = %s AND table_schema = ANY(%s) "
                "LIMIT 1",
                (object_name, list(self._include_schemas)),
            )
            schema_row = cur.fetchone()
            table_schema = schema_row[0] if schema_row else "public"

            # --- Columns (with defaults + GENERATED ALWAYS / IDENTITY detection) ---
            # Join pg_attribute to detect GENERATED ALWAYS AS (expr) STORED columns
            # (attgenerated = 's') and IDENTITY columns (attidentity = 'a'/'d').
            # Also read udt_name so ARRAY columns get their element type.
            cur.execute(
                "SELECT "
                "  c.column_name, "
                "  pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type, "
                "  c.is_nullable, "
                "  c.character_maximum_length, "
                "  c.column_default, "
                "  c.is_identity, "
                "  c.identity_generation, "
                "  c.identity_start, "
                "  c.identity_increment, "
                "  CASE WHEN a.attgenerated = 's' "
                "       THEN pg_get_expr(ad.adbin, ad.adrelid) "
                "       ELSE NULL END AS generation_expr "
                "FROM information_schema.columns c "
                "JOIN pg_class pc ON pc.relname = c.table_name "
                "  AND pc.relnamespace = ANY(SELECT oid FROM pg_namespace WHERE nspname = ANY(%s)) "
                "JOIN pg_attribute a ON a.attrelid = pc.oid AND a.attname = c.column_name "
                "  AND a.attnum > 0 AND NOT a.attisdropped "
                "LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
                "WHERE c.table_name = %s AND c.table_schema = ANY(%s) "
                "ORDER BY c.ordinal_position",
                (list(self._include_schemas), object_name, list(self._include_schemas)),
            )
            for row in cur.fetchall():
                (col_name, data_type, nullable, max_len, col_default, is_identity,
                 identity_generation, identity_start, identity_increment, gen_expr) = row
                is_seq = col_default is not None and "nextval" in str(col_default)
                if is_seq:
                    sequences.append(col_name)
                columns.append(Column(
                    name=col_name,
                    source_type=data_type,
                    target_type=None,
                    nullable=(nullable == "YES"),
                    size=max_len,
                    default=None if gen_expr else col_default,
                    generated=gen_expr,
                    is_identity = (is_identity == 'YES'),
                    identity_seed=int(identity_start) if identity_start is not None else None,
                    identity_increment=int(identity_increment) if identity_increment is not None else None,
                    identity_kind=identity_generation if is_identity else None,
                ))

            # --- Primary Key ---
            # The constraint name is selected alongside the columns so an
            # explicitly named source PK can be reproduced on the target.
            # pg_constraint is joined on conrelid (not on relname alone) so a
            # same-named table in another schema cannot contribute a constraint.
            # Column ordering still comes from kcu.ordinal_position, unchanged.
            cur.execute(
                "SELECT kcu.column_name, pgc.conname "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "LEFT JOIN pg_catalog.pg_constraint pgc "
                "  ON pgc.conname = tc.constraint_name "
                " AND pgc.contype = 'p' "
                " AND pgc.conrelid = to_regclass(quote_ident(tc.table_schema) || '.' || quote_ident(tc.table_name)) "
                "WHERE tc.table_name = %s AND tc.table_schema = %s "
                "  AND tc.constraint_type = 'PRIMARY KEY' "
                "ORDER BY kcu.ordinal_position",
                (object_name, table_schema),
            )
            pk_rows = cur.fetchall()
            primary_key = [row[0] for row in pk_rows]
            # All rows of one PK share a single constraint name; take it from
            # the first row so composite keys resolve to the same constraint.
            primary_key_name = pk_rows[0][1] if pk_rows else None

            # --- Indexes (full DDL via pg_get_indexdef — handles partial & expression) ---
            cur.execute(
                "SELECT i.relname, ix.indisunique, "
                "array_agg(a.attname ORDER BY a.attnum), "
                "pg_get_indexdef(i.oid), "
                # conindid points from a constraint to the index that backs it,
                # so this identifies indexes PostgreSQL created automatically
                # for UNIQUE/PK/EXCLUDE constraints. Manually-created unique
                # indexes return NULL here and stay independent indexes.
                "EXISTS (SELECT 1 FROM pg_constraint c "
                "        WHERE c.conindid = i.oid) AS constraint_backed "
                "FROM pg_class t "
                "JOIN pg_index ix ON t.oid = ix.indrelid "
                "JOIN pg_class i ON i.oid = ix.indexrelid "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey) "
                "JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = %s "
                "WHERE t.relname = %s AND NOT ix.indisprimary "
                "GROUP BY i.relname, ix.indisunique, i.oid",
                (table_schema, object_name),
            )
            for row in cur.fetchall():
                idx_name, is_unique, idx_cols, idx_ddl, constraint_backed = row
                indexes.append(Index(
                    name=idx_name,
                    columns=list(idx_cols),
                    unique=bool(is_unique),
                    ddl=idx_ddl,
                    constraint_backed=bool(constraint_backed),
                ))

            # --- Foreign Keys ---
            cur.execute(
                "SELECT tc.constraint_name, kcu.column_name, "
                "ccu.table_schema AS ref_schema, ccu.table_name AS ref_table, ccu.column_name AS ref_col, "
                "rc.delete_rule, rc.update_rule "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.referential_constraints rc "
                "  ON tc.constraint_name = rc.constraint_name "
                "  AND tc.constraint_schema = rc.constraint_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON rc.unique_constraint_name = ccu.constraint_name "
                "  AND rc.unique_constraint_schema = ccu.constraint_schema "
                "WHERE tc.table_name = %s AND tc.table_schema = %s "
                "  AND tc.constraint_type = 'FOREIGN KEY'",
                (object_name, table_schema),
            )
            fk_map: dict[str, ForeignKey] = {}
            for row in cur.fetchall():
                fk_name, col, ref_schema, ref_table, ref_col, on_delete, on_update = row
                if fk_name not in fk_map:
                    fk_map[fk_name] = ForeignKey(
                        name=fk_name, columns=[], ref_table=ref_table,
                        ref_columns=[], ref_schema=ref_schema,
                        on_delete=on_delete, on_update=on_update,
                    )
                fk_map[fk_name].columns.append(col)
                fk_map[fk_name].ref_columns.append(ref_col)
            foreign_keys = list(fk_map.values())

            # --- Check Constraints ---
            cur.execute(
                "SELECT tc.constraint_name, cc.check_clause "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.check_constraints cc "
                "  ON tc.constraint_name = cc.constraint_name "
                "  AND tc.constraint_schema = cc.constraint_schema "
                "WHERE tc.table_name = %s AND tc.table_schema = %s "
                "  AND tc.constraint_type = 'CHECK' "
                "  AND cc.check_clause NOT LIKE '%%IS NOT NULL%%'",
                (object_name, table_schema),
            )
            for row in cur.fetchall():
                chk_name, expression = row
                check_constraints.append(CheckConstraint(name=chk_name, expression=expression))

            # --- Unique Constraints (excluding primary keys) ---
            cur.execute(
                "SELECT conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint "
                "WHERE conrelid = (SELECT oid FROM pg_class WHERE relname = %s AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)) "
                "  AND contype = 'u'",
                (object_name, table_schema),
            )
            for row in cur.fetchall():
                uq_name, uq_def = row
                # Parse columns from constraint definition: UNIQUE (col1, col2)
                import re
                cols_match = re.search(r'UNIQUE\s*\(([^)]+)\)', uq_def, re.IGNORECASE)
                if cols_match:
                    cols = [c.strip().strip('"') for c in cols_match.group(1).split(',')]
                else:
                    cols = []
                unique_constraints.append(UniqueConstraint(name=uq_name, columns=cols))

            # --- RLS enabled? ---
            cur.execute(
                "SELECT c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = %s AND n.nspname = ANY(%s)",
                (object_name, list(self._include_schemas)),
            )
            rls_row = cur.fetchone()
            rls_enabled = bool(rls_row[0]) if rls_row else False

            # --- Partition key (for partitioned tables) ---
            cur.execute(
                "SELECT pg_get_partkeydef(c.oid) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = %s AND n.nspname = ANY(%s) AND c.relkind = 'p'",
                (object_name, list(self._include_schemas)),
            )
            part_row = cur.fetchone()
            partition_key = part_row[0] if part_row else None

        return Schema(
            name=object_name,
            schema_name=table_schema,
            columns=columns,
            primary_key=primary_key,
            primary_key_name=primary_key_name,
            indexes=indexes,
            foreign_keys=foreign_keys,
            check_constraints=check_constraints,
            unique_constraints=unique_constraints,
            sequences=sequences,
            rls_enabled=rls_enabled,
            partition_key=partition_key,
        )

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------

    def list_extensions(self) -> list[ExtensionDef]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT extname, n.nspname "
                "FROM pg_extension e "
                "JOIN pg_namespace n ON e.extnamespace = n.oid "
                "WHERE extname NOT IN ('plpgsql') "
                "ORDER BY extname"
            )
            return [ExtensionDef(name=row[0], schema=row[1]) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Sequences — all sequences, standalone + column-owned
    # ------------------------------------------------------------------

    def list_all_sequences(self) -> list[SequenceDef]:
        """Return every sequence in the configured schemas with full metadata."""
        return _postgres_sequence.discover_sequences(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Partitions — child partition tables
    # ------------------------------------------------------------------

    def list_partitions(self) -> list[PartitionDef]:
        """Return every partition child in the configured schemas."""
        return _postgres_partition.discover_partitions(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Schemas
    # ------------------------------------------------------------------

    def list_schemas(self) -> list[SchemaDef]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname NOT IN ('public', 'pg_catalog', 'information_schema', 'pg_toast') "
                "  AND nspname NOT LIKE 'pg_%' "
                "ORDER BY nspname"
            )
            return [SchemaDef(name=row[0]) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Custom Types: ENUM, DOMAIN, COMPOSITE
    # ------------------------------------------------------------------

    def list_types(self) -> list[TypeDef]:
        return _postgres_type.discover_types(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def list_views(self) -> list[ViewDefinition]:
        return _postgres_view.discover_views(self._conn, self._include_schemas)

    def list_materialized_views(self) -> list[MaterializedViewDef]:
        return _postgres_view.discover_materialized_views(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Functions & Stored Procedures
    # ------------------------------------------------------------------

    def list_functions(self) -> list[FunctionDef]:
        return _postgres_function.discover_functions(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    def get_all_triggers(self) -> list[TriggerDef]:
        return _postgres_trigger.discover_triggers(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Row-Level Security
    # ------------------------------------------------------------------

    def get_rls_policies(self, table: str, schema_name: str | None = None) -> list[RLSPolicy]:
        return _postgres_security.discover_rls_policies(
            self._conn, self._include_schemas, table, schema_name
        )

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def list_comments(self) -> list[CommentDef]:
        return _postgres_comment.discover_comments(self._conn, self._include_schemas)

    # ------------------------------------------------------------------
    # Grants
    # ------------------------------------------------------------------

    def list_grants(self) -> list[GrantDef]:
        return _postgres_security.discover_grants(self._conn, self._include_schemas)
