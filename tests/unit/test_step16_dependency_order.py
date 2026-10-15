from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column,
    ForeignKey,
    Schema,
    SourceConnector,
    TargetConnector,
    UpsertResult,
)
from core.connectors.mssql import PartitionedTableDef
from core.orchestrator import MigrationOrchestrator


def _schema(name: str, schema_name: str = "dbo") -> Schema:
    return Schema(
        name=name,
        schema_name=schema_name,
        columns=[Column(name="id", source_type="int", nullable=False)],
        primary_key=["id"],
    )


def _orchestrator_for_order_test():
    """Create orchestrator with mocks that track call order."""
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)

    objects = ["table1", "table2"]
    schemas_map = {name: _schema(name) for name in objects}

    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas_map[name]
    source.get_object_count.return_value = 1
    source.export_full.side_effect = lambda name, **kw: iter([{"id": 1}])
    source.list_extensions.return_value = []
    source.list_schemas.return_value = [MagicMock(name="dbo")]
    source.list_types.return_value = []
    # These methods are engine-specific; orchestrator handles AttributeError
    del source.list_all_sequences
    del source.list_partitions
    source.list_views.return_value = []
    source.list_materialized_views.return_value = []
    source.list_functions.return_value = []
    source.list_synonyms.return_value = []
    source.get_all_triggers.return_value = []
    source.list_comments.return_value = []
    source.list_roles.return_value = []
    source.list_users.return_value = []
    source.list_role_memberships.return_value = []
    source.list_grants.return_value = []

    target.get_object_count.return_value = 1
    target.upsert_batch.return_value = UpsertResult(success_count=1)

    orchestrator = MigrationOrchestrator(
        source,
        target,
        {"migration": {"stop_on_error": False}},
    )
    return orchestrator, source, target


def _orchestrator_with_partitioned_table():
    """Create orchestrator with a partitioned table."""
    source = MagicMock()  # No spec to allow get_partitioned_tables
    target = MagicMock()  # No spec to allow create_partitioned_table

    # Regular table and partitioned table
    objects = ["regular_table", "partitioned_table"]
    schemas_map = {
        "regular_table": _schema("regular_table"),
        "partitioned_table": _schema("partitioned_table"),
    }

    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas_map[name]
    source.get_object_count.return_value = 1
    source.export_full.side_effect = lambda name, **kw: iter([{"id": 1}])
    source.list_extensions.return_value = []
    source.list_schemas.return_value = [MagicMock(name="dbo")]
    source.list_types.return_value = []
    del source.list_all_sequences
    del source.list_partitions
    source.list_views.return_value = []
    source.list_materialized_views.return_value = []
    source.list_functions.return_value = []
    source.list_synonyms.return_value = []
    source.get_all_triggers.return_value = []
    source.list_comments.return_value = []
    source.list_roles.return_value = []
    source.list_users.return_value = []
    source.list_role_memberships.return_value = []
    source.list_grants.return_value = []

    # Partitioned table metadata - returned by get_partitioned_tables()
    pt_def = PartitionedTableDef(
        table_name="partitioned_table",
        schema_name="dbo",
        index_name="idx_partitioned_table",
        partition_function_name="pf_date",
        partition_scheme_name="ps_date",
        partition_column="created_date",
    )
    source.get_partitioned_tables.return_value = [pt_def]

    # Partition function/scheme creation
    source.list_partition_functions.return_value = [
        MagicMock(name="pf_date", schema_name="dbo", data_type="datetime2", boundaries=[], range_desc="RANGE RIGHT")
    ]
    source.list_partition_schemes.return_value = [
        MagicMock(name="ps_date", schema_name="dbo", partition_function_name="pf_date", filegroups=["PRIMARY"])
    ]

    target.get_object_count.return_value = 1
    target.upsert_batch.return_value = UpsertResult(success_count=1)

    orchestrator = MigrationOrchestrator(
        source,
        target,
        {"migration": {"stop_on_error": False}},
    )
    return orchestrator, source, target


