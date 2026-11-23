"""Source-metadata-driven Oracle permission migration contract tests."""
from unittest.mock import MagicMock

import pytest

from core.connectors.base import GrantDef, RoleDef, RoleMembershipDef, UserDef
from core.connectors.oracle import permissions
from core.connectors.oracle.source import OracleSourceConnector


def _policy(**updates):
    return {"enabled": True, **updates}


def test_permission_inventory_comparison_covers_graph_and_owner_mapping():
    source = {
        "users": [UserDef("APP_USER")], "roles": [RoleDef("READ_ROLE")],
        "memberships": [RoleMembershipDef("APP_USER", "READ_ROLE")],
        "grants": [
            GrantDef("SELECT", "TABLE", "DATA_T", "READ_ROLE", "SRC_OWNER"),
            GrantDef("CREATE SESSION", "SYSTEM", "*", "APP_USER", "SRC_OWNER"),
        ],
    }
    target = {
        "users": [UserDef("APP_USER")], "roles": [RoleDef("READ_ROLE")],
        "memberships": [RoleMembershipDef("APP_USER", "READ_ROLE")],
        "grants": [
            GrantDef("SELECT", "TABLE", "DATA_T", "READ_ROLE", "TGT_OWNER"),
            GrantDef("CREATE SESSION", "SYSTEM", "*", "APP_USER", "TGT_OWNER"),
        ],
    }
    result = permissions.compare_inventories(
        source, target, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
    )
    assert result["match"] is True
    assert all(component["match"] for component in result["components"].values())


def test_permission_inventory_comparison_reports_missing_and_target_only_graph_edges():
    source = {
        "users": [UserDef("APP_USER")], "roles": [RoleDef("READ_ROLE")],
        "memberships": [RoleMembershipDef("APP_USER", "READ_ROLE")],
        "grants": [GrantDef("SELECT", "TABLE", "DATA_T", "READ_ROLE", "SRC_OWNER")],
    }
    target = {
        "users": [UserDef("APP_USER"), UserDef("TARGET_ONLY")],
        "roles": [RoleDef("READ_ROLE")], "memberships": [],
        "grants": [],
    }
    result = permissions.compare_inventories(
        source, target, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
    )
    assert result["match"] is False
    assert result["components"]["memberships"]["missing"]
    assert result["components"]["grants"]["missing"]
    assert result["components"]["users"]["target_only"]


def _tab_grant(grantee, privilege, *, table="P8_S40_DATA", owner="SRC_OWNER",
               grantable="NO", object_type="TABLE", common="NO", inherited="NO"):
    return (
        grantee, owner, table, owner, privilege, grantable, "NO", common,
        object_type, inherited,
    )


def _column_grant(grantee, privilege, *, table="P8_S40_DATA", column="VALUE",
                  owner="SRC_OWNER", grantable="NO", common="NO", inherited="NO"):
    return (
        grantee, owner, table, column, owner, privilege, grantable, common, inherited,
    )


