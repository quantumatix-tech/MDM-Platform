"""Read-only Oracle E2E validation against YAML-defined expectations."""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from core.connectors.base import quote_identifier
from core.connectors.oracle import permissions as oracle_permissions
from core.connectors.oracle.indexes import normalize_index_rows
from core.connectors.oracle.objects import (
    _get_ddl,
    _remap_owner_qualifiers,
    discover_comments,
    discover_table_partitioning,
)
from core.connectors.oracle.objects.type import serialize_object
from core.connectors.oracle.precision import decimal_output_type_handler
from core.secrets import create_secret_provider
from tests.e2e.acceptance import E2EConfig, load_e2e_config, write_acceptance_report
from tests.e2e.oracle.config import (
    configured_source_table_allowlist,
    connect,
    connection_config,
    expected_foreign_key_relations,
    fixture_names_and_expected_tables,
    resolver_for_side,
    validated_oracle_context,
)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _permission_executor_connection_config(cfg: E2EConfig, target_config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the configured read-only permission executor without logging secrets."""
    policy = cfg.raw.get("oracle_permission_migration")
    executor = policy.get("executor") if isinstance(policy, dict) else None
    if not isinstance(executor, dict) or not executor.get("username"):
        raise RuntimeError("Configured Oracle permission executor is incomplete")
    if executor.get("target_credentials") is True:
        if str(executor["username"]).casefold() != str(target_config["username"]).casefold():
            raise RuntimeError("Oracle permission executor identity does not match TARGET credentials")
        secret = target_config["password"]
    else:
        secret_name = executor.get("password_secret")
        if not isinstance(secret_name, str) or not secret_name:
            raise RuntimeError("Configured Oracle permission executor secret reference is missing")
        provider_name = policy.get("secret_provider")
        resolver = create_secret_provider(cfg.raw, provider_name)
        try:
            secret = resolver.resolve(secret_name) if resolver is not None else None
        except KeyError:
            # Match the migration path: executor credentials can live in the
            # encrypted local store while SOURCE/TARGET credentials use env.
            if provider_name not in (None, "env"):
                raise RuntimeError("Oracle permission executor credential is unavailable") from None
            local_store = create_secret_provider(cfg.raw, "local_encrypted_file")
            try:
                secret = local_store.resolve(secret_name) if local_store is not None else None
            except Exception:
                raise RuntimeError("Oracle permission executor credential is unavailable") from None
        except Exception:
            raise RuntimeError("Oracle permission executor credential is unavailable") from None
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Oracle permission executor credential is unavailable")
    return {**target_config, "username": str(executor["username"]), "password": secret}


def _permission_graph_check(
    cfg: E2EConfig, source_conn: Any, target_conn: Any,
    target_config: dict[str, Any], driver: Any,
    source_owner: str, target_owner: str,
) -> dict[str, Any]:
    policy = cfg.raw.get("oracle_permission_migration", {})
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return {"name": "permissions_and_grants", "status": "PASS", "message": "Permission migration is disabled"}
    executor_conn = None
    try:
        executor_config = _permission_executor_connection_config(cfg, target_config)
        executor_conn = connect(executor_config, driver)
        target_identity = oracle_permissions._identity(target_conn)
        executor_identity = oracle_permissions._identity(executor_conn)
        if executor_identity["user"].casefold() != str(executor_config["username"]).casefold():
            raise RuntimeError("permission executor session identity mismatch")
        if any(
            executor_identity[key].casefold() != target_identity[key].casefold()
            for key in ("db_unique_name", "container_name", "service_name")
        ):
            raise RuntimeError("permission executor is connected to a different TARGET database or PDB")
        source_graph = oracle_permissions.discover(source_conn, source_owner, policy)
        target_graph = oracle_permissions.discover(
            executor_conn, target_owner, policy, catalog_scope="dba"
        )
        comparison = oracle_permissions.compare_inventories(
            source_graph, target_graph,
            source_owner=source_owner, target_owner=target_owner,
        )
        return {
            "name": "permissions_and_grants",
            "status": "PASS" if comparison["match"] else "FAIL",
            "comparison": comparison,
            "visibility": "complete configured catalog views verified",
            "operation": "read_only",
        }
    except Exception as exc:  # noqa: BLE001 - never expose driver/secret details
        return {
            "name": "permissions_and_grants",
            "status": "FAIL",
            "message": f"Permission graph could not be completely verified ({type(exc).__name__})",
            "operation": "read_only",
        }
    finally:
        if executor_conn is not None:
            with suppress(Exception):
                executor_conn.close()


def _read_table_names(connection: Any, owner: str) -> set[str]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT t.table_name FROM all_tables t "
            "WHERE t.owner = :1 AND t.nested = 'NO' AND NOT EXISTS ("
            "SELECT 1 FROM all_mviews m WHERE m.owner = t.owner "
            "AND m.mview_name = t.table_name) ORDER BY t.table_name",
            (owner.upper(),),
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}
    finally:
        cursor.close()


def _count_rows(connection: Any, table_name: str, owner: str) -> int:
    cursor = connection.cursor()
    try:
        qualified_name = (
            f"{quote_identifier(owner)}.{quote_identifier(table_name.upper())}"
        )
        cursor.execute(f"SELECT COUNT(*) FROM {qualified_name}")
        row = cursor.fetchone()
        return int(row[0])
    finally:
        cursor.close()


def _canonical_value(value: Any) -> Any:
    """Return a typed, equality-safe value for exact cross-connection row checks."""
    if value is None or isinstance(value, (str, int, bool, Decimal, date, datetime, time, timedelta)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return ("float", "nan")
        if math.isinf(value):
            return ("float", "inf" if value > 0 else "-inf")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if hasattr(value, "read"):
        return _canonical_value(value.read())
    if isinstance(value, dict):
        if set(value) == {"__oracle_collection__", "kind", "items"}:
            items = [_canonical_value(item) for item in value["items"]]
            if value["kind"] == "nested_table":
                items.sort(key=repr)
            return (
                ("__oracle_collection__", str(value["__oracle_collection__"]).casefold()),
                ("kind", value["kind"]), ("items", tuple(items)),
            )
        return tuple(sorted((str(key), _canonical_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_canonical_value(item) for item in value), key=repr))
    value_type = f"{type(value).__module__}.{type(value).__qualname__}"
    raise TypeError(f"exact row comparison does not support returned value type {value_type}")


def _read_row_multiset(
    connection: Any, table_name: str, column_names: list[str], owner: str,
    object_types: dict[str, dict[str, str]] | None = None,
    collection_kinds: dict[str, str] | None = None,
    driver: Any | None = None,
) -> Counter[tuple[Any, ...]]:
    """Read exact table values as a NULL-safe multiset, including duplicate rows."""
    if not column_names:
        raise ValueError("table metadata contains no columns")
    columns = ", ".join(quote_identifier(name) for name in column_names)
    table = f"{quote_identifier(owner)}.{quote_identifier(table_name.upper())}"
    cursor = connection.cursor()
    rows: Counter[tuple[Any, ...]] = Counter()
    object_types = object_types or {}
    type_handles = {
        column: connection.gettype(f"{metadata['owner']}.{metadata['name']}")
        for column, metadata in object_types.items()
    }
    try:
        cursor.arraysize = 1000
        if driver is not None and hasattr(driver, "DB_TYPE_NUMBER"):
            cursor.outputtypehandler = decimal_output_type_handler(driver)
        cursor.execute(f"SELECT {columns} FROM {table}")
        while True:
            batch = cursor.fetchmany(1000)
            if not batch:
                break
            for values in batch:
                canonical = []
                for name, value in zip(column_names, values):
                    if name in type_handles:
                        value = serialize_object(value, type_handles[name], collection_kinds)
                    canonical.append(_canonical_value(value))
                rows[tuple(canonical)] += 1
        return rows
    finally:
        cursor.close()


_SUPPORTED_OWNER_OBJECT_TYPES = frozenset(
    {
        "TABLE", "INDEX", "VIEW", "MATERIALIZED VIEW", "SEQUENCE", "TRIGGER",
        "FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY", "SYNONYM", "TYPE",
    }
)
_INTERNAL_OWNER_OBJECT_TYPES = frozenset(
    {
        "TABLE PARTITION", "TABLE SUBPARTITION", "INDEX PARTITION",
        "INDEX SUBPARTITION", "LOB", "LOB PARTITION", "LOB SUBPARTITION",
    }
)


def _read_owner_object_inventory(
    connection: Any, owner: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Inventory supported and unsupported non-generated owner objects."""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT object_name, object_type, status FROM all_objects "
            "WHERE generated = 'N' AND owner = :1 "
            "ORDER BY object_type, object_name",
            (owner.upper(),),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
    supported: list[dict[str, str]] = []
    unsupported: list[dict[str, str]] = []
    for name, kind, status in rows:
        item = {
            "name": str(name),
            "type": str(kind).upper(),
            "status": str(status or "UNKNOWN").upper(),
        }
        if item["type"] in _INTERNAL_OWNER_OBJECT_TYPES:
            continue
        if item["type"] in _SUPPORTED_OWNER_OBJECT_TYPES:
            supported.append(item)
        else:
            unsupported.append(item)
    return supported, unsupported


def _read_trigger_inventory(
    connection: Any, owner: str
) -> dict[str, tuple[str, str, str, str]]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT trigger_name, status, trigger_type, triggering_event, table_name "
            "FROM all_triggers WHERE owner = :1 ORDER BY trigger_name",
            (owner.upper(),),
        )
        return {
            str(row[0]).upper(): tuple(str(value or "").upper() for value in row[1:])
            for row in cursor.fetchall()
        }
    finally:
        cursor.close()


