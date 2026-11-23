from __future__ import annotations
from typing import Any
from core.connectors.base import validate_identifier

def _clob_text(v):
    if v is None:
        return None
    return str(v)

def discover_check_constraints(conn, schema_name):
    validate_identifier(schema_name, "schema")
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tc.CONSTNAME, tc.TABNAME, CAST(c.TEXT AS VARCHAR(32672)), tc.ENFORCED FROM SYSCAT.TABCONST tc JOIN SYSCAT.CHECKS c ON tc.CONSTNAME=c.CONSTNAME AND tc.TABSCHEMA=c.TABSCHEMA AND tc.TABNAME=c.TABNAME WHERE tc.TABSCHEMA=?",
            (schema_name,),
        )
        for r in cur.fetchall():
            cname = str(r[0]).strip()
            # Skip Db2 auto-generated constraints (e.g. generated-column validation)
            if cname.upper().startswith("SQL") and cname[3:].isdigit():
                continue
            out.append({"schema": schema_name, "table": str(r[1]).strip(), "name": cname, "expression": _clob_text(r[2]), "enforced": str(r[3]).strip()})
    return out

def discover_column_defaults(conn, schema_name):
    validate_identifier(schema_name, "schema")
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT TABNAME, COLNAME, CAST(DEFAULT AS VARCHAR(32672)) FROM SYSCAT.COLUMNS WHERE TABSCHEMA=?", (schema_name,))
        for r in cur.fetchall():
            out.append({"schema": schema_name, "table": str(r[0]).strip(), "column": str(r[1]).strip(), "default": _clob_text(r[2])})
    return out
