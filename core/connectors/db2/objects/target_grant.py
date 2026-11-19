from __future__ import annotations

from typing import Any

from core.connectors.base import validate_identifier


def _existing_target_privilege(conn: Any, schema_name: str, grant: dict) -> bool:
    g = grant["grantee"].upper()
    priv = grant["privilege"].upper()
    if grant["object_type"] == "SCHEMA":
        with conn.cursor() as cur:
            cur.execute("SELECT CREATEINAUTH FROM SYSCAT.SCHEMAAUTH WHERE SCHEMANAME=? AND GRANTEE=?", (schema_name, grant["grantee"]))
            row = cur.fetchone()
            return bool(row and str(row[0]).strip().upper() in ("Y", "G"))
    if grant["object_type"] == "TABLE":
        col = "SELECTAUTH" if priv == "SELECT" else "INSERTAUTH" if priv == "INSERT" else "UPDATEAUTH" if priv == "UPDATE" else "DELETEAUTH" if priv == "DELETE" else None
        if not col:
            return False
        with conn.cursor() as cur:
            cur.execute(f"SELECT {col} FROM SYSCAT.TABAUTH WHERE TABSCHEMA=? AND TABNAME=? AND GRANTEE=?", (schema_name, grant["object_name"], grant["grantee"]))
            row = cur.fetchone()
            return bool(row and str(row[0]).strip().upper() in ("Y", "G"))
    if grant["object_type"] == "COLUMN":
        colauth = "SELECTAUTH" if priv == "SELECT" else "UPDATEAUTH" if priv == "UPDATE" else "REFERENCESAUTH" if priv == "REFERENCES" else None
        if not colauth:
            return False
        with conn.cursor() as cur:
            cur.execute(f"SELECT {colauth} FROM SYSCAT.COLAUTH WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=? AND GRANTEE=?", (schema_name, grant["object_name"], grant["column"], grant["grantee"]))
            row = cur.fetchone()
            return bool(row and str(row[0]).strip().upper() in ("Y", "G"))
    if grant["object_type"] == "SEQUENCE":
        with conn.cursor() as cur:
            cur.execute("SELECT USAGEAUTH FROM SYSCAT.SEQUENCEAUTH WHERE SEQSCHEMA=? AND SEQNAME=? AND GRANTEE=?", (schema_name, grant["object_name"], grant["grantee"]))
            row = cur.fetchone()
            return bool(row and str(row[0]).strip().upper() in ("Y", "G"))
    if grant["object_type"] == "ROUTINE":
        with conn.cursor() as cur:
            cur.execute("SELECT EXECUTEAUTH FROM SYSCAT.ROUTINEAUTH WHERE SCHEMA=? AND ROUTINENAME=? AND GRANTEE=?", (schema_name, grant["object_name"], grant["grantee"]))
            row = cur.fetchone()
            return bool(row and str(row[0]).strip().upper() in ("Y", "G"))
    return False


