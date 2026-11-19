from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def create_mqt_if_missing(conn: Any, schema_name: str, mqt: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(mqt["name"], "mqt")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=? AND TYPE='S'", (schema_name, mqt["name"]))
        if cur.fetchone():
            return "skipped-existing"
        cur.execute(mqt["definition"])
    return "created"


def refresh_mqt(conn: Any, schema_name: str, mqt_name: str) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(mqt_name, "mqt")
    with conn.cursor() as cur:
        cur.execute(f"REFRESH TABLE {schema_name}.{mqt_name}")
    return "refreshed"
