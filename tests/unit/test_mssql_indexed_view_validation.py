from __future__ import annotations

from unittest.mock import MagicMock

from tests.e2e.mssql.validation.catalog import MSSQLCatalog
from tests.e2e.mssql.validation.source_validator import SourceValidator
from tests.e2e.mssql.validation.target_validator import TargetValidator


def test_catalog_reads_schema_binding_and_view_clustered_index():
    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [("Task4_SourceManaged_IV", 1)],
        [("Task4_SourceManaged_IV", "CIX_Task4_SourceManaged_IV", "CLUSTERED", 1, "ID", 1)],
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    catalog = MSSQLCatalog(conn)
    result = catalog.get_view_properties("training")
    assert result["Task4_SourceManaged_IV"]["is_schema_bound"] is True
    assert result["Task4_SourceManaged_IV"]["indexes"]["CIX_Task4_SourceManaged_IV"] == {
        "type_desc": "CLUSTERED", "is_unique": True, "columns": ["ID"],
    }
    assert "OBJECTPROPERTY(v.object_id, 'IsSchemaBound')" in cursor.execute.call_args_list[0].args[0]


def _validator(target_only: bool, props):
    catalog = MagicMock()
    catalog.get_database_name.return_value = "MigrationE2E_MSSQL_Target"
    catalog.get_all_user_schemas.return_value = ["training"]
    catalog.get_base_tables.return_value = []
    catalog.get_views.return_value = {"Task4_SourceManaged_IV": "CREATE VIEW"}
    catalog.get_view_properties.return_value = props
    validator = TargetValidator(catalog, "MigrationE2E_MSSQL_Target", skip_connectivity=True) if target_only else SourceValidator(catalog, "MigrationE2E_MSSQL_Source")
    # Exercise only the view validation phase; the source fixture has unrelated checks.
    validator._validate_views()
    if target_only:
        validator._validate_target_only_views()
    return validator._report.phases


def test_source_validator_requires_schema_bound_view_and_unique_clustered_index():
    good = {
        "Task4_SourceManaged_IV": {
            "is_schema_bound": True,
            "indexes": {
                "CIX_Task4_SourceManaged_IV": {
                    "type_desc": "CLUSTERED", "is_unique": True, "columns": ["ID"],
                },
            },
        },
    }
    phases = _validator(False, good)
    indexed = next(check for check in phases[0].checks if check.name.endswith(":indexed_view"))
    assert indexed.passed

    bad = {"Task4_SourceManaged_IV": {"is_schema_bound": False, "indexes": {}}}
    phases = _validator(False, bad)
    indexed = next(check for check in phases[0].checks if check.name.endswith(":indexed_view"))
    assert not indexed.passed


def test_target_validator_checks_target_only_indexed_view_preservation():
    props = {
        "Task4_SourceManaged_IV": {"is_schema_bound": True, "indexes": {}},
        "Task4_TargetOnly_IV": {
            "is_schema_bound": True,
            "indexes": {
                "CIX_Task4_TargetOnly_IV": {
                    "type_desc": "CLUSTERED", "is_unique": True, "columns": ["ID"],
                },
            },
        },
    }
    phases = _validator(True, props)
    preservation = next(phase for phase in phases if phase.name == "target_only_indexed_views")
    assert preservation.checks[0].passed