def _read_type_definitions(connection: Any, owner: str) -> tuple[dict[str, Any], list[str]]:
    """Compare safe schema-level type structure, normalizing the mapped owner."""
    cursor = connection.cursor()
    unsupported: list[str] = []
    try:
        cursor.execute(
            "SELECT type_name, typecode, attributes, methods, predefined, incomplete, "
            "supertype_name FROM user_types ORDER BY type_name"
        )
        types = cursor.fetchall()
        cursor.execute("SELECT object_name, status FROM user_objects WHERE object_type = 'TYPE'")
        type_statuses = {str(name).upper(): str(status).upper() for name, status in cursor.fetchall()}
        cursor.execute(
            "SELECT type_name, attr_no, attr_name, attr_type_mod, attr_type_owner, "
            "attr_type_name, length, precision, scale, character_set_name "
            "FROM user_type_attrs ORDER BY type_name, attr_no"
        )
        attributes = cursor.fetchall()
        cursor.execute(
            "SELECT type_name, coll_type, upper_bound, elem_type_owner, elem_type_name, "
            "elem_type_mod FROM user_coll_types ORDER BY type_name"
        )
        collections = cursor.fetchall()
    finally:
        cursor.close()
    attrs_by_type: dict[str, list[tuple[Any, ...]]] = {}
    for row in attributes:
        attrs_by_type.setdefault(str(row[0]).upper(), []).append(tuple(row[1:]))
    collections_by_type = {str(row[0]).upper(): tuple(row[1:]) for row in collections}
    definitions: dict[str, Any] = {}
    for name, typecode, _count, methods, predefined, incomplete, supertype in types:
        key = str(name).upper()
        kind = str(typecode or "").upper()
        if (kind not in {"OBJECT", "COLLECTION"} or int(methods or 0) != 0
                or str(predefined).upper() == "YES" or str(incomplete).upper() == "YES"
                or supertype or type_statuses.get(key) != "VALID"):
            unsupported.append(f"{key}: unsupported type kind, method, or inheritance")
        normalized_attrs = []
        for attr_no, attr_name, attr_mod, attr_owner, attr_type, length, precision, scale, character_set in attrs_by_type.get(key, []):
            attr_owner = str(attr_owner or "").upper()
            if attr_owner not in {"", "SYS", "SYSTEM", owner.upper()}:
                unsupported.append(f"{key}.{attr_name}: cross-owner type dependency")
            if str(attr_type or "").upper() in {"BLOB", "CLOB", "NCLOB", "BFILE"}:
                unsupported.append(f"{key}.{attr_name}: LOB attribute")
            normalized_attrs.append((
                int(attr_no), str(attr_name).upper(), str(attr_mod or "").upper(),
                "$OWNER" if attr_owner == owner.upper() else attr_owner,
                str(attr_type or "").upper(), length, precision, scale,
                str(character_set or "").upper(),
            ))
        coll = collections_by_type.get(key)
        if kind == "COLLECTION" and coll is None:
            unsupported.append(f"{key}: collection metadata unavailable")
        if coll is not None:
            coll_kind, bound, elem_owner, elem_type, elem_mod = coll
            elem_owner = str(elem_owner or "").upper()
            if elem_owner not in {"", "SYS", "SYSTEM", owner.upper()}:
                unsupported.append(f"{key}: cross-owner collection element")
            definitions[key] = {
                "typecode": kind, "collection": (
                    str(coll_kind).upper(), bound,
                    "$OWNER" if elem_owner == owner.upper() else elem_owner,
                    str(elem_type or "").upper(), str(elem_mod or "").upper(),
                ),
                "attributes": normalized_attrs,
            }
        else:
            definitions[key] = {"typecode": kind, "attributes": normalized_attrs}
    return definitions, unsupported


def _collection_kinds(connection: Any) -> dict[str, str]:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT type_name, coll_type FROM user_coll_types")
        return {
            str(name).casefold(): (
                "varray" if "VARYING ARRAY" in " ".join(str(kind).upper().split())
                else "nested_table"
            )
            for name, kind in cursor.fetchall()
        }
    finally:
        cursor.close()


