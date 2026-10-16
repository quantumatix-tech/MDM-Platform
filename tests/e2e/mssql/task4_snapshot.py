#!/usr/bin/env python
"""Capture read-only MSSQL catalog snapshots for Task 4 live verification.

Passwords are resolved only from the configured environment secret names.
The snapshot stores module hashes, not SQL source text or credentials.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pyodbc

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.mssql.setup.database import _resolve_connection_params, make_conn_str
from tests.e2e.mssql.validation.sql_normalize import normalize_sql_definition


def _rows(conn: pyodbc.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [item[0] for item in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _normalized_module_hash(definition: str | None) -> str | None:
    if definition is None:
        return None
    text = normalize_sql_definition(definition)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_COMPARISON_CATEGORIES = (
    "inventory", "modules", "sequences", "views", "indexes", "constraints",
    "grants", "role_memberships", "dependencies", "partition_functions",
    "partition_schemes", "partition_assignments", "extended_properties",
)


def _stable_row(row: dict[str, Any], category: str) -> str:
    value = dict(row)
    if category == "inventory":
        value.pop("parent_object_id", None)
    if category == "modules":
        value.pop("definition_sha256", None)
        value.pop("definition_text", None)
    return json.dumps(value, sort_keys=True, default=str)


def compare_snapshot_sides(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compare source/target catalog rows, normalized module bodies, and type conflicts."""
    source, target = snapshot["source"], snapshot["target"]
    result: dict[str, Any] = {
        "categories": {}, "module_definition_differences": [], "type_conflicts": [],
    }
    src_inventory = {(r.get("schema_name"), r.get("object_name")): r
                     for r in source.get("inventory", [])}
    tgt_inventory = {(r.get("schema_name"), r.get("object_name")): r
                     for r in target.get("inventory", [])}
    for key in sorted(src_inventory.keys() & tgt_inventory.keys()):
        src_type = src_inventory[key].get("object_type_desc")
        tgt_type = tgt_inventory[key].get("object_type_desc")
        if src_type != tgt_type:
            result["type_conflicts"].append({
                "schema_name": key[0], "object_name": key[1],
                "source_type": src_type, "target_type": tgt_type,
            })

    for category in _COMPARISON_CATEGORIES:
        src_rows, tgt_rows = source.get(category, []), target.get(category, [])
        src_counts = Counter(_stable_row(row, category) for row in src_rows)
        tgt_counts = Counter(_stable_row(row, category) for row in tgt_rows)
        src_by_key = {_stable_row(row, category): row for row in src_rows}
        tgt_by_key = {_stable_row(row, category): row for row in tgt_rows}
        missing, extra = src_counts - tgt_counts, tgt_counts - src_counts
        result["categories"][category] = {
            "source_count": len(src_rows), "target_count": len(tgt_rows),
            "source_only": [src_by_key[k] for k in sorted(missing) for _ in range(missing[k])],
            "target_only": [tgt_by_key[k] for k in sorted(extra) for _ in range(extra[k])],
        }

    src_modules = {(r.get("schema_name"), r.get("object_name"), r.get("type_desc")): r
                   for r in source.get("modules", [])}
    tgt_modules = {(r.get("schema_name"), r.get("object_name"), r.get("type_desc")): r
                   for r in target.get("modules", [])}
    for key in sorted(src_modules.keys() & tgt_modules.keys()):
        src_hash = src_modules[key].get("definition_normalized_sha256")
        tgt_hash = tgt_modules[key].get("definition_normalized_sha256")
        if src_hash != tgt_hash:
            result["module_definition_differences"].append({
                "schema_name": key[0], "object_name": key[1], "type_desc": key[2],
                "source_normalized_sha256": src_hash,
                "target_normalized_sha256": tgt_hash,
            })
    return result