def apply_grant_if_possible(conn: Any, schema_name: str, grant: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(grant["grantee"], "grantee")
    if str(grant["grantee"]).upper() == "PUBLIC":
        raise RuntimeError("Grant to PUBLIC requires security approval")
    if _existing_target_privilege(conn, schema_name, grant):
        return "already_present"
    # build SQL
    priv = grant["privilege"]
    grantee = grant["grantee"]
    gtype = grant.get("grantee_type", "U")
    to_clause = "TO USER" if gtype in ("U",) else "TO GROUP" if gtype == "G" else "TO ROLE" if gtype == "R" else "TO"
    if grant["object_type"] == "SCHEMA":
        sql = f"GRANT CREATEIN ON SCHEMA {schema_name} {to_clause} {grantee}".replace("TO  ", "TO ")
    elif grant["object_type"] == "TABLE":
        validate_identifier(grant["object_name"], "object")
        sql = f"GRANT {priv} ON TABLE {schema_name}.{grant['object_name']} {to_clause} {grantee}".replace("TO  ", "TO ")
    elif grant["object_type"] == "COLUMN":
        validate_identifier(grant["object_name"], "object")
        validate_identifier(grant["column"], "column")
        sql = f"GRANT {priv} ({grant['column']}) ON TABLE {schema_name}.{grant['object_name']} {to_clause} {grantee}".replace("TO  ", "TO ")
    elif grant["object_type"] == "SEQUENCE":
        validate_identifier(grant["object_name"], "object")
        sql = f"GRANT USAGE ON SEQUENCE {schema_name}.{grant['object_name']} {to_clause} {grantee}".replace("TO  ", "TO ")
    elif grant["object_type"] == "ROUTINE":
        validate_identifier(grant["object_name"], "object")
        sql = f"GRANT EXECUTE ON FUNCTION {schema_name}.{grant['object_name']} {to_clause} {grantee}".replace("TO  ", "TO ")
    else:
        raise RuntimeError(f"Unsupported grant object type: {grant['object_type']}")
    with conn.cursor() as cur:
        cur.execute(sql)
    return "granted"


from core.connectors.base import validate_identifier


def check_existing_db2_grant(conn: Any, schema_name: str, grant: dict) -> str:
    validate_identifier(schema_name, "schema")
    validate_identifier(grant["grantee"], "grantee")
    obj_type = str(grant.get("object_type", "")).upper()
    grantee = grant["grantee"]
    gtype = grant.get("grantee_type", "U")
    priv = str(grant.get("privilege", "")).upper()

    if obj_type == "SCHEMA":
        if priv != "CREATEIN":
            return "unsupported"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT CREATEINAUTH FROM SYSCAT.SCHEMAAUTH WHERE SCHEMANAME=? AND GRANTEE=? AND GRANTEETYPE=?",
                (schema_name, grantee, gtype),
            )
            row = cur.fetchone()
        return "already_present" if row and str(row[0]).strip().upper() in ("Y", "G") else "missing"

    if obj_type == "TABLE":
        col = {"SELECT": "SELECTAUTH", "INSERT": "INSERTAUTH", "UPDATE": "UPDATEAUTH", "DELETE": "DELETEAUTH"}.get(priv)
        if not col:
            return "unsupported"
        validate_identifier(grant["object_name"], "object")
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {col} FROM SYSCAT.TABAUTH WHERE TABSCHEMA=? AND TABNAME=? AND GRANTEE=? AND GRANTEETYPE=?",
                (schema_name, grant["object_name"], grantee, gtype),
            )
            row = cur.fetchone()
        return "already_present" if row and str(row[0]).strip().upper() in ("Y", "G") else "missing"

    if obj_type == "COLUMN":
        priv_map = {"SELECT": "S", "UPDATE": "U", "REFERENCES": "R"}
        if priv not in priv_map:
            return "unsupported"
        validate_identifier(grant["object_name"], "object")
        validate_identifier(grant["column"], "column")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT PRIVTYPE, GRANTABLE FROM SYSCAT.COLAUTH WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=? AND GRANTEE=? AND GRANTEETYPE=?",
                (schema_name, grant["object_name"], grant["column"], grantee, gtype),
            )
            rows = cur.fetchall()
        for r in rows:
            if str(r[0]).strip().upper() == priv_map[priv]:
                return "already_present"
        # Effective access: table-wide UPDATE/SELECT satisfies column-level requirement.
        if priv in ("SELECT", "UPDATE"):
            col = "SELECTAUTH" if priv == "SELECT" else "UPDATEAUTH"
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {col} FROM SYSCAT.TABAUTH WHERE TABSCHEMA=? AND TABNAME=? AND GRANTEE=? AND GRANTEETYPE=?",
                    (schema_name, grant["object_name"], grantee, gtype),
                )
                row = cur.fetchone()
            if row and str(row[0]).strip().upper() in ("Y", "G"):
                return "already_present"
        return "missing"

    if obj_type == "SEQUENCE":
        if priv != "USAGE":
            return "unsupported"
        validate_identifier(grant["object_name"], "object")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT USAGEAUTH FROM SYSCAT.SEQUENCEAUTH WHERE SEQSCHEMA=? AND SEQNAME=? AND GRANTEE=? AND GRANTEETYPE=?",
                (schema_name, grant["object_name"], grantee, gtype),
            )
            row = cur.fetchone()
        return "already_present" if row and str(row[0]).strip().upper() in ("Y", "G") else "missing"

    if obj_type == "ROUTINE":
        if priv != "EXECUTE":
            return "unsupported"
        validate_identifier(grant["object_name"], "object")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SPECIFICNAME FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?",
                (schema_name, grant["object_name"]),
            )
            specifics = [str(r[0]).strip() for r in cur.fetchall() if r and r[0] is not None]
            if not specifics:
                return "missing"
            placeholders = ",".join("?" for _ in specifics)
            cur.execute(
                f"SELECT SPECIFICNAME, EXECUTEAUTH FROM SYSCAT.ROUTINEAUTH WHERE SCHEMA=? AND SPECIFICNAME IN ({placeholders}) AND GRANTEE=? AND GRANTEETYPE=?",
                (schema_name, *specifics, grantee, gtype),
            )
            rows = cur.fetchall()
        # Normalize rows that may lack SPECIFICNAME (backward compat).
        norm = []
        for r in rows:
            if len(r) == 1:
                norm.append((None, r[0]))
            else:
                norm.append((r[0], r[1]))
        if not norm:
            return "missing"
        distinct = {str(r[0]).strip() for r in norm if r[0] is not None}
        if not distinct and len(norm) > 1:
            return "unresolved"
        if len(distinct) > 1:
            # ambiguous overloads: only unresolved if no usable EXECUTE
            if any(str(r[1]).strip().upper() in ("Y", "G") for r in norm):
                # Could be satisfied but ambiguous identity; prefer unresolved to avoid wrong grant
                return "unresolved"
            return "unresolved"
        return "already_present" if any(str(r[1]).strip().upper() in ("Y", "G") for r in norm) else "missing"

    return "unsupported"


