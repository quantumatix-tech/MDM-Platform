from tests.e2e.mssql.task4_snapshot import (
    _normalized_module_hash,
    compare_snapshot_sides,
    compare_target_snapshots,
)
from tests.e2e.mssql.validation.sql_normalize import normalize_sql_definition


def test_normalized_module_hash_ignores_formatting_and_identifier_brackets():
    source = "CREATE VIEW [training].[v] AS\nSELECT [ID] FROM [training].[t];"
    target = "create or alter view training.v as select ID from training.t; -- generated\n"
    assert _normalized_module_hash(source) == _normalized_module_hash(target)


def test_normalized_module_hash_changes_when_module_body_changes():
    assert _normalized_module_hash("CREATE FUNCTION f() RETURNS int AS BEGIN RETURN 1 END") != (
        _normalized_module_hash("CREATE FUNCTION f() RETURNS int AS BEGIN RETURN 2 END")
    )


def test_normalization_preserves_case_sensitive_string_literal_changes():
    assert normalize_sql_definition("SELECT 'Open' AS [Value]") != normalize_sql_definition(
        "select 'open' as value"
    )


def test_snapshot_comparison_reports_module_definition_deltas_and_type_conflict():
    source = {
        "inventory": [
            {"schema_name": "training", "object_name": "f", "object_type_desc": "SQL_SCALAR_FUNCTION"},
            {"schema_name": "training", "object_name": "conflict", "object_type_desc": "VIEW"},
        ],
        "modules": [
            {"schema_name": "training", "object_name": "f", "type_desc": "SQL_SCALAR_FUNCTION",
             "definition_normalized_sha256": "source-hash"},
        ],
    }
    target = {
        "inventory": [
            {"schema_name": "training", "object_name": "f", "object_type_desc": "SQL_SCALAR_FUNCTION"},
            {"schema_name": "training", "object_name": "conflict", "object_type_desc": "SQL_STORED_PROCEDURE"},
        ],
        "modules": [
            {"schema_name": "training", "object_name": "f", "type_desc": "SQL_SCALAR_FUNCTION",
             "definition_normalized_sha256": "target-hash"},
        ],
    }
    comparison = compare_snapshot_sides({"source": source, "target": target})
    assert comparison["module_definition_differences"] == [{
        "schema_name": "training", "object_name": "f", "type_desc": "SQL_SCALAR_FUNCTION",
        "source_normalized_sha256": "source-hash", "target_normalized_sha256": "target-hash",
    }]
    assert comparison["type_conflicts"] == [{
        "schema_name": "training", "object_name": "conflict",
        "source_type": "VIEW", "target_type": "SQL_STORED_PROCEDURE",
    }]


def test_snapshot_comparison_keeps_target_only_objects_visible():
    before = {"target": {"inventory": [{"schema_name": "training", "object_name": "target_owned",
                                         "object_type": "V"}]}}
    after = {"target": {"inventory": [{"schema_name": "training", "object_name": "target_owned",
                                        "object_type": "V"}]}}
    delta = compare_target_snapshots(before, after)
    assert delta["inventory"] == {"removed": [], "added": []}

    comparison = compare_snapshot_sides({
        "source": {"inventory": [], "modules": []},
        "target": {"inventory": [{"schema_name": "training", "object_name": "target_owned",
                                   "object_type": "V"}], "modules": []},
    })
    assert comparison["categories"]["inventory"]["target_only"] == [
        {"schema_name": "training", "object_name": "target_owned", "object_type": "V"}
    ]
