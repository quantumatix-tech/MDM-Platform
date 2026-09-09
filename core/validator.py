from __future__ import annotations

import hashlib
import json
from typing import Any

from core.connectors.base import SourceConnector, TargetConnector
from core.audit_logger import audit_log


class Validator:
    def __init__(self, source: SourceConnector, target: TargetConnector) -> None:
        self._source = source
        self._target = target

    def validate_count(self, object_name: str, schema_name: str | None = None) -> dict[str, Any]:
        source_count = self._source.get_object_count(object_name, schema_name=schema_name)
        target_count = self._target.get_object_count(object_name, schema_name=schema_name)
        match = source_count == target_count
        audit_log(
            phase="validate_count",
            status="pass" if match else "fail",
            details={"object": object_name, "source": source_count, "target": target_count},
        )
        return {
            "object": object_name,
            "type": "count",
            "source_count": source_count,
            "target_count": target_count,
            "match": match,
        }

    # ------------------------------------------------------------------
    # Fix #6: DB-side checksum validation
    # ------------------------------------------------------------------

    @staticmethod
    def _pg_md5_hash(conn: Any, object_name: str, sample_size: int, schema_name: str | None = None) -> str | None:
        """Compute an MD5 aggregate hash entirely inside PostgreSQL.

        Using MD5(CAST(t AS text)) avoids pulling rows into Python and is
        much faster for large tables.  Returns None if the connector is not
        a psycopg connection (falls back to Python-side hashing).
        """
        table_qname = f"{schema_name}.{object_name}" if schema_name else object_name
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT MD5(STRING_AGG(row_text, '|' ORDER BY row_text)) "
                    f"FROM ("
                    f"  SELECT CAST(t AS text) AS row_text "
                    f"  FROM {table_qname} t "
                    f"  LIMIT %s"
                    f") sub",
                    (sample_size,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def validate_checksum(self, object_name: str, sample_size: int = 1000, schema_name: str | None = None) -> dict[str, Any]:
        """Fix #6: Prefer DB-side MD5 aggregation over pulling rows into Python.

        For PostgreSQL connectors the hash is computed entirely inside the
        database (fast, no memory pressure).  For other connectors the
        original Python-side approach is used as a fallback.
        """
        source_hash: str | None = None
        target_hash: str | None = None

        src_conn = getattr(self._source, "_conn", None)
        tgt_conn = getattr(self._target, "_conn", None)

        if src_conn is not None and tgt_conn is not None:
            source_hash = self._pg_md5_hash(src_conn, object_name, sample_size, schema_name)
            target_hash = self._pg_md5_hash(tgt_conn, object_name, sample_size, schema_name)

        if source_hash is None or target_hash is None:
            source_rows = list(self._source.export_full(object_name, schema_name=schema_name))
            target_rows = list(self._target.export_full(object_name, schema_name=schema_name))

            def _row_hash(rows: list[dict]) -> str:
                sample = sorted(
                    rows[:sample_size],
                    key=lambda r: json.dumps(r, sort_keys=True, default=str),
                )
                return hashlib.sha256(
                    json.dumps(sample, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()

            source_hash = _row_hash(source_rows)
            target_hash = _row_hash(target_rows)

        match = source_hash == target_hash
        audit_log(
            phase="validate_checksum",
            status="pass" if match else "fail",
            details={"object": object_name, "sample_size": sample_size},
        )
        return {
            "object": object_name,
            "type": "checksum",
            "match": match,
            "source_sample_hash": source_hash,
            "target_sample_hash": target_hash,
        }

    def validate_full(self, object_name: str, schema_name: str | None = None) -> dict[str, Any]:
        source_rows = list(self._source.export_full(object_name, schema_name=schema_name))
        target_rows = list(self._target.export_full(object_name, schema_name=schema_name))

        source_sorted = sorted(
            source_rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)
        )
        target_sorted = sorted(
            target_rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)
        )
        match = source_sorted == target_sorted

        audit_log(
            phase="validate_full",
            status="pass" if match else "fail",
            details={"object": object_name, "source_rows": len(source_rows), "target_rows": len(target_rows)},
        )
        return {
            "object": object_name,
            "type": "full",
            "match": match,
            "source_rows": len(source_rows),
            "target_rows": len(target_rows),
        }

    def validate(self, object_name: str, mode: str = "count", sample_size: int = 1000, schema_name: str | None = None) -> dict[str, Any]:
        if mode == "count":
            return self.validate_count(object_name, schema_name=schema_name)
        elif mode == "checksum":
            return self.validate_checksum(object_name, sample_size=sample_size, schema_name=schema_name)
        elif mode == "full":
            return self.validate_full(object_name, schema_name=schema_name)
        else:
            raise ValueError(f"Unknown validation mode: {mode}")