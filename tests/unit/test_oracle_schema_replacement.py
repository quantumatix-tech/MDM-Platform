"""Focused tests for Oracle owner-scoped schema replacement planning."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    CheckConstraint,
    Column,
    Index,
    MaterializedViewDef,
    Schema,
    ViewDefinition,
)
from core.connectors.oracle.replacement import (
    ReplacementPlan,
    apply_plan,
    build_plan,
    overwrite_authorization,
)
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import (
    OracleTargetConnector,
    _oracle_constraint_matches,
    _oracle_constraint_name,
)


def _source_tab_grant(grantee, privilege, *, owner="MIGRATIONE2E_ORACLE_SOURCE",
                      table="SOURCE_OBJECT", kind="TABLE"):
    return (grantee, owner, table, owner, privilege, "NO", "NO", "NO", kind, "NO")


def _source_col_grant(grantee, privilege, *, owner="MIGRATIONE2E_ORACLE_SOURCE",
                      table="SOURCE_OBJECT", column="SOURCE_COLUMN"):
    return (grantee, owner, table, column, owner, privilege, "NO", "NO", "NO")


def _inventory_connection(*, user="TARGET_OWNER", objects=None, indexes=None,
                          constraints=None, grants=None, dependencies=None,
                          external_fks=None, identity_sequences=None,
                          inspection_failures=None, local_dependencies=None,
                          table_names=None, lob_indexes=None, partitioning=None,
                          partition_objects=None, mview_rows=None, column_grants=None,
                          framework_provenance=False, index_expression_rows=None,
                          index_column_type_rows=None, table_columns=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    objects = objects if objects is not None else [
        ("KEEP_T", "TABLE"), ("OLD_T", "TABLE"),
        ("V_OLD", "VIEW"), ("IX_OLD", "INDEX"),
    ]
    indexes = indexes if indexes is not None else [
        ("IX_OLD", "OLD_T", None, "NORMAL", "VALID", "NO", "N")
    ]
    constraints = constraints if constraints is not None else []
    grants = grants if grants is not None else []
    column_grants = column_grants if column_grants is not None else []
    dependencies = dependencies if dependencies is not None else []
    local_dependencies = local_dependencies if local_dependencies is not None else []
    external_fks = external_fks if external_fks is not None else []
    identity_sequences = identity_sequences if identity_sequences is not None else []
    inspection_failures = inspection_failures or set()
    table_names = table_names if table_names is not None else ["KEEP_T", "OLD_T"]
    table_columns = table_columns if table_columns is not None else {
        name: ["ID"] for name in table_names
    }
    lob_indexes = lob_indexes if lob_indexes is not None else []
    partitioning = partitioning or {}
    if partition_objects is None:
        partition_objects = [
            (table, part["name"])
            for table, metadata in partitioning.items()
            for part in metadata.get("partitions", [])
        ]
    lob_index_names = {
        item[0] if isinstance(item, tuple) else item for item in lob_indexes
    }

    def fetchall_for_sql():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "FROM USER_PART_KEY_COLUMNS" in sql:
            table = cur.execute.call_args.args[1][0]
            metadata = partitioning.get(table)
            return [(key,) for key in metadata["keys"]] if metadata else []
        if "FROM USER_TAB_PARTITIONS" in sql:
            table = cur.execute.call_args.args[1][0]
            metadata = partitioning.get(table)
            if not metadata:
                return []
            return [
                (part["name"], part["position"], part["high_value"],
                 "NO", "NO", "NO", "NO", "ON")
                for part in metadata["partitions"]
            ]
        if "SELECT USER FROM DUAL" in sql:
            return [(user,)]
        if "FROM USER_TABLES" in sql and "NESTED = 'NO'" in sql:
            return [(name,) for name in table_names]
        if "FROM USER_TABLES" in sql and "TABLE_NAME=:1" in sql:
            return [("DMT_PERMISSION_PROVENANCE",)] if framework_provenance else []
        if "FROM USER_TAB_COLUMNS" in sql and "TABLE_NAME=:1" in sql:
            table = cur.execute.call_args.args[1][0]
            if table != "DMT_PERMISSION_PROVENANCE":
                return []
            return [(name, "VARCHAR2") for name in (
                "ITEM_KEY", "KIND", "OBJECT_NAME", "COLUMN_NAME",
                "PRIVILEGE", "GRANTEE", "STATE",
            )] if framework_provenance else []
        if "FROM USER_MVIEWS" in sql:
            if mview_rows is not None:
                return mview_rows
            return [
                (name, "IMMEDIATE", "DEMAND", "COMPLETE", "N", "VALID",
                 "SELECT 1 X FROM DUAL")
                for name, kind in objects if kind == "MATERIALIZED VIEW"
            ]
        if "JOIN USER_TAB_IDENTITY_COLS" in sql:
            return identity_sequences
        if "SELECT INDEX_NAME, TABLE_NAME, COLUMN_NAME FROM USER_LOBS" in sql:
            return [
                tuple(name) if isinstance(name, tuple)
                else (name, "KEEP_T", "LOB_VALUE")
                for name in lob_indexes
            ]
        if "FROM USER_OBJECTS" in sql:
            rows = []
            for item in objects:
                name, kind = item[:2]
                if str(kind).upper() != "TABLE PARTITION":
                    rows.append((name, kind, None))
            rows.extend((parent, "TABLE PARTITION", sub) for parent, sub in partition_objects)
            return rows
        if "FROM USER_INDEXES" in sql:
            if "IE.COLUMN_EXPRESSION" in sql:
                return index_expression_rows or []
            if "FROM USER_LOBS" in sql:
                return [row for row in indexes if row[0] not in lob_index_names]
            return indexes
        if "FROM USER_CONSTRAINTS" in sql:
            return constraints
        if "FROM ALL_TAB_PRIVS_MADE" in sql:
            if "ALL_TAB_PRIVS_MADE" in inspection_failures:
                raise PermissionError("ORA-00942: table or view does not exist")
            return [
                (grantee, user, name, user, privilege, "NO", "NO", "NO", kind, "NO")
                for name, grantee, privilege, kind in grants
            ]
        if "FROM ALL_COL_PRIVS_MADE" in sql:
            if "ALL_COL_PRIVS_MADE" in inspection_failures:
                raise PermissionError("ORA-00942: table or view does not exist")
            return [
                (grantee, user, name, column, user, privilege, "NO", "NO", "NO")
                for name, column, grantee, privilege in column_grants
            ]
        if "FROM DBA_DEPENDENCIES" in sql:
            if "DBA_DEPENDENCIES" in inspection_failures:
                raise PermissionError("ORA-00942: table or view does not exist")
            return dependencies
        if "FROM DBA_CONSTRAINTS" in sql:
            if "DBA_CONSTRAINTS" in inspection_failures:
                raise PermissionError("ORA-00942: table or view does not exist")
            return external_fks
        if "SELECT COLUMN_NAME FROM USER_TAB_COLS" in sql:
            if "USER_TAB_COLS" in inspection_failures:
                raise PermissionError("ORA-00942: table or view does not exist")
            table = cur.execute.call_args.args[1][0]
            return [(name,) for name in table_columns.get(table, [])]
        if "FROM USER_TAB_COLS" in sql and "HIDDEN_COLUMN = 'NO'" in sql:
            return index_column_type_rows or []
        if "FROM USER_TAB_COLS" in sql:
            return []
        if "FROM USER_TAB_COMMENTS" in sql or "FROM USER_COL_COMMENTS" in sql:
            return []
        if "FROM USER_DEPENDENCIES" in sql:
            return local_dependencies
        raise AssertionError(f"Unexpected metadata query: {sql}")

    cur.fetchall.side_effect = fetchall_for_sql

    def fetchone_for_sql():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "FROM USER_PART_TABLES" not in sql:
            return None
        table = cur.execute.call_args.args[1][0]
        metadata = partitioning.get(table)
        if not metadata:
            return None
        return (
            metadata["method"], "NONE", None, "NO", "NO", None,
            "VALID", len(metadata["partitions"]),
        )

    cur.fetchone.side_effect = fetchone_for_sql
    return conn, cur


def _plan(conn, *, views=None, schemas=None, tables=None):
    schemas = schemas if schemas is not None else [
        Schema("KEEP_T", columns=[Column("ID", "NUMBER")])
    ]
    return build_plan(
        conn, "TARGET_OWNER", tables=tables if tables is not None else {"KEEP_T"},
        schemas=schemas, views=views or [],
        sequences=[], routines=[], triggers=[], preserve_target_only=True,
    )


def _partitioned_schema(table, method, parts):
    return Schema(
        table, columns=[Column("PART_KEY", "NUMBER")],
        partition_method=method,
        options={"oracle_partitioning": {
            "method": method, "keys": ["PART_KEY"], "partitions": parts,
        }},
    )


def _replacement_plan(
    *, inventory=(), cleanup=(), plan_id="PLAN-EXACT", destructive_schema_changes=()
):
    return ReplacementPlan(
        owner="TARGET_OWNER",
        plan_id=plan_id,
        inventory=tuple(inventory),
        cleanup=tuple(cleanup),
        implicit_cleanup=(),
        blockers=(),
        inspection_errors=(),
        dependency_impact=(),
        grants_impact=(),
        source_inventory=(),
        destructive_schema_changes=tuple(destructive_schema_changes),
    )


def test_empty_target_does_not_require_overwrite_approval():
    result = overwrite_authorization(
        _replacement_plan(),
        source_inventory={"schemas": [Schema("CUSTOMERS")]},
        policy={},
    )

    assert result["status"] == "not_required"
    assert result["requires_explicit_approval"] is False


@pytest.mark.parametrize(
    "policy,expected_status",
    [
        ({}, "blocked"),
        ({"enabled": True}, "enabled"),
        ({"enabled": True, "approval_id": "OTHER"}, "enabled"),
        ({"enabled": True, "approval_id": "PLAN-EXACT"}, "enabled"),
    ],
)
def test_existing_source_managed_tables_use_stable_policy_without_plan_approval(
    policy, expected_status
):
    result = overwrite_authorization(
        _replacement_plan(inventory=({"type": "TABLE", "name": "CUSTOMERS"},)),
        source_inventory={"schemas": [Schema("CUSTOMERS")]},
        policy=policy,
    )

    assert result["status"] == expected_status
    assert result["requires_explicit_approval"] is False
    assert result["replacement_policy_enabled"] is (policy.get("enabled") is True)
    assert result["overwrite_source_managed_tables"] == ["CUSTOMERS"]


def test_stale_cleanup_requires_exact_approval_even_without_matching_table():
    result = overwrite_authorization(
        _replacement_plan(cleanup=({"type": "VIEW", "name": "STALE_VIEW"},)),
        source_inventory={"schemas": [Schema("CUSTOMERS")]},
        policy={"enabled": True, "approval_id": "PLAN-EXACT"},
    )

    assert result["status"] == "approved"
    assert result["requires_explicit_approval"] is True
    assert result["cleanup_objects"] == [{"type": "VIEW", "name": "STALE_VIEW"}]


def test_schema_cleanup_with_missing_plan_id_remains_blocked():
    result = overwrite_authorization(
        _replacement_plan(
            inventory=({"type": "TABLE", "name": "CUSTOMERS"},),
            plan_id="",
            cleanup=({"type": "VIEW", "name": "STALE_VIEW"},),
        ),
        source_inventory={"schemas": [Schema("CUSTOMERS")]},
        policy={"enabled": True, "approval_id": ""},
    )

    assert result["status"] == "blocked"
    assert result["requires_explicit_approval"] is True
    assert "approval_id must match this exact" in result["reason"]


def test_target_only_column_drop_is_in_plan_and_requires_exact_approval():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE")], table_names=["KEEP_T"],
        table_columns={"KEEP_T": ["ID", "TARGET_ONLY_VALUE"]},
    )
    plan = _plan(conn)

    assert plan.cleanup == ()
    assert plan.destructive_schema_changes == ({
        "type": "DROP_COLUMN", "table": "KEEP_T", "name": "TARGET_ONLY_VALUE",
    },)
    assert plan.as_dict()["destructive_ddl_count"] == 1
    assert overwrite_authorization(
        plan, source_inventory={"schemas": [Schema("KEEP_T")]},
        policy={"enabled": True},
    )["status"] == "blocked"
    approved = overwrite_authorization(
        plan, source_inventory={"schemas": [Schema("KEEP_T")]},
        policy={"enabled": True, "approval_id": plan.plan_id},
    )
    assert approved["status"] == "approved"
    assert approved["requires_explicit_approval"] is True


def test_target_column_inventory_failure_marks_replacement_plan_incomplete():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE")], table_names=["KEEP_T"],
        inspection_failures={"USER_TAB_COLS"},
    )

    plan = _plan(conn)

    assert any("USER_TAB_COLS" in error for error in plan.inspection_errors)
    assert plan.as_dict()["status"] == "inspection_incomplete"


@pytest.mark.parametrize(
    "method,parts",
    [
        ("RANGE", [
            {"name": "P_LOW", "position": 1, "high_value": "10"},
            {"name": "P_HIGH", "position": 2, "high_value": "MAXVALUE"},
        ]),
        ("LIST", [
            {"name": "P_NORTH", "position": 1, "high_value": "('NORTH','SOUTH')"},
            {"name": "P_DEFAULT", "position": 2, "high_value": "DEFAULT"},
        ]),
    ],
)
def test_replacement_verification_accepts_native_partition_children(method, parts):
    table = f"S39_{method}"
    schema = _partitioned_schema(table, method, parts)
    conn, _ = _inventory_connection(
        objects=[(table, "TABLE"), *((table, "TABLE PARTITION") for _ in parts)],
        table_names=[table], partitioning={table: schema.options["oracle_partitioning"]},
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn
    target.verify_replacement_table_definitions = lambda _schemas: None

    report = target.verify_schema_replacement(
        tables={table}, schemas=[schema], views=[], materialized_views=[],
        sequences=[], routines=[], triggers=[], synonyms=[], comments=[],
    )

    assert report["status"] == "verified"
    assert not any(
        item["type"] == "TABLE PARTITION" for item in report["cleanup"]
    )


@pytest.mark.parametrize(
    "target_parts",
    [
        [{"name": "P_LOW", "position": 1, "high_value": "20"}],
        [
            {"name": "P_LOW", "position": 1, "high_value": "10"},
            {"name": "P_UNEXPECTED", "position": 2, "high_value": "MAXVALUE"},
        ],
    ],
)
def test_replacement_verification_blocks_unexpected_partition_name_or_boundary(target_parts):
    table = "S39_RANGE"
    expected_parts = [{"name": "P_LOW", "position": 1, "high_value": "10"}]
    target_partitioning = {
        "method": "RANGE", "keys": ["PART_KEY"],
        "partitions": target_parts,
    }
    schema = _partitioned_schema(table, "RANGE", expected_parts)
    conn, _ = _inventory_connection(
        objects=[(table, "TABLE"),
                 *((table, "TABLE PARTITION") for _ in target_parts)],
        table_names=[table], partitioning={table: target_partitioning},
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn
    target.verify_replacement_table_definitions = lambda _schemas: None

    with pytest.raises(RuntimeError, match="partition metadata does not match source"):
        target.verify_schema_replacement(
            tables={table}, schemas=[schema], views=[], materialized_views=[],
            sequences=[], routines=[], triggers=[], synonyms=[], comments=[],
        )


def test_target_only_partitioned_table_is_planned_as_parent_and_implicit_children():
    old_table = "OLD_PARTITIONED"
    metadata = {
        "method": "RANGE", "keys": ["PART_KEY"],
        "partitions": [{"name": "P_OLD", "position": 1, "high_value": "MAXVALUE"}],
    }
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), (old_table, "TABLE"),
                 (old_table, "TABLE PARTITION")],
        table_names=["KEEP_T", old_table], partitioning={old_table: metadata},
    )

    plan = _plan(conn)

    assert plan.cleanup == ()
    assert plan.implicit_cleanup == ()
    assert any(item["name"] == old_table for item in plan.preserved_target_only)
    assert plan.blockers == ()


def test_orphan_partition_object_is_rejected_instead_of_ignored():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("NO_PARENT", "TABLE PARTITION")],
        table_names=["KEEP_T"], partition_objects=[("NO_PARENT", "P_ORPHAN")],
    )

    with pytest.raises(RuntimeError, match="orphaned TABLE PARTITION"):
        _plan(conn)


def test_unsupported_target_partition_scheme_fails_closed():
    table = "TARGET_HASH"
    metadata = {
        "method": "HASH", "keys": ["PART_KEY"],
        "partitions": [{"name": "P1", "position": 1, "high_value": "MAXVALUE"}],
    }
    conn, _ = _inventory_connection(
        objects=[(table, "TABLE"), (table, "TABLE PARTITION")],
        table_names=[table], partitioning={table: metadata},
    )

    with pytest.raises(RuntimeError, match="cannot safely migrate partitioned table"):
        _plan(conn, schemas=[_partitioned_schema(table, "RANGE", metadata["partitions"])],
              tables={table})


def test_oracle_replacement_plan_preserves_target_only_owner_objects():
    conn, _ = _inventory_connection()

    plan = _plan(conn)

    assert plan.cleanup == ()
    assert plan.implicit_cleanup == ()
    assert {(item["type"], item["name"]) for item in plan.preserved_target_only} == {
        ("TABLE", "OLD_T"), ("VIEW", "V_OLD"), ("INDEX", "IX_OLD"),
    }
    assert plan.blockers == ()
    assert plan.as_dict()["destructive_ddl_count"] == 0
    assert plan.as_dict()["status"] == "ready"


def test_oracle_replacement_counts_mview_and_storage_table_once_as_native_mview():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("MV_TOTAL", "TABLE"),
                 ("MV_TOTAL", "MATERIALIZED VIEW")],
        table_names=["KEEP_T", "MV_TOTAL"],
    )
    mv = MaterializedViewDef("MV_TOTAL", "SELECT 1 X FROM DUAL", "TARGET_OWNER")

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"},
        schemas=[Schema("KEEP_T", columns=[Column("ID", "NUMBER")])],
        views=[], materialized_views=[mv], sequences=[], routines=[], triggers=[],
        preserve_target_only=True,
    )

    assert { (item["type"], item["name"]) for item in plan.source_inventory } >= {
        ("MATERIALIZED VIEW", "MV_TOTAL")
    }
    assert ("TABLE", "MV_TOTAL") not in {
        (item["type"], item["name"]) for item in plan.inventory
    }
    assert ("MATERIALIZED VIEW", "MV_TOTAL") in {
        (item["type"], item["name"]) for item in plan.inventory
    }
    assert not plan.blockers
    assert not plan.cleanup


def test_oracle_replacement_allows_matching_invalid_mview_for_pre_cleanup_revalidation():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("MV_TOTAL", "TABLE"),
                 ("MV_TOTAL", "MATERIALIZED VIEW")],
        table_names=["KEEP_T", "MV_TOTAL"],
        mview_rows=[(
            "MV_TOTAL", "IMMEDIATE", "DEMAND", "COMPLETE", "N", "INVALID",
            "SELECT 1 X FROM DUAL",
        )],
    )
    mv = MaterializedViewDef("MV_TOTAL", "SELECT 1 X FROM DUAL", "TARGET_OWNER")

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"},
        schemas=[Schema("KEEP_T", columns=[Column("ID", "NUMBER")])],
        views=[], materialized_views=[mv], sequences=[], routines=[], triggers=[],
        preserve_target_only=True,
    )

    assert not plan.blockers
    assert next(
        item for item in plan.inventory
        if item["type"] == "MATERIALIZED VIEW" and item["name"] == "MV_TOTAL"
    )["mview_status"] == "INVALID"
    assert plan.as_dict()["mviews_requiring_revalidation"] == [
        {"type": "MATERIALIZED VIEW", "name": "MV_TOTAL"}
    ]
    assert not any(
        item["type"] == "TABLE" and item["name"] == "MV_TOTAL"
        for item in plan.inventory
    )
    assert not plan.cleanup


def test_oracle_replacement_blocks_invalid_mview_with_mismatched_definition():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("MV_TOTAL", "TABLE"),
                 ("MV_TOTAL", "MATERIALIZED VIEW")],
        table_names=["KEEP_T", "MV_TOTAL"],
        mview_rows=[(
            "MV_TOTAL", "IMMEDIATE", "DEMAND", "COMPLETE", "N", "INVALID",
            "SELECT 2 X FROM DUAL",
        )],
    )
    mv = MaterializedViewDef("MV_TOTAL", "SELECT 1 X FROM DUAL", "TARGET_OWNER")

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"},
        schemas=[Schema("KEEP_T", columns=[Column("ID", "NUMBER")])],
        views=[], materialized_views=[mv], sequences=[], routines=[], triggers=[],
        preserve_target_only=True,
    )

    assert any("definition differs from source" in item for item in plan.blockers)
    assert not plan.cleanup


def test_oracle_replacement_blocks_real_table_instead_of_claiming_mv_storage():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("MV_TOTAL", "TABLE")],
        table_names=["KEEP_T", "MV_TOTAL"],
        mview_rows=[],
    )
    mv = MaterializedViewDef("MV_TOTAL", "SELECT 1 X FROM DUAL", "TARGET_OWNER")

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"},
        schemas=[Schema("KEEP_T", columns=[Column("ID", "NUMBER")])],
        views=[], materialized_views=[mv], sequences=[], routines=[], triggers=[],
        preserve_target_only=True,
    )

    assert any("conflicts with target TABLE" in item for item in plan.blockers)
    assert any(
        item["type"] == "TABLE" and item["name"] == "MV_TOTAL"
        for item in plan.preserved_target_only
    )
    assert not plan.cleanup


def test_oracle_replacement_preserves_existing_grants_when_source_table_replaces_target_view():
    conn, _ = _inventory_connection(
        objects=[("P8_S40_OBJECT", "VIEW")],
        table_names=[],
        grants=[("P8_S40_OBJECT", "KEEP_ROLE", "SELECT", "VIEW")],
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"P8_S40_OBJECT"},
        schemas=[Schema("P8_S40_OBJECT", columns=[Column("ID", "NUMBER")])],
        views=[], materialized_views=[], sequences=[], routines=[], triggers=[],
        preserve_target_only=True,
    )

    assert any("existing permissions are preserved" in item for item in plan.blockers)
    assert any(item["type"] == "VIEW" for item in plan.cleanup)
    with pytest.raises(RuntimeError, match="blockers or incomplete inspection"):
        apply_plan(conn, plan)
    assert not any(
        str(call.args[0]).upper().startswith("DROP")
        for call in conn.cursor.return_value.__enter__.return_value.execute.call_args_list
    )


def test_oracle_replacement_blocks_dropping_target_column_with_existing_column_grant():
    conn, _ = _inventory_connection(
        objects=[("P8_S40_OBJECT", "TABLE")],
        table_names=["P8_S40_OBJECT"],
        column_grants=[("P8_S40_OBJECT", "OLD_VALUE", "KEEP_ROLE", "UPDATE")],
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"P8_S40_OBJECT"},
        schemas=[Schema("P8_S40_OBJECT", columns=[Column("ID", "NUMBER")])],
        views=[], materialized_views=[], sequences=[], routines=[], triggers=[],
        preserve_target_only=True,
    )

    assert any("source-absent target column P8_S40_OBJECT.OLD_VALUE" in item for item in plan.blockers)
    with pytest.raises(RuntimeError, match="blockers or incomplete inspection"):
        apply_plan(conn, plan)


def test_oracle_replacement_excludes_internal_lob_indexes_but_keeps_user_indexes():
    conn, _ = _inventory_connection(
        objects=[
            ("KEEP_T", "TABLE"),
            ("OLD_T", "TABLE"),
            ("SYS_IL0000073849C00002$$", "INDEX"),
            ("IX_REAL", "INDEX"),
        ],
        indexes=[
            ("SYS_IL0000073849C00002$$", "OLD_T", None, "LOB", "VALID", "NO", "N"),
            ("IX_REAL", "KEEP_T", None, "NORMAL", "VALID", "NO", "N"),
        ],
        lob_indexes=[("SYS_IL0000073849C00002$$", "OLD_T", "LOB_VALUE")],
        table_names=["KEEP_T", "OLD_T"],
    )

    plan = _plan(conn)

    target_indexes = {
        item["name"] for item in plan.inventory if item["type"] == "INDEX"
    }
    assert target_indexes == {"IX_REAL"}
    assert any(item["name"] == "IX_REAL" for item in plan.preserved_target_only)
    assert not any(item["name"].startswith("SYS_IL") for item in plan.cleanup)
    assert plan.cleanup == ()
    assert plan.implicit_cleanup == ()
    assert any(
        item["type"] == "LOB INDEX" and item["name"] == "SYS_IL0000073849C00002$$"
        for item in plan.inventory
    )


def test_oracle_replacement_recognizes_intrinsic_tstz_function_index_as_supported():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("IX_TSTZ", "INDEX")],
        indexes=[("IX_TSTZ", "KEEP_T", None, "FUNCTION-BASED NORMAL", "VALID", "NO", "Y")],
        table_names=["KEEP_T"],
        index_expression_rows=[
            ("IX_TSTZ", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
             "ID", 1, "ASC", None),
            ("IX_TSTZ", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
             "SYS_NC00001$", 2, "ASC", 'SYS_EXTRACT_UTC("ORDER_DATE")'),
        ],
        index_column_type_rows=[
            ("KEEP_T", "ID", "NUMBER"),
            ("KEEP_T", "ORDER_DATE", "TIMESTAMP WITH TIME ZONE"),
        ],
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"},
        schemas=[Schema("KEEP_T", columns=[Column("ID", "NUMBER")])],
        views=[], sequences=[], routines=[], triggers=[], preserve_target_only=True,
    )

    target_index = next(item for item in plan.inventory if item["name"] == "IX_TSTZ")
    assert target_index["index_type"] == "NORMAL"
    assert target_index["functional"] is False
    assert target_index["columns"] == ["ID", "ORDER_DATE"]
    assert not any("unsupported or unsafe target-only index IX_TSTZ" in item for item in plan.blockers)


def test_oracle_replacement_still_blocks_genuine_target_function_index():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("IX_EXPR", "INDEX")],
        indexes=[("IX_EXPR", "KEEP_T", None, "FUNCTION-BASED NORMAL", "VALID", "NO", "Y")],
        table_names=["KEEP_T"],
        index_expression_rows=[(
            "IX_EXPR", "NONUNIQUE", "FUNCTION-BASED NORMAL", "VALID", "NO",
            "SYS_NC00001$", 1, "ASC", 'UPPER("ID")',
        )],
        index_column_type_rows=[("KEEP_T", "ID", "NUMBER")],
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"},
        schemas=[Schema(
            "KEEP_T", columns=[Column("ID", "NUMBER")],
            indexes=[Index("IX_EXPR", ["ID"], index_type="NORMAL")],
        )],
        views=[], sequences=[], routines=[], triggers=[], preserve_target_only=True,
    )

    assert any("source-managed target index IX_EXPR is unsupported" in item for item in plan.blockers)


def test_oracle_full_preserves_target_only_table_and_index_while_retaining_source_view():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("OLD_T", "TABLE"), ("V_SOURCE", "VIEW"), ("IX_OLD", "INDEX")],
        indexes=[("IX_OLD", "OLD_T", None, "NORMAL", "VALID", "NO", "N")],
        local_dependencies=[("V_SOURCE", "VIEW", "TARGET_OWNER", "KEEP_T", "TABLE")],
    )

    plan = _plan(
        conn,
        views=[ViewDefinition("V_SOURCE", "CREATE OR REPLACE VIEW V_SOURCE AS SELECT 1 ID FROM DUAL")],
    )

    assert plan.cleanup == ()
    assert plan.implicit_cleanup == ()
    assert {item["name"] for item in plan.preserved_target_only} == {"OLD_T", "IX_OLD"}
    assert not plan.blockers


def test_oracle_full_preserves_extra_target_index_on_managed_table_and_reports_missing_source_index():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("IX_TARGET_EXTRA", "INDEX")],
        indexes=[("IX_TARGET_EXTRA", "KEEP_T", None, "NORMAL", "VALID", "NO", "N")],
        table_names=["KEEP_T"],
    )
    schema = Schema(
        "KEEP_T", columns=[Column("ID", "NUMBER"), Column("SOURCE_COL", "NUMBER")],
        indexes=[Index("IX_SOURCE", ["SOURCE_COL"], index_type="NORMAL")],
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"}, schemas=[schema], views=[],
        sequences=[], routines=[], triggers=[], preserve_target_only=True,
    )

    assert plan.cleanup == ()
    assert any(
        item["type"] == "INDEX" and item["name"] == "IX_TARGET_EXTRA"
        and item["table"] == "KEEP_T"
        for item in plan.preserved_target_only
    )
    assert { (item["type"], item["name"]) for item in plan.missing } >= {
        ("INDEX", "IX_SOURCE")
    }


def test_oracle_full_blocks_target_table_conflicting_with_source_object_name():
    conn, cur = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("V_SOURCE", "TABLE")],
        indexes=[], table_names=["KEEP_T", "V_SOURCE"],
    )

    plan = _plan(
        conn, views=[ViewDefinition(
            "V_SOURCE", "CREATE OR REPLACE VIEW V_SOURCE AS SELECT 1 ID FROM DUAL"
        )],
    )

    assert any("conflicts with target TABLE" in item for item in plan.blockers)
    assert not plan.cleanup
    with pytest.raises(RuntimeError, match="has blockers"):
        apply_plan(conn, plan)
    assert not any(str(call.args[0]).lstrip().upper().startswith(("DROP", "ALTER"))
                   for call in cur.execute.call_args_list)


def test_oracle_full_blocks_cleanup_conflict_with_target_only_dependent():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("V_SOURCE", "VIEW")],
        indexes=[], local_dependencies=[
            ("V_TARGET_ONLY", "VIEW", "TARGET_OWNER", "V_SOURCE", "VIEW"),
        ],
    )

    plan = _plan(conn, tables={"KEEP_T", "V_SOURCE"})

    assert any("target-only dependent VIEW V_TARGET_ONLY" in item for item in plan.blockers)
    assert [(item["type"], item["name"]) for item in plan.cleanup] == [
        ("VIEW", "V_SOURCE"),
    ]


def test_oracle_replacement_matches_generated_not_null_by_structure_not_name():
    conn, _ = _inventory_connection(
        objects=[("CUSTOMERS", "TABLE")], indexes=[],
        constraints=[("CUSTOMERS", "SYS_C008743", "C", None, None, "GENERATED NAME")],
        table_names=["CUSTOMERS"],
    )
    schema = Schema(
        "CUSTOMERS",
        columns=[Column("CUSTOMER_NAME", "VARCHAR2", nullable=False)],
        check_constraints=[CheckConstraint(
            "SYS_C008715", '"CUSTOMER_NAME" IS NOT NULL'
        )],
        options={"oracle_generated_check_constraints": {"SYS_C008715"}},
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"CUSTOMERS"}, schemas=[schema], views=[],
        sequences=[], routines=[], triggers=[],
    )

    assert plan.missing == ()
    assert not any(
        item["type"] == "CONSTRAINT" and item["name"].startswith("DMS_CK_")
        for item in plan.source_inventory
    )


def test_oracle_replacement_accepts_equivalent_generated_name_primary_key(monkeypatch):
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE")], indexes=[],
        constraints=[("KEEP_T", "SYS_C009999", "P", None, None, "GENERATED NAME")],
        table_names=["KEEP_T"],
    )
    schema = Schema(
        "KEEP_T", columns=[Column("ID", "NUMBER")],
        primary_key=["ID"], primary_key_name="SOURCE_PK",
    )
    matched: list[tuple[str, str, list[str]]] = []

    def matches(_cursor, name, table, kind, columns):
        matched.append((name, table, columns))
        return True

    monkeypatch.setattr(
        "core.connectors.oracle.target._oracle_constraint_matches", matches
    )
    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"}, schemas=[schema], views=[],
        sequences=[], routines=[], triggers=[],
    )

    assert plan.missing == ()
    assert matched == [(
        _oracle_constraint_name("KEEP_T", "PK", "SOURCE_PK"), "KEEP_T", ["ID"]
    )]
    # Generated constraint names remain excluded from the replacement inventory.
    assert not any(item["name"] == "SYS_C009999" for item in plan.inventory)


def test_oracle_replacement_repeated_verification_is_stable_with_equivalent_pk(monkeypatch):
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE")], indexes=[],
        constraints=[("KEEP_T", "SYS_C009999", "P", None, None, "GENERATED NAME")],
        table_names=["KEEP_T"],
    )
    schema = Schema(
        "KEEP_T", columns=[Column("ID", "NUMBER")],
        primary_key=["ID"], primary_key_name="SOURCE_PK",
    )
    monkeypatch.setattr(
        "core.connectors.oracle.target._oracle_constraint_matches",
        lambda *_args: True,
    )
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn
    # Exercise the real replacement inventory verification; the separate
    # table-definition verifier has its own existing constraint tests.
    target.verify_replacement_table_definitions = MagicMock()
    source_inventory = {
        "tables": {"KEEP_T"}, "schemas": [schema], "views": [],
        "sequences": [], "routines": [], "triggers": [],
    }

    first = target.verify_schema_replacement(**source_inventory)
    second = target.verify_schema_replacement(**source_inventory)

    assert first["status"] == second["status"] == "verified"
    assert first["missing_source_objects"] == second["missing_source_objects"] == []
    assert first["plan_id"] == second["plan_id"]


def test_read_only_post_migration_verification_does_not_require_dba_dependency_catalogs(
    monkeypatch,
):
    conn, cursor = _inventory_connection(
        objects=[("KEEP_T", "TABLE")], indexes=[],
        constraints=[("KEEP_T", "FK_OTHER", "R", "OTHER_OWNER", "PK_OTHER", "USER NAME")],
        table_names=["KEEP_T"],
        inspection_failures={"DBA_DEPENDENCIES", "DBA_CONSTRAINTS"},
    )
    schema = Schema("KEEP_T", columns=[Column("ID", "NUMBER")])
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn
    target.verify_replacement_table_definitions = lambda _schemas: None

    report = target.verify_schema_replacement(
        tables={"KEEP_T"}, schemas=[schema], views=[], materialized_views=[],
        sequences=[], routines=[], triggers=[], synonyms=[], comments=[],
    )

    assert report["status"] == "verified"
    statements = [str(call.args[0]).upper() for call in cursor.execute.call_args_list]
    assert not any("FROM DBA_DEPENDENCIES" in sql for sql in statements)
    assert not any("FROM DBA_CONSTRAINTS" in sql for sql in statements)


def test_destructive_replacement_preflight_still_fails_closed_without_dba_catalogs():
    conn, _cursor = _inventory_connection(
        objects=[("KEEP_T", "TABLE")], indexes=[],
        constraints=[("KEEP_T", "FK_OTHER", "R", "OTHER_OWNER", "PK_OTHER", "USER NAME")],
        table_names=["KEEP_T"],
        inspection_failures={"DBA_DEPENDENCIES", "DBA_CONSTRAINTS"},
    )

    plan = _plan(conn)

    assert len(plan.inspection_errors) == 2
    assert any("DBA_DEPENDENCIES" in item for item in plan.inspection_errors)
    assert any("DBA_CONSTRAINTS" in item for item in plan.inspection_errors)


@pytest.mark.parametrize(
    ("candidate", "candidate_columns", "expected_columns", "expected"),
    [
        (("KEEP_T", "P", None, None, "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"), ["ID"], ["ID"], True),
        (("OTHER_T", "P", None, None, "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"), ["ID"], ["ID"], False),
        (("KEEP_T", "P", None, None, "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"), ["OTHER_ID"], ["ID"], False),
        (("KEEP_T", "P", None, None, "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"), ["SEQ", "ID"], ["ID", "SEQ"], False),
    ],
)
def test_oracle_pk_matcher_requires_same_table_and_ordered_columns(
    candidate, candidate_columns, expected_columns, expected,
):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [None, candidate]
    cursor.fetchall.side_effect = [[("SYS_C009999",)], [(column,) for column in candidate_columns]]

    with conn.cursor() as cur:
        matched = _oracle_constraint_matches(
            cur, "DMS_PK_EXPECTED", "KEEP_T", "P", expected_columns
        )

    assert matched is expected


def test_oracle_pk_matcher_rejects_invalid_state_and_genuinely_missing_pk():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [
        None,
        ("KEEP_T", "P", None, None, "ENABLED", "NOT VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"),
    ]
    cursor.fetchall.side_effect = [[("SYS_C009999",)]]
    with conn.cursor() as cur, pytest.raises(RuntimeError, match="non-default state"):
        _oracle_constraint_matches(cur, "DMS_PK_EXPECTED", "KEEP_T", "P", ["ID"])

    missing_conn = MagicMock()
    missing_cursor = missing_conn.cursor.return_value.__enter__.return_value
    missing_cursor.fetchone.return_value = None
    missing_cursor.fetchall.return_value = []
    with missing_conn.cursor() as cur:
        assert not _oracle_constraint_matches(
            cur, "DMS_PK_EXPECTED", "KEEP_T", "P", ["ID"]
        )


def test_oracle_replacement_keeps_explicit_not_null_check_strictly_named():
    conn, _ = _inventory_connection(
        objects=[("CUSTOMERS", "TABLE")], indexes=[], table_names=["CUSTOMERS"],
    )
    schema = Schema(
        "CUSTOMERS",
        columns=[Column("CUSTOMER_NAME", "VARCHAR2", nullable=False)],
        check_constraints=[CheckConstraint(
            "CK_CUSTOMER_NAME", '"CUSTOMER_NAME" IS NOT NULL'
        )],
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"CUSTOMERS"}, schemas=[schema], views=[],
        sequences=[], routines=[], triggers=[],
    )

    expected_name = _oracle_constraint_name("CUSTOMERS", "CK", "CK_CUSTOMER_NAME")
    assert {item["name"] for item in plan.missing} == {expected_name}


def test_oracle_generated_not_null_verification_uses_equivalent_condition_and_state():
    cursor = MagicMock()
    cursor.fetchall.return_value = [(
        '"CUSTOMER_NAME" IS NOT NULL', "ENABLED", "VALIDATED",
        "NOT DEFERRABLE", "IMMEDIATE",
    )]

    OracleTargetConnector._verify_generated_not_null_check(
        cursor, "CUSTOMERS", "CUSTOMER_NAME"
    )

    assert "TABLE_NAME = :1" in cursor.execute.call_args.args[0].upper()
    assert cursor.execute.call_args.args[1] == ("CUSTOMERS",)


def test_oracle_generated_not_null_verification_rejects_unsupported_state():
    cursor = MagicMock()
    cursor.fetchall.return_value = [(
        '"CUSTOMER_NAME" IS NOT NULL', "ENABLED", "NOT VALIDATED",
        "NOT DEFERRABLE", "IMMEDIATE",
    )]

    with pytest.raises(RuntimeError, match="unsupported NOT NULL constraint state"):
        OracleTargetConnector._verify_generated_not_null_check(
            cursor, "CUSTOMERS", "CUSTOMER_NAME"
        )


def test_oracle_full_replacement_preserves_objects_owned_by_other_schemas():
    conn, cur = _inventory_connection()
    plan = _plan(conn)

    statements = apply_plan(conn, plan)

    assert statements == []
    assert all("OTHER_OWNER" not in statement for statement in statements)
    assert all(
        not any(token in statement.upper() for token in ("CASCADE", "FORCE", "DROP USER", "DROP DATABASE"))
        for statement in statements
    )
    metadata_queries = [str(call.args[0]).upper() for call in cur.execute.call_args_list]
    assert any("SELECT USER FROM DUAL" in query for query in metadata_queries)
    assert any("FROM USER_OBJECTS" in query for query in metadata_queries)
    assert not any("FROM DBA_OBJECTS" in query for query in metadata_queries)
    assert conn.commit.call_count == 0
    assert len(cur.execute.call_args_list) > 0


def test_oracle_replacement_requires_connected_target_owner():
    conn, cur = _inventory_connection(user="SOMEONE_ELSE")

    with pytest.raises(RuntimeError, match="direct connection as target owner"):
        _plan(conn)

    assert not any(
        str(call.args[0]).lstrip().upper().startswith(("DROP", "ALTER"))
        for call in cur.execute.call_args_list
    )


def test_oracle_replacement_blocks_cross_owner_dependencies_before_ddl():
    conn, cur = _inventory_connection(
        dependencies=[("OTHER_OWNER", "V_REMOTE", "VIEW", "KEEP_T", "TABLE")]
    )

    plan = _plan(conn)

    assert any("cross-owner dependency OTHER_OWNER.V_REMOTE" in item for item in plan.blockers)
    assert plan.dependency_impact[0]["owner"] == "OTHER_OWNER"
    with pytest.raises(RuntimeError, match="has blockers"):
        apply_plan(conn, plan)
    assert not any(
        str(call.args[0]).lstrip().upper().startswith(("DROP", "ALTER"))
        for call in cur.execute.call_args_list
    )


def test_oracle_replacement_preserves_external_dependency_to_target_only_table():
    conn, _ = _inventory_connection(
        dependencies=[("OTHER_OWNER", "V_REMOTE", "VIEW", "OLD_T", "TABLE")]
    )

    plan = _plan(conn)

    assert not plan.blockers
    assert plan.cleanup == ()
    assert plan.dependency_impact[0]["referenced_name"] == "OLD_T"


def test_oracle_replacement_preserves_target_only_materialized_views_natively():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("OLD_MV", "MATERIALIZED VIEW")],
        indexes=[],
    )

    plan = _plan(conn)

    assert plan.blockers == ()
    assert any(item["type"] == "MATERIALIZED VIEW" and item["name"] == "OLD_MV"
               for item in plan.preserved_target_only)


def test_oracle_replacement_preserves_target_only_synonyms():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("LOCAL_ALIAS", "SYNONYM")],
        indexes=[],
    )

    plan = _plan(conn)

    assert plan.cleanup == ()
    assert any(
        item["type"] == "SYNONYM" and item["name"] == "LOCAL_ALIAS"
        for item in plan.preserved_target_only
    )


def test_oracle_replacement_preserves_unsupported_target_only_types_without_touching_them():
    conn, cur = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("OLD_TYPE", "TYPE")],
        indexes=[],
    )

    plan = _plan(conn)

    assert not plan.blockers
    assert any(item["type"] == "TYPE" and item["name"] == "OLD_TYPE"
               for item in plan.preserved_target_only)
    assert apply_plan(conn, plan) == []
    assert not any(str(call.args[0]).lstrip().upper().startswith(("DROP", "ALTER"))
                   for call in cur.execute.call_args_list)


def test_oracle_replacement_reports_target_grant_impact():
    conn, _ = _inventory_connection(
        grants=[("OLD_T", "APP_ROLE", "SELECT", "TABLE")],
    )

    plan = _plan(conn)

    assert plan.blockers == ()
    assert plan.grants_impact == ({
        "object": "OLD_T", "grantee": "APP_ROLE", "privilege": "SELECT", "kind": "object",
    },)


def test_oracle_replacement_does_not_classify_public_inherit_privileges_as_object_grant():
    conn, _ = _inventory_connection(
        grants=[("TARGET_OWNER", "PUBLIC", "INHERIT PRIVILEGES", "USER")],
    )

    plan = _plan(conn)

    assert not any(item["privilege"] == "INHERIT PRIVILEGES" for item in plan.grants_impact)


def test_oracle_source_grant_preflight_ignores_public_inherit_privileges():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [
        [_source_tab_grant("PUBLIC", "INHERIT PRIVILEGES", table="MIGRATIONE2E_ORACLE_SOURCE", kind="USER")],
        [],
    ]
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    source.validate_schema_replacement_grants()

    object_query, column_query = cur.execute.call_args_list
    assert "privilege, grantable" in object_query.args[0].lower()
    assert "from all_tab_privs_made" in object_query.args[0].lower()
    assert "where owner = :1" in object_query.args[0].lower()
    assert object_query.args[1] == ("MIGRATIONE2E_ORACLE_SOURCE",)
    assert "where owner = :1" in column_query.args[0].lower()
    assert column_query.args[1] == ("MIGRATIONE2E_ORACLE_SOURCE",)


def test_oracle_source_preflight_rejects_undiscovered_owner_object_types():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [("CUSTOMER_TYPE", "TYPE")]
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    with pytest.raises(RuntimeError, match="unsupported source-owner object.*TYPE CUSTOMER_TYPE"):
        source.validate_schema_replacement_objects()

    query = cur.execute.call_args.args[0]
    assert "FROM USER_OBJECTS" in query.upper()
    assert "TABLE PARTITION" in query


def test_oracle_source_preflight_blocks_object_and_nested_table_value_transport():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [[], [("OBJECT_ROWS", "ADDRESS_T")], []]
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    with pytest.raises(RuntimeError, match="cannot safely transport object or nested-table values") as caught:
        source.validate_schema_replacement_objects()

    object_table_query = next(
        call.args[0]
        for call in cur.execute.call_args_list
        if "FROM USER_OBJECT_TABLES" in call.args[0].upper()
    )
    assert object_table_query.strip().upper() == (
        "SELECT TABLE_NAME, TABLE_TYPE FROM USER_OBJECT_TABLES ORDER BY TABLE_NAME"
    )
    assert "TYPE_NAME" not in object_table_query.upper()
    assert "object table OBJECT_ROWS OF ADDRESS_T" in str(caught.value)


def test_oracle_source_replacement_preflight_accepts_schema_without_native_object_tables():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [[], [], []]
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    source.validate_schema_replacement_objects()

    object_table_query = next(
        call.args[0]
        for call in cur.execute.call_args_list
        if "FROM USER_OBJECT_TABLES" in call.args[0].upper()
    )
    assert object_table_query.strip().upper() == (
        "SELECT TABLE_NAME, TABLE_TYPE FROM USER_OBJECT_TABLES ORDER BY TABLE_NAME"
    )


@pytest.mark.parametrize("object_type", ["TABLE", "VIEW", "PROCEDURE"])
def test_oracle_source_grant_preflight_still_rejects_object_grants(object_type):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [
        [_source_tab_grant("APP_ROLE", "SELECT", kind=object_type)],
        [],
    ]
    source = OracleSourceConnector({"username": "MIGRATIONE2E_ORACLE_SOURCE"})
    source._conn = conn

    with pytest.raises(RuntimeError, match="does not support source object grants"):
        source.validate_schema_replacement_grants()

    assert "where owner = :1" in cur.execute.call_args_list[0].args[0].lower()
    assert cur.execute.call_args_list[0].args[1] == ("MIGRATIONE2E_ORACLE_SOURCE",)


def test_oracle_replacement_dictionary_queries_bind_owner_and_user_view_is_owner_scoped():
    conn, cur = _inventory_connection(
        constraints=[("KEEP_T", "PK_KEEP", "P", None, None, "USER NAME")],
    )

    _plan(conn)

    calls = cur.execute.call_args_list
    dependency_call = next(call for call in calls if "FROM dba_dependencies" in call.args[0])
    constraints_call = next(call for call in calls if "FROM dba_constraints" in call.args[0])
    local_call = next(call for call in calls if "FROM user_dependencies" in call.args[0])
    object_grant_call = next(
        call for call in calls if "FROM all_tab_privs_made" in call.args[0]
    )
    column_grant_call = next(
        call for call in calls if "FROM all_col_privs_made" in call.args[0]
    )
    assert dependency_call.args[1] == ("TARGET_OWNER", "TARGET_OWNER")
    assert constraints_call.args[1] == ("TARGET_OWNER", "TARGET_OWNER")
    assert local_call.args[1] == ()
    assert "UPPER(OWNER)" not in local_call.args[0].upper()
    assert "WHERE OWNER = :1" in object_grant_call.args[0].upper()
    assert object_grant_call.args[1] == ("TARGET_OWNER",)
    assert "WHERE OWNER = :1" in column_grant_call.args[0].upper()
    assert column_grant_call.args[1] == ("TARGET_OWNER",)


def test_oracle_replacement_reports_local_dependency_and_plans_dependent_first():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("OLD_T", "TABLE"), ("V_LOCAL", "VIEW")],
        indexes=[],
        local_dependencies=[("V_LOCAL", "VIEW", "TARGET_OWNER", "OLD_T", "TABLE")],
    )

    plan = _plan(conn)

    assert plan.blockers == ()
    assert plan.cleanup == ()
    assert plan.dependency_impact == ()


def test_oracle_replacement_distinguishes_missing_dictionary_inspection_privileges():
    conn, _ = _inventory_connection(
        inspection_failures={"DBA_DEPENDENCIES", "DBA_CONSTRAINTS"},
        constraints=[("KEEP_T", "PK_KEEP", "P", None, None, "USER NAME")],
    )

    plan = _plan(conn)
    report = plan.as_dict()

    assert report["status"] == "inspection_incomplete"
    assert len(plan.inspection_errors) == 2
    assert "DBA_DEPENDENCIES" in plan.inspection_errors[0]
    assert any("DBA_CONSTRAINTS" in item for item in plan.inspection_errors)
    assert any(
        'GRANT SELECT ON SYS.DBA_DEPENDENCIES TO "TARGET_OWNER"' in item
        for item in plan.inspection_errors
    )
    assert any(
        'GRANT SELECT ON SYS.DBA_CONSTRAINTS TO "TARGET_OWNER"' in item
        for item in plan.inspection_errors
    )


def test_oracle_replacement_empty_owner_does_not_require_cross_owner_catalog_grants():
    conn, cur = _inventory_connection(
        objects=[], indexes=[], constraints=[], table_names=[],
        inspection_failures={"DBA_DEPENDENCIES", "DBA_CONSTRAINTS"},
    )

    plan = _plan(conn)

    sql = [call.args[0].upper() for call in cur.execute.call_args_list]
    assert plan.inspection_errors == ()
    assert not any("FROM DBA_DEPENDENCIES" in statement for statement in sql)
    assert not any("FROM DBA_CONSTRAINTS" in statement for statement in sql)


def test_oracle_replacement_reports_grant_inspection_privilege_failure_separately():
    conn, _ = _inventory_connection(inspection_failures={"ALL_TAB_PRIVS_MADE"})

    plan = _plan(conn)

    assert any("Grant inventory unavailable" in item for item in plan.inspection_errors)
    assert not any("dependency inventory unavailable" in item.lower() for item in plan.inspection_errors)


def test_oracle_replacement_dry_run_is_read_only_and_reports_exact_plan():
    conn, cur = _inventory_connection()

    plan = _plan(conn)
    report = plan.as_dict()

    assert report["plan_id"] == plan.plan_id
    assert report["destructive_ddl_count"] == 0
    assert not any(
        str(call.args[0]).lstrip().upper().startswith(("DROP", "ALTER", "CREATE"))
        for call in cur.execute.call_args_list
    )


def test_valid_framework_provenance_created_after_preflight_does_not_drift_plan(monkeypatch):
    from core.connectors.oracle import permissions

    monkeypatch.setattr(permissions, "_read_provenance", lambda _conn, _owner: {})
    before, _ = _inventory_connection()
    after, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("OLD_T", "TABLE"),
                 ("V_OLD", "VIEW"), ("IX_OLD", "INDEX"),
                 ("DMT_PERMISSION_PROVENANCE", "TABLE")],
        table_names=["KEEP_T", "OLD_T", "DMT_PERMISSION_PROVENANCE"],
        grants=[("DMT_PERMISSION_PROVENANCE", "ORACLE_PERMISSION_EXECUTOR", privilege, "TABLE")
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")],
        framework_provenance=True,
    )
    preflight = _plan(before)
    after_setup = _plan(after)
    assert after_setup.plan_id == preflight.plan_id
    assert not any(item["name"] == "DMT_PERMISSION_PROVENANCE"
                   for item in after_setup.inventory)
    assert not any(item["object"] == "DMT_PERMISSION_PROVENANCE"
                   for item in after_setup.grants_impact)


def test_reserved_provenance_name_with_invalid_marker_is_not_excluded(monkeypatch):
    from core.connectors.oracle import permissions

    monkeypatch.setattr(
        permissions, "_read_provenance",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("missing framework marker")),
    )
    conn, _ = _inventory_connection(
        objects=[("DMT_PERMISSION_PROVENANCE", "TABLE")],
        table_names=["DMT_PERMISSION_PROVENANCE"],
        framework_provenance=True,
    )
    with pytest.raises(RuntimeError, match="invalid framework provenance metadata"):
        _plan(conn)


def test_concurrent_target_inventory_change_still_blocks_before_ddl(monkeypatch):
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    plan = MagicMock(plan_id="PREFLIGHT")
    target.plan_schema_replacement = MagicMock(return_value=MagicMock(plan_id="CONCURRENT_CHANGE"))
    apply_calls = []
    monkeypatch.setattr(
        "core.connectors.oracle.replacement.apply_plan",
        lambda *_args, **_kwargs: apply_calls.append("apply"),
    )
    with pytest.raises(RuntimeError, match="inventory changed after preflight"):
        target.apply_schema_replacement(plan, tables=set(), schemas=[], views=[], sequences=[], routines=[], triggers=[])
    assert apply_calls == []


def test_oracle_replacement_preserves_constraint_backing_indexes():
    constraint_name = _oracle_constraint_name("KEEP_T", "PK", "PK_KEEP")
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("PK_I", "INDEX")],
        indexes=[("PK_I", "KEEP_T", "KEEP_PK", "NORMAL", "VALID", "NO", "N")],
        constraints=[("KEEP_T", constraint_name, "P", None, None, "USER NAME")],
    )
    schema = Schema(
        "KEEP_T", columns=[Column("ID", "NUMBER")],
        primary_key=["ID"], primary_key_name="PK_KEEP",
    )

    plan = build_plan(
        conn, "TARGET_OWNER", tables={"KEEP_T"}, schemas=[schema], views=[],
        sequences=[], routines=[], triggers=[],
    )

    assert not any(item["type"] == "INDEX" for item in plan.cleanup)
    assert {item["name"] for item in plan.inventory if item["type"] == "CONSTRAINT"} == {constraint_name}


def test_oracle_replacement_reports_parent_managed_objects_for_stale_table_drop():
    conn, _ = _inventory_connection(
        objects=[("KEEP_T", "TABLE"), ("OLD_T", "TABLE"), ("OLD_PK_I", "INDEX")],
        indexes=[("OLD_PK_I", "OLD_T", "OLD_PK", "NORMAL", "VALID", "NO", "N")],
        constraints=[("OLD_T", "OLD_PK", "P", None, None, "USER NAME")],
        identity_sequences=[("ISEQ$$_123", "OLD_T", "ID")],
    )

    plan = _plan(conn)

    assert plan.cleanup == ()
    assert plan.implicit_cleanup == ()
    assert any(item["name"] == "OLD_T" for item in plan.preserved_target_only)


def test_oracle_replacement_drops_only_the_owner_local_plan():
    conn, cur = _inventory_connection()
    plan = _plan(conn)

    statements = apply_plan(conn, plan)

    assert statements == []
    assert all("CASCADE" not in statement.upper() and "FORCE" not in statement.upper()
               for statement in statements)
    assert conn.commit.call_count == 0
    assert len(cur.execute.call_args_list) >= len(statements)
