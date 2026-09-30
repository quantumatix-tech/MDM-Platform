"""MySQL partition object operations.

Owns the two genuinely partition-specific concerns:

* discovery of a table's partition metadata from ``INFORMATION_SCHEMA``,
  and
* generation of the ``PARTITION BY`` clause used in ``CREATE TABLE``.

This module must not import ``source``, ``target`` or ``cdc``, and it must
not import any sibling object module: ``objects/table.py`` depends on this
module, never the reverse.

It deliberately does *not* own table DDL or schema composition. The
``CREATE TABLE`` statement itself stays in ``objects/table.py`` and only
appends the clause returned by :func:`_mysql_partition_clause`; the
cross-engine ``Schema`` DTO is composed by ``mysql/_schema.py``, which
delegates the partition metadata read to :func:`discover_partitions`.

``_mysql_partition_boundary`` and ``_mysql_partition_clause`` are carried
over byte-for-byte from ``core.connectors.mysql._models``; they keep their
original names because ``core.connectors.mysql`` re-exports them as part of
its public helper surface.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from core.connectors.base import (
    Schema,
    TablePartition,
)

from core.connectors.mysql._models import _q


class PartitionMetadata(NamedTuple):
    """Partitioning details read from ``INFORMATION_SCHEMA.PARTITIONS``.

    ``method`` and ``expression`` are ``None`` and ``partitions`` is empty
    for a non-partitioned table, which is what ``Schema`` stores in that
    case.
    """

    method: str | None
    expression: str | None
    partitions: list[TablePartition]


def discover_partitions(
    cur: Any,
    database: str,
    object_name: str,
) -> PartitionMetadata:
    """Read the partition layout of ``object_name`` in ``database``.

    ``cur`` must be an open cursor; the caller owns it, so this stays
    composable with the surrounding ``inspect_schema`` cursor block and
    preserves the existing query order.
    """
    cur.execute("SELECT PARTITION_METHOD,PARTITION_EXPRESSION,PARTITION_NAME,PARTITION_DESCRIPTION FROM INFORMATION_SCHEMA.PARTITIONS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND PARTITION_NAME IS NOT NULL ORDER BY PARTITION_ORDINAL_POSITION", (database, object_name))
    partition_rows = cur.fetchall()

    method = partition_rows[0][0] if partition_rows else None
    expression = partition_rows[0][1] if partition_rows else None
    partitions = [
        TablePartition(name=name, description=description)
        for _, _, name, description in partition_rows
    ]
    return PartitionMetadata(method, expression, partitions)


def _mysql_partition_boundary(description: str | None) -> str:
    boundary = (description or "").strip()
    if boundary.upper() == "MAXVALUE":
        return "(MAXVALUE)"
    if boundary.startswith("(") and boundary.endswith(")"):
        return boundary
    return f"({boundary})"


def _mysql_partition_clause(schema: Schema) -> str:
    method = (schema.partition_method or "").upper()
    expression = (schema.partition_expression or "").strip()
    partitions = schema.partitions
    if not method:
        return ""
    if method not in {"RANGE", "RANGE COLUMNS", "LIST", "LIST COLUMNS", "HASH", "KEY"}:
        raise ValueError(f"Unsupported MySQL partition method: {method}")
    if not expression:
        raise ValueError(f"MySQL {method} partitioning has no expression")

    if method in {"HASH", "KEY"}:
        return f"PARTITION BY {method} ({expression}) PARTITIONS {len(partitions)}"

    boundary_keyword = "VALUES LESS THAN" if method.startswith("RANGE") else "VALUES IN"
    definitions = ", ".join(
        f"PARTITION {_q(partition.name)} {boundary_keyword} "
        f"{_mysql_partition_boundary(partition.description)}"
        for partition in partitions
    )
    if not definitions:
        raise ValueError(f"MySQL {method} partitioning has no partitions")
    return f"PARTITION BY {method} ({expression}) ({definitions})"
