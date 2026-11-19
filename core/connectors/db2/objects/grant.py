from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def discover_grants(conn: Any, schema_name: str) -> list[dict]:
    validate_identifier(schema_name, "schema")
    grants = []
    with conn.cursor() as cur:
        # schema
        cur.execute("SELECT GRANTEE, GRANTEETYPE, CREATEINAUTH FROM SYSCAT.SCHEMAAUTH WHERE SCHEMANAME=?", (schema_name,))
        for r in cur.fetchall():
            if r and str(r[2]).strip().upper() in ("Y", "G"):
                grants.append({"object_type": "SCHEMA", "object_name": schema_name, "column": None, "grantee": str(r[0]).strip(), "grantee_type": str(r[1]).strip(), "privilege": "CREATEIN", "grantable": str(r[2]).strip().upper() == "G"})
        # table
        cur.execute("SELECT TABNAME, GRANTEE, GRANTEETYPE, SELECTAUTH, INSERTAUTH, UPDATEAUTH, DELETEAUTH FROM SYSCAT.TABAUTH WHERE TABSCHEMA=?", (schema_name,))
        for r in cur.fetchall():
            if not r:
                continue
            tab = str(r[0]).strip()
            grantee = str(r[1]).strip()
            gtype = str(r[2]).strip()
            for idx, priv in enumerate(["SELECT", "INSERT", "UPDATE", "DELETE"], start=3):
                val = str(r[idx]).strip().upper() if r[idx] is not None else "N"
                if val in ("Y", "G"):
                    grants.append({"object_type": "TABLE", "object_name": tab, "column": None, "grantee": grantee, "grantee_type": gtype, "privilege": priv, "grantable": val == "G"})
        # column
        cur.execute("SELECT TABNAME, COLNAME, GRANTEE, GRANTEETYPE, PRIVTYPE, GRANTABLE FROM SYSCAT.COLAUTH WHERE TABSCHEMA=?", (schema_name,))
        priv_map = {"S": "SELECT", "U": "UPDATE", "R": "REFERENCES"}
        for r in cur.fetchall():
            if not r:
                continue
            tab = str(r[0]).strip()
            col = str(r[1]).strip()
            grantee = str(r[2]).strip()
            gtype = str(r[3]).strip()
            ptype = str(r[4]).strip().upper()
            grantable = str(r[5]).strip().upper() in ("Y", "G")
            if ptype in priv_map:
                grants.append({"object_type": "COLUMN", "object_name": tab, "column": col, "grantee": grantee, "grantee_type": gtype, "privilege": priv_map[ptype], "grantable": grantable})
        # sequence
        cur.execute("SELECT SEQNAME, GRANTEE, GRANTEETYPE, USAGEAUTH FROM SYSCAT.SEQUENCEAUTH WHERE SEQSCHEMA=?", (schema_name,))
        for r in cur.fetchall():
            if r and str(r[3]).strip().upper() in ("Y", "G"):
                grants.append({"object_type": "SEQUENCE", "object_name": str(r[0]).strip(), "column": None, "grantee": str(r[1]).strip(), "grantee_type": str(r[2]).strip(), "privilege": "USAGE", "grantable": str(r[3]).strip().upper() == "G"})
        # routine
        cur.execute("SELECT ra.SPECIFICNAME, ra.GRANTEE, ra.GRANTEETYPE, ra.EXECUTEAUTH, ra.ROUTINETYPE FROM SYSCAT.ROUTINEAUTH ra JOIN SYSCAT.ROUTINES r ON ra.SPECIFICNAME=r.SPECIFICNAME AND ra.SCHEMA=r.ROUTINESCHEMA WHERE ra.SCHEMA=? AND (r.ORIGIN IS NULL OR r.ORIGIN <> 'S')", (schema_name,))
        for r in cur.fetchall():
            if r and str(r[3]).strip().upper() in ("Y", "G"):
                specific = str(r[0]).strip() if r[0] else None
                routine_name = specific
                if specific:
                    try:
                        with conn.cursor() as c2:
                            c2.execute("SELECT ROUTINENAME FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND SPECIFICNAME=?", (schema_name, specific))
                            rr = c2.fetchone()
                            if rr and rr[0]:
                                routine_name = str(rr[0]).strip()
                    except Exception:
                        pass
                grants.append({"object_type": "ROUTINE", "object_name": routine_name, "specific": specific, "routine_type": str(r[4]).strip().upper() if r[4] is not None else None, "column": None, "grantee": str(r[1]).strip(), "grantee_type": str(r[2]).strip(), "privilege": "EXECUTE", "grantable": str(r[3]).strip().upper() == "G"})
    return grants
