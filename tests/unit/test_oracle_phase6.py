"""Focused contracts for Oracle FULL comments, synonyms, and table partitions."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, Schema, SynonymDef
from core.connectors.oracle import objects, replacement
from core.connectors.oracle.target import OracleTargetConnector
from tests.e2e.oracle.run_validation import _partition_inventory


def _connection():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    return conn, cursor


def test_partition_discovery_preserves_range_and_list_bounds():
    for method, bound in (("RANGE", "10"), ("LIST", "('NORTH','SOUTH')")):
        conn, cur = _connection()
        cur.fetchone.return_value = (method, "NONE", None, "NO", "NO", None, "VALID", 1)
        cur.fetchall.side_effect = [
            [("DEPT_ID",)],
            [("P_ONE", 1, bound, "NO", "NO", "NO", "NO", "ON")],
        ]

        found = objects.discover_table_partitioning(conn, "SALES")

        assert found == {
            "method": method, "keys": ["DEPT_ID"],
            "partitions": [{"name": "P_ONE", "position": 1, "high_value": bound}],
        }


def test_partition_discovery_accepts_oracle_catalog_date_high_value_clob():
    conn, cur = _connection()
    cur.fetchone.return_value = ("RANGE", "NONE", None, "NO", "NO", None, "VALID", 2)
    catalog_date = (
        "TO_DATE(' 2025-01-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
        "'NLS_CALENDAR=GREGORIAN')"
    )
    high_value_lob = MagicMock()
    high_value_lob.read.return_value = catalog_date
    cur.fetchall.side_effect = [
        [("EVENT_DATE",)],
        [
            ("P_2024", 1, high_value_lob, "NO", "NO", "NO", "NO", "ON"),
            ("P_MAX", 2, "MAXVALUE", "NO", "NO", "NO", "NO", "ON"),
        ],
    ]

    metadata = objects.discover_table_partitioning(conn, "PARTITIONED_EVENTS")

    assert metadata is not None
    assert [part["high_value"] for part in metadata["partitions"]] == [
        catalog_date,
        "MAXVALUE",
    ]
    high_value_lob.read.assert_called_once_with()


@pytest.mark.parametrize(
    "bound",
    [
        "DATE '2025-01-01'",
        "TIMESTAMP '2025-01-01 12:30:00.125'",
        (
            "TO_DATE(' 2025-01-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
            "'NLS_CALENDAR=GREGORIAN')"
        ),
        (
            "TO_DATE(' 2025-01-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
            "'NLS_CALENDAR=THAI BUDDHA')"
        ),
        "TO_TIMESTAMP('2025-01-01 12:30:00.125', 'YYYY-MM-DD HH24:MI:SS.FF')",
        "TO_TIMESTAMP_TZ('2025-01-01 12:30:00 +00:00', 'YYYY-MM-DD HH24:MI:SS TZH:TZM')",
        "MAXVALUE",
        "-12.5",
        "('NORTH','SOUTH')",
    ],
)
def test_partition_bound_parser_accepts_literal_only_oracle_forms(bound):
    objects._validate_partition_bound_expression(bound, "EVENTS", "P_BOUND")


@pytest.mark.parametrize(
    "bound",
    [
        "TO_CHAR('2025-01-01')",
        "TO_DATE('2025-01-01', 'YYYY-MM-DD'); DROP TABLE EVENTS",
        "TO_DATE('2025-01-01', 'YYYY-MM-DD', 'NLS_CALENDAR=GREGORIAN') || 'x'",
        "TO_DATE('2025-01-01', 'YYYY-MM-DD', 'NLS_CALENDAR=GREGORIAN');",
        "DATE '2025-1-1'",
        "TIMESTAMP 'not a timestamp'",
        "TO_DATE('2025-01-01', 'YYYY-MM-DD', 'ARBITRARY=VALUE')",
        "('NORTH', (SELECT 1 FROM DUAL))",
        "TO_DATE('unterminated, 'YYYY-MM-DD')",
    ],
)
def test_partition_bound_parser_rejects_malformed_or_executable_syntax(bound):
    with pytest.raises(RuntimeError, match="unsupported expression"):
        objects._validate_partition_bound_expression(bound, "EVENTS", "P_BOUND")


def test_oracle_partition_fixture_bounds_are_discoverable_and_ordered():
    fixture = (
        Path(__file__).parents[1]
        / "e2e"
        / "oracle"
        / "fixtures"
        / "12_create_partitions.sql"
    ).read_text(encoding="utf-8")
    expected = [
        ("E2E_EVENT_2024", "DATE '2025-01-01'"),
        ("E2E_EVENT_2025_H1", "DATE '2025-07-01'"),
        ("E2E_EVENT_2025_H2", "DATE '2026-01-01'"),
        ("E2E_EVENT_MAX", "MAXVALUE"),
    ]

    discovered = []
    for name, bound in expected:
        assert f"PARTITION {name} VALUES LESS THAN ({bound})" in fixture
        objects._validate_partition_bound_expression(bound, "E2E_PARTITIONED_EVENT", name)
        discovered.append((name, bound))
    assert discovered == expected


def test_partition_validation_inventory_compares_discovered_bounds_and_row_counts():
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.__enter__.return_value = cursor
    catalog_date = (
        "TO_DATE(' 2025-01-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
        "'NLS_CALENDAR=GREGORIAN')"
    )
    cursor.fetchall.side_effect = [
        [("PARTITIONED_EVENTS",)],
        [("EVENT_DATE",)],
        [
            ("P_2024", 1, catalog_date, "NO", "NO", "NO", "NO", "ON"),
            ("P_MAX", 2, "MAXVALUE", "NO", "NO", "NO", "NO", "ON"),
        ],
    ]
    cursor.fetchone.side_effect = [
        ("RANGE", "NONE", None, "NO", "NO", None, "VALID", 2),
        (3,),
        (1,),
    ]

    inventory = _partition_inventory(connection, {"PARTITIONED_EVENTS"})

    assert inventory["PARTITIONED_EVENTS"]["definition"]["partitions"] == [
        {"name": "P_2024", "position": 1, "high_value": catalog_date},
        {"name": "P_MAX", "position": 2, "high_value": "MAXVALUE"},
    ]
    assert inventory["PARTITIONED_EVENTS"]["row_counts"] == {"P_2024": 3, "P_MAX": 1}


def test_partition_discovery_rejects_multi_column_bounds_before_ddl():
    conn, cur = _connection()
    cur.fetchone.return_value = ("RANGE", "NONE", None, "NO", "NO", None, "VALID", 1)
    cur.fetchall.side_effect = [
        [("DEPT_ID",), ("SALE_DATE",)],
        [("P_ONE", 1, "(10, DATE '2026-01-01')", "NO", "NO", "NO", "NO", "ON")],
    ]

    with pytest.raises(RuntimeError, match="multi-column partition keys"):
        objects.discover_table_partitioning(conn, "SALES")


def test_partition_discovery_rejects_hash_or_subpartitioning():
    conn, cur = _connection()
    cur.fetchone.return_value = ("HASH", "RANGE", None, "NO", "NO", None, "VALID", 1)
    cur.fetchall.side_effect = [[("ID",)], []]

    with pytest.raises(RuntimeError, match="partitioning_type=HASH.*subpartitioning_type=RANGE"):
        objects.discover_table_partitioning(conn, "SALES")

    malformed_conn, malformed_cur = _connection()
    malformed_cur.fetchone.return_value = ()
    with pytest.raises(RuntimeError, match="malformed USER_PART_TABLES metadata"):
        objects.discover_table_partitioning(malformed_conn, "SALES")


def test_oracle_table_ddl_retains_range_partitions():
    conn, cur = _connection()
    cur.fetchone.return_value = None
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = conn
    schema = Schema(
        name="SALES", schema_name="SOURCE_OWNER",
        columns=[Column("DEPT_ID", "NUMBER", nullable=False, target_type="NUMBER")],
        partition_method="RANGE",
        options={"oracle_partitioning": {
            "method": "RANGE", "keys": ["DEPT_ID"],
            "partitions": [
                {"name": "P_LOW", "position": 1, "high_value": "10"},
                {"name": "P_HIGH", "position": 2, "high_value": "MAXVALUE"},
            ],
        }},
    )

    target.create_object_if_missing(schema)

    ddl = next(call.args[0] for call in cur.execute.call_args_list
               if str(call.args[0]).startswith("CREATE TABLE"))
    assert 'PARTITION BY RANGE ("DEPT_ID")' in ddl
    assert 'PARTITION "P_LOW" VALUES LESS THAN (10)' in ddl
    assert 'PARTITION "P_HIGH" VALUES LESS THAN (MAXVALUE)' in ddl


def test_oracle_table_ddl_preserves_catalog_date_bounds_and_partition_order():
    conn, cur = _connection()
    cur.fetchone.return_value = None
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = conn
    date_bound = (
        "TO_DATE(' 2025-01-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
        "'NLS_CALENDAR=GREGORIAN')"
    )
    later_bound = (
        "TO_DATE(' 2025-07-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
        "'NLS_CALENDAR=GREGORIAN')"
    )
    schema = Schema(
        name="PARTITIONED_EVENTS",
        columns=[Column("EVENT_DATE", "DATE", nullable=False, target_type="DATE")],
        partition_method="RANGE",
        options={"oracle_partitioning": {
            "method": "RANGE", "keys": ["EVENT_DATE"],
            "partitions": [
                {"name": "P_2024", "position": 1, "high_value": date_bound},
                {"name": "P_2025", "position": 2, "high_value": later_bound},
                {"name": "P_MAX", "position": 3, "high_value": "MAXVALUE"},
            ],
        }},
    )

    target.create_object_if_missing(schema)

    ddl = next(call.args[0] for call in cur.execute.call_args_list
               if str(call.args[0]).startswith("CREATE TABLE"))
    positions = [ddl.index(f'PARTITION "{name}"') for name in ("P_2024", "P_2025", "P_MAX")]
    assert positions == sorted(positions)
    assert f'PARTITION "P_2024" VALUES LESS THAN ({date_bound})' in ddl
    assert f'PARTITION "P_2025" VALUES LESS THAN ({later_bound})' in ddl
    assert 'PARTITION "P_MAX" VALUES LESS THAN (MAXVALUE)' in ddl


def test_oracle_table_ddl_retains_list_default_partition():
    conn, cur = _connection()
    cur.fetchone.return_value = None
    target = OracleTargetConnector({"username": "TARGET_OWNER", "source_engine": "oracle"})
    target._conn = conn
    schema = Schema(
        name="SALES", columns=[Column("REGION", "VARCHAR2", target_type="VARCHAR2(20)")],
        partition_method="LIST",
        options={"oracle_partitioning": {
            "method": "LIST", "keys": ["REGION"],
            "partitions": [{"name": "P_DEFAULT", "position": 1, "high_value": "DEFAULT"}],
        }},
    )

    target.create_object_if_missing(schema)

    ddl = next(call.args[0] for call in cur.execute.call_args_list
               if str(call.args[0]).startswith("CREATE TABLE"))
    assert 'PARTITION "P_DEFAULT" VALUES (DEFAULT)' in ddl


def test_oracle_comment_application_escapes_literal_and_preserves_unicode():
    conn, cur = _connection()
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn

    target.apply_comment(type("Comment", (), {
        "object_type": "COLUMN", "object_name": "SALES", "column_name": "REGION",
        "comment": "Région d'Asie",
    })())

    statement = next(
        call.args[0] for call in cur.execute.call_args_list
        if str(call.args[0]).upper().startswith("COMMENT ON COLUMN")
    )
    assert statement == 'COMMENT ON COLUMN "SALES"."REGION" IS \'Région d\'\'Asie\''
    conn.commit.assert_called_once_with()


def test_oracle_comment_discovery_keeps_table_and_column_text_exact():
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("SALES", "Résumé and notes", "TABLE")],
        [("SALES", "REGION", "Région d'Asie", "TABLE")],
    ]

    comments = objects.discover_comments(conn, "SOURCE_OWNER")

    assert [(item.object_type, item.object_name, item.column_name, item.comment)
            for item in comments] == [
        ("TABLE", "SALES", None, "Résumé and notes"),
        ("COLUMN", "SALES", "REGION", "Région d'Asie"),
    ]


def test_oracle_comment_discovery_rejects_view_comment_before_replacement():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[("V_SALES", "unsupported", "VIEW")], []]

    with pytest.raises(RuntimeError, match="comments on unsupported target"):
        objects.discover_comments(conn, "SOURCE_OWNER")


def test_oracle_synonym_discovery_orders_chains_and_rejects_external_references():
    conn, cur = _connection()
    cur.fetchall.side_effect = [
        [("ALIAS_TWO", "SOURCE_OWNER", "ALIAS_ONE", None),
         ("ALIAS_ONE", "SOURCE_OWNER", "SALES", None)],
        [],
        [("ALIAS_TWO", "SYNONYM"), ("ALIAS_ONE", "SYNONYM"), ("SALES", "TABLE")],
    ]

    synonyms = objects.discover_synonyms(conn, "SOURCE_OWNER")

    assert [item.name for item in synonyms] == ["ALIAS_ONE", "ALIAS_TWO"]
    assert synonyms[1].base_owner == "SOURCE_OWNER"
    assert synonyms[1].base_name == "ALIAS_ONE"


@pytest.mark.parametrize(
    "synonym_row,objects_rows,match",
    [
        (("MISSING", "SOURCE_OWNER", "NO_SUCH_OBJECT", None), [], "does not exist"),
        (("REMOTE", "OTHER_OWNER", "SALES", None), [("SALES", "TABLE")], "cross-owner"),
        (("REMOTE", "SOURCE_OWNER", "SALES", "REMOTE_DB"), [("SALES", "TABLE")], "database-link"),
    ],
)
def test_oracle_synonym_discovery_rejects_unresolved_external_and_db_link(
    synonym_row, objects_rows, match,
):
    conn, cur = _connection()
    cur.fetchall.side_effect = [[synonym_row], [], objects_rows]

    with pytest.raises(RuntimeError, match=match):
        objects.discover_synonyms(conn, "SOURCE_OWNER")


def test_oracle_synonym_discovery_fails_closed_for_relevant_public_alias():
    conn, cur = _connection()
    cur.fetchall.side_effect = [[], [("PUBLIC_SALES", "SOURCE_OWNER", "SALES", None)], []]

    with pytest.raises(RuntimeError, match="PUBLIC synonym"):
        objects.discover_synonyms(conn, "SOURCE_OWNER")


def test_oracle_synonym_requires_create_synonym_privilege():
    conn, cur = _connection()
    cur.fetchall.return_value = [("CREATE TABLE",)]

    with pytest.raises(RuntimeError, match="CREATE SYNONYM"):
        objects.validate_privileges(
            conn, views=[], sequences=[], routines=[], triggers=[],
            synonyms=[SynonymDef("SALES_ALIAS", "SOURCE_OWNER", "SOURCE_OWNER.SALES")],
        )


def test_replacement_plan_can_drop_target_only_private_synonym():
    conn, _ = _connection()
    plan = replacement.ReplacementPlan(
        owner="TARGET_OWNER", plan_id="PLAN",
        inventory=(), cleanup=({"type": "SYNONYM", "name": "OLD_ALIAS"},),
        implicit_cleanup=(), blockers=(), inspection_errors=(), dependency_impact=(),
        grants_impact=(), source_inventory=(),
    )

    statements = replacement.apply_plan(conn, plan)

    assert statements == ['DROP SYNONYM "OLD_ALIAS"']
    conn.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
        'DROP SYNONYM "OLD_ALIAS"'
    )


def test_oracle_private_synonym_remaps_source_owner_to_target_owner():
    conn, cur = _connection()
    cur.fetchone.return_value = (1,)
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn

    target.create_synonym(SynonymDef(
        "SALES_ALIAS", "SOURCE_OWNER", "SOURCE_OWNER.SALES",
        base_owner="SOURCE_OWNER", base_name="SALES",
    ))

    statement = cur.execute.call_args.args[0]
    assert statement == (
        'CREATE OR REPLACE SYNONYM "SALES_ALIAS" FOR "TARGET_OWNER"."SALES"'
    )


def test_oracle_synonym_rejects_public_and_cross_owner_definitions():
    conn, _ = _connection()
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    target._conn = conn

    with pytest.raises(RuntimeError, match="never creates or modifies PUBLIC"):
        target.create_synonym(SynonymDef(
            "SHARED_ALIAS", "SOURCE_OWNER", "SOURCE_OWNER.SALES",
            base_owner="SOURCE_OWNER", base_name="SALES", is_public=True,
        ))
    with pytest.raises(RuntimeError, match="unsafe synonym owner remapping"):
        target.create_synonym(SynonymDef(
            "EXTERNAL_ALIAS", "SOURCE_OWNER", "OTHER_OWNER.SALES",
            base_owner="OTHER_OWNER", base_name="SALES",
        ))
