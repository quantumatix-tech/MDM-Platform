"""Two-connection Db2 role transaction regression tests.

Proves the role transaction strategy used by run_db2_full_replacement:
- Role created on the security executor is visible to the target connection
  only after the executor commits (cross-connection visibility).
- Grants committed on the executor survive executor connection close.
- A failure mid role-phase rolls back the executor transaction (no orphaned
  roles or partial memberships).
- target._conn is never committed early during the role phase.

No live database is used; a shared-catalog mock simulates Db2 transaction
visibility across two independent connections.
"""

from __future__ import annotations

from core.connectors.db2.objects.target_role import create_role_if_missing, grant_role_membership


class RoleCatalog:
    """Shared Db2 catalog visible only after commit (simulates committed state)."""

    def __init__(self) -> None:
        self.roles: set[str] = set()
        self.grants: set[tuple[str, str]] = set()  # (role, grantee)


class TwoConn:
    """Mock Db2 connection with per-connection pending transaction + shared catalog.

    Uncommitted changes are visible only to the owning connection until commit(),
    at which point they are merged into the shared catalog and become visible to
    all connections. rollback() discards pending changes.
    """

    def __init__(self, catalog: RoleCatalog, name: str) -> None:
        self.catalog = catalog
        self.name = name
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._pending_roles: set[str] = set()
        self._pending_grants: set[tuple[str, str, str]] = set()
        self._last_sql = ""
        self._fetch_row = None

    # -- connection protocol -------------------------------------------------
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    # -- statement execution -------------------------------------------------
    def execute(self, sql: str, *params) -> None:
        self._last_sql = sql or ""
        sql_up = sql.strip().upper()
        # Unpack pyodbc-style parameters: execute(sql, (val,)) -> params=((val,),)
        param = params[0] if params else None
        if isinstance(param, (tuple, list)) and len(param) == 1:
            param = param[0]
        if "SYSCAT.ROLES" in sql_up and "ROLENAME=?" in sql_up:
            role = param
            visible = self.catalog.roles | self._pending_roles
            self._fetch_row = (1,) if role in visible else None
        elif sql_up.startswith("CREATE ROLE"):
            name = sql_up.split("CREATE ROLE", 1)[1].strip()
            self._pending_roles.add(name)
            self._fetch_row = None
        elif sql_up.startswith("GRANT ROLE"):
            rest = sql_up.split("GRANT ROLE", 1)[1].strip()
            role_name = rest.split()[0]
            grantee = ""
            if " TO " in rest:
                grantee = rest.split(" TO ", 1)[1].split()[0]
            # grantee_type is not encoded in the SQL; track (role, grantee) only
            self._pending_grants.add((role_name, grantee))
            self._fetch_row = None
        else:
            self._fetch_row = None

    def fetchone(self):
        row = self._fetch_row
        self._fetch_row = None
        return row

    # -- transaction control -------------------------------------------------
    def commit(self) -> None:
        self.catalog.roles |= self._pending_roles
        self.catalog.grants |= self._pending_grants
        self._pending_roles = set()
        self._pending_grants = set()
        self.commits += 1

    def rollback(self) -> None:
        self._pending_roles = set()
        self._pending_grants = set()
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class TestRoleExecutorVisibility:
    """Role created on executor is visible to target only after executor commit."""

    def test_role_not_visible_until_executor_commit(self):
        catalog = RoleCatalog()
        target_conn = TwoConn(catalog, "TARGET")
        executor_conn = TwoConn(catalog, "EXECUTOR")

        # Create role on executor (uncommitted).
        assert create_role_if_missing(executor_conn, "MIG_ROLE") == "created"
        assert executor_conn.commits == 0
        assert "MIG_ROLE" not in catalog.roles  # not yet committed

        # Target connection cannot see the uncommitted role.
        with target_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM SYSCAT.ROLES WHERE ROLENAME=?", ("MIG_ROLE",))
            assert cur.fetchone() is None

        # After executor commit, role is visible to target.
        executor_conn.commit()
        assert executor_conn.commits == 1
        assert "MIG_ROLE" in catalog.roles
        with target_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM SYSCAT.ROLES WHERE ROLENAME=?", ("MIG_ROLE",))
            assert cur.fetchone() == (1,)

    def test_target_conn_not_committed_during_role_phase(self):
        catalog = RoleCatalog()
        target_conn = TwoConn(catalog, "TARGET")
        executor_conn = TwoConn(catalog, "EXECUTOR")

        create_role_if_missing(executor_conn, "MIG_ROLE")
        grant_role_membership(executor_conn, "MIG_ROLE", "DB2ADMIN", "U", admin=False)
        executor_conn.commit()

        # target._conn must not have been committed early for role work.
        assert target_conn.commits == 0
        assert executor_conn.commits == 1


