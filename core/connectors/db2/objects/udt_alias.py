from __future__ import annotations

from core.connectors.base import validate_identifier


def discover_udts(conn, schema_name):
    validate_identifier(schema_name, "schema")
    out=[]
    with conn.cursor() as cur:
        # Distinct UDTs (METATYPE='T' with source type)
        cur.execute("SELECT TYPESCHEMA, TYPENAME, SOURCESCHEMA, SOURCENAME, METATYPE FROM SYSCAT.DATATYPES WHERE TYPESCHEMA=? AND METATYPE='T'", (schema_name,))
        for r in cur.fetchall():
            out.append({"schema": str(r[0]).strip(), "name": str(r[1]).strip(), "source_schema": str(r[2]).strip(), "source_type": str(r[3]).strip(), "metatype": "T", "supported": True})
        # Structured UDTs (METATYPE='S') — discovered but not migrated
        cur.execute("SELECT TYPESCHEMA, TYPENAME FROM SYSCAT.DATATYPES WHERE TYPESCHEMA=? AND METATYPE='S'", (schema_name,))
        for r in cur.fetchall():
            out.append({"schema": str(r[0]).strip(), "name": str(r[1]).strip(), "source_schema": None, "source_type": None, "metatype": "S", "supported": False})
    return out

def discover_aliases(conn, schema_name):
    validate_identifier(schema_name, "schema")
    out=[]
    with conn.cursor() as cur:
        cur.execute("SELECT TABSCHEMA, TABNAME, BASE_TABSCHEMA, BASE_TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TYPE='A'", (schema_name,))
        for r in cur.fetchall():
            out.append({"schema": str(r[0]).strip(), "name": str(r[1]).strip(), "base_schema": str(r[2]).strip(), "base_table": str(r[3]).strip()})
    return out

def discover_structured_udts(conn, schema_name):
    validate_identifier(schema_name, 'schema')
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TYPESCHEMA, TYPENAME FROM SYSCAT.DATATYPES WHERE TYPESCHEMA=? AND METATYPE IN ('S','R') ORDER BY TYPENAME",
            (schema_name,),
        )
        for r in cur.fetchall():
            if not r:
                continue
            type_schema = str(r[0]).strip()
            type_name = str(r[1]).strip()
            attrs = []
            cur2 = conn.cursor()
            cur2.execute(
                "SELECT ATTR_NAME, ATTR_TYPENAME, LENGTH, SCALE FROM SYSCAT.ATTRIBUTES WHERE TYPESCHEMA=? AND TYPENAME=? ORDER BY ORDINAL",
                (type_schema, type_name),
            )
            for a in cur2.fetchall():
                if not a:
                    continue
                attrs.append({
                    'name': str(a[0]).strip(),
                    'type': str(a[1]).strip(),
                    'length': a[2],
                    'scale': a[3],
                })
            cur2.close()
            out.append({
                'schema': type_schema,
                'name': type_name,
                'attributes': attrs,
                'supported': True,
            })
    return out
