from __future__ import annotations

import abc
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Allows a double-quoted identifier containing any characters except NUL,
# with internal double-quotes escaped as "" (SQL standard).
_QUOTED_IDENTIFIER_RE = re.compile(r'^"([^"\x00]|"")*"$')


class UnmappedTypeError(ValueError):
    """Raised when a cross-engine column type has no target representation."""

    def __init__(
        self,
        *,
        table: str,
        column: str,
        source_type: str,
        source_engine: str | None,
        target_engine: str,
    ) -> None:
        source_label = source_engine or "unknown"
        super().__init__(
            "Unmapped cross-engine column type: "
            f"table={table!r}, column={column!r}, source_type={source_type!r}, "
            f"source_engine={source_label!r}, target_engine={target_engine!r}. "
            "Configure a target type mapping before running the migration."
        )


# ---------------------------------------------------------------------------
# Column & Schema dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Column:
    name: str
    source_type: str
    target_type: str | None = None
    nullable: bool = True
    size: int | None = None
    default: str | None = None          # column default expression (non-sequence)
    generated: str | None = None        # GENERATED ALWAYS AS (expr) STORED expression
    generated_kind: str | None = None   # engine-specific storage mode, e.g. VIRTUAL/STORED
    auto_increment: bool = False
    comment: str | None = None


@dataclass
class Index:
    name: str
    columns: list[str]
    unique: bool = False
    ddl: str | None = None              # full DDL from pg_get_indexdef (handles partial/expression)
    index_type: str | None = None       # e.g. BTREE, FULLTEXT, SPATIAL


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    ref_schema: str = "public"
    on_delete: str = "NO ACTION"
    on_update: str = "NO ACTION"


@dataclass
class CheckConstraint:
    name: str
    expression: str


@dataclass
class Schema:
    name: str
    schema_name: str = "public"
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    type_map_hints: dict[str, str] = field(default_factory=dict)
    indexes: list[Index] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    check_constraints: list[CheckConstraint] = field(default_factory=list)
    sequences: list[str] = field(default_factory=list)     # column names backed by sequences
    rls_enabled: bool = False
    partition_key: str | None = None    # e.g. "RANGE (created_at)" for partitioned tables
    mysql_partition_method: str | None = None
    mysql_partition_expression: str | None = None
    mysql_partitions: list["MySQLPartitionDef"] = field(default_factory=list)
    comment: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Full-database object dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PartitionDef:
    """A child partition table (PARTITION OF parent_table ...)."""
    name: str
    parent_table: str
    bound: str          # e.g. "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"


@dataclass
class MySQLPartitionDef:
    """One ordered partition from INFORMATION_SCHEMA.PARTITIONS."""
    name: str
    description: str | None = None


@dataclass
class SequenceDef:
    """A PostgreSQL sequence — standalone or column-owned."""
    name: str
    start_value: int
    min_value: int
    max_value: int
    increment: int
    cycle: bool
    last_value: int | None = None
    owned_by: str | None = None    # e.g. "orders.id" if column-owned
    schema: str | None = None      # source schema; None means "public" or unknown


@dataclass
class ExtensionDef:
    """A PostgreSQL extension (e.g. uuid-ossp, pgcrypto, PostGIS)."""
    name: str
    schema: str = "public"


@dataclass
class SchemaDef:
    """A non-public PostgreSQL schema."""
    name: str


@dataclass
class TypeDef:
    """A user-defined type: ENUM, DOMAIN, or COMPOSITE.
    ddl contains the complete CREATE TYPE / CREATE DOMAIN statement."""
    name: str
    kind: str           # 'enum', 'domain', 'composite'
    ddl: str            # complete CREATE DDL — ready to execute


@dataclass
class ViewDefinition:
    """A regular SQL view."""
    name: str
    definition: str     # raw SELECT definition
    schema_name: str = "public"


@dataclass
class MaterializedViewDef:
    """A materialized view — stored like a table, refreshed on demand."""
    name: str
    definition: str     # raw SELECT definition
    schema_name: str = "public"


@dataclass
class FunctionDef:
    """A function or stored procedure.
    ddl contains the complete CREATE OR REPLACE FUNCTION / PROCEDURE statement."""
    name: str
    ddl: str            # complete DDL from pg_get_functiondef — ready to execute
    schema_name: str = "public"
    kind: str = "function"             # function or procedure


@dataclass
class TriggerDef:
    """A trigger on a table.
    ddl contains the complete CREATE TRIGGER statement."""
    name: str
    table: str
    ddl: str            # complete DDL from pg_get_triggerdef — ready to execute
    schema_name: str = "public"