def test_partitioned_table_excluded_from_create_tables_phase():
    """
    Partitioned tables should NOT be created in Phase 4 (create_tables).
    They should be created in Phase 4.5 (create_partitions) via create_partitioned_table.
    """
    _orchestrator, _source, target = _orchestrator_with_partitioned_table()

    _orchestrator.run_full()

    # Verify create_object_if_missing was NOT called for partitioned_table
    create_calls = [c.args[0].name for c in target.create_object_if_missing.call_args_list]
    assert "regular_table" in create_calls, "Regular table should be created in create_tables phase"
    assert "partitioned_table" not in create_calls, "Partitioned table should NOT be created in create_tables phase"

    # Verify create_partitioned_table WAS called for partitioned_table
    partitioned_calls = [c.args[0].name for c in target.create_partitioned_table.call_args_list]
    assert "partitioned_table" in partitioned_calls, "Partitioned table should be created in create_partitions phase"


def test_partitioned_table_excluded_from_create_tables_incremental():
    """
    Partitioned tables should NOT be created in Phase 4 (create_tables) for run_cdc.
    They should be created in Phase 4.5 (create_partitions) via create_partitioned_table.
    """
    _orchestrator, _source, target = _orchestrator_with_partitioned_table()

    _orchestrator.run_cdc(max_iterations=1)

    # Verify create_object_if_missing was NOT called for partitioned_table
    create_calls = [c.args[0].name for c in target.create_object_if_missing.call_args_list]
    assert "regular_table" in create_calls, "Regular table should be created in create_tables phase"
    assert "partitioned_table" not in create_calls, "Partitioned table should NOT be created in create_tables phase"

    # Verify create_partitioned_table WAS called for partitioned_table
    partitioned_calls = [c.args[0].name for c in target.create_partitioned_table.call_args_list]
    assert "partitioned_table" in partitioned_calls, "Partitioned table should be created in create_partitions phase"


def test_phase_order_schemas_before_tables():
    """Schemas phase must execute before create_tables phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    schemas_idx = phase_order.index("schemas")
    create_tables_idx = phase_order.index("create_tables")
    assert schemas_idx < create_tables_idx, "schemas phase must run before create_tables"


def test_phase_order_custom_types_before_tables():
    """Custom types (UDTs) phase must execute before create_tables phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    types_idx = phase_order.index("custom_types")
    create_tables_idx = phase_order.index("create_tables")
    assert types_idx < create_tables_idx, "custom_types phase must run before create_tables"


def test_phase_order_tables_before_constraints():
    """Create tables phase must execute before apply_constraints phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    constraints_idx = phase_order.index("apply_constraints")
    assert create_tables_idx < constraints_idx, "create_tables must run before apply_constraints"


def test_phase_order_tables_before_data():
    """Create tables phase must execute before data migration phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    # Data migration is per-object; check first object's phase appears after create_tables
    first_obj_idx = phase_order.index("table1")
    assert create_tables_idx < first_obj_idx, "create_tables must run before data migration"


def test_phase_order_tables_before_views():
    """Create tables phase must execute before views phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    views_idx = phase_order.index("views")
    assert create_tables_idx < views_idx, "create_tables must run before views"


def test_phase_order_tables_before_functions():
    """Create tables phase must execute before functions phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    functions_idx = phase_order.index("functions")
    assert create_tables_idx < functions_idx, "create_tables must run before functions"


def test_phase_order_tables_before_synonyms():
    """Create tables phase must execute before synonyms phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    synonyms_idx = phase_order.index("synonyms")
    assert create_tables_idx < synonyms_idx, "create_tables must run before synonyms"


def test_phase_order_tables_before_triggers():
    """Create tables phase must execute before triggers phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    triggers_idx = phase_order.index("triggers")
    assert create_tables_idx < triggers_idx, "create_tables must run before triggers"


def test_phase_order_tables_before_comments():
    """Create tables phase must execute before comments phase."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    comments_idx = phase_order.index("comments")
    assert create_tables_idx < comments_idx, "create_tables must run before comments"


def test_phase_order_schemas_objects_before_security_grants():
    """Schemas and tables must execute before security and grants phases."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    schemas_idx = phase_order.index("schemas")
    create_tables_idx = phase_order.index("create_tables")
    security_idx = phase_order.index("security")
    grants_idx = phase_order.index("grants")

    assert schemas_idx < security_idx, "schemas must run before security"
    assert create_tables_idx < security_idx, "create_tables must run before security"
    assert security_idx < grants_idx, "security must run before grants"


