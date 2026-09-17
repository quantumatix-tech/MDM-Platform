from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    Index,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    UnmappedTypeError,
    ViewDefinition,
    FunctionDef,
    SynonymDef,
    TypeDef,
    validate_identifier,
    quote_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


_MSSQL_SYSTEM_SCHEMAS = frozenset({"sys", "INFORMATION_SCHEMA", "guest"})


# ---------------------------------------------------------------------------
# Step 12 — Partitioning dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PartitionFunctionDef:
    """MSSQL partition function metadata."""
    name: str
    schema_name: str = "dbo"
    data_type: str = "datetime2"
    boundaries: list[Any] = field(default_factory=list)
    range_desc: str = "RANGE RIGHT"


@dataclass
class PartitionSchemeDef:
    """MSSQL partition scheme metadata."""
    name: str
    schema_name: str = "dbo"
    partition_function_name: str = ""
    filegroups: list[str] = field(default_factory=list)


@dataclass
class PartitionedTableDef:
    """MSSQL partitioned table/index metadata."""
    table_name: str
    schema_name: str = "dbo"
    index_name: str | None = None
    partition_function_name: str = ""
    partition_column: str = ""


def _resolve_mssql_schemas(config: dict[str, Any]) -> tuple[str, ...] | None:
    raw = config.get("include_schemas")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    cleaned = [
        s for s in raw
        if isinstance(s, str) and s and s not in _MSSQL_SYSTEM_SCHEMAS
    ]
    return tuple(cleaned) if cleaned else None


def _qualify(schema_name: str | None, object_name: str) -> str:
    schema = schema_name or "dbo"
    return f"{quote_identifier(schema)}.{quote_identifier(object_name)}"


def _build_mssql_index_ddl(
    idx_name: str,
    is_unique: bool,
    schema_name: str | None,
    table_name: str,
    key_cols: list[tuple[str, bool]],   # (column_name, is_descending)
    included_cols: list[str] = None,
    filter_def: str | None = None,
) -> str:
    """Build a complete CREATE [UNIQUE] INDEX DDL for MSSQL."""
    schema = schema_name or "dbo"
    schema_q = quote_identifier(schema)
    table_q = quote_identifier(table_name)
    unique_str = "UNIQUE " if is_unique else ""
    cols = ", ".join(
        f"{quote_identifier(col)} {'DESC' if desc else 'ASC'}"
        for col, desc in key_cols
    )
    ddl = f"CREATE {unique_str}INDEX {quote_identifier(idx_name)} ON {schema_q}.{table_q}({cols})"
    if included_cols:
        inc = ", ".join(quote_identifier(c) for c in included_cols)
        ddl += f" INCLUDE ({inc})"
    if filter_def:
        ddl += f" WHERE {filter_def}"
    return ddl


_MSSQL_SIZE_TYPES = frozenset({
    "nvarchar", "varchar", "char", "nchar", "varbinary", "binary",
})
_MSSQL_PRECISION_TYPES = frozenset({"decimal", "numeric"})


def _mssql_column_type(
    base_type: str,
    size: Any,
    precision: int | None = None,
    scale: int | None = None,
) -> str:
    """Re-attach length/precision to MSSQL types.

    INFORMATION_SCHEMA.COLUMNS reports only the base DATA_TYPE (e.g.
    ``nvarchar``). Emitting it bare makes SQL Server default to
    ``nvarchar(1)`` (truncating data) or ``decimal`` -> ``decimal(18,0)``
    (rounding away fractional values). Re-attach length/precision so the
    target DDL matches the source (e.g. ``nvarchar(100)``, ``decimal(10,2)``).
    """
    if not base_type or "(" in base_type:
        return base_type
    bt = base_type.lower()
    if bt in _MSSQL_PRECISION_TYPES and precision is not None and scale is not None:
        return f"{base_type}({precision},{scale})"
    if bt in _MSSQL_SIZE_TYPES:
        if size == -1:
            return f"{base_type}(MAX)"
        if size is not None:
            return f"{base_type}({size})"
    return base_type