def _source_connection(*, overrides=None, failure=None, memberships=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    overrides = overrides or {}
    edges = memberships if memberships is not None else [
        ("SRC_APP", "SRC_READ", "NO", "YES", "NO", "NO"),
        ("SRC_READ", "SRC_NESTED", "NO", "YES", "NO", "NO"),
        ("OTHER_USER", "UNRELATED_ROLE", "NO", "YES", "NO", "NO"),
    ]

    def fetchall():
        sql = str(cur.execute.call_args.args[0]).upper()
        binds = cur.execute.call_args.args[1] if len(cur.execute.call_args.args) > 1 else ()
        if failure and failure in sql:
            raise PermissionError("catalog access denied")
        if "SELECT 1 FROM" in sql:
            return []
        for needle, rows in overrides.items():
            if needle in sql:
                return rows(binds) if callable(rows) else rows
        if "FROM ALL_TAB_PRIVS_MADE" in sql:
            return [
                _tab_grant("SRC_APP", "SELECT"),
                _tab_grant("SRC_READ", "INSERT"),
            ]
        if "FROM ALL_COL_PRIVS_MADE" in sql:
            return [_column_grant("SRC_APP", "UPDATE")]
        if "FROM DBA_ROLE_PRIVS" in sql:
            return edges
        if "FROM DBA_USERS" in sql:
            if not binds:
                return []
            name = binds[0]
            if name == "SYS":
                return [(name, "Y", "YES")]
            return [(name, "N", "NO")] if name in {"SRC_APP", "OTHER_USER", "P8_S40_SRC_APP"} else []
        if "FROM DBA_ROLES" in sql:
            if not binds:
                return []
            name = binds[0]
            role = {"SRC_READ", "SRC_NESTED", "UNRELATED_ROLE", "P8_S40_SRC_ROLE"}
            return [(name, "N", "NO", "NONE")] if name in role else []
        if "FROM DBA_SYS_PRIVS" in sql:
            return []
        return []

    cur.fetchall.side_effect = fetchall
    return conn, cur


def _source_inventory(policy=None, **kwargs):
    conn, _ = _source_connection(**kwargs)
    return permissions.discover(conn, "SRC_OWNER", policy or _policy())


def _target_connection(*, missing_users=(), missing_roles=(), conflicting_users=(),
                       conflicting_roles=(), authority=True, overrides=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    missing_users, missing_roles = set(missing_users), set(missing_roles)
    conflicting_users, conflicting_roles = set(conflicting_users), set(conflicting_roles)
    overrides = overrides or {}

    def fetchall():
        sql = str(cur.execute.call_args.args[0]).upper()
        binds = cur.execute.call_args.args[1] if len(cur.execute.call_args.args) > 1 else ()
        name = binds[0] if binds else None
        for needle, rows in overrides.items():
            if needle in sql:
                return rows(binds) if callable(rows) else rows
        if "FROM DBA_USERS" in sql:
            if name in conflicting_users:
                return [(name, "N", "NO", "OPEN")]
            if name in missing_users:
                return []
            if name == "SRC_APP" or name == "OTHER_USER":
                return [(name, "N", "NO", "OPEN")]
            return []
        if "FROM DBA_ROLES" in sql:
            if name in conflicting_roles:
                return [(name, "N", "NO", "NONE")]
            if name in missing_roles:
                return []
            if name in {"SRC_READ", "SRC_NESTED", "UNRELATED_ROLE"}:
                return [(name, "N", "NO", "NONE")]
            return []
        if "FROM USER_SYS_PRIVS" in sql:
            if not authority:
                return []
            return [(name, "YES")]
        if "FROM SESSION_PRIVS" in sql:
            return [("CREATE TABLE",)]
        if "FROM USER_ROLE_PRIVS" in sql:
            return [("YES",)]
        if "FROM DBA_ROLE_PRIVS" in sql:
            return []
        if "FROM DBA_SYS_PRIVS" in sql:
            return []
        if "FROM ALL_TAB_PRIVS_MADE" in sql or "FROM ALL_COL_PRIVS_MADE" in sql:
            return []
        return []

    def fetchone():
        sql = str(cur.execute.call_args.args[0]).upper()
        binds = cur.execute.call_args.args[1] if len(cur.execute.call_args.args) > 1 else ()
        for needle, row in overrides.items():
            if needle in sql and not callable(row):
                return row[0] if row else None
        if "FROM DBA_ROLE_PRIVS" in sql:
            return None
        if "FROM ALL_TAB_PRIVS_MADE" in sql or "FROM ALL_COL_PRIVS_MADE" in sql:
            return None
        if "FROM DBA_SYS_PRIVS" in sql:
            return None
        if "SELECT USER FROM DUAL" in sql:
            return ("TGT_OWNER",)
        return None

    cur.fetchall.side_effect = fetchall
    cur.fetchone.side_effect = fetchone
    return conn, cur


def test_policy_is_minimal_and_does_not_accept_repetitive_mappings_or_grant_lists():
    normalized = permissions.normalize_policy(_policy())
    assert normalized["user_mappings"] == {}
    assert normalized["role_mappings"] == {}
    assert normalized["new_user_policy"] == "LOCKED"
    assert normalized["approved_object_privileges"] == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert normalized["approved_column_privileges"] == {"INSERT", "UPDATE", "REFERENCES"}
    assert not normalized["approved_system_privileges"]
    with pytest.raises(RuntimeError, match="metadata-driven"):
        permissions.normalize_policy(_policy(user_mappings=[{"source": "U", "target": "U"}]))
    with pytest.raises(RuntimeError, match="approval fields must be boolean"):
        permissions.normalize_policy(_policy(approve_create_session="yes"))
    with pytest.raises(RuntimeError, match="LOCKED or FAIL"):
        permissions.normalize_policy(_policy(new_user_policy="PASSWORD"))


def test_discovery_automatically_maps_same_name_principals_and_supported_grants():
    conn, cur = _source_connection()
    source = OracleSourceConnector({"username": "SRC_OWNER"})
    source._conn = conn
    source.configure_permission_migration(_policy())

    inventory = source.discover_permission_inventory()

    assert inventory["user_mappings"] == {"SRC_APP": "SRC_APP"}
    assert inventory["role_mappings"] == {
        "SRC_NESTED": "SRC_NESTED", "SRC_READ": "SRC_READ",
    }
    assert {user.name for user in inventory["users"]} == {"SRC_APP"}
    assert {role.name for role in inventory["roles"]} == {"SRC_READ", "SRC_NESTED"}
    assert inventory["grants"] == [
        GrantDef("SELECT", "TABLE", "P8_S40_DATA", "SRC_APP", "SRC_OWNER"),
        GrantDef("INSERT", "TABLE", "P8_S40_DATA", "SRC_READ", "SRC_OWNER"),
        GrantDef("UPDATE", "COLUMN", "P8_S40_DATA.VALUE", "SRC_APP", "SRC_OWNER"),
    ]
    assert {(m.member_name, m.role_name) for m in inventory["memberships"]} == {
        ("SRC_APP", "SRC_READ"), ("SRC_READ", "SRC_NESTED"),
    }
    assert source.list_users() == inventory["users"]
    assert source.list_roles() == inventory["roles"]
    assert source.list_grants() == inventory["grants"]
    assert source.list_role_memberships() == inventory["memberships"]
    queries = [str(call.args[0]).upper() for call in cur.execute.call_args_list]
    assert any("FROM DBA_ROLE_PRIVS" in query for query in queries)
    object_query = next(query for query in queries if "FROM ALL_TAB_PRIVS_MADE" in query and "GRANTEE" in query)
    column_query = next(query for query in queries if "FROM ALL_COL_PRIVS_MADE" in query and "GRANTEE" in query)
    assert "GRANTEE, OWNER, TABLE_NAME, GRANTOR, PRIVILEGE, GRANTABLE" in object_query
    assert "HIERARCHY, COMMON, TYPE, INHERITED" in object_query
    assert "GRANTEE, OWNER, TABLE_NAME, COLUMN_NAME, GRANTOR, PRIVILEGE" in column_query
    assert "GRANTABLE, COMMON, INHERITED" in column_query
    assert not any("PASSWORD" in query or "SPARE4" in query for query in queries)


def test_executor_inventory_reads_owner_grants_from_dba_catalog_views():
    conn, cur = _source_connection()
    permissions.discover(conn, "TGT_OWNER", _policy(), catalog_scope="dba")
    queries = [str(call.args[0]).upper() for call in cur.execute.call_args_list]
    assert any("FROM DBA_TAB_PRIVS WHERE OWNER = :1" in query for query in queries)
    assert any("FROM DBA_COL_PRIVS WHERE OWNER = :1" in query for query in queries)
    assert not any("FROM ALL_TAB_PRIVS_MADE WHERE OWNER = :1" in query for query in queries)
    with pytest.raises(ValueError, match="catalog_scope"):
        permissions.discover(conn, "TGT_OWNER", _policy(), catalog_scope="unknown")


def test_discovery_excludes_oracle_maintained_sys_membership_but_keeps_application_edge():
    conn, _ = _source_connection(
        overrides={
            "FROM ALL_TAB_PRIVS_MADE": [_tab_grant("P8_S40_SRC_APP", "SELECT")],
            "FROM ALL_COL_PRIVS_MADE": [],
        },
        memberships=[
            ("P8_S40_SRC_APP", "P8_S40_SRC_ROLE", "NO", "YES", "NO", "NO"),
            ("SYS", "P8_S40_SRC_ROLE", "YES", "YES", "NO", "NO"),
        ],
    )
    inventory = permissions.discover(conn, "SRC_OWNER", _policy())

    assert inventory["user_mappings"] == {"P8_S40_SRC_APP": "P8_S40_SRC_APP"}
    assert inventory["role_mappings"] == {"P8_S40_SRC_ROLE": "P8_S40_SRC_ROLE"}
    assert {(item.member_name, item.role_name) for item in inventory["memberships"]} == {
        ("P8_S40_SRC_APP", "P8_S40_SRC_ROLE"),
    }


def test_discovery_scopes_public_grant_rows_to_source_owned_objects():
    conn, cur = _source_connection(overrides={
        "FROM ALL_TAB_PRIVS_MADE": lambda binds: (
            [] if binds == ("SRC_OWNER",)
            else [_tab_grant("PUBLIC", "SELECT", table="SRC_OWNER")]
        ),
        "FROM ALL_COL_PRIVS_MADE": [],
    })

    inventory = permissions.discover(conn, "SRC_OWNER", _policy())

    assert inventory["grants"] == []
    object_query = next(
        call for call in cur.execute.call_args_list
        if "FROM ALL_TAB_PRIVS_MADE" in call.args[0].upper() and "GRANTEE" in call.args[0].upper()
    )
    column_query = next(
        call for call in cur.execute.call_args_list
        if "FROM ALL_COL_PRIVS_MADE" in call.args[0].upper() and "GRANTEE" in call.args[0].upper()
    )
    assert "WHERE OWNER = :1" in object_query.args[0].upper()
    assert object_query.args[1] == ("SRC_OWNER",)
    assert "WHERE OWNER = :1" in column_query.args[0].upper()
    assert column_query.args[1] == ("SRC_OWNER",)
    assert "GRANTEE, OWNER, TABLE_NAME, COLUMN_NAME, GRANTOR, PRIVILEGE" in column_query.args[0].upper()
    assert "GRANTABLE, COMMON, INHERITED" in column_query.args[0].upper()


def test_discovery_still_rejects_public_grant_on_source_owned_object():
    conn, cur = _source_connection(overrides={
        "FROM ALL_TAB_PRIVS_MADE": [_tab_grant("PUBLIC", "SELECT", table="SOURCE_TABLE")],
    })

    with pytest.raises(RuntimeError, match="PUBLIC object grant on SOURCE_TABLE"):
        permissions.discover(conn, "SRC_OWNER", _policy())

    object_query = next(
        call for call in cur.execute.call_args_list
        if "FROM ALL_TAB_PRIVS_MADE" in call.args[0].upper() and "GRANTEE" in call.args[0].upper()
    )
    assert "WHERE OWNER = :1" in object_query.args[0].upper()
    assert object_query.args[1] == ("SRC_OWNER",)


def test_discovery_still_rejects_public_column_grant_on_source_owned_object():
    conn, _ = _source_connection(overrides={
        "FROM ALL_TAB_PRIVS_MADE": [],
        "FROM ALL_COL_PRIVS_MADE": [
            _column_grant("PUBLIC", "UPDATE", table="SOURCE_TABLE", column="VALUE")
        ],
    })

    with pytest.raises(RuntimeError, match="PUBLIC column grant on SOURCE_TABLE.VALUE"):
        permissions.discover(conn, "SRC_OWNER", _policy())


def test_discovery_skips_public_inherit_privileges_user_row():
    conn, cur = _source_connection(overrides={
        "FROM ALL_TAB_PRIVS_MADE": [
            _tab_grant(
                "PUBLIC", "INHERIT PRIVILEGES", table="SRC_OWNER",
                object_type="USER",
            )
        ],
        "FROM ALL_COL_PRIVS_MADE": [],
    })

    inventory = permissions.discover(conn, "SRC_OWNER", _policy())

    assert inventory["grants"] == []
    assert inventory["users"] == []
    object_query = next(
        call for call in cur.execute.call_args_list
        if "FROM ALL_TAB_PRIVS_MADE" in call.args[0].upper() and "GRANTEE" in call.args[0].upper()
    )
    assert "GRANTEE, OWNER, TABLE_NAME, GRANTOR, PRIVILEGE, GRANTABLE" in object_query.args[0].upper()
    assert "HIERARCHY, COMMON, TYPE, INHERITED" in object_query.args[0].upper()
    assert object_query.args[1] == ("SRC_OWNER",)


def test_discovery_rejects_catalog_rows_outside_bound_source_owner():
    conn, _ = _source_connection(overrides={
        "FROM ALL_TAB_PRIVS_MADE": [
            _tab_grant("SRC_APP", "SELECT", owner="OTHER_OWNER")
        ],
    })

    with pytest.raises(RuntimeError, match="outside requested source owner"):
        permissions.discover(conn, "SRC_OWNER", _policy())


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"FROM ALL_TAB_PRIVS_MADE": [_tab_grant("PUBLIC", "SELECT", table="T")]}, "PUBLIC object grant"),
        ({"FROM ALL_TAB_PRIVS_MADE": [_tab_grant("SRC_APP", "READ", table="T")]}, "Unsupported Oracle object privilege"),
        ({"FROM ALL_TAB_PRIVS_MADE": [_tab_grant("SRC_APP", "SELECT", table="T", grantable="YES")]}, "grant option"),
        ({"FROM ALL_TAB_PRIVS_MADE": [_tab_grant("SRC_APP", "SELECT", table="T", object_type="PROCEDURE")]}, "Unsupported Oracle object grant type"),
        ({"FROM ALL_COL_PRIVS_MADE": [_column_grant("SRC_APP", "SELECT", table="T", column="C")]}, "Unsupported Oracle column privilege"),
        ({"FROM ALL_COL_PRIVS_MADE": [_column_grant("SRC_APP", "UPDATE", table="T", column="C", grantable="YES")]}, "column grant option"),
    ],
)
def test_source_rejects_public_unsupported_and_grant_option_permissions(override, message):
    conn, _ = _source_connection(overrides=override)
    with pytest.raises(RuntimeError, match=message):
        permissions.discover(conn, "SRC_OWNER", _policy())


