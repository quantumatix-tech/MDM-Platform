"""PostgreSQL partition object implementation.

PostgreSQL partitioning is a distinct catalog concern — child partitions are
linked to their parent through ``pg_inherits`` and carry their range in
``relpartbound`` — and it is migrated in its own orchestration phase, after
the parent table exists and before data is loaded.

Two parts of partitioning deliberately stay outside this module:

  * ``Schema.partition_key`` discovery (``pg_get_partkeydef``) stays in
    ``source.get_schema()``, because it is part of the cross-engine ``Schema``
    contract rather than partition-child discovery, and
  * the ``PARTITION BY`` clause of ``CREATE TABLE`` stays in ``objects.table``,
    because it is part of parent-table DDL.
"""
from __future__ import annotations

from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import PartitionDef


# ============================================================================
# SOURCE-SIDE PARTITION OPERATIONS
# ============================================================================

def discover_partitions(conn: Any, schemas: tuple[str, ...]) -> list[PartitionDef]:
    """Return every partition child in the configured schemas.

    The bound expression is decompiled with ``pg_get_expr(relpartbound, oid)``,
    which yields the ready-to-use ``FOR VALUES ...`` clause for the target.
    """
    results: list[PartitionDef] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  c.relname AS partition_name, "
            "  parent.relname AS parent_table, "
            "  pg_get_expr(c.relpartbound, c.oid) AS bound_expr "
            "FROM pg_class c "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_class parent ON i.inhparent = parent.oid "
            "WHERE n.nspname = ANY(%s) AND c.relispartition = true "
            "ORDER BY parent.relname, c.relname",
            (list(schemas),)
        )
        for row in cur.fetchall():
            part_name, parent_table, bound = row
            results.append(PartitionDef(
                name=part_name,
                parent_table=parent_table,
                bound=bound,
            ))
    return results


# ============================================================================
# TARGET-SIDE PARTITION OPERATIONS
# ============================================================================

def create_partition(conn: Any, partition: PartitionDef) -> None:
    """Create a child partition table (PARTITION OF parent ...).

    Known limitation (pre-existing, deliberately unchanged): the existence
    probe only checks the ``public`` schema, so a partition that already exists
    in another schema is not detected and the CREATE fails. That failure is
    audited as "skipped" rather than raised.
    """
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = %s AND n.nspname = 'public'",
                (partition.name,),
            )
            if cur.fetchone() is not None:
                return   # already exists
            cur.execute(
                f"CREATE TABLE {partition.name} "
                f"PARTITION OF {partition.parent_table} "
                f"{partition.bound}"
            )
            conn.commit()
            audit_log(phase="create_partition", status="created",
                      details={"partition": partition.name, "parent": partition.parent_table})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_partition", status="skipped",
                      details={"partition": partition.name, "reason": str(exc)})
