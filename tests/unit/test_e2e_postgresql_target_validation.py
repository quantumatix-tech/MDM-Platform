"""Unit tests for the PostgreSQL E2E Phase D validation framework.

Tests the three Phase D layers using mocked catalog data and mocked database
connections — no live PostgreSQL connection required.

Covers:
  - TargetValidator: structural validation of the target database
  - SourceTargetComparator: source-vs-target comparison logic
  - FunctionalValidator: behavioral checks (views, functions, procedures,
    triggers, FKs, CHECK constraints, identities, sequences, partitions,
    enums, RLS)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.e2e.postgresql.validation.comparator import (
    ComparisonCategory,
    SourceTargetComparator,
)
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
from tests.e2e.postgresql.validation.functional import FunctionalValidator
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    ValidationReport,
)
from tests.e2e.postgresql.validation.target_validator import TargetValidator

# ============================================================
# Mock helpers
# ============================================================

def _mock_columns(table_name: str) -> list[dict]:
    """Build mock column dicts matching the catalog's expected shape."""
    tbl = TABLES[table_name]
    return [
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
            "udt_name": c.udt_name if c.udt_name else None,
        }
        for c in tbl.columns
    ]


def _mock_foreign_keys() -> list[dict]:
    return [
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


def _mock_indexes(schema: str, table_name: str) -> list[dict]:
    return [
        {"name": idx.name, "is_unique": idx.unique, "columns": idx.columns}
        for idx in INDEXES.get(table_name, [])
    ]


def _mock_partitions() -> list[dict]:
    return [
        {
            "schema_name": SCHEMA_NAME, "name": p,
            "parent_schema": SCHEMA_NAME,
            "parent_table": parent,
            "bound_expr": "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
        }
        for parent, parts in PARTITIONS.items()
        for p in parts
    ]


def _mock_grants() -> list[dict]:
    return [
        {
            "grantee": g.grantee, "privilege": g.privilege,
             "object_type": g.object_type, "object_name": g.object_name,
        }
        for g in GRANTS
    ]


def _make_mock_catalog(**overrides) -> MagicMock:
    """Return a mock PostgreSQLCatalog with expected fixture data."""
    cat = MagicMock()
    cat._conn = MagicMock()
    cat.database_name = "MigrationE2E_PostgreSQL_Target"

    cat._conn.cursor.return_value.__enter__ = MagicMock(
        return_value=MagicMock(fetchone=MagicMock(return_value=("MigrationE2E_PostgreSQL_Target",)))
    )
    cat._conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cat.schema_exists.return_value = True
    cat.get_base_tables.return_value = list(TABLES.keys())
    cat.get_user_types.return_value = [{"name": t.name, "kind": t.kind} for t in USER_TYPES.values()]
    cat.get_enum_labels.return_value = ["pending", "processing", "shipped"]
    cat.get_row_count.side_effect = lambda schema, table: ROW_COUNTS.get(table, 0)
    cat.get_columns.side_effect = lambda schema, table: _mock_columns(table)
    cat.get_rls_enabled.side_effect = lambda schema, table: TABLES[table].rls_enabled
    cat.get_roles.return_value = list(ROLES)
    cat.get_all_schemas.return_value = [SCHEMA_NAME, "public"]
    cat.get_all_tables.return_value = list(TABLES.keys())
    cat.get_views.return_value = [{"name": v.name, "definition": "SELECT 1"} for v in VIEWS]
    cat.get_functions.return_value = [
        {"name": f_name, "kind": f.kind} for f_name, f in FUNCTIONS.items()
    ] + [
        {"name": p_name, "kind": "procedure"} for p_name in PROCEDURES
    ]
    cat.get_routines.return_value = [
        {"schema": SCHEMA_NAME, "name": f_name, "kind": f.kind,
         "arg_types": "p_id integer", "return_type": "void", "language": "plpgsql"}
        for f_name, f in FUNCTIONS.items()
    ] + [
        {"schema": SCHEMA_NAME, "name": p_name, "kind": "procedure",
         "arg_types": "p_id integer", "return_type": "void", "language": "plpgsql"}
        for p_name in PROCEDURES
    ]
    cat.get_triggers.return_value = [
        {"name": t.name, "table": t.table, "is_disabled": t.is_disabled,
         "timing": "AFTER", "event": "INSERT", "granularity": "ROW"}
        for t in TRIGGERS.values()
    ]
    cat.get_standalone_sequences.return_value = {
        s_name: {"data_type": meta["data_type"], "start_value": meta["start_value"],
                 "increment": meta["increment"]}
        for s_name, meta in SEQUENCES.items()
    }
    cat.get_primary_key.side_effect = lambda schema, table: {
        "name": TABLES[table].pk_name,
        "columns": TABLES[table].pk_columns,
    }
    cat.get_foreign_keys.return_value = _mock_foreign_keys()
    cat.get_unique_constraints.side_effect = lambda schema, table: [
        {"name": uc.name, "columns": uc.columns}
        for uc in UNIQUE_CONSTRAINTS.get(table, [])
    ]
    cat.get_check_constraints.side_effect = lambda schema, table: [
        {"name": n, "definition": "CHECK"}
        for n in CHECK_CONSTRAINTS.get(table, [])
    ]
    cat.get_indexes.side_effect = _mock_indexes
    cat.get_partitions.return_value = _mock_partitions()
    cat.get_partitioned_tables.return_value = [
        {"table": "partitionedorders", "strategy": "RANGE",
         "partition_key": "ORDER BY (orderdate)"},
    ]
    cat.get_rls_policies.side_effect = lambda schema, table: [
        {"name": name} for name in RLS_POLICIES.get(table, [])
    ]
    cat.get_grants.return_value = _mock_grants()
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
    cat.count_by_column_value.return_value = ROW_COUNTS.get("orders", 0)

    for key, value in overrides.items():
        setattr(cat, key, value)

    return cat


def _make_mock_conn():
    """Create a mock psycopg connection with a default cursor."""
    conn = MagicMock()
    conn.autocommit = False

    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


# ============================================================
# TargetValidator tests
# ============================================================

class TestTargetValidator:
    """Tests for the target structural validator."""

    def test_target_validator_inherits_source_validator(self):
        from tests.e2e.postgresql.validation.source_validator import SourceValidator
        assert issubclass(TargetValidator, SourceValidator)

    def test_full_pass(self):
        """TargetValidator should pass all checks when catalog returns expected data."""
        cat = _make_mock_catalog()
        validator = TargetValidator(cat, database_name="MigrationE2E_PostgreSQL_Target")
        report = validator.validate()
        assert report.passed, "Expected all target validation checks to pass"

    def test_target_safety_check_exists(self):
        """TargetValidator should include a target_safety phase."""
        cat = _make_mock_catalog()
        validator = TargetValidator(cat, database_name="MigrationE2E_PostgreSQL_Target")
        report = validator.validate()
        phase_names = [p.name for p in report.phases]
        assert "target_safety" in phase_names

    def test_target_connectivity_check_exists(self):
        """TargetValidator should include a target_connectivity phase."""
        cat = _make_mock_catalog()
        validator = TargetValidator(cat, database_name="MigrationE2E_PostgreSQL_Target")
        report = validator.validate()
        phase_names = [p.name for p in report.phases]
        assert "target_connectivity" in phase_names

    def test_skip_connectivity(self):
        """When skip_connectivity=True, target_safety and target_connectivity should be absent."""
        cat = _make_mock_catalog()
        validator = TargetValidator(
            cat, database_name="MigrationE2E_PostgreSQL_Target", skip_connectivity=True
        )
        report = validator.validate()
        phase_names = [p.name for p in report.phases]
        assert "target_safety" not in phase_names
        assert "target_connectivity" not in phase_names

    def test_target_inherits_all_structural_phases(self):
        """TargetValidator should run all source structural phases."""
        cat = _make_mock_catalog()
        validator = TargetValidator(cat, database_name="MigrationE2E_PostgreSQL_Target")
        report = validator.validate()
        phase_names = [p.name for p in report.phases]
        for name in ["schemas", "tables", "columns", "primary_keys", "foreign_keys",
                      "unique_constraints", "check_constraints", "indexes", "views",
                      "functions", "procedures", "triggers", "sequences",
                      "partitions", "user_defined_types", "security",
                      "rls_policies", "grants", "comments", "row_counts",
                      "trigger_data"]:
            assert name in phase_names, f"Missing phase: {name}"


# ============================================================
# SourceTargetComparator tests
# ============================================================

class TestComparatorNormalization:
    """Tests for DDL normalization in the comparator."""

    def test_normalize_strips_comments(self):
        result = SourceTargetComparator._normalize_ddl("SELECT 1 -- comment\n")
        assert "--" not in result

    def test_normalize_collapses_whitespace(self):
        result = SourceTargetComparator._normalize_ddl("SELECT  1   FROM  foo")
        assert "  " not in result

    def test_normalize_strips_quotes(self):
        result = SourceTargetComparator._normalize_ddl('"schema"."table"')
        assert '"' not in result
        assert "schema.table" in result

    def test_normalize_lowercases(self):
        result = SourceTargetComparator._normalize_ddl("SELECT MyTable")
        assert result == "select mytable"


class TestComparator:
    """Tests for the source-vs-target comparator."""

    def test_all_categories_present(self):
        """Comparator should produce all expected comparison categories."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        names = [c.name for c in comp.categories]
        for expected in ["schemas", "tables", "columns", "primary_keys",
                          "foreign_keys", "unique_constraints", "check_constraints",
                          "indexes", "views", "routines", "triggers", "sequences",
                          "user_types", "partitions", "partitioned_tables",
                          "security", "rls_policies", "grants", "comments",
                          "row_counts"]:
            assert expected in names, f"Missing category: {expected}"

    def test_full_match_passes(self):
        """Comparator should pass when source and target are identical."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        assert comp.passed

    def test_missing_table_detected(self):
        """Comparator should detect missing tables in target."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()
        tgt.get_base_tables.return_value = ["customers", "products"]
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        assert not comp.passed
        tables_cat = next(c for c in comp.categories if c.name == "tables")
        assert tables_cat.status == STATUS_FAIL
        assert "orders" in tables_cat.missing

    def test_row_count_mismatch_detected(self):
        """Comparator should detect row count differences."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()

        def src_count(schema, table):
            return ROW_COUNTS.get(table, 0)

        def tgt_count(schema, table):
            return ROW_COUNTS.get(table, 0) - 1

        src.get_row_count.side_effect = src_count
        tgt.get_row_count.side_effect = tgt_count
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        assert not comp.passed
        rc_cat = next(c for c in comp.categories if c.name == "row_counts")
        assert rc_cat.status == STATUS_FAIL

    def test_routine_signature_compared(self):
        """Routines should be compared by signature (name + arg_types)."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()
        tgt.get_routines.return_value = [
            {"schema": SCHEMA_NAME, "name": "fn_getordertotal", "kind": "function",
             "arg_types": "p_order_id integer", "return_type": "numeric",
             "language": "plpgsql"},
        ]
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        assert not comp.passed
        routines_cat = next(c for c in comp.categories if c.name == "routines")
        assert routines_cat.status == STATUS_FAIL

    def test_partitions_compared_with_partition_filter(self):
        """Partition comparison should use pg_inherits + relkind filter (no indexes)."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        assert comp.passed
        part_cat = next(c for c in comp.categories if c.name == "partitions")
        assert part_cat.status == STATUS_PASS

    def test_to_report_returns_validation_report(self):
        """to_report() should return a ValidationReport with all categories."""
        src = _make_mock_catalog()
        tgt = _make_mock_catalog()
        comp = SourceTargetComparator(src, tgt)
        comp.compare()
        report = comp.to_report()
        assert isinstance(report, ValidationReport)
        assert len(report.phases) == len(comp.categories)


# ============================================================
# FunctionalValidator tests (fully mocked)
# ============================================================

class TestFunctionalValidator:
    """Tests for the functional validator using a mocked connection."""

    @pytest.fixture
    def mock_setup(self):
        conn, cur = _make_mock_conn()
        validator = FunctionalValidator(conn, database_name="test_target")
        return conn, cur, validator

    def test_view_validation_pass(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(5,)]
        validator._validate_view()
        phases = [p for p in validator._report.phases if p.name == "functional_view"]
        assert len(phases) == 1
        assert phases[0].status == STATUS_PASS

    def test_view_validation_error_fails(self, mock_setup):
        import psycopg.errors
        _conn, cur, validator = mock_setup
        cur.fetchall.side_effect = psycopg.errors.UndefinedTable(
            "relation does not exist"
        )
        validator._validate_view()
        phases = [p for p in validator._report.phases if p.name == "functional_view"]
        assert phases[0].status == STATUS_FAIL

    def test_scalar_function_pass(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(77400.00,)]
        validator._validate_scalar_function()
        phases = [p for p in validator._report.phases if p.name == "functional_scalar_function"]
        assert phases[0].status == STATUS_PASS

    def test_scalar_function_wrong_value_fails(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(0.00,)]
        validator._validate_scalar_function()
        phases = [p for p in validator._report.phases if p.name == "functional_scalar_function"]
        assert phases[0].status == STATUS_FAIL

    def test_procedure_executes_with_zero_change(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(50,)]
        validator._validate_procedure()
        phases = [p for p in validator._report.phases if p.name == "functional_procedure"]
        assert phases[0].status == STATUS_PASS

    def test_trigger_fires_on_insert(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            None,  # INSERT
            None,  # SELECT audit count
            None,  # ROLLBACK
        ]
        ctx_cur.fetchone.return_value = (1,)
        validator._validate_trigger()
        phases = [p for p in validator._report.phases if p.name == "functional_trigger"]
        assert phases[0].status == STATUS_PASS

    def test_trigger_no_audit_row_fails(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            None,  # INSERT
            None,  # SELECT audit count
            None,  # ROLLBACK
        ]
        ctx_cur.fetchone.return_value = (0,)
        validator._validate_trigger()
        phases = [p for p in validator._report.phases if p.name == "functional_trigger"]
        assert phases[0].status == STATUS_FAIL

    def test_foreign_key_rejects_invalid(self, mock_setup):
        import psycopg.errors
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            psycopg.errors.ForeignKeyViolation("fk violation"),
            None,  # ROLLBACK (in except handler)
        ]
        validator._validate_foreign_key()
        phases = [p for p in validator._report.phases if p.name == "functional_foreign_key"]
        assert phases[0].status == STATUS_PASS

    def test_foreign_key_passes_when_no_violation(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            None,  # INSERT succeeds (no violation)
            None,  # ROLLBACK
        ]
        validator._validate_foreign_key()
        phases = [p for p in validator._report.phases if p.name == "functional_foreign_key"]
        assert phases[0].status == STATUS_FAIL

    def test_check_constraint_rejects_invalid(self, mock_setup):
        import psycopg.errors
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            psycopg.errors.CheckViolation("check violation"),
            None,  # ROLLBACK
        ]
        validator._validate_check_constraint()
        phases = [p for p in validator._report.phases if p.name == "functional_check_constraint"]
        assert phases[0].status == STATUS_PASS

    def test_check_constraint_passes_when_no_violation(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            None,  # INSERT succeeds
            None,  # ROLLBACK
        ]
        validator._validate_check_constraint()
        phases = [p for p in validator._report.phases if p.name == "functional_check_constraint"]
        assert phases[0].status == STATUS_FAIL

    def test_identity_generates_value(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            None,  # INSERT ... RETURNING
            None,  # ROLLBACK
        ]
        ctx_cur.fetchone.return_value = (42,)
        validator._validate_identity()
        phases = [p for p in validator._report.phases if p.name == "functional_identity"]
        assert phases[0].status == STATUS_PASS

    def test_sequence_next_value(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(1001,)]
        validator._validate_sequence()
        phases = [p for p in validator._report.phases if p.name == "functional_sequence"]
        assert phases[0].status == STATUS_PASS

    def test_partition_routing(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.execute.side_effect = [
            None,  # BEGIN
            None,  # INSERT ... RETURNING
            None,  # SELECT partition existence
            None,  # SELECT COUNT in partition
            None,  # ROLLBACK
        ]
        ctx_cur.fetchone.side_effect = [
            MagicMock(),  # RETURNING partitionid
            MagicMock(),  # partition existence -> not None
            (1,),  # COUNT -> 1
        ]
        validator._validate_partition_routing()
        phases = [p for p in validator._report.phases if p.name == "functional_partition_routing"]
        assert phases[0].status == STATUS_PASS

    def test_enum_values_valid(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(1,)]
        validator._validate_enum()
        phases = [p for p in validator._report.phases if p.name == "functional_enum"]
        assert phases[0].status == STATUS_PASS

    def test_rls_policy_restricts(self, mock_setup):
        conn, _cur, validator = mock_setup
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        # _execute uses fetchall for the RLS check query
        ctx_cur.fetchall.side_effect = [[(True,)]]
        ctx_cur.execute.side_effect = [
            None,  # _execute (RLS check query)
            None,  # BEGIN
            None,  # SET LOCAL ROLE migration_role
            None,  # SET LOCAL Mumbai
            None,  # SELECT COUNT
            None,  # SET LOCAL NonExistentCity
            None,  # SELECT COUNT
            None,  # ROLLBACK
        ]
        ctx_cur.fetchone.side_effect = [(5,), (0,)]
        validator._validate_rls()
        phases = [p for p in validator._report.phases if p.name == "functional_rls"]
        assert phases[0].status == STATUS_PASS

    def test_rls_disabled_fails(self, mock_setup):
        _conn, cur, validator = mock_setup
        cur.fetchall.return_value = [(False,)]
        validator._validate_rls()
        phases = [p for p in validator._report.phases if p.name == "functional_rls"]
        assert phases[0].status == STATUS_FAIL

    def test_full_validate_returns_report(self, mock_setup):
        conn, cur, validator = mock_setup
        # Set up defaults that will work for all phases
        cur.fetchall.return_value = [(0,)]  # default for view count
        cur.fetchone.return_value = (0,)  # default for single-value selects
        ctx_cur = conn.cursor.return_value.__enter__.return_value
        ctx_cur.fetchall.return_value = [(0,)]
        ctx_cur.fetchone.return_value = (0,)
        ctx_cur.execute.side_effect = None  # no side effect, just returns MagicMock
        report = validator.validate()
        assert isinstance(report, ValidationReport)
        assert len(report.phases) >= 10
        phase_names = [p.name for p in report.phases]
        assert "functional_view" in phase_names
        assert "functional_scalar_function" in phase_names
        assert "functional_procedure" in phase_names
        assert "functional_rls" in phase_names


# ============================================================
# ComparatorCategory tests
# ============================================================

class TestComparisonCategory:
    """Tests for the ComparisonCategory dataclass."""

    def test_default_status_is_pass(self):
        cat = ComparisonCategory(name="test")
        assert cat.status == STATUS_PASS
        assert cat.passed is True

    def test_fail_sets_status(self):
        cat = ComparisonCategory(name="test")
        cat.fail("some reason")
        assert cat.status == STATUS_FAIL
        assert cat.passed is False

    def test_fail_with_reason_adds_mismatch(self):
        cat = ComparisonCategory(name="test")
        cat.fail("bad data")
        assert "bad data" in cat.mismatches

    def test_empty_mismatches_passes(self):
        cat = ComparisonCategory(name="test")
        assert cat.passed is True
        assert cat.mismatches == []