def test_catalog_access_and_malformed_role_graph_fail_closed():
    conn, _ = _source_connection(failure="FROM SYS.DBA_ROLE_PRIVS")
    with pytest.raises(RuntimeError, match="multiple inaccessible catalog views"):
        permissions.discover(conn, "SRC_OWNER", _policy())
    conn, _ = _source_connection(memberships=[("SRC_APP", "SRC_READ", "YES", "YES", "NO", "NO")])
    with pytest.raises(RuntimeError, match="ADMIN_OPTION"):
        permissions.discover(conn, "SRC_OWNER", _policy())


def test_source_catalog_access_preflight_reports_all_unavailable_views_together():
    conn, cur = _source_connection()
    denied = {"SYS.DBA_USERS", "SYS.DBA_ROLES", "SYS.DBA_SYS_PRIVS"}

    def execute(statement, *_args, **_kwargs):
        cur._active_sql = str(statement).upper()
        if any(view in cur._active_sql for view in denied):
            raise PermissionError("metadata denied")

    cur.execute.side_effect = execute
    gaps = permissions.verify_source_catalog_access(conn)
    assert gaps == [
        "SELECT on SYS.DBA_USERS", "SELECT on SYS.DBA_ROLES", "SELECT on SYS.DBA_SYS_PRIVS",
    ]


def test_permission_discovery_reports_exact_source_catalog_grants_without_broad_fallback():
    conn, cur = _source_connection()
    denied = {"SYS.DBA_USERS", "SYS.DBA_ROLES", "SYS.DBA_ROLE_PRIVS", "SYS.DBA_SYS_PRIVS"}

    def execute(statement, *_args, **_kwargs):
        if any(view in str(statement).upper() for view in denied):
            raise PermissionError("ORA-00942: table or view does not exist")

    cur.execute.side_effect = execute

    with pytest.raises(RuntimeError) as exc:
        permissions.discover(conn, "SRC_OWNER", _policy())

    message = str(exc.value)
    for view in sorted(denied):
        assert f"SELECT on {view}" in message
        assert f'GRANT SELECT ON {view} TO "SRC_OWNER"' in message
    assert "SELECT ANY DICTIONARY" not in message
    assert "SELECT_CATALOG_ROLE" not in message