def _custom_column_types(columns: list[dict[str, Any]], owner: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    result: dict[str, dict[str, str]] = {}
    unsupported: list[str] = []
    for column in columns:
        type_owner = str(column.get("type_owner") or "").upper()
        if not type_owner or type_owner in {"SYS", "SYSTEM"}:
            continue
        if type_owner != owner.upper():
            unsupported.append(f"{column['name']}: cross-owner custom TYPE {type_owner}.{column['type']}")
            continue
        result[column["name"]] = {"owner": type_owner, "name": column["type"]}
    return result, unsupported


def _unsafe_table_storage(connection: Any) -> list[str]:
    cursor = connection.cursor()
    try:
        # USER_OBJECT_TABLES exposes TABLE_NAME (and TABLE_TYPE), not TYPE_NAME.
        # The validator only needs to know whether any object tables exist, so
        # select the smallest owner-scoped catalog projection available.
        cursor.execute("SELECT table_name FROM user_object_tables ORDER BY table_name")
        object_tables = [f"{row[0]}: object table" for row in cursor.fetchall()]
        cursor.execute("SELECT table_name, parent_table_name FROM user_nested_tables ORDER BY table_name")
        nested_storage = [f"{row[0]} for {row[1]}: nested-table storage" for row in cursor.fetchall()]
        return object_tables + nested_storage
    finally:
        cursor.close()


def _ddl_signature(connection: Any, kind: str, name: str, owner: str) -> str:
    metadata_kind = {
        "PACKAGE": "PACKAGE_SPEC",
        "PACKAGE BODY": "PACKAGE_BODY",
        "MATERIALIZED VIEW": "MATERIALIZED_VIEW",
    }.get(kind, kind)
    ddl = _get_ddl(connection, metadata_kind, name, owner)
    return " ".join(ddl.split())


def _object_definition_inventory(
    source: Any, target: Any, source_owner: str, target_owner: str,
    source_objects: list[dict[str, str]], target_objects: list[dict[str, str]],
) -> dict[str, Any]:
    source_keys = {
        (item["type"], item["name"].upper()): item for item in source_objects
        if item["type"] not in {"TABLE", "INDEX", "TYPE", "SEQUENCE"}
    }
    target_keys = {
        (item["type"], item["name"].upper()): item for item in target_objects
        if item["type"] not in {"TABLE", "INDEX", "TYPE", "SEQUENCE"}
    }
    compared: list[tuple[str, str]] = []
    mismatched: list[tuple[str, str]] = []
    unavailable: list[tuple[str, str, str]] = []
    for key, item in source_keys.items():
        if key not in target_keys:
            mismatched.append(key)
            continue
        try:
            source_ddl = _ddl_signature(source, key[0], item["name"], source_owner)
            source_ddl = _remap_owner_qualifiers(source_ddl, source_owner, target_owner)
            target_ddl = _ddl_signature(target, key[0], target_keys[key]["name"], target_owner)
            (compared if source_ddl == target_ddl else mismatched).append(key)
        except Exception as exc:
            unavailable.append((key[0], key[1], type(exc).__name__))
    return {
        "source_objects_compared": len(compared),
        "definition_mismatch": [list(item) for item in mismatched],
        "unavailable": [list(item) for item in unavailable],
        "target_only": [list(item) for item in sorted(set(target_keys) - set(source_keys))],
        "match": not mismatched and not unavailable,
    }


def _comment_signatures(connection: Any, owner: str) -> dict[tuple[str, str, str | None], str]:
    comments = discover_comments(connection, owner)
    return {
        (item.object_type.upper(), item.object_name.upper(),
         item.column_name.upper() if item.column_name else None): item.comment
        for item in comments
    }


def _partition_inventory(connection: Any, table_names: set[str]) -> dict[str, Any]:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT table_name FROM user_part_tables ORDER BY table_name")
        partitioned = {str(row[0]).upper() for row in cursor.fetchall()}
    finally:
        cursor.close()
    result: dict[str, Any] = {}
    selected = partitioned if not table_names else partitioned & table_names
    for table in sorted(selected):
        metadata = discover_table_partitioning(connection, table)
        if metadata is None:
            raise RuntimeError(f"Partition metadata is unavailable for {table}")
        partition_rows = {}
        with connection.cursor() as cur:
            for partition in metadata["partitions"]:
                cur.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)} "
                    f"PARTITION ({quote_identifier(partition['name'])})"
                )
                partition_rows[partition["name"].upper()] = int(cur.fetchone()[0])
        result[table] = {"definition": metadata, "row_counts": partition_rows}
    return result


