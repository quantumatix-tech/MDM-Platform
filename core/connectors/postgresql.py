from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    Index,
    ForeignKey,
    CheckConstraint,
    ViewDefinition,
    MaterializedViewDef,
    ExtensionDef,
    SchemaDef,
    TypeDef,
    FunctionDef,
    TriggerDef,
    RLSPolicy,
    CommentDef,
    GrantDef,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    validate_identifier,
    quote_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


def _make_conn_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Build psycopg connection kwargs from connector config."""
    kwargs: dict[str, Any] = {
        "host": config["host"],
        "port": config.get("port", 5432),
        "dbname": config.get("database", "postgres"),
        "user": config["username"],
        "password": config.get("password", ""),
        "sslmode": (
            "verify-full" if config.get("ssl_ca_cert")
            else "require" if config.get("ssl", True)
            else "disable"
        ),
    }
    if config.get("ssl_ca_cert"):
        kwargs["sslrootcert"] = config["ssl_ca_cert"]
    # Fix #1: Pass TCP keepalive settings so long-running CDC / export
    # connections to Azure (or any cloud PG) don't drop silently.
    if config.get("keepalives_idle") is not None:
        kwargs["keepalives"] = 1
        kwargs["keepalives_idle"] = int(config["keepalives_idle"])
        kwargs["keepalives_interval"] = int(config.get("keepalives_interval", 10))
        kwargs["keepalives_count"] = int(config.get("keepalives_count", 5))
    return kwargs


# ---------------------------------------------------------------------------
# Schema scope — STEP 3B
# ---------------------------------------------------------------------------

# Default schema scope: backward-compatible with every test/config/migration
# that existed before the schema-scope feature. Callers that omit
# include_schemas get exactly the old `public`-only behavior.
DEFAULT_INCLUDE_SCHEMAS: tuple[str, ...] = ("public",)

# PostgreSQL internal schemas that must NEVER be enumerated as user
# metadata, regardless of what the operator puts in `include_schemas`.
# Mirrors the existing exclusion list in list_schemas().
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog",
    "information_schema",
    "pg_toast",
})


def _resolve_include_schemas(config: dict[str, Any]) -> tuple[str, ...]:
    """
    Return the normalized, system-schema-free list of schemas that
    metadata discovery should consult.

    Precedence:
      1. ``config["include_schemas"]`` — list of strings (the
         orchestrator injects this from ``migration.include_schemas``).
      2. ``DEFAULT_INCLUDE_SCHEMAS`` ("public") when absent.

    Validation:
      * Must be a non-empty list/tuple of non-empty strings.
      * PostgreSQL internal schemas (``pg_catalog``, ``information_schema``,
        ``pg_toast``, anything starting with ``pg_``) are filtered out
        defensively so an explicit include cannot accidentally enumerate
        system objects.
    """
    raw = config.get("include_schemas")
    if raw is None:
        return DEFAULT_INCLUDE_SCHEMAS
    if not isinstance(raw, (list, tuple)) or not raw:
        return DEFAULT_INCLUDE_SCHEMAS
    cleaned: list[str] = []
    for s in raw:
        if not isinstance(s, str) or not s:
            continue
        if s in _SYSTEM_SCHEMAS or s.startswith("pg_"):
            continue
        cleaned.append(s)
    if not cleaned:
        return DEFAULT_INCLUDE_SCHEMAS
    return tuple(cleaned)


# ---------------------------------------------------------------------------
# Source Connector
# ---------------------------------------------------------------------------

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
        with self._conn.cursor() as cur:
            # Exclude partition child tables (relispartition=true) so only the
            # partition parent + regular tables are returned here.
            cur.execute(
                "SELECT t.table_name "
                "FROM information_schema.tables t "
                "LEFT JOIN pg_class c "
                "  ON c.relname = t.table_name "
                "  AND c.relnamespace = ANY(SELECT oid FROM pg_namespace WHERE nspname = ANY(%s)) "
                "WHERE t.table_schema = ANY(%s) "
                "  AND t.table_type = 'BASE TABLE' "
                "  AND (c.relispartition IS NULL OR c.relispartition = false) "
                "ORDER BY t.table_name",
                (list(self._include_schemas), list(self._include_schemas)),
            )
            tables = [row[0] for row in cur.fetchall()]
        for t in tables:
            validate_identifier(t, "table")
        return tables

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        table_name = f"{schema_name}.{object_name}" if schema_name else object_name
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table_name}")
            return cur.fetchone()[0]

    @staticmethod
    def _coerce_value(v: Any) -> Any:
        """Coerce psycopg3 types that don't pass cleanly through upsert_batch."""
        import decimal
        if v is None:
            return None
        if isinstance(v, memoryview):
            return bytes(v)          # bytea → bytes (psycopg3 can INSERT bytes as bytea)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        if isinstance(v, decimal.Decimal):
            return float(v)          # numeric / money coercion
        if isinstance(v, list):
            return [PostgresSourceConnector._coerce_value(i) for i in v]  # PG arrays
        return v

    def export_full(self, object_name: str, statement_timeout_ms: int = 0, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream all rows from *object_name* using a server-side cursor.

        Fix #8: Sets lock_timeout (5 s) so the SELECT never queues behind a
        long-running DDL lock.  statement_timeout is configurable (default 0 =
        disabled) so callers can cap run-away exports.
        """
        validate_identifier(object_name, "table")
        table_name = f"{schema_name}.{object_name}" if schema_name else object_name
        with self._conn.cursor() as setup_cur:
            # 5-second lock timeout — fails fast rather than waiting forever.
            setup_cur.execute("SET LOCAL lock_timeout = '5s'")
            if statement_timeout_ms:
                setup_cur.execute(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")
        with self._conn.cursor(name=f"export_{object_name}") as cur:
            cur.execute(f"SELECT * FROM {table_name}")
            columns = [desc.name for desc in cur.description]
            for row in cur:
                yield {k: self._coerce_value(v) for k, v in zip(columns, row)}

    def get_schema(self, object_name: str) -> Schema:
        validate_identifier(object_name, "table")
        columns: list[Column] = []
        primary_key: list[str] = []
        indexes: list[Index] = []
        foreign_keys: list[ForeignKey] = []
        check_constraints: list[CheckConstraint] = []
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

            # --- Columns (with defaults + GENERATED ALWAYS detection) ---
            # Join pg_attribute to detect GENERATED ALWAYS AS (expr) STORED columns
            # (attgenerated = 's').  Also read udt_name so ARRAY columns get their
            # element type (e.g. "_int4" → "integer[]") instead of just "ARRAY".
            cur.execute(
                "SELECT "
                "  c.column_name, "
                "  pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type, "
                "  c.is_nullable, "
                "  c.character_maximum_length, "
                "  c.column_default, "
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
                col_name, data_type, nullable, max_len, col_default, gen_expr = row
                is_seq = col_default is not None and "nextval" in str(col_default)
                if is_seq:
                    sequences.append(col_name)
                columns.append(Column(
                    name=col_name,
                    source_type=data_type,
                    target_type=None,
                    nullable=(nullable == "YES"),
                    size=max_len,
                    # Keep nextval() default so the column wires to its sequence on target;
                    # strip it only for generated columns (they use GENERATED ALWAYS syntax)
                    default=None if gen_expr else col_default,
                    generated=gen_expr,   # non-None → GENERATED ALWAYS AS (expr) STORED
                ))

            # --- Primary Key ---
            cur.execute(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY' "
                "ORDER BY kcu.ordinal_position",
                (object_name,),
            )
            primary_key = [row[0] for row in cur.fetchall()]

            # --- Indexes (full DDL via pg_get_indexdef — handles partial & expression) ---
            cur.execute(
                "SELECT i.relname, ix.indisunique, "
                "array_agg(a.attname ORDER BY a.attnum), "
                "pg_get_indexdef(i.oid) "
                "FROM pg_class t "
                "JOIN pg_index ix ON t.oid = ix.indrelid "
                "JOIN pg_class i ON i.oid = ix.indexrelid "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey) "
                "WHERE t.relname = %s AND NOT ix.indisprimary "
                "GROUP BY i.relname, ix.indisunique, i.oid",
                (object_name,),
            )
            for row in cur.fetchall():
                idx_name, is_unique, idx_cols, idx_ddl = row
                indexes.append(Index(
                    name=idx_name,
                    columns=list(idx_cols),
                    unique=bool(is_unique),
                    ddl=idx_ddl,
                ))

            # --- Foreign Keys ---
            cur.execute(
                "SELECT tc.constraint_name, kcu.column_name, "
                "ccu.table_schema AS ref_schema, ccu.table_name AS ref_table, ccu.column_name AS ref_col, "
                "rc.delete_rule, rc.update_rule "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "JOIN information_schema.referential_constraints rc "
                "  ON tc.constraint_name = rc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON rc.unique_constraint_name = ccu.constraint_name "
                "WHERE tc.table_name = %s AND tc.constraint_type = 'FOREIGN KEY'",
                (object_name,),
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
                "WHERE tc.table_name = %s AND tc.constraint_type = 'CHECK' "
                "  AND cc.check_clause NOT LIKE '%%IS NOT NULL%%'",
                (object_name,),
            )
            for row in cur.fetchall():
                chk_name, expression = row
                check_constraints.append(CheckConstraint(name=chk_name, expression=expression))

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
            indexes=indexes,
            foreign_keys=foreign_keys,
            check_constraints=check_constraints,
            sequences=sequences,
            rls_enabled=rls_enabled,
            partition_key=partition_key,
        )

    # ------------------------------------------------------------------
    # Extensions (Phase 1)
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
    # Sequences — all sequences, standalone + column-owned (Phase 3.5)
    # ------------------------------------------------------------------

    def list_all_sequences(self) -> list["SequenceDef"]:
        """Return every sequence in the configured schemas with full metadata."""
        from core.connectors.base import SequenceDef
        results: list["SequenceDef"] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT "
                "  s.schemaname, "
                "  s.sequencename, "
                "  s.start_value, s.min_value, s.max_value, "
                "  s.increment_by, s.cycle, "
                "  s.last_value, "
                "  ("
                "    SELECT n.nspname || '.' || pc.relname || '.' || a.attname "
                "    FROM pg_class sc "
                "    JOIN pg_depend d ON d.objid = sc.oid AND d.deptype = 'a' "
                "    JOIN pg_class pc ON pc.oid = d.refobjid "
                "    JOIN pg_namespace n ON pc.relnamespace = n.oid "
                "    JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
                "    WHERE sc.relname = s.sequencename "
                "      AND sc.relnamespace = ANY(SELECT oid FROM pg_namespace WHERE nspname = ANY(%s)) "
                "    LIMIT 1 "
                "  ) AS owned_by "
                "FROM pg_sequences s "
                "WHERE s.schemaname = ANY(%s) "
                "ORDER BY s.schemaname, s.sequencename",
                (list(self._include_schemas), list(self._include_schemas)),
            )
            for row in cur.fetchall():
                seq_schema, seq_name, start, min_v, max_v, incr, cycle, last_v, owned_by = row
                results.append(SequenceDef(
                    name=seq_name,
                    start_value=int(start),
                    min_value=int(min_v),
                    max_value=int(max_v),
                    increment=int(incr),
                    cycle=bool(cycle),
                    last_value=int(last_v) if last_v is not None else None,
                    owned_by=owned_by,
                    schema=seq_schema,
                ))
        return results

    # ------------------------------------------------------------------
    # Partitions — child partition tables (Phase 4.5)
    # ------------------------------------------------------------------

    def list_partitions(self) -> list["PartitionDef"]:
        """Return every partition child in the configured schemas."""
        from core.connectors.base import PartitionDef
        results: list[PartitionDef] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT "
                "  c.relname AS partition_name, "
                "  parent.relname AS parent_table, "
                "  pg_get_expr(c.relpartbound, c.oid) AS bound_expr "
                "FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class parent ON i.inhparent = parent.oid "
                "WHERE n.nspname = ANY(%s) AND c.relispartition = true "
                "ORDER BY parent.relname, c.relname",
                (list(self._include_schemas),)
            )
            for row in cur.fetchall():
                part_name, parent_table, bound = row
                results.append(PartitionDef(
                    name=part_name,
                    parent_table=parent_table,
                    bound=bound,
                ))
        return results

    # ------------------------------------------------------------------
    # Schemas (Phase 2)
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
    # Custom Types: ENUM, DOMAIN, COMPOSITE (Phase 3)
    # ------------------------------------------------------------------

    def list_types(self) -> list[TypeDef]:
        types: list[TypeDef] = []
        with self._conn.cursor() as cur:
            schemas = list(self._include_schemas)
            # ENUMs
            cur.execute(
                "SELECT t.typname, "
                "  array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels "
                "FROM pg_type t "
                "JOIN pg_enum e ON t.oid = e.enumtypid "
                "JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE n.nspname = ANY(%s) "
                "GROUP BY t.typname ORDER BY t.typname",
                (schemas,),
            )
            for row in cur.fetchall():
                type_name, labels = row
                labels_sql = ", ".join(f"'{lbl}'" for lbl in labels)
                ddl = f"CREATE TYPE {type_name} AS ENUM ({labels_sql})"
                types.append(TypeDef(name=type_name, kind="enum", ddl=ddl))

            # DOMAINs
            cur.execute(
                "SELECT t.typname, "
                "  pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base, "
                "  pg_catalog.pg_get_expr(t.typdefaultbin, 0) AS dflt, "
                "  t.typnotnull, "
                "  (SELECT string_agg('CONSTRAINT ' || c.conname || ' CHECK (' || "
                "    pg_get_constraintdef(c.oid) || ')', ' ') "
                "   FROM pg_constraint c WHERE c.contypid = t.oid) AS checks "
                "FROM pg_type t "
                "JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE t.typtype = 'd' AND n.nspname = ANY(%s) "
                "ORDER BY t.typname",
                (schemas,),
            )
            for row in cur.fetchall():
                type_name, base, dflt, notnull, checks = row
                ddl = f"CREATE DOMAIN {type_name} AS {base}"
                if dflt:
                    ddl += f" DEFAULT {dflt}"
                if notnull:
                    ddl += " NOT NULL"
                if checks:
                    ddl += f" {checks}"
                types.append(TypeDef(name=type_name, kind="domain", ddl=ddl))

            # COMPOSITE types
            cur.execute(
                "SELECT t.typname, "
                "  string_agg(a.attname || ' ' || pg_catalog.format_type(a.atttypid, a.atttypmod), "
                "    ', ' ORDER BY a.attnum) AS cols "
                "FROM pg_type t "
                "JOIN pg_class c ON c.oid = t.typrelid "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
                "JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE t.typtype = 'c' AND n.nspname = ANY(%s) "
                "  AND c.relkind = 'c' "
                "GROUP BY t.typname ORDER BY t.typname",
                (schemas,),
            )
            for row in cur.fetchall():
                type_name, cols = row
                ddl = f"CREATE TYPE {type_name} AS ({cols})"
                types.append(TypeDef(name=type_name, kind="composite", ddl=ddl))

        return types

    # ------------------------------------------------------------------
    # Views (Phase 11)
    # ------------------------------------------------------------------

    def list_views(self) -> list[ViewDefinition]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, table_schema, view_definition "
                "FROM information_schema.views "
                "WHERE table_schema = ANY(%s) "
                "ORDER BY table_name",
                (list(self._include_schemas),),
            )
            return [ViewDefinition(name=row[0], schema_name=row[1], definition=row[2]) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Materialized Views (Phase 12)
    # ------------------------------------------------------------------

    def list_materialized_views(self) -> list[MaterializedViewDef]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT m.matviewname, m.schemaname, pg_get_viewdef(c.oid) "
                "FROM pg_matviews m "
                "JOIN pg_class c ON c.relname = m.matviewname "
                "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = m.schemaname "
                "WHERE m.schemaname = ANY(%s) "
                "ORDER BY m.matviewname",
                (list(self._include_schemas),),
            )
            return [MaterializedViewDef(name=row[0], schema_name=row[1], definition=row[2]) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Functions & Stored Procedures (Phase 13)
    # ------------------------------------------------------------------

    def list_functions(self) -> list[FunctionDef]:
        funcs: list[FunctionDef] = []
        try:
            with self._conn.cursor() as cur:
                # prokind 'f'=function 'p'=procedure 'a'=aggregate 'w'=window
                # IN ('f','p') already excludes aggregates — no need for proisagg (PG10 only)
                cur.execute(
                    "SELECT p.proname, n.nspname, pg_get_functiondef(p.oid) "
                    "FROM pg_proc p "
                    "JOIN pg_namespace n ON p.pronamespace = n.oid "
                    "WHERE n.nspname = ANY(%s) "
                    "  AND p.prokind IN ('f', 'p') "
                    "ORDER BY p.proname",
                    (list(self._include_schemas),),
                )
                for row in cur.fetchall():
                    func_name, schema_name, ddl = row
                    funcs.append(FunctionDef(name=func_name, schema_name=schema_name, ddl=ddl))
        except Exception:
            self._conn.rollback()   # keep source connection clean for subsequent phases
            raise
        return funcs

    # ------------------------------------------------------------------
    # Triggers (Phase 14)
    # ------------------------------------------------------------------

    def get_all_triggers(self) -> list[TriggerDef]:
        triggers: list[TriggerDef] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT t.tgname, c.relname, n.nspname, pg_get_triggerdef(t.oid) "
                    "FROM pg_trigger t "
                    "JOIN pg_class c ON t.tgrelid = c.oid "
                    "JOIN pg_namespace n ON c.relnamespace = n.oid "
                    "WHERE n.nspname = ANY(%s) AND NOT t.tgisinternal "
                    "ORDER BY c.relname, t.tgname",
                    (list(self._include_schemas),),
                )
                for row in cur.fetchall():
                    trig_name, table_name, schema_name, ddl = row
                    triggers.append(TriggerDef(name=trig_name, table=table_name, schema_name=schema_name, ddl=ddl))
        except Exception:
            self._conn.rollback()   # keep source connection clean for subsequent phases
            raise
        return triggers

    # ------------------------------------------------------------------
    # Row-Level Security (Phase 9)
    # ------------------------------------------------------------------

    def get_rls_policies(self, table: str, schema_name: str | None = None) -> list[RLSPolicy]:
        policies: list[RLSPolicy] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pol.polname, "
                "  CASE pol.polcmd "
                "    WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT' "
                "    WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE' "
                "    ELSE 'ALL' END AS cmd, "
                "  CASE pol.polpermissive WHEN true THEN 'PERMISSIVE' ELSE 'RESTRICTIVE' END, "
                "  pg_get_expr(pol.polqual, pol.polrelid) AS using_expr, "
                "  pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr, "
                "  n.nspname "
                "FROM pg_policy pol "
                "JOIN pg_class c ON c.oid = pol.polrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = ANY(%s) AND c.relname = %s",
                (list(self._include_schemas), table),
            )
            for row in cur.fetchall():
                pol_name, cmd, permissive, using_expr, check_expr, nspname = row
                policies.append(RLSPolicy(
                    name=pol_name, table=table, cmd=cmd,
                    permissive=permissive,
                    using_expr=using_expr,
                    check_expr=check_expr,
                    schema_name=nspname or schema_name or "public",
                ))
        return policies

    # ------------------------------------------------------------------
    # Comments (Phase 15)
    # ------------------------------------------------------------------

    def list_comments(self) -> list[CommentDef]:
        comments: list[CommentDef] = []
        with self._conn.cursor() as cur:
            schemas = list(self._include_schemas)
            # Table / view / matview comments
            cur.execute(
                "SELECT CASE c.relkind "
                "  WHEN 'r' THEN 'TABLE' WHEN 'v' THEN 'VIEW' "
                "  WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, "
                "  n.nspname, c.relname, d.description "
                "FROM pg_description d "
                "JOIN pg_class c ON d.objoid = c.oid "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = ANY(%s) AND d.objsubid = 0 "
                "  AND c.relkind IN ('r', 'v', 'm') "
                "ORDER BY c.relname",
                (schemas,),
            )
            for row in cur.fetchall():
                obj_type, schema_name, obj_name, comment = row
                comments.append(CommentDef(object_type=obj_type, object_name=obj_name, comment=comment, schema_name=schema_name))

            # Column comments
            cur.execute(
                "SELECT n.nspname, c.relname, a.attname, d.description "
                "FROM pg_description d "
                "JOIN pg_attribute a ON d.objoid = a.attrelid AND d.objsubid = a.attnum "
                "JOIN pg_class c ON a.attrelid = c.oid "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = ANY(%s) AND a.attnum > 0 "
                "ORDER BY c.relname, a.attnum",
                (schemas,),
            )
            for row in cur.fetchall():
                schema_name, table_name, col_name, comment = row
                comments.append(CommentDef(
                    object_type="COLUMN",
                    object_name=f"{table_name}.{col_name}",
                    comment=comment,
                    schema_name=schema_name,
                ))

            # Function comments
            cur.execute(
                "SELECT n.nspname, p.proname || '(' || "
                "  pg_get_function_arguments(p.oid) || ')', d.description "
                "FROM pg_description d "
                "JOIN pg_proc p ON d.objoid = p.oid "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "WHERE n.nspname = ANY(%s) "
                "ORDER BY p.proname",
                (schemas,),
            )
            for row in cur.fetchall():
                schema_name, func_sig, comment = row
                comments.append(CommentDef(object_type="FUNCTION", object_name=func_sig, comment=comment, schema_name=schema_name))

            # Schema comments
            cur.execute(
                "SELECT n.nspname, d.description "
                "FROM pg_description d "
                "JOIN pg_namespace n ON d.objoid = n.oid "
                "WHERE d.classoid = 'pg_namespace'::regclass "
                "  AND n.nspname = ANY(%s) "
                "ORDER BY n.nspname",
                (schemas,),
            )
            for row in cur.fetchall():
                schema_name, comment = row
                comments.append(CommentDef(object_type="SCHEMA", object_name=schema_name, comment=comment, schema_name=schema_name))

        return comments

    # ------------------------------------------------------------------
    # Grants (Phase 16)
    # ------------------------------------------------------------------

    def list_grants(self) -> list[GrantDef]:
        grants: list[GrantDef] = []
        with self._conn.cursor() as cur:
            schemas = list(self._include_schemas)
            # Table grants
            cur.execute(
                "SELECT grantee, table_schema, table_name, "
                "  string_agg(privilege_type, ', ' ORDER BY privilege_type) "
                "FROM information_schema.role_table_grants "
                "WHERE table_schema = ANY(%s) "
                "  AND grantee NOT IN ('PUBLIC') "
                "  AND grantor != grantee "
                "GROUP BY grantee, table_schema, table_name "
                "ORDER BY table_schema, table_name, grantee",
                (schemas,),
            )
            for row in cur.fetchall():
                grantee, schema_name, table_name, privs = row
                grants.append(GrantDef(
                    privileges=privs, object_type="TABLE",
                    object_name=table_name, grantee=grantee, schema_name=schema_name,
                ))

            # Column grants
            cur.execute(
                "SELECT grantee, table_schema, table_name, column_name, "
                "  string_agg(privilege_type, ', ' ORDER BY privilege_type) "
                "FROM information_schema.role_column_grants "
                "WHERE table_schema = ANY(%s) "
                "  AND grantee NOT IN ('PUBLIC') "
                "  AND grantor != grantee "
                "GROUP BY grantee, table_schema, table_name, column_name "
                "ORDER BY table_schema, table_name, column_name, grantee",
                (schemas,),
            )
            for row in cur.fetchall():
                grantee, schema_name, table_name, column_name, privs = row
                grants.append(GrantDef(
                    privileges=privs, object_type="COLUMN",
                    object_name=f"{table_name}.{column_name}", grantee=grantee, schema_name=schema_name,
                ))

            # Sequence grants
            cur.execute(
                "SELECT grantee, object_schema, object_name, "
                "  string_agg(privilege_type, ', ' ORDER BY privilege_type) "
                "FROM information_schema.role_usage_grants "
                "WHERE object_schema = ANY(%s) "
                "  AND object_type = 'SEQUENCE' "
                "  AND grantee NOT IN ('PUBLIC') "
                "GROUP BY grantee, object_schema, object_name "
                "ORDER BY object_schema, object_name, grantee",
                (schemas,),
            )
            for row in cur.fetchall():
                grantee, schema_name, seq_name, privs = row
                grants.append(GrantDef(
                    privileges=privs, object_type="SEQUENCE",
                    object_name=seq_name, grantee=grantee, schema_name=schema_name,
                ))

            # Schema grants
            cur.execute(
                "SELECT n.nspname, r.rolname AS grantee, acl.privilege_type "
                "FROM pg_namespace n "
                "JOIN aclexplode(n.nspacl) acl ON true "
                "JOIN pg_roles r ON r.oid = acl.grantee "
                "WHERE n.nspname = ANY(%s) "
                "  AND n.nspacl IS NOT NULL "
                "  AND r.rolname NOT IN ('PUBLIC') "
                "  AND acl.grantee != (SELECT oid FROM pg_roles WHERE rolname = current_user)",
                (schemas,),
            )
            for row in cur.fetchall():
                schema_name, grantee, privilege_type = row
                grants.append(GrantDef(
                    privileges=privilege_type,
                    object_type="SCHEMA",
                    object_name=schema_name, grantee=grantee, schema_name=schema_name,
                ))

            # Function grants
            cur.execute(
                "SELECT n.nspname, p.proname || '(' || "
                "  pg_get_function_arguments(p.oid) || ')', r.rolname AS grantee, acl.privilege_type "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "JOIN aclexplode(p.proacl) acl ON true "
                "JOIN pg_roles r ON r.oid = acl.grantee "
                "WHERE n.nspname = ANY(%s) "
                "  AND p.prokind IN ('f', 'p') "
                "  AND p.proacl IS NOT NULL "
                "  AND r.rolname NOT IN ('PUBLIC') "
                "  AND acl.grantee != (SELECT oid FROM pg_roles WHERE rolname = current_user)",
                (schemas,),
            )
            for row in cur.fetchall():
                schema_name, func_sig, grantee, privilege_type = row
                grants.append(GrantDef(
                    privileges=privilege_type,
                    object_type="FUNCTION",
                    object_name=func_sig, grantee=grantee, schema_name=schema_name,
                ))

        return grants


# ---------------------------------------------------------------------------
# Target Connector
# ---------------------------------------------------------------------------

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
    # Phase 1 — Extensions
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
    # Phase 2 — Schemas
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
    # Phase 3 — Custom Types
    # ------------------------------------------------------------------

    def create_type(self, type_def: TypeDef) -> None:
        with self._conn.cursor() as cur:
            try:
                # Check if type already exists
                cur.execute(
                    "SELECT 1 FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid "
                    "WHERE t.typname = %s AND n.nspname = 'public'",
                    (type_def.name,),
                )
                if cur.fetchone() is not None:
                    return
                cur.execute(type_def.ddl)
                self._conn.commit()
                audit_log(phase="create_type", status="created",
                          details={"type": type_def.name, "kind": type_def.kind})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_type", status="skipped",
                          details={"type": type_def.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 4 — Tables
    # ------------------------------------------------------------------

    def create_object_if_missing(self, schema: Schema) -> None:
        validate_identifier(schema.name, "table")
        table_schema = schema.schema_name or "public"
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = %s AND table_schema = %s",
                    (schema.name, table_schema),
                )
                if cur.fetchone() is not None:
                    return

                col_defs = []
                for col in schema.columns:
                    col_type = col.target_type or col.source_type
                    if col.generated:
                        # GENERATED ALWAYS AS (expr) STORED — do NOT include NULL/DEFAULT
                        col_defs.append(
                            f"{col.name} {col_type} GENERATED ALWAYS AS ({col.generated}) STORED"
                        )
                    else:
                        null_str = "NULL" if col.nullable else "NOT NULL"
                        default_str = f" DEFAULT {col.default}" if col.default else ""
                        col_defs.append(f"{col.name} {col_type}{default_str} {null_str}")

                if schema.primary_key:
                    pk_cols = ", ".join(schema.primary_key)
                    col_defs.append(f"PRIMARY KEY ({pk_cols})")

                if table_schema == "public":
                    ddl = f"CREATE TABLE {schema.name} ({', '.join(col_defs)})"
                else:
                    ddl = f"CREATE TABLE {quote_identifier(table_schema)}.{quote_identifier(schema.name)} ({', '.join(col_defs)})"
                if schema.partition_key:
                    ddl += f" PARTITION BY {schema.partition_key}"
                cur.execute(ddl)
                self._conn.commit()
                audit_log(phase="create_table", status="created", details={"table": schema.name})
            except Exception:
                # Always roll back so a single bad DDL does not poison the
                # connection for subsequent tables / phases.
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------------
    # Phase 3.5 — Sequences (create before tables that reference them)
    # ------------------------------------------------------------------

    def create_sequence(self, seq: "SequenceDef") -> None:
        """Create a sequence with the exact same properties as on the source.

        Phase 3.5 runs BEFORE tables are created (Phase 4), so we
        intentionally do NOT apply ``ALTER SEQUENCE ... OWNED BY`` here:
        PostgreSQL rejects ``OWNED BY`` when the referenced table does
        not yet exist, and the orchestrator would otherwise see every
        sequence creation fail and lose them.  Ownership is reattached
        later by ``apply_constraints`` / ``advance_sequence`` once the
        owning table exists.
        """
        from core.connectors.base import SequenceDef  # noqa: F401
        seq_schema = seq.schema or "public"
        seq_qname = (
            seq.name if seq_schema == "public"
            else f"{quote_identifier(seq_schema)}.{quote_identifier(seq.name)}"
        )
        with self._conn.cursor() as cur:
            try:
                cycle_clause = "CYCLE" if seq.cycle else "NO CYCLE"
                cur.execute(
                    f"CREATE SEQUENCE IF NOT EXISTS {seq_qname} "
                    f"START WITH {seq.start_value} "
                    f"INCREMENT BY {seq.increment} "
                    f"MINVALUE {seq.min_value} "
                    f"MAXVALUE {seq.max_value} "
                    f"{cycle_clause}"
                )
                self._conn.commit()
                audit_log(phase="create_sequence", status="created",
                          details={"sequence": seq_qname, "owned_by": seq.owned_by})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_sequence", status="skipped",
                          details={"sequence": seq_qname, "reason": str(exc)})

    def advance_sequence(self, seq_name: str, table: str, column: str, owned_by: str | None = None) -> None:
        """After data load: advance sequence to max(column) so next INSERT gets the right value.

        Fix #5: sequence name is passed via a %s placeholder (psycopg casts it
        to regclass automatically), which safely handles quoted names like
        'public."Order-id_seq"' without raw f-string injection.

        Fix #9: For non-public schemas, the table may be in a different schema
        than the sequence. Extract the schema from owned_by (schema.table.column)
        to qualify the table reference in the MAX() query.
        """
        validate_identifier(column, "column")
        table_qname = table
        if owned_by:
            parts = owned_by.rsplit(".", 2)
            if len(parts) == 3:
                table_schema, table_name_from_owned, _ = parts
                if table_name_from_owned == table:
                    table_qname = f"{quote_identifier(table_schema)}.{quote_identifier(table)}"
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    f"SELECT setval(%s::regclass, "
                    f"COALESCE((SELECT MAX({column}) FROM {table_qname}), 0) + 1, false)",
                    (seq_name,),
                )
                self._conn.commit()
                audit_log(phase="advance_sequence", status="advanced",
                          details={"sequence": seq_name, "table": table, "column": column})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="advance_sequence", status="skipped",
                          details={"sequence": seq_name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 4.5 — Partition children
    # ------------------------------------------------------------------

    def create_partition(self, partition: "PartitionDef") -> None:
        """Create a child partition table (PARTITION OF parent ...)."""
        from core.connectors.base import PartitionDef  # noqa: F401
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM pg_class c "
                    "JOIN pg_namespace n ON c.relnamespace = n.oid "
                    "WHERE c.relname = %s AND n.nspname = 'public'",
                    (partition.name,),
                )
                if cur.fetchone() is not None:
                    return   # already exists
                cur.execute(
                    f"CREATE TABLE {partition.name} "
                    f"PARTITION OF {partition.parent_table} "
                    f"{partition.bound}"
                )
                self._conn.commit()
                audit_log(phase="create_partition", status="created",
                          details={"partition": partition.name, "parent": partition.parent_table})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_partition", status="skipped",
                          details={"partition": partition.name, "reason": str(exc)})

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        validate_identifier(object_name, "table")
        result = UpsertResult()
        batch = list(rows)
        if not batch:
            return result

        # Strip GENERATED ALWAYS columns — PostgreSQL rejects explicit inserts into them
        if schema:
            generated_cols = {col.name for col in schema.columns if col.generated}
            if generated_cols:
                batch = [{k: v for k, v in row.items() if k not in generated_cols} for row in batch]

        all_columns: list[str] = []
        seen: set[str] = set()
        for row in batch:
            for col in row:
                if col not in seen:
                    all_columns.append(col)
                    seen.add(col)

        col_names = ", ".join(all_columns)
        placeholders = ", ".join("%s" for _ in all_columns)
        update_set = ", ".join(f"{col} = EXCLUDED.{col}" for col in all_columns)

        pk_cols = schema.primary_key if schema else []
        if pk_cols:
            pk_clause = ", ".join(pk_cols)
            conflict_clause = f"ON CONFLICT ({pk_clause}) DO UPDATE"
        else:
            conflict_clause = "ON CONFLICT DO NOTHING"

        table_name = (
            object_name if not schema or not schema.schema_name or schema.schema_name == "public"
            else f"{quote_identifier(schema.schema_name)}.{quote_identifier(object_name)}"
        )
        sql = (
            f"INSERT INTO {table_name} ({col_names}) "
            f"VALUES ({placeholders}) "
            f"{conflict_clause} SET {update_set}"
            if pk_cols else
            f"INSERT INTO {table_name} ({col_names}) "
            f"VALUES ({placeholders}) "
            f"{conflict_clause}"
        )

        values_list = [[row.get(col) for col in all_columns] for row in batch]

        with self._conn.cursor() as cur:
            try:
                cur.executemany(sql, values_list)
                self._conn.commit()
                result.success_count = len(batch)
                audit_log(phase="upsert_batch", status="success",
                          details={"table": object_name, "count": len(batch)})
            except Exception as exc:
                self._conn.rollback()
                result.failure_count = len(batch)
                result.errors.append(str(exc))
                result.failed_items.extend(batch)
                audit_log(phase="upsert_batch", status="failure",
                          details={"table": object_name, "error": str(exc)})
        return result

    # ------------------------------------------------------------------
    # Phase 6 + 7 + 8 — Indexes, FKs, Check Constraints
    # ------------------------------------------------------------------

    def apply_constraints(self, schema: Schema) -> None:
        validate_identifier(schema.name, "table")
        table_qname = (
            schema.name if not schema.schema_name or schema.schema_name == "public"
            else f"{quote_identifier(schema.schema_name)}.{quote_identifier(schema.name)}"
        )
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
                ref_table_qname = (
                    fk.ref_table if fk.ref_schema == "public"
                    else f"{quote_identifier(fk.ref_schema)}.{quote_identifier(fk.ref_table)}"
                )
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
    # Phase 9 — Row-Level Security
    # ------------------------------------------------------------------

    def apply_rls_policy(self, policy: RLSPolicy) -> None:
        validate_identifier(policy.table, "table")
        table_qname = (
            policy.table if policy.schema_name == "public"
            else f"{quote_identifier(policy.schema_name)}.{quote_identifier(policy.table)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"ALTER TABLE {table_qname} ENABLE ROW LEVEL SECURITY")
                self._conn.commit()
            except Exception:
                self._conn.rollback()

            try:
                using_clause = f" USING ({policy.using_expr})" if policy.using_expr else ""
                check_clause = f" WITH CHECK ({policy.check_expr})" if policy.check_expr else ""
                cur.execute(
                    f"CREATE POLICY {policy.name} ON {table_qname} "
                    f"AS {policy.permissive} FOR {policy.cmd}"
                    f"{using_clause}{check_clause}"
                )
                self._conn.commit()
                audit_log(phase="create_rls_policy", status="created",
                          details={"table": table_qname, "policy": policy.name})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_rls_policy", status="skipped",
                          details={"policy": policy.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 10 — Sequences
    # ------------------------------------------------------------------

    def sync_sequence(self, table: str, column: str, schema_name: str | None = None) -> None:
        validate_identifier(table, "table")
        validate_identifier(column, "column")
        schema = schema_name or "public"
        table_qname = (
            table if schema == "public"
            else f"{quote_identifier(schema)}.{quote_identifier(table)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    f"SELECT setval("
                    f"  pg_get_serial_sequence(%s, %s), "
                    f"  COALESCE((SELECT MAX({column}) FROM {table_qname}), 1)"
                    f")",
                    (table_qname, column),
                )
                self._conn.commit()
                audit_log(phase="sync_sequence", status="synced",
                          details={"table": table_qname, "column": column})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="sync_sequence", status="skipped",
                          details={"table": table_qname, "column": column, "reason": str(exc)})

    def apply_sequence_ownership(self, seq: "SequenceDef") -> None:
        """Apply ALTER SEQUENCE ... OWNED BY after the owning table/column exists."""
        if not seq.owned_by:
            return
        parts = seq.owned_by.split(".")
        if len(parts) not in (2, 3):
            return
        seq_schema = seq.schema or "public"
        if len(parts) == 3:
            owned_schema, table_name, column_name = parts
            if owned_schema != seq_schema:
                return
        else:
            table_name, column_name = parts
            if seq_schema != "public":
                return
        seq_qname = (
            seq.name if seq_schema == "public"
            else f"{quote_identifier(seq_schema)}.{quote_identifier(seq.name)}"
        )
        table_qname = (
            table_name if seq_schema == "public"
            else f"{quote_identifier(seq_schema)}.{quote_identifier(table_name)}"
        )
        column_qname = quote_identifier(column_name)
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    f"ALTER SEQUENCE {seq_qname} OWNED BY {table_qname}.{column_qname}"
                )
                self._conn.commit()
                audit_log(phase="apply_sequence_ownership", status="owned",
                          details={"sequence": seq.name, "owned_by": f"{table_qname}.{column_qname}"})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="apply_sequence_ownership", status="skipped",
                          details={"sequence": seq.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 11 — Views
    # ------------------------------------------------------------------

    def create_view(self, view: ViewDefinition) -> None:
        validate_identifier(view.name, "view")
        view_name = (
            view.name if view.schema_name == "public"
            else f"{quote_identifier(view.schema_name)}.{quote_identifier(view.name)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"CREATE OR REPLACE VIEW {view_name} AS {view.definition}")
                self._conn.commit()
                audit_log(phase="create_view", status="created", details={"view": view.name})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_view", status="failed",
                          details={"view": view.name, "reason": str(exc)})
                raise

    # ------------------------------------------------------------------
    # Phase 12 — Materialized Views
    # ------------------------------------------------------------------

    def create_materialized_view(self, mv: MaterializedViewDef) -> None:
        validate_identifier(mv.name, "materialized view")
        mv_schema = mv.schema_name or "public"
        mv_qname = (
            mv.name if mv_schema == "public"
            else f"{quote_identifier(mv_schema)}.{quote_identifier(mv.name)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM pg_matviews WHERE matviewname = %s AND schemaname = %s",
                    (mv.name, mv_schema),
                )
                if cur.fetchone() is not None:
                    return
                # pg_get_viewdef may include a trailing semicolon — strip it before
                # appending WITH NO DATA (which must be the last clause)
                clean_def = mv.definition.rstrip().rstrip(";")
                cur.execute(
                    f"CREATE MATERIALIZED VIEW {mv_qname} AS {clean_def} WITH NO DATA"
                )
                self._conn.commit()
                audit_log(phase="create_matview", status="created", details={"matview": mv_qname})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_matview", status="failed",
                          details={"matview": mv_qname, "reason": str(exc)})
                raise

    def refresh_materialized_view(self, name: str, schema_name: str | None = None) -> None:
        validate_identifier(name, "materialized view")
        mv_qname = (
            name if not schema_name or schema_name == "public"
            else f"{quote_identifier(schema_name)}.{quote_identifier(name)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"REFRESH MATERIALIZED VIEW {mv_qname}")
                self._conn.commit()
                audit_log(phase="refresh_matview", status="refreshed", details={"matview": mv_qname})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="refresh_matview", status="failed",
                          details={"matview": mv_qname, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 13 — Functions & Stored Procedures
    # ------------------------------------------------------------------

    def create_function(self, func: FunctionDef) -> None:
        validate_identifier(func.name, "function")
        func_qname = (
            func.name if func.schema_name == "public"
            else f"{quote_identifier(func.schema_name)}.{quote_identifier(func.name)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(func.ddl)
                self._conn.commit()
                audit_log(phase="create_function", status="created", details={"function": func_qname})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_function", status="skipped",
                          details={"function": func_qname, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 14 — Triggers
    # ------------------------------------------------------------------

    def create_trigger(self, trigger: TriggerDef) -> None:
        validate_identifier(trigger.name, "trigger")
        trigger_schema = trigger.schema_name or "public"
        table_qname = (
            trigger.table if trigger_schema == "public"
            else f"{quote_identifier(trigger_schema)}.{quote_identifier(trigger.table)}"
        )
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    f"DROP TRIGGER IF EXISTS {quote_identifier(trigger.name)} ON {table_qname}"
                )
                self._conn.commit()
                cur.execute(trigger.ddl)
                self._conn.commit()
                audit_log(phase="create_trigger", status="created",
                          details={"trigger": trigger.name, "table": table_qname})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="create_trigger", status="skipped",
                          details={"trigger": trigger.name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 15 — Comments
    # ------------------------------------------------------------------

    def apply_comment(self, comment: CommentDef) -> None:
        with self._conn.cursor() as cur:
            try:
                escaped = comment.comment.replace("'", "''")
                if comment.object_type == "SCHEMA":
                    qualified_name = quote_identifier(comment.schema_name)
                elif comment.schema_name == "public":
                    qualified_name = comment.object_name
                elif comment.object_type == "COLUMN":
                    parts = comment.object_name.split(".")
                    qualified_name = (
                        f"{quote_identifier(comment.schema_name)}.{quote_identifier(parts[0])}"
                        f".{quote_identifier(parts[1])}"
                    )
                elif comment.object_type == "FUNCTION":
                    qualified_name = f"{quote_identifier(comment.schema_name)}.{comment.object_name}"
                else:
                    qualified_name = f"{quote_identifier(comment.schema_name)}.{quote_identifier(comment.object_name)}"
                cur.execute(
                    f"COMMENT ON {comment.object_type} {qualified_name} IS '{escaped}'"
                )
                self._conn.commit()
                audit_log(phase="apply_comment", status="applied",
                          details={"object": qualified_name})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="apply_comment", status="skipped",
                          details={"object": comment.object_name, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Phase 16 — Grants
    # ------------------------------------------------------------------

    def apply_grant(self, grant: GrantDef) -> None:
        with self._conn.cursor() as cur:
            try:
                if grant.object_type == "SCHEMA":
                    qualified_name = quote_identifier(grant.schema_name)
                elif grant.object_type == "COLUMN":
                    parts = grant.object_name.split(".")
                    qualified_name = f"{quote_identifier(parts[0])}.{quote_identifier(parts[1])}"
                elif grant.object_type == "FUNCTION":
                    qualified_name = f"{quote_identifier(grant.schema_name)}.{grant.object_name}"
                elif grant.schema_name == "public":
                    qualified_name = grant.object_name
                else:
                    qualified_name = f"{quote_identifier(grant.schema_name)}.{quote_identifier(grant.object_name)}"
                cur.execute(
                    f"GRANT {grant.privileges} ON {grant.object_type} "
                    f"{qualified_name} TO {grant.grantee}"
                )
                self._conn.commit()
                audit_log(phase="apply_grant", status="applied",
                          details={"object": qualified_name, "grantee": grant.grantee})
            except Exception as exc:
                self._conn.rollback()
                audit_log(phase="apply_grant", status="skipped",
                          details={"object": grant.object_name, "grantee": grant.grantee, "reason": str(exc)})

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        table_name = f"{schema_name}.{object_name}" if schema_name else object_name
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table_name}")
            return cur.fetchone()[0]

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        validate_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            if schema and schema.primary_key:
                conditions = [f"{pk_col} = %s" for pk_col in schema.primary_key]
                values = [document.get(pk_col) for pk_col in schema.primary_key]
                where_clause = " AND ".join(conditions)
                cur.execute(f"DELETE FROM {object_name} WHERE {where_clause}", values)
            else:
                cur.execute(f"DELETE FROM {object_name} WHERE id = %s", (document.get("id"),))
            self._conn.commit()
            audit_log(phase="cdc_delete", status="deleted", details={"table": object_name})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        table_name = f"{schema_name}.{object_name}" if schema_name else object_name
        with self._conn.cursor(name=f"export_target_{object_name}") as cur:
            cur.execute(f"SELECT * FROM {table_name}")
            columns = [desc.name for desc in cur.description]
            for row in cur:
                yield dict(zip(columns, row))


# ---------------------------------------------------------------------------
# CDC Engine — uses pgoutput (built-in to PostgreSQL 10+, no installation needed)
# ---------------------------------------------------------------------------

class PostgresCDCEngine(CDCEngine):
    """
    Logical replication CDC using pgoutput — the built-in output plugin.

    Flow:
      1. CREATE PUBLICATION for all tables on source
      2. CREATE REPLICATION SLOT using pgoutput
      3. Poll using pg_logical_slot_get_binary_changes (raw pgoutput messages)
      4. Decode INSERT / UPDATE / DELETE messages
      5. Apply to target via upsert / delete
      6. Advance slot LSN checkpoint
    """

    _PUBLICATION = "migration_pub"

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._source_conn: Any = None   # regular connection (for setup + polling)
        self._slot_name = config.get("slot_name", "migration_slot")
        self._last_lsn: str | None = None
        self._relations: dict[int, dict[str, Any]] = {}  # oid → relation metadata

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("psycopg")
        import psycopg
        kwargs = _make_conn_kwargs(self._config)
        self._source_conn = psycopg.connect(**kwargs)
        self._source_conn.autocommit = True

    def start(self) -> None:
        """Create publication and replication slot (idempotent)."""
        with self._source_conn.cursor() as cur:
            # Create publication if not exists
            cur.execute(
                "SELECT 1 FROM pg_publication WHERE pubname = %s",
                (self._PUBLICATION,),
            )
            if cur.fetchone() is None:
                cur.execute(f"CREATE PUBLICATION {self._PUBLICATION} FOR ALL TABLES")
                audit_log(phase="cdc_start", status="publication_created",
                          details={"publication": self._PUBLICATION})

            # Create replication slot if not exists — uses pgoutput (built-in)
            cur.execute(
                "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s",
                (self._slot_name,),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT pg_create_logical_replication_slot(%s, 'pgoutput')",
                    (self._slot_name,),
                )
                audit_log(phase="cdc_start", status="slot_created",
                          details={"slot": self._slot_name, "plugin": "pgoutput"})
            else:
                audit_log(phase="cdc_start", status="slot_reused",
                          details={"slot": self._slot_name})

    def poll_changes(self) -> list[ChangeEvent]:
        """
        Read binary messages from the pgoutput slot and decode them.
        Uses pg_logical_slot_get_changes with pgoutput options.
        Returns decoded ChangeEvents.
        """
        events: list[ChangeEvent] = []
        commit_lsn: str | None = None   # LSN of the Commit record — needed for advance

        with self._source_conn.cursor() as cur:
            cur.execute(
                # peek (not get) so the slot LSN is NOT auto-advanced here;
                # we advance it explicitly in checkpoint() after successful apply.
                # This gives at-least-once delivery: if we crash before checkpoint,
                # changes will be re-delivered on the next run.
                "SELECT lsn, data FROM pg_logical_slot_peek_binary_changes("
                "  %s, NULL, NULL,"
                "  'proto_version', '1',"
                "  'publication_names', %s"
                ")",
                (self._slot_name, self._PUBLICATION),
            )
            rows = cur.fetchall()

        for lsn, data in rows:
            if not data:
                continue
            msg_type = chr(data[0])

            if msg_type == 'C':
                # Commit message — save its LSN for checkpointing.
                # We must advance to the Commit LSN (not individual change LSNs)
                # so that the slot moves past the entire committed transaction.
                commit_lsn = str(lsn)

            elif msg_type == 'R':
                self._decode_relation(data)

            elif msg_type == 'I':
                event = self._decode_tuple_change("insert", data, lsn)
                if event:
                    events.append(event)

            elif msg_type == 'U':
                event = self._decode_tuple_change("update", data, lsn)
                if event:
                    events.append(event)

            elif msg_type == 'D':
                event = self._decode_delete(data, lsn)
                if event:
                    events.append(event)

            # B=Begin, O=Origin, T=Truncate — skip

        # Override every event's watermark with the Commit LSN so that
        # checkpoint() advances the slot past the complete transaction,
        # preventing the same changes from being re-delivered next poll.
        if commit_lsn and events:
            for event in events:
                event.watermark = commit_lsn

        return events

    # ------------------------------------------------------------------ helpers

    def _decode_relation(self, data: bytes) -> None:
        """Parse a Relation (R) message and cache column + PK info keyed by OID."""
        import struct
        pos = 1
        rel_oid = struct.unpack_from(">I", data, pos)[0]; pos += 4
        # namespace
        ns_end = data.index(b'\x00', pos); ns = data[pos:ns_end].decode(); pos = ns_end + 1
        # table name
        tbl_end = data.index(b'\x00', pos); tbl = data[pos:tbl_end].decode(); pos = tbl_end + 1
        pos += 1  # replica identity byte (ignored here — we use column flags instead)
        col_count = struct.unpack_from(">H", data, pos)[0]; pos += 2
        columns: list[str] = []
        pk_columns: list[str] = []
        for _ in range(col_count):
            # flag byte: 0x01 = column is part of the replica identity (usually the PK)
            flags = data[pos]; pos += 1
            col_end = data.index(b'\x00', pos); col = data[pos:col_end].decode(); pos = col_end + 1
            pos += 4  # type OID
            pos += 4  # type modifier
            columns.append(col)
            if flags & 0x01:   # replica-identity column → treat as primary key
                pk_columns.append(col)
        self._relations[rel_oid] = {
            "schema": ns,
            "table": tbl,
            "columns": columns,
            "pk_columns": pk_columns,   # used to build Schema for upsert ON CONFLICT
        }

    def _decode_tuple_change(self, operation: str, data: bytes, lsn: Any) -> ChangeEvent | None:
        """Parse INSERT (I) or UPDATE (U) message into a ChangeEvent with PK schema."""
        import struct
        pos = 1
        rel_oid = struct.unpack_from(">I", data, pos)[0]; pos += 4
        if operation == "update":
            # May have an 'O' (old tuple) or 'K' (key) before new tuple
            if chr(data[pos]) in ('O', 'K'):
                pos = self._skip_tuple(data, pos)
        if chr(data[pos]) != 'N':
            return None  # no new tuple
        pos += 1
        rel = self._relations.get(rel_oid)
        if rel is None:
            return None
        doc, pos = self._decode_tuple(data, pos, rel["columns"])
        table_name = rel["table"]
        # Build a minimal Schema so upsert_batch uses ON CONFLICT (pk) DO UPDATE
        # instead of ON CONFLICT DO NOTHING (which silently drops UPDATEs)
        event_schema = Schema(
            name=table_name,
            primary_key=rel.get("pk_columns", []),
        )
        return ChangeEvent(
            operation=operation,
            document=doc,
            object_name=table_name,
            schema=event_schema,
            watermark=str(lsn),
        )

    def _decode_delete(self, data: bytes, lsn: Any) -> ChangeEvent | None:
        """Parse DELETE (D) message into a ChangeEvent with PK schema."""
        import struct
        pos = 1
        rel_oid = struct.unpack_from(">I", data, pos)[0]; pos += 4
        chr(data[pos]); pos += 1  # 'K' = key, 'O' = old tuple
        rel = self._relations.get(rel_oid)
        if rel is None:
            return None
        doc, _ = self._decode_tuple(data, pos, rel["columns"])
        event_schema = Schema(
            name=rel["table"],
            primary_key=rel.get("pk_columns", []),
        )
        return ChangeEvent(
            operation="delete",
            document=doc,
            object_name=rel["table"],
            schema=event_schema,
            watermark=str(lsn),
        )

    def _decode_tuple(self, data: bytes, pos: int, columns: list[str]) -> tuple[dict, int]:
        """Decode a TupleData block into a dict. Returns (doc, next_pos)."""
        import struct
        col_count = struct.unpack_from(">H", data, pos)[0]; pos += 2
        doc: dict[str, Any] = {}
        for i, col_name in enumerate(columns[:col_count]):
            kind = chr(data[pos]); pos += 1
            if kind == 'n':    # NULL
                doc[col_name] = None
            elif kind == 'u':  # unchanged toast
                pass
            elif kind == 't':  # text
                length = struct.unpack_from(">I", data, pos)[0]; pos += 4
                doc[col_name] = data[pos:pos + length].decode("utf-8", errors="replace")
                pos += length
            elif kind == 'b':  # binary
                length = struct.unpack_from(">I", data, pos)[0]; pos += 4
                doc[col_name] = data[pos:pos + length]
                pos += length
        return doc, pos

    def _skip_tuple(self, data: bytes, pos: int) -> int:
        """Skip past a TupleData block (old key tuple in UPDATE/DELETE)."""
        import struct
        pos += 1  # skip 'O' or 'K' marker
        col_count = struct.unpack_from(">H", data, pos)[0]; pos += 2
        for _ in range(col_count):
            kind = chr(data[pos]); pos += 1
            if kind == 't' or kind == 'b':
                length = struct.unpack_from(">I", data, pos)[0]; pos += 4
                pos += length
        return pos

    # ------------------------------------------------------------------ apply / checkpoint

    def apply(self, events: list[ChangeEvent], target: TargetConnector) -> ApplyResult:
        result = ApplyResult()
        if not events:
            return result
        for event in events:
            try:
                if event.operation in ("insert", "update"):
                    target.upsert_batch(event.object_name, iter([event.document]), event.schema)
                elif event.operation == "delete":
                    target.delete(event.object_name, event.document, event.schema)
                result.success_count += 1
            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0:
            result.last_checkpoint = events[-1].watermark
            audit_log(phase="cdc_apply", status="success",
                      details={"applied": result.success_count})
        else:
            audit_log(phase="cdc_apply", status="partial_failure",
                      details={"success": result.success_count, "failure": result.failure_count})
        return result

    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return
        with self._source_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_replication_slot_advance(%s, %s::pg_lsn)",
                (self._slot_name, str(result.last_checkpoint)),
            )
        self._last_lsn = str(result.last_checkpoint)
        audit_log(phase="cdc_checkpoint", status="advanced",
                  details={"lsn": self._last_lsn})

    def cleanup(self) -> None:
        """Fix #7: Drop the replication slot after migration completes (or on failure).

        Orphaned replication slots block WAL recycling and can fill the source
        disk.  This should be called in a finally block by the orchestrator.
        The slot is only dropped if it still exists — safe to call multiple times.
        """
        if self._source_conn is None:
            return
        try:
            with self._source_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s",
                    (self._slot_name,),
                )
                if cur.fetchone() is not None:
                    cur.execute(
                        "SELECT pg_drop_replication_slot(%s)",
                        (self._slot_name,),
                    )
                    audit_log(phase="cdc_cleanup", status="slot_dropped",
                              details={"slot": self._slot_name})
        except Exception as exc:
            audit_log(phase="cdc_cleanup", status="slot_drop_failed",
                      details={"slot": self._slot_name, "reason": str(exc)})