def test_full_phase_sequence_matches_dependency_graph():
    """Verify the complete phase sequence follows the expected dependency order."""
    orchestrator, _source, _target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())

    # Expected dependency sequence (phases that must be ordered)
    expected_sequence = [
        "connect",
        "ensure_database",
        "discover",
        "extensions",
        "schemas",
        "custom_types",
        "create_sequences",
        "create_tables",
        "create_partitions",
        "apply_constraints",
        "apply_sequence_ownership",
        "row_level_security",
        "advance_sequences",
        "views",
        "materialized_views",
        "functions",
        "synonyms",
        "triggers",
        "comments",
        "security",
        "grants",
        "validation",
    ]

    # Check each expected phase appears in order
    last_idx = -1
    for phase in expected_sequence:
        if phase in phase_order:
            idx = phase_order.index(phase)
            assert idx > last_idx, f"Phase {phase} appears out of order (idx {idx} <= {last_idx})"
            last_idx = idx


def test_full_migration_loads_data_in_schema_dependency_order():
    orchestrator, source, target = _orchestrator_for_order_test()
    objects = ["child", "independent", "parent"]
    schemas = {
        "child": Schema(
            name="child",
            schema_name="dbo",
            columns=[Column(name="id", source_type="int")],
            foreign_keys=[
                ForeignKey(
                    name="fk_child_parent",
                    columns=["parent_id"],
                    ref_table="parent",
                    ref_columns=["id"],
                    ref_schema="dbo",
                )
            ],
        ),
        "independent": _schema("independent"),
        "parent": _schema("parent"),
    }
    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas[name]

    result = orchestrator.run_full()

    loaded = [c.args[0] for c in target.upsert_batch.call_args_list]
    assert loaded == ["independent", "parent", "child"]
    assert result["phases"]["data_load_order"] == loaded


def test_full_migration_reports_fk_cycle_before_clearing_or_loading_data():
    orchestrator, source, target = _orchestrator_for_order_test()
    objects = ["A", "B"]
    schemas = {
        "A": Schema(
            name="A",
            schema_name="dbo",
            foreign_keys=[ForeignKey("fk_A_B", ["b_id"], "B", ["id"], "dbo")],
        ),
        "B": Schema(
            name="B",
            schema_name="dbo",
            foreign_keys=[ForeignKey("fk_B_A", ["a_id"], "A", ["id"], "dbo")],
        ),
    }
    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas[name]

    result = orchestrator.run_full()

    assert result["status"] == "failed"
    assert "dependency cycle" in result["error"].lower()
    target.clear_objects_for_full_sync.assert_not_called()
    target.upsert_batch.assert_not_called()


def test_data_load_order_handles_chains_and_multiple_parents():
    from core.dependency_order import order_data_load_objects
    schemas = {
        "leaf": Schema(
            name="leaf",
            schema_name="dbo",
            foreign_keys=[
                ForeignKey("fk_leaf_middle", ["middle_id"], "middle", ["id"], "dbo"),
                ForeignKey("fk_leaf_other", ["other_id"], "other", ["id"], "dbo"),
            ],
        ),
        "middle": Schema(
            name="middle",
            schema_name="dbo",
            foreign_keys=[ForeignKey("fk_middle_root", ["root_id"], "root", ["id"], "dbo")],
        ),
        "root": _schema("root"),
        "other": _schema("other"),
    }

    order = order_data_load_objects(schemas, ["leaf", "middle", "other", "root"])

    assert order.index("root") < order.index("middle") < order.index("leaf")
    assert order.index("other") < order.index("leaf")


def test_data_load_order_preserves_order_without_fks_and_for_unrelated_objects():
    from core.dependency_order import order_data_load_objects
    no_fks = {name: _schema(name) for name in ["z", "a", "m"]}
    assert order_data_load_objects(no_fks) == ["z", "a", "m"]

    schemas = {"A": _schema("A"), "B": _schema("B"), "C": Schema(
        name="C",
        schema_name="dbo",
        foreign_keys=[ForeignKey("fk_C_A", ["a_id"], "A", ["id"], "dbo")],
    )}
    assert order_data_load_objects(schemas) == ["A", "B", "C"]


def test_data_load_order_uses_schema_qualified_reference_when_names_repeat():
    from core.dependency_order import order_data_load_objects
    schemas = {
        "child": Schema(
            name="child",
            schema_name="child_schema",
            foreign_keys=[
                ForeignKey("fk_child_parent", ["parent_id"], "parent", ["id"], "target_schema")
            ],
        ),
        "target_parent": Schema(name="parent", schema_name="target_schema"),
        "other_parent": Schema(name="parent", schema_name="other_schema"),
    }

    order = order_data_load_objects(
        schemas, ["child", "target_parent", "other_parent"]
    )

    assert order.index("target_parent") < order.index("child")
    assert order.index("child") < order.index("other_parent")