@dataclass
class EventDef:
    """A scheduled database event (native to MySQL/MariaDB)."""
    name: str
    ddl: str
    schema_name: str = "public"
    event_type: str | None = None
    status: str | None = None
    execute_at: datetime | None = None
    interval_value: str | None = None
    interval_field: str | None = None
    starts: datetime | None = None
    ends: datetime | None = None
    on_completion: str | None = None
    time_zone: str | None = None
    definer: str | None = None
    snapshot_at: datetime | None = None
    safety_lead_seconds: int = 300


@dataclass
class RLSPolicy:
    """A Row-Level Security policy on a table."""
    name: str
    table: str
    cmd: str            # SELECT, INSERT, UPDATE, DELETE, ALL
    permissive: str     # PERMISSIVE or RESTRICTIVE
    using_expr: str | None = None
    check_expr: str | None = None
    schema_name: str = "public"


@dataclass
class CommentDef:
    """A COMMENT ON ... IS '...' statement."""
    object_type: str    # TABLE, COLUMN, VIEW, MATERIALIZED VIEW, FUNCTION, etc.
    object_name: str    # For columns: 'table.column'; schema is tracked separately
    comment: str
    schema_name: str = "public"


@dataclass
class GrantDef:
    """A GRANT privilege statement."""
    privileges: str     # SELECT, INSERT, ALL, etc.
    object_type: str    # TABLE, SEQUENCE, FUNCTION, SCHEMA, COLUMN
    object_name: str    # schema-qualified when applicable
    grantee: str
    schema_name: str = "public"
    grant_option: bool = False


@dataclass
class SecurityPrincipalDef:
    """A database account or role; authentication secrets are never included."""
    user: str
    host: str
    principal_type: str  # USER or ROLE
    authentication_plugin: str | None = None
    account_locked: bool | None = None
    password_expired: bool | None = None


@dataclass
class RoleMembershipDef:
    """A MySQL role edge from role to either a user or another role."""
    role_user: str
    role_host: str
    grantee_user: str
    grantee_host: str
    with_admin_option: bool = False


# ---------------------------------------------------------------------------
# CDC / result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class UpsertResult:
    success_count: int = 0
    failure_count: int = 0
    errors: list[str] = field(default_factory=list)
    failed_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ApplyResult:
    success_count: int = 0
    failure_count: int = 0
    errors: list[str] = field(default_factory=list)
    last_checkpoint: Any = None


@dataclass
class ChangeEvent:
    operation: str
    document: dict[str, Any]
    object_name: str = ""
    schema: Schema | None = None
    watermark: Any = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate_identifier(name: str, kind: str = "object") -> str:
    """Validate a SQL identifier — accepts both plain and double-quoted names.

    Fix #2: The original regex only allowed plain alphanumeric names, silently
    rejecting valid quoted identifiers like ``"Order-Items"`` or ``"mySchema"``.

    Accepted forms:
    * Plain:  ``table_name``, ``_my_col123``
    * Quoted: ``"Table-Name"``, ``"has ""internal"" quotes"``

    Returns the identifier unchanged.  Raises ValueError for anything else
    (e.g., bare names with hyphens that are NOT quoted).
    """
    if IDENTIFIER_RE.match(name) or _QUOTED_IDENTIFIER_RE.match(name):
        return name
    raise ValueError(
        f"Invalid {kind} name {name!r}. Must be a plain identifier "
        f"(^[a-zA-Z_][a-zA-Z0-9_]*$) or a double-quoted identifier "
        f'(e.g. "has-hyphens").'
    )


def quote_identifier(name: str) -> str:
    """Wrap *name* in double-quotes, escaping any internal double-quotes.

    Use this instead of bare f-strings when building dynamic SQL with names
    that may contain hyphens, spaces, or reserved words.

    Examples::

        quote_identifier("orders")           # → '"orders"'
        quote_identifier('"already-quoted"') # → '"already-quoted"' (no double-wrap)
        quote_identifier("Order Items")      # → '"Order Items"'
    """
    # If the caller already passed a correctly quoted identifier, return as-is.
    if _QUOTED_IDENTIFIER_RE.match(name):
        return name
    # Escape internal double-quotes (SQL standard: "" → literal ")
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Abstract connector interfaces
# ---------------------------------------------------------------------------

