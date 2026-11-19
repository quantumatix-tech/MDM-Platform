from __future__ import annotations

from core.connectors.base import validate_identifier


def discover_partitions(conn, schema_name, table_name):
    validate_identifier(schema_name, "schema")
    validate_identifier(table_name, "table")
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DATAPARTITIONNAME, SEQNO, LOWVALUE, HIGHVALUE, LOWINCLUSIVE, HIGHINCLUSIVE FROM SYSCAT.DATAPARTITIONS WHERE TABSCHEMA=? AND TABNAME=? ORDER BY SEQNO",
            (schema_name, table_name),
        )
        for r in cur.fetchall():
            out.append(
                {
                    "name": str(r[0]).strip(),
                    "seqno": r[1],
                    "low": None if r[2] is None else str(r[2]).strip(),
                    "high": None if r[3] is None else str(r[3]).strip(),
                    "low_inclusive": str(r[4]).strip(),
                    "high_inclusive": str(r[5]).strip(),
                }
            )
    return out


def discover_partition_column(conn, schema_name, table_name):
    """Discover the range partition key column(s) for a partitioned table.

    Db2 LUW stores the partitioning key expressions in SYSCAT.DATAPARTITIONEXPRESSION,
    ordered by DATAPARTITIONKEYSEQ. For simple column partitioning, the expression
    is the column name. Complex expressions are not supported.

    Note: DATAPARTITIONEXPRESSION is a CLOB(32K) in Db2 11.5.9, which the ODBC driver
    may not fetch directly (SQL type -99 / HY106). We CAST to VARCHAR(32672) to avoid this.
    """
    validate_identifier(schema_name, "schema")
    validate_identifier(table_name, "table")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT CAST(DATAPARTITIONEXPRESSION AS VARCHAR(32672))
            FROM SYSCAT.DATAPARTITIONEXPRESSION
            WHERE TABSCHEMA = ? AND TABNAME = ?
            ORDER BY DATAPARTITIONKEYSEQ
            """,
            (schema_name, table_name),
        )
        rows = cur.fetchall()
    if not rows:
        raise RuntimeError(f"Could not discover partition key column for {schema_name}.{table_name}")
    cols = []
    for r in rows:
        expr = str(r[0]).strip() if r[0] else ""
        if not expr:
            continue
        # Simple column reference: just the column name
        # Complex expressions (e.g., "EMP_ID + 1", "UPPER(NAME)") are not supported
        if not expr.replace("_", "").replace(".", "").isalnum():
            raise RuntimeError(
                f"Unsupported partition expression for {schema_name}.{table_name}: {expr}. "
                "Only simple column references are supported."
            )
        cols.append(expr)
    if not cols:
        raise RuntimeError(f"Could not discover partition key column for {schema_name}.{table_name}")
    return cols
