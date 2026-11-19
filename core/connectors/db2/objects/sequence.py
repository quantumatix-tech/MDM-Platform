from __future__ import annotations

from typing import Any


def discover_standalone_sequences(conn: Any, schema_name: str) -> list[dict]:
    seqs = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT SEQNAME, DATATYPEID, START, INCREMENT, MINVALUE, MAXVALUE, CYCLE, CACHE, "ORDER", NEXTCACHEFIRSTVALUE, PRECISION
            FROM SYSCAT.SEQUENCES
            WHERE SEQSCHEMA=? AND SEQTYPE='S'
            ORDER BY SEQNAME
            """,
            (schema_name,),
        )
        for r in cur.fetchall():
            if not r:
                continue
            datatype = {24: "INTEGER", 28: "SMALLINT", 29: "BIGINT"}.get(int(r[1]), None) if r[1] is not None else None
            if datatype is None:
                raise ValueError(f"Unsupported Db2 sequence datatype ID {r[1]!r}")
            seqs.append({
                "name": str(r[0]).strip(),
                "datatype": datatype,
                "start": r[2],
                "increment": r[3],
                "minvalue": r[4],
                "maxvalue": r[5],
                "cycle": str(r[6]).strip() if r[6] is not None else "N",
                "cache": r[7],
                "order": str(r[8]).strip() if r[8] is not None else "N",
                "nextcache": r[9],
                "precision": r[10],
            })
    return seqs
