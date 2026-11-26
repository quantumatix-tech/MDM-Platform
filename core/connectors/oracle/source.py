"""Oracle table discovery, metadata inspection, and batched extraction."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    CheckConstraint,
    Column,
    ForeignKey,
    FunctionDef,
    GrantDef,
    Index,
    MaterializedViewDef,
    RoleDef,
    RoleMembershipDef,
    Schema,
    SequenceDef,
    SourceConnector,
    TablePartition,
    TriggerDef,
    TypeDef,
    UniqueConstraint,
    UserDef,
    ViewDefinition,
    quote_identifier,
)
from core.connectors.oracle import objects as oracle_objects
from core.connectors.oracle import permissions as oracle_permissions
from core.connectors.oracle import precision as oracle_precision
from core.connectors.oracle._models import (
    OracleConnectionError,
    close_connection,
    is_transient_connection_error,
    make_connect_kwargs,
    oracle_error_code,
    safe_connection_error_message,
    validate_oracle_identifier,
    validate_oracle_virtual_expression,
)
from core.connectors.oracle.indexes import normalize_index_rows
from core.connectors.oracle.objects.type import serialize_object


def _validate_oracle_object_type(type_object: Any, expected_name: str) -> None:
    if not getattr(type_object, "iscollection", False) and not hasattr(type_object, "attributes"):
        raise RuntimeError(f"Oracle custom type {expected_name} is not an OBJECT or collection")


class OracleSourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        self._precision_safe_full_load = False
        self._schema_cache: dict[str, Schema] = {}
        self._permission_policy: dict[str, Any] | None = None
        self._permission_inventory: dict[str, Any] | None = None
        self._object_type_cache: dict[str, Any] = {}
        self._oracle_collection_kinds: dict[str, str] = {}

    def configure_permission_migration(self, policy: dict[str, Any]) -> None:
        """Install explicit root permission policy before source discovery."""
        self._permission_policy = policy
        self._permission_inventory = None

    def permission_scope_identity(self) -> dict[str, str]:
        """Return Oracle identity fields used to isolate TARGET provenance."""
        if self._conn is None:
            raise RuntimeError("Oracle source must be connected before scope inspection")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT USER, SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), "
                "SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
            )
            row = cur.fetchone()
        if row is None or len(row) != 4 or any(value is None for value in row):
            raise RuntimeError("Oracle source permission scope identity could not be verified")
        return dict(zip(("user", "db_unique_name", "container_name", "service_name"), map(str, row)))

    def discover_permission_inventory(self) -> dict[str, Any]:
        if self._permission_policy is None:
            raise RuntimeError("Oracle permission migration policy was not configured")
        if self._permission_inventory is None:
            self._permission_inventory = oracle_permissions.discover(
                self._conn, self._owner, self._permission_policy
            )
        return self._permission_inventory

    def list_grants(self) -> list[GrantDef]:
        if not self._permission_policy or self._permission_policy.get("enabled") is not True:
            return []
        return list(self.discover_permission_inventory()["grants"])

    def list_roles(self) -> list[RoleDef]:
        if not self._permission_policy or self._permission_policy.get("enabled") is not True:
            return []
        return list(self.discover_permission_inventory()["roles"])

    def list_users(self) -> list[UserDef]:
        if not self._permission_policy or self._permission_policy.get("enabled") is not True:
            return []
        return list(self.discover_permission_inventory()["users"])

    def list_role_memberships(self) -> list[RoleMembershipDef]:
        if not self._permission_policy or self._permission_policy.get("enabled") is not True:
            return []
        return list(self.discover_permission_inventory()["memberships"])

    def enable_precision_safe_full_load(self) -> None:
        """Use lossless Oracle NUMBER and temporal transport for Oracle FULL."""
        self._precision_safe_full_load = True
        for schema in self._schema_cache.values():
            schema.options["oracle_precision_safe_transport"] = True

    def connect(self) -> None:
        import oracledb

        kwargs = make_connect_kwargs(self._config, oracledb)
        retries = int(self._config.get("connection_max_retries", 3))
        if retries < 0:
            raise ValueError("Oracle connection_max_retries cannot be negative")
        delay = float(self._config.get("connection_retry_delay", 1.0))
        if delay < 0:
            raise ValueError("Oracle connection_retry_delay cannot be negative")

        for attempt in range(retries + 1):
            original_fetch_decimals = oracledb.defaults.fetch_decimals
            try:
                oracledb.defaults.fetch_decimals = True
                try:
                    self._conn = oracledb.connect(**kwargs)
                finally:
                    oracledb.defaults.fetch_decimals = original_fetch_decimals
                audit_log(
                    phase="connect",
                    status="success",
                    details={"engine": "oracle", "role": "source"},
                )
                return
            except oracledb.Error as exc:
                if not is_transient_connection_error(exc) or attempt >= retries:
                    # Do not include driver exception text: it can contain DSN/user details.
                    audit_log(
                        phase="connect",
                        status="failed",
                        details={
                            "engine": "oracle",
                            "role": "source",
                            "error_code": oracle_error_code(exc),
                        },
                    )
                    raise OracleConnectionError(safe_connection_error_message(exc)) from None
                audit_log(
                    phase="connect",
                    status="retrying",
                    details={
                        "engine": "oracle",
                        "role": "source",
                        "attempt": attempt + 1,
                        "error_code": oracle_error_code(exc),
                    },
                )
                time.sleep(delay * (2**attempt))

    def close(self) -> None:
        conn, self._conn = self._conn, None
        close_connection(conn)

    def list_objects(self) -> list[str]:
        include_schemas = self._config.get("include_schemas")
        if include_schemas:
            normalized = {str(owner).casefold() for owner in include_schemas}
            if normalized != {self._owner.casefold()}:
                raise ValueError(
                    "Oracle source discovery is scoped to the connected username/schema owner; "
                    "migration.include_schemas must contain only that owner"
                )
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT t.table_name FROM user_tables t "
                "WHERE t.nested = 'NO' AND NOT EXISTS ("
                "SELECT 1 FROM user_mviews m WHERE m.mview_name = t.table_name) "
                "ORDER BY t.table_name"
            )
            names = [row[0] for row in cur.fetchall()]
        for name in names:
            validate_oracle_identifier(name, "table")
        include_tables = self._config.get("include_tables")
        if include_tables is not None:
            if not isinstance(include_tables, (list, tuple)) or not include_tables:
                raise ValueError("Oracle source include_tables must be a non-empty list")
            normalized_filter: set[str] = set()
            for table_name in include_tables:
                validate_oracle_identifier(table_name, "table")
                key = table_name.upper()
                if key in normalized_filter:
                    raise ValueError("Oracle source include_tables contains duplicate names")
                normalized_filter.add(key)
            available = {name.upper() for name in names}
            missing = sorted(normalized_filter - available)
            if missing:
                raise ValueError(
                    f"Configured Oracle source include_tables were not found: {', '.join(missing)}"
                )
            names = [name for name in names if name.upper() in normalized_filter]
        return names

    def validate_constraint_states(self, table_names: list[str]) -> None:
        """Fail before target writes when Oracle constraint state cannot be preserved."""
        selected = {str(name).upper() for name in table_names}
        if not selected:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, constraint_name, status, validated, deferrable, deferred "
                "FROM user_constraints WHERE constraint_type IN ('P','U','R','C')"
            )
            rows = cur.fetchall()
        for table, name, status, validated, deferrable, deferred in rows:
            if str(table).upper() not in selected:
                continue
            state = (
                str(status or "").upper(),
                str(validated or "").upper(),
                str(deferrable or "").upper(),
                str(deferred or "").upper(),
            )
            if state == ("ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"):
                continue
            initial_timing = (
                f"INITIALLY {state[3]}" if state[2] == "DEFERRABLE" else "NOT DEFERRABLE"
            )
            raise ValueError(
                f"Oracle constraint {table}.{name} has unsupported state: "
                f"{state[0] or 'UNKNOWN'}, {state[1] or 'UNKNOWN'}, {initial_timing}; "
                "FULL migration supports only ENABLED VALIDATED NOT DEFERRABLE constraints"
            )

    def validate_schema_replacement_grants(self) -> None:
        """Reject owner-issued grants because Oracle FULL does not migrate them."""
        if self._permission_policy and self._permission_policy.get("enabled") is True:
            self.discover_permission_inventory()
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT grantee, owner, table_name, grantor, privilege, grantable, "
                    "hierarchy, common, type, inherited FROM all_tab_privs_made "
                    "WHERE owner = :1 ORDER BY table_name, grantee, privilege",
                    (self._owner,),
                )
                object_grants = []
                for row in cur.fetchall():
                    if len(row) != 10:
                        raise RuntimeError("Malformed ALL_TAB_PRIVS_MADE row")
                    grantee, owner, table_name, _grantor, privilege, _grantable, _hierarchy, _common, object_type, _inherited = row
                    if str(owner).casefold() != self._owner.casefold():
                        raise RuntimeError("ALL_TAB_PRIVS_MADE returned a row outside the connected owner")
                    if str(object_type).upper() == "USER" or str(privilege).upper() == "INHERIT PRIVILEGES":
                        continue
                    object_grants.append((table_name, grantee, privilege, object_type))
                cur.execute(
                    "SELECT grantee, owner, table_name, column_name, grantor, privilege, "
                    "grantable, common, inherited FROM all_col_privs_made "
                    "WHERE owner = :1 ORDER BY table_name, grantee, privilege",
                    (self._owner,),
                )
                column_grants = []
                for row in cur.fetchall():
                    if len(row) != 9:
                        raise RuntimeError("Malformed ALL_COL_PRIVS_MADE row")
                    _grantee, owner, table_name, column_name, _grantor, privilege, _grantable, _common, _inherited = row
                    if str(owner).casefold() != self._owner.casefold():
                        raise RuntimeError("ALL_COL_PRIVS_MADE returned a row outside the connected owner")
                    column_grants.append((table_name, column_name, privilege))
        except Exception as exc:  # noqa: BLE001 - Normalize database-driver failures into actionable diagnostics.
            raise RuntimeError(
                "Source grant inventory unavailable (ALL_TAB_PRIVS_MADE/ALL_COL_PRIVS_MADE); "
                f"schema replacement is not approvable: {type(exc).__name__}: {exc}"
            ) from None
        if object_grants or column_grants:
            raise RuntimeError(
                "Oracle schema replacement does not support source object grants; "
                "remove or separately approve them before migration"
            )

    def validate_schema_replacement_objects(self) -> None:
        """Reject owner objects that Oracle FULL cannot discover and recreate."""
        supported = (
            "'TABLE','INDEX','VIEW','MATERIALIZED VIEW','SEQUENCE','TRIGGER','FUNCTION',"
            "'PROCEDURE','PACKAGE','PACKAGE BODY','SYNONYM','TYPE'"
        )
        internal = (
            "'TABLE PARTITION','TABLE SUBPARTITION','INDEX PARTITION',"
            "'INDEX SUBPARTITION','LOB','LOB PARTITION','LOB SUBPARTITION'"
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT object_name, object_type FROM user_objects "
                "WHERE generated = 'N' "
                f"AND object_type NOT IN ({supported}) "
                f"AND object_type NOT IN ({internal}) "
                "ORDER BY object_type, object_name"
            )
            unsupported = [(str(name), str(kind)) for name, kind in cur.fetchall()]
        if unsupported:
            detail = ", ".join(f"{kind} {name}" for name, kind in unsupported)
            raise RuntimeError(
                "Oracle FULL cannot replace unsupported source-owner object(s): " + detail
            )
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, table_type FROM user_object_tables ORDER BY table_name"
            )
            object_tables = cur.fetchall()
            cur.execute(
                "SELECT table_name, parent_table_name FROM user_nested_tables "
                "ORDER BY table_name"
            )
            nested_tables = cur.fetchall()
        if object_tables or nested_tables:
            names = [
                f"object table {table} OF {type_name}"
                for table, type_name in object_tables
            ] + [
                f"nested-table storage {table} for {parent}"
                for table, parent in nested_tables
            ]
            raise RuntimeError(
                "Oracle FULL cannot safely transport object or nested-table values: "
                + ", ".join(names)
            )

    def list_views(self) -> list[ViewDefinition]:
        """Discover ordinary owner-scoped views in dependency order."""
        selected_tables = self._selected_tables
        if selected_tables is None:
            return oracle_objects.discover_views(self._conn, self._owner)
        # A view is in migration scope only when every table dependency is
        # selected. Views depending on other views are retained transitively.
        return oracle_objects.discover_views(
            self._conn, self._owner, selected_tables=selected_tables
        )

    def list_comments(self) -> list[Any]:
        return oracle_objects.discover_comments(self._conn, self._owner)

    def list_synonyms(self) -> list[Any]:
        return oracle_objects.discover_synonyms(self._conn, self._owner)

    def list_materialized_views(self) -> list[MaterializedViewDef]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT mview_name, query, build_mode, refresh_mode, refresh_method, rewrite_enabled "
                "FROM user_mviews ORDER BY mview_name"
            )
            rows = cur.fetchall()
        ordered_names = [name for _, name in oracle_objects._dependency_order(
            self._conn, self._owner,
            {"MATERIALIZED VIEW": {str(row[0]) for row in rows}},
        )]
        rows_by_name = {str(row[0]): row for row in rows}
        result = []
        for name in ordered_names:
            row = rows_by_name[name]
            name, query, build, mode, method, rewrite = row
            oracle_objects._check_local_dependencies(
                self._conn, self._owner, str(name), "MATERIALIZED VIEW"
            )
            values = tuple(str(value or "").upper() for value in (build, mode, method, rewrite))
            if values != ("IMMEDIATE", "DEMAND", "COMPLETE", "N"):
                raise RuntimeError(
                    f"Oracle FULL does not support materialized view {name} configuration "
                    f"BUILD_MODE={values[0]}, REFRESH_MODE={values[1]}, "
                    f"REFRESH_METHOD={values[2]}, REWRITE_ENABLED={values[3]}; "
                    "supported: BUILD IMMEDIATE, REFRESH COMPLETE ON DEMAND, REWRITE DISABLED"
                )
            definition = str(query.read() if hasattr(query, "read") else query).strip().rstrip(";")
            result.append(MaterializedViewDef(
                name=str(name), definition=definition, schema_name=self._owner,
                build_mode=values[0], refresh_mode=values[1], refresh_method=values[2],
                rewrite_enabled=values[3],
            ))
        return result

    def materialized_view_query_rows(self, mv: MaterializedViewDef) -> list[tuple[Any, ...]]:
        """Read the source query result used to verify stored target MV contents."""
        definition = oracle_objects._remap_owner_qualifiers(mv.definition, mv.schema_name, self._owner)
        with self._conn.cursor() as cur:
            cur.execute(definition)
            return list(cur.fetchall())

    def list_all_sequences(self) -> list[SequenceDef]:
        return oracle_objects.discover_sequences(self._conn, self._owner)

    def list_functions(self) -> list[FunctionDef]:
        return oracle_objects.discover_routines(self._conn, self._owner)

    def list_types(self) -> list[TypeDef]:
        """Return supported Oracle object and collection types in dependency order."""
        types = oracle_objects.discover_types(self._conn, self._owner)
        self._oracle_collection_kinds = {
            str(item.name).casefold(): item.kind.casefold()
            for item in types if item.kind.casefold() in {"varray", "nested_table"}
        }
        return types

    def get_all_triggers(self) -> list[TriggerDef]:
        selected_tables = self._selected_tables
        return oracle_objects.discover_triggers(
            self._conn, self._owner, selected_tables=selected_tables
        )

    @property
    def _selected_tables(self) -> set[str] | None:
        include_tables = self._config.get("include_tables")
        return None if include_tables is None else {str(name).upper() for name in include_tables}

    @property
    def _owner(self) -> str:
        owner = self._config.get("username")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Oracle username is required to scope schema discovery")
        return owner.strip().upper()

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_oracle_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {quote_identifier(object_name)}")
            return int(cur.fetchone()[0])

    def partition_row_counts(self, table_name: str) -> dict[str, int]:
        validate_oracle_identifier(table_name, "table")
        partitioning = oracle_objects.discover_table_partitioning(self._conn, table_name)
        if partitioning is None:
            return {}
        counts: dict[str, int] = {}
        with self._conn.cursor() as cur:
            for part in partitioning["partitions"]:
                validate_oracle_identifier(part["name"], "partition")
                cur.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table_name)} "
                    f"PARTITION ({quote_identifier(part['name'])})"
                )
                counts[part["name"]] = int(cur.fetchone()[0])
        return counts

    def export_full(
        self, object_name: str, schema_name: str | None = None
    ) -> Iterator[dict[str, Any]]:
        import oracledb

        validate_oracle_identifier(object_name, "table")
        schema = self._schema_cache.get(object_name)
        if self._precision_safe_full_load and schema is None:
            schema = self.get_schema(object_name)
        columns = schema.columns if schema is not None else []
        if self._precision_safe_full_load and not columns:
            raise RuntimeError(
                f"Oracle precision-safe extraction cannot inspect columns for table {object_name!r}"
            )
        select_sql = "*"
        if self._precision_safe_full_load:
            select_sql = ", ".join(
                oracle_precision.transport_select_expression(column) for column in columns
            )
        with self._conn.cursor() as cur:
            cur.arraysize = int(self._config.get("fetch_batch_size", 1000))
            if self._precision_safe_full_load:
                cur.outputtypehandler = oracle_precision.decimal_output_type_handler(oracledb)
            object_types = schema.options.get("oracle_object_types", {}) if schema else {}
            for metadata in object_types.values():
                owner = validate_oracle_identifier(str(metadata["owner"]), "type owner")
                type_name = validate_oracle_identifier(str(metadata["name"]), "type")
                cur.execute(
                    "SELECT typecode FROM all_types WHERE owner = :1 AND type_name = :2",
                    (owner, type_name),
                )
                row = cur.fetchone()
                if not row or str(row[0]).upper() not in {"OBJECT", "COLLECTION"}:
                    raise RuntimeError(
                        f"Oracle source custom type {owner}.{type_name} is unavailable"
                    )
                type_key = f"{owner}.{type_name}"
                if type_key not in self._object_type_cache:
                    self._object_type_cache[type_key] = self._conn.gettype(type_key)
                _validate_oracle_object_type(self._object_type_cache[type_key], type_name)
            order_by = ""
            order_columns = (
                schema.primary_key if schema and schema.primary_key else [
                    column.name for column in columns
                    if not oracle_precision.oracle_lob_kind(column.source_type)
                ]
            )
            if self._precision_safe_full_load and order_columns:
                order_by = " ORDER BY " + ", ".join(
                    quote_identifier(name) for name in order_columns
                )
            cur.execute(
                f"SELECT {select_sql} FROM {quote_identifier(object_name)}{order_by}"
            )
            names = [desc[0] for desc in cur.description]
            lob_kinds = {
                column.name: oracle_precision.oracle_lob_kind(column.source_type)
                for column in columns
            }
            while True:
                batch = cur.fetchmany(cur.arraysize)
                if not batch:
                    break
                for row in batch:
                    materialized = {
                        name: self._materialize(
                            value,
                            lob_kinds.get(name) if self._precision_safe_full_load else None,
                            spool_lob=self._precision_safe_full_load,
                        )
                        for name, value in zip(names, row)
                    }
                    for column_name in object_types:
                        metadata = object_types[column_name]
                        type_key = f"{metadata['owner']}.{metadata['name']}"
                        materialized[column_name] = serialize_object(
                            materialized.get(column_name), self._object_type_cache[type_key],
                            self._oracle_collection_kinds,
                        )
                    yield materialized

    @staticmethod
    def _materialize(
        value: Any, lob_kind: str | None = None, *, spool_lob: bool = False
    ) -> Any:
        # python-oracledb returns LOB locators for large text/binary values.
        if hasattr(value, "read") and callable(value.read):
            # Fetched locators returned by SQL expressions (including
            # XMLSERIALIZE) can be unopened/read-only. Calling LOB.close() on
            # these locators issues a database operation and raises ORA-22289
            # or invalidates a related locator (ORA-64219). Materialize the
            # value and let cursor/row scope release the fetched locator; this
            # object does not own the connector connection.
            if spool_lob and lob_kind:
                return oracle_precision.OracleLobPayload.from_lob(value, lob_kind)
            return value.read()
        return value

    def get_schema(self, object_name: str) -> Schema:
        validate_oracle_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, data_length, char_length, char_used, "
                "data_precision, data_scale, nullable, data_default, identity_column, virtual_column "
                "FROM user_tab_cols WHERE table_name = :1 AND hidden_column = 'NO' "
                "ORDER BY column_id",
                (object_name,),
            )
            column_rows = cur.fetchall()
            if not column_rows:
                raise ValueError(f"Oracle table {object_name!r} was not found for owner {self._owner}")

            native_types = {
                "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW", "NUMBER", "FLOAT",
                "BINARY_FLOAT", "BINARY_DOUBLE", "DATE", "TIMESTAMP",
                "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE",
                "BLOB", "CLOB", "NCLOB", "JSON", "XMLTYPE", "BFILE", "LONG",
                "LONG RAW", "ROWID", "UROWID", "INTERVAL YEAR TO MONTH",
                "INTERVAL DAY TO SECOND",
            }
            candidate_type_names = {
                str(row[1]).upper() for row in column_rows
                if str(row[1]).upper() not in native_types
            }
            if candidate_type_names:
                cur.execute(
                    "SELECT c.column_name, c.data_type_owner, c.data_type, t.typecode "
                    "FROM user_tab_columns c LEFT JOIN user_types t "
                    "ON t.type_name = c.data_type "
                    "WHERE c.table_name = :1 AND c.data_type_owner IS NOT NULL",
                    (object_name,),
                )
                object_type_rows = cur.fetchall()
            else:
                object_type_rows = []
            object_types: dict[str, tuple[str, str, str]] = {}
            for column_name, type_owner, type_name, typecode in object_type_rows:
                if str(type_owner).upper() != self._owner:
                    raise RuntimeError(
                        f"Oracle custom TYPE column {object_name}.{column_name} references "
                        f"external owner {type_owner}; cross-owner TYPE migration is unsupported"
                    )
                if str(typecode or "").upper() not in {"OBJECT", "COLLECTION"}:
                    raise RuntimeError(
                        f"Oracle custom TYPE column {object_name}.{column_name} has unsupported "
                        f"type category {typecode or 'UNKNOWN'}"
                    )
                object_types[str(column_name)] = (
                    str(type_owner), str(type_name), str(typecode).upper()
                )

            xml_columns = {
                str(row[0]) for row in column_rows
                if str(row[1]).upper() == "XMLTYPE"
            }
            xml_storage: dict[str, dict[str, Any]] = {}
            if xml_columns:
                cur.execute(
                    "SELECT column_name, xmlschema, schema_owner, element_name, "
                    "storage_type, anyschema, nonschema FROM user_xml_tab_cols "
                    "WHERE table_name = :1",
                    (object_name,),
                )
                xml_storage = {
                    str(row[0]): {
                        "xmlschema": row[1], "schema_owner": row[2],
                        "element_name": row[3], "storage_type": row[4],
                        "anyschema": row[5], "nonschema": row[6],
                    }
                    for row in cur.fetchall() if str(row[0]) in xml_columns
                }

            columns: list[Column] = []
            char_semantics: dict[str, str] = {}
            identity_options: dict[str, str] = {}
            identity_columns = [row[0] for row in column_rows if row[9] == "YES"]
            if identity_columns:
                cur.execute(
                    "SELECT column_name, generation_type, identity_options "
                    "FROM user_tab_identity_cols "
                    "WHERE table_name = :1",
                    (object_name,),
                )
                identity_options = {
                    str(name): (str(generation), str(options))
                    for name, generation, options in cur.fetchall()
                }
            ordinary_column_names = {
                str(row[0]) for row in column_rows if row[10] != "YES"
            }
            for name, typ, data_length, char_length, char_used, precision, scale, nullable, default, identity, virtual in column_rows:
                generated_expression = None
                source_type = str(typ).upper()
                object_type = object_types.get(str(name))
                if object_type is not None:
                    source_type = object_type[1]
                # XMLType pseudo columns are exposed as virtual in some Oracle
                # releases although they are not user generated expressions.
                # Preserve actual user virtual validation for every other type.
                if virtual == "YES" and source_type != "XMLTYPE":
                    try:
                        generated_expression = validate_oracle_virtual_expression(
                            default, ordinary_column_names
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"Oracle virtual column {object_name}.{name} has an unsupported expression: {exc}"
                        ) from None
                size = char_length if source_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"} else data_length
                if char_used in {"B", "C"}:
                    char_semantics[name] = "BYTE" if char_used == "B" else "CHAR"
                identity_kind, options = identity_options.get(str(name), (None, ""))
                seed_match = re.search(r"START WITH:\s*(-?\d+)", options, re.IGNORECASE)
                increment_match = re.search(r"INCREMENT BY:\s*(-?\d+)", options, re.IGNORECASE)
                columns.append(Column(
                    name=name,
                    source_type=source_type,
                    target_type=(
                        validate_oracle_identifier(object_type[1], "type")
                        if object_type is not None else None
                    ),
                    nullable=nullable == "Y",
                    size=int(size) if size is not None else None,
                    precision=int(precision) if precision is not None else None,
                    scale=int(scale) if scale is not None else None,
                    default=(
                        str(default).strip()
                        if default is not None and virtual != "YES" else None
                    ),
                    generated=generated_expression,
                    is_identity=identity == "YES",
                    identity_seed=int(seed_match.group(1)) if seed_match else None,
                    identity_increment=int(increment_match.group(1)) if increment_match else None,
                    identity_kind=(
                        "ALWAYS"
                        if identity == "YES" and str(identity_kind).upper() == "ALWAYS"
                        else "BY DEFAULT" if identity == "YES" else None
                    ),
                    generated_kind="VIRTUAL" if generated_expression is not None else None,
                ))

            cur.execute(
                "SELECT c.constraint_name, c.constraint_type, cc.column_name, "
                "cc.position, c.r_owner, c.r_constraint_name, c.delete_rule, "
                "c.search_condition_vc, c.generated "
                "FROM user_constraints c JOIN user_cons_columns cc "
                "ON c.constraint_name = cc.constraint_name "
                "WHERE c.table_name = :1 AND c.constraint_type IN ('P','U','R','C') "
                "ORDER BY c.constraint_name, cc.position",
                (object_name,),
            )
            rows = cur.fetchall()
            groups: dict[str, list[tuple[Any, ...]]] = {}
            for row in rows:
                groups.setdefault(row[0], []).append(row)

            primary_key: list[str] = []
            primary_key_name = None
            unique_constraints: list[UniqueConstraint] = []
            foreign_keys: list[ForeignKey] = []
            checks: list[CheckConstraint] = []
            generated_check_names: set[str] = set()
            for constraint_name, constraint_rows in groups.items():
                kind = constraint_rows[0][1]
                if kind == "P":
                    primary_key_name = constraint_name
                    primary_key = [row[2] for row in constraint_rows]
                elif kind == "U":
                    unique_constraints.append(UniqueConstraint(
                        constraint_name, [row[2] for row in constraint_rows]
                    ))
                elif kind == "C":
                    expression = constraint_rows[0][7]
                    if expression:
                        checks.append(CheckConstraint(constraint_name, str(expression)))
                        if (
                            len(constraint_rows[0]) > 8
                            and str(constraint_rows[0][8]).upper() == "GENERATED NAME"
                        ):
                            generated_check_names.add(str(constraint_name))
                elif kind == "R":
                    first = constraint_rows[0]
                    ref_owner, ref_constraint = first[4], first[5]
                    cur.execute(
                        "SELECT table_name FROM all_constraints "
                        "WHERE owner = :1 AND constraint_name = :2",
                        (ref_owner, ref_constraint),
                    )
                    ref_row = cur.fetchone()
                    if not ref_row:
                        raise ValueError(
                            f"Oracle foreign key {constraint_name} references a table "
                            "not visible to the source account"
                        )
                    cur.execute(
                        "SELECT column_name FROM all_cons_columns "
                        "WHERE owner = :1 AND constraint_name = :2 ORDER BY position",
                        (ref_owner, ref_constraint),
                    )
                    ref_columns = [r[0] for r in cur.fetchall()]
                    foreign_keys.append(ForeignKey(
                        name=constraint_name,
                        columns=[row[2] for row in constraint_rows],
                        ref_table=ref_row[0],
                        ref_columns=ref_columns,
                        ref_schema=self._owner if str(ref_owner).upper() == self._owner else str(ref_owner),
                        on_delete=first[6] or "NO ACTION",
                    ))

            cur.execute(
                "SELECT i.index_name, i.uniqueness, i.index_type, i.status, "
                "i.partitioned, ic.column_name, ic.column_position, ic.descend, "
                "ie.column_expression "
                "FROM user_indexes i "
                "JOIN user_ind_columns ic ON ic.index_name = i.index_name "
                "AND ic.table_name = i.table_name "
                "LEFT JOIN user_ind_expressions ie ON ie.index_name = i.index_name "
                "AND ie.table_name = i.table_name "
                "AND ie.column_position = ic.column_position "
                "WHERE i.table_name = :1 AND NOT EXISTS ("
                "SELECT 1 FROM user_lobs l WHERE l.table_name = i.table_name "
                "AND l.index_name = i.index_name) AND NOT EXISTS ("
                "SELECT 1 FROM user_constraints c WHERE c.table_name = i.table_name "
                "AND c.index_name = i.index_name AND c.constraint_type IN ('P','U')) "
                "ORDER BY i.index_name, ic.column_position",
                (object_name,),
            )
            index_rows = cur.fetchall()
            index_column_types = {
                str(column.name): str(column.source_type) for column in columns
            }
            normalized_indexes = normalize_index_rows(index_rows, index_column_types)
            indexes: list[Index] = []
            for index_name, definition in normalized_indexes.items():
                if not definition["supported"]:
                    reason = (
                        f"type={definition['catalog_index_type']}, status={definition['status']}, "
                        f"partitioned={definition['partitioned']}, "
                        f"functional={definition['functional']}, reason={definition['reason']}"
                    )
                    raise ValueError(
                        f"Oracle index {index_name!r} on {object_name!r} is unsupported "
                        f"for FULL migration ({reason}); only valid, nonpartitioned "
                        "column-key B-tree indexes and Oracle's intrinsic timestamp/descending "
                        "representations are supported"
                    )
                indexes.append(Index(
                    name=index_name,
                    columns=definition["columns"],
                    unique=definition["unique"],
                    index_type="NORMAL",
                    descending_columns=definition["descending_columns"],
                ))

        partitioning = oracle_objects.discover_table_partitioning(self._conn, object_name)
        if partitioning:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT i.index_name FROM user_indexes i JOIN user_constraints c "
                    "ON c.table_name=i.table_name AND c.index_name=i.index_name "
                    "WHERE i.table_name=:1 AND i.partitioned='YES' "
                    "AND c.constraint_type IN ('P','U') ORDER BY i.index_name",
                    (object_name,),
                )
                partitioned_constraint_indexes = [str(row[0]) for row in cur.fetchall()]
            if partitioned_constraint_indexes:
                raise RuntimeError(
                    f"Oracle FULL cannot preserve partitioned constraint index(es) on "
                    f"{object_name}: {', '.join(partitioned_constraint_indexes)}"
                )
        schema = Schema(
            name=object_name,
            schema_name=self._owner,
            columns=columns,
            primary_key=primary_key,
            primary_key_name=primary_key_name,
            indexes=indexes,
            unique_constraints=unique_constraints,
            foreign_keys=foreign_keys,
            check_constraints=checks,
            options={
                "oracle_char_semantics": char_semantics,
                "oracle_object_types": {
                    column: {"owner": owner, "name": type_name, "typecode": typecode}
                    for column, (owner, type_name, typecode) in object_types.items()
                },
                "oracle_generated_check_constraints": generated_check_names,
                **({"oracle_xml_storage": xml_storage} if xml_columns else {}),
                **({"oracle_precision_safe_transport": True} if self._precision_safe_full_load else {}),
                **({"oracle_partitioning": partitioning} if partitioning else {}),
            },
            partition_method=partitioning["method"] if partitioning else None,
            partition_key=(
                f"{partitioning['method']} ("
                + ", ".join(partitioning["keys"])
                + ")" if partitioning else None
            ),
            partitions=[
                TablePartition(name=item["name"], description=item["high_value"])
                for item in partitioning["partitions"]
            ] if partitioning else [],
        )
        # Keep an independent source-column snapshot. The orchestrator may
        # rename the Schema for target field mappings after discovery.
        self._schema_cache[object_name] = deepcopy(schema)
        return schema