def _non_computed_column_names(conn: Any, object_name: str, schema_name: str | None) -> list[str]:
    """Return insertable (non-computed) column names for a table, in ordinal order."""
    sn = schema_name or "dbo"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM sys.columns c "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE t.name = ? AND s.name = ? AND c.is_computed = 0 "
            "ORDER BY c.column_id",
            (object_name, sn),
        )
        return [r[0] for r in cur.fetchall()]


def _target_identity_columns(conn: Any, object_name: str, schema_name: str | None) -> list[str]:
    """Return the columns that are IDENTITY columns on the *target* table.

    Used to decide whether ``SET IDENTITY_INSERT`` is required — we inspect the
    actual target table (not the source schema), so pre-existing Step 4 tables
    that were created as plain ``INT`` are left untouched.
    """
    sn = schema_name or "dbo"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM sys.columns c "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE t.name = ? AND s.name = ? AND c.is_identity = 1",
            (object_name, sn),
        )
        return [r[0] for r in cur.fetchall()]


class MSSQLSourceConnector(SourceConnector):
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
            f"DATABASE={self._config['database']};"
            f"UID={self._config['username']};"
            f"PWD={self._config.get('password', '')};"
            f"Encrypt={'yes' if self._config.get('ssl', True) else 'no'};"
            f"TrustServerCertificate={'no' if self._config.get('ssl', True) else 'yes'};"
        )

        self._conn = pyodbc.connect(conn_str)
        audit_log(phase="connect", status="success", details={"engine": "mssql", "role": "source"})

    def list_objects(self) -> list[str]:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        schemas = _resolve_mssql_schemas(self._config)
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    f"SELECT DISTINCT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    f"WHERE TABLE_TYPE = 'BASE TABLE' "
                    f"AND TABLE_SCHEMA IN ({placeholders}) "
                    f"ORDER BY TABLE_NAME",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_TYPE = 'BASE TABLE' "
                    "AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "ORDER BY TABLE_NAME"
                )
            tables = [row[0] for row in cur.fetchall()]
        for t in tables:
            validate_identifier(t, "table")
        return tables

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "table")
        qualified = _qualify(schema_name, object_name)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {qualified}")
            return cur.fetchone()[0]

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        qualified = _qualify(schema_name, object_name)
        with self._conn.cursor() as cur:
            cols = _non_computed_column_names(self._conn, object_name, schema_name)
            if cols:
                col_list = ", ".join(quote_identifier(c) for c in cols)
                cur.execute(f"SELECT {col_list} FROM {qualified}")
            else:
                cur.execute(f"SELECT * FROM {qualified}")
            columns = [desc[0] for desc in cur.description]
            for row in cur:
                yield dict(zip(columns, row))

    def get_schema(self, object_name: str) -> Schema:
        validate_identifier(object_name, "table")
        columns: list[Column] = []
        primary_key: list[str] = []
        schema_name = "dbo"

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_SCHEMA FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME = ? AND TABLE_TYPE = 'BASE TABLE'",
                (object_name,),
            )
            row = cur.fetchone()
            if row is not None:
                schema_name = row[0]

            # Identity / computed metadata from the catalog (INFORMATION_SCHEMA
            # does not expose these). Casts avoid pyodbc "type -16" fetch errors
            # on sys.identity_columns.seed_value.
            cur.execute(
                "SELECT c.name, c.is_identity, "
                "TRY_CAST(ic.seed_value AS INT), TRY_CAST(ic.increment_value AS INT), "
                "c.is_computed, TRY_CAST(cc.definition AS NVARCHAR(MAX)) "
                "FROM sys.columns c "
                "JOIN sys.tables t ON c.object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "LEFT JOIN sys.identity_columns ic "
                "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
                "LEFT JOIN sys.computed_columns cc "
                "  ON cc.object_id = c.object_id AND cc.column_id = c.column_id "
                "WHERE t.name = ? AND s.name = ?",
                (object_name, schema_name),
            )
            meta = {
                r[0]: (bool(r[1]), r[2], r[3], bool(r[4]), r[5])
                for r in cur.fetchall()
            }

            # User-defined (alias) types: INFORMATION_SCHEMA.COLUMNS reports only the
            # base DATA_TYPE for columns that use a UDT, so resolve the UDT via
            # sys.columns.user_type_id so the target table reuses the UDT.
            cur.execute(
                "SELECT c.name, sy.name, ty.name "
                "FROM sys.columns c "
                "JOIN sys.types ty ON c.user_type_id = ty.user_type_id AND ty.is_user_defined = 1 "
                "JOIN sys.schemas sy ON ty.schema_id = sy.schema_id "
                "JOIN sys.tables t ON c.object_id = t.object_id "
                "JOIN sys.schemas ts ON t.schema_id = ts.schema_id "
                "WHERE t.name = ? AND ts.name = ?",
                (object_name, schema_name),
            )
            udt_columns = {
                row[0]: f"[{row[1]}].[{row[2]}]" for row in cur.fetchall()
            }

            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, "
                "NUMERIC_PRECISION, NUMERIC_SCALE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ? "
                "ORDER BY ORDINAL_POSITION",
                (object_name, schema_name),
            )
            for row in cur.fetchall():
                col_name, data_type, nullable, max_len, numeric_precision, numeric_scale = row
                is_identity, seed, inc, is_computed, definition = meta.get(
                    col_name, (False, None, None, False, None)
                )
                columns.append(
                    Column(
                        name=col_name,
                        source_type=udt_columns.get(col_name, data_type),
                        target_type=None,
                        nullable=(nullable == "YES"),
                        size=max_len,
                        is_identity=is_identity,
                        identity_seed=seed,
                        identity_increment=inc,
                        is_computed=is_computed,
                        computed_definition=definition,
                        precision=int(numeric_precision) if numeric_precision else None,
                        scale=int(numeric_scale) if numeric_scale is not None else None,
                    )
                )

            # --- Indexes (excludes PK constraint indexes;
            #     PKs are handled inline in create_object_if_missing.
            #     UNIQUE constraint indexes are discovered here and applied
            #     via apply_constraints to avoid dropping them.) ---
            cur.execute(
                "SELECT i.name, i.is_unique, i.is_primary_key, i.is_unique_constraint, "
                "ic.key_ordinal, ic.is_included_column, ic.is_descending_key, "
                "c.name, "
                "i.filter_definition "
                "FROM sys.indexes i "
                "JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                "JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
                "JOIN sys.tables t ON i.object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE t.name = ? AND s.name = ? "
                "AND i.is_primary_key = 0 "
                "ORDER BY i.name, ic.key_ordinal",
                (object_name, schema_name),
            )
            _idx_groups: dict[str, dict] = {}
            _idx_filter: dict[str, str | None] = {}
            for row in cur.fetchall():
                idx_name, is_unique, _pk, _uq, key_ord, is_incl, is_desc, col_name, filter_def = row
                if idx_name is None:
                    continue
                if idx_name not in _idx_groups:
                    _idx_groups[idx_name] = {
                        "key_cols": [],
                        "included_cols": [],
                        "is_unique": bool(is_unique),
                    }
                    _idx_filter[idx_name] = filter_def
                entry = _idx_groups[idx_name]
                if is_incl:
                    entry["included_cols"].append(col_name)
                else:
                    entry["key_cols"].append((col_name, bool(is_desc)))

            indexes: list[Index] = []
            for idx_name, entry in _idx_groups.items():
                key_cols = entry["key_cols"]
                if not key_cols:
                    continue
                ddl = _build_mssql_index_ddl(
                    idx_name,
                    entry["is_unique"],
                    schema_name,
                    object_name,
                    key_cols,
                    entry["included_cols"] or None,
                    _idx_filter[idx_name],
                )
                indexes.append(
                    Index(
                        name=idx_name,
                        columns=[c[0] for c in key_cols],
                        unique=entry["is_unique"],
                        ddl=ddl,
                        included_columns=entry["included_cols"],
                        filter_definition=_idx_filter[idx_name],
                    )
                )

            cur.execute(
                "SELECT kcu.COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
                "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                "WHERE tc.TABLE_NAME = ? AND tc.TABLE_SCHEMA = ? "
                "AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'",
                (object_name, schema_name),
            )
            primary_key = [row[0] for row in cur.fetchall()]

        return Schema(name=object_name, schema_name=schema_name, columns=columns, primary_key=primary_key, indexes=indexes)

    def list_views(self) -> list["ViewDefinition"]:
        """Return user views in the configured schemas.

        Reads INFORMATION_SCHEMA.VIEWS (excludes system schemas).  The
        VIEW_DEFINITION column is NVARCHAR(MAX) and pyodbc can choke on it
        when fetched together with other columns, so it is CAST explicitly.
        """
        from core.connectors.base import ViewDefinition

        schemas = _resolve_mssql_schemas(self._config)
        results: list["ViewDefinition"] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_SCHEMA, "
                    "CAST(VIEW_DEFINITION AS NVARCHAR(MAX)) AS VIEW_DEFINITION "
                    "FROM INFORMATION_SCHEMA.VIEWS "
                    f"WHERE TABLE_SCHEMA IN ({placeholders}) "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_SCHEMA, "
                    "CAST(VIEW_DEFINITION AS NVARCHAR(MAX)) AS VIEW_DEFINITION "
                    "FROM INFORMATION_SCHEMA.VIEWS "
                    "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
                )
            for row in cur.fetchall():
                view_name, view_schema, view_def = row
                validate_identifier(view_name, "view")
                validate_identifier(view_schema, "schema")
                results.append(
                    ViewDefinition(
                        name=view_name,
                        schema_name=view_schema,
                        definition=view_def,
                    )
                )
        return results

    def list_all_sequences(self) -> list["SequenceDef"]:
        """Return user sequences in the configured schemas with SQL Server metadata."""
        from core.connectors.base import SequenceDef

        schemas = _resolve_mssql_schemas(self._config)
        results: list["SequenceDef"] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT s.name, sch.name, "
                    "CAST(TYPE_NAME(s.user_type_id) AS NVARCHAR(128)) AS sequence_type, "
                    "CAST(s.start_value AS BIGINT) AS start_value, "
                    "CAST(s.increment AS BIGINT) AS increment, "
                    "CAST(s.minimum_value AS BIGINT) AS minimum_value, "
                    "CAST(s.maximum_value AS BIGINT) AS maximum_value, "
                    "s.is_cycling, "
                    "CAST(s.cache_size AS BIGINT) AS cache_size, "
                    "CAST(s.current_value AS BIGINT) AS current_value, "
                    "s.is_cached "
                    "FROM sys.sequences AS s "
                    "JOIN sys.schemas AS sch ON sch.schema_id = s.schema_id "
                    f"WHERE sch.name IN ({placeholders}) "
                    "ORDER BY sch.name, s.name",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT s.name, sch.name, "
                    "CAST(TYPE_NAME(s.user_type_id) AS NVARCHAR(128)) AS sequence_type, "
                    "CAST(s.start_value AS BIGINT) AS start_value, "
                    "CAST(s.increment AS BIGINT) AS increment, "
                    "CAST(s.minimum_value AS BIGINT) AS minimum_value, "
                    "CAST(s.maximum_value AS BIGINT) AS maximum_value, "
                    "s.is_cycling, "
                    "CAST(s.cache_size AS BIGINT) AS cache_size, "
                    "CAST(s.current_value AS BIGINT) AS current_value, "
                    "s.is_cached "
                    "FROM sys.sequences AS s "
                    "JOIN sys.schemas AS sch ON sch.schema_id = s.schema_id "
                    "WHERE sch.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "ORDER BY sch.name, s.name"
                )

            for row in cur.fetchall():
                (
                    seq_name,
                    seq_schema,
                    seq_type,
                    start_value,
                    increment,
                    minimum_value,
                    maximum_value,
                    is_cycling,
                    cache_size,
                    current_value,
                    is_cached,
                ) = row
                validate_identifier(seq_name, "sequence")
                validate_identifier(seq_schema, "schema")
                results.append(
                    SequenceDef(
                        name=seq_name,
                        schema=seq_schema,
                        start_value=int(start_value),
                        increment=int(increment),
                        min_value=int(minimum_value),
                        max_value=int(maximum_value),
                        cycle=bool(is_cycling),
                        last_value=(
                            int(current_value) if current_value is not None else None
                        ),
                        owned_by=None,
                        data_type=seq_type,
                        cache_size=int(cache_size) if cache_size is not None else 1,
                        is_cached=bool(is_cached),
                    )
            )
        return results

    def list_functions(self) -> list[FunctionDef]:
        """Return user functions and stored procedures in the configured schemas.

        SQL Server stores the full CREATE definition in sys.sql_modules.definition
        (types: 'FN' scalar, 'TF' table-valued, 'IF' inline table-valued, 'P' procedure).
        """
        schemas = _resolve_mssql_schemas(self._config)
        results: list[FunctionDef] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT s.name, o.name, m.definition "
                    "FROM sys.objects o "
                    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                    "JOIN sys.sql_modules m ON o.object_id = m.object_id "
                    f"WHERE s.name IN ({placeholders}) "
                    "AND o.type IN ('FN', 'TF', 'IF', 'P') "
                    "ORDER BY s.name, o.name",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT s.name, o.name, m.definition "
                    "FROM sys.objects o "
                    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                    "JOIN sys.sql_modules m ON o.object_id = m.object_id "
                    "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "AND o.type IN ('FN', 'TF', 'IF', 'P') "
                    "ORDER BY s.name, o.name"
                )
            for schema_name, obj_name, definition in cur.fetchall():
                validate_identifier(obj_name, "function")
                validate_identifier(schema_name, "schema")
                results.append(
                    FunctionDef(
                        name=obj_name,
                        schema_name=schema_name,
                        ddl=definition,
                    )
                )
        return results

    def list_synonyms(self) -> list[SynonymDef]:
        """Return user synonyms in the configured schemas."""
        schemas = _resolve_mssql_schemas(self._config)
        results: list[SynonymDef] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT syn.name, sch.name, syn.base_object_name "
                    "FROM sys.synonyms syn "
                    "JOIN sys.schemas sch ON syn.schema_id = sch.schema_id "
                    f"WHERE sch.name IN ({placeholders}) "
                    "ORDER BY sch.name, syn.name",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT syn.name, sch.name, syn.base_object_name "
                    "FROM sys.synonyms syn "
                    "JOIN sys.schemas sch ON syn.schema_id = sch.schema_id "
                    "WHERE sch.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "ORDER BY sch.name, syn.name"
                )
            for syn_name, syn_schema, base_object in cur.fetchall():
                validate_identifier(syn_name, "synonym")
                validate_identifier(syn_schema, "schema")
                results.append(
                    SynonymDef(
                        name=syn_name,
                        schema_name=syn_schema,
                        base_object=base_object,
                    )
                )
        return results

    def list_types(self) -> list["TypeDef"]:
        """Return user-defined (alias) data types (TVPs are not migrated as types).

        SQL Server stores alias/user-defined types in sys.types with
        is_user_defined = 1. INFORMATION_SCHEMA.COLUMNS only reports the *base*
        DATA_TYPE for columns that use such a type, so these are discovered here
        (and re-applied to columns in get_schema) to preserve schema-qualified
        UDT references on the target.
        """
        schemas = _resolve_mssql_schemas(self._config)
        results: list["TypeDef"] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT s.name, t.name, t.is_nullable, t.max_length, "
                    "TRY_CAST(t.precision AS INT), TRY_CAST(t.scale AS INT), "
                    "TYPE_NAME(t.system_type_id) AS base_name "
                    "FROM sys.types t "
                    "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                    f"WHERE s.name IN ({placeholders}) AND t.is_user_defined = 1 "
                    "ORDER BY s.name, t.name",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT s.name, t.name, t.is_nullable, t.max_length, "
                    "TRY_CAST(t.precision AS INT), TRY_CAST(t.scale AS INT), "
                    "TYPE_NAME(t.system_type_id) AS base_name "
                    "FROM sys.types t "
                    "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                    "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "AND t.is_user_defined = 1 "
                    "ORDER BY s.name, t.name"
                )
            for row in cur.fetchall():
                type_schema, type_name, is_nullable, max_len, precision, scale, base_name = row
                validate_identifier(type_name, "type")
                validate_identifier(type_schema, "schema")
                base_type = _mssql_column_type(base_name, max_len, precision, scale)
                null_str = "NULL" if is_nullable else "NOT NULL"
                ddl = (
                    f"CREATE TYPE [{type_schema}].[{type_name}] "
                    f"FROM {base_type} {null_str}"
                )
                results.append(
                    TypeDef(
                        name=f"{type_schema}.{type_name}",
                        kind="alias",
                        ddl=ddl,
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Step 12 — Partition discovery
    # ------------------------------------------------------------------

    def list_partition_functions(self) -> list["PartitionFunctionDef"]:
        """Return user partition functions with their boundaries."""
        schemas = _resolve_mssql_schemas(self._config)
        results: list["PartitionFunctionDef"] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pf.name, pf.type_desc, pf.boundary_value_on_right, "
                "prv.value, prv.boundary_id "
                "FROM sys.partition_functions pf "
                "LEFT JOIN sys.partition_range_values prv "
                "  ON prv.function_id = pf.function_id "
                "ORDER BY pf.name, prv.boundary_id",
            )
            pf_map: dict[str, PartitionFunctionDef] = {}
            for row in cur.fetchall():
                pf_name, type_desc, bvr, value, boundary_id = row
                if pf_name not in pf_map:
                    range_desc = "RANGE RIGHT" if bvr else "RANGE LEFT"
                    pf_map[pf_name] = PartitionFunctionDef(
                        name=pf_name,
                        schema_name="dbo",
                        data_type="datetime2",
                        range_desc=range_desc,
                    )
                if value is not None:
                    pf_map[pf_name].boundaries.append(value)
            results = list(pf_map.values())
        return results

    def list_partition_schemes(self) -> list["PartitionSchemeDef"]:
        """Return user partition schemes with their filegroup mappings."""
        schemas = _resolve_mssql_schemas(self._config)
        results: list["PartitionSchemeDef"] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ps.name, pf.name AS pf_name "
                "FROM sys.partition_schemes ps "
                "JOIN sys.partition_functions pf ON ps.function_id = pf.function_id "
                "ORDER BY ps.name",
            )
            scheme_names = [row[0] for row in cur.fetchall()]

            for ps_name in scheme_names:
                cur.execute(
                    "SELECT ps.name, pf.name AS pf_name, "
                    "fg.name AS fg_name "
                    "FROM sys.partition_schemes ps "
                    "JOIN sys.partition_functions pf ON ps.function_id = pf.function_id "
                    "JOIN sys.destination_data_spaces dds "
                    "  ON dds.partition_scheme_id = ps.data_space_id "
                    "JOIN sys.filegroups fg ON fg.data_space_id = dds.data_space_id "
                    "WHERE ps.name = ? "
                    "ORDER BY dds.destination_id",
                    (ps_name,),
                )
                fg_list = []
                pf_name_val = ""
                for row in cur.fetchall():
                    _, pf_name, fg_name = row
                    pf_name_val = pf_name
                    fg_list.append(fg_name)
                results.append(PartitionSchemeDef(
                    name=ps_name,
                    schema_name="dbo",
                    partition_function_name=pf_name_val,
                    filegroups=fg_list,
                ))
        return results

    def get_partitioned_tables(self) -> list["PartitionedTableDef"]:
        """Return partitioned table/index metadata."""
        schemas = _resolve_mssql_schemas(self._config)
        results: list["PartitionedTableDef"] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT t.name, s.name AS schema_name, "
                    "i.name AS index_name, "
                    "pf.name AS pf_name, "
                    "c.name AS partition_column "
                    "FROM sys.tables t "
                    "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                    "JOIN sys.indexes i ON t.object_id = i.object_id "
                    "JOIN sys.partition_schemes ps ON i.data_space_id = ps.data_space_id "
                    "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
                    "LEFT JOIN sys.index_columns ic "
                    "  ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                    "  AND ic.is_included_column = 0 "
                    "LEFT JOIN sys.columns c ON c.object_id = t.object_id "
                    "  AND c.column_id = ic.column_id "
                    f"WHERE s.name IN ({placeholders}) "
                    "AND i.data_space_id IS NOT NULL "
                    "ORDER BY t.name, i.name",
                    list(schemas),
                )
            else:
                cur.execute(
                    "SELECT t.name, s.name AS schema_name, "
                    "i.name AS index_name, "
                    "pf.name AS pf_name, "
                    "c.name AS partition_column "
                    "FROM sys.tables t "
                    "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                    "JOIN sys.indexes i ON t.object_id = i.object_id "
                    "JOIN sys.partition_schemes ps ON i.data_space_id = ps.data_space_id "
                    "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
                    "LEFT JOIN sys.index_columns ic "
                    "  ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                    "  AND ic.is_included_column = 0 "
                    "LEFT JOIN sys.columns c ON c.object_id = t.object_id "
                    "  AND c.column_id = ic.column_id "
                    "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "AND i.data_space_id IS NOT NULL "
                    "ORDER BY t.name, i.name"
                )
            for row in cur.fetchall():
                table_name, schema_name, index_name, pf_name, partition_column = row
                results.append(PartitionedTableDef(
                    table_name=table_name,
                    schema_name=schema_name,
                    index_name=index_name,
                    partition_function_name=pf_name,
                    partition_column=partition_column,
                ))
        return results


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
        validate_identifier(db_name, "database")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM sys.databases WHERE name = ?", (db_name,)
            )
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {db_name}")
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
            cur.execute(ddl)
            self._conn.commit()
            audit_log(
                phase="create_sequence",
                status="created",
                details={"sequence": seq_qname, "owned_by": seq.owned_by},
            )

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

    def create_object_if_missing(self, schema: Schema) -> None:
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

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
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
        with self._conn.cursor() as cur:
            columns = list(batch[0].keys())
            col_names = ", ".join(columns)
            placeholders = ", ".join(["?"] * len(columns))
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

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
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

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "table")
        qualified = _qualify(schema_name, object_name)
        with self._conn.cursor() as cur:
            cols = _non_computed_column_names(self._conn, object_name, schema_name)
            if cols:
                col_list = ", ".join(quote_identifier(c) for c in cols)
                cur.execute(f"SELECT {col_list} FROM {qualified}")
            else:
                cur.execute(f"SELECT * FROM {qualified}")
            columns = [desc[0] for desc in cur.description]
            for row in cur:
                yield dict(zip(columns, row))

    def apply_constraints(self, schema: Schema) -> None:
        validate_identifier(schema.name, "table")
        schema_name = schema.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
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

    def create_view(self, view: ViewDefinition) -> None:
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
            try:
                definition = view.definition
                if definition.upper().startswith("CREATE VIEW"):
                    definition = definition[len("CREATE VIEW") :].lstrip()
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


    def create_function(self, func: FunctionDef) -> None:
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

    def create_synonym(self, synonym: SynonymDef) -> None:
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
            cur.execute(ddl)
            self._conn.commit()
            audit_log(
                phase="create_partition_function", status="created",
                details={"function": f"{pf_schema}.{pf.name}"},
            )

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
            cur.execute(ddl)
            self._conn.commit()
            audit_log(
                phase="create_partition_scheme", status="created",
                details={"scheme": f"{ps_schema}.{ps.name}"},
            )

    def create_partitioned_table(
        self,
        schema: "Schema",
        partition_function_name: str,
        partition_column: str,
    ) -> None:
        """Create a table with partitioning applied."""
        validate_identifier(schema.name, "table")
        schema_name = schema.schema_name or "dbo"
        validate_identifier(schema_name, "schema")
        validate_identifier(partition_function_name, "partition function")
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
                f"ON {partition_function_name}({partition_column})"
            )
            cur.execute(ddl)
            self._conn.commit()
            audit_log(
                phase="create_partitioned_table", status="created",
                details={"table": qualified},
            )


