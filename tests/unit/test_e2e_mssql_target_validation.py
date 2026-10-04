"""Unit tests for Phase D — target structural validation, comparison,
and functional checks.

Covers PASS cases, missing/unexpected objects, metadata mismatches,
row-count mismatches, functional failures, comparison aggregation,
and report/exit-code behavior.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.e2e.mssql.validation.comparator import (
    ComparisonCategory,
    SourceTargetComparator,
)
from tests.e2e.mssql.validation.functional import FunctionalValidator
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
)
from tests.e2e.mssql.validation.target_validator import TargetValidator

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_catalog(rows_by_query: dict[str, list]) -> MagicMock:
    """Build a mock MSSQLCatalog whose query methods return canned data."""
    cat = MagicMock()
    cat._conn = MagicMock()

    def _rows(sql, *args, **kwargs):
        for key, val in rows_by_query.items():
            if key in sql:
                mock_cur = MagicMock()
                mock_cur.fetchall.return_value = val
                mock_cursor_ctx = MagicMock()
                mock_cursor_ctx.__enter__.return_value = mock_cur
                mock_cursor_ctx.__exit__.return_value = False
                cat._conn.cursor.return_value = mock_cursor_ctx
                return
        return

    return cat


def _mock_cursor_with_rows(rows: list[tuple]):
    """Create a mock cursor context that returns *rows*."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    return ctx


def _make_catalog_mock(methods: dict[str, MagicMock]) -> MagicMock:
    """Build a mock MSSQLCatalog with pre-configured method return values."""
    cat = MagicMock()
    cat._conn = MagicMock()
    for name, mock in methods.items():
        setattr(cat, name, mock)
    return cat


# --------------------------------------------------------------------------- #
# ComparisonCategory
# --------------------------------------------------------------------------- #

class TestComparisonCategory:
    def test_new_category_is_pass(self):
        cat = ComparisonCategory(name="schemas")
        assert cat.status == STATUS_PASS
        assert cat.passed
        assert cat.expected_count == 0
        assert cat.actual_count == 0

    def test_fail_sets_status(self):
        cat = ComparisonCategory(name="tables")
        cat.fail("something broke")
        assert cat.status == STATUS_FAIL
        assert not cat.passed
        assert "something broke" in cat.mismatches


# --------------------------------------------------------------------------- #
# SourceTargetComparator — PASS case
# --------------------------------------------------------------------------- #