def plan_db2_grant(conn: Any, schema_name: str, grant: dict) -> dict:
    validate_identifier(schema_name, "schema")
    status = check_existing_db2_grant(conn, schema_name, grant)
    result = {
        "status": status,
        "object_type": grant.get("object_type"),
        "schema": schema_name,
        "object_name": grant.get("object_name"),
        "privilege": grant.get("privilege"),
        "grantee": grant.get("grantee"),
        "grantee_type": grant.get("grantee_type"),
        "sql": None,
        "reason": None,
    }
    if status == "already_present":
        result["reason"] = "Equivalent or stronger privilege already exists on target"
        return result
    if status == "unresolved":
        result["reason"] = "Routine identity could not be resolved safely"
        return result
    if status == "unsupported":
        result["reason"] = "Unsupported object type or privilege"
        return result

    grantee = str(grant["grantee"]).strip()
    gtype = str(grant.get("grantee_type", "U")).strip().upper()
    if grantee.upper() == "PUBLIC":
        result["status"] = "approval_required"
        result["reason"] = "Grant to PUBLIC requires security approval"
        return result
    if gtype not in ("U", "G", "R"):
        result["status"] = "unsupported"
        result["reason"] = f"Unsupported grantee type: {gtype}"
        return result

    to_clause = "TO USER" if gtype == "U" else "TO GROUP" if gtype == "G" else "TO ROLE"
    priv = str(grant["privilege"]).upper()
    obj_type = str(grant["object_type"]).upper()
    grantable = bool(grant.get("grantable", False))
    grant_option = " WITH GRANT OPTION" if grantable else ""

    try:
        if obj_type == "SCHEMA":
            validate_identifier(grant["object_name"], "object")
            if priv != "CREATEIN":
                raise ValueError("unsupported privilege")
            sql = f"GRANT CREATEIN ON SCHEMA {grant['object_name']} {to_clause} {grantee}{grant_option}"
        elif obj_type == "TABLE":
            validate_identifier(grant["object_name"], "object")
            if priv not in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                raise ValueError("unsupported privilege")
            sql = f"GRANT {priv} ON TABLE {schema_name}.{grant['object_name']} {to_clause} {grantee}{grant_option}"
        elif obj_type == "COLUMN":
            validate_identifier(grant["object_name"], "object")
            validate_identifier(grant["column"], "column")
            if priv not in ("SELECT", "UPDATE", "REFERENCES"):
                raise ValueError("unsupported privilege")
            sql = f"GRANT {priv} ({grant['column']}) ON TABLE {schema_name}.{grant['object_name']} {to_clause} {grantee}{grant_option}"
        elif obj_type == "SEQUENCE":
            validate_identifier(grant["object_name"], "object")
            if priv != "USAGE":
                raise ValueError("unsupported privilege")
            sql = f"GRANT USAGE ON SEQUENCE {schema_name}.{grant['object_name']} {to_clause} {grantee}{grant_option}"
        elif obj_type == "ROUTINE":
            validate_identifier(grant["object_name"], "object")
            if priv != "EXECUTE":
                raise ValueError("unsupported privilege")
            routine_type = str(grant.get("routine_type", "")).upper()
            keyword = "PROCEDURE" if routine_type == "P" else "FUNCTION"
            sql = f"GRANT EXECUTE ON {keyword} {schema_name}.{grant['object_name']} {to_clause} {grantee}{grant_option}"
        else:
            raise ValueError("unsupported object type")
    except Exception as exc:
        result["status"] = "unsupported"
        result["reason"] = str(exc)
        return result

    result["status"] = "would_grant"
    result["sql"] = sql
    result["reason"] = "Missing privilege; GRANT required"
    return result


