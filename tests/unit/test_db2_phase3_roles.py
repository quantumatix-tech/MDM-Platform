"""Phase 3 regression tests: Db2 role discovery, planning, and security parity."""

from core.connectors.db2.objects.role import (
    discover_role_definitions,
    discover_roles,
    plan_role_migration,
    validate_role_identifier,
)


class TestRoleDiscovery:
    def test_discover_roles_returns_list(self):
        conn = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []
        result = discover_roles(conn)
        assert isinstance(result, list)

    def test_discover_roles_parses_memberships(self):
        conn = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        # First call: SYSCAT.ROLES
        # Second call: SYSCAT.ROLEAUTH
        cur.fetchall.side_effect = [
            [("MYROLE",)],
            [("DB2ADMIN", "U", "MYROLE", "Y")],
        ]
        result = discover_roles(conn)
        assert len(result) == 1
        assert result[0]["role"] == "MYROLE"
        assert result[0]["grantee"] == "DB2ADMIN"
        assert result[0]["admin"] is True

    def test_discover_role_definitions(self):
        conn = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [("ROLE_A",), ("ROLE_B",)]
        result = discover_role_definitions(conn)
        assert result == ["ROLE_A", "ROLE_B"]

    def test_discover_roles_handles_empty(self):
        conn = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.side_effect = [[], []]
        result = discover_roles(conn)
        assert result == []


class TestRolePlanning:
    def test_plan_creates_missing_roles(self):
        source = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        target = []
        plan = plan_role_migration(source, target)
        assert "R1" in plan["roles_to_create"]
        assert ("R1", "U1", "U") in plan["memberships_to_grant"]

    def test_plan_detects_existing(self):
        source = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        target = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        plan = plan_role_migration(source, target)
        assert plan["roles_to_create"] == []
        assert plan["memberships_to_grant"] == []
        assert ("R1", "U1", "U") in plan["memberships_existing"]

    def test_plan_detects_excess_on_target(self):
        source = []
        target = [{"role": "R2", "grantee": "U2", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        plan = plan_role_migration(source, target)
        assert "R2" in plan["roles_excess_on_target"]

    def test_plan_detects_admin_option_diff(self):
        source = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": True, "is_defined_role": True}]
        target = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        plan = plan_role_migration(source, target)
        assert len(plan["admin_option_diffs"]) == 1
        assert plan["admin_option_diffs"][0]["source_admin"] is True
        assert plan["admin_option_diffs"][0]["target_admin"] is False

    def test_plan_detects_self_grant_cycle(self):
        source = [{"role": "R1", "grantee": "R1", "grantee_type": "R", "admin": False, "is_defined_role": True}]
        target = []
        plan = plan_role_migration(source, target)
        assert any(c["type"] == "self_grant" for c in plan["conflicts"])

    def test_plan_detects_public_grant(self):
        source = [{"role": "R1", "grantee": "PUBLIC", "grantee_type": "P", "admin": False, "is_defined_role": True}]
        target = []
        plan = plan_role_migration(source, target)
        assert any(c["type"] == "public_grant" for c in plan["conflicts"])

    def test_plan_handles_grantee_types(self):
        source = [
            {"role": "R1", "grantee": "USER1", "grantee_type": "U", "admin": False, "is_defined_role": True},
            {"role": "R1", "grantee": "GROUP1", "grantee_type": "G", "admin": False, "is_defined_role": True},
            {"role": "R1", "grantee": "ROLE2", "grantee_type": "R", "admin": False, "is_defined_role": True},
        ]
        target = []
        plan = plan_role_migration(source, target)
        assert len(plan["memberships_to_grant"]) == 3


class TestRoleValidation:
    def test_valid_role_name(self):
        assert validate_role_identifier("MY_ROLE") is True

    def test_invalid_role_name(self):
        assert validate_role_identifier("123BAD") is False

    def test_empty_role_name(self):
        assert validate_role_identifier("") is False


class TestRoleSecurityParity:
    def test_compare_reports_missing_role(self):
        from unittest.mock import MagicMock

        from core.connectors.db2.objects.role import compare_role_security
        result = MagicMock()
        source = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        target = []
        compare_role_security(source, target, result)
        result.add_mismatch.assert_called()
        # add_warning not expected for missing role (mismatch is correct)

    def test_compare_reports_excess_membership(self):
        from unittest.mock import MagicMock

        from core.connectors.db2.objects.role import compare_role_security
        result = MagicMock()
        source = []
        target = [{"role": "R2", "grantee": "U2", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        compare_role_security(source, target, result)
        result.add_warning.assert_called()

    def test_compare_no_diffs_no_calls(self):
        from unittest.mock import MagicMock

        from core.connectors.db2.objects.role import compare_role_security
        result = MagicMock()
        source = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        target = [{"role": "R1", "grantee": "U1", "grantee_type": "U", "admin": False, "is_defined_role": True}]
        compare_role_security(source, target, result)
        result.add_mismatch.assert_not_called()
        # admin_option_diffs empty, conflicts empty, excess empty -> only warnings for excess/conflicts
        # memberships_to_grant empty -> no mismatch
        # roles_to_create empty -> no mismatch
