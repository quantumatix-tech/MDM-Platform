"""Oracle native materialized-view FULL migration contracts."""
from unittest.mock import MagicMock

import pytest

from core.connectors.base import MaterializedViewDef
from core.connectors.oracle.objects import create_materialized_view, refresh_materialized_view
from core.connectors.oracle.source import OracleSourceConnector


def _conn():
    conn = MagicMock()
    return conn, conn.cursor.return_value.__enter__.return_value


def test_oracle_table_discovery_excludes_mview_storage_tables():
    conn, cur = _conn()
    cur.fetchall.return_value = [("S33_SALES",)]
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = conn

    assert source.list_objects() == ["S33_SALES"]
    sql = str(cur.execute.call_args.args[0]).upper()
    assert "FROM USER_MVIEWS" in sql and "NOT EXISTS" in sql


def test_oracle_discovers_native_mview_and_refresh_metadata():
    conn, cur = _conn()
    mv_row = (
        "S33_MV_TOTAL", "SELECT DEPT_ID, SUM(AMOUNT) TOTAL_AMOUNT FROM S33_SALES GROUP BY DEPT_ID",
        "IMMEDIATE", "DEMAND", "COMPLETE", "N",
    )
    cur.fetchall.side_effect = lambda: [mv_row] if "FROM USER_MVIEWS" in str(
        cur.execute.call_args.args[0]
    ).upper() else []
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = conn

    [mv] = source.list_materialized_views()

    assert mv.name == "S33_MV_TOTAL"
    assert mv.schema_name == "SOURCE_OWNER"
    assert mv.refresh_mode == "DEMAND" and mv.refresh_method == "COMPLETE"
    assert any(
        "FROM USER_MVIEWS" in str(call.args[0]).upper()
        for call in cur.execute.call_args_list
    )


@pytest.mark.parametrize(
    "metadata", [("DEFERRED", "DEMAND", "COMPLETE", "N"),
                 ("IMMEDIATE", "COMMIT", "FAST", "N"),
                 ("IMMEDIATE", "DEMAND", "COMPLETE", "Y")],
)
def test_oracle_rejects_unsupported_mview_before_full_preflight(metadata):
    conn, cur = _conn()
    mv_row = ("MV_BAD", "SELECT 1 X FROM DUAL", *metadata)
    cur.fetchall.side_effect = lambda: [mv_row] if "FROM USER_MVIEWS" in str(
        cur.execute.call_args.args[0]
    ).upper() else []
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = conn

    with pytest.raises(RuntimeError, match="does not support materialized view MV_BAD"):
        source.list_materialized_views()


def test_oracle_creates_native_mview_then_explicitly_refreshes_complete():
    conn, cur = _conn()
    cur.fetchone.return_value = None
    def fetchall_for_sql():
        sql = str(cur.execute.call_args.args[0]).upper()
        return [("MATERIALIZED VIEW", "VALID")] if "SELECT OBJECT_TYPE, STATUS" in sql else []
    cur.fetchall.side_effect = fetchall_for_sql
    mv = MaterializedViewDef(
        "MV_TOTAL", "SELECT * FROM SOURCE_OWNER.SALES", schema_name="SOURCE_OWNER"
    )

    create_materialized_view(conn, mv, target_owner="TARGET_OWNER")
    create_sql = next(
        str(call.args[0]) for call in cur.execute.call_args_list
        if str(call.args[0]).upper().startswith("CREATE MATERIALIZED VIEW")
    )
    assert "CREATE MATERIALIZED VIEW \"MV_TOTAL\"" in create_sql
    assert "BUILD IMMEDIATE REFRESH COMPLETE ON DEMAND" in create_sql
    # The source qualifier is remapped to a quoted target owner. SALES was
    # unquoted in the source SQL, so Oracle folds it to uppercase identically
    # to the quoted spelling "SALES".
    assert '"TARGET_OWNER".SALES' in create_sql

    refresh_materialized_view(conn, "MV_TOTAL")
    assert "DBMS_MVIEW.REFRESH" in str(cur.execute.call_args_list[-1].args[0])
    assert cur.execute.call_args_list[-1].args[1] == {"name": "MV_TOTAL"}


def test_oracle_full_reuses_matching_native_mview_without_dropping_target_object():
    conn, cur = _conn()
    cur.fetchall.side_effect = lambda: (
        [("TABLE",), ("MATERIALIZED VIEW",)]
        if "SELECT OBJECT_TYPE FROM USER_OBJECTS" in str(cur.execute.call_args.args[0]).upper()
        else []
    )
    definition = 'SELECT * FROM "TARGET_OWNER".SALES'
    cur.fetchone.return_value = (
        "IMMEDIATE", "DEMAND", "COMPLETE", "N", definition, "VALID"
    )
    mv = MaterializedViewDef(
        "MV_TOTAL", "SELECT * FROM SOURCE_OWNER.SALES", schema_name="SOURCE_OWNER"
    )

    create_materialized_view(conn, mv, target_owner="TARGET_OWNER")

    assert not any(
        str(call.args[0]).upper().startswith(("DROP MATERIALIZED VIEW", "CREATE MATERIALIZED VIEW"))
        for call in cur.execute.call_args_list
    )
    assert conn.commit.call_count == 0


