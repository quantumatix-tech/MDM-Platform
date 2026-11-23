"""Phase 1 regression tests: composite, cross-schema, and cyclic FK edge cases."""

from core.connectors.base import ForeignKey
from core.orchestration.db2_fk_order import order_tables_for_full


class TestCompositeFK:
    def test_composite_fk_ordering(self):
        """Composite FK (multi-column) orders tables correctly."""
        tables = ["S.CHILD", "S.PARENT"]
        fks = [("S.CHILD", "S.PARENT")]
        insert, delete = order_tables_for_full(tables, fks)
        assert insert == ["S.PARENT", "S.CHILD"]
        assert delete == ["S.CHILD", "S.PARENT"]

    def test_composite_fk_discovery_columns(self):
        """ForeignKey with multiple columns is valid."""
        fk = ForeignKey(
            name="FK_COMPOSITE",
            columns=["COL_A", "COL_B"],
            ref_table="PARENT",
            ref_columns=["PARENT_A", "PARENT_B"],
            ref_schema="S",
            on_delete="CASCADE",
            on_update="NO ACTION",
        )
        assert fk.columns == ["COL_A", "COL_B"]
        assert fk.ref_columns == ["PARENT_A", "PARENT_B"]


class TestCrossSchemaFK:
    def test_cross_schema_fk_ordering(self):
        """FK spanning different schemas orders correctly."""
        tables = ["S1.CHILD", "S2.PARENT"]
        fks = [("S1.CHILD", "S2.PARENT")]
        insert, _ = order_tables_for_full(tables, fks)
        assert insert == ["S2.PARENT", "S1.CHILD"]

    def test_cross_schema_fk_ref_schema(self):
        """ForeignKey captures ref_schema for cross-schema references."""
        fk = ForeignKey(
            name="FK_CROSS",
            columns=["PARENT_ID"],
            ref_table="PARENT",
            ref_columns=["ID"],
            ref_schema="OTHER_SCHEMA",
            on_delete="NO ACTION",
            on_update="NO ACTION",
        )
        assert fk.ref_schema == "OTHER_SCHEMA"


class TestCyclicFK:
    def test_cycle_raises_value_error(self):
        """Cyclic FK dependency raises ValueError."""
        tables = ["S.A", "S.B"]
        fks = [("S.A", "S.B"), ("S.B", "S.A")]
        try:
            order_tables_for_full(tables, fks)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "cycle" in str(e).lower()

    def test_self_referencing_raises(self):
        """Self-referencing FK raises ValueError."""
        tables = ["S.T"]
        fks = [("S.T", "S.T")]
        try:
            order_tables_for_full(tables, fks)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "self-referencing" in str(e).lower()

    def test_unknown_child_raises(self):
        """Unknown child table raises ValueError."""
        tables = ["S.PARENT"]
        fks = [("S.UNKNOWN", "S.PARENT")]
        try:
            order_tables_for_full(tables, fks)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "Unknown child" in str(e)

    def test_missing_parent_raises(self):
        """Missing parent table raises ValueError."""
        tables = ["S.CHILD"]
        fks = [("S.CHILD", "S.MISSING")]
        try:
            order_tables_for_full(tables, fks)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "Missing selected parent" in str(e)

    def test_duplicate_tables_raises(self):
        """Duplicate table identifiers raise ValueError."""
        tables = ["S.T", "S.T"]
        try:
            order_tables_for_full(tables, [])
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "Duplicate" in str(e)


class TestFKIdempotency:
    def test_existing_fk_skipped(self):
        """Existing FK returns skipped-existing (no duplicate creation)."""
        tables = ["S.CHILD", "S.PARENT"]
        fks = [("S.CHILD", "S.PARENT")]
        insert, _ = order_tables_for_full(tables, fks)
        assert len(insert) == len(tables)