class TestComparatorPass:
    def test_compare_pass_when_identical(self):
        src_methods = {
            "get_all_user_schemas": MagicMock(return_value=["training"]),
            "get_base_tables": MagicMock(return_value=["Customers", "Orders"]),
            "get_columns": MagicMock(return_value=[
                {"name": "id", "base_type": "int", "is_nullable": False,
                  "is_identity": True, "is_computed": False, "udt_name": None,
                  "max_length": None, "numeric_precision": None,
                  "numeric_scale": None},
            ]),
            "get_primary_key": MagicMock(
                return_value={"name": "PK_Customers", "columns": ["CustomerID"]}
            ),
            "get_foreign_keys": MagicMock(return_value=[]),
            "get_unique_constraints": MagicMock(return_value=[]),
            "get_check_constraints": MagicMock(return_value=[]),
            "get_default_constraints": MagicMock(return_value=[]),
            "get_indexes": MagicMock(return_value=[]),
            "get_views": MagicMock(return_value={}),
            "get_functions": MagicMock(return_value={}),
            "get_procedures": MagicMock(return_value={}),
            "get_triggers": MagicMock(return_value=[]),
            "get_sequences": MagicMock(return_value={}),
            "get_synonyms": MagicMock(return_value={}),
            "get_user_types": MagicMock(return_value={}),
            "get_partition_functions": MagicMock(return_value=[]),
            "get_partition_schemes": MagicMock(return_value=[]),
            "get_partitioned_tables": MagicMock(return_value=[]),
            "get_security_info": MagicMock(return_value={
                "roles": [], "users": [], "memberships": [], "grants": []
            }),
            "get_extended_properties": MagicMock(return_value=[]),
            "get_row_count": MagicMock(return_value=5),
        }
        src = _make_catalog_mock(src_methods)
        tgt = _make_catalog_mock(src_methods)

        comparator = SourceTargetComparator(src, tgt)
        comparator.compare()

        assert comparator.passed
        assert len(comparator.categories) > 0

    def test_compare_detects_missing_table(self):
        src_methods = {
            "get_all_user_schemas": MagicMock(return_value=["training"]),
            "get_base_tables": MagicMock(return_value=["Customers", "Orders"]),
            "get_columns": MagicMock(return_value=[
                {"name": "id", "base_type": "int", "is_nullable": False,
                 "is_identity": True, "is_computed": False, "udt_name": None,
                 "max_length": None, "numeric_precision": None,
                 "numeric_scale": None},
            ]),
            "get_primary_key": MagicMock(
                return_value={"name": "PK_Customers", "columns": ["CustomerID"]}
            ),
            "get_foreign_keys": MagicMock(return_value=[]),
            "get_unique_constraints": MagicMock(return_value=[]),
            "get_check_constraints": MagicMock(return_value=[]),
            "get_default_constraints": MagicMock(return_value=[]),
            "get_indexes": MagicMock(return_value=[]),
            "get_views": MagicMock(return_value={}),
            "get_functions": MagicMock(return_value={}),
            "get_procedures": MagicMock(return_value={}),
            "get_triggers": MagicMock(return_value=[]),
            "get_sequences": MagicMock(return_value={}),
            "get_synonyms": MagicMock(return_value={}),
            "get_user_types": MagicMock(return_value={}),
            "get_partition_functions": MagicMock(return_value=[]),
            "get_partition_schemes": MagicMock(return_value=[]),
            "get_partitioned_tables": MagicMock(return_value=[]),
            "get_security_info": MagicMock(return_value={
                "roles": [], "users": [], "memberships": [], "grants": []
            }),
            "get_extended_properties": MagicMock(return_value=[]),
            "get_row_count": MagicMock(return_value=5),
        }
        src = _make_catalog_mock(src_methods)

        tgt_methods = dict(src_methods)
        tgt_methods["get_base_tables"] = MagicMock(return_value=["Customers"])
        tgt = _make_catalog_mock(tgt_methods)

        comparator = SourceTargetComparator(src, tgt)
        comparator.compare()

        tables_cat = next(c for c in comparator.categories if c.name == "tables")
        assert not tables_cat.passed
        assert "Orders" in tables_cat.missing

    def test_compare_detects_row_count_mismatch(self):
        src_methods = {
            "get_all_user_schemas": MagicMock(return_value=["training"]),
            "get_base_tables": MagicMock(return_value=["Customers"]),
            "get_columns": MagicMock(return_value=[
                {"name": "id", "base_type": "int", "is_nullable": False,
                 "is_identity": True, "is_computed": False, "udt_name": None,
                 "max_length": None, "numeric_precision": None,
                 "numeric_scale": None},
            ]),
            "get_primary_key": MagicMock(
                return_value={"name": "PK_Customers", "columns": ["CustomerID"]}
            ),
            "get_foreign_keys": MagicMock(return_value=[]),
            "get_unique_constraints": MagicMock(return_value=[]),
            "get_check_constraints": MagicMock(return_value=[]),
            "get_default_constraints": MagicMock(return_value=[]),
            "get_indexes": MagicMock(return_value=[]),
            "get_views": MagicMock(return_value={}),
            "get_functions": MagicMock(return_value={}),
            "get_procedures": MagicMock(return_value={}),
            "get_triggers": MagicMock(return_value=[]),
            "get_sequences": MagicMock(return_value={}),
            "get_synonyms": MagicMock(return_value={}),
            "get_user_types": MagicMock(return_value={}),
            "get_partition_functions": MagicMock(return_value=[]),
            "get_partition_schemes": MagicMock(return_value=[]),
            "get_partitioned_tables": MagicMock(return_value=[]),
            "get_security_info": MagicMock(return_value={
                "roles": [], "users": [], "memberships": [], "grants": []
            }),
            "get_extended_properties": MagicMock(return_value=[]),
            "get_row_count": MagicMock(side_effect=[5, 3]),
        }
        src = _make_catalog_mock(src_methods)
        tgt = _make_catalog_mock(src_methods)

        comparator = SourceTargetComparator(src, tgt)
        comparator.compare()

        rc_cat = next(c for c in comparator.categories if c.name == "row_counts")
        assert not rc_cat.passed
        assert "Customers: src=5, tgt=3" in rc_cat.mismatches

    def test_compare_detects_metadata_mismatch(self):
        src_pk = {"name": "PK_Orders", "columns": ["OrderID"]}
        tgt_pk = {"name": "PK_Orders", "columns": ["OrderID", "Extra"]}

        src_methods = {
            "get_all_user_schemas": MagicMock(return_value=["training"]),
            "get_base_tables": MagicMock(return_value=["Orders"]),
            "get_columns": MagicMock(return_value=[
                {"name": "OrderID", "base_type": "int", "is_nullable": False,
                 "is_identity": True, "is_computed": False, "udt_name": None,
                 "max_length": None, "numeric_precision": None,
                 "numeric_scale": None},
            ]),
            "get_primary_key": MagicMock(return_value=src_pk),
            "get_foreign_keys": MagicMock(return_value=[]),
            "get_unique_constraints": MagicMock(return_value=[]),
            "get_check_constraints": MagicMock(return_value=[]),
            "get_default_constraints": MagicMock(return_value=[]),
            "get_indexes": MagicMock(return_value=[]),
            "get_views": MagicMock(return_value={}),
            "get_functions": MagicMock(return_value={}),
            "get_procedures": MagicMock(return_value={}),
            "get_triggers": MagicMock(return_value=[]),
            "get_sequences": MagicMock(return_value={}),
            "get_synonyms": MagicMock(return_value={}),
            "get_user_types": MagicMock(return_value={}),
            "get_partition_functions": MagicMock(return_value=[]),
            "get_partition_schemes": MagicMock(return_value=[]),
            "get_partitioned_tables": MagicMock(return_value=[]),
            "get_security_info": MagicMock(return_value={
                "roles": [], "users": [], "memberships": [], "grants": []
            }),
            "get_extended_properties": MagicMock(return_value=[]),
            "get_row_count": MagicMock(return_value=5),
        }
        src = _make_catalog_mock(src_methods)
        tgt_methods = dict(src_methods)
        tgt_methods["get_primary_key"] = MagicMock(return_value=tgt_pk)
        tgt = _make_catalog_mock(tgt_methods)

        comparator = SourceTargetComparator(src, tgt)
        comparator.compare()

        pk_cat = next(c for c in comparator.categories if c.name == "primary_keys")
        assert not pk_cat.passed
        assert any("PK_Orders" in m for m in pk_cat.mismatches)

    def test_compare_detects_unexpected_object(self):
        src_methods = {
            "get_all_user_schemas": MagicMock(return_value=["training"]),
            "get_base_tables": MagicMock(return_value=["Customers"]),
            "get_views": MagicMock(return_value={}),
            "get_functions": MagicMock(return_value={}),
            "get_procedures": MagicMock(return_value={}),
            "get_synonyms": MagicMock(return_value={}),
        }
        tgt_methods = dict(src_methods)
        tgt_methods["get_views"] = MagicMock(return_value={"vw_Extra": "SELECT 1"})

        src = _make_catalog_mock(src_methods)
        tgt = _make_catalog_mock(tgt_methods)

        comparator = SourceTargetComparator(src, tgt)
        comparator.compare()

        views_cat = next(c for c in comparator.categories if c.name == "views")
        assert not views_cat.passed
        assert "vw_Extra" in views_cat.unexpected


