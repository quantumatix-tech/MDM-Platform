"""MSSQL Sequence object implementation.

Reusable Sequence-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    SequenceDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _qualify,
    _resolve_mssql_schemas,
)
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE SEQUENCE OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_all_sequences.
# Discovery of user sequences from the source database.
# ============================================================================

def discover_sequences(conn: Any, config: dict[str, Any]) -> list[SequenceDef]:
    """Discover user sequences in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_all_sequences``.

    SQL Server exposes sequence metadata via ``sys.sequences`` joined to
    ``sys.schemas`` for the schema name, and ``sys.sql_modules``-like
    attributes (start, increment, min, max, cycle, cache, current value).
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[SequenceDef] = []
    with conn.cursor() as cur:
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


# ============================================================================
# TARGET-SIDE SEQUENCE OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_sequence.
# Sequence creation on the target database.
# ============================================================================

def create_sequence(conn: Any, seq: SequenceDef) -> None:
    """Create a schema-qualified SQL Server sequence with source metadata.

    Replaces ``MSSQLTargetConnector.create_sequence``.

    Uses ``CREATE SEQUENCE`` with all source metadata (start, increment,
    min, max, cycle, cache, data type). Schema creation is not committed
    separately: CREATE SCHEMA is not the start of a new batch and can be
    followed by additional statements in the same transaction.

    If the sequence already exists on the target, creation is skipped
    (idempotency) without emitting any DDL or committing.
    """
    seq_schema = seq.schema or "dbo"
    validate_identifier(seq.name, "sequence")
    validate_identifier(seq_schema, "schema")
    seq_qname = _qualify(seq_schema, seq.name)

    with conn.cursor() as cur:
        # Ensure the target schema exists (dbo always exists in SQL Server).
        if seq_schema != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (seq_schema,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(seq_schema)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": seq_schema},
                )

        # Skip if the sequence already exists on the target (idempotency).
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
            conn.commit()
            audit_log(
                phase="create_sequence",
                status="created",
                details={"sequence": seq_qname, "owned_by": seq.owned_by},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_sequence", status="failed",
                details={"sequence": seq_qname, "reason": str(exc)},
            )
            raise