def _sequence_inventory(connection: Any) -> dict[str, dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute("SELECT * FROM user_sequences WHERE 1 = 0")
        available = {str(item[0]).upper() for item in (getattr(cur, "description", None) or [])}
        optional = [
            name for name in (
                "ORDER_FLAG", "SCALE_FLAG", "EXTEND_FLAG", "SESSION_FLAG",
                "KEEP_VALUE", "SHARDED_FLAG",
            ) if name in available
        ]
        names = [
            "SEQUENCE_NAME", "MIN_VALUE", "MAX_VALUE", "INCREMENT_BY",
            "CYCLE_FLAG", "CACHE_SIZE", "LAST_NUMBER", *optional,
        ]
        cur.execute("SELECT " + ", ".join(names) + " FROM user_sequences ORDER BY sequence_name")
        rows = cur.fetchall()
        if any(str(row[0]).startswith("ISEQ$$_") for row in rows):
            cur.execute(
                "SELECT o.object_id FROM user_objects o WHERE o.object_type = 'TABLE' "
                "AND EXISTS (SELECT 1 FROM user_tab_identity_cols i "
                "WHERE i.table_name = o.object_name)"
            )
            identity_names = {f"ISEQ$$_{int(row[0])}" for row in cur.fetchall()}
        else:
            identity_names = set()
    result = {}
    for row in rows:
        name = str(row[0]).upper()
        if name in identity_names:
            continue
        values = dict(zip(names[1:], row[1:]))
        result[name] = values
    return result


def _materialized_view_value_check(
    source: Any, target: Any, source_owner: str, target_owner: str,
    source_objects: list[dict[str, str]], target_objects: list[dict[str, str]],
    driver: Any | None = None,
) -> dict[str, Any]:
    source_names = {item["name"].upper() for item in source_objects if item["type"] == "MATERIALIZED VIEW"}
    target_names = {item["name"].upper() for item in target_objects if item["type"] == "MATERIALIZED VIEW"}
    mismatched, unavailable = [], []
    for name in sorted(source_names):
        if name not in target_names:
            mismatched.append(name)
            continue
        try:
            left_schema = _read_table_structure(source, name)
            right_schema = _read_table_structure(target, name)
            left_types, left_errors = _custom_column_types(left_schema["columns"], source_owner)
            right_types, right_errors = _custom_column_types(right_schema["columns"], target_owner)
            if left_errors or right_errors:
                raise RuntimeError("cross-owner materialized-view custom TYPE dependency")
            left_rows = _read_row_multiset(
                source, name, [c["name"] for c in left_schema["columns"]],
                source_owner, left_types, _collection_kinds(source),
                driver,
            )
            right_rows = _read_row_multiset(
                target, name, [c["name"] for c in right_schema["columns"]],
                target_owner, right_types, _collection_kinds(target),
                driver,
            )
            left_columns = [
                {**c, "type_owner": "$OWNER" if c["type_owner"] == source_owner.upper() else c["type_owner"]}
                for c in left_schema["columns"]
            ]
            right_columns = [
                {**c, "type_owner": "$OWNER" if c["type_owner"] == target_owner.upper() else c["type_owner"]}
                for c in right_schema["columns"]
            ]
            if left_columns != right_columns or left_rows != right_rows:
                mismatched.append(name)
        except Exception as exc:
            unavailable.append((name, type(exc).__name__))
    return {
        "match": not mismatched and not unavailable,
        "mismatch": mismatched,
        "unavailable": [list(item) for item in unavailable],
        "target_only": sorted(target_names - source_names),
    }


def _constraint_state(row: tuple[Any, ...]) -> dict[str, str]:
    return {
        "status": str(row[2] or "").upper(),
        "validated": str(row[3] or "").upper(),
        "deferrable": str(row[6] or "").upper(),
        "deferred": str(row[7] or "").upper(),
    }


def _read_table_structure(connection: Any, table_name: str) -> dict[str, Any]:
    """Read table columns, constraints, and supported secondary-index definitions."""
    normalized = table_name.upper()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT column_name, data_type, data_length, char_length, char_used, "
            "data_precision, data_scale, nullable, data_type_owner FROM user_tab_columns "
            "WHERE table_name = :1 ORDER BY column_id",
            (normalized,),
        )
        columns = [
            {
                "name": str(row[0]),
                "type": str(row[1]).upper(),
                "data_length": row[2],
                "char_length": row[3],
                "char_semantics": row[4],
                "precision": row[5],
                "scale": row[6],
                "nullable": str(row[7]).upper() == "Y",
                "type_owner": str(row[8] or "").upper() if len(row) > 8 else "",
            }
            for row in cursor.fetchall()
        ]
        cursor.execute(
            "SELECT c.constraint_name, c.constraint_type, c.status, c.validated, "
            "c.r_constraint_name, c.delete_rule, c.deferrable, c.deferred, "
            "cc.column_name, cc.position "
            "FROM user_constraints c LEFT JOIN user_cons_columns cc "
            "ON c.constraint_name = cc.constraint_name AND c.table_name = cc.table_name "
            "WHERE c.table_name = :1 AND c.constraint_type IN ('P','U','R','C') "
            "ORDER BY c.constraint_type, c.constraint_name, cc.position",
            (normalized,),
        )
        constraint_rows = cursor.fetchall()
        cursor.execute(
            "SELECT constraint_name, search_condition FROM user_constraints "
            "WHERE table_name = :1 AND constraint_type = 'C'",
            (normalized,),
        )
        check_conditions = {
            str(name): str(condition.read() if hasattr(condition, "read") else condition or "").strip()
            for name, condition in cursor.fetchall()
        }
        cursor.execute(
            "SELECT i.index_name, i.uniqueness, i.index_type, i.status, i.partitioned, "
            "ic.column_name, ic.column_position, ic.descend, ie.column_expression "
            "FROM user_indexes i JOIN user_ind_columns ic "
            "ON ic.index_name = i.index_name AND ic.table_name = i.table_name "
            "LEFT JOIN user_ind_expressions ie ON ie.index_name = i.index_name "
            "AND ie.table_name = i.table_name AND ie.column_position = ic.column_position "
            "WHERE i.table_name = :1 AND NOT EXISTS ("
            "SELECT 1 FROM user_lobs l WHERE l.table_name = i.table_name "
            "AND l.index_name = i.index_name) AND NOT EXISTS ("
            "SELECT 1 FROM user_constraints c WHERE c.table_name = i.table_name "
            "AND c.index_name = i.index_name AND c.constraint_type IN ('P','U')) "
            "ORDER BY i.index_name, ic.column_position",
            (normalized,),
        )
        index_rows = cursor.fetchall()
        cursor.execute(
            "SELECT column_name, generation_type, identity_options "
            "FROM user_tab_identity_cols WHERE table_name = :1 ORDER BY column_name",
            (normalized,),
        )
        identity_rows = cursor.fetchall()
    finally:
        cursor.close()

    grouped_constraints: dict[str, list[tuple[Any, ...]]] = {}
    for row in constraint_rows:
        grouped_constraints.setdefault(str(row[0]), []).append(row)

    primary_key: list[str] = []
    primary_key_state: dict[str, str] | None = None
    unique_constraints: list[dict[str, Any]] = []
    check_constraints: list[dict[str, Any]] = []
    foreign_keys: list[dict[str, Any]] = []
    for constraint_name, rows in grouped_constraints.items():
        first = rows[0]
        columns_in_constraint = [
            str(row[8]) for row in rows if row[8] is not None
        ]
        state = _constraint_state(first)
        kind = str(first[1]).upper()
        if kind == "P":
            primary_key = columns_in_constraint
            primary_key_state = state
        elif kind == "U":
            unique_constraints.append({"columns": columns_in_constraint, **state})
        elif kind == "C":
            check_constraints.append(
                {
                    "columns": columns_in_constraint,
                    "expression": check_conditions.get(constraint_name, ""),
                    **state,
                }
            )
        elif kind == "R":
            ref_cursor = connection.cursor()
            try:
                ref_cursor.execute(
                    "SELECT c.table_name, cc.column_name FROM user_constraints c "
                    "JOIN user_cons_columns cc ON c.constraint_name = cc.constraint_name "
                    "WHERE c.constraint_name = :1 AND c.constraint_type IN ('P','U') "
                    "ORDER BY cc.position",
                    (first[4],),
                )
                referenced = ref_cursor.fetchall()
            finally:
                ref_cursor.close()
            foreign_keys.append(
                {
                    "table": normalized,
                    "columns": columns_in_constraint,
                    "referenced_table": str(referenced[0][0]) if referenced else None,
                    "referenced_columns": [str(row[1]) for row in referenced],
                    "delete_rule": str(first[5] or "NO ACTION").upper(),
                    **state,
                }
            )

    indexes: list[dict[str, Any]] = []
    unsupported_indexes: list[dict[str, str]] = []
    normalized_indexes = normalize_index_rows(
        index_rows, {column["name"]: column["type"] for column in columns}
    )
    for index_name, definition in normalized_indexes.items():
        if not definition["supported"]:
            unsupported_indexes.append(
                {
                    "name": index_name,
                    "reason": (
                        f"type={definition['catalog_index_type'] or 'UNKNOWN'}, "
                        f"status={definition['status'] or 'UNKNOWN'}, "
                        f"partitioned={definition['partitioned'] or 'UNKNOWN'}, "
                        f"functional={definition['functional']}, "
                        f"reason={definition['reason']}"
                    ),
                }
            )
            continue
        indexes.append(
            {
                "name": index_name,
                "unique": definition["unique"],
                "columns": [
                    {
                        "name": str(column_name),
                        "position": position,
                        "descending": column_name in definition["descending_columns"],
                    }
                    for position, column_name in enumerate(definition["columns"], start=1)
                ],
            }
        )

    return {
        "columns": columns,
        "primary_key": primary_key,
        "primary_key_state": primary_key_state,
        "unique_constraints": sorted(unique_constraints, key=repr),
        "check_constraints": sorted(check_constraints, key=repr),
        "foreign_keys": sorted(foreign_keys, key=repr),
        "indexes": sorted(indexes, key=repr),
        "unsupported_indexes": unsupported_indexes,
        "identity_columns": {
            str(name).upper(): {
                "generation_type": str(generation or "").upper(),
                "options": str(options or "").strip(),
            }
            for name, generation, options in identity_rows
        },
    }