# --------------------------------------------------------------------------- #
# SourceTargetComparator — report
# --------------------------------------------------------------------------- #

class TestComparatorReport:
    def test_to_report_includes_all_phases(self):
        src = MagicMock()
        tgt = MagicMock()
        src.get_all_user_schemas.return_value = ["training"]
        tgt.get_all_user_schemas.return_value = ["training"]

        comparator = SourceTargetComparator(src, tgt)
        # Manually add a category
        cat = ComparisonCategory(name="schemas")
        cat.expected_count = 1
        cat.actual_count = 1
        comparator._categories.append(cat)

        report = comparator.to_report()
        assert report.passed
        assert any(p.name == "schemas" for p in report.phases)


# --------------------------------------------------------------------------- #
# FunctionalValidator — mocked
# --------------------------------------------------------------------------- #

class TestFunctionalValidator:
    def _make_validator(self, execute_results: dict[str, list]):
        conn = MagicMock()

        def mock_cursor():
            cur = MagicMock()
            ctx = MagicMock()
            ctx.__enter__.return_value = cur
            ctx.__exit__.return_value = False
            cur.fetchall.return_value = execute_results.get("default", [])
            cur.fetchone.return_value = (1,)  # for COUNT(*)
            return ctx

        conn.cursor.side_effect = mock_cursor
        return FunctionalValidator(conn, database_name="TestDB")

    def test_view_readable_passes(self):
        v = self._make_validator({})
        with patch.object(v, "_execute", return_value=[(5,)]):
            report = v.validate()
            view_phase = next(p for p in report.phases if p.name == "functional_view")
            assert view_phase.status == STATUS_PASS

    def test_scalar_function_fails(self):
        v = self._make_validator({})
        with patch.object(
            v, "_execute", side_effect=ValueError("function not found")
        ):
            report = v.validate()
            fn_phase = next(
                p for p in report.phases
                if p.name == "functional_scalar_function"
            )
            assert fn_phase.status == STATUS_FAIL