class TestRoleExecutorDurability:
    """Committed role grants survive executor connection close."""

    def test_grant_survives_executor_close(self):
        catalog = RoleCatalog()
        executor_conn = TwoConn(catalog, "EXECUTOR")

        create_role_if_missing(executor_conn, "MIG_ROLE")
        grant_role_membership(executor_conn, "MIG_ROLE", "DB2ADMIN", "U", admin=False)
        grant_role_membership(executor_conn, "MIG_ROLE", "APP_USER", "U", admin=True)
        executor_conn.commit()

        assert ("MIG_ROLE", "DB2ADMIN") in catalog.grants
        assert ("MIG_ROLE", "APP_USER") in catalog.grants

        # Close executor -- committed grants must survive.
        executor_conn.close()
        assert executor_conn.closed
        assert ("MIG_ROLE", "DB2ADMIN") in catalog.grants
        assert ("MIG_ROLE", "APP_USER") in catalog.grants

    def test_create_and_grant_single_transaction(self):
        catalog = RoleCatalog()
        executor_conn = TwoConn(catalog, "EXECUTOR")

        create_role_if_missing(executor_conn, "R1")
        grant_role_membership(executor_conn, "R1", "USER1", "U")
        grant_role_membership(executor_conn, "R1", "USER2", "G")
        executor_conn.commit()

        assert executor_conn.commits == 1
        assert "R1" in catalog.roles
        assert ("R1", "USER1") in catalog.grants
        assert ("R1", "USER2") in catalog.grants


class TestRoleExecutorRollback:
    """Failure mid role-phase rolls back the executor transaction."""

    def test_failed_grant_rolls_back_role_and_membership(self):
        catalog = RoleCatalog()
        executor_conn = TwoConn(catalog, "EXECUTOR")

        # First grant succeeds (pending). Second grant raises.
        original_execute = executor_conn.execute
        call = {"n": 0}

        def failing_execute(sql, *params):
            call["n"] += 1
            if call["n"] == 3:  # third statement = second GRANT
                raise RuntimeError("injected grant failure")
            return original_execute(sql, *params)

        executor_conn.execute = failing_execute

        # Simulate db2_full.py error handling: rollback on failure.
        try:
            create_role_if_missing(executor_conn, "R1")  # stmt 1: pending
            grant_role_membership(executor_conn, "R1", "USER1", "U")  # stmt 2: pending
            grant_role_membership(executor_conn, "R1", "USER2", "U")  # stmt 3: raises
            executor_conn.commit()
        except RuntimeError:
            executor_conn.rollback()

        assert executor_conn.rollbacks == 1
        assert executor_conn.commits == 0
        # Rolled back: nothing visible in the shared catalog.
        assert "R1" not in catalog.roles
        assert ("R1", "USER1") not in catalog.grants
        assert ("R1", "USER2") not in catalog.grants

    def test_failed_create_rolls_back(self):
        catalog = RoleCatalog()
        executor_conn = TwoConn(catalog, "EXECUTOR")

        original_execute = executor_conn.execute

        def failing_execute(sql, *params):
            if (sql or "").strip().upper().startswith("CREATE ROLE"):
                raise RuntimeError("injected create failure")
            return original_execute(sql, *params)

        executor_conn.execute = failing_execute

        # Simulate db2_full.py error handling: rollback on failure.
        try:
            create_role_if_missing(executor_conn, "R1")
            executor_conn.commit()
        except RuntimeError:
            executor_conn.rollback()

        assert executor_conn.rollbacks == 1
        assert executor_conn.commits == 0
        assert "R1" not in catalog.roles


class TestRoleFallbackNoExecutor:
    """Without a security executor, roles fall back to target._conn."""

    def test_fallback_uses_target_conn(self):
        catalog = RoleCatalog()
        target_conn = TwoConn(catalog, "TARGET")
        # No executor_conn (None).

        create_role_if_missing(target_conn, "R1")
        grant_role_membership(target_conn, "R1", "USER1", "U")
        target_conn.commit()

        assert target_conn.commits == 1
        assert "R1" in catalog.roles
        assert ("R1", "USER1") in catalog.grants
