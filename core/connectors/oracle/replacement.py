"""Owner-scoped Oracle FULL schema replacement planning.

Planning is deliberately read-only. Applying a plan is performed by Oracle
FULL after all source and target safety checks have completed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from core.connectors.base import quote_identifier
from core.connectors.oracle._models import validate_oracle_identifier
from core.connectors.oracle.indexes import normalize_index_rows
from core.connectors.oracle.objects import discover_table_partitioning

_DROP_ORDER = {
    "SYNONYM": -1,
    "VIEW": 0,
    "MATERIALIZED VIEW": 0,
    "TRIGGER": 1,
    "FUNCTION": 2,
    "PROCEDURE": 2,
    "PACKAGE": 2,
    "SEQUENCE": 3,
    "INDEX": 4,
    "CONSTRAINT": 5,
    "TABLE": 6,
    "TYPE": 7,
}


@dataclass(frozen=True)
class ReplacementPlan:
    owner: str
    plan_id: str
    inventory: tuple[dict[str, str], ...]
    cleanup: tuple[dict[str, str], ...]
    implicit_cleanup: tuple[dict[str, str], ...]
    blockers: tuple[str, ...]
    inspection_errors: tuple[str, ...]
    dependency_impact: tuple[dict[str, str], ...]
    grants_impact: tuple[dict[str, str], ...]
    source_inventory: tuple[dict[str, str], ...]
    missing: tuple[dict[str, str], ...] = ()
    source_metadata: tuple[dict[str, str], ...] = ()
    preserved_target_only: tuple[dict[str, str], ...] = ()
    mviews_requiring_revalidation: tuple[dict[str, str], ...] = ()
    destructive_schema_changes: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "plan_id": self.plan_id,
            "preservation_policy": "source_managed",
            "status": (
                "inspection_incomplete" if self.inspection_errors else
                "blocked" if self.blockers else "ready"
            ),
            "source_inventory": list(self.source_inventory),
            "source_metadata": list(self.source_metadata),
            "target_inventory": list(self.inventory),
            "missing_source_objects": list(self.missing),
            "preserved_target_only": list(self.preserved_target_only),
            "mviews_requiring_revalidation": list(self.mviews_requiring_revalidation),
            "cleanup": list(self.cleanup),
            "destructive_schema_changes": list(self.destructive_schema_changes),
            "implicit_cleanup": list(self.implicit_cleanup),
            "blockers": list(self.blockers),
            "inspection_errors": list(self.inspection_errors),
            "dependency_impact": list(self.dependency_impact),
            "grants_impact": list(self.grants_impact),
            "destructive_ddl_count": (
                len(self.cleanup) + len(self.destructive_schema_changes)
            ),
        }


def overwrite_authorization(
    plan: ReplacementPlan,
    *,
    source_inventory: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Describe the configured permission to replace Oracle FULL target state.

    The stable ``enabled`` policy is the opt-in for replacing rows in the
    discovered source-managed tables. Routine data replacement does not need
    a plan-specific approval. Owner-local cleanup is schema DDL and continues
    to require an ``approval_id`` matching the exact read-only plan.
    """
    source_tables = {
        str(schema.name).upper() for schema in source_inventory.get("schemas", [])
    }
    plan_view = plan.as_dict() if callable(getattr(plan, "as_dict", None)) else {}
    inventory = getattr(plan, "inventory", None)
    if inventory is None:
        inventory = plan_view.get("target_inventory", ())
    cleanup = getattr(plan, "cleanup", None)
    if cleanup is None:
        cleanup = plan_view.get("cleanup", ())
    schema_changes = getattr(plan, "destructive_schema_changes", None)
    if schema_changes is None:
        schema_changes = plan_view.get("destructive_schema_changes", ())
    plan_id = str(getattr(plan, "plan_id", plan_view.get("plan_id", "")))
    target_tables = {
        str(item.get("name", "")).upper()
        for item in inventory
        if str(item.get("type", "")).upper() == "TABLE"
    }
    existing_managed_tables = sorted(source_tables & target_tables)
    requires_replacement_policy = bool(existing_managed_tables or cleanup or schema_changes)
    requires_approval = bool(cleanup or schema_changes)
    enabled = policy.get("enabled") is True
    approval_id = str(policy.get("approval_id") or "").strip().upper()
    plan_id = plan_id.strip().upper()
    exact_plan_approved = bool(plan_id) and approval_id == plan_id
    approved = not requires_replacement_policy or (
        enabled and (not requires_approval or exact_plan_approved)
    )
    reason = None
    if requires_replacement_policy and not enabled:
        reason = (
            "migration.oracle_schema_replacement.enabled must be true to replace "
            "existing source-managed Oracle target state"
        )
    elif requires_approval and not exact_plan_approved:
        reason = (
            "migration.oracle_schema_replacement.approval_id must match this exact "
            "read-only plan before destructive owner-local schema changes; review "
            "--oracle-overwrite-plan and approve its plan_id"
        )
    return {
        "status": (
            "blocked" if not approved else
            "approved" if requires_approval else
            "enabled" if requires_replacement_policy else
            "not_required"
        ),
        "requires_explicit_approval": requires_approval,
        "replacement_policy_enabled": enabled,
        "overwrite_source_managed_tables": existing_managed_tables,
        "cleanup_objects": list(cleanup),
        "destructive_schema_changes": list(schema_changes),
        "destructive_ddl_count": len(cleanup) + len(schema_changes),
        "plan_id": plan_id,
        "configured_enabled": enabled,
        "configured_approval_id": approval_id or None,
        "reason": reason,
    }


