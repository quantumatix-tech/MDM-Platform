"""Oracle routine and package discovery and application."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.connectors.base import FunctionDef
from core.connectors.oracle._models import validate_oracle_identifier
from core.connectors.oracle.objects._common import (
    _assert_no_unmanaged_dependents,
    _assert_target_type,
    _check_compile_status,
    _check_local_dependencies,
    _dependency_order,
    _get_ddl,
    _remap_owner_qualifiers,
    _validate_single_create,
)


def discover_routines(conn: Any, owner: str) -> list[FunctionDef]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_name, object_type, status FROM user_objects "
            "WHERE object_type IN ('FUNCTION','PROCEDURE','PACKAGE','PACKAGE BODY') "
            "ORDER BY object_type, object_name"
        )
        rows = [(str(n), str(t), str(s)) for n, t, s in cur.fetchall()]
    invalid = [f"{typ} {name}" for name, typ, status in rows if status != "VALID"]
    if invalid:
        raise RuntimeError(f"Invalid Oracle source routine/package(s): {', '.join(invalid)}")
    names_by_type: dict[str, set[str]] = defaultdict(set)
    for name, typ, _ in rows:
        names_by_type[typ].add(name)
    # Specs are ordered before standalone routines; package bodies are last.
    spec_order = _dependency_order(conn, owner, {"PACKAGE": names_by_type["PACKAGE"]})
    routine_types = {
        typ: names for typ, names in names_by_type.items() if typ in {"FUNCTION", "PROCEDURE"}
    }
    routine_order = _dependency_order(conn, owner, routine_types)
    body_order = _dependency_order(conn, owner, {"PACKAGE BODY": names_by_type["PACKAGE BODY"]})
    ordered = [*spec_order, *routine_order, *body_order]
    result = []
    type_map = {
        "PACKAGE": ("PACKAGE_SPEC", "package_spec"),
        "PACKAGE BODY": ("PACKAGE_BODY", "package_body"),
        "FUNCTION": ("FUNCTION", "function"),
        "PROCEDURE": ("PROCEDURE", "procedure"),
    }
    for typ, name in ordered:
        validate_oracle_identifier(name, "routine/package")
        _check_local_dependencies(conn, owner, name, typ)
        metadata_type, kind = type_map[typ]
        result.append(
            FunctionDef(
                name=name,
                schema_name=owner,
                ddl=_get_ddl(conn, metadata_type, name, owner),
                kind=kind,
            )
        )
    return result


def create_function(
    conn: Any,
    func: FunctionDef,
    managed: set[tuple[str, str]] | None = None,
    target_owner: str | None = None,
) -> None:
    expected, kinds = {
        "function": ("FUNCTION", ("FUNCTION",)),
        "procedure": ("PROCEDURE", ("PROCEDURE",)),
        "package_spec": ("PACKAGE", ("PACKAGE",)),
        "package_body": ("PACKAGE", ("PACKAGE BODY",)),
    }.get(func.kind, (None, ()))
    if expected is None:
        raise RuntimeError(f"Unsupported Oracle routine kind {func.kind!r} for {func.name}")
    _assert_target_type(conn, func.name, expected)
    if func.kind in {"function", "procedure", "package_spec", "package_body"}:
        _assert_no_unmanaged_dependents(
            conn, func.name, ("FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY"), managed or set()
        )
    expected_ddl_kind = {
        "function": "FUNCTION",
        "procedure": "PROCEDURE",
        "package_spec": "PACKAGE",
        "package_body": "PACKAGE BODY",
    }[func.kind]
    ddl = _remap_owner_qualifiers(func.ddl, func.schema_name, target_owner)
    ddl = _validate_single_create(ddl, expected_ddl_kind, func.name)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    _check_compile_status(conn, func.name, kinds)
