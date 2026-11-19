from __future__ import annotations

from core.connectors.base import validate_identifier


def _normalize_udt_type(type_str: str) -> str:
    """Normalize UDT type for comparison: extract base type name without length/precision."""
    s = type_str.strip().upper()
    # Remove length/precision/scale in parentheses
    if "(" in s:
        s = s.split("(")[0]
    return s


def _existing_udt_def(conn, schema, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT SOURCESCHEMA, SOURCENAME FROM SYSCAT.DATATYPES WHERE TYPESCHEMA=? AND TYPENAME=?",
            (schema, name),
        )
        row = cur.fetchone()
    if not row:
        return None
    return f"{str(row[0]).strip()}.{_normalize_udt_type(str(row[1]))}".upper()


def create_udt_if_missing(conn, udt):
    validate_identifier(udt["schema"], "schema")
    validate_identifier(udt["name"], "type")
    existing_def = _existing_udt_def(conn, udt["schema"], udt["name"])
    # Structured UDT (METATYPE S/R) with attributes
    if udt.get("attributes"):
        def _fmt_attr(a):
            t = a["type"]
            length = a.get("length")
            scale = a.get("scale")
            if length is not None and t.upper() in ("VARCHAR", "CHAR", "CHARACTER", "GRAPHIC", "VARGRAPHIC"):
                if scale is not None and scale > 0:
                    return f"{a['name']} {t}({length},{scale})"
                return f"{a['name']} {t}({length})"
            return f"{a['name']} {t}"
        attrs = ", ".join(_fmt_attr(a) for a in udt["attributes"])
        if existing_def is not None:
            return "already_present"
        sql = f"CREATE TYPE {udt['schema']}.{udt['name']} AS ({attrs}) INSTANTIABLE REF USING INTEGER MODE DB2SQL"
        with conn.cursor() as cur:
            cur.execute(sql)
        return "created"
    # Distinct UDT (METATYPE T)
    desired_def = f"{udt['source_schema']}.{_normalize_udt_type(udt['source_type'])}".upper()
    if existing_def is not None:
        if existing_def != desired_def:
            raise RuntimeError(
                f"UDT conflict for {udt['schema']}.{udt['name']}: "
                f"source={desired_def}, target={existing_def}. "
                "Existing target UDT differs from source; manual resolution required."
            )
        return "already_present"
    sql = f"CREATE TYPE {udt['schema']}.{udt['name']} AS {udt['source_type']}"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"


def _existing_alias_def(conn, schema, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT BASE_TABSCHEMA, BASE_TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=? AND TYPE='A'",
            (schema, name),
        )
        row = cur.fetchone()
    if not row:
        return None
    return f"{str(row[0]).strip()}.{str(row[1]).strip()}".upper()


def create_alias_if_missing(conn, alias):
    validate_identifier(alias["schema"], "schema")
    validate_identifier(alias["name"], "alias")
    desired_def = f"{alias['base_schema']}.{alias['base_table']}".upper()
    existing_def = _existing_alias_def(conn, alias["schema"], alias["name"])
    if existing_def is not None:
        if existing_def != desired_def:
            raise RuntimeError(
                f"Alias conflict for {alias['schema']}.{alias['name']}: "
                f"source={desired_def}, target={existing_def}. "
                "Existing target alias differs from source; manual resolution required."
            )
        return "already_present"
    sql = f"CREATE ALIAS {alias['schema']}.{alias['name']} FOR {alias['base_schema']}.{alias['base_table']}"
    with conn.cursor() as cur:
        cur.execute(sql)
    return "created"
