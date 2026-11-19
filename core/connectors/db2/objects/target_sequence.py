from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier
from core.connectors.db2.objects.ddl import _quote_ident


def _existing_sequence_def(conn, schema_name, seq_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT START, INCREMENT, MINVALUE, MAXVALUE, CYCLE, CACHE, "ORDER"
            FROM SYSCAT.SEQUENCES
            WHERE SEQSCHEMA=? AND SEQNAME=? AND SEQTYPE='S'
            """,
            (schema_name, seq_name),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "start": row[0], "increment": row[1], "minvalue": row[2], "maxvalue": row[3],
            "cycle": str(row[4]).strip() if row[4] is not None else "N",
            "cache": row[5], "order": str(row[6]).strip() if row[6] is not None else "N",
        }


def create_sequence_if_missing(conn: Any, schema_name: str, seq: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(seq["name"], "sequence")
    existing = _existing_sequence_def(conn, schema_name, seq["name"])
    desired = {
        "start": seq["start"], "increment": seq["increment"], "minvalue": seq["minvalue"],
        "maxvalue": seq["maxvalue"], "cycle": seq["cycle"], "cache": seq["cache"], "order": seq["order"],
    }
    if existing is not None:
        if existing != desired:
            raise RuntimeError(f"Conflicting sequence definition for {schema_name}.{seq['name']}")
        return "skipped-existing"
    cycle = "CYCLE" if str(seq["cycle"]).upper() == "Y" else "NO CYCLE"
    order = "ORDER" if str(seq["order"]).upper() == "Y" else "NO ORDER"
    sql = (
        f"CREATE SEQUENCE {_quote_ident(schema_name)}.{_quote_ident(seq['name'])} "
        f"AS {seq.get('datatype', 'INTEGER')} START WITH {seq['start']} INCREMENT BY {seq['increment']} "
        f"MINVALUE {seq['minvalue']} MAXVALUE {seq['maxvalue']} {cycle} CACHE {seq['cache']} {order}"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"
