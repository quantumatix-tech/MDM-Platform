"""Unit tests for the MSSQL E2E source validation framework.

These tests use mocked catalog data — no live MSSQL connection required.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from tests.e2e.mssql.validation.expected import (
    CHECK_CONSTRAINTS,
    DEFAULT_CONSTRAINTS,
    EXPECTED_EXT_PROP_COUNT,
    FOREIGN_KEYS,
    FUNCTIONS,
    INDEXES,
    PARTITION_FUNCTION,
    PARTITION_SCHEME,
    PARTITIONED_TABLE,
    PROCEDURES,
    ROLES,
    ROW_COUNTS,
    SCHEMA_NAME,
    SEQUENCES,
    SYNONYMS,
    TABLES,
    TRIGGERS,
    UNIQUE_CONSTRAINTS,
    USER_TYPES,
    USERS,
    VIEWS,
)
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)
from tests.e2e.mssql.validation.source_validator import (
    SourceValidator,
    _norm_udt,
)


def _full_pass_catalog() -> MagicMock:
    """Return a mock MSSQLCatalog where every query returns the expected
    fixture data, so the validator should produce an all-PASS report."""
    cat = MagicMock()
    cat.database_exists.return_value = True
    cat.schema_exists.return_value = True
    cat.get_base_tables.return_value = list(TABLES.keys())
    cat.get_columns.side_effect = lambda schema, table: [
        {
            "name": c.name,
            "base_type": c.base_type,
            "is_nullable": c.nullable,
            "max_length": c.size,
            "numeric_precision": c.precision,
            "numeric_scale": c.scale,
            "is_identity": c.is_identity,
            "identity_seed": None,
            "identity_increment": None,
            "is_computed": c.is_computed,
            "computed_definition": None,
            "udt_name": c.udt_name,
        }
        for c in TABLES[table].columns
    ]
    cat.get_primary_key.side_effect = lambda schema, table: {
        "name": TABLES[table].pk_name,
        "columns": TABLES[table].pk_columns,
    }
    cat.get_foreign_keys.return_value = [
        {
            "name": fk_name,
            "columns": cols,
            "ref_table": ref_table,
            "ref_schema": SCHEMA_NAME,
            "ref_columns": ref_cols,
            "delete_action": "NO_ACTION",
            "update_action": "NO_ACTION",
        }
        for table_fks in FOREIGN_KEYS.values()
        for fk_name, cols, ref_table, ref_cols in table_fks
    ]
    cat.get_unique_constraints.side_effect = lambda schema, table: [
        {"name": name, "columns": cols}
        for name, cols in UNIQUE_CONSTRAINTS.get(table, [])
    ]
    cat.get_check_constraints.side_effect = lambda schema, table: [
        {"name": n, "definition": "CHECK"}
        for n in CHECK_CONSTRAINTS.get(table, [])
    ]
    cat.get_default_constraints.side_effect = lambda schema, table: [
        {"name": n, "column": "col", "definition": "DEF"}
        for n in DEFAULT_CONSTRAINTS.get(table, [])
    ]
    cat.get_indexes.side_effect = lambda schema, table: [
        {
            "name": idx_name,
            "is_unique": is_unique,
            "type_desc": "CLUSTERED" if is_clustered else "NONCLUSTERED",
            "columns": cols,
            "included_columns": included,
            "filter_definition": None,
        }
        for idx_name, is_unique, cols, included, is_clustered in INDEXES.get(table, [])
    ]
    cat.get_views.return_value = {v: f"SELECT 1 AS {v}" for v in VIEWS}
    cat.get_functions.return_value = {
        f_name: {"type": f_type, "definition": "FN"}
        for f_name, f_type in FUNCTIONS.items()
    }
    cat.get_procedures.return_value = {p: "PROC" for p in PROCEDURES}
    cat.get_triggers.return_value = [
        {"name": t_name, "table": t_table, "is_disabled": t_disabled}
        for t_name, (t_table, t_disabled) in TRIGGERS.items()
    ]
    cat.get_sequences.return_value = {
        s_name: {"data_type": s_type, "start_value": s_start,
                 "increment": 1, "is_cycling": False, "current_value": s_start}
        for s_name, (s_type, s_start) in SEQUENCES.items()
    }
    cat.get_synonyms.return_value = dict(SYNONYMS)
    cat.get_user_types.return_value = {
        t_name: {"base_type": t_base, "is_nullable": t_nullable}
        for t_name, (t_base, t_nullable) in USER_TYPES.items()
    }
    cat.get_partition_functions.return_value = [
        {"name": PARTITION_FUNCTION, "type_desc": "RANGE",
         "boundaries": ["2024-01-01"], "range_right": True}
    ]
    cat.get_partition_schemes.return_value = [
        {"name": PARTITION_SCHEME, "function": PARTITION_FUNCTION,
         "filegroups": ["PRIMARY"]}
    ]
    cat.get_partitioned_tables.return_value = [
        {"table": PARTITIONED_TABLE, "partition_column": "OrderDate",
         "scheme": PARTITION_SCHEME}
    ]
    cat.get_security_info.return_value = {
        "roles": list(ROLES),
        "users": list(USERS),
        "memberships": [
            {"member": "E2E_TestUser", "role": "Role_ReadOnly"}
        ],
        "grants": [{"grantee": "Role_ReadOnly", "privilege": "SELECT",
                     "class_desc": "OBJECT_OR_COLUMN", "object_name": "Customers",
                     "schema_name": SCHEMA_NAME}],
    }
    cat.get_extended_properties.return_value = [
        {"class_desc": "SCHEMA", "object_name": SCHEMA_NAME,
         "prop_name": "MS_Description", "value": "desc", "column_name": ""}
    ] * EXPECTED_EXT_PROP_COUNT
    cat.get_row_count.side_effect = lambda schema, table: ROW_COUNTS[table]
    cat.count_by_column_value.return_value = ROW_COUNTS["Orders"]
    return cat


# ============================================================
# Models tests
# ============================================================

class TestCheckResult:
    def test_pass_is_passed(self):
        cr = CheckResult(name="t1", status=STATUS_PASS)
        assert cr.passed is True

    def test_fail_is_not_passed(self):
        cr = CheckResult(name="t1", status=STATUS_FAIL)
        assert cr.passed is False

    def test_check_result_defaults(self):
        cr = CheckResult(name="t1", status=STATUS_PASS)
        assert cr.expected is None
        assert cr.actual is None
        assert cr.message == ""
        assert cr.details == {}


class TestPhaseResult:
    def test_pass_when_no_failures(self):
        phase = PhaseResult(name="tables")
        phase.add_check(CheckResult(name="t1", status=STATUS_PASS))
        assert phase.status == STATUS_PASS

    def test_fail_when_any_failure(self):
        phase = PhaseResult(name="tables")
        phase.add_check(CheckResult(name="t1", status=STATUS_PASS))
        phase.add_check(CheckResult(name="t2", status=STATUS_FAIL,
                                     message="bad"))
        assert phase.status == STATUS_FAIL

    def test_to_dict_structure(self):
        phase = PhaseResult(name="tables")
        phase.add_check(CheckResult(name="t1", status=STATUS_PASS,
                                     expected=5, actual=5))
        d = phase.to_dict()
        assert d["name"] == "tables"
        assert d["status"] == STATUS_PASS
        assert len(d["checks"]) == 1
        assert d["checks"][0]["name"] == "t1"
        assert d["checks"][0]["expected"] == 5


class TestValidationReport:
    def test_empty_report_passes(self):
        report = ValidationReport()
        assert report.passed is True
        assert report.summary["PASS"] == 0
        assert report.summary["FAIL"] == 0

    def test_report_includes_database(self):
        report = ValidationReport(database="MyDB")
        d = report.to_dict()
        assert d["database"] == "MyDB"

    def test_passed_property_false_on_failure(self):
        report = ValidationReport()
        phase = PhaseResult(name="p")
        phase.add_check(CheckResult(name="x", status=STATUS_FAIL))
        report.phases.append(phase)
        assert report.passed is False

    def test_summary_counts(self):
        report = ValidationReport()
        phase = PhaseResult(name="p")
        phase.add_check(CheckResult(name="a", status=STATUS_PASS))
        phase.add_check(CheckResult(name="b", status=STATUS_PASS))
        phase.add_check(CheckResult(name="c", status=STATUS_FAIL))
        report.phases.append(phase)
        s = report.summary
        assert s["PASS"] == 2
        assert s["FAIL"] == 1


# ============================================================
# _norm_udt helper tests
# ============================================================

class TestNormUdt:
    def test_normalizes_brackets(self):
        assert _norm_udt("[training].[CustomerCode]") == "training.CustomerCode"

    def test_none_returns_none(self):
        assert _norm_udt(None) is None

    def test_no_brackets(self):
        assert _norm_udt("training.CustomerCode") == "training.CustomerCode"


# ============================================================
# SourceValidator tests with mocked catalog
# ============================================================

class TestSourceValidatorPass:
    """All validation phases should PASS when the mock catalog
    returns exactly the expected fixture data."""

    def test_full_pass(self):
        cat = _full_pass_catalog()
        validator = SourceValidator(cat, database_name="MigrationE2E_MSSQL_Source")
        report = validator.validate()
        assert report.passed, "Expected all checks to pass, got failures"
        for phase in report.phases:
            assert phase.status == STATUS_PASS, \
                f"Phase '{phase.name}' failed unexpectedly"

    def test_reports_included(self):
        """Each expected phase should appear in the report."""
        cat = _full_pass_catalog()
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        phase_names = [p.name for p in report.phases]
        for name in ["database", "schemas", "tables", "columns",
                      "primary_keys", "foreign_keys", "unique_constraints",
                      "check_constraints", "default_constraints", "indexes",
                      "views", "functions", "procedures", "triggers",
                      "sequences", "synonyms", "user_defined_types",
                      "partitioning", "security", "extended_properties",
                      "row_counts", "trigger_data"]:
            assert name in phase_names, f"Missing phase: {name}"

    def test_total_check_count(self):
        """With the full-pass mock, all expected checks should pass."""
        cat = _full_pass_catalog()
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        assert report.summary["FAIL"] == 0
        # 1 (database) + 1 (schema) + ... should be > 40 checks
        assert report.summary["PASS"] > 40


class TestSourceValidatorFailures:
    """When the mock returns wrong data, the validator should report FAIL."""

    def test_missing_table_detected(self):
        cat = _full_pass_catalog()
        cat.get_base_tables.return_value = ["Customers", "Products"]  # missing 5 tables
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        tables_phase = next(p for p in report.phases if p.name == "tables")
        assert tables_phase.status == STATUS_FAIL

    def test_wrong_row_count_detected(self):
        cat = _full_pass_catalog()
        cat.get_row_count.side_effect = lambda schema, table: {
            t: ROW_COUNTS[t] for t in ROW_COUNTS
        }
        cat.get_row_count.side_effect = lambda s, t: ROW_COUNTS[t] - 1
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        rc_phase = next(p for p in report.phases if p.name == "row_counts")
        assert rc_phase.status == STATUS_FAIL

    def test_wrong_column_type_detected(self):
        cat = _full_pass_catalog()
        cat.get_columns.side_effect = lambda schema, table: [
            {
                "name": c.name,
                "base_type": "wrong_type" if c.name == "CustomerID" else c.base_type,
                "is_nullable": c.nullable,
                "max_length": c.size,
                "numeric_precision": c.precision,
                "numeric_scale": c.scale,
                "is_identity": c.is_identity,
                "identity_seed": None,
                "identity_increment": None,
                "is_computed": c.is_computed,
                "computed_definition": None,
                "udt_name": c.udt_name,
            }
            for c in TABLES[table].columns
        ]
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        cols_phase = next(p for p in report.phases if p.name == "columns")
        assert cols_phase.status == STATUS_FAIL

    def test_bad_synonym_base_detected(self):
        cat = _full_pass_catalog()
        cat.get_synonyms.return_value = {
            "syn_Orders": "wrong.BaseTable",
            "syn_OrderDetails": "wrong.BaseTable2",
        }
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        syn_phase = next(p for p in report.phases if p.name == "synonyms")
        assert syn_phase.status == STATUS_FAIL

    def test_missing_partition_function_detected(self):
        cat = _full_pass_catalog()
        cat.get_partition_functions.return_value = []
        cat.get_partition_schemes.return_value = []
        cat.get_partitioned_tables.return_value = []
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        part_phase = next(p for p in report.phases if p.name == "partitioning")
        assert part_phase.status == STATUS_FAIL

    def test_wrong_trigger_action_detected(self):
        cat = _full_pass_catalog()
        cat.count_by_column_value.return_value = 0  # no INSERT actions
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        td_phase = next(p for p in report.phases if p.name == "trigger_data")
        assert td_phase.status == STATUS_FAIL

    def test_db_not_found_detected(self):
        cat = _full_pass_catalog()
        cat.database_exists.return_value = False
        validator = SourceValidator(cat, database_name="NonExistentDB")
        report = validator.validate()
        db_phase = next(p for p in report.phases if p.name == "database")
        assert db_phase.status == STATUS_FAIL


# ============================================================
# Catalog synonym normalization test (using a mock cursor)
# ============================================================

class TestCatalogSynonymNormalization:
    def _make_catalog(self, synonyms_data):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = synonyms_data
        conn.cursor.return_value.__enter__.return_value = cursor
        conn.cursor.return_value.__exit__.return_value = False
        from tests.e2e.mssql.validation.catalog import MSSQLCatalog
        return MSSQLCatalog(conn)

    def test_brackets_stripped(self):
        cat = self._make_catalog([
            ("syn_Orders", "[training].[Orders]"),
            ("syn_OrderDetails", "[training].[OrderDetails]"),
        ])
        result = cat.get_synonyms("training")
        assert result["syn_Orders"] == "training.Orders"
        assert result["syn_OrderDetails"] == "training.OrderDetails"

    def test_no_brackets_preserved(self):
        cat = self._make_catalog([
            ("syn_Orders", "training.Orders"),
        ])
        result = cat.get_synonyms("training")
        assert result["syn_Orders"] == "training.Orders"