# --------------------------------------------------------------------------- #
# TargetValidator
# --------------------------------------------------------------------------- #

class TestTargetValidator:
    def test_target_validator_inherits_structural_checks(self):
        """TargetValidator must include all SourceValidator phases."""
        cat = MagicMock()
        cat._conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        ctx.__exit__.return_value = False
        cat._conn.cursor.return_value = ctx

        # Point get_base_tables to expected tables
        from tests.e2e.mssql.validation.expected import TABLES
        cat.get_base_tables.return_value = list(TABLES.keys())
        cat.get_columns.return_value = [
            {"name": "TestID", "base_type": "int", "is_nullable": False,
             "is_identity": True, "is_computed": False, "udt_name": None,
             "max_length": None, "numeric_precision": None,
             "numeric_scale": None}
        ]
        cat.get_primary_key.return_value = {"name": "PK_Test", "columns": ["TestID"]}
        cat.get_foreign_keys.return_value = []
        cat.get_unique_constraints.return_value = []
        cat.get_check_constraints.return_value = []
        cat.get_default_constraints.return_value = []
        cat.get_indexes.return_value = []
        cat.get_views.return_value = {}
        cat.get_functions.return_value = {}
        cat.get_procedures.return_value = {}
        cat.get_triggers.return_value = []
        cat.get_sequences.return_value = {}
        cat.get_synonyms.return_value = {}
        cat.get_user_types.return_value = {}
        cat.get_partition_functions.return_value = []
        cat.get_partition_schemes.return_value = []
        cat.get_partitioned_tables.return_value = []
        cat.get_security_info.return_value = {
            "roles": [], "users": [], "memberships": [], "grants": []
        }
        cat.get_extended_properties.return_value = []
        cat.get_row_count.return_value = 2
        cat.schema_exists.return_value = True
        cat.database_exists.return_value = True
        cat.count_by_column_value.return_value = 5

        # Mock the trigger data check
        with patch(
            "tests.e2e.mssql.validation.source_validator.STATUS_PASS"
        ):
            validator = TargetValidator(cat, database_name="TestDB")
            report = validator.validate()

        phase_names = [p.name for p in report.phases]
        assert "target_safety" in phase_names
        assert "target_connectivity" in phase_names
        assert "tables" in phase_names
        assert "columns" in phase_names
        assert "primary_keys" in phase_names

    def test_target_validator_flags_protected_database(self):
        cat = MagicMock()
        cat._conn = MagicMock()

        with patch(
            "tests.e2e.mssql.validation.target_validator.is_protected_database",
            return_value=True,
        ):
            validator = TargetValidator(cat, database_name="master")
            report = validator.validate()

        safety_phase = next(p for p in report.phases if p.name == "target_safety")
        assert safety_phase.status == STATUS_FAIL

    def test_target_validator_connectivity_passes(self):
        cat = MagicMock()
        cat._conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [(1,), ("TestDB",), ("ONLINE",)]
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        ctx.__exit__.return_value = False
        cat._conn.cursor.return_value = ctx

        with patch(
            "tests.e2e.mssql.validation.target_validator.is_protected_database",
            return_value=False,
        ):
            validator = TargetValidator(cat, database_name="TestDB")
            report = validator.validate()

        conn_phase = next(p for p in report.phases if p.name == "target_connectivity")
        assert conn_phase.status == STATUS_PASS
