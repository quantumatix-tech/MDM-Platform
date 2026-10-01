"""PostgreSQL Sequence object implementation.

Reusable sequence-specific logic for source discovery and target creation.
Every function takes an explicit ``conn`` parameter so that neither
``PostgresSourceConnector`` nor ``PostgresTargetConnector`` is imported.

A PostgreSQL sequence has a multi-stage lifecycle, and the three "setval"
shapes below are genuinely different operations rather than duplicates:

  * ``create_sequence``          Phase 3.5 — before the owning table exists
  * ``advance_sequence``         after data load — set to MAX(column) + 1
  * ``apply_sequence_ownership`` after the owning table exists — re-attach
  * ``sync_sequence``            legacy variant retained for API compatibility
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    SequenceDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.postgresql._models import _qualify


# ============================================================================
# SOURCE-SIDE SEQUENCE OPERATIONS
# ============================================================================

def discover_sequences(conn: Any, schemas: tuple[str, ...]) -> list[SequenceDef]:
    """Return every sequence in the configured schemas with full metadata.

    Covers both standalone sequences and column-owned (SERIAL/IDENTITY)
    sequences. Ownership is resolved through ``pg_depend`` and reported as
    ``schema.table.column`` so the target can re-attach it later.
    """
    results: list[SequenceDef] = []
    with conn.cursor() as cur:
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
            "    JOIN pg_depend d ON d.objid = sc.oid AND d.deptype IN ('a', 'i', 'n') "
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
            (list(schemas), list(schemas)),
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


def owned_sequences(
    conn: Any,
    schemas: tuple[str, ...],
) -> dict[tuple[str, str, str], tuple[str, str]]:
    """Map ``(table schema, table, column)`` -> ``(sequence schema, name)``.

    Resolves which sequence each column owns through ``pg_depend``. Shared by
    the sequence lifecycle and by security grant discovery, which needs it to
    decide which sequences a role must be able to use.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.schemaname, s.sequencename, "
            "  n.nspname || '.' || pc.relname || '.' || a.attname AS owned_by "
            "FROM pg_sequences s "
            "JOIN pg_class sc ON sc.relname = s.sequencename AND sc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = s.schemaname) "
            "JOIN pg_depend d ON d.objid = sc.oid AND d.deptype IN ('a', 'i', 'n') "
            "JOIN pg_class pc ON pc.oid = d.refobjid "
            "JOIN pg_namespace n ON pc.relnamespace = n.oid "
            "JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
            "WHERE s.schemaname = ANY(%s)",
            (list(schemas),),
        )
        owned: dict[tuple[str, str, str], tuple[str, str]] = {}
        for row in cur.fetchall():
            seq_schema, seq_name, owned_by = row
            if owned_by:
                parts = owned_by.split(".")
                if len(parts) == 3:
                    owned[(parts[0], parts[1], parts[2])] = (seq_schema, seq_name)
        return owned


# ============================================================================
# TARGET-SIDE SEQUENCE OPERATIONS
# ============================================================================

def create_sequence(conn: Any, seq: SequenceDef) -> None:
    """Create a sequence with the exact same properties as on the source.

    This runs BEFORE tables are created, so ``ALTER SEQUENCE ... OWNED BY`` is
    intentionally not applied here: PostgreSQL rejects ``OWNED BY`` when the
    referenced table does not exist yet, and the caller would otherwise see
    every sequence creation fail and lose them. Ownership is reattached later
    by ``apply_sequence_ownership`` once the owning table exists.
    """
    seq_schema = seq.schema or "public"
    seq_qname = _qualify(seq_schema, seq.name)
    with conn.cursor() as cur:
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
            conn.commit()
            audit_log(phase="create_sequence", status="created",
                      details={"sequence": seq_qname, "owned_by": seq.owned_by})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_sequence", status="skipped",
                      details={"sequence": seq_qname, "reason": str(exc)})


def advance_sequence(
    conn: Any,
    seq_name: str,
    table: str,
    column: str,
    owned_by: str | None = None,
) -> None:
    """After data load: advance the sequence to max(column) so the next INSERT gets the right value.

    Uses pg_get_serial_sequence to resolve the correct sequence for the column,
    which handles both standalone sequences and identity columns (which may have
    auto-generated sequence names with numeric suffixes).
    """
    validate_identifier(column, "column")
    table_qname = table
    if owned_by:
        parts = owned_by.rsplit(".", 2)
        if len(parts) == 3:
            table_schema, table_name_from_owned, _ = parts
            if table_name_from_owned == table:
                table_qname = f"{quote_identifier(table_schema)}.{quote_identifier(table)}"
    with conn.cursor() as cur:
        try:
            # Resolve the correct sequence for the column using pg_get_serial_sequence.
            # This handles identity columns whose internal sequence may have a
            # different name than the standalone sequence (e.g., suffix _1).
            cur.execute(
                "SELECT pg_get_serial_sequence(%s, %s)",
                (f"{table_qname}", column),
            )
            actual_seq = cur.fetchone()[0]
            if not actual_seq:
                # Fallback to provided sequence name if pg_get_serial_sequence fails
                actual_seq = seq_name
            cur.execute(
                f"SELECT setval(%s::regclass, "
                f"COALESCE((SELECT MAX({column}) FROM {table_qname}), 0) + 1, false)",
                (actual_seq,),
            )
            conn.commit()
            audit_log(phase="advance_sequence", status="advanced",
                      details={"sequence": actual_seq, "table": table, "column": column})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="advance_sequence", status="skipped",
                      details={"sequence": actual_seq if 'actual_seq' in locals() else seq_name, "reason": str(exc)})


def sync_sequence(
    conn: Any,
    table: str,
    column: str,
    schema_name: str | None = None,
) -> None:
    """Set a table's owned sequence to the current MAX(column).

    Distinct from ``advance_sequence``: this resolves the sequence implicitly
    through ``pg_get_serial_sequence`` and uses setval's default
    ``is_called=true``, so the very next ``nextval`` returns the value just
    written rather than the following one.

    Retained as part of the target connector API; the orchestrator currently
    uses ``advance_sequence`` instead.
    """
    validate_identifier(table, "table")
    validate_identifier(column, "column")
    schema = schema_name or "public"
    table_qname = _qualify(schema, table)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"SELECT setval("
                f"  pg_get_serial_sequence(%s, %s), "
                f"  COALESCE((SELECT MAX({column}) FROM {table_qname}), 1)"
                f")",
                (table_qname, column),
            )
            conn.commit()
            audit_log(phase="sync_sequence", status="synced",
                      details={"table": table_qname, "column": column})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="sync_sequence", status="skipped",
                      details={"table": table_qname, "column": column, "reason": str(exc)})


def apply_sequence_ownership(conn: Any, seq: SequenceDef) -> None:
    """Apply ALTER SEQUENCE ... OWNED BY after the owning table/column exists.

    Ownership is only re-attached when the owning table lives in the same
    schema as the sequence; a cross-schema owned sequence is skipped because
    PostgreSQL does not support it.
    """
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
    seq_qname = _qualify(seq_schema, seq.name)
    table_qname = _qualify(seq_schema, table_name)
    column_qname = quote_identifier(column_name)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"ALTER SEQUENCE {seq_qname} OWNED BY {table_qname}.{column_qname}"
            )
            conn.commit()
            audit_log(phase="apply_sequence_ownership", status="owned",
                      details={"sequence": seq.name, "owned_by": f"{table_qname}.{column_qname}"})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="apply_sequence_ownership", status="skipped",
                      details={"sequence": seq.name, "reason": str(exc)})