def compare_target_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Report exact target catalog additions/removals between two snapshots."""
    result = {}
    for category in _COMPARISON_CATEGORIES:
        old_rows, new_rows = before["target"].get(category, []), after["target"].get(category, [])
        old_counts = Counter(_stable_row(row, category) for row in old_rows)
        new_counts = Counter(_stable_row(row, category) for row in new_rows)
        old_by_key = {_stable_row(row, category): row for row in old_rows}
        new_by_key = {_stable_row(row, category): row for row in new_rows}
        removed, added = old_counts - new_counts, new_counts - old_counts
        result[category] = {
            "removed": [old_by_key[k] for k in sorted(removed) for _ in range(removed[k])],
            "added": [new_by_key[k] for k in sorted(added) for _ in range(added[k])],
        }
    return result


def _capture_side(cfg, side: str, database: str) -> dict[str, Any]:
    c = cfg.raw[side]["connection"]
    p = _resolve_connection_params(c["host"], c["port"], c["username"], c["password_secret"])
    conn = pyodbc.connect(make_conn_str(p["host"], p["port"], p["username"], p["password"], database), timeout=10)
    try:
        product_major = _rows(conn, "SELECT TRY_CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) AS product_major")[0]["product_major"]
        last_used_column = "CONVERT(varchar(50),q.last_used_value)" if product_major and int(product_major) >= 14 else "CAST(NULL AS varchar(50))"
        result: dict[str, Any] = {
            "identity": _rows(conn, "SELECT DB_NAME() AS database_name, ORIGINAL_LOGIN() AS login_name, @@SERVERNAME AS server_name"),
            "inventory": _rows(conn, """
                SELECT s.name AS schema_name, o.name AS object_name,
                       o.type AS object_type, o.type_desc AS object_type_desc,
                       o.parent_object_id
                FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
                WHERE o.is_ms_shipped=0 AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,o.name,o.type
            """),
            "modules": _rows(conn, """
                SELECT s.name AS schema_name, o.name AS object_name, o.type_desc,
                       m.uses_ansi_nulls, m.uses_quoted_identifier,
                       CONVERT(varchar(64), HASHBYTES('SHA2_256',
                           CONVERT(varbinary(max), REPLACE(REPLACE(m.definition, CHAR(13)+CHAR(10), CHAR(10)), CHAR(13), CHAR(10)))), 2) AS definition_sha256,
                       m.definition AS definition_text
                FROM sys.sql_modules m JOIN sys.objects o ON o.object_id=m.object_id
                JOIN sys.schemas s ON s.schema_id=o.schema_id
                WHERE o.is_ms_shipped=0 AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,o.name,o.type_desc
            """),
            "sequences": _rows(conn, f"""
                SELECT s.name AS schema_name, q.name AS sequence_name,
                       TYPE_NAME(q.user_type_id) AS data_type,
                       CONVERT(varchar(50),q.start_value) AS start_value,
                       CONVERT(varchar(50),q.increment) AS increment,
                       CONVERT(varchar(50),q.minimum_value) AS minimum_value,
                       CONVERT(varchar(50),q.maximum_value) AS maximum_value,
                       CONVERT(varchar(50),q.current_value) AS current_value,
                       {last_used_column} AS last_used_value,
                       q.is_exhausted,
                       q.is_cycling, q.is_cached, q.cache_size
                FROM sys.sequences q JOIN sys.schemas s ON s.schema_id=q.schema_id
                WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,q.name
            """),
            "views": _rows(conn, """
                SELECT s.name AS schema_name, v.name AS view_name,
                       CONVERT(int, OBJECTPROPERTY(v.object_id,'IsSchemaBound')) AS is_schema_bound,
                       i.name AS index_name, i.type_desc AS index_type,
                       i.is_unique, ic.key_ordinal, ic.is_included_column, c.name AS column_name
                FROM sys.views v JOIN sys.schemas s ON s.schema_id=v.schema_id
                LEFT JOIN sys.indexes i ON i.object_id=v.object_id AND i.index_id>0
                LEFT JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
                LEFT JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
                WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,v.name,i.index_id,ic.key_ordinal,ic.index_column_id
            """),
            "indexes": _rows(conn, """
                SELECT s.name AS schema_name, o.name AS object_name, i.name AS index_name,
                       i.type_desc, i.is_unique, i.is_primary_key, i.is_disabled,
                       i.has_filter, i.filter_definition, ic.key_ordinal,
                       ic.is_included_column, c.name AS column_name
                FROM sys.indexes i JOIN sys.objects o ON o.object_id=i.object_id
                JOIN sys.schemas s ON s.schema_id=o.schema_id
                LEFT JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
                LEFT JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
                WHERE o.is_ms_shipped=0 AND i.index_id>0
                  AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,o.name,i.index_id,ic.key_ordinal,ic.index_column_id
            """),
            "constraints": _rows(conn, """
                SELECT s.name AS schema_name, t.name AS table_name,
                       o.name AS constraint_name, o.type_desc,
                       cc.definition, cc.is_disabled, cc.is_not_trusted,
                       dc.definition AS default_definition,
                       fk.delete_referential_action_desc, fk.update_referential_action_desc,
                       fk.is_disabled AS fk_is_disabled, fk.is_not_trusted AS fk_is_not_trusted
                FROM sys.objects o JOIN sys.tables t ON t.object_id=o.parent_object_id
                JOIN sys.schemas s ON s.schema_id=t.schema_id
                LEFT JOIN sys.check_constraints cc ON cc.object_id=o.object_id
                LEFT JOIN sys.default_constraints dc ON dc.object_id=o.object_id
                LEFT JOIN sys.foreign_keys fk ON fk.object_id=o.object_id
                WHERE o.type IN ('C','D','F','PK','UQ') AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,t.name,o.name
            """),
            "grants": _rows(conn, """
                SELECT grantee.name AS grantee, grantee.type_desc AS grantee_type,
                       grantor.name AS grantor, p.state_desc, p.permission_name,
                       p.class_desc, p.major_id, p.minor_id,
                       CASE WHEN p.state_desc='GRANT_WITH_GRANT_OPTION' THEN 1 ELSE 0 END AS grant_option,
                       OBJECT_SCHEMA_NAME(p.major_id) AS object_schema,
                       OBJECT_NAME(p.major_id) AS object_name,
                       col.name AS column_name
                FROM sys.database_permissions p
                JOIN sys.database_principals grantee ON grantee.principal_id=p.grantee_principal_id
                JOIN sys.database_principals grantor ON grantor.principal_id=p.grantor_principal_id
                LEFT JOIN sys.columns col ON p.class=1 AND col.object_id=p.major_id AND col.column_id=p.minor_id
                ORDER BY grantee.name,p.class_desc,p.major_id,p.minor_id,p.permission_name,p.state_desc
            """),
            "role_memberships": _rows(conn, """
                SELECT rolep.name AS role_name, memberp.name AS member_name
                FROM sys.database_role_members rm
                JOIN sys.database_principals rolep ON rolep.principal_id=rm.role_principal_id
                JOIN sys.database_principals memberp ON memberp.principal_id=rm.member_principal_id
                ORDER BY rolep.name,memberp.name
            """),
            "dependencies": _rows(conn, """
                SELECT OBJECT_SCHEMA_NAME(d.referencing_id) AS referencing_schema,
                       OBJECT_NAME(d.referencing_id) AS referencing_object,
                       o.type_desc AS referencing_type,
                       d.referenced_server_name, d.referenced_database_name,
                       d.referenced_schema_name, d.referenced_entity_name,
                       d.is_schema_bound_reference
                FROM sys.sql_expression_dependencies d
                LEFT JOIN sys.objects o ON o.object_id=d.referencing_id
                WHERE d.referencing_id IS NOT NULL
                  AND OBJECTPROPERTY(d.referencing_id,'IsMSShipped')=0
                ORDER BY referencing_schema,referencing_object,referenced_schema_name,referenced_entity_name
            """),
            "partition_functions": _rows(conn, """
                SELECT pf.name, pf.fanout, pf.boundary_value_on_right,
                       TYPE_NAME(pp.user_type_id) AS parameter_type,
                       prv.boundary_id, CONVERT(nvarchar(100),prv.value,126) AS boundary_value
                FROM sys.partition_functions pf
                JOIN sys.partition_parameters pp ON pp.function_id=pf.function_id
                LEFT JOIN sys.partition_range_values prv ON prv.function_id=pf.function_id
                ORDER BY pf.name,prv.boundary_id
            """),
            "partition_schemes": _rows(conn, """
                SELECT ps.name AS scheme_name, pf.name AS function_name,
                       dds.destination_id, ds.name AS filegroup_name
                FROM sys.partition_schemes ps
                JOIN sys.partition_functions pf ON pf.function_id=ps.function_id
                JOIN sys.destination_data_spaces dds ON dds.partition_scheme_id=ps.data_space_id
                JOIN sys.data_spaces ds ON ds.data_space_id=dds.data_space_id
                ORDER BY ps.name,dds.destination_id
            """),
            "partition_assignments": _rows(conn, """
                SELECT s.name AS schema_name,t.name AS table_name,i.name AS index_name,
                       i.type_desc, ps.name AS scheme_name,pf.name AS function_name,
                       c.name AS partition_column
                FROM sys.indexes i JOIN sys.tables t ON t.object_id=i.object_id
                JOIN sys.schemas s ON s.schema_id=t.schema_id
                LEFT JOIN sys.partition_schemes ps ON ps.data_space_id=i.data_space_id
                LEFT JOIN sys.partition_functions pf ON pf.function_id=ps.function_id
                LEFT JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
                    AND ic.partition_ordinal>0
                LEFT JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
                WHERE i.index_id>0 AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
                ORDER BY s.name,t.name,i.name
            """),
            "extended_properties": _rows(conn, """
                SELECT s.name AS schema_name,o.name AS object_name,o.type_desc,
                       ep.name AS property_name,CONVERT(nvarchar(max),ep.value) AS property_value,
                       ep.minor_id AS column_id,c.name AS column_name
                FROM sys.extended_properties ep
                LEFT JOIN sys.objects o ON o.object_id=ep.major_id
                LEFT JOIN sys.schemas s ON s.schema_id=COALESCE(o.schema_id,ep.major_id)
                LEFT JOIN sys.columns c ON c.object_id=ep.major_id AND c.column_id=ep.minor_id
                WHERE ep.class IN (1,3)
                ORDER BY s.name,o.name,ep.minor_id,ep.name
            """),
        }
        normalized: dict[str, Any] = {}
        for key, rows in result.items():
            converted = []
            for row in rows:
                item = {k: _json_value(v) for k, v in row.items()}
                if key == "modules":
                    item["definition_normalized_sha256"] = _normalized_module_hash(item.pop("definition_text"))
                converted.append(item)
            normalized[key] = converted
        return normalized
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/mssql_e2e_acceptance.yaml")
    parser.add_argument("--output", required=True, help="Snapshot JSON path")
    parser.add_argument("--baseline", help="Optional earlier snapshot for target idempotency comparison")
    args = parser.parse_args()
    cfg = load_e2e_config(args.config)
    snapshot = {
        "source": _capture_side(cfg, "source", cfg.source_db),
        "target": _capture_side(cfg, "target", cfg.target_db),
    }
    snapshot["comparison"] = compare_snapshot_sides(snapshot)
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        snapshot["target_delta_from_baseline"] = compare_target_snapshots(baseline, snapshot)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Catalog snapshot saved: {out}")
    print(f"source inventory={len(snapshot['source']['inventory'])}, target inventory={len(snapshot['target']['inventory'])}")
    print(f"source grants={len(snapshot['source']['grants'])}, target grants={len(snapshot['target']['grants'])}")
    for side in ("source", "target"):
        ident = snapshot[side]["identity"][0]
        print(f"{side}: database={ident['database_name']} login={ident['login_name']} server={ident['server_name']}")


if __name__ == "__main__":
    main()