def test_executor_catalog_access_reports_all_missing_views():
    conn, cur = _target_connection()
    unavailable = {"SYS.DBA_USERS", "SYS.DBA_ROLE_PRIVS"}

    def execute(statement, *_args, **_kwargs):
        sql = str(statement).upper()
        if "SELECT 1 FROM" in sql and any(view in sql for view in unavailable):
            raise PermissionError("metadata denied")

    cur.execute.side_effect = execute
    assert permissions.verify_executor_catalog_access(conn) == [
        "SELECT on SYS.DBA_USERS", "SELECT on SYS.DBA_ROLE_PRIVS",
    ]


def test_executor_authority_preflight_consolidates_missing_system_and_role_authority():
    class Cursor:
        def __init__(self, conn):
            self.conn, self.sql, self.binds = conn, "", ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, binds=()):
            self.sql, self.binds = str(sql).upper(), binds

        def fetchall(self):
            if "DBA_USERS" in self.sql and self.binds:
                return [(self.binds[0],)]
            if "DBA_ROLES" in self.sql and self.binds:
                return [(self.binds[0],)]
            return []

    class Connection:
        def cursor(self):
            return Cursor(self)

    inv = _source_inventory(
        _policy(approve_create_session=True),
        overrides={"FROM DBA_SYS_PRIVS": [("CREATE SESSION", "NO", "NO", "NO")]},
    )
    gaps = permissions.executor_authority_gaps(
        Connection(), _policy(approve_create_session=True), inv
    )
    assert "ADMIN OPTION on existing target role SRC_READ" in gaps
    assert "ADMIN OPTION on existing target role SRC_NESTED" in gaps
    assert "CREATE SESSION WITH ADMIN OPTION to apply the approved source grant" in gaps


def test_permission_onboarding_requires_explicit_approval_and_specific_catalog_grant_option():
    class Cursor:
        def __init__(self, conn):
            self.conn = conn
            self.sql = ""
            self.binds = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, binds=()):
            self.sql, self.binds = str(sql).upper(), binds
            self.conn.statements.append(self.sql)

        def fetchone(self):
            if "SYS_CONTEXT" in self.sql:
                return (self.conn.user, "DB1", "PDB1", "SERVICE1")
            return None

        def fetchall(self):
            if "USER_TAB_PRIVS_RECD" in self.sql:
                return [(view,) for view in permissions.PERMISSION_CATALOG_VIEWS]
            if "USER_SYS_PRIVS" in self.sql:
                return []
            if "DBA_USERS" in self.sql:
                return []
            if "DBA_ROLES" in self.sql:
                    return []
            return []

    class Connection:
        def __init__(self, user):
            self.user = user
            self.statements = []

        def cursor(self):
            return Cursor(self)

    admin, executor = Connection("BOOTSTRAP"), Connection("MIGRATION_EXECUTOR")
    base_policy = _policy(
        onboarding={"enabled": True, "approve_catalog_access": True},
        executor={"username": "MIGRATION_EXECUTOR", "password_secret": "executor_secret"},
    )
    with pytest.raises(RuntimeError, match="explicit approval to grant CREATE USER"):
        permissions.bootstrap_permission_executor(
            admin, executor, admin_user="BOOTSTRAP", executor_user="MIGRATION_EXECUTOR",
            target_identity={"user": "TARGET_OWNER", "db_unique_name": "DB1", "container_name": "PDB1", "service_name": "SERVICE1"},
            inventory={"user_mappings": {"APP": "APP"}, "role_mappings": {}, "memberships": [], "grants": []},
            raw_policy=base_policy,
        )
    assert not any(sql.startswith("GRANT ") for sql in admin.statements)


def test_permission_onboarding_grants_only_approved_catalog_views():
    class Cursor:
        def __init__(self, conn):
            self.conn, self.sql, self.binds = conn, "", ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, binds=()):
            self.sql, self.binds = str(sql).upper(), binds
            self.conn.statements.append(self.sql)

        def fetchone(self):
            if "SYS_CONTEXT" in self.sql:
                return (self.conn.user, "DB1", "PDB1", "SERVICE1")
            return None

        def fetchall(self):
            if "USER_TAB_PRIVS_RECD" in self.sql:
                return [(view,) for view in permissions.PERMISSION_CATALOG_VIEWS]
            if "DBA_USERS" in self.sql:
                return [(self.binds[0],)]
            return []

    class Connection:
        def __init__(self, user):
            self.user, self.statements = user, []

        def cursor(self):
            return Cursor(self)

    admin, executor = Connection("BOOTSTRAP"), Connection("MIGRATION_EXECUTOR")
    policy = _policy(onboarding={
        "enabled": True, "approve_catalog_access": True,
        "admin_connection": {"username": "BOOTSTRAP", "password_secret": "admin_secret"},
    })
    applied = permissions.bootstrap_permission_executor(
        admin, executor, admin_user="BOOTSTRAP", executor_user="MIGRATION_EXECUTOR",
        target_identity={"user": "TARGET_OWNER", "db_unique_name": "DB1", "container_name": "PDB1", "service_name": "SERVICE1"},
        inventory={"user_mappings": {"APP": "APP"}, "role_mappings": {}, "memberships": [], "grants": []},
        raw_policy=policy,
    )
    assert len(applied) == len(permissions.PERMISSION_CATALOG_VIEWS)
    assert all(statement.startswith("GRANT SELECT ON SYS.DBA_") for statement in applied)
    assert not any("ANY" in statement or "DBA TO" in statement for statement in applied)


def test_source_rejects_privileged_common_or_authenticated_principals():
    conn, _ = _source_connection(overrides={
        "FROM DBA_USERS": lambda binds: (
            [("SRC_APP", "Y", "NO")] if binds == ("SRC_APP",) else []
        ),
    })
    with pytest.raises(RuntimeError, match="Oracle-maintained"):
        permissions.discover(conn, "SRC_OWNER", _policy())
    conn, _ = _source_connection(overrides={
        "FROM DBA_ROLES": lambda binds: (
            [(binds[0], "N", "NO", "PASSWORD" if binds[0] == "SRC_READ" else "NONE")]
            if binds[0] in {"SRC_READ", "SRC_NESTED"} else []
        ),
    })
    with pytest.raises(RuntimeError, match="only local NOT IDENTIFIED"):
        permissions.discover(conn, "SRC_OWNER", _policy())
    conn, _ = _source_connection(overrides={
        "FROM DBA_SYS_PRIVS": [("CREATE ANY TABLE", "NO", "NO", "NO")],
    })
    with pytest.raises(RuntimeError, match="Unsupported Oracle system privilege"):
        permissions.discover(conn, "SRC_OWNER", _policy())


