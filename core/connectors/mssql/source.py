"""MSSQL Source Connector — connector-facing source API / routing layer.

Every object-specific discovery and export implementation lives under
``core.connectors.mssql.objects``; this module owns the connector surface
and forwards to it:

  table     : table discovery, row counts, data export
  view      : view discovery
  trigger   : DML trigger discovery
  function  : function/procedure discovery
  sequence  : sequence discovery
  synonym   : synonym discovery
  type      : user-defined (alias) type discovery
  comment   : extended property (comment) discovery
  partition : partition function/scheme and partitioned-table discovery
  security  : grant, user, role and role-membership discovery

Two responsibilities intentionally remain here rather than in an object
module:

``connect()``
    Shared connector infrastructure (ODBC connection string, driver
    setup, retry, audit).

``get_schema()``
    Mixed composition point. It assembles the cross-engine ``Schema``
    DTO from several metadata families in one pass — columns/identity/
    computed, user-defined types, indexes, primary key, foreign keys,
    CHECK constraints and DEFAULT constraints — so it belongs to no
    single object type. See its docstring for details.
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
        """Build the cross-engine ``Schema`` DTO for a base table.

        Intentionally retained in ``source.py`` rather than moved to an
        object module. This is a *composition point*, not a single object
        implementation: it combines several metadata families in one
        pass — columns with identity/computed attributes, user-defined
        type references, indexes, primary key, foreign keys, CHECK
        constraints and DEFAULT constraints — into the single
        cross-engine ``Schema`` contract that the target consumes.

        It is the source-side counterpart of
        ``MSSQLTargetConnector.apply_constraints()``, which applies the
        very same four constraint families in the same order. Splitting
        discovery into artificial index / FK / CHECK / DEFAULT modules
        would fragment that contract and leave both the DTO assembly and
        the orchestration behind as the largest remaining methods.
        """
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
        """Return user sequences in the configured schemas with SQL Server metadata.

        Delegates to ``core.connectors.mssql.objects.sequence.discover_sequences``.
        """
        return _mssql_sequence.discover_sequences(self._conn, self._config)

    def list_functions(self) -> list[FunctionDef]:
        """Return user functions and stored procedures in the configured schemas.

        Delegates to ``core.connectors.mssql.objects.function.discover_functions``.
        """
        return _mssql_function.discover_functions(self._conn, self._config)

    def list_synonyms(self) -> list[SynonymDef]:
        """Return user synonyms in the configured schemas.

        Delegates to ``core.connectors.mssql.objects.synonym.discover_synonyms``.
        """
        return _mssql_synonym.discover_synonyms(self._conn, self._config)

    def list_types(self) -> list[TypeDef]:
        """Return user-defined (alias) data types (TVPs are not migrated as types).

        SQL Server stores alias/user-defined types in sys.types with
        is_user_defined = 1. INFORMATION_SCHEMA.COLUMNS only reports the base
        DATA_TYPE for columns that use such a type, so these are discovered here
        (and re-applied to columns in get_schema) to preserve schema-qualified
        UDT references on the target.

        Delegates to ``core.connectors.mssql.objects.type.discover_types``.
        """
        return _mssql_type.discover_types(self._conn, self._config)

    # ------------------------------------------------------------------
    # Step 14 — Security: Grants
    # ------------------------------------------------------------------

    def list_grants(self) -> list[GrantDef]:
        """Discover database permissions (schema, table, column, database level).

        Delegates to ``core.connectors.mssql.objects.security.list_grants``.
        """
        return _mssql_security.list_grants(self._conn, self._config)

    # ------------------------------------------------------------------
    # Step 14 — Security: Users, Roles & Role Memberships (source discovery)
    # ------------------------------------------------------------------

    def list_users(self) -> list[UserDef]:
        """Discover database users (excluding system principals).

        Delegates to ``core.connectors.mssql.objects.security.list_users``.
        """
        return _mssql_security.list_users(self._conn)

    def list_roles(self) -> list[RoleDef]:
        """Discover database roles (excluding fixed/system roles).

        Delegates to ``core.connectors.mssql.objects.security.list_roles``.
        """
        return _mssql_security.list_roles(self._conn)

    def list_role_memberships(self) -> list[RoleMembershipDef]:
        """Discover database role memberships (excluding system principals).

        Delegates to
        ``core.connectors.mssql.objects.security.list_role_memberships``.
        """
        return _mssql_security.list_role_memberships(self._conn)

    # ------------------------------------------------------------------
    # Step 12 — Partition discovery
    # ------------------------------------------------------------------

    def list_partition_functions(self) -> list[PartitionFunctionDef]:
        """Return user partition functions with their boundaries.

        Delegates to ``objects.partition.list_partition_functions``.
        """
        return _mssql_partition.list_partition_functions(self._conn)

    def list_partition_schemes(self) -> list[PartitionSchemeDef]:
        """Return user partition schemes with their filegroup mappings.

        Delegates to ``objects.partition.list_partition_schemes``.
        """
        return _mssql_partition.list_partition_schemes(self._conn)

    def get_partitioned_tables(self) -> list[PartitionedTableDef]:
        """Return partitioned table/index metadata.

        Delegates to ``objects.partition.get_partitioned_tables``.
        """
        return _mssql_partition.get_partitioned_tables(self._conn, self._config)

    # ------------------------------------------------------------------
    # Step 15 — Comments / Extended Properties
    # ------------------------------------------------------------------

    def list_comments(self) -> list[CommentDef]:
        """Return extended properties (comments) for tables, columns, views, functions, schemas.

        Queries sys.extended_properties where name = 'MS_Description'.
        Supports: TABLE, VIEW, FUNCTION, PROCEDURE, SCHEMA, COLUMN.

        Delegates to ``core.connectors.mssql.objects.comment.discover_comments``.
        """
        return _mssql_comment.discover_comments(self._conn, self._config)

    # ------------------------------------------------------------------
    # Step 9 — Triggers (Phase 15 source discovery)
    # ------------------------------------------------------------------

    def get_all_triggers(self) -> list[TriggerDef]:
        """Return user DML triggers in the configured schemas.

        Delegates to ``core.connectors.mssql.objects.trigger.discover_triggers``.
        """
        return _mssql_trigger.discover_triggers(self._conn, self._config)
