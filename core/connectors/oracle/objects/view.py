"""Oracle view and materialized-view discovery and application."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.connectors.base import ViewDefinition, quote_identifier
from core.connectors.oracle._models import validate_oracle_identifier
from core.connectors.oracle.objects._common import (
    _SYSTEM_OWNERS,
    _assert_no_unmanaged_dependents,
    _assert_target_type,
    _check_compile_status,
    _check_local_dependencies,
    _dependency_order,
    _get_ddl,
    _normalize_materialized_view_query,
    _remap_owner_qualifiers,
    _validate_single_create,
)


def discover_views(
    conn: Any, owner: str, selected_tables: set[str] | None = None
) -> list[ViewDefinition]:
    with conn.cursor() as cur:
        cur.execute("SELECT view_name FROM user_views ORDER BY view_name")
        names = [str(r[0]) for r in cur.fetchall()]
        if names:
            cur.execute(
                "SELECT object_name, status FROM user_objects "
                "WHERE object_type = 'VIEW' ORDER BY object_name"
            )
            statuses = {str(n): str(s) for n, s in cur.fetchall()}
    invalid = [name for name in names if statuses.get(name, "VALID") != "VALID"]
    if invalid:
        raise RuntimeError(f"Invalid Oracle source view(s): {', '.join(invalid)}")
    order = _dependency_order(conn, owner, {"VIEW": set(names)})
    if selected_tables is not None:
        order = _filter_view_order_to_tables(conn, order, selected_tables)
    result = []
    for _, name in order:
        validate_oracle_identifier(name, "view")
        _check_local_dependencies(conn, owner, name, "VIEW")
        result.append(
            ViewDefinition(
                name=name, schema_name=owner, definition=_get_ddl(conn, "VIEW", name, owner)
            )
        )
    return result


def _filter_view_order_to_tables(
    conn: Any,
    order: list[tuple[str, str]],
    selected_tables: set[str],
) -> list[tuple[str, str]]:
    """Keep views whose table dependencies are within the selected scope."""
    names = {name.upper() for _, name in order}
    dependencies: dict[str, set[str]] = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, referenced_name, referenced_type FROM user_dependencies "
            "WHERE type = 'VIEW' AND referenced_owner IS NOT NULL"
        )
        rows = cur.fetchall()
    for name, referenced_name, referenced_type in rows:
        key = str(name).upper()
        dep = str(referenced_name).upper()
        if key in names and str(referenced_type).upper() in {"TABLE", "VIEW"}:
            dependencies[key].add(dep)

    retained: set[str] = set()
    rejected: set[str] = set()

    def in_scope(name: str, visiting: set[str]) -> bool:
        if name in retained:
            return True
        if name in rejected or name in visiting:
            return False
        visiting.add(name)
        valid = True
        for dep in dependencies[name]:
            if dep in names:
                valid = in_scope(dep, visiting) and valid
            elif dep not in selected_tables and dep not in _SYSTEM_OWNERS:
                valid = False
        visiting.remove(name)
        (retained if valid else rejected).add(name)
        return valid

    for name in names:
        in_scope(name, set())
    return [(typ, name) for typ, name in order if name.upper() in retained]


def create_view(
    conn: Any,
    view: ViewDefinition,
    managed: set[tuple[str, str]] | None = None,
    target_owner: str | None = None,
) -> None:
    _assert_target_type(conn, view.name, "VIEW")
    _assert_no_unmanaged_dependents(conn, view.name, ("VIEW",), managed or set())
    ddl = _remap_owner_qualifiers(view.definition, view.schema_name, target_owner)
    ddl = _validate_single_create(ddl, "VIEW", view.name).rstrip(";")
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    _check_compile_status(conn, view.name, ("VIEW",))


def create_materialized_view(
    conn: Any,
    mv: Any,
    target_owner: str | None = None,
    *,
    create_if_missing: bool = True,
) -> None:
    """Create or safely revalidate a matching Oracle native materialized view."""
    validate_oracle_identifier(mv.name, "materialized view")
    expected = ("IMMEDIATE", "DEMAND", "COMPLETE", "N")
    actual = tuple(
        str(getattr(mv, key, default) or "").upper()
        for key, default in (
            ("build_mode", "IMMEDIATE"),
            ("refresh_mode", "DEMAND"),
            ("refresh_method", "COMPLETE"),
            ("rewrite_enabled", "N"),
        )
    )
    if actual != expected:
        raise RuntimeError(
            f"Unsupported Oracle materialized view configuration for {mv.name}: {actual}"
        )
    _assert_target_type(conn, mv.name, "MATERIALIZED VIEW")
    definition = _remap_owner_qualifiers(mv.definition, mv.schema_name, target_owner)
    if not definition:
        raise RuntimeError(f"Oracle materialized view {mv.name} has an invalid query definition")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.build_mode, m.refresh_mode, m.refresh_method, "
            "m.rewrite_enabled, m.query, o.status FROM user_mviews m "
            "JOIN user_objects o ON o.object_name = m.mview_name "
            "AND o.object_type = 'MATERIALIZED VIEW' "
            "WHERE m.mview_name = :1",
            (mv.name,),
        )
        existing = cur.fetchone()
    if existing is not None:
        if len(existing) < 6:
            raise RuntimeError(f"Oracle materialized-view metadata for {mv.name} is incomplete")
        actual_settings = tuple(str(value or "").upper() for value in existing[:4])
        expected_query = _normalize_materialized_view_query(definition)
        actual_query = _normalize_materialized_view_query(existing[4])
        if actual_settings != expected or actual_query != expected_query:
            raise RuntimeError(
                f"Existing Oracle materialized view {mv.name} differs from the source; "
                "it was preserved and FULL stopped before replacing managed table rows"
            )
        status = str(existing[5] or "UNKNOWN").upper()
        if status == "INVALID":
            # Oracle can invalidate a compatible MV after supported changes to
            # its base table. COMPILE revalidates its query without refreshing
            # or replacing the stored rows; FULL refreshes only after data load.
            with conn.cursor() as cur:
                cur.execute(f"ALTER MATERIALIZED VIEW {quote_identifier(mv.name)} COMPILE")
            conn.commit()
            _check_compile_status(conn, mv.name, ("MATERIALIZED VIEW",))
        elif status != "VALID":
            raise RuntimeError(
                f"Existing Oracle materialized view {mv.name} has unsupported status "
                f"{status}; refusing reuse"
            )
        if actual_settings == expected and actual_query == expected_query:
            # Keep the native target object in place. The orchestrator performs
            # its COMPLETE refresh after source-managed table rows are loaded.
            return
    # A listed materialized view must have both its native object row and its
    # USER_MVIEWS definition. Missing catalog metadata is an inspection failure,
    # not permission to attempt a duplicate CREATE.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_type FROM user_objects WHERE object_name = :1 "
            "AND object_type = 'MATERIALIZED VIEW'",
            (mv.name,),
        )
        native_rows = cur.fetchall()
    if native_rows:
        raise RuntimeError(
            f"Oracle materialized view {mv.name} exists in USER_OBJECTS but its "
            "USER_MVIEWS definition is unavailable; refusing unsafe reuse"
        )
    if not create_if_missing:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE MATERIALIZED VIEW {quote_identifier(mv.name)} "
            "BUILD IMMEDIATE REFRESH COMPLETE ON DEMAND DISABLE QUERY REWRITE AS " + definition
        )
    conn.commit()
    _check_compile_status(conn, mv.name, ("MATERIALIZED VIEW",))


def refresh_materialized_view(conn: Any, name: str) -> None:
    validate_oracle_identifier(name, "materialized view")
    with conn.cursor() as cur:
        cur.execute("BEGIN DBMS_MVIEW.REFRESH(:name, method => 'C'); END;", {"name": name})
    conn.commit()