def test_oracle_rejects_genuine_table_conflict_without_mview_catalog_association():
    conn, cur = _conn()
    cur.fetchall.side_effect = lambda: (
        [("TABLE",)]
        if "SELECT OBJECT_TYPE FROM USER_OBJECTS" in str(cur.execute.call_args.args[0]).upper()
        else []
    )
    cur.fetchone.return_value = None
    mv = MaterializedViewDef(
        "MV_TOTAL", "SELECT * FROM SOURCE_OWNER.SALES", schema_name="SOURCE_OWNER"
    )

    with pytest.raises(RuntimeError, match="TABLE without matching USER_MVIEWS metadata"):
        create_materialized_view(conn, mv, target_owner="TARGET_OWNER")

    assert not any(
        str(call.args[0]).upper().startswith(("DROP", "CREATE MATERIALIZED VIEW"))
        for call in cur.execute.call_args_list
    )


def test_oracle_rejects_invalid_native_mview_even_when_storage_table_is_associated():
    conn, cur = _conn()
    def fetchall():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "SELECT OBJECT_TYPE, STATUS FROM USER_OBJECTS" in sql:
            return [("MATERIALIZED VIEW", "INVALID")]
        if "SELECT OBJECT_TYPE FROM USER_OBJECTS" in sql:
            return [("TABLE",), ("MATERIALIZED VIEW",)]
        return []
    cur.fetchall.side_effect = fetchall
    cur.fetchone.return_value = (
        "IMMEDIATE", "DEMAND", "COMPLETE", "N",
        'SELECT * FROM "TARGET_OWNER".SALES', "INVALID",
    )
    mv = MaterializedViewDef(
        "MV_TOTAL", "SELECT * FROM SOURCE_OWNER.SALES", schema_name="SOURCE_OWNER"
    )

    with pytest.raises(RuntimeError, match="compilation/status check failed for MV_TOTAL"):
        create_materialized_view(conn, mv, target_owner="TARGET_OWNER")

    assert any(
        "ALTER MATERIALIZED VIEW \"MV_TOTAL\" COMPILE" in str(call.args[0]).upper()
        for call in cur.execute.call_args_list
    )
    assert not any(
        str(call.args[0]).upper().startswith(("DROP", "CREATE MATERIALIZED VIEW"))
        for call in cur.execute.call_args_list
    )


def test_oracle_repeated_full_revalidates_matching_mv_invalidated_by_base_ddl():
    conn, cur = _conn()
    def fetchall():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "SELECT OBJECT_TYPE, STATUS FROM USER_OBJECTS" in sql:
            return [("MATERIALIZED VIEW", "VALID")]
        if "SELECT OBJECT_TYPE FROM USER_OBJECTS" in sql:
            return [("TABLE",), ("MATERIALIZED VIEW",)]
        return []
    cur.fetchall.side_effect = fetchall
    matching_valid = (
        "IMMEDIATE", "DEMAND", "COMPLETE", "N",
        'SELECT * FROM "TARGET_OWNER".SALES', "VALID",
    )
    invalid_after_schema_change = (
        "IMMEDIATE", "DEMAND", "COMPLETE", "N",
        'SELECT * FROM "TARGET_OWNER".SALES', "INVALID",
    )
    metadata_rows = iter([matching_valid, invalid_after_schema_change])
    def fetchone():
        sql = str(cur.execute.call_args.args[0]).upper()
        if "SELECT MVIEW_NAME FROM USER_MVIEWS" in sql:
            return ("MV_TOTAL",)
        if "FROM USER_MVIEWS M" in sql:
            return next(metadata_rows)
        return None
    cur.fetchone.side_effect = fetchone
    mv = MaterializedViewDef(
        "MV_TOTAL", "SELECT * FROM SOURCE_OWNER.SALES", schema_name="SOURCE_OWNER"
    )

    # First FULL reuses the valid MV. Simulate supported base-table DDL between
    # runs, then ensure the next FULL revalidates the same definition first.
    create_materialized_view(conn, mv, target_owner="TARGET_OWNER")
    create_materialized_view(conn, mv, target_owner="TARGET_OWNER")
    refresh_materialized_view(conn, "MV_TOTAL")

    statements = [str(call.args[0]).upper() for call in cur.execute.call_args_list]
    compile_index = next(
        index for index, statement in enumerate(statements)
        if "ALTER MATERIALIZED VIEW \"MV_TOTAL\" COMPILE" in statement
    )
    refresh_index = next(
        index for index, statement in enumerate(statements)
        if "DBMS_MVIEW.REFRESH" in statement
    )
    assert compile_index < refresh_index
    assert conn.commit.call_count == 2  # one revalidation and one COMPLETE refresh
    assert not any(
        str(call.args[0]).upper().startswith(("DROP", "CREATE MATERIALIZED VIEW"))
        for call in cur.execute.call_args_list
    )


def test_oracle_rejects_mismatched_same_name_native_mview_without_replacement():
    conn, cur = _conn()
    cur.fetchall.side_effect = lambda: (
        [("MATERIALIZED VIEW",)]
        if "SELECT OBJECT_TYPE FROM USER_OBJECTS" in str(cur.execute.call_args.args[0]).upper()
        else []
    )
    cur.fetchone.return_value = (
        "IMMEDIATE", "DEMAND", "COMPLETE", "N",
        'SELECT * FROM "TARGET_OWNER".OTHER_SALES', "VALID",
    )
    mv = MaterializedViewDef(
        "MV_TOTAL", "SELECT * FROM SOURCE_OWNER.SALES", schema_name="SOURCE_OWNER"
    )

    with pytest.raises(RuntimeError, match="materialized view MV_TOTAL differs from the source"):
        create_materialized_view(conn, mv, target_owner="TARGET_OWNER")

    assert conn.commit.call_count == 0
    assert not any(
        str(call.args[0]).upper().startswith(("DROP", "CREATE MATERIALIZED VIEW"))
        for call in cur.execute.call_args_list
    )
