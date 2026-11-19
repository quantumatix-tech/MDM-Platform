from __future__ import annotations

from core.connectors.base import validate_identifier


def _existing_default(conn, schema, table, column):
    with conn.cursor() as cur:
        cur.execute("SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (schema, table))
        if not cur.fetchone():
            return ("object_missing", None)
        cur.execute("SELECT COLNAME FROM SYSCAT.COLUMNS WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=?", (schema, table, column))
        if not cur.fetchone():
            return ("object_missing", None)
        cur.execute("SELECT CAST(DEFAULT AS VARCHAR(32672)) FROM SYSCAT.COLUMNS WHERE TABSCHEMA=? AND TABNAME=? AND COLNAME=?", (schema, table, column))
        row = cur.fetchone()
    val = None if not row or row[0] is None else str(row[0])
    return ("ok", val)

def plan_default(conn, schema_name, table, column, source_default):
    validate_identifier(schema_name, "schema")
    validate_identifier(table, "table")
    validate_identifier(column, "column")
    status, target = _existing_default(conn, schema_name, table, column)
    if status == "object_missing":
        return {"status": "object_missing", "sql": None, "reason": "target object missing"}
    if source_default in (None, ""):
        return {"status": "already_present", "sql": None, "reason": "no source default"}
    if target == source_default:
        return {"status": "already_present", "sql": None, "reason": "default matches"}
    if target not in (None, ""):
        return {"status": "conflict", "sql": None, "reason": "target default differs"}
    sql = f"ALTER TABLE {schema_name}.{table} ALTER COLUMN {column} SET DEFAULT {source_default}"
    return {"status": "would_apply", "sql": sql, "reason": "missing default"}

def _existing_check(conn, schema, table, name):
    with conn.cursor() as cur:
        cur.execute("SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (schema, table))
        if not cur.fetchone():
            return ("object_missing", None)
        cur.execute("SELECT CAST(c.TEXT AS VARCHAR(32672)) FROM SYSCAT.TABCONST tc JOIN SYSCAT.CHECKS c ON tc.CONSTNAME=c.CONSTNAME AND tc.TABSCHEMA=c.TABSCHEMA AND tc.TABNAME=c.TABNAME WHERE tc.TABSCHEMA=? AND tc.TABNAME=? AND tc.CONSTNAME=?", (schema, table, name))
        row = cur.fetchone()
    if not row:
        return ("missing", None)
    return ("ok", None if row[0] is None else str(row[0]))

def plan_check(conn, schema_name, table, name, expression):
    validate_identifier(schema_name, "schema")
    validate_identifier(table, "table")
    validate_identifier(name, "constraint")
    status, target_expr = _existing_check(conn, schema_name, table, name)
    if status == "object_missing":
        return {"status": "object_missing", "sql": None, "reason": "target table missing"}
    if target_expr is None and status == "missing":
        sql = f"ALTER TABLE {schema_name}.{table} ADD CONSTRAINT {name} CHECK ({expression})"
        return {"status": "would_apply", "sql": sql, "reason": "missing check"}
    if target_expr == expression:
        return {"status": "already_present", "sql": None, "reason": "check matches"}
    return {"status": "conflict", "sql": None, "reason": "check definition differs"}


def apply_planned_default(conn, schema_name, table, column, source_default):
    plan = plan_default(conn, schema_name, table, column, source_default)
    if plan["status"] in ("already_present", "object_missing", "conflict", "unsupported"):
        return plan
    if plan["status"] != "would_apply":
        return plan
    # recheck
    recheck = plan_default(conn, schema_name, table, column, source_default)
    if recheck["status"] == "already_present":
        plan["status"] = "already_present"
        plan["sql"] = None
        plan["reason"] = "appeared before execution"
        return plan
    try:
        with conn.cursor() as cur:
            cur.execute(plan["sql"])
        plan["status"] = "applied"
        plan["reason"] = "default applied"
        return plan
    except Exception as exc:
        plan["status"] = "failed"
        sqlstate = None
        msg = str(exc)
        if hasattr(exc, 'args') and exc.args:
            if len(exc.args) > 0 and isinstance(exc.args[0], str):
                sqlstate = exc.args[0]
            if len(exc.args) > 1:
                msg = exc.args[1]
        plan["reason"] = f"SQLSTATE={sqlstate} {msg}" if sqlstate else msg
        return plan


def apply_planned_check(conn, schema_name, table, name, expression):
    plan = plan_check(conn, schema_name, table, name, expression)
    if plan["status"] in ("already_present", "object_missing", "conflict", "unsupported"):
        return plan
    if plan["status"] != "would_apply":
        return plan
    recheck = plan_check(conn, schema_name, table, name, expression)
    if recheck["status"] == "already_present":
        plan["status"] = "already_present"
        plan["sql"] = None
        plan["reason"] = "appeared before execution"
        return plan
    try:
        with conn.cursor() as cur:
            cur.execute(plan["sql"])
        plan["status"] = "applied"
        plan["reason"] = "check applied"
        return plan
    except Exception as exc:
        plan["status"] = "failed"
        sqlstate = None
        msg = str(exc)
        if hasattr(exc, 'args') and exc.args:
            if len(exc.args) > 0 and isinstance(exc.args[0], str):
                sqlstate = exc.args[0]
            if len(exc.args) > 1:
                msg = exc.args[1]
        plan["reason"] = f"SQLSTATE={sqlstate} {msg}" if sqlstate else msg
        return plan