def _connected_user_grantee_type(conn: Any) -> str | None:
    """Determine the grantee type ('U'/'G'/'R') of the connected principal.

    Db2 does not expose a single column for the connected user's grantee type;
    SYSCAT.DBAUTH lists every grantee with database-level authority. Query it
    for CURRENT USER. Returns None when the type cannot be determined so the
    caller fails closed rather than assuming 'U'.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT GRANTEETYPE FROM SYSCAT.DBAUTH WHERE GRANTEE = CURRENT USER FETCH FIRST 1 ROW ONLY"
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).strip().upper()
    except Exception:
        return None
    return None


def assess_db2_grant_authority(conn: Any, schema_name: str, grant: dict) -> tuple[bool, str]:
    validate_identifier(schema_name, "schema")
    try:
        with conn.cursor() as cur:
            cur.execute("VALUES CURRENT USER")
            row = cur.fetchone()
            current_user = str(row[0]).strip().upper() if row else ""
            cur.execute("SELECT OWNER FROM SYSCAT.SCHEMATA WHERE SCHEMANAME=?", (schema_name,))
            row = cur.fetchone()
            schema_owner = str(row[0]).strip().upper() if row else ""

            current_user_type = _connected_user_grantee_type(conn)
            if not current_user_type:
                return False, "indeterminate: connected principal grantee type could not be determined; authority check fails closed"

            obj_type = str(grant.get("object_type", "")).upper()
            obj_name = str(grant.get("object_name", "")).strip()
            priv = str(grant.get("privilege", "")).upper()

            owner = schema_owner
            if obj_type in ("TABLE", "COLUMN"):
                cur.execute("SELECT OWNER FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (schema_name, obj_name))
                row = cur.fetchone()
                if row and row[0] is not None:
                    owner = str(row[0]).strip().upper()
            elif obj_type == "ROUTINE":
                cur.execute("SELECT OWNER FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?", (schema_name, obj_name))
                row = cur.fetchone()
                if row and row[0] is not None:
                    owner = str(row[0]).strip().upper()
            elif obj_type == "SEQUENCE":
                cur.execute("SELECT OWNER FROM SYSCAT.SEQUENCES WHERE SEQSCHEMA=? AND SEQNAME=?", (schema_name, obj_name))
                row = cur.fetchone()
                if row and row[0] is not None:
                    owner = str(row[0]).strip().upper()

            if current_user and owner and current_user == owner:
                return True, "object owner"

            grantable = False
            if obj_type == "SCHEMA" and priv == "CREATEIN":
                cur.execute("SELECT CREATEINAUTH FROM SYSCAT.SCHEMAAUTH WHERE SCHEMANAME=? AND GRANTEE=? AND GRANTEETYPE=?", (schema_name, current_user, current_user_type))
                row = cur.fetchone()
                grantable = bool(row and str(row[0]).strip().upper() == "G")
            elif obj_type == "TABLE":
                col = {"SELECT": "SELECTAUTH", "INSERT": "INSERTAUTH", "UPDATE": "UPDATEAUTH", "DELETE": "DELETEAUTH"}.get(priv)
                if col:
                    cur.execute(f"SELECT {col} FROM SYSCAT.TABAUTH WHERE TABSCHEMA=? AND TABNAME=? AND GRANTEE=? AND GRANTEETYPE=?", (schema_name, obj_name, current_user, current_user_type))
                    row = cur.fetchone()
                    grantable = bool(row and str(row[0]).strip().upper() == "G")
            elif obj_type == "COLUMN":
                priv_map = {"SELECT": "S", "UPDATE": "U", "REFERENCES": "R"}
                ptype = priv_map.get(priv)
                if ptype:
                    cur.execute("SELECT GRANTABLE FROM SYSCAT.COLAUTH WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=? AND GRANTEE=? AND GRANTEETYPE=? AND PRIVTYPE=?", (schema_name, obj_name, grant.get("column"), current_user, current_user_type, ptype))
                    rows = cur.fetchall()
                    grantable = any(str(r[0]).strip().upper() in ("Y", "G") for r in rows)
            elif obj_type == "SEQUENCE" and priv == "USAGE":
                cur.execute("SELECT USAGEAUTH FROM SYSCAT.SEQUENCEAUTH WHERE SEQSCHEMA=? AND SEQNAME=? AND GRANTEE=? AND GRANTEETYPE=?", (schema_name, obj_name, current_user, current_user_type))
                row = cur.fetchone()
                grantable = bool(row and str(row[0]).strip().upper() == "G")
            elif obj_type == "ROUTINE" and priv == "EXECUTE":
                cur.execute("SELECT EXECUTEAUTH FROM SYSCAT.ROUTINEAUTH WHERE SCHEMA=? AND ROUTINENAME=? AND GRANTEE=? AND GRANTEETYPE=?", (schema_name, obj_name, current_user, current_user_type))
                rows = cur.fetchall()
                grantable = any(str(r[0]).strip().upper() == "G" for r in rows)

            if grantable:
                return True, "grantable privilege held"

            cur.execute("SELECT DBADMAUTH, SECURITYADMAUTH FROM SYSCAT.DBAUTH WHERE GRANTEE=? AND GRANTEETYPE=?", (current_user, current_user_type))
            row = cur.fetchone()
            if row:
                if str(row[0]).strip().upper() in ("Y", "G") or str(row[1]).strip().upper() in ("Y", "G"):
                    return True, "DBADM/SECADM authority"
    except Exception as exc:
        return False, f"indeterminate: {exc}"

    return False, "indeterminate: connected principal lacks WITH GRANT OPTION on the target object; administrator must grant the privilege WITH GRANT OPTION (or DBADM/SECADM) before migration"


def apply_planned_db2_grant(conn: Any, schema_name: str, grant: dict) -> dict:
    plan = plan_db2_grant(conn, schema_name, grant)
    status = plan["status"]
    if status == "already_present":
        return plan
    if status in ("unsupported", "unresolved", "approval_required"):
        plan["status"] = "blocked" if status == "approval_required" else status
        return plan
    if status != "would_grant":
        return plan

    allowed, reason = assess_db2_grant_authority(conn, schema_name, grant)
    if not allowed:
        plan["status"] = "authorization_failed"
        plan["reason"] = reason
        return plan

    # recheck before execution
    recheck = check_existing_db2_grant(conn, schema_name, grant)
    if recheck == "already_present":
        plan["status"] = "already_present"
        plan["sql"] = None
        plan["reason"] = "Privilege appeared before execution"
        return plan

    sql = plan["sql"]
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        plan["status"] = "granted"
        plan["reason"] = "Grant executed"
        return plan
    except Exception as exc:
        msg = str(exc)
        if "SQL0551N" in msg or "42501" in msg:
            plan["status"] = "authorization_failed"
        else:
            plan["status"] = "failed"
        plan["reason"] = msg
        return plan
