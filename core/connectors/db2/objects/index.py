from __future__ import annotations

from typing import Any

from core.connectors.base import Index, validate_identifier


def discover_indexes(conn: Any, schema_name: str, table_name: str) -> list[Index]:
    validate_identifier(table_name, "table")
    validate_identifier(schema_name, "schema")
    indexes: list[Index] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT INDSCHEMA, INDNAME, UNIQUERULE, INDEXTYPE, SYSTEM_REQUIRED
            FROM SYSCAT.INDEXES
            WHERE TABSCHEMA = ? AND TABNAME = ?
            ORDER BY INDSCHEMA, INDNAME
            """,
            (schema_name, table_name),
        )
        idx_rows = cur.fetchall()
        for idx_row in idx_rows:
            if not idx_row:
                continue
            ind_schema = str(idx_row[0]).strip()
            idx_name = str(idx_row[1]).strip()
            unique_rule = str(idx_row[2]).strip().upper() if idx_row[2] else "D"
            index_type = str(idx_row[3]).strip().upper() if idx_row[3] else "REG"
            system_required = int(idx_row[4]) if len(idx_row) > 4 and idx_row[4] is not None else 0
            if unique_rule == "P":
                continue  # PK-backed
            if unique_rule == "U" and system_required == 1:
                continue  # UNIQUE constraint backing index
            if index_type not in ("REG", "BTREE"):
                raise ValueError(f"Unsupported Db2 index type {index_type!r} for {schema_name}.{table_name}.{idx_name}")
            cur.execute(
                """
                SELECT COLNAME, COLSEQ, COLORDER
                FROM SYSCAT.INDEXCOLUSE
                WHERE INDSCHEMA = ? AND INDNAME = ?
                ORDER BY COLSEQ
                """,
                (ind_schema, idx_name),
            )
            cols = []
            desc = []
            for c in cur.fetchall():
                if not c:
                    continue
                colname = str(c[0]).strip()
                cols.append(colname)
                order = str(c[2]).strip().upper() if len(c) > 2 and c[2] else "A"
                if order == "D":
                    desc.append(colname)
            indexes.append(Index(
                name=idx_name,
                columns=cols,
                unique=(unique_rule == "U"),
                index_type=index_type,
                constraint_backed=False,
                descending_columns=desc,
            ))
    return indexes
