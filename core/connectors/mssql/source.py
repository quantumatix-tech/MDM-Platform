"""MSSQL Source Connector.

Extracted from ``core/connectors/mssql.py`` — all discovery and export
logic for the source database.

Table-specific discovery and export logic is delegated to
``core.connectors.mssql.objects.table`` so that ``source.py`` acts as the
connector-facing router.
"""
from __future__ import annotations

from collections.abc import Iterator

from core.connectors.base import (
    CheckConstraint,
    Column,
    DefaultConstraint,
    ForeignKey,
    FunctionDef,
    GrantDef,
    Index,
    RoleDef,
    RoleMembershipDef,
    Schema,
    SourceConnector,
    SynonymDef,
    TriggerDef,
    TypeDef,
    UserDef,
    ViewDefinition,
    validate_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log

from core.connectors.mssql._models import (
    PartitionFunctionDef,
    PartitionSchemeDef,
    PartitionedTableDef,
    _build_mssql_index_ddl,
    _mssql_column_type,
    _qualify,
    _resolve_mssql_schemas,
)
from core.connectors.mssql.objects import table as _mssql_table
from core.connectors.mssql.objects import view as _mssql_view
from core.connectors.mssql.objects import trigger as _mssql_trigger


class MSSQLSourceConnector(SourceConnector):
    def __init__(self, config: dict) -> None:
        self._config = config
        self._conn = None

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
        """Return user base tables in the configured schemas.

        Delegates to ``table.discover_tables`` for the table-discovery logic.
        """
        return _mssql_table.discover_tables(self._conn, self._config)

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        """Count rows in a table. Delegates to ``table.get_table_row_count``."""
        return _mssql_table.get_table_row_count(self._conn, object_name, schema_name)

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict]:
        """Stream all rows from a table. Delegates to ``table.export_table_data``.

        The column-introspection helpers ``_non_computed_column_names`` and
        ``_variant_column_names`` are resolved inside ``table.py`` through the
        ``core.connectors.mssql`` package namespace at call time so that test
        patches on ``core.connectors.mssql._non_computed_column_names`` are
        honoured.
        """
        yield from _mssql_table.export_table_data(self._conn, object_name, schema_name)

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
            _idx_groups: dict = {}
            _idx_filter: dict = {}
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

            # --- Foreign Keys (cross-schema aware) ---
            cur.execute(
                "SELECT "
                "  fk.name AS fk_name, "
                "  pc.name AS parent_col, "
                "  rc.name AS ref_col, "
                "  OBJECT_SCHEMA_NAME(fk.referenced_object_id) AS ref_schema, "
                "  OBJECT_NAME(fk.referenced_object_id) AS ref_table, "
                "  fkc.constraint_column_id AS ord "
                "FROM sys.foreign_keys fk "
                "JOIN sys.foreign_key_columns fkc "
                "  ON fk.object_id = fkc.constraint_object_id "
                "JOIN sys.columns pc "
                "  ON fkc.parent_column_id = pc.column_id "
                "  AND pc.object_id = fk.parent_object_id "
                "JOIN sys.columns rc "
                "  ON fkc.referenced_column_id = rc.column_id "
                "  AND rc.object_id = fk.referenced_object_id "
                "JOIN sys.tables t ON fk.parent_object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE t.name = ? AND s.name = ? "
                "ORDER BY fkc.constraint_column_id",
                (object_name, schema_name),
            )
            fk_map: dict = {}
            for row in cur.fetchall():
                fk_name, parent_col, ref_col, ref_schema, ref_table, _ord = row
                if fk_name not in fk_map:
                    fk_map[fk_name] = ForeignKey(
                        name=fk_name,
                        columns=[],
                        ref_table=ref_table,
                        ref_columns=[],
                        ref_schema=ref_schema,
                    )
                fk_map[fk_name].columns.append(parent_col)
                fk_map[fk_name].ref_columns.append(ref_col)
            foreign_keys = list(fk_map.values())

            # --- CHECK Constraints ---
            cur.execute(
                "SELECT cc.name, cc.definition "
                "FROM sys.check_constraints cc "
                "JOIN sys.tables t ON cc.parent_object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE t.name = ? AND s.name = ? AND cc.is_disabled = 0 "
                "ORDER BY cc.name",
                (object_name, schema_name),
            )
            check_constraints = [
                CheckConstraint(name=r[0], expression=r[1]) for r in cur.fetchall()
            ]

            # --- DEFAULT Constraints ---
            cur.execute(
                "SELECT dc.name, COL_NAME(dc.parent_object_id, dc.parent_column_id), dc.definition "
                "FROM sys.default_constraints dc "
                "JOIN sys.tables t ON dc.parent_object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE t.name = ? AND s.name = ? "
                "ORDER BY dc.name",
                (object_name, schema_name),
            )
            default_constraints = [
                DefaultConstraint(name=r[0], column=r[1], definition=r[2])
                for r in cur.fetchall()
            ]

        return Schema(
            name=object_name,
            schema_name=schema_name,
            columns=columns,
            primary_key=primary_key,
            indexes=indexes,
            foreign_keys=foreign_keys,
            check_constraints=check_constraints,
            default_constraints=default_constraints,
        )

    def list_views(self) -> list[ViewDefinition]:
        """Return user views in the configured schemas. Delegates to ``view.discover_views``."""
        return _mssql_view.discover_views(self._conn, self._config)

    def list_all_sequences(self) -> list:
        """Return user sequences in the configured schemas with SQL Server metadata."""
        from core.connectors.base import SequenceDef

        schemas = _resolve_mssql_schemas(self._config)
        results: list[SequenceDef] = []
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

    def list_types(self) -> list[TypeDef]:
        """Return user-defined (alias) data types (TVPs are not migrated as types).

        SQL Server stores alias/user-defined types in sys.types with
        is_user_defined = 1. INFORMATION_SCHEMA.COLUMNS only reports the base
        DATA_TYPE for columns that use such a type, so these are discovered here
        (and re-applied to columns in get_schema) to preserve schema-qualified
        UDT references on the target.
        """
        from core.connectors.mssql._models import _mssql_column_type

        schemas = _resolve_mssql_schemas(self._config)
        results: list[TypeDef] = []
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
    # Step 14 — Security: Grants (Phase 16)
    # ------------------------------------------------------------------

    def list_grants(self) -> list[GrantDef]:
        """Discover database permissions (schema, table, column, database level).

        Queries sys.database_permissions, filtering to grants on objects
        within the configured schemas (or all non-system schemas), excluding
        system principals (public, dbo, fixed database roles).
        """
        from core.connectors.mssql._models import _MSSQL_FIXED_DB_ROLES

        schemas = _resolve_mssql_schemas(self._config)
        results: list[GrantDef] = []

        _fixed_roles_sql = ", ".join(f"'{r}'" for r in _MSSQL_FIXED_DB_ROLES)

        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    f"SELECT dp.permission_name, dp.class_desc, "
                    f"dp.major_id, dp.minor_id, "
                    f"grantee.name AS grantee_name, "
                    f"obj.name AS object_name, "
                    f"col.name AS column_name, "
                    f"sch.name AS schema_name, "
                    f"db.name AS database_name "
                    f"FROM sys.database_permissions dp "
                    f"JOIN sys.database_principals grantee "
                    f"  ON dp.grantee_principal_id = grantee.principal_id "
                    f"LEFT JOIN sys.objects obj "
                    f"  ON dp.major_id = obj.object_id "
                    f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                    f"LEFT JOIN sys.columns col "
                    f"  ON dp.major_id = col.object_id "
                    f"  AND dp.minor_id = col.column_id "
                    f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                    f"LEFT JOIN sys.schemas sch "
                    f"  ON (dp.class_desc = 'OBJECT_OR_COLUMN' "
                    f"      AND obj.schema_id = sch.schema_id) "
                    f"  OR (dp.class_desc = 'SCHEMA' "
                    f"      AND dp.major_id = sch.schema_id) "
                    f"LEFT JOIN sys.databases db "
                    f"  ON dp.major_id = db.database_id "
                    f"  AND dp.class_desc = 'DATABASE' "
                    f"WHERE dp.state = 'G' "
                    f"  AND grantee.name NOT IN ({_fixed_roles_sql}) "
                    f"  AND (dp.class_desc = 'DATABASE' "
                    f"       OR sch.name IN ({placeholders})) "
                    f"ORDER BY dp.class_desc, grantee.name, "
                    f"ISNULL(sch.name, db.name), "
                    f"ISNULL(obj.name, sch.name), col.name",
                    list(schemas),
                )
            else:
                cur.execute(
                    f"SELECT dp.permission_name, dp.class_desc, "
                    f"dp.major_id, dp.minor_id, "
                    f"grantee.name AS grantee_name, "
                    f"obj.name AS object_name, "
                    f"col.name AS column_name, "
                    f"sch.name AS schema_name, "
                    f"db.name AS database_name "
                    f"FROM sys.database_permissions dp "
                    f"JOIN sys.database_principals grantee "
                    f"  ON dp.grantee_principal_id = grantee.principal_id "
                    f"LEFT JOIN sys.objects obj "
                    f"  ON dp.major_id = obj.object_id "
                    f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                    f"LEFT JOIN sys.columns col "
                    f"  ON dp.major_id = col.object_id "
                    f"  AND dp.minor_id = col.column_id "
                    f"  AND dp.class_desc = 'OBJECT_OR_COLUMN' "
                    f"LEFT JOIN sys.schemas sch "
                    f"  ON (dp.class_desc = 'OBJECT_OR_COLUMN' "
                    f"      AND obj.schema_id = sch.schema_id) "
                    f"  OR (dp.class_desc = 'SCHEMA' "
                    f"      AND dp.major_id = sch.schema_id) "
                    f"LEFT JOIN sys.databases db "
                    f"  ON dp.major_id = db.database_id "
                    f"  AND dp.class_desc = 'DATABASE' "
                    f"WHERE dp.state = 'G' "
                    f"  AND grantee.name NOT IN ({_fixed_roles_sql}) "
                    f"  AND (dp.class_desc = 'DATABASE' "
                    f"       OR sch.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')) "
                    f"ORDER BY dp.class_desc, grantee.name, "
                    f"ISNULL(sch.name, db.name), "
                    f"ISNULL(obj.name, sch.name), col.name"
                )
            rows = cur.fetchall()

        groups: dict = {}
        for row in rows:
            (
                permission_name,
                class_desc,
                major_id,
                minor_id,
                grantee_name,
                object_name,
                column_name,
                schema_name,
                database_name,
            ) = row

            if class_desc == "DATABASE":
                obj_type = "DATABASE"
                obj_name = database_name or ""
                sch = ""
            elif class_desc == "SCHEMA":
                obj_type = "SCHEMA"
                obj_name = schema_name or ""
                sch = schema_name or ""
            elif class_desc == "OBJECT_OR_COLUMN":
                if minor_id and minor_id > 0:
                    obj_type = "COLUMN"
                    obj_name = f"{object_name}.{column_name}"
                    sch = schema_name or ""
                else:
                    obj_type = "TABLE"
                    obj_name = object_name or ""
                    sch = schema_name or ""
            else:
                continue

            key = (grantee_name, obj_type, obj_name, sch)
            if key not in groups:
                groups[key] = {
                    "privileges": [],
                    "object_type": obj_type,
                    "object_name": obj_name,
                    "schema_name": sch,
                    "grantee": grantee_name,
                }
            groups[key]["privileges"].append(permission_name)

        for info in groups.values():
            results.append(
                GrantDef(
                    privileges=", ".join(sorted(set(info["privileges"]))),
                    object_type=info["object_type"],
                    object_name=info["object_name"],
                    grantee=info["grantee"],
                    schema_name=info["schema_name"],
                )
            )
        return results

    # ------------------------------------------------------------------
    # Step 14 — Security: Users, Roles & Role Memberships (source discovery)
    # ------------------------------------------------------------------

    def list_users(self) -> list[UserDef]:
        """Discover database users (excluding system principals).

        Returns user-defined database users with type 'S' (SQL user) or
        'U' (Windows user), excluding system principals (dbo, guest,
        INFORMATION_SCHEMA, sys).
        """
        results: list[UserDef] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name, type FROM sys.database_principals "
                "WHERE type IN ('S', 'U') "
                "AND name NOT IN ('dbo', 'guest', 'INFORMATION_SCHEMA', 'sys') "
                "ORDER BY name"
            )
            for row in cur.fetchall():
                results.append(UserDef(name=row[0], type=row[1]))
        return results

    def list_roles(self) -> list[RoleDef]:
        """Discover database roles (excluding fixed/system roles).

        Returns user-defined database roles with type 'R' (database role)
        or 'C' (application role), excluding fixed system roles.
        """
        from core.connectors.mssql._models import _MSSQL_FIXED_DB_ROLES

        _fixed_roles_sql = ", ".join(f"'{r}'" for r in _MSSQL_FIXED_DB_ROLES)
        results: list[RoleDef] = []
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT name, type FROM sys.database_principals "
                f"WHERE type IN ('R', 'C') "
                f"AND name NOT IN ({_fixed_roles_sql}) "
                f"ORDER BY name"
            )
            for row in cur.fetchall():
                results.append(RoleDef(name=row[0], type=row[1]))
        return results

    def list_role_memberships(self) -> list[RoleMembershipDef]:
        """Discover database role memberships (excluding system principals).

        Returns mappings of member_principal -> role_principal, excluding
        memberships involving fixed/system principals (public, dbo, db_*).
        """
        from core.connectors.mssql._models import _MSSQL_FIXED_DB_ROLES

        _fixed_roles_sql = ", ".join(f"'{r}'" for r in _MSSQL_FIXED_DB_ROLES)
        results: list[RoleMembershipDef] = []
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT m.name AS member_name, r.name AS role_name "
                f"FROM sys.database_role_members drm "
                f"JOIN sys.database_principals m "
                f"  ON drm.member_principal_id = m.principal_id "
                f"JOIN sys.database_principals r "
                f"  ON drm.role_principal_id = r.principal_id "
                f"WHERE m.name NOT IN ({_fixed_roles_sql}) "
                f"  AND r.name NOT IN ({_fixed_roles_sql}) "
                f"ORDER BY r.name, m.name"
            )
            for row in cur.fetchall():
                results.append(RoleMembershipDef(
                    member_name=row[0], role_name=row[1]
                ))
        return results

    # ------------------------------------------------------------------
    # Step 12 — Partition discovery
    # ------------------------------------------------------------------

    def list_partition_functions(self) -> list[PartitionFunctionDef]:
        """Return user partition functions with their boundaries."""
        results: list[PartitionFunctionDef] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pf.name, pf.type_desc, pf.boundary_value_on_right, "
                "prv.value, prv.boundary_id "
                "FROM sys.partition_functions pf "
                "LEFT JOIN sys.partition_range_values prv "
                "  ON prv.function_id = pf.function_id "
                "ORDER BY pf.name, prv.boundary_id",
            )
            pf_map: dict = {}
            for row in cur.fetchall():
                pf_name, type_desc, bvr, value, boundary_id = row
                if pf_name not in pf_map:
                    range_desc = "RANGE RIGHT" if bvr else "RANGE LEFT"
                    pf_map[pf_name] = PartitionFunctionDef(
                        name=pf_name,
                        schema_name="dbo",
                        data_type="datetime2",
                        boundaries=[],
                        range_desc=range_desc,
                    )
                if value is not None:
                    pf_map[pf_name].boundaries.append(value)
            results = list(pf_map.values())
        return results

    def list_partition_schemes(self) -> list[PartitionSchemeDef]:
        """Return user partition schemes with their filegroup mappings."""
        results: list[PartitionSchemeDef] = []
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

    def get_partitioned_tables(self) -> list[PartitionedTableDef]:
        """Return partitioned table/index metadata."""
        schemas = _resolve_mssql_schemas(self._config)
        results: list[PartitionedTableDef] = []
        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                cur.execute(
                    "SELECT t.name, s.name AS schema_name, "
                    "i.name AS index_name, "
                    "pf.name AS pf_name, "
                    "ps.name AS ps_name, "
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
                    "ps.name AS ps_name, "
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
                table_name, schema_name, index_name, pf_name, ps_name, partition_column = row
                results.append(PartitionedTableDef(
                    table_name=table_name,
                    schema_name=schema_name,
                    index_name=index_name,
                    partition_function_name=pf_name,
                    partition_scheme_name=ps_name,
                    partition_column=partition_column,
                ))
        return results

    # ------------------------------------------------------------------
    # Step 15 — Comments / Extended Properties
    # ------------------------------------------------------------------

    def list_comments(self) -> list:
        """Return extended properties (comments) for tables, columns, views, functions, schemas.

        Queries sys.extended_properties where name = 'MS_Description'.
        Supports: TABLE, VIEW, FUNCTION, PROCEDURE, SCHEMA, COLUMN.
        """
        from core.connectors.base import CommentDef

        schemas = _resolve_mssql_schemas(self._config)
        comments: list[CommentDef] = []

        with self._conn.cursor() as cur:
            if schemas:
                placeholders = ", ".join("?" for _ in schemas)
                schema_filter = f"AND s.name IN ({placeholders})"
                params = list(schemas)
            else:
                schema_filter = "AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')"
                params = []

            # Table, View, Function, Procedure comments (class=1, minor_id=0)
            cur.execute(
                f"SELECT ep.value, s.name AS schema_name, o.name AS object_name, "
                f"o.type_desc, "
                f"CASE o.type "
                f"  WHEN 'U' THEN 'TABLE' "
                f"  WHEN 'V' THEN 'VIEW' "
                f"  WHEN 'FN' THEN 'FUNCTION' "
                f"  WHEN 'TF' THEN 'FUNCTION' "
                f"  WHEN 'IF' THEN 'FUNCTION' "
                f"  WHEN 'P' THEN 'PROCEDURE' "
                f"  ELSE 'OBJECT' END AS obj_type "
                f"FROM sys.extended_properties ep "
                f"JOIN sys.objects o ON ep.major_id = o.object_id "
                f"JOIN sys.schemas s ON o.schema_id = s.schema_id "
                f"WHERE ep.class = 1 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                f"{schema_filter} "
                f"ORDER BY s.name, o.name",
                params,
            )
            for row in cur.fetchall():
                value, schema_name, object_name, type_desc, obj_type = row
                comments.append(CommentDef(
                    object_type=obj_type,
                    object_name=object_name,
                    comment=value,
                    schema_name=schema_name,
                ))

            # Column comments (class=1, minor_id=column_id)
            cur.execute(
                f"SELECT ep.value, s.name AS schema_name, o.name AS table_name, c.name AS column_name "
                f"FROM sys.extended_properties ep "
                f"JOIN sys.objects o ON ep.major_id = o.object_id "
                f"JOIN sys.schemas s ON o.schema_id = s.schema_id "
                f"JOIN sys.columns c ON c.object_id = o.object_id AND c.column_id = ep.minor_id "
                f"WHERE ep.class = 1 AND ep.minor_id > 0 AND ep.name = 'MS_Description' "
                f"{schema_filter} "
                f"ORDER BY s.name, o.name, c.column_id",
                params,
            )
            for row in cur.fetchall():
                value, schema_name, table_name, column_name = row
                comments.append(CommentDef(
                    object_type="COLUMN",
                    object_name=f"{table_name}.{column_name}",
                    comment=value,
                    schema_name=schema_name,
                ))

            # Schema comments (class=3, major_id=schema_id, minor_id=0)
            cur.execute(
                f"SELECT ep.value, s.name AS schema_name "
                f"FROM sys.extended_properties ep "
                f"JOIN sys.schemas s ON ep.major_id = s.schema_id "
                f"WHERE ep.class = 3 AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                f"{schema_filter} "
                f"ORDER BY s.name",
                params,
            )
            for row in cur.fetchall():
                value, schema_name = row
                comments.append(CommentDef(
                    object_type="SCHEMA",
                    object_name=schema_name,
                    comment=value,
                    schema_name=schema_name,
                ))

        return comments

    # ------------------------------------------------------------------
    # Step 9 — Triggers (Phase 15 source discovery)
    # ------------------------------------------------------------------

    def get_all_triggers(self) -> list[TriggerDef]:
        """Return user DML triggers in the configured schemas.

        Delegates to ``core.connectors.mssql.objects.trigger.discover_triggers``.
        """
        return _mssql_trigger.discover_triggers(self._conn, self._config)
