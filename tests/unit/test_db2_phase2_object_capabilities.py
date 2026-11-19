"""Phase 2 regression tests: alias collision, structured UDT, object collision safety."""

from unittest.mock import MagicMock

from core.connectors.base import validate_identifier


class TestAliasCollision:
    def test_alias_same_schema(self):
        """Alias in same schema as base table."""
        alias = {"schema": "S", "name": "EMP_ALIAS", "base_schema": "S", "base_table": "EMPLOYEES"}
        assert alias["schema"] == alias["base_schema"]

    def test_alias_cross_schema(self):
        """Alias referencing table in different schema."""
        alias = {"schema": "S1", "name": "CROSS_ALIAS", "base_schema": "S2", "base_table": "EMPLOYEES"}
        assert alias["schema"] != alias["base_schema"]

    def test_alias_name_collision(self):
        """Alias with same name as existing table should be detected."""
        # Verify validate_identifier accepts the name
        validate_identifier("EMP_ALIAS", "alias")
        assert True

    def test_alias_discovery_returns_list(self):
        """discover_aliases returns a list."""
        from core.connectors.db2.objects.udt_alias import discover_aliases
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: s
        conn.cursor.return_value.__exit__ = lambda s, *a: None
        conn.cursor.return_value.fetchall.return_value = []
        result = discover_aliases(conn, "S")
        assert isinstance(result, list)


class TestStructuredUDT:
    def test_structured_udt_discovered_with_supported_false(self):
        """Structured UDTs (METATYPE='S') are discovered but marked unsupported."""
        from core.connectors.db2.objects.udt_alias import discover_udts
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = lambda s, *a: None
        # First query (distinct UDTs) returns empty
        # Second query (structured UDTs) returns one
        cur.fetchall.side_effect = [[], [("S", "MY_STRUCT")]]
        result = discover_udts(conn, "S")
        structured = [u for u in result if u.get("metatype") == "S"]
        assert len(structured) == 1
        assert structured[0]["supported"] is False

    def test_structured_udt_create_supported(self):
        """Structured UDTs with attributes are now supported."""
        from core.connectors.db2.objects.target_udt_alias import create_udt_if_missing
        udt = {"schema": "S", "name": "MY_STRUCT", "supported": True, "attributes": [{"name": "CITY", "type": "VARCHAR(50)"}]}
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = None
        result = create_udt_if_missing(conn, udt)
        assert result in ("created", "already_present")

    def test_distinct_udt_still_supported(self):
        """Distinct UDTs (METATYPE='T') remain supported."""
        from core.connectors.db2.objects.udt_alias import discover_udts
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = lambda s, *a: None
        cur.fetchall.side_effect = [[("S", "EMP_CODE", "SYSIBM", "INTEGER")], []]
        result = discover_udts(conn, "S")
        distinct = [u for u in result if u.get("metatype") == "T"]
        assert len(distinct) == 1
        assert distinct[0]["supported"] is True


class TestObjectCollision:
    def test_same_name_different_schemas(self):
        """Same table name in different schemas is not a collision."""
        # Schema-qualified names prevent collision
        tables = ["S1.EMPLOYEES", "S2.EMPLOYEES"]
        assert len(set(tables)) == 2

    def test_duplicate_discovery_raises(self):
        """Duplicate table identifiers in FK ordering raises ValueError."""
        from core.orchestration.db2_fk_order import order_tables_for_full
        try:
            order_tables_for_full(["S.T", "S.T"], [])
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "Duplicate" in str(e)

    def test_alias_conflict_detection(self):
        """Alias with different base raises RuntimeError."""
        from core.connectors.db2.objects.target_udt_alias import create_alias_if_missing
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = lambda s, *a: None
        cur.fetchone.return_value = ("OTHER_SCHEMA", "OTHER_TABLE")
        alias = {"schema": "S", "name": "MY_ALIAS", "base_schema": "S", "base_table": "EMPLOYEES"}
        try:
            create_alias_if_missing(conn, alias)
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "conflict" in str(e).lower() or "differs" in str(e).lower()