class SourceConnector(abc.ABC):
    """Reads data and schema from the source database."""

    @abc.abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:
        """Release connector resources when a migration run finishes."""
        return None

    @abc.abstractmethod
    def list_objects(self) -> list[str]: ...

    @abc.abstractmethod
    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int: ...

    @abc.abstractmethod
    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]: ...

    @abc.abstractmethod
    def get_schema(self, object_name: str) -> Schema: ...

    # --- Full-database extraction (optional — connectors override as supported) ---

    def list_extensions(self) -> list[ExtensionDef]:
        return []

    def list_schemas(self) -> list[SchemaDef]:
        return []

    def list_types(self) -> list[TypeDef]:
        return []

    def list_views(self) -> list[ViewDefinition]:
        return []

    def list_materialized_views(self) -> list[MaterializedViewDef]:
        return []

    def list_functions(self) -> list[FunctionDef]:
        return []

    def get_all_triggers(self) -> list[TriggerDef]:
        return []

    def list_events(self) -> list[EventDef]:
        return []

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        """Describe engine object support for planning and reporting.

        Values intentionally remain connector-owned: the orchestrator consumes
        these outcomes without encoding source/target engine pairs.
        """
        return {}

    def get_rls_policies(self, table: str, schema_name: str | None = None) -> list[RLSPolicy]:
        return []

    def list_comments(self) -> list[CommentDef]:
        return []

    def list_grants(self) -> list[GrantDef]:
        return []

    def list_security_principals(self) -> list[SecurityPrincipalDef]:
        return []

    def list_role_memberships(self) -> list[RoleMembershipDef]:
        return []

    def list_security_grants(self) -> list[GrantDef]:
        """Privileges outside the connector's historical object-grant scope."""
        return []

    def security_scope_status(self) -> str | None:
        """Optional connector-owned explanation of its security migration scope."""
        return None


class TargetConnector(abc.ABC):
    """Writes data and schema to the target database."""

    @abc.abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:
        """Release connector resources when a migration run finishes."""
        return None

    @abc.abstractmethod
    def ensure_database_exists(self) -> None: ...

    @abc.abstractmethod
    def create_object_if_missing(self, schema: Schema) -> str | None: ...

    @abc.abstractmethod
    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult: ...

    @abc.abstractmethod
    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None: ...

    @abc.abstractmethod
    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int: ...

    @abc.abstractmethod
    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]: ...

    # --- Full-database application (optional — connectors override as supported) ---

    def create_extension(self, ext: ExtensionDef) -> None:
        pass

    def create_schema(self, schema_def: SchemaDef) -> None:
        pass

    def create_type(self, type_def: TypeDef) -> None:
        pass

    def apply_constraints(self, schema: Schema) -> None:
        pass

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        return {}

    def inspect_schema(self, object_name: str, schema_name: str | None = None) -> Schema | None:
        """Read target metadata needed by post-migration verification."""
        return None

    def create_view(self, view: ViewDefinition) -> None:
        pass

    def create_materialized_view(self, mv: MaterializedViewDef) -> None:
        pass

    def refresh_materialized_view(self, name: str, schema_name: str | None = None) -> None:
        pass

    def create_function(self, func: FunctionDef) -> None:
        pass

    def create_trigger(self, trigger: TriggerDef) -> None:
        pass

    def create_event(self, event: EventDef) -> None:
        pass

    def suspend_triggers_for_data_load(self, triggers: list[TriggerDef]) -> list[TriggerDef]:
        """Temporarily remove target triggers that would observe migration DML."""
        return []

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        """Remove target rows for a full replacement sync before loading data.

        Connectors which do not define full replacement semantics retain the
        historical upsert-only behaviour.  Relational targets can override
        this to make a successful full run exactly match the source.
        """
        return []

    def sync_auto_increment(self, table: str, column: str) -> None:
        pass

    def reconcile_mysql_table(self, schema: Schema, managed_tables: set[str]) -> str:
        raise NotImplementedError("Target does not support MySQL schema reconciliation")

    def finalize_schema_reconciliations(self) -> list[str]:
        return []

    def apply_rls_policy(self, policy: RLSPolicy) -> None:
        pass

    def sync_sequence(self, table: str, column: str, schema_name: str | None = None) -> None:
        pass

    def apply_sequence_ownership(self, seq: "SequenceDef") -> None:
        pass

    def apply_comment(self, comment: CommentDef) -> None:
        pass

    def apply_grant(self, grant: GrantDef) -> None:
        pass

    def create_security_principal(self, principal: SecurityPrincipalDef) -> None:
        raise NotImplementedError("Target does not support security principal migration")

    def apply_role_membership(self, membership: RoleMembershipDef) -> None:
        raise NotImplementedError("Target does not support role membership migration")

    def security_migration_authorization(self) -> tuple[bool, str]:
        """Return whether target security writes may be attempted."""
        return True, ""


class CDCEngine(abc.ABC):
    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def poll_changes(self) -> list[ChangeEvent]: ...

    @abc.abstractmethod
    def apply(self, events: list[ChangeEvent]) -> ApplyResult: ...

    @abc.abstractmethod
    def checkpoint(self, result: ApplyResult) -> None: ...
