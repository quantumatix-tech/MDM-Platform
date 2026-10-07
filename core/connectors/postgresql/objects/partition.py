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
from core.connectors.base import PartitionDef, quote_identifier

# ============================================================================
# SOURCE-SIDE PARTITION OPERATIONS
# ============================================================================

def discover_partitions(conn: Any, schemas: tuple[str, ...]) -> list[PartitionDef]:
    """Return every partition child in the configured schemas.

    The bound expression is decompiled with ``pg_get_expr(relpartbound, oid)``,
    which yields the ready-to-use ``FOR VALUES ...`` clause for the target.
    For DEFAULT partitions the bound is ``None`` and should be omitted from DDL.
    """
    results: list[PartitionDef] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  nsp.nspname AS schema_name, "
            "  c.relname AS partition_name, "
            "  parent.relname AS parent_table, "
            "  pg_get_expr(c.relpartbound, c.oid) AS bound_expr "
            "FROM pg_class c "
            "JOIN pg_namespace nsp ON c.relnamespace = nsp.oid "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_class parent ON i.inhparent = parent.oid "
            "JOIN pg_namespace parent_ns ON parent.relnamespace = parent_ns.oid "
            "WHERE nsp.nspname = ANY(%s) "
            "  AND c.relispartition = true "
            "  AND c.relkind IN ('r', 'p', 'f') "
            "ORDER BY parent.relname, c.relname",
            (list(schemas),)
        )
        for row in cur.fetchall():
            schema_name, part_name, parent_table, bound = row
            results.append(PartitionDef(
                name=part_name,
                parent_table=parent_table,
                bound=bound,
                schema=schema_name,
            ))
    return results


# ============================================================================
# TARGET-SIDE PARTITION OPERATIONS
# ============================================================================

def create_partition(conn: Any, partition: PartitionDef) -> None:
    """Create a child partition table.

    Schema-qualifies both the partition name and the parent table so that
    partitioning works correctly in non-``public`` schemas (e.g. ``training``).

    For DEFAULT partitions (``bound`` is ``None``), the DDL omits the
    ``FOR VALUES`` clause entirely and uses ``DEFAULT`` instead.
    """
    schema = partition.schema or "public"
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = %s AND n.nspname = %s",
                (partition.name, schema),
            )
            if cur.fetchone() is not None:
                return   # already exists
            qualified_name = f"{quote_identifier(schema)}.{quote_identifier(partition.name)}"
            qualified_parent = f"{quote_identifier(schema)}.{quote_identifier(partition.parent_table)}"
            if partition.bound:
                ddl = (
                    f"CREATE TABLE {qualified_name} "
                    f"PARTITION OF {qualified_parent} "
                    f"{partition.bound}"
                )
            else:
                ddl = (
                    f"CREATE TABLE {qualified_name} "
                    f"PARTITION OF {qualified_parent} "
                    f"DEFAULT"
                )
            cur.execute(ddl)
            conn.commit()
            audit_log(phase="create_partition", status="created",
                      details={"partition": partition.name, "parent": partition.parent_table,
                               "schema": schema})
        except Exception as exc:
            conn.rollback()
            audit_log(phase="create_partition", status="skipped",
                      details={"partition": partition.name, "reason": str(exc)})
