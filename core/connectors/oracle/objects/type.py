"""Oracle user-defined type discovery and guarded creation.

Schema-level OBJECT, VARRAY, and NESTED TABLE values are transported through
the shared batched row path. TYPE BODY, method-bearing, inherited, external,
LOB-bearing, timezone-bearing object attributes, and object-table storage
cases are rejected during preflight when their values cannot be transported
losslessly by the configured driver path.
"""
from __future__ import annotations

import re
from typing import Any

from core.connectors.base import TypeDef
from core.connectors.oracle._models import validate_oracle_identifier
from core.connectors.oracle.objects._common import (
    _assert_target_type,
    _check_local_dependencies,
    _get_ddl,
    _remap_owner_qualifiers,
    _validate_single_create,
)

_SUPPORTED_ATTRIBUTE_TYPES = {
    "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW", "NUMBER", "FLOAT",
    "BINARY_FLOAT", "BINARY_DOUBLE", "DATE", "TIMESTAMP",
}
_ATTRIBUTE_TYPE_ALIASES = {
    "TIMESTAMP WITH TZ": "TIMESTAMP WITH TIME ZONE",
}
_TIMEZONE_ATTRIBUTE_TYPES = {
    "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE",
}


def discover_types(conn: Any, owner: str) -> list[TypeDef]:
    """Discover safe object and SQL collection types in dependency order."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.object_name, o.status, t.typecode, t.predefined, t.incomplete "
            "FROM user_objects o LEFT JOIN user_types t "
            "ON t.type_name = o.object_name "
            "WHERE o.object_type = 'TYPE' AND o.generated = 'N' "
            "ORDER BY o.object_name"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT object_name FROM user_objects "
            "WHERE object_type = 'TYPE BODY' AND generated = 'N'"
        )
        type_bodies = {str(row[0]).upper() for row in cur.fetchall()}
        cur.execute(
            "SELECT type_name FROM user_types WHERE supertype_name IS NOT NULL"
        )
        inherited_types = {str(row[0]) for row in cur.fetchall()}
        cur.execute("SELECT type_name FROM user_types WHERE methods > 0")
        method_types = {str(row[0]) for row in cur.fetchall()}
        cur.execute(
            "SELECT type_name, coll_type, elem_type_owner, elem_type_name, "
            "elem_type_mod FROM user_coll_types"
        )
        collection_rows = cur.fetchall()
        cur.execute(
            "SELECT name, type, referenced_name, referenced_type FROM user_dependencies "
            "WHERE UPPER(referenced_owner) = :1",
            (owner.upper(),),
        )
        dependency_rows = cur.fetchall()
        cur.execute(
            "SELECT type_name, attr_type_owner, attr_type_name, attr_type_mod "
            "FROM user_type_attrs ORDER BY type_name, attr_no"
        )
        attribute_rows = cur.fetchall()

    names: set[str] = set()
    typecodes: dict[str, str] = {}
    unsupported: list[str] = []
    for name, status, typecode, predefined, incomplete in rows:
        name = str(name)
        validate_oracle_identifier(name, "type")
        names.add(name)
        kind = str(typecode or "UNKNOWN").upper()
        typecodes[name] = kind
        if (
            kind not in {"OBJECT", "COLLECTION"}
            or str(predefined).upper() == "YES" or str(incomplete).upper() == "YES"
            or name.upper() in type_bodies
        ):
            unsupported.append(f"{kind} {name}")
        if name in inherited_types:
            unsupported.append(f"inherited OBJECT TYPE {name}")
        if name in method_types:
            unsupported.append(f"method-bearing TYPE {name}")
        if str(status).upper() != "VALID":
            unsupported.append(f"invalid TYPE {name} ({status})")
    collection_info: dict[str, tuple[str, str | None, str | None, Any]] = {}
    for type_name, coll_type, elem_owner, elem_name, elem_mod in collection_rows:
        normalized = " ".join(str(coll_type or "").upper().split())
        if normalized not in {"VARYING ARRAY", "TABLE"}:
            unsupported.append(f"unsupported collection kind {coll_type} {type_name}")
            continue
        collection_info[str(type_name)] = (
            "varray" if normalized == "VARYING ARRAY" else "nested_table",
            str(elem_owner) if elem_owner else None,
            str(elem_name) if elem_name else None,
            elem_mod,
        )
    for name, kind in typecodes.items():
        if kind == "COLLECTION" and name not in collection_info:
            unsupported.append(f"collection metadata unavailable for {name}")

    dependencies: dict[str, set[str]] = {name: set() for name in names}
    for dep_name, dep_kind, referenced_name, referenced_kind in dependency_rows:
        node, dependency = str(dep_name), str(referenced_name)
        if (
            node in names and dependency in names
            and str(dep_kind).upper() == "TYPE"
            and str(referenced_kind).upper() == "TYPE"
        ):
            dependencies[node].add(dependency)
    for type_name, attr_owner, attr_name, attr_mod in attribute_rows:
        node = str(type_name)
        if node in names:
            _collect_type_reference(
                node, attr_name, attr_owner, attr_mod, owner, names,
                dependencies[node], unsupported,
            )
    for type_name, info in collection_info.items():
        if type_name in names:
            _, elem_owner, elem_name, elem_mod = info
            _collect_type_reference(
                type_name, elem_name, elem_owner, elem_mod, owner,
                names, dependencies[type_name], unsupported,
            )
    if unsupported:
        raise RuntimeError(
            "Oracle FULL cannot safely migrate unsupported user-defined TYPE(s): "
            + ", ".join(sorted(set(unsupported)))
        )

    ordered = _type_dependency_order(names, dependencies)
    results: list[TypeDef] = []
    for name in ordered:
        _check_local_dependencies(conn, owner, name, "TYPE")
        ddl = _get_ddl(conn, "TYPE", name, owner)
        ddl = _validate_single_create(ddl, "TYPE", name)
        if re.search(
            r"\b(?:MEMBER|STATIC|CONSTRUCTOR)\s+(?:FUNCTION|PROCEDURE)\b|"
            r"\b(?:MAP|ORDER)\s+MEMBER\b",
            ddl,
            flags=re.IGNORECASE,
        ):
            raise RuntimeError(
                f"Oracle OBJECT TYPE {name} declares methods, which are unsupported for migration"
            )
        kind = "object" if typecodes[name] == "OBJECT" else collection_info[name][0]
        results.append(TypeDef(name=name, kind=kind, ddl=ddl, schema=owner))
    return results


def _collect_type_reference(
    type_name: str, member_type: Any, type_owner: Any, type_mod: Any,
    owner: str, known_names: set[str],
    dependencies: set[str], unsupported: list[str],
) -> None:
    catalog_name = " ".join(str(member_type or "UNKNOWN").upper().split())
    name = _ATTRIBUTE_TYPE_ALIASES.get(catalog_name, catalog_name)
    type_owner = str(type_owner or "").upper()
    if name in _TIMEZONE_ATTRIBUTE_TYPES and type_owner in {"", "SYS", "SYSTEM"}:
        unsupported.append(
            f"{type_name}.{member_type} resolves to {name}, but python-oracledb's "
            "OBJECT attribute fetch returns a naive datetime without the source "
            "time-zone displacement; lossless OBJECT value transport is unsupported"
        )
        return
    if name in _SUPPORTED_ATTRIBUTE_TYPES and type_owner in {"", "SYS", "SYSTEM"}:
        if type_mod not in (None, ""):
            unsupported.append(f"{type_name}.{member_type} modified type")
        return
    referenced_name = next(
        (candidate for candidate in known_names if candidate.casefold() == name.casefold()),
        None,
    )
    if (
        referenced_name is not None and type_owner in {"", owner.upper()}
        and type_mod in (None, "")
    ):
        dependencies.add(referenced_name)
        return
    unsupported.append(
        f"{type_name}.{member_type} {type_owner + '.' if type_owner else ''}{name}"
    )


def _type_dependency_order(
    names: set[str], dependencies: dict[str, set[str]]
) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise RuntimeError(
                f"Oracle TYPE dependency cycle prevents safe FULL ordering at TYPE {name}"
            )
        visiting.add(name)
        for dependency in sorted(dependencies[name]):
            if dependency in names:
                visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in sorted(names):
        visit(name)
    return ordered


def serialize_object(
    value: Any, type_object: Any, collection_kinds: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """Convert nested objects and SQL collections into portable batch data."""
    return _serialize_value(
        value, type_object,
        {str(name).casefold(): str(kind).casefold() for name, kind in (collection_kinds or {}).items()},
    )


def _serialize_value(value: Any, type_object: Any, collection_kinds: dict[str, str]) -> Any:
    if value is None:
        return None
    if getattr(type_object, "iscollection", False):
        if not callable(getattr(value, "aslist", None)):
            raise RuntimeError(f"Oracle collection value for {type_object.name} is malformed")
        kind = collection_kinds.get(str(type_object.name).casefold())
        if kind not in {"varray", "nested_table"}:
            raise RuntimeError(f"Oracle collection kind for {type_object.name} is unknown")
        return {
            "__oracle_collection__": str(type_object.name),
            "kind": kind,
            "items": [
                _serialize_value(item, type_object.element_type, collection_kinds)
                for item in value.aslist()
            ],
        }
    if not hasattr(type_object, "attributes"):
        return value
    attributes: dict[str, Any] = {}
    for attribute in type_object.attributes:
        item = getattr(value, attribute.name)
        if hasattr(item, "read") and callable(item.read):
            raise RuntimeError(
                f"Oracle OBJECT LOB attribute {attribute.name} is unsupported for safe transport"
            )
        attr_type = attribute.type
        if getattr(attr_type, "iscollection", False) or hasattr(attr_type, "attributes"):
            attributes[attribute.name] = _serialize_value(item, attr_type, collection_kinds)
        else:
            attributes[attribute.name] = item
    return {"__oracle_object__": str(type_object.name), "attributes": attributes}


def hydrate_object(
    payload: Any, type_object: Any, collection_kinds: dict[str, str] | None = None
) -> Any:
    """Construct a target driver object while preserving SQL NULL distinctly."""
    if payload is None:
        return None
    if getattr(type_object, "iscollection", False):
        if not isinstance(payload, dict) or set(payload) != {
            "__oracle_collection__", "kind", "items"
        }:
            raise RuntimeError("Oracle collection payload is malformed")
        if str(payload["__oracle_collection__"]).casefold() != str(type_object.name).casefold():
            raise RuntimeError("Oracle collection payload type does not match target type metadata")
        expected_kind = (collection_kinds or {}).get(str(type_object.name).casefold())
        if (
            payload["kind"] not in {"varray", "nested_table"}
            or (expected_kind is not None and payload["kind"] != expected_kind)
            or not isinstance(payload["items"], list)
        ):
            raise RuntimeError("Oracle collection payload kind or items are malformed")
        items = [
            _hydrate_value(item, type_object.element_type, collection_kinds)
            for item in payload["items"]
        ]
        return type_object.newobject(items)
    if not isinstance(payload, dict) or set(payload) != {"__oracle_object__", "attributes"}:
        raise RuntimeError("Oracle OBJECT payload is malformed")
    if str(payload["__oracle_object__"]).casefold() != str(type_object.name).casefold():
        raise RuntimeError("Oracle OBJECT payload type does not match the target column type")
    expected = {attribute.name for attribute in type_object.attributes}
    attributes = payload["attributes"]
    if not isinstance(attributes, dict) or set(attributes) != expected:
        raise RuntimeError("Oracle OBJECT payload attributes do not match target type metadata")
    obj = type_object.newobject()
    for attribute in type_object.attributes:
        value = attributes[attribute.name]
        if value is not None and (
            getattr(attribute.type, "iscollection", False)
            or hasattr(attribute.type, "attributes")
        ):
            value = _hydrate_value(value, attribute.type, collection_kinds)
        setattr(obj, attribute.name, value)
    return obj


def _hydrate_value(
    payload: Any, type_object: Any, collection_kinds: dict[str, str] | None
) -> Any:
    if payload is None:
        return None
    if getattr(type_object, "iscollection", False):
        if not isinstance(payload, dict) or set(payload) != {
            "__oracle_collection__", "kind", "items"
        } or str(payload["__oracle_collection__"]).casefold() != str(type_object.name).casefold():
            raise RuntimeError("Oracle nested collection payload does not match target metadata")
        expected_kind = (collection_kinds or {}).get(str(type_object.name).casefold())
        if (
            payload["kind"] not in {"varray", "nested_table"}
            or (expected_kind is not None and payload["kind"] != expected_kind)
            or not isinstance(payload["items"], list)
        ):
            raise RuntimeError("Oracle nested collection items are malformed")
        return type_object.newobject([
            _hydrate_value(item, type_object.element_type, collection_kinds)
            for item in payload["items"]
        ])
    if hasattr(type_object, "attributes"):
        return hydrate_object(payload, type_object, collection_kinds)
    return payload


def create_type(
    conn: Any,
    type_def: TypeDef,
    *,
    target_owner: str,
) -> None:
    """Create a supported Oracle OBJECT type after safe conflict checks."""
    validate_oracle_identifier(type_def.name, "type")
    validate_oracle_identifier(target_owner, "target owner")
    if type_def.kind.casefold() not in {"object", "varray", "nested_table"}:
        raise RuntimeError(
            f"Unsupported Oracle TYPE kind {type_def.kind!r} for {type_def.name}"
        )
    _assert_target_type(conn, type_def.name, "TYPE")
    ddl = _remap_owner_qualifiers(type_def.ddl, type_def.schema, target_owner)
    ddl = _validate_single_create(ddl, "TYPE", type_def.name)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM user_objects WHERE object_name = :1 AND object_type = 'TYPE'",
            (type_def.name,),
        )
        if cur.fetchone() is not None:
            existing = _get_ddl(conn, "TYPE", type_def.name, target_owner)
            normalize = lambda value: str(value).strip().rstrip(";").strip()
            if normalize(existing) != normalize(ddl):
                raise RuntimeError(
                    f"Existing Oracle TYPE {type_def.name} differs from source; "
                    "automatic TYPE replacement is not supported"
                )
            return
        cur.execute(ddl)
    conn.commit()