class MSSQLCDCEngine(CDCEngine):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        self._last_lsn: str | None = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pyodbc")
        import pyodbc

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={self._config['host']},{self._config.get('port', 1433)};"
            f"DATABASE={self._config['database']};"
            f"UID={self._config['username']};"
            f"PWD={self._config.get('password', '')};"
            f"Encrypt=yes;TrustServerCertificate=no;"
        )

        self._conn = pyodbc.connect(conn_str)

    def start(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM sys.databases WHERE is_cdc_enabled = 1")
            cdc_dbs = [row[0] for row in cur.fetchall()]
            if self._config["database"] not in cdc_dbs:
                cur.execute(f"EXEC sys.sp_cdc_enable_db")
        audit_log(phase="cdc_start", status="success", details={"engine": "mssql"})

    def poll_changes(self) -> list[ChangeEvent]:
        self._config["database"]
        schema_name = "dbo"

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT t.name FROM sys.tables t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN cdc.change_tables ct ON ct.object_id = t.object_id "
                "WHERE s.name = ?",
                (schema_name,),
            )
            tables = [row[0] for row in cur.fetchall()]

        start_lsn = self._last_lsn if self._last_lsn is not None else f"sys.fn_cdc_get_min_lsn('{schema_name}')"
        events = []
        for table_name in tables:
            func_name = f"cdc.fn_cdc_get_all_changes_{schema_name}_{table_name}"
            validate_identifier(table_name, "table")

            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {func_name}("
                    f"{start_lsn}, "
                    f"sys.fn_cdc_get_max_lsn(), 'all')"
                )
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

            for row in rows:
                row_dict = dict(zip(columns, row))
                operation_code = row_dict.get("__$operation", 2)
                operation_map = {1: "delete", 2: "insert", 4: "update"}
                operation = operation_map.get(operation_code)
                if operation is None:
                    continue

                document = {
                    k: v
                    for k, v in row_dict.items()
                    if not k.startswith("__$")
                }

                object_name = table_name
                events.append(
                    ChangeEvent(
                        operation=operation,
                        document=document,
                        object_name=object_name,
                        watermark=row_dict.get("__$start_lsn"),
                    )
                )

        return events

    def apply(self, events: list[ChangeEvent], target: TargetConnector) -> ApplyResult:
        result = ApplyResult()
        if not events:
            return result

        for event in events:
            try:
                if event.operation == "insert":
                    target.upsert_batch(event.object_name, iter([event.document]), event.schema)
                elif event.operation == "update":
                    target.upsert_batch(event.object_name, iter([event.document]), event.schema)
                elif event.operation == "delete":
                    target.delete(event.object_name, event.document, event.schema)
                result.success_count += 1
            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0:
            result.last_checkpoint = events[-1].watermark
            audit_log(phase="cdc_apply", status="success", details={"applied": result.success_count})
        else:
            audit_log(phase="cdc_apply", status="partial_failure", details={"success": result.success_count, "failure": result.failure_count})

        return result

    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return

        lsn = result.last_checkpoint
        self._last_lsn = (
            f"0x{lsn.hex()}" if isinstance(lsn, (bytes, bytearray)) else str(lsn)
        )
        audit_log(phase="cdc_checkpoint", status="advanced", details={"lsn": self._last_lsn})