def test_create_session_requires_global_explicit_approval_and_never_copies_other_sys_privileges():
    conn, _ = _source_connection(overrides={
        "FROM DBA_SYS_PRIVS": [("CREATE SESSION", "NO", "NO", "NO")],
    })
    with pytest.raises(RuntimeError, match="approve_create_session: true"):
        permissions.discover(conn, "SRC_OWNER", _policy())
    inventory = permissions.discover(
        conn, "SRC_OWNER", _policy(approve_create_session=True)
    )
    assert any(grant.object_type == "SYSTEM" for grant in inventory["grants"])


def test_preflight_uses_same_name_mappings_and_preserves_existing_target_principals():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    target, cur = _target_connection()
    plan = permissions.preflight_target(
        target, _policy(approve_role_creation_admin_option=True), inventory,
        source_owner="SRC_OWNER", target_owner="TGT_OWNER",
    )
    assert plan["policy"]["user_mappings"] == {"SRC_APP": "SRC_APP"}
    assert plan["policy"]["role_mappings"]["SRC_READ"] == "SRC_READ"
    assert plan["new_users"] == []
    assert plan["new_roles"] == []
    assert plan["memberships"]
    assert permissions.apply_principals(target, plan) == []
    assert not any(str(call.args[0]).upper().startswith(("CREATE USER", "ALTER USER", "CREATE ROLE"))
                   for call in cur.execute.call_args_list)


def test_preflight_routes_principal_metadata_to_executor_and_object_grants_to_owner():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    owner, owner_cur = _target_connection()
    executor, executor_cur = _target_connection()
    permissions.preflight_target(
        owner, _policy(approve_role_creation_admin_option=True), inventory,
        source_owner="SRC_OWNER", target_owner="TGT_OWNER", executor_conn=executor,
    )
    owner_sql = [str(call.args[0]).upper() for call in owner_cur.execute.call_args_list]
    executor_sql = [str(call.args[0]).upper() for call in executor_cur.execute.call_args_list]
    assert any("FROM ALL_TAB_PRIVS_MADE" in sql for sql in owner_sql)
    assert not any("FROM DBA_USERS" in sql for sql in owner_sql)
    assert any("FROM DBA_USERS" in sql for sql in executor_sql)
    assert not any("FROM ALL_TAB_PRIVS_MADE" in sql for sql in executor_sql)


def test_object_grants_stay_on_owner_session_and_system_grants_use_executor():
    from core.connectors.oracle.target import OracleTargetConnector

    owner, owner_cur = _target_connection()
    executor, executor_cur = _target_connection()
    target = OracleTargetConnector({"username": "TGT_OWNER"})
    target._conn = owner
    target._permission_executor_conn = executor
    plan = {
        "object_grants": [{
            "kind": "TABLE", "object_name": "DATA", "privilege": "SELECT", "grantee": "APP",
        }],
        "system_grants": [{
            "privilege": "CREATE SESSION", "grantee": "APP",
        }],
    }
    target.apply_permission_grants(plan)
    target.apply_permission_system_grants(plan)
    owner_sql = [str(call.args[0]).upper() for call in owner_cur.execute.call_args_list]
    executor_sql = [str(call.args[0]).upper() for call in executor_cur.execute.call_args_list]
    assert any(statement.startswith('GRANT SELECT ON "TGT_OWNER"."DATA"') for statement in owner_sql)
    assert not any("GRANT CREATE SESSION" in statement for statement in owner_sql)
    assert any('GRANT CREATE SESSION TO "APP"' in statement for statement in executor_sql)
    target.close()


def test_permission_executor_rejects_wrong_target_service_before_catalog_or_cleanup(monkeypatch):
    import sys
    from types import SimpleNamespace

    from core.connectors.oracle.target import OracleTargetConnector

    class Cursor:
        def __init__(self, conn):
            self.conn, self.sql = conn, ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _binds=()):
            self.sql = str(sql).upper()
            self.conn.statements.append(self.sql)

        def fetchone(self):
            if self.sql == "SELECT USER FROM DUAL":
                return (self.conn.user,)
            if "SYS_CONTEXT('USERENV', 'CON_NAME')" in self.sql:
                return (self.conn.container,)
            if "DB_UNIQUE_NAME" in self.sql:
                return (self.conn.user, "DB1", self.conn.container, self.conn.service)
            return None

        def fetchall(self):
            return []

    class Connection:
        def __init__(self, user, service):
            self.user, self.container, self.service = user, "PDB1", service
            self.statements = []

        def cursor(self):
            return Cursor(self)

        def close(self):
            pass

    owner = Connection("TGT_OWNER", "MIGRATION_DB")
    executor = Connection("PERMISSION_EXECUTOR", "OTHER_SERVICE")
    fake_driver = SimpleNamespace(makedsn=lambda *args, **kwargs: "dsn", connect=lambda **_kwargs: executor)
    monkeypatch.setitem(sys.modules, "oracledb", fake_driver)
    target = OracleTargetConnector({"username": "TGT_OWNER"})
    target._conn = owner
    with pytest.raises(RuntimeError, match="different database, PDB, or service"):
        target.preflight_permission_migration(
            _policy(), {"grants": [], "memberships": [], "user_mappings": {}, "role_mappings": []},
            {}, source_owner="SRC_OWNER",
            executor_connection={
                "username": "PERMISSION_EXECUTOR", "password": "secret",
                "host": "host", "service_name": "MIGRATION_DB",
            },
        )
    assert not any("DBA_" in sql for sql in executor.statements)
    target.close()


def test_missing_user_is_locked_by_default_or_fails_by_policy_and_requires_direct_create_user():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    target, cur = _target_connection(missing_users={"SRC_APP"})
    plan = permissions.preflight_target(
        target, _policy(approve_role_creation_admin_option=True), inventory,
        source_owner="SRC_OWNER", target_owner="TGT_OWNER",
    )
    assert plan["new_users"] == [{"source": "SRC_APP", "target": "SRC_APP", "account_state": "LOCKED"}]
    created = permissions.apply_principals(target, plan)
    assert created == ["USER SRC_APP (LOCKED)"]
    assert 'CREATE USER "SRC_APP" IDENTIFIED EXTERNALLY ACCOUNT LOCK' in [
        str(call.args[0]) for call in cur.execute.call_args_list
    ]
    target, _ = _target_connection(missing_users={"SRC_APP"}, authority=False)
    with pytest.raises(RuntimeError, match="lacks direct CREATE USER"):
        permissions.preflight_target(
            target, _policy(), inventory, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
        )
    target, _ = _target_connection(missing_users={"SRC_APP"})
    with pytest.raises(RuntimeError, match="new_user_policy: LOCKED"):
        permissions.preflight_target(
            target, _policy(new_user_policy="FAIL"), inventory,
            source_owner="SRC_OWNER", target_owner="TGT_OWNER",
        )