def _fetchall(conn: Any, sql: str, binds: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        return list(cur.fetchall())


def _validated_framework_provenance_table(conn: Any, owner: str) -> bool:
    """Identify only the connector's valid marked ledger as internal metadata."""
    from core.connectors.oracle.permissions import (
        PROVENANCE_TABLE,
        _read_provenance,
    )

    if not _fetchall(
        conn, "SELECT table_name FROM user_tables WHERE table_name=:1",
        (PROVENANCE_TABLE,),
    ):
        return False
    columns = _fetchall(
        conn,
        "SELECT column_name, data_type FROM user_tab_columns "
        "WHERE table_name=:1 ORDER BY column_id",
        (PROVENANCE_TABLE,),
    )
    expected = {
        "ITEM_KEY", "KIND", "OBJECT_NAME", "COLUMN_NAME", "PRIVILEGE", "GRANTEE", "STATE"
    }
    if {str(row[0]).upper() for row in columns} != expected or any(
        str(row[1]).upper() != "VARCHAR2" for row in columns
    ):
        raise RuntimeError(
            f"Target object {owner}.{PROVENANCE_TABLE} uses a reserved framework name "
            "but does not match the validated provenance-table structure; refusing replacement planning"
        )
    try:
        _read_provenance(conn, owner)
    except Exception as exc:
        raise RuntimeError(
            f"Target object {owner}.{PROVENANCE_TABLE} has invalid framework provenance metadata; "
            f"refusing replacement planning ({type(exc).__name__})"
        ) from None
    return True


def _target_inventory(
    conn: Any, owner: str, *, inspect_cross_owner_dependencies: bool = True
) -> tuple[
    list[dict[str, str]], list[dict[str, str]], dict[str, list[str]],
    list[dict[str, str]], list[str], list[dict[str, str]], list[dict[str, str]],
    dict[str, dict[str, Any] | None],
]:
    """Read owner objects and validate native partition children against their parents."""
    session_user = _fetchall(conn, "SELECT USER FROM dual")
    if not session_user or str(session_user[0][0]).upper() != owner.upper():
        raise RuntimeError(
            f"Oracle schema replacement requires a direct connection as target owner {owner}"
        )
    if owner.upper() in {"SYS", "SYSTEM"}:
        raise RuntimeError("Oracle schema replacement refuses SYS and SYSTEM owners")
    from core.connectors.oracle.permissions import PROVENANCE_TABLE

    normal_tables = {
        str(row[0]) for row in _fetchall(
            conn, "SELECT table_name FROM user_tables WHERE nested = 'NO'"
        )
    }
    internal_tables = (
        {PROVENANCE_TABLE}
        if PROVENANCE_TABLE in normal_tables
        and _validated_framework_provenance_table(conn, owner)
        else set()
    )
    normal_tables.difference_update(internal_tables)
    materialized_rows = _fetchall(
        conn,
        "SELECT m.mview_name, m.build_mode, m.refresh_mode, m.refresh_method, "
        "m.rewrite_enabled, o.status, m.query FROM user_mviews m "
        "LEFT JOIN user_objects o ON o.object_name = m.mview_name "
        "AND o.object_type = 'MATERIALIZED VIEW' ORDER BY m.mview_name",
    )
    materialized_metadata: dict[str, dict[str, Any]] = {}
    for row in materialized_rows:
        if not isinstance(row, (tuple, list)) or len(row) != 7:
            raise RuntimeError(
                "Oracle target materialized-view catalog returned malformed USER_MVIEWS metadata; "
                "expected name, refresh settings, status, and query"
            )
        name, build_mode, refresh_mode, refresh_method, rewrite_enabled, status, query = row
        if hasattr(query, "read") and callable(query.read):
            query = query.read()
        materialized_metadata[str(name)] = {
            "mview_build_mode": str(build_mode or "").upper(),
            "mview_refresh_mode": str(refresh_mode or "").upper(),
            "mview_refresh_method": str(refresh_method or "").upper(),
            "mview_rewrite_enabled": str(rewrite_enabled or "").upper(),
            "mview_status": str(status or "").upper(),
            "mview_query": str(query or ""),
        }
    # USER_MVIEWS identifies the MV definition; USER_OBJECTS must also confirm
    # its native MATERIALIZED VIEW row before an identically named TABLE row is
    # treated as the MV storage segment.
    materialized_names = {
        name for name, metadata in materialized_metadata.items()
        if metadata["mview_status"]
    }
    normal_tables.difference_update(materialized_names)
    table_partitioning = {
        table_name: discover_table_partitioning(conn, table_name)
        for table_name in sorted(normal_tables)
    }
    object_rows = _fetchall(
        conn,
        "SELECT object_name, object_type, subobject_name FROM user_objects "
        "WHERE generated = 'N' ORDER BY object_type, object_name, subobject_name",
    )
    objects: list[dict[str, str]] = []
    partition_object_rows: list[tuple[str, str | None]] = []
    for object_row in object_rows:
        if not isinstance(object_row, (tuple, list)) or len(object_row) != 3:
            raise RuntimeError(
                "Oracle target object inventory returned malformed USER_OBJECTS metadata; "
                "expected object_name, object_type, and subobject_name"
            )
        name, kind, subobject_name = object_row
        name, kind = str(name), str(kind).upper()
        if kind == "TABLE PARTITION":
            partition_object_rows.append((name, subobject_name))
        else:
            item = {
                "type": "PACKAGE" if kind == "PACKAGE BODY" else kind,
                "name": name,
            }
            if kind == "MATERIALIZED VIEW":
                item.update(materialized_metadata.get(name, {
                    "mview_catalog_missing": "Y",
                }))
            objects.append(item)
    for parent_name, partition_name in partition_object_rows:
        parent_name = str(parent_name)
        partition_name = str(partition_name or "")
        if parent_name in internal_tables:
            continue
        if parent_name not in normal_tables or not partition_name:
            raise RuntimeError(
                "Oracle target inventory found an orphaned TABLE PARTITION object "
                f"{parent_name}.{partition_name or '<unnamed>'}"
            )
        partitioning = table_partitioning.get(parent_name)
        known_partition_names = {
            str(part["name"]) for part in (partitioning or {}).get("partitions", [])
        }
        if partitioning is None or partition_name not in known_partition_names:
            raise RuntimeError(
                "Oracle target inventory found an unmanaged TABLE PARTITION object "
                f"{parent_name}.{partition_name}"
            )
    lob_rows = _fetchall(
            conn,
            "SELECT index_name, table_name, column_name FROM user_lobs "
            "WHERE index_name IS NOT NULL",
    )
    lob_index_names = {str(row[0]) for row in lob_rows}
    indexes = _fetchall(
        conn,
        "SELECT i.index_name, i.table_name, c.constraint_name, i.index_type, i.status, "
        "i.partitioned, CASE WHEN EXISTS (SELECT 1 FROM user_ind_expressions ie "
        "WHERE ie.index_name = i.index_name AND ie.table_name = i.table_name) "
        "THEN 'Y' ELSE 'N' END "
        "FROM user_indexes i LEFT JOIN user_constraints c "
        "ON c.table_name = i.table_name AND c.index_name = i.index_name "
        "WHERE NOT EXISTS (SELECT 1 FROM user_lobs l "
        "WHERE l.table_name = i.table_name AND l.index_name = i.index_name) "
        "ORDER BY i.index_name",
    )
    indexes = [row for row in indexes if str(row[1]) not in internal_tables]
    index_tables = {str(row[0]): str(row[1]) for row in indexes}
    constraint_index_names = {str(row[0]) for row in indexes if row[2] is not None}
    parent_managed = [
        {"type": "CONSTRAINT INDEX", "name": str(row[0]), "table": str(row[1]),
         "managed_by": str(row[2])}
        for row in indexes if row[2] is not None
    ]
    parent_managed.extend(
        {"type": "LOB INDEX", "name": str(name), "table": str(table),
         "managed_by": str(column)}
        for name, table, column in lob_rows
    )
    index_details = {
        str(row[0]): {
            "index_type": str(row[3]).upper(), "status": str(row[4]).upper(),
            "partitioned": str(row[5]).upper(), "functional": str(row[6]).upper() == "Y",
        }
        for row in indexes
    }
    functional_index_names = {
        str(row[0]) for row in indexes if str(row[6]).upper() == "Y"
    }
    if functional_index_names:
        catalog_rows = _fetchall(
            conn,
            "SELECT i.index_name, i.uniqueness, i.index_type, i.status, i.partitioned, "
            "ic.column_name, ic.column_position, ic.descend, ie.column_expression "
            "FROM user_indexes i LEFT JOIN user_ind_columns ic "
            "ON ic.index_name = i.index_name AND ic.table_name = i.table_name "
            "LEFT JOIN user_ind_expressions ie ON ie.index_name = i.index_name "
            "AND ie.table_name = i.table_name AND ie.column_position = ic.column_position "
            "WHERE EXISTS (SELECT 1 FROM user_ind_expressions ex "
            "WHERE ex.index_name = i.index_name AND ex.table_name = i.table_name) "
            "ORDER BY i.index_name, ic.column_position",
        )
        index_tables_for_columns = {
            str(row[0]): str(row[1]) for row in indexes if str(row[0]) in functional_index_names
        }
        table_column_types: dict[str, dict[str, Any]] = {}
        if any(len(row) > 8 and row[8] is not None for row in catalog_rows):
            column_rows = _fetchall(
                conn,
                "SELECT table_name, column_name, data_type FROM user_tab_cols "
                "WHERE hidden_column = 'NO'",
            )
            table_column_types = {}
            for table_name, column_name, data_type in column_rows:
                table_column_types.setdefault(str(table_name), {})[str(column_name)] = data_type
        grouped_functional_rows: dict[str, list[tuple[Any, ...]]] = {}
        for row in catalog_rows:
            if str(row[0]) in functional_index_names:
                grouped_functional_rows.setdefault(str(row[0]), []).append(row)
        for index_name, rows in grouped_functional_rows.items():
            normalized = normalize_index_rows(
                rows,
                table_column_types.get(index_tables_for_columns.get(index_name, ""), {}),
            )[index_name]
            if normalized["supported"]:
                index_details[index_name] = {
                    "index_type": "NORMAL", "status": normalized["status"],
                    "partitioned": normalized["partitioned"], "functional": False,
                    "columns": normalized["columns"],
                    "descending_columns": normalized["descending_columns"],
                }
    missing_index_metadata = [
        item["name"] for item in objects
        if item["type"] == "INDEX"
        and item["name"] not in lob_index_names
        and item["name"] not in index_tables
    ]
    if missing_index_metadata:
        raise RuntimeError(
            "Oracle index inventory is incomplete for " + ", ".join(missing_index_metadata)
        )
    objects = [
        ({**item, "table": index_tables[item["name"]], **index_details[item["name"]]}
         if item["type"] == "INDEX" else item)
        for item in objects
        if item["type"] != "TABLE PARTITION"
        if not (item["type"] == "INDEX" and item["name"] in lob_index_names)
        and not (item["type"] == "INDEX" and item["name"] in constraint_index_names)
        and not (item["type"] == "TABLE" and item["name"] not in normal_tables)
        and not (item["type"] == "INDEX" and index_tables.get(item["name"]) not in normal_tables)
    ]
    objects = list({(item["type"], item["name"]): item for item in objects}.values())
    identity_sequences = _fetchall(
        conn,
        "SELECT 'ISEQ$$_' || o.object_id, i.table_name, i.column_name "
        "FROM user_objects o JOIN user_tab_identity_cols i "
        "ON i.table_name = o.object_name WHERE o.object_type = 'TABLE'",
    )
    parent_managed.extend(
        {"type": "IDENTITY SEQUENCE", "name": str(name), "table": str(table),
         "managed_by": str(column)}
        for name, table, column in identity_sequences
    )
    parent_managed.extend(
        {
            "type": "TABLE PARTITION", "name": str(part["name"]),
            "table": table_name, "managed_by": "TABLE",
            "position": str(part["position"]),
            "high_value": str(part["high_value"]),
        }
        for table_name, partitioning in table_partitioning.items()
        for part in (partitioning or {}).get("partitions", [])
    )
    constraint_rows = _fetchall(
        conn,
        "SELECT table_name, constraint_name, constraint_type, r_owner, r_constraint_name, generated "
        "FROM user_constraints WHERE constraint_type IN ('P','U','R','C') "
        "ORDER BY table_name, constraint_name",
    )
    constraints = [
        {"type": "CONSTRAINT", "name": str(row[1]), "table": str(row[0]),
         "constraint_type": str(row[2]), "r_owner": str(row[3] or ""),
         "r_constraint_name": str(row[4] or "")}
        for row in constraint_rows
        if str(row[0]) not in internal_tables
        and (len(row) < 6 or str(row[5]).upper() != "GENERATED NAME")
    ]
    blockers: dict[str, list[str]] = {}
    inspection_errors: list[str] = []
    grants_impact: list[dict[str, str]] = []
    dependency_impact: list[dict[str, str]] = []
    try:
        grants = _fetchall(
            conn,
            "SELECT grantee, owner, table_name, grantor, privilege, grantable, "
            "hierarchy, common, type, inherited FROM all_tab_privs_made "
            "WHERE owner = :1 ORDER BY table_name, grantee, privilege",
            (owner,),
        )
        for row in grants:
            if len(row) != 10:
                raise RuntimeError("Malformed ALL_TAB_PRIVS_MADE row")
            grantee, grant_owner, name, _grantor, privilege, _grantable, _hierarchy, _common, object_type, _inherited = row
            if str(grant_owner).casefold() != owner.casefold():
                raise RuntimeError("ALL_TAB_PRIVS_MADE returned a row outside the target owner")
            if str(name) in internal_tables:
                continue
            # Oracle exposes GRANT INHERIT PRIVILEGES ON USER through this
            # catalog view too. It is a user-level privilege and is untouched
            # by owner-schema replacement.
            if str(object_type).upper() == "USER" or str(privilege).upper() == "INHERIT PRIVILEGES":
                continue
            grants_impact.append({"object": str(name), "grantee": str(grantee), "privilege": str(privilege), "kind": "object"})
        column_grants = _fetchall(
            conn,
            "SELECT grantee, owner, table_name, column_name, grantor, privilege, "
            "grantable, common, inherited FROM all_col_privs_made "
            "WHERE owner = :1 ORDER BY table_name, column_name, grantee, privilege",
            (owner,),
        )
        for row in column_grants:
            if len(row) != 9:
                raise RuntimeError("Malformed ALL_COL_PRIVS_MADE row")
            grantee, grant_owner, name, column, _grantor, privilege, _grantable, _common, _inherited = row
            if str(grant_owner).casefold() != owner.casefold():
                raise RuntimeError("ALL_COL_PRIVS_MADE returned a row outside the target owner")
            if str(name) in internal_tables:
                continue
            grants_impact.append({
                "object": str(name), "column": str(column), "grantee": str(grantee),
                "privilege": str(privilege), "kind": "column",
            })
    except Exception as exc:
        inspection_errors.append(
            "Grant inventory unavailable (ALL_TAB_PRIVS_MADE/ALL_COL_PRIVS_MADE): "
            f"{type(exc).__name__}: {exc}"
        )
    # No external object can depend on an object in an empty owner. Avoid
    # requiring dictionary-wide visibility for the common fresh-target case;
    # once target objects/constraints exist, the complete cross-owner catalog
    # scan remains mandatory and fails closed when it cannot be read.
    if objects and inspect_cross_owner_dependencies:
        try:
            external_dependencies = _fetchall(
                conn,
                "SELECT owner, name, type, referenced_name, referenced_type "
                "FROM dba_dependencies WHERE UPPER(referenced_owner) = :1 "
                "AND UPPER(owner) <> :2 ORDER BY owner, name, type",
                (owner.upper(), owner.upper()),
            )
            for dep_owner, name, kind, ref_name, ref_type in external_dependencies:
                impact = {
                    "owner": str(dep_owner), "name": str(name), "type": str(kind),
                    "referenced_name": str(ref_name), "referenced_type": str(ref_type),
                }
                dependency_impact.append(impact)
        except Exception as exc:  # noqa: BLE001 - inventory must fail closed
            inspection_errors.append(
                "Cross-owner dependency inventory unavailable (DBA_DEPENDENCIES): "
                f"{type(exc).__name__}: {exc}; required least-privilege grant: "
                f"GRANT SELECT ON SYS.DBA_DEPENDENCIES TO {quote_identifier(owner)}"
            )
    if constraints and inspect_cross_owner_dependencies:
        try:
            external_fk = _fetchall(
                conn,
                "SELECT owner, constraint_name, table_name, r_constraint_name "
                "FROM dba_constraints WHERE constraint_type = 'R' "
                "AND UPPER(r_owner) = :1 AND UPPER(owner) <> :2 "
                "ORDER BY owner, constraint_name",
                (owner.upper(), owner.upper()),
            )
            for fk_owner, fk_name, table, ref_name in external_fk:
                dependency_impact.append({
                    "owner": str(fk_owner), "name": str(fk_name), "type": "FOREIGN KEY",
                    "referenced_name": str(ref_name), "referenced_type": "CONSTRAINT",
                    "table": str(table),
                })
        except Exception as exc:  # noqa: BLE001 - inventory must fail closed
            inspection_errors.append(
                "Cross-owner foreign-key inventory unavailable (DBA_CONSTRAINTS): "
                f"{type(exc).__name__}: {exc}; required least-privilege grant: "
                f"GRANT SELECT ON SYS.DBA_CONSTRAINTS TO {quote_identifier(owner)}"
            )
    try:
        defaults = _fetchall(
            conn,
            "SELECT table_name, column_name, data_default FROM user_tab_cols "
            "WHERE hidden_column = 'NO' AND data_default IS NOT NULL",
        )
        for sequence_name in [
            item["name"] for item in objects if item["type"] == "SEQUENCE"
        ]:
            pattern = re.compile(
                rf"(?i)(?:\"?{re.escape(sequence_name)}\"?\s*\.\s*NEXTVAL)"
            )
            for table_name, column_name, expression in defaults:
                if hasattr(expression, "read") and callable(expression.read):
                    expression = expression.read()
                if pattern.search(str(expression or "")):
                    blockers.setdefault(sequence_name, []).append(
                        f"column default {table_name}.{column_name} references this sequence"
                    )
    except Exception as exc:
        inspection_errors.append(
            "Owner-local sequence-default dependency inventory unavailable (USER_TAB_COLS): "
            f"{type(exc).__name__}: {exc}"
        )
    return (
        objects, constraints, blockers, parent_managed, inspection_errors,
        dependency_impact, grants_impact, table_partitioning,
    )


def build_plan(
    conn: Any,
    owner: str,
    *,
    tables: set[str],
    schemas: list[Any],
    views: list[Any],
    sequences: list[Any],
    routines: list[Any],
    triggers: list[Any],
    materialized_views: list[Any] | None = None,
    synonyms: list[Any] | None = None,
    comments: list[Any] | None = None,
    types: list[Any] | None = None,
    preserve_target_only: bool = True,
    inspect_cross_owner_dependencies: bool = True,
) -> ReplacementPlan:
    """Plan source-managed conflicts while preserving target-only owner objects."""
    if not preserve_target_only:
        raise RuntimeError(
            "Oracle FULL owner-wide replacement is unsupported; "
            "target-only objects must be preserved"
        )
    validate_oracle_identifier(owner, "target owner")
    expected: set[tuple[str, str]] = {("TABLE", str(name)) for name in tables}
    for schema in schemas:
        for index in schema.indexes:
            if not index.constraint_backed:
                expected.add(("INDEX", str(index.name)))
    expected.update(("VIEW", str(item.name)) for item in views)
    expected.update(("MATERIALIZED VIEW", str(item.name)) for item in (materialized_views or []))
    expected.update(("SYNONYM", str(item.name)) for item in (synonyms or []))
    expected.update(("SEQUENCE", str(item.name)) for item in sequences)
    expected.update(("TRIGGER", str(item.name)) for item in triggers)
    expected.update((
        {"function": "FUNCTION", "procedure": "PROCEDURE",
         "package_spec": "PACKAGE", "package_body": "PACKAGE"}[str(item.kind)],
        str(item.name),
    ) for item in routines)
    expected.update(("TYPE", str(item.name)) for item in (types or []))

    # Constraint names are normalized by the existing Oracle target contract.
    from core.connectors.oracle.target import (
        _oracle_constraint_matches,
        _oracle_constraint_name,
        _oracle_not_null_check_column,
    )
    for schema in schemas:
        if schema.primary_key:
            expected.add(("CONSTRAINT", _oracle_constraint_name(
                schema.name, "PK", schema.primary_key_name or ",".join(schema.primary_key)
            )))
        for unique in schema.unique_constraints:
            expected.add(("CONSTRAINT", _oracle_constraint_name(schema.name, "UQ", unique.name)))
        for foreign in schema.foreign_keys:
            expected.add(("CONSTRAINT", _oracle_constraint_name(schema.name, "FK", foreign.name)))
        generated_checks = set(
            schema.options.get("oracle_generated_check_constraints", set())
        )
        for check in schema.check_constraints:
            not_null_column = _oracle_not_null_check_column(check.expression)
            if check.name in generated_checks and not_null_column:
                column = next(
                    (item for item in schema.columns
                     if item.name.casefold() == not_null_column.casefold()),
                    None,
                )
                if column is None or column.nullable:
                    raise RuntimeError(
                        f"Oracle generated NOT NULL check {schema.name}.{check.name} "
                        "does not match a non-nullable source column"
                    )
                # The target column DDL recreates this condition and Oracle
                # generates its own SYS_C constraint name for it.
                continue
            expected.add(("CONSTRAINT", _oracle_constraint_name(schema.name, "CK", check.name)))

    (
        inventory, target_constraints, object_blockers, parent_managed,
        inspection_errors, dependency_impact, grants_impact, target_partitioning,
    ) = (
        _target_inventory(conn, owner)
        if inspect_cross_owner_dependencies
        else _target_inventory(
            conn, owner, inspect_cross_owner_dependencies=False
        )
    )
    actual_keys = {(item["type"], item["name"]) for item in inventory}
    actual_keys.update(("CONSTRAINT", item["name"]) for item in target_constraints)
    # Oracle FULL owns the source inventory by name and type. Objects absent
    # from that inventory belong to the target and must remain untouched.
    # INDEX names occupy a separate Oracle namespace, so only compare index
    # objects against source indexes.
    expected_object_types: dict[str, set[str]] = {}
    for kind, name in expected:
        if kind not in {"INDEX", "CONSTRAINT"}:
            expected_object_types.setdefault(name, set()).add(kind)
    stale: list[dict[str, str]] = []
    blockers: list[str] = []
    from core.connectors.oracle import objects as oracle_objects

    source_materialized_views = {
        str(item.name): item for item in (materialized_views or [])
    }
    from core.connectors.oracle.objects import _get_ddl, _remap_owner_qualifiers
    type_definition_blockers: list[str] = []
    for type_def in types or []:
        if ("TYPE", str(type_def.name)) not in actual_keys:
            continue
        try:
            target_ddl = _get_ddl(conn, "TYPE", str(type_def.name), owner)
            expected_ddl = _remap_owner_qualifiers(
                str(type_def.ddl), str(type_def.schema or ""), owner
            )
            normalize = lambda value: str(value).strip().rstrip(";").strip()
            if normalize(target_ddl) != normalize(expected_ddl):
                type_definition_blockers.append(
                    f"target TYPE {type_def.name} definition differs from source; "
                    "automatic Oracle TYPE replacement is not supported"
                )
        except Exception as exc:
            inspection_errors.append(
                f"Could not compare existing target TYPE {type_def.name}: "
                f"{type(exc).__name__}: {exc}"
            )
    source_columns = {
        str(schema.name).upper(): {str(column.name).upper() for column in schema.columns}
        for schema in schemas
    }
    destructive_schema_changes: list[dict[str, str]] = []
    target_table_names = {
        str(item.get("name", "")).upper()
        for item in inventory if str(item.get("type", "")).upper() == "TABLE"
    }
    for schema in schemas:
        table_name = str(schema.name).upper()
        if table_name not in target_table_names:
            continue
        try:
            target_column_rows = _fetchall(
                conn,
                "SELECT column_name FROM user_tab_cols "
                "WHERE table_name = :1 AND hidden_column = 'NO' ORDER BY column_id",
                (schema.name,),
            )
            if not target_column_rows:
                raise RuntimeError(
                    "target table exists in USER_TABLES but has no visible USER_TAB_COLS rows"
                )
            target_columns = {str(row[0]).upper() for row in target_column_rows}
            for column_name in sorted(target_columns - source_columns.get(table_name, set())):
                destructive_schema_changes.append({
                    "type": "DROP_COLUMN", "table": str(schema.name),
                    "name": column_name,
                })
        except Exception as exc:  # noqa: BLE001 - incomplete catalog inspection must fail closed
            inspection_errors.append(
                f"Could not inspect target columns for {schema.name} (USER_TAB_COLS): "
                f"{type(exc).__name__}: {exc}"
            )
    for impact in grants_impact:
        table_name = str(impact.get("object", "")).upper()
        column_name = str(impact.get("column", "")).upper()
        if (
            impact.get("kind") == "column"
            and table_name in source_columns
            and column_name not in source_columns[table_name]
        ):
            blockers.append(
                f"cannot safely remove source-absent target column {table_name}.{column_name}; "
                f"column privilege for {impact['grantee']} is preserved"
            )
    supported_mview_settings = ("IMMEDIATE", "DEMAND", "COMPLETE", "N")
    for item in inventory:
        if item["type"] != "MATERIALIZED VIEW":
            continue
        source_mv = source_materialized_views.get(item["name"])
        if source_mv is None:
            continue
        if item.get("mview_catalog_missing") == "Y":
            blockers.append(
                f"target MATERIALIZED VIEW {item['name']} has no matching USER_MVIEWS metadata"
            )
            continue
        if item.get("mview_status") not in {"VALID", "INVALID"}:
            blockers.append(
                f"target MATERIALIZED VIEW {item['name']} is "
                f"{item.get('mview_status') or 'UNKNOWN'}"
            )
            continue
        target_settings = tuple(item.get(key, "") for key in (
            "mview_build_mode", "mview_refresh_mode", "mview_refresh_method",
            "mview_rewrite_enabled",
        ))
        source_settings = tuple(str(getattr(source_mv, key, default) or "").upper()
                                for key, default in (
            ("build_mode", "IMMEDIATE"), ("refresh_mode", "DEMAND"),
            ("refresh_method", "COMPLETE"), ("rewrite_enabled", "N"),
        ))
        if source_settings != supported_mview_settings:
            blockers.append(
                f"unsupported source materialized-view settings for {item['name']}: "
                f"{source_settings}"
            )
            continue
        if target_settings != source_settings:
            blockers.append(
                f"target MATERIALIZED VIEW {item['name']} has unsupported or mismatched "
                f"refresh settings {target_settings}"
            )
            continue
        expected_query = oracle_objects._remap_owner_qualifiers(
            source_mv.definition, source_mv.schema_name, owner
        )
        if oracle_objects._normalize_materialized_view_query(
            item.get("mview_query", "")
        ) != oracle_objects._normalize_materialized_view_query(expected_query):
            blockers.append(
                f"target MATERIALIZED VIEW {item['name']} definition differs from source"
            )
    for item in inventory:
        kind, name = item["type"], item["name"]
        if (kind, name) in expected:
            continue
        desired_types = expected_object_types.get(name, set())
        if not desired_types or kind == "INDEX":
            continue
        # Replacing a target table with a different source object type would
        # destroy target data. Keep it and stop before any cleanup.
        if kind == "TABLE":
            blockers.append(
                f"source object name {name} conflicts with target TABLE; "
                "target data is preserved and the conflict must be reviewed"
            )
            continue
        stale.append(dict(item))
    stale_object_names = {item["name"] for item in stale}
    for impact in grants_impact:
        grant_object = str(impact.get("object", "")).split(".", 1)[0]
        if grant_object in stale_object_names:
            blockers.append(
                f"cannot safely replace target {impact['kind']} grants on stale object "
                f"{grant_object} for grantee {impact['grantee']}; existing permissions are preserved"
            )
    # Oracle may retain its own SYS_C* name for an otherwise valid primary
    # key. The table-definition verifier applies the same strict structural
    # and state checks; use that result to satisfy the generated DMS name in
    # this name-oriented inventory without treating unrelated keys as matches.
    with conn.cursor() as cur:
        for schema in schemas:
            if not schema.primary_key:
                continue
            expected_pk_name = _oracle_constraint_name(
                schema.name, "PK",
                schema.primary_key_name or ",".join(schema.primary_key),
            )
            expected_pk = ("CONSTRAINT", expected_pk_name)
            if expected_pk not in actual_keys and _oracle_constraint_matches(
                cur, expected_pk_name, schema.name, "P", schema.primary_key
            ):
                actual_keys.add(expected_pk)
    missing = sorted(expected - actual_keys)
    expected_names = {name for _kind, name in expected}
    for impact in dependency_impact:
        referenced_name = str(impact.get("referenced_name", ""))
        if referenced_name and referenced_name in expected_names:
            blockers.append(
                f"cross-owner dependency {impact.get('owner')}.{impact.get('name')} "
                f"({impact.get('type')}) references source-managed object "
                f"{impact.get('referenced_type')} {referenced_name}"
            )
    partition_blockers: list[str] = []
    for schema in schemas:
        if schema.name not in target_partitioning:
            continue
        expected_partitioning = schema.options.get("oracle_partitioning")
        actual_partitioning = target_partitioning[schema.name]
        if expected_partitioning != actual_partitioning:
            partition_blockers.append(
                f"Oracle target partition metadata does not match source for table {schema.name}"
            )
    stale_tables = {item["name"] for item in stale if item["type"] == "TABLE"}
    unsupported = [
        f"unsupported target-only object type {item['type']} {item['name']}"
        for item in stale if item["type"] not in _DROP_ORDER
    ]
    blockers.extend(unsupported)
    blockers.extend(partition_blockers)
    for item in stale:
        if item["type"] == "INDEX" and (
            item.get("index_type") != "NORMAL" or item.get("status") != "VALID"
            or item.get("partitioned") != "NO" or item.get("functional")
        ):
            blockers.append(
                f"unsupported or unsafe target-only index {item['name']} "
                f"(type={item.get('index_type')}, status={item.get('status')}, "
                f"partitioned={item.get('partitioned')}, functional={item.get('functional')})"
            )
    for schema in schemas:
        expected_index_names = {
            str(index.name) for index in schema.indexes if not index.constraint_backed
        }
        for index in schema.indexes:
            if index.constraint_backed:
                continue
            managed_parent = next((
                item for item in parent_managed
                if item["type"] in {"CONSTRAINT INDEX", "LOB INDEX"}
                and item["name"] == index.name
            ), None)
            if managed_parent:
                blockers.append(
                    f"source-managed index {index.name} conflicts with target "
                    f"{managed_parent['type'].lower()} on {managed_parent['table']}"
                )
            target_index = next((
                item for item in inventory
                if item["type"] == "INDEX" and item["name"] == index.name
            ), None)
            if target_index and str(target_index["table"]).casefold() != schema.name.casefold():
                blockers.append(
                    f"source-managed index {index.name} exists on target table "
                    f"{target_index['table']}, expected {schema.name}; index was not replaced"
                )
        for item in inventory:
            if item["type"] == "INDEX" and item["name"] in expected_index_names and (
                item.get("index_type") != "NORMAL" or item.get("status") != "VALID"
                or item.get("partitioned") != "NO" or item.get("functional")
            ):
                blockers.append(
                    f"source-managed target index {item['name']} is unsupported for safe replacement"
                )
    # A whole-owner plan cannot infer completeness from a selected table list.
    if not tables:
        blockers.append("source owner has no discovered tables; complete scope cannot be established")

    # Constraints on retained tables may be removed explicitly and recreated by
    # the existing apply_constraints phase.  Constraints on stale tables vanish
    # with DROP TABLE.  The planner refuses out-of-owner FKs touching scope.
    # Unmatched target constraints are target-owned state. Source constraints
    # are added or checked by the normal constraint phase; do not prune extras.
    stale_constraints: list[dict[str, str]] = []
    stale_table_foreign_keys = [
        item for item in target_constraints
        if item["table"] in stale_tables and item["constraint_type"] == "R"
    ]
    for item in stale_constraints:
        if item["r_owner"] and item["r_owner"].upper() != owner.upper():
            dependency_impact.append({
                "owner": owner.upper(), "name": item["name"], "type": "FOREIGN KEY",
                "referenced_name": item["r_constraint_name"],
                "referenced_type": "CONSTRAINT", "referenced_owner": item["r_owner"],
                "scope": "target-owner constraint; cleanup does not alter referenced owner",
            })
        stale.append(item)
    for item in stale_table_foreign_keys:
        if item["r_owner"] and item["r_owner"].upper() != owner.upper():
            dependency_impact.append({
                "owner": owner.upper(), "name": item["name"], "type": "FOREIGN KEY",
                "table": item["table"], "referenced_name": item["r_constraint_name"],
                "referenced_owner": item["r_owner"],
                "scope": "target-owner table/constraint removal; referenced owner remains untouched",
            })

    # Reject retained owner objects that depend on objects scheduled for drop;
    # order dependencies among stale objects from leaves toward referenced roots.
    try:
        local_dependencies = _fetchall(
            conn,
            "SELECT name, type, referenced_owner, referenced_name, referenced_type "
            # USER_DEPENDENCIES is inherently scoped to the connected user;
            # unlike DBA_DEPENDENCIES it has no OWNER column.
            "FROM user_dependencies ORDER BY name, type",
        )
    except Exception as exc:
        inspection_errors.append(
            "Owner-local dependency inventory unavailable (USER_DEPENDENCIES): "
            f"{type(exc).__name__}: {exc}; cleanup is unsafe without this inventory"
        )
        local_dependencies = []
    stale_keys = {(item["type"], item["name"]) for item in stale}
    dependency_edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for name, kind, referenced_owner, referenced_name, referenced_type in local_dependencies:
        dependent = ("PACKAGE" if str(kind).upper() == "PACKAGE BODY" else str(kind).upper(), str(name))
        referenced = ("PACKAGE" if str(referenced_type).upper() == "PACKAGE BODY" else str(referenced_type).upper(), str(referenced_name))
        if str(referenced_owner or "").upper() != owner.upper():
            dependency_impact.append({
                "owner": owner.upper(), "name": dependent[1], "type": dependent[0],
                "referenced_owner": str(referenced_owner or ""),
                "referenced_name": referenced[1], "referenced_type": referenced[0],
                "scope": "target-owner object references outside owner; cleanup will not alter referenced object",
            })
            continue
        if referenced not in stale_keys:
            continue
        if dependent == referenced:
            continue
        dependency_impact.append({
            "owner": owner.upper(), "name": dependent[1], "type": dependent[0],
            "referenced_name": referenced[1], "referenced_type": referenced[0],
            "scope": "target owner; dependent will be recreated from source when supported",
        })
        if dependent not in stale_keys:
            if (dependent[0], dependent[1]) not in expected:
                blockers.append(
                    f"cannot safely replace source-managed {referenced[0]} "
                    f"{referenced[1]} while target-only dependent "
                    f"{dependent[0]} {dependent[1]} exists"
                )
            continue
        # Drop dependent conflicts before their referenced objects.
        dependency_edges.setdefault(referenced, set()).add(dependent)

    # Oracle refuses DROP TABLE when an out-of-scope FK references its key. Do
    # not use CASCADE CONSTRAINTS; block before cleanup in those cases.
    stale_constraint_names = {item["name"] for item in stale_constraints}
    for item in target_constraints:
        if item["r_constraint_name"] in stale_constraint_names and item["name"] not in stale_constraint_names:
            blockers.append(
                f"foreign key {item['table']}.{item['name']} references stale constraint "
                f"{item['r_constraint_name']}"
            )

    # Exclude generated indexes/sequences from inventory; ordinary indexes on a
    # stale table are dropped implicitly with their parent table.
    cleanup = [item for item in stale if item["type"] not in {"CONSTRAINT"}]
    implicit_cleanup = [
        item for item in stale
        if item["type"] == "INDEX" and item.get("table") in stale_tables
    ]
    implicit_cleanup.extend(
        item for item in target_constraints
        if item["table"] in stale_tables and item["constraint_type"] != "R"
    )
    implicit_cleanup.extend(
        item for item in parent_managed if item["table"] in stale_tables
    )
    cleanup = [
        item for item in cleanup
        if not (item["type"] == "INDEX" and item.get("table") in stale_tables)
    ]
    for item in stale_constraints:
        cleanup.append(item)
    for item in stale_table_foreign_keys:
        cleanup.append(item)

    stale_names = {item["name"] for item in cleanup}
    for name in stale_names:
        blockers.extend(f"{name}: {reason}" for reason in object_blockers.get(name, []))
    blockers = sorted(set(blockers + type_definition_blockers))
    cleanup_by_key = {(item["type"], item["name"]): item for item in cleanup}
    ordered_keys: list[tuple[str, str]] = []
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(key: tuple[str, str]) -> None:
        if key in visited:
            return
        if key in visiting:
            blockers.append(f"dependency cycle prevents safe cleanup at {key[0]} {key[1]}")
            return
        visiting.add(key)
        for dependency in sorted(dependency_edges.get(key, set())):
            if dependency in cleanup_by_key:
                visit(dependency)
        visiting.remove(key)
        visited.add(key)
        ordered_keys.append(key)

    for key in sorted(cleanup_by_key, key=lambda item: (_DROP_ORDER.get(item[0], 99), item[1])):
        visit(key)
    cleanup = [cleanup_by_key[key] for key in ordered_keys]
    blockers = sorted(set(blockers))
    source_signature = {
        "tables": [
            {
                "name": schema.name,
                "columns": [
                    (column.name, str(column.source_type), str(column.target_type),
                     bool(column.nullable), bool(column.is_identity), str(column.identity_kind),
                     str(column.generated_kind), str(column.generated), str(column.default))
                    for column in schema.columns
                ],
                "oracle_object_types": schema.options.get("oracle_object_types", {}),
                "primary_key": list(schema.primary_key),
                "unique": [(item.name, list(item.columns)) for item in schema.unique_constraints],
                "foreign_keys": [
                    (item.name, list(item.columns), item.ref_schema, item.ref_table,
                     list(item.ref_columns), item.on_delete)
                    for item in schema.foreign_keys
                ],
                "checks": [(item.name, item.expression) for item in schema.check_constraints],
                "indexes": [
                    (item.name, list(item.columns), bool(item.unique), item.index_type,
                     list(item.descending_columns))
                    for item in schema.indexes
                ],
            }
            for schema in schemas
        ],
        "views": [(item.name, item.definition) for item in views],
        "materialized_views": [
            (item.name, item.definition, item.build_mode, item.refresh_mode,
             item.refresh_method, item.rewrite_enabled) for item in (materialized_views or [])
        ],
        "synonyms": [
            (item.name, item.base_owner, item.base_name, item.db_link, bool(item.is_public))
            for item in (synonyms or [])
        ],
        "comments": sorted(
            (item.object_type, item.object_name, item.column_name, item.comment)
            for item in (comments or [])
        ),
        "partitioning": [
            (schema.name, schema.options.get("oracle_partitioning"))
            for schema in schemas if schema.options.get("oracle_partitioning")
        ],
        "sequences": [
            (item.name, item.start_value, item.increment, item.min_value, item.max_value,
             bool(item.cycle), item.cache_size, item.options)
            for item in sequences
        ],
        "routines": [(item.name, item.kind, item.ddl) for item in routines],
        "types": [(item.name, item.kind, item.schema, item.ddl) for item in (types or [])],
        "triggers": [(item.name, item.table, item.ddl, bool(item.is_disabled)) for item in triggers],
    }
    plan_payload = {
        "owner": owner.upper(),
        "preservation_policy": "source_managed" if preserve_target_only else "owner_all",
        "source": sorted(expected),
        "source_signature": source_signature,
        "target": sorted((item["type"], item["name"]) for item in inventory),
        "target_constraints": sorted((item["name"], item["table"]) for item in target_constraints),
        "target_partitioning": sorted(
            (table, metadata) for table, metadata in target_partitioning.items()
        ),
        "parent_managed": sorted(
            (item["type"], item["name"], item["table"], item["managed_by"])
            for item in parent_managed
        ),
        "cleanup": cleanup,
        "destructive_schema_changes": destructive_schema_changes,
        "implicit_cleanup": implicit_cleanup,
        "blockers": blockers,
        "inspection_errors": inspection_errors,
        "dependency_impact": dependency_impact,
        "grants_impact": grants_impact,
        "preserved_target_only": [
            item for item in inventory
            if (item["type"], item["name"]) not in expected
            and (item["type"], item["name"]) not in stale_keys
        ],
        "mviews_requiring_revalidation": [
            {"type": "MATERIALIZED VIEW", "name": item["name"]}
            for item in inventory
            if item["type"] == "MATERIALIZED VIEW"
            and item.get("mview_status") == "INVALID"
            and item["name"] in source_materialized_views
        ],
    }
    plan_id = hashlib.sha256(
        json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20].upper()
    return ReplacementPlan(
        owner=owner.upper(), plan_id=plan_id,
        inventory=tuple([*inventory, *target_constraints, *parent_managed]),
        cleanup=tuple(cleanup), implicit_cleanup=tuple(implicit_cleanup), blockers=tuple(blockers),
        inspection_errors=tuple(inspection_errors),
        dependency_impact=tuple(dependency_impact), grants_impact=tuple(grants_impact),
        source_inventory=tuple({"type": kind, "name": name} for kind, name in sorted(expected)),
        missing=tuple({"type": kind, "name": name} for kind, name in missing),
        source_metadata=tuple(
            [
                {"type": f"{item.object_type.upper()} COMMENT", "name": item.object_name,
                 "column": str(item.column_name or ""), "value": item.comment}
                for item in (comments or [])
            ]
            + [
                {"type": "SYNONYM TARGET", "name": item.name,
                 "target_owner": str(item.base_owner or ""),
                 "target_name": str(item.base_name or ""),
                 "database_link": str(item.db_link or "")}
                for item in (synonyms or [])
            ]
            + [
                {"type": f"{schema.partition_method} PARTITIONING", "name": schema.name,
                 "keys": ",".join(schema.options["oracle_partitioning"]["keys"]),
                 "partition_count": str(len(schema.options["oracle_partitioning"]["partitions"]))}
                for schema in schemas if schema.options.get("oracle_partitioning")
            ]
            + [
                {"type": f"{schema.partition_method} PARTITION", "name": f"{schema.name}.{part['name']}",
                 "position": str(part["position"]), "high_value": part["high_value"]}
                for schema in schemas if schema.options.get("oracle_partitioning")
                for part in schema.options["oracle_partitioning"]["partitions"]
            ]
        ),
        preserved_target_only=tuple(
            dict(item) for item in inventory
            if (item["type"], item["name"]) not in expected
            and (item["type"], item["name"]) not in stale_keys
        ),
        mviews_requiring_revalidation=tuple(
            {"type": "MATERIALIZED VIEW", "name": item["name"]}
            for item in inventory
            if item["type"] == "MATERIALIZED VIEW"
            and item.get("mview_status") == "INVALID"
            and item["name"] in source_materialized_views
        ),
        destructive_schema_changes=tuple(destructive_schema_changes),
    )


def apply_plan(conn: Any, plan: ReplacementPlan) -> list[str]:
    """Apply explicit owner-local DROP statements after preflight succeeds."""
    if plan.blockers or plan.inspection_errors:
        raise RuntimeError("Oracle schema replacement plan has blockers or incomplete inspection and cannot be applied")
    if not plan.cleanup:
        return []
    supported = {"TABLE", "VIEW", "MATERIALIZED VIEW", "SYNONYM", "SEQUENCE", "TRIGGER", "FUNCTION", "PROCEDURE", "PACKAGE", "INDEX", "CONSTRAINT", "TYPE"}
    for item in plan.cleanup:
        kind, name = item["type"], item["name"]
        if kind not in supported:
            raise RuntimeError(f"Unsupported Oracle cleanup type {kind}")
        validate_oracle_identifier(name, f"{kind.lower()} name")
        if kind == "CONSTRAINT":
            validate_oracle_identifier(item.get("table"), "constraint table")
    executed: list[str] = []
    try:
        with conn.cursor() as cur:
            for item in plan.cleanup:
                kind, name = item["type"], item["name"]
                if kind == "CONSTRAINT":
                    table = item["table"]
                    statement = f"ALTER TABLE {quote_identifier(table)} DROP CONSTRAINT {quote_identifier(name)}"
                else:
                    statement = f"DROP {kind} {quote_identifier(name)}"
                cur.execute(statement)
                executed.append(statement)
    except Exception as exc:
        progress = "; ".join(executed) or "none"
        raise RuntimeError(
            f"Oracle schema replacement cleanup stopped after DDL [{progress}]: {exc}"
        ) from None
    conn.commit()
    return executed
