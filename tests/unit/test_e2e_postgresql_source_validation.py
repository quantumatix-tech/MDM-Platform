"""Unit tests for the PostgreSQL E2E source validation framework.

These tests use mocked catalog data — no live PostgreSQL connection required.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from tests.e2e.postgresql.validation.expected import (
    CHECK_CONSTRAINTS,
    FOREIGN_KEYS,
    FUNCTIONS,
    GRANTS,
    INDEXES,
    PARTITIONS,
    PROCEDURES,
    RLS_POLICIES,
    ROLES,
    ROW_COUNTS,
    SCHEMA_NAME,
    SEQUENCES,
    TABLES,
    TRIGGERS,
    UNIQUE_CONSTRAINTS,
    USER_TYPES,
    VIEWS,
)
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)
from tests.e2e.postgresql.validation.source_validator import SourceValidator


def _full_pass_catalog() -> MagicMock:
    """Return a mock PostgreSQLCatalog where every query returns the expected
    fixture data, so the validator should produce an all-PASS report."""
    cat = MagicMock()
    cat.database_name = "test_db"
    cat.schema_exists.return_value = True
    cat.get_base_tables.return_value = list(TABLES.keys())

    cat.get_columns.side_effect = lambda schema, table: [
        {
            "name": c.name,
            "base_type": c.base_type,
            "is_nullable": c.nullable,
            "size": c.size,
            "precision": c.precision,
            "scale": c.scale,
            "default": getattr(c, "default_value", None),
            "is_identity": c.is_identity,
            "identity_kind": c.identity_kind,
            "identity_seed": c.identity_seed,
            "identity_increment": c.identity_increment,
            "is_generated": c.is_generated,
            "generated_expression": None,
        }
        for c in TABLES[table].columns
    ]

    cat.get_primary_key.side_effect = lambda schema, table: {
        "name": TABLES[table].pk_name,
        "columns": TABLES[table].pk_columns,
    }

    cat.get_foreign_keys.return_value = [
        {
            "name": fk.constraint_name,
            "columns": fk.columns,
            "ref_table": fk.ref_table,
            "ref_schema": SCHEMA_NAME,
            "ref_columns": fk.ref_columns,
            "on_delete": fk.on_delete,
            "on_update": fk.on_update,
        }
        for table_fks in FOREIGN_KEYS.values()
        for fk in table_fks
    ]

    cat.get_unique_constraints.side_effect = lambda schema, table: [
        {"name": uc.name, "columns": uc.columns}
        for uc in UNIQUE_CONSTRAINTS.get(table, [])
    ]

    cat.get_check_constraints.side_effect = lambda schema, table: [
        {"name": n, "definition": "CHECK"}
        for n in CHECK_CONSTRAINTS.get(table, [])
    ]

    cat.get_indexes.side_effect = lambda schema, table: [
        {"name": idx.name, "is_unique": idx.unique, "columns": idx.columns}
        for idx in INDEXES.get(table, [])
    ]

    cat.get_views.return_value = [{"name": v.name, "definition": "SELECT 1"} for v in VIEWS]

    cat.get_functions.return_value = [
        {"name": f_name, "kind": f.kind}
        for f_name, f in list(FUNCTIONS.items()) + [(p, __import__("tests.e2e.postgresql.validation.expected", fromlist=["ExpectedFunction"]).ExpectedFunction(p, "procedure")) for p in PROCEDURES]
    ]

    cat.get_triggers.return_value = [
        {"name": t.name, "table": t.table, "is_disabled": t.is_disabled}
        for t in TRIGGERS.values()
    ]

    cat.get_standalone_sequences.return_value = {
        s_name: {"start_value": meta["start_value"], "increment": meta["increment"],
                 "data_type": meta["data_type"]}
        for s_name, meta in SEQUENCES.items()
    }

    cat.get_partitions.return_value = [
        {"name": p, "parent_table": parent, "schema_name": SCHEMA_NAME,
         "parent_schema": SCHEMA_NAME, "bound_expr": None}
        for parent, parts in PARTITIONS.items()
        for p in parts
    ]

    cat.get_rls_enabled.side_effect = lambda schema, table: TABLES[table].rls_enabled

    cat.get_rls_policies.side_effect = lambda schema, table: [
        {"name": name} for name in RLS_POLICIES.get(table, [])
    ]

    cat.get_roles.return_value = list(ROLES)

    cat.get_grants.return_value = [
        {"grantee": g.grantee, "privilege": g.privilege, "object_type": g.object_type,
         "object_name": g.object_name}
        for g in GRANTS
    ]

    cat.get_comments.return_value = [
        {"object_type": "TABLE", "schema_name": SCHEMA_NAME,
         "object_name": t, "comment": "test comment"}
        for t in ["customers", "orderaudit", "orderdetails", "orders",
                  "pk_name_test", "products"]
        ] + [
        {"object_type": "COLUMN", "schema_name": SCHEMA_NAME,
         "object_name": "customers.email", "comment": "Unique email address."},
        {"object_type": "COLUMN", "schema_name": SCHEMA_NAME,
         "object_name": "orders.ordernumber", "comment": "From seq_ordernumber."},
    ]

    cat.get_row_count.side_effect = lambda schema, table: ROW_COUNTS.get(table, 0)
    cat.count_by_column_value.return_value = ROW_COUNTS["orders"]

    cat.get_user_types.return_value = [{"name": t.name, "kind": t.kind} for t in USER_TYPES.values()]
    return cat


# ============================================================
# Model tests
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
        phase.add_check(CheckResult(name="t2", status=STATUS_FAIL, message="bad"))
        assert phase.status == STATUS_FAIL

    def test_to_dict_structure(self):
        phase = PhaseResult(name="tables")
        phase.add_check(CheckResult(name="t1", status=STATUS_PASS, expected=5, actual=5))
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
# SourceValidator tests with mocked catalog
# ============================================================

class TestSourceValidatorPass:
    """All validation phases should PASS when the mock catalog
    returns exactly the expected fixture data."""

    def test_full_pass(self):
        cat = _full_pass_catalog()
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        assert report.passed, "Expected all checks to pass, got failures"

    def test_all_phases_present(self):
        cat = _full_pass_catalog()
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        phase_names = [p.name for p in report.phases]
        for name in ["database", "schemas", "tables", "columns",
                      "primary_keys", "foreign_keys", "unique_constraints",
                      "check_constraints", "indexes", "views",
                      "functions", "procedures", "triggers", "sequences",
                      "partitions", "user_defined_types", "security",
                      "rls_policies", "grants", "comments",
                      "row_counts", "trigger_data"]:
            assert name in phase_names, f"Missing phase: {name}"

    def test_zero_failures_on_pass(self):
        cat = _full_pass_catalog()
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        assert report.summary["FAIL"] == 0
        assert report.summary["PASS"] > 40


class TestSourceValidatorFailures:
    """When the mock returns wrong data, the validator should report FAIL."""

    def test_missing_table_detected(self):
        cat = _full_pass_catalog()
        cat.get_base_tables.return_value = ["customers", "products"]
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        tables_phase = next(p for p in report.phases if p.name == "tables")
        assert tables_phase.status == STATUS_FAIL

    def test_wrong_row_count_detected(self):
        cat = _full_pass_catalog()
        cat.get_row_count.side_effect = lambda s, t: ROW_COUNTS.get(t, 0) - 1
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        rc_phase = next(p for p in report.phases if p.name == "row_counts")
        assert rc_phase.status == STATUS_FAIL

    def test_wrong_column_type_detected(self):
        cat = _full_pass_catalog()
        cat.get_columns.side_effect = lambda schema, table: [
            {
                "name": c.name,
                "base_type": "wrong_type" if c.name == "customerid" else c.base_type,
                "is_nullable": c.nullable,
                "size": c.size,
                "precision": c.precision,
                "scale": c.scale,
                "default": getattr(c, "default_value", None),
                "is_identity": c.is_identity,
                "identity_kind": c.identity_kind,
                "identity_seed": c.identity_seed,
                "identity_increment": c.identity_increment,
                "is_generated": c.is_generated,
                "generated_expression": None,
            }
            for c in TABLES[table].columns
        ]
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        cols_phase = next(p for p in report.phases if p.name == "columns")
        assert cols_phase.status == STATUS_FAIL

    def test_missing_partition_detected(self):
        cat = _full_pass_catalog()
        cat.get_partitions.return_value = []
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        part_phase = next(p for p in report.phases if p.name == "partitions")
        assert part_phase.status == STATUS_FAIL

    def test_missing_rls_detected(self):
        cat = _full_pass_catalog()
        cat.get_rls_enabled.side_effect = lambda schema, table: False
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        rls_phase = next(p for p in report.phases if p.name == "rls_policies")
        assert rls_phase.status == STATUS_FAIL

    def test_wrong_trigger_action_detected(self):
        cat = _full_pass_catalog()
        cat.count_by_column_value.return_value = 0
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        td_phase = next(p for p in report.phases if p.name == "trigger_data")
        assert td_phase.status == STATUS_FAIL

    def test_wrong_procedure_kind_detected(self):
        cat = _full_pass_catalog()
        cat.get_functions.return_value = [
            {"name": f_name, "kind": "function"}  # all are functions, no procedures
            for f_name in list(FUNCTIONS.keys()) + list(PROCEDURES)
        ]
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        proc_phase = next(p for p in report.phases if p.name == "procedures")
        assert proc_phase.status == STATUS_FAIL

    def test_missing_grant_detected(self):
        cat = _full_pass_catalog()
        cat.get_grants.return_value = []
        validator = SourceValidator(cat, database_name="test_db")
        report = validator.validate()
        grant_phase = next(p for p in report.phases if p.name == "grants")
        assert grant_phase.status == STATUS_FAIL