def _identity_sequence_state(connection: Any, table_name: str, identity_columns: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if not identity_columns:
        return state
    with connection.cursor() as cur:
        cur.execute(
            "SELECT object_id FROM user_objects WHERE object_type = 'TABLE' AND object_name = :1",
            (table_name.upper(),),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("identity table object id is unavailable")
        sequence_name = f"ISEQ$$_{int(row[0])}"
        for column in sorted(identity_columns):
            cur.execute("SELECT last_number FROM user_sequences WHERE sequence_name = :1", (sequence_name,))
            seq_row = cur.fetchone()
            if not seq_row or seq_row[0] is None:
                raise RuntimeError("identity sequence state is unavailable")
            cur.execute(
                f"SELECT MAX({quote_identifier(column)}) FROM {quote_identifier(table_name.upper())}"
            )
            maximum_row = cur.fetchone()
            maximum = maximum_row[0] if maximum_row else None
            last_number = seq_row[0]
            state[column] = {
                "max_value": str(maximum) if maximum is not None else None,
                "last_number": str(last_number),
                "safe_next_value": maximum is None or last_number > maximum,
            }
    return state


def run_validation(cfg: E2EConfig, *, driver: Any | None = None) -> dict[str, Any]:
    """Compare owner-discovered objects, table definitions, and exact row content."""
    owners, services = validated_oracle_context(cfg)
    _fixture_names, expected_tables = fixture_names_and_expected_tables(
        cfg, allow_source_discovery=True
    )
    source_allowlist = configured_source_table_allowlist(cfg, expected_tables)
    expected_relations = expected_foreign_key_relations(cfg, expected_tables)
    endpoint_configs = {
        side: connection_config(
            cfg, side, owners[side], resolver_for_side(cfg.raw, side)
        )
        for side in ("source", "target")
    }
    if driver is None:
        import oracledb as driver

    source_conn = target_conn = None
    report: dict[str, Any] = {
        "mode": "acceptance_validation",
        "engine": "oracle",
        "timestamp": datetime.now(UTC).isoformat(),
        "service_name": services["source"],
        "source_service_name": services["source"],
        "target_service_name": services["target"],
        "source_owner": owners["source"],
        "target_owner": owners["target"],
        "migration_status": "externally_executed",
        "checks": [],
        "result": "FAIL",
    }
    try:
        source_conn = connect(endpoint_configs["source"], driver)
        target_conn = connect(endpoint_configs["target"], driver)
        actual_tables: dict[str, set[str]] = {}
        catalog_tables: dict[str, set[str]] = {}
        configured_expectations = {name.upper() for name in expected_tables}
        filter_set = (
            {name.upper() for name in source_allowlist}
            if source_allowlist is not None else None
        )
        inventories: dict[str, list[dict[str, str]]] = {}
        inventory_readable: set[str] = set()
        unsupported_objects: list[dict[str, str]] = []
        for side, connection in (("source", source_conn), ("target", target_conn)):
            try:
                inventories[side], unsupported = _read_owner_object_inventory(
                    connection, owners[side]
                )
                inventory_readable.add(side)
                if side == "source":
                    unsupported_objects = unsupported
                report["checks"].append(
                    {
                        "name": f"{side}_object_inventory",
                        "status": "PASS",
                        "objects": inventories[side],
                        "unsupported": unsupported,
                    }
                )
            except Exception:  # noqa: BLE001 - avoid raw driver details in report
                inventories[side] = []
                unsupported = []
                report["checks"].append(
                    {
                        "name": f"{side}_object_inventory",
                        "status": "FAIL",
                        "message": "Unable to read Oracle owner object inventory",
                    }
                )
        if {"source", "target"}.issubset(inventory_readable):
            source_supported = {
                (item["type"], item["name"].upper()): item
                for item in inventories["source"]
                if item["type"] not in {"TABLE", "INDEX"}
            }
            target_supported = {
                (item["type"], item["name"].upper()): item
                for item in inventories["target"]
                if item["type"] not in {"TABLE", "INDEX"}
            }
            missing_objects = sorted(set(source_supported) - set(target_supported))
            invalid_target_objects = sorted(
                key for key in set(source_supported) & set(target_supported)
                if target_supported[key]["status"] != "VALID"
            )
            unsupported_target_objects = [
                item for item in inventories["target"]
                if item["type"] not in {"TABLE", "INDEX"}
                and item["type"] not in _SUPPORTED_OWNER_OBJECT_TYPES
            ]
            unsupported_target_objects.sort(key=lambda item: (item["type"], item["name"]))
            report["checks"].append(
                {
                    "name": "supported_object_presence",
                    "status": "PASS" if (
                        not missing_objects and not invalid_target_objects
                        and not unsupported_target_objects
                    ) else "FAIL",
                    "missing": [
                        {"type": kind, "name": name} for kind, name in missing_objects
                    ],
                    "invalid_target": [
                        {"type": kind, "name": name, "status": target_supported[(kind, name)]["status"]}
                        for kind, name in invalid_target_objects
                    ],
                    "unsupported_target": unsupported_target_objects,
                    "target_only": [
                        {"type": kind, "name": name}
                        for kind, name in sorted(set(target_supported) - set(source_supported))
                    ],
                }
            )
        else:
            report["checks"].append(
                {"name": "supported_object_presence", "status": "FAIL", "message": "Owner object inventory unavailable"}
            )
        report["checks"].append(
            {
                "name": "unsupported_source_objects",
                "status": "PASS" if "source" in inventory_readable and not unsupported_objects else "FAIL",
                "objects": unsupported_objects,
            }
        )

        type_definitions: dict[str, dict[str, Any]] = {}
        type_unsupported: dict[str, list[str]] = {}
        for side, connection in (("source", source_conn), ("target", target_conn)):
            try:
                type_definitions[side], type_unsupported[side] = _read_type_definitions(
                    connection, owners[side]
                )
            except Exception:
                type_definitions[side], type_unsupported[side] = {}, ["type catalog unavailable"]
        source_type_names = set(type_definitions.get("source", {}))
        target_type_names = set(type_definitions.get("target", {}))
        missing_type_definitions = {
            name for name, definition in type_definitions.get("source", {}).items()
            if type_definitions.get("target", {}).get(name) != definition
        }
        target_incompatible_source_types = [
            item for item in type_unsupported.get("target", [])
            if item.split(":", 1)[0].upper() in source_type_names
        ]
        type_match = (
            not missing_type_definitions and not type_unsupported.get("source")
            and not target_incompatible_source_types
        )
        report["checks"].append({
            "name": "type_definitions",
            "status": "PASS" if type_match else "FAIL",
            "source": type_definitions.get("source", {}),
            "target": type_definitions.get("target", {}),
            "missing_or_incompatible_target": sorted(missing_type_definitions),
            "target_only": sorted(target_type_names - source_type_names),
            "unsupported_source": type_unsupported.get("source", []),
            "unsupported_target_source_dependencies": target_incompatible_source_types,
        })

        if "source" in inventory_readable and "target" in inventory_readable:
            definitions = _object_definition_inventory(
                source_conn, target_conn, owners["source"], owners["target"],
                inventories["source"], inventories["target"],
            )
            report["checks"].append({
                "name": "object_definition_parity",
                "status": "PASS" if definitions["match"] else "FAIL",
                **definitions,
            })
            try:
                source_comments = _comment_signatures(source_conn, owners["source"])
                target_comments = _comment_signatures(target_conn, owners["target"])
                missing_comments = {
                    key for key, value in source_comments.items()
                    if target_comments.get(key) != value
                }
                report["checks"].append({
                    "name": "comment_parity",
                    "status": "PASS" if not missing_comments else "FAIL",
                    "missing_or_different": [list(key) for key in sorted(missing_comments, key=repr)],
                    "target_only": [list(key) for key in sorted(set(target_comments) - set(source_comments), key=repr)],
                })
            except Exception as exc:
                report["checks"].append({
                    "name": "comment_parity", "status": "FAIL",
                    "message": f"Comment comparison unavailable ({type(exc).__name__})",
                })
            try:
                source_partitions = _partition_inventory(source_conn, catalog_tables.get("source", set()))
                target_partitions = _partition_inventory(target_conn, catalog_tables.get("target", set()))
                report["checks"].append({
                    "name": "partition_definition_and_row_parity",
                    "status": "PASS" if source_partitions == target_partitions else "FAIL",
                    "source": source_partitions,
                    "target": target_partitions,
                })
            except Exception as exc:
                report["checks"].append({
                    "name": "partition_definition_and_row_parity", "status": "FAIL",
                    "message": f"Partition comparison unavailable ({type(exc).__name__})",
                })
            try:
                source_sequences = _sequence_inventory(source_conn)
                target_sequences = _sequence_inventory(target_conn)
                sequence_fields = (
                    "MIN_VALUE", "MAX_VALUE", "INCREMENT_BY", "CYCLE_FLAG", "CACHE_SIZE",
                    "ORDER_FLAG", "SCALE_FLAG", "EXTEND_FLAG", "SESSION_FLAG", "KEEP_VALUE", "SHARDED_FLAG",
                )
                missing_or_incompatible = []
                for name, source_state in source_sequences.items():
                    target_state = target_sequences.get(name)
                    if target_state is None:
                        missing_or_incompatible.append(name)
                        continue
                    if any(source_state.get(field) != target_state.get(field) for field in sequence_fields):
                        missing_or_incompatible.append(name)
                    elif int(target_state["LAST_NUMBER"]) < int(source_state["LAST_NUMBER"]):
                        missing_or_incompatible.append(name)
                report["checks"].append({
                    "name": "sequence_definition_and_state",
                    "status": "PASS" if not missing_or_incompatible else "FAIL",
                    "missing_incompatible_or_behind": sorted(set(missing_or_incompatible)),
                    "target_only": sorted(set(target_sequences) - set(source_sequences)),
                    "state_rule": "target LAST_NUMBER must not be below source LAST_NUMBER",
                })
            except Exception as exc:
                report["checks"].append({
                    "name": "sequence_definition_and_state", "status": "FAIL",
                    "message": f"Sequence comparison unavailable ({type(exc).__name__})",
                })
            mviews = _materialized_view_value_check(
                source_conn, target_conn, owners["source"], owners["target"],
                inventories["source"], inventories["target"],
                driver,
            )
            report["checks"].append({
                "name": "materialized_view_value_parity",
                "status": "PASS" if mviews["match"] else "FAIL",
                **mviews,
            })
        else:
            for check_name in (
                "object_definition_parity", "comment_parity",
                "partition_definition_and_row_parity", "sequence_definition_and_state",
                "materialized_view_value_parity",
            ):
                report["checks"].append({
                    "name": check_name, "status": "FAIL",
                    "message": "Owner inventory unavailable; parity was not verified",
                })

        report["checks"].append(_permission_graph_check(
            cfg, source_conn, target_conn, endpoint_configs["target"], driver,
            owners["source"], owners["target"],
        ))
        for side, connection in (("source", source_conn), ("target", target_conn)):
            try:
                unsafe_storage = _unsafe_table_storage(connection)
                report["checks"].append({
                    "name": f"{side}_object_table_and_nested_storage",
                    "status": "FAIL" if unsafe_storage else "PASS",
                    "unsupported": unsafe_storage,
                })
            except Exception:
                report["checks"].append({
                    "name": f"{side}_object_table_and_nested_storage",
                    "status": "FAIL",
                    "message": "Unable to verify object-table and nested-table storage metadata",
                })

        try:
            source_triggers = _read_trigger_inventory(source_conn, owners["source"])
            target_triggers = _read_trigger_inventory(target_conn, owners["target"])
            missing_triggers = sorted(set(source_triggers) - set(target_triggers))
            mismatched_triggers = sorted(
                name for name in set(source_triggers) & set(target_triggers)
                if source_triggers[name] != target_triggers[name]
            )
            report["checks"].append(
                {
                    "name": "trigger_inventory",
                    "status": "PASS" if not missing_triggers and not mismatched_triggers else "FAIL",
                    "source": sorted(source_triggers),
                    "target": sorted(target_triggers),
                    "missing": missing_triggers,
                    "state_or_definition_metadata_mismatch": mismatched_triggers,
                    "target_only": sorted(set(target_triggers) - set(source_triggers)),
                }
            )
        except Exception:  # noqa: BLE001 - avoid raw driver details in report
            report["checks"].append(
                {"name": "trigger_inventory", "status": "FAIL", "message": "Unable to inspect Oracle triggers"}
            )

        for side, connection in (("source", source_conn), ("target", target_conn)):
            try:
                catalog_tables[side] = _read_table_names(connection, owners[side])
                if side == "source":
                    expected_set = configured_expectations or filter_set or catalog_tables[side]
                    actual_tables[side] = (
                        catalog_tables[side] & filter_set
                        if filter_set is not None else catalog_tables[side]
                    )
                    excluded = sorted(catalog_tables[side] - actual_tables[side])
                    passed = actual_tables[side] == expected_set
                    if filter_set is not None:
                        passed = passed and filter_set.issubset(catalog_tables[side])
                    if not actual_tables[side]:
                        passed = False
                else:
                    expected_set = configured_expectations or filter_set or catalog_tables.get(
                        "source", set()
                    )
                    actual_tables[side] = catalog_tables[side]
                    excluded = []
                    # In source-discovered schema-wide mode, extra target-only
                    # tables are preserved by Oracle FULL and are expected.
                    passed = (
                        actual_tables[side] == expected_set
                        if configured_expectations
                        else expected_set.issubset(actual_tables[side])
                    )
                report["checks"].append(
                    {
                        "name": f"{side}_tables",
                        "status": "PASS" if passed else "FAIL",
                        "expected": sorted(expected_set),
                        "actual": sorted(actual_tables[side]),
                        "excluded": excluded,
                        "target_only": (
                            sorted(actual_tables[side] - expected_set)
                            if side == "target" and not configured_expectations else []
                        ),
                    }
                )
            except Exception:  # noqa: BLE001 - avoid raw driver details in report
                actual_tables[side] = set()
                catalog_tables[side] = set()
                report["checks"].append(
                    {
                        "name": f"{side}_tables",
                        "status": "FAIL",
                        "message": "Unable to read Oracle schema objects",
                    }
                )

        validation_tables = sorted(
            configured_expectations
            or filter_set
            or actual_tables.get("source", set())
        )
        for table_name in validation_tables:
            table_key = table_name.upper()
            if (
                table_key not in actual_tables["source"]
                or table_key not in actual_tables["target"]
            ):
                report["checks"].append(
                    {
                        "name": f"row_count:{table_name}",
                        "status": "FAIL",
                        "message": "Expected table is missing from source or target",
                    }
                )
                continue
            try:
                source_count = _count_rows(source_conn, table_name, owners["source"])
                target_count = _count_rows(target_conn, table_name, owners["target"])
                passed = source_count == target_count
                report["checks"].append(
                    {
                        "name": f"row_count:{table_name}",
                        "status": "PASS" if passed else "FAIL",
                        "expected_source_rows": source_count,
                        "source": source_count,
                        "target": target_count,
                    }
                )
            except Exception:  # noqa: BLE001 - avoid raw driver details in report
                report["checks"].append(
                    {
                        "name": f"row_count:{table_name}",
                        "status": "FAIL",
                        "message": "Unable to read configured table row counts",
                    }
                )
        for table_name in validation_tables:
            table_key = table_name.upper()
            source_schema = target_schema = None
            if (
                table_key not in actual_tables["source"]
                or table_key not in actual_tables["target"]
            ):
                report["checks"].append(
                    {"name": f"structure:{table_name}", "status": "FAIL", "message": "Expected table is missing"}
                )
                report["checks"].append(
                    {"name": f"foreign_keys:{table_name}", "status": "FAIL", "message": "Expected table is missing"}
                )
                continue
            try:
                source_schema = _read_table_structure(source_conn, table_name)
                target_schema = _read_table_structure(target_conn, table_name)
                source_custom_types, source_type_errors = _custom_column_types(
                    source_schema["columns"], owners["source"]
                )
                target_custom_types, target_type_errors = _custom_column_types(
                    target_schema["columns"], owners["target"]
                )
                source_type_columns = {
                    column["name"]: ("$OWNER" if column["type_owner"] == owners["source"].upper() else column["type_owner"], column["type"])
                    for column in source_schema["columns"] if column["type_owner"]
                }
                target_type_columns = {
                    column["name"]: ("$OWNER" if column["type_owner"] == owners["target"].upper() else column["type_owner"], column["type"])
                    for column in target_schema["columns"] if column["type_owner"]
                }
                type_columns_match = (
                    source_type_columns == target_type_columns
                    and not source_type_errors and not target_type_errors
                )
                report["checks"].append({
                    "name": f"custom_type_columns:{table_name}",
                    "status": "PASS" if type_columns_match else "FAIL",
                    "source": source_type_columns,
                    "target": target_type_columns,
                    "unsupported_source": source_type_errors,
                    "unsupported_target": target_type_errors,
                })
                source_structure = {
                    "columns": [
                        {**column, "type_owner": "$OWNER" if column["type_owner"] == owners["source"].upper() else column["type_owner"]}
                        for column in source_schema["columns"]
                    ],
                    "primary_key": source_schema["primary_key"],
                    "primary_key_state": source_schema["primary_key_state"],
                    "unique_constraints": source_schema["unique_constraints"],
                    "check_constraints": source_schema["check_constraints"],
                    "identity_columns": source_schema["identity_columns"],
                }
                target_structure = {
                    "columns": [
                        {**column, "type_owner": "$OWNER" if column["type_owner"] == owners["target"].upper() else column["type_owner"]}
                        for column in target_schema["columns"]
                    ],
                    "primary_key": target_schema["primary_key"],
                    "primary_key_state": target_schema["primary_key_state"],
                    "unique_constraints": target_schema["unique_constraints"],
                    "check_constraints": target_schema["check_constraints"],
                    "identity_columns": target_schema["identity_columns"],
                }
                report["checks"].append(
                    {
                        "name": f"structure:{table_name}",
                        "status": "PASS" if source_structure == target_structure else "FAIL",
                        "source": source_structure,
                        "target": target_structure,
                    }
                )
                source_identity_state = _identity_sequence_state(
                    source_conn, table_name, source_schema["identity_columns"]
                )
                target_identity_state = _identity_sequence_state(
                    target_conn, table_name, target_schema["identity_columns"]
                )
                identity_state_safe = all(
                    item["safe_next_value"]
                    for item in (*source_identity_state.values(), *target_identity_state.values())
                )
                report["checks"].append({
                    "name": f"identity_definition_and_state:{table_name}",
                    "status": "PASS" if (
                        source_schema["identity_columns"] == target_schema["identity_columns"]
                        and identity_state_safe
                    ) else "FAIL",
                    "source_identity_columns": source_schema["identity_columns"],
                    "target_identity_columns": target_schema["identity_columns"],
                    "source_sequence_state": source_identity_state,
                    "target_sequence_state": target_identity_state,
                })
                source_fks = source_schema["foreign_keys"]
                target_fks = target_schema["foreign_keys"]
                fks_valid = all(
                    foreign_key["status"] == "ENABLED"
                    and foreign_key["validated"] == "VALIDATED"
                    and foreign_key["deferrable"] == "NOT DEFERRABLE"
                    and foreign_key["deferred"] == "IMMEDIATE"
                    for foreign_key in source_fks + target_fks
                )
                expected_pairs = {
                    (relation["table"].upper(), relation["referenced_table"].upper())
                    for relation in expected_relations
                    if relation["table"].upper() == table_key
                }
                source_pairs = {
                    (foreign_key["table"], (foreign_key["referenced_table"] or "").upper())
                    for foreign_key in source_fks
                }
                target_pairs = {
                    (foreign_key["table"], (foreign_key["referenced_table"] or "").upper())
                    for foreign_key in target_fks
                }
                required_pairs_present = expected_pairs.issubset(source_pairs) and expected_pairs.issubset(target_pairs)
                report["checks"].append(
                    {
                        "name": f"foreign_keys:{table_name}",
                        "status": "PASS" if fks_valid and required_pairs_present and source_fks == target_fks else "FAIL",
                        "source": source_fks,
                        "target": target_fks,
                        "expected": [
                            relation for relation in expected_relations
                            if relation["table"].upper() == table_key
                        ],
                        "all_enabled_and_validated": fks_valid,
                        "required_relationships_present": required_pairs_present,
                    }
                )
                constraint_states = [
                    state
                    for state in (
                        [source_schema["primary_key_state"], target_schema["primary_key_state"]]
                        + source_schema["unique_constraints"]
                        + target_schema["unique_constraints"]
                        + source_schema["check_constraints"]
                        + target_schema["check_constraints"]
                        + source_fks
                        + target_fks
                    )
                    if state is not None
                ]
                constraints_supported = all(
                    state["status"] == "ENABLED"
                    and state["validated"] == "VALIDATED"
                    and state["deferrable"] == "NOT DEFERRABLE"
                    and state["deferred"] == "IMMEDIATE"
                    for state in constraint_states
                )
                report["checks"].append(
                    {
                        "name": f"constraints:{table_name}",
                        "status": "PASS" if constraints_supported else "FAIL",
                        "supported_states": constraints_supported,
                        "primary_key_present": bool(source_schema["primary_key"]),
                        "source_unique_constraints": len(source_schema["unique_constraints"]),
                        "target_unique_constraints": len(target_schema["unique_constraints"]),
                        "source_check_constraints": len(source_schema["check_constraints"]),
                        "target_check_constraints": len(target_schema["check_constraints"]),
                    }
                )

                def index_signature(index: dict[str, Any]) -> tuple[Any, ...]:
                    return (
                        index["unique"],
                        tuple(
                            (column["name"].upper(), column["descending"])
                            for column in index["columns"]
                        ),
                    )

                source_index_signatures = Counter(
                    index_signature(index) for index in source_schema["indexes"]
                )
                target_index_signatures = Counter(
                    index_signature(index) for index in target_schema["indexes"]
                )
                missing_indexes = source_index_signatures - target_index_signatures
                target_extra_indexes = target_index_signatures - source_index_signatures
                indexes_pass = (
                    not missing_indexes
                    and not source_schema["unsupported_indexes"]
                    and not target_schema["unsupported_indexes"]
                )
                report["checks"].append(
                    {
                        "name": f"indexes:{table_name}",
                        "status": "PASS" if indexes_pass else "FAIL",
                        "source": source_schema["indexes"],
                        "target": target_schema["indexes"],
                        "unsupported_source": source_schema["unsupported_indexes"],
                        "unsupported_target": target_schema["unsupported_indexes"],
                        "missing_definitions": [
                            {"definition": repr(signature), "count": count}
                            for signature, count in missing_indexes.items()
                        ],
                        "target_only_definitions": [
                            {"definition": repr(signature), "count": count}
                            for signature, count in target_extra_indexes.items()
                        ],
                    }
                )
            except Exception:  # noqa: BLE001 - don't expose raw Oracle diagnostics
                report["checks"].append(
                    {"name": f"structure:{table_name}", "status": "FAIL", "message": "Unable to inspect configured table structure"}
                )
                report["checks"].append(
                    {"name": f"foreign_keys:{table_name}", "status": "FAIL", "message": "Unable to inspect configured foreign keys"}
                )
                report["checks"].append(
                    {"name": f"constraints:{table_name}", "status": "FAIL", "message": "Unable to inspect configured constraints"}
                )
                report["checks"].append(
                    {"name": f"indexes:{table_name}", "status": "FAIL", "message": "Unable to inspect configured indexes"}
                )

            if (
                source_schema is not None
                and target_schema is not None
                and table_key in actual_tables["source"]
                and table_key in actual_tables["target"]
            ):
                try:
                    source_rows = _read_row_multiset(
                        source_conn,
                        table_name,
                        [column["name"] for column in source_schema["columns"]],
                        owners["source"],
                        source_custom_types,
                        _collection_kinds(source_conn),
                        driver,
                    )
                    target_rows = _read_row_multiset(
                        target_conn,
                        table_name,
                        [column["name"] for column in target_schema["columns"]],
                        owners["target"],
                        target_custom_types,
                        _collection_kinds(target_conn),
                        driver,
                    )
                    missing_rows = sum((source_rows - target_rows).values())
                    extra_rows = sum((target_rows - source_rows).values())
                    report["checks"].append(
                        {
                            "name": f"content_parity:{table_name}",
                            "status": "PASS" if not missing_rows and not extra_rows else "FAIL",
                            "missing_source_rows": missing_rows,
                            "extra_target_rows": extra_rows,
                            "comparison": "exact NULL-safe row multiset comparison",
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - never expose row values
                    report["checks"].append(
                        {
                            "name": f"content_parity:{table_name}",
                            "status": "FAIL",
                            "message": (
                                "Exact row comparison is unavailable for this table "
                                f"({type(exc).__name__})"
                            ),
                        }
                    )
            elif table_key in actual_tables["source"] and table_key in actual_tables["target"]:
                report["checks"].append(
                    {
                        "name": f"content_parity:{table_name}",
                        "status": "FAIL",
                        "message": "Table metadata is required before exact row comparison",
                    }
                )
        report["result"] = (
            "PASS"
            if report["checks"] and all(check["status"] == "PASS" for check in report["checks"])
            else "FAIL"
        )
        return report
    finally:
        for connection in (target_conn, source_conn):
            if connection is not None:
                with suppress(Exception):
                    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Oracle E2E validation")
    parser.add_argument("--config", help="Path to Oracle acceptance configuration")
    args = parser.parse_args()
    cfg = load_e2e_config(args.config)
    try:
        report = run_validation(cfg)
    except Exception:  # noqa: BLE001 - never print raw driver/provider messages
        print(
            "[VALIDATION] FAILED; detailed errors omitted to protect credentials and connection data.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    path = Path(
        write_acceptance_report(
            str(REPORTS_DIR / "validation_report.json"),
            report,
            preserve_existing=True,
        )
    )
    print(f"[VALIDATION] {report['result']}; report: {path}")
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