def test_missing_role_requires_global_side_effect_approval_and_direct_create_role():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    target, cur = _target_connection(missing_roles={"SRC_READ", "SRC_NESTED"})
    with pytest.raises(RuntimeError, match="approve_role_creation_admin_option: true"):
        permissions.preflight_target(
            target, _policy(), inventory, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
        )
    target, _ = _target_connection(missing_roles={"SRC_READ", "SRC_NESTED"}, authority=False)
    with pytest.raises(RuntimeError, match="lacks direct CREATE ROLE"):
        permissions.preflight_target(
            target, _policy(approve_role_creation_admin_option=True), inventory,
            source_owner="SRC_OWNER", target_owner="TGT_OWNER",
        )
    target, cur = _target_connection(missing_roles={"SRC_READ", "SRC_NESTED"})
    plan = permissions.preflight_target(
        target, _policy(approve_role_creation_admin_option=True), inventory,
        source_owner="SRC_OWNER", target_owner="TGT_OWNER",
    )
    assert {item["target"] for item in plan["new_roles"]} == {"SRC_READ", "SRC_NESTED"}
    permissions.apply_principals(target, plan)
    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert 'CREATE ROLE "SRC_READ" NOT IDENTIFIED' in statements
    assert 'CREATE ROLE "SRC_NESTED" NOT IDENTIFIED' in statements


def test_repeat_full_preflight_reuses_provisioned_same_name_principals():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    policy = _policy(approve_role_creation_admin_option=True)
    first_target, first_cur = _target_connection(
        missing_users={"SRC_APP"}, missing_roles={"SRC_READ", "SRC_NESTED"}
    )
    first_plan = permissions.preflight_target(
        first_target, policy, inventory, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
    )
    assert first_plan["new_users"] and first_plan["new_roles"]
    permissions.apply_principals(first_target, first_plan)
    assert any(str(call.args[0]).upper().startswith("CREATE USER")
               for call in first_cur.execute.call_args_list)

    # A later FULL sees those same principals in catalog metadata and therefore
    # leaves their authentication, account state, and role state untouched.
    second_target, second_cur = _target_connection()
    second_plan = permissions.preflight_target(
        second_target, policy, inventory, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
    )
    assert second_plan["new_users"] == []
    assert second_plan["new_roles"] == []
    assert permissions.apply_principals(second_target, second_plan) == []
    assert not any(str(call.args[0]).upper().startswith(("CREATE USER", "CREATE ROLE", "ALTER USER"))
                   for call in second_cur.execute.call_args_list)


def test_target_principal_type_conflicts_and_target_only_state_are_not_overwritten():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    target, _ = _target_connection(missing_users={"SRC_APP"}, conflicting_roles={"SRC_APP"})
    with pytest.raises(RuntimeError, match="conflicts with an existing target role"):
        permissions.preflight_target(
            target, _policy(), inventory, source_owner="SRC_OWNER", target_owner="TGT_OWNER"
        )
    target, cur = _target_connection()
    permissions.apply_grants(target, {"object_grants": [], "system_grants": []}, "TGT_OWNER")
    assert not any("REVOKE" in str(call.args[0]).upper() for call in cur.execute.call_args_list)


def test_preflight_refuses_membership_that_would_expose_unrelated_existing_role_grants():
    inventory = _source_inventory()
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    target, _ = _target_connection(overrides={
        "FROM DBA_TAB_PRIVS": [("TGT_OWNER", "TARGET_ONLY", "SELECT", "NO")],
    })
    with pytest.raises(RuntimeError, match="unrelated object grants; refusing to add members"):
        permissions.preflight_target(
            target, _policy(approve_role_creation_admin_option=True), inventory,
            source_owner="SRC_OWNER", target_owner="TGT_OWNER",
        )


def test_memberships_and_object_column_grants_are_applied_idempotently():
    target, cur = _target_connection()
    plan = {
        "object_grants": [
            {"kind": "TABLE", "object_name": "P8_S40_DATA", "privilege": "SELECT", "grantee": "SRC_APP"},
            {"kind": "COLUMN", "object_name": "P8_S40_DATA", "column_name": "VALUE",
             "privilege": "UPDATE", "grantee": "SRC_APP"},
        ],
        "system_grants": [],
        "memberships": [{"member": "SRC_APP", "role": "SRC_READ"}],
    }
    permissions.apply_memberships(target, plan)
    permissions.apply_grants(target, plan, "TGT_OWNER")
    sql = [str(call.args[0]).upper() for call in cur.execute.call_args_list]
    assert 'GRANT "SRC_READ" TO "SRC_APP"' in sql
    assert 'GRANT SELECT ON "TGT_OWNER"."P8_S40_DATA" TO "SRC_APP"' in sql
    assert 'GRANT UPDATE ("VALUE") ON "TGT_OWNER"."P8_S40_DATA" TO "SRC_APP"' in sql
    assert not any(statement.startswith("REVOKE ") for statement in sql)

    def existing():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "DBA_ROLE_PRIVS" in sql:
            return ("NO", "YES")
        if "ALL_TAB_PRIVS_MADE" in sql or "ALL_COL_PRIVS_MADE" in sql:
            return ("NO",)
        return None
    cur.fetchone.side_effect = existing
    before = len(cur.execute.call_args_list)
    permissions.apply_memberships(target, plan)
    permissions.apply_grants(target, plan, "TGT_OWNER")
    assert not any(str(call.args[0]).upper().startswith("GRANT ")
                   for call in cur.execute.call_args_list[before:])


def test_create_session_copy_and_missing_grant_authority_are_preflighted():
    inventory = _source_inventory(_policy(approve_create_session=True), overrides={
        "FROM DBA_SYS_PRIVS": [("CREATE SESSION", "NO", "NO", "NO")],
    })
    inventory.update(tables={"P8_S40_DATA"}, views=[], materialized_views=[])
    target, _ = _target_connection(overrides={"FROM USER_SYS_PRIVS": []})
    with pytest.raises(RuntimeError, match="lacks ADMIN OPTION on system privilege CREATE SESSION"):
        permissions.preflight_target(
            target, _policy(approve_create_session=True), inventory,
            source_owner="SRC_OWNER", target_owner="TGT_OWNER",
        )


def test_denied_grant_and_membership_operations_surface_actionable_errors():
    target, cur = _target_connection()
    cur.execute.side_effect = lambda statement, *a, **k: (
        (_ for _ in ()).throw(PermissionError("ORA-01031 insufficient privileges"))
        if str(statement).upper().startswith("GRANT ") else None
    )
    with pytest.raises(RuntimeError, match="grants applied earlier in this run: none"):
        permissions.apply_grants(target, {
            "object_grants": [{"kind": "TABLE", "object_name": "T", "privilege": "SELECT", "grantee": "APP"}],
            "system_grants": [],
        }, "TGT_OWNER")
    with pytest.raises(RuntimeError, match="memberships applied earlier in this run: none"):
        permissions.apply_memberships(target, {
            "memberships": [{"member": "APP", "role": "R"}],
        })


