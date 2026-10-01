"""PostgreSQL CDC (Change Data Capture) Engine.

Logical replication using ``pgoutput``, the output plugin built into
PostgreSQL 10 and later, so no additional extension is required.

Flow:
  1. ``CREATE PUBLICATION`` for all tables on the source
  2. ``CREATE REPLICATION SLOT`` using the ``pgoutput`` plugin
  3. Poll with ``pg_logical_slot_peek_binary_changes`` (raw pgoutput messages)
  4. Decode INSERT / UPDATE / DELETE messages
  5. Apply to the target via upsert / delete
  6. Advance the slot LSN checkpoint

This engine talks to the target only through the ``TargetConnector``
interface, so it has no dependency on the object layer.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    CDCEngine,
    ApplyResult,
    ChangeEvent,
    Schema,
    TargetConnector,
)
from core.connectors.postgresql._models import _make_conn_kwargs
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff


class PostgresCDCEngine(CDCEngine):
    """Logical replication CDC using pgoutput — the built-in output plugin.

    Note: ``apply`` takes ``(events, target)`` rather than the ABC's
    ``apply(events)``. The extra target argument is required to dispatch
    changes and is passed explicitly by the orchestrator.
    """

    _PUBLICATION = "migration_pub"

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._source_conn: Any = None   # regular connection (for setup + polling)
        self._slot_name = config.get("slot_name", "migration_slot")
        self._last_lsn: str | None = None
        self._relations: dict[int, dict[str, Any]] = {}  # oid -> relation metadata

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
            if flags & 0x01:   # replica-identity column -> treat as primary key
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
        """Drop the replication slot after migration completes (or on failure).

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
