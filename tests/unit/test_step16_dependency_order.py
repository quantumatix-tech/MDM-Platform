from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock, call

import pytest

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    Schema,
    Column,
    UpsertResult,
    ViewDefinition,
    FunctionDef,
    SynonymDef,
    TriggerDef,
    CommentDef,
    GrantDef,
    RoleDef,
    UserDef,
    RoleMembershipDef,
)
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


def test_phase_order_schemas_before_tables():
    """Schemas phase must execute before create_tables phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    schemas_idx = phase_order.index("schemas")
    create_tables_idx = phase_order.index("create_tables")
    assert schemas_idx < create_tables_idx, "schemas phase must run before create_tables"


def test_phase_order_custom_types_before_tables():
    """Custom types (UDTs) phase must execute before create_tables phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    types_idx = phase_order.index("custom_types")
    create_tables_idx = phase_order.index("create_tables")
    assert types_idx < create_tables_idx, "custom_types phase must run before create_tables"


def test_phase_order_tables_before_constraints():
    """Create tables phase must execute before apply_constraints phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    constraints_idx = phase_order.index("apply_constraints")
    assert create_tables_idx < constraints_idx, "create_tables must run before apply_constraints"


def test_phase_order_tables_before_data():
    """Create tables phase must execute before data migration phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    # Data migration is per-object; check first object's phase appears after create_tables
    first_obj_idx = phase_order.index("table1")
    assert create_tables_idx < first_obj_idx, "create_tables must run before data migration"


def test_phase_order_tables_before_views():
    """Create tables phase must execute before views phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    views_idx = phase_order.index("views")
    assert create_tables_idx < views_idx, "create_tables must run before views"


def test_phase_order_tables_before_functions():
    """Create tables phase must execute before functions phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    functions_idx = phase_order.index("functions")
    assert create_tables_idx < functions_idx, "create_tables must run before functions"


def test_phase_order_tables_before_synonyms():
    """Create tables phase must execute before synonyms phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    synonyms_idx = phase_order.index("synonyms")
    assert create_tables_idx < synonyms_idx, "create_tables must run before synonyms"


def test_phase_order_tables_before_triggers():
    """Create tables phase must execute before triggers phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    triggers_idx = phase_order.index("triggers")
    assert create_tables_idx < triggers_idx, "create_tables must run before triggers"


def test_phase_order_tables_before_comments():
    """Create tables phase must execute before comments phase."""
    orchestrator, source, target = _orchestrator_for_order_test()

    result = orchestrator.run_full()

    phase_order = list(result["phases"].keys())
    create_tables_idx = phase_order.index("create_tables")
    comments_idx = phase_order.index("comments")
    assert create_tables_idx < comments_idx, "create_tables must run before comments"


def test_phase_order_schemas_objects_before_security_grants():
    """Schemas and tables must execute before security and grants phases."""
    orchestrator, source, target = _orchestrator_for_order_test()

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
    orchestrator, source, target = _orchestrator_for_order_test()

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])