def _tracked_item(kind, privilege, object_name="", column_name="", grantee="APP"):
    return {"kind": kind, "privilege": privilege, "object_name": object_name,
            "column_name": column_name, "grantee": grantee}


def _reconciliation_plan(*, desired=(), stale=(), ledger=(), kinds=("OBJECT", "COLUMN")):
    return {
        "target_owner": "TGT_OWNER",
        "object_grants": [item for item in desired if item["kind"] in {"OBJECT", "COLUMN"}],
        "system_grants": [item for item in desired if item["kind"] == "SYSTEM"],
        "memberships": [item for item in desired if item["kind"] == "ROLE"],
        "reconciliation_kinds": set(kinds),
        "permission_reconciliation": {
            "desired": list(desired), "stale": list(stale),
            "ledger": {permissions._permission_key(item): {**item, "state": state}
                       for item, state in ledger},
        },
    }


def _mock_tracked_apply(monkeypatch, conn, plan, *, initially_present=()):
    present = {permissions._permission_key(item) for item in initially_present}
    provenance = {}
    statements = []
    item_for_sql = {kind: None for kind in ("GRANT", "REVOKE")}
    cursor = conn.cursor.return_value.__enter__.return_value

    def exists(_conn, item, _owner):
        return permissions._permission_key(item) in present

    def set_provenance(_conn, _owner, item, state):
        provenance[permissions._permission_key(item)] = {**item, "state": state}

    def delete_provenance(_conn, _owner, item):
        provenance.pop(permissions._permission_key(item), None)

    def execute(statement, *args, **kwargs):
        sql = str(statement)
        statements.append(sql)
        if sql.startswith("GRANT "):
            present.add(permissions._permission_key(item_for_sql["GRANT"]))
        elif sql.startswith("REVOKE "):
            present.discard(permissions._permission_key(item_for_sql["REVOKE"]))

    original_execute = cursor.execute

    def route(statement, *args, **kwargs):
        sql = str(statement)
        if sql.startswith("GRANT "):
            item_for_sql["GRANT"] = next(
                item for item in plan["permission_reconciliation"]["desired"]
                if permissions._permission_key(item) not in present
            )
            execute(sql, *args, **kwargs)
        elif sql.startswith("REVOKE "):
            item_for_sql["REVOKE"] = plan["permission_reconciliation"]["stale"][0]
            execute(sql, *args, **kwargs)
        else:
            original_execute(statement, *args, **kwargs)

    cursor.execute.side_effect = route
    monkeypatch.setattr(permissions, "_permission_exists", exists)
    monkeypatch.setattr(permissions, "_validate_live_permission_state", exists)
    monkeypatch.setattr(permissions, "_provenance_set", set_provenance)
    monkeypatch.setattr(permissions, "_provenance_delete", delete_provenance)
    return present, provenance, statements


@pytest.mark.parametrize(
    ("item", "expected", "kind"),
    [
        (_tracked_item("OBJECT", "SELECT", "P8_DATA"),
         'REVOKE SELECT ON "TGT_OWNER"."P8_DATA" FROM "APP"', "OBJECT"),
        (_tracked_item("COLUMN", "UPDATE", "P8_DATA", "VALUE"),
         'REVOKE UPDATE ("VALUE") ON "TGT_OWNER"."P8_DATA" FROM "APP"', "COLUMN"),
        (_tracked_item("ROLE", "MEMBERSHIP", "APP_ROLE"),
         'REVOKE "APP_ROLE" FROM "APP"', "ROLE"),
        (_tracked_item("SYSTEM", "CREATE SESSION"),
         'REVOKE CREATE SESSION FROM "APP"', "SYSTEM"),
    ],
)
def test_reconciliation_revokes_only_tracked_permissions(monkeypatch, item, expected, kind):
    conn = MagicMock()
    plan = _reconciliation_plan(stale=(item,), ledger=((item, "ACTIVE"),), kinds=(kind,))
    _, provenance, statements = _mock_tracked_apply(monkeypatch, conn, plan, initially_present=(item,))
    if kind == "ROLE":
        permissions.apply_memberships(conn, plan)
    else:
        permissions.apply_grants(conn, plan, "TGT_OWNER")
    assert expected in statements
    assert permissions._permission_key(item) not in provenance


def test_reconciliation_regrants_and_repeat_is_idempotent(monkeypatch):
    item = _tracked_item("OBJECT", "SELECT", "P8_DATA")
    conn = MagicMock()
    plan = _reconciliation_plan(desired=(item,))
    _, provenance, statements = _mock_tracked_apply(monkeypatch, conn, plan)
    permissions.apply_grants(conn, plan, "TGT_OWNER")
    assert 'GRANT SELECT ON "TGT_OWNER"."P8_DATA" TO "APP"' in statements
    assert provenance[permissions._permission_key(item)]["state"] == "ACTIVE"
    plan["permission_reconciliation"]["ledger"].update(provenance)
    statements.clear()
    permissions.apply_grants(conn, plan, "TGT_OWNER")
    assert statements == []


def test_interrupted_pending_grant_is_verified_without_duplicate_grant(monkeypatch):
    item = _tracked_item("OBJECT", "SELECT", "P8_DATA")
    conn = MagicMock()
    plan = _reconciliation_plan(desired=(item,), ledger=((item, "PENDING_GRANT"),))
    _, provenance, statements = _mock_tracked_apply(monkeypatch, conn, plan, initially_present=(item,))
    permissions.apply_grants(conn, plan, "TGT_OWNER")
    assert not any(statement.startswith("GRANT ") for statement in statements)
    assert provenance[permissions._permission_key(item)]["state"] == "ACTIVE"


def test_interrupted_revoke_cleans_journal_after_catalog_confirms_absence(monkeypatch):
    item = _tracked_item("OBJECT", "SELECT", "P8_DATA")
    conn = MagicMock()
    plan = _reconciliation_plan(ledger=((item, "PENDING_REVOKE"),))
    _, provenance, statements = _mock_tracked_apply(monkeypatch, conn, plan)
    permissions.apply_grants(conn, plan, "TGT_OWNER")
    assert statements == []
    assert permissions._permission_key(item) not in provenance


def test_untracked_stale_target_permission_fails_closed_with_consolidated_manifest(monkeypatch):
    monkeypatch.setattr(permissions, "_read_provenance", lambda _conn, _owner: {})

    def fetchall(_conn, statement, binds=()):
        if "FROM ALL_TAB_PRIVS_MADE" in statement.upper():
            return [("APP", "P8_DATA", "TGT_OWNER", "SELECT", "NO", "NO", "NO", "TABLE")]
        return []

    monkeypatch.setattr(permissions, "_fetchall", fetchall)
    with pytest.raises(RuntimeError, match="no durable framework provenance") as error:
        permissions._build_reconciliation(
            MagicMock(), MagicMock(), target_owner="TGT_OWNER", target_objects={"P8_DATA"},
            target_principals={"APP"}, target_roles=set(), desired=[])
    assert '"permissions"' in str(error.value)
    assert '"approval_key"' in str(error.value)
    assert "audit records" in str(error.value)


def test_explicit_legacy_approval_is_journaled_before_stale_revoke(monkeypatch):
    monkeypatch.setattr(permissions, "_read_provenance", lambda _conn, _owner: {})
    item = {"kind": "OBJECT", "object_name": "P8_DATA", "column_name": "",
            "privilege": "SELECT", "grantee": "APP", "grantor": "TGT_OWNER",
            "grantable": "NO", "common": "NO", "inherited": "NO", "object_type": "TABLE"}

    def fetchall(_conn, statement, binds=()):
        if "FROM ALL_TAB_PRIVS_MADE" in statement.upper():
            return [("APP", "P8_DATA", "TGT_OWNER", "SELECT", "NO", "NO", "NO", "TABLE")]
        return []

    monkeypatch.setattr(permissions, "_fetchall", fetchall)
    plan = permissions._build_reconciliation(
        MagicMock(), MagicMock(), target_owner="TGT_OWNER", target_objects={"P8_DATA"},
        target_principals={"APP"}, target_roles=set(), desired=[],
        approved_legacy_keys={permissions._permission_key(item)})
    assert plan["adopted_legacy"][0]["privilege"] == "SELECT"
    assert plan["stale"][0]["privilege"] == "SELECT"


def test_legacy_approval_rejects_stale_keys(monkeypatch):
    monkeypatch.setattr(permissions, "_read_provenance", lambda _conn, _owner: {})
    monkeypatch.setattr(permissions, "_fetchall", lambda _conn, statement, binds=(): (
        [("APP", "P8_DATA", "TGT_OWNER", "SELECT", "NO", "NO", "NO", "TABLE")]
        if "FROM ALL_TAB_PRIVS_MADE" in statement.upper() else []
    ))
    with pytest.raises(RuntimeError, match="stale or nonmatching"):
        permissions._build_reconciliation(
            MagicMock(), MagicMock(), target_owner="TGT_OWNER", target_objects={"P8_DATA"},
            target_principals={"APP"}, target_roles=set(), desired=[],
            approved_legacy_keys={"0" * 64})


def test_legacy_approval_cli_candidate_mode_returns_exact_review_set(monkeypatch):
    monkeypatch.setattr(permissions, "_read_provenance", lambda _conn, _owner: {})
    monkeypatch.setattr(permissions, "_fetchall", lambda _conn, statement, binds=(): (
        [("APP", "P8_DATA", "TGT_OWNER", "SELECT", "NO", "NO", "NO", "TABLE")]
        if "FROM ALL_TAB_PRIVS_MADE" in statement.upper() else []
    ))
    plan = permissions._build_reconciliation(
        MagicMock(), MagicMock(), target_owner="TGT_OWNER", target_objects={"P8_DATA"},
        target_principals={"APP"}, target_roles=set(), desired=[],
        permit_legacy_candidates=True)
    assert len(plan["adopted_legacy"]) == 1
    assert plan["adopted_legacy"][0]["grantee"] == "APP"


def test_legacy_approval_persists_only_after_live_state_matches(monkeypatch):
    item = {"kind": "OBJECT", "object_name": "P8_DATA", "column_name": "",
            "privilege": "SELECT", "grantee": "APP"}
    writes = []
    monkeypatch.setattr(permissions, "_validate_live_permission_state", lambda *_args: True)
    monkeypatch.setattr(permissions, "_provenance_set", lambda _conn, _owner, candidate, state: writes.append((candidate, state)))
    keys = permissions.record_legacy_adoptions(MagicMock(), "TGT_OWNER", [item])
    assert keys == [permissions._permission_key(item)]
    assert writes == [(item, "APPROVED_LEGACY")]


def test_permission_scope_id_changes_across_source_or_target_database():
    source = {"user": "SRC", "db_unique_name": "DB1", "container_name": "PDB1", "service_name": "SVC"}
    target = {"user": "TGT", "db_unique_name": "DB2", "container_name": "PDB2", "service_name": "SVC"}
    scope = permissions.permission_scope_id(source, target)
    changed = permissions.permission_scope_id({**source, "db_unique_name": "DB3"}, target)
    assert scope != changed


def test_reconciliation_preserves_unrelated_target_only_permission(monkeypatch):
    monkeypatch.setattr(permissions, "_read_provenance", lambda _conn, _owner: {})

    def fetchall(_conn, statement, binds=()):
        if "FROM ALL_TAB_PRIVS_MADE" in statement.upper():
            return [("OTHER_APP", "TARGET_ONLY", "TGT_OWNER", "SELECT", "NO", "NO", "NO", "TABLE")]
        return []

    monkeypatch.setattr(permissions, "_fetchall", fetchall)
    plan = permissions._build_reconciliation(
        MagicMock(), MagicMock(), target_owner="TGT_OWNER", target_objects={"P8_DATA"},
        target_principals={"APP"}, target_roles=set(), desired=[])
    assert plan["stale"] == []


def test_provenance_setup_grants_executor_only_ledger_dml_and_is_idempotent(monkeypatch):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    created = {"table": False, "privileges": set()}
    statements = []

    def fetchall(_conn, statement, binds=()):
        sql = statement.upper()
        if "FROM ALL_TABLES" in sql:
            return [("DMT_PERMISSION_PROVENANCE",)] if created["table"] else []
        if "SELECT ITEM_KEY" in sql:
            return [(permissions.PROVENANCE_META_KEY, "META", None, None, "SCHEMA_V1", "FRAMEWORK", "ACTIVE")]
        if "FROM ALL_TAB_PRIVS_MADE" in sql:
            return [("EXECUTOR", privilege, "NO", "NO", "NO")
                    for privilege in sorted(created["privileges"])]
        return []

    def execute(statement, *args, **kwargs):
        sql = str(statement)
        statements.append(sql)
        upper = sql.upper()
        if upper.startswith("CREATE TABLE"):
            created["table"] = True
        elif upper.startswith("GRANT "):
            privilege_list = sql.split(" ON ", 1)[0][6:]
            created["privileges"].update(part.strip().upper() for part in privilege_list.split(","))

    cursor.execute.side_effect = execute
    monkeypatch.setattr(permissions, "_fetchall", fetchall)
    first = permissions.prepare_provenance(conn, "TGT_OWNER", "EXECUTOR")
    assert len(first) == 4
    assert any(statement.startswith("CREATE TABLE \"TGT_OWNER\".\"DMT_PERMISSION_PROVENANCE\"")
               for statement in statements)
    assert any(statement.startswith("GRANT SELECT, DELETE, INSERT, UPDATE ON")
               or statement.startswith("GRANT DELETE, INSERT, SELECT, UPDATE ON")
               for statement in statements)
    statements.clear()
    second = permissions.prepare_provenance(conn, "TGT_OWNER", "EXECUTOR")
    assert second == []
    assert statements == []