def test_data_load_order_ignores_missing_referenced_table():
    """FK referencing a table not in the migration scope should not affect ordering."""
    from core.dependency_order import order_data_load_objects
    schemas = {
        "child": Schema(
            name="child",
            schema_name="dbo",
            foreign_keys=[
                ForeignKey("fk_child_missing", ["parent_id"], "missing_table", ["id"], "dbo")
            ],
        ),
        "other": _schema("other"),
    }
    # "missing_table" is not in schemas - should be ignored, not cause an error
    order = order_data_load_objects(schemas, ["child", "other"])
    # Both tables have no resolvable dependencies, so original order preserved
    assert order == ["child", "other"]


def test_data_load_order_self_referencing_table():
    """Self-referencing FK (e.g., employee.manager_id -> employee.id) should not create cycle."""
    from core.dependency_order import order_data_load_objects
    schemas = {
        "employee": Schema(
            name="employee",
            schema_name="dbo",
            foreign_keys=[
                ForeignKey("fk_emp_mgr", ["manager_id"], "employee", ["id"], "dbo")
            ],
        ),
    }
    order = order_data_load_objects(schemas, ["employee"])
    # Self-reference is ignored for dependency ordering, so the table should be loadable
    assert order == ["employee"]


def test_data_load_order_multiple_independent_trees():
    """Two independent parent->child trees should be loadable in any order relative to each other."""
    from core.dependency_order import order_data_load_objects
    schemas = {
        "child1": Schema(name="child1", schema_name="dbo", foreign_keys=[ForeignKey("fk_c1_p1", ["p1_id"], "parent1", ["id"], "dbo")]),
        "parent1": _schema("parent1"),
        "child2": Schema(name="child2", schema_name="dbo", foreign_keys=[ForeignKey("fk_c2_p2", ["p2_id"], "parent2", ["id"], "dbo")]),
        "parent2": _schema("parent2"),
    }
    # Order within each tree must be preserved, but trees can be interleaved
    order = order_data_load_objects(schemas, ["child1", "parent1", "child2", "parent2"])
    assert order.index("parent1") < order.index("child1")
    assert order.index("parent2") < order.index("child2")


def test_data_load_order_one_parent_multiple_children():
    """One parent with multiple children - parent first, children in discovery order."""
    from core.dependency_order import order_data_load_objects
    schemas = {
        "child_a": Schema(name="child_a", schema_name="dbo", foreign_keys=[ForeignKey("fk_ca_p", ["p_id"], "parent", ["id"], "dbo")]),
        "child_b": Schema(name="child_b", schema_name="dbo", foreign_keys=[ForeignKey("fk_cb_p", ["p_id"], "parent", ["id"], "dbo")]),
        "parent": _schema("parent"),
    }
    order = order_data_load_objects(schemas, ["child_a", "child_b", "parent"])
    assert order.index("parent") < order.index("child_a")
    assert order.index("parent") < order.index("child_b")
    # Children should preserve discovery order
    assert order.index("child_a") < order.index("child_b")


def test_cdc_migration_uses_dependency_order():
    """run_cdc should also use FK-aware data load order for initial sync."""
    orchestrator, source, target = _orchestrator_for_order_test()
    objects = ["child", "independent", "parent"]
    schemas = {
        "child": Schema(
            name="child",
            schema_name="dbo",
            columns=[Column(name="id", source_type="int")],
            foreign_keys=[ForeignKey("fk_child_parent", ["parent_id"], "parent", ["id"], "dbo")],
        ),
        "independent": _schema("independent"),
        "parent": _schema("parent"),
    }
    source.list_objects.return_value = objects
    source.get_schema.side_effect = lambda name, **kw: schemas[name]
    source.list_all_sequences = MagicMock(return_value=[])
    source.list_partitions = MagicMock(return_value=[])
    source.list_partition_functions = MagicMock(return_value=[])
    source.list_partition_schemes = MagicMock(return_value=[])
    source.get_partitioned_tables = MagicMock(return_value=[])

    result = orchestrator.run_cdc(max_iterations=1)

    loaded = [c.args[0] for c in target.upsert_batch.call_args_list]
    assert loaded == ["independent", "parent", "child"]
    assert result["phases"]["data_load_order"] == loaded


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
