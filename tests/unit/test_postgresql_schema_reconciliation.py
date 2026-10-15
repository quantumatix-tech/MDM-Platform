from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.connectors.base import (
    Column,
    Index,
    PartitionDef,
    Schema,
    SourceConnector,
    TargetConnector,
    UniqueConstraint,
    UpsertResult,
    ViewDefinition,
)
from core.connectors.postgresql.target import PostgresTargetConnector
from core.orchestrator import MigrationOrchestrator


def test_postgresql_reconciliation_drops_extra_target_column_only():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("customerid", "integer"), ("fullname", "text"), ("manual_note", "text")],
        [],
        [],
    ]
    schema = Schema(
        name="customers",
        schema_name="training",
        columns=[
            Column(name="customerid", source_type="integer"),
            Column(name="fullname", source_type="text"),
        ],
    )

    with patch(
        "core.connectors.postgresql.target._postgres_table.create_table"
    ) as create_table:
        result = target.reconcile_postgresql_table(schema)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert result == "reconciled"
    assert statements[0].startswith("SELECT c.column_name, pg_catalog.format_type(")
    assert 'ALTER TABLE "training"."customers" DROP COLUMN "manual_note"' in statements
    assert not any("DROP TABLE" in statement for statement in statements)
    assert not any("manual_table" in statement for statement in statements)
    create_table.assert_called_once_with(target._conn, schema)
    target._conn.commit.assert_called_once_with()


def test_postgresql_reconciliation_drops_target_only_constraints_and_indexes():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("customerid", "integer"), ("email", "text")],
        [("pk_customers", "p"), ("uq_customers_email", "u"), ("chk_manual", "c")],
        [("customers_pkey",), ("uq_customers_email",), ("idx_customers_run3_extra",)],
    ]
    schema = Schema(
        name="customers",
        schema_name="training",
        columns=[
            Column(name="customerid", source_type="integer"),
            Column(name="email", source_type="text"),
        ],
        primary_key=["customerid"],
        primary_key_name="pk_customers",
        unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
        indexes=[Index(name="customers_pkey", columns=["customerid"], constraint_backed=True)],
    )

    with patch("core.connectors.postgresql.target._postgres_table.create_table"):
        target.reconcile_postgresql_table(schema)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'ALTER TABLE "training"."customers" DROP CONSTRAINT "chk_manual"' in statements
    assert 'DROP INDEX "training"."idx_customers_run3_extra"' in statements
    assert not any('DROP CONSTRAINT "pk_customers"' in statement for statement in statements)
    assert not any('DROP CONSTRAINT "uq_customers_email"' in statement for statement in statements)
    assert not any('DROP TABLE' in statement and 'manual_table' in statement for statement in statements)


def test_postgresql_full_reconciliation_preserves_target_only_table():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    phase_order = []
    target.reconcile_postgresql_table = MagicMock(
        side_effect=lambda *_args: phase_order.append("table_reconcile") or "reconciled"
    )
    source.get_partitioned_tables = MagicMock(return_value=[])
    source.list_views.return_value = [
        ViewDefinition(
            name="vw_ordersummary",
            schema_name="training",
            definition="SELECT fullname AS customername FROM training.customers",
        )
    ]
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = Schema(
        name="customers",
        schema_name="training",
        columns=[Column(name="customerid", source_type="integer")],
    )
    source.get_object_count.return_value = 0
    source.export_full.return_value = iter([])
    source.list_extensions.return_value = []
    source.list_schemas.return_value = []
    source.list_types.return_value = []
    source.list_events.return_value = []
    source.list_functions.return_value = []
    source.list_users.return_value = []
    source.list_roles.return_value = []
    source.get_all_triggers.return_value = []
    target.upsert_batch.return_value = UpsertResult()
    target.inspect_schema.return_value = None
    target.apply_constraints.return_value = None
    target.create_view.side_effect = lambda _view: phase_order.append("view_recreate")

    orchestrator = MigrationOrchestrator(
        source,
        target,
        {
            "source": {"engine": "postgresql"},
            "target": {"engine": "postgresql"},
            "migration": {"reconcile_target_schema": True},
        },
    )
    orchestrator.validate = MagicMock(return_value={"status": "success"})

    orchestrator.run_full()

    target.reconcile_postgresql_table.assert_called_once_with(
        source.get_schema.return_value, source.list_views.return_value
    )
    target.create_object_if_missing.assert_not_called()
    assert phase_order.index("table_reconcile") < phase_order.index("view_recreate")
    assert "manual_table" not in source.list_objects.return_value
    assert [
        call.args[0].name
        for call in target.reconcile_postgresql_table.call_args_list
    ] == ["customers"]


def test_postgresql_partition_reconciliation_drops_extra_managed_partition_only():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        ("partitionedorders_2024",),
        ("partitionedorders_2026",),
    ]

    removed = target.reconcile_partitions(
        [
            PartitionDef(
                name="partitionedorders_2024",
                parent_table="partitionedorders",
                bound="FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
                schema="training",
            )
        ],
        [Schema(name="partitionedorders", schema_name="training", partition_key="RANGE (orderdate)")],
    )

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert removed == ["partitionedorders_2026"]
    assert any(
        statement == (
            'ALTER TABLE "training"."partitionedorders" DETACH PARTITION '
            '"training"."partitionedorders_2026"'
        )
        for statement in statements
    )
    assert 'DROP TABLE "training"."partitionedorders_2026"' in statements
    assert not any("manual_table" in statement for statement in statements)
    target._conn.commit.assert_called_once_with()


def test_postgresql_reconciliation_expands_varchar_column_to_source_length():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("fullname", "character varying(50)")],
        [],
        [],
    ]
    schema = Schema(
        name="customers",
        schema_name="training",
        columns=[Column(name="fullname", source_type="character varying(100)")],
    )

    with patch("core.connectors.postgresql.target._postgres_table.create_table"):
        result = target.reconcile_postgresql_table(schema)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert result == "reconciled"
    assert (
        'ALTER TABLE "training"."customers" ALTER COLUMN "fullname" '
        'TYPE character varying(100) USING "fullname"::character varying(100)'
    ) in statements


def test_postgresql_reconciliation_does_not_alter_matching_column_type():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("fullname", "character varying(100)")],
        [],
        [],
    ]
    schema = Schema(
        name="customers",
        schema_name="training",
        columns=[Column(name="fullname", source_type="character varying(100)")],
    )

    with patch("core.connectors.postgresql.target._postgres_table.create_table"):
        result = target.reconcile_postgresql_table(schema)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert result == "unchanged"
    assert not any("ALTER COLUMN" in statement for statement in statements)


def test_postgresql_reconciliation_restores_missing_source_column():
    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("customerid", "integer")],
        [],
        [],
    ]
    schema = Schema(
        name="customers",
        schema_name="training",
        columns=[
            Column(name="customerid", source_type="integer"),
            Column(name="fullname", source_type="character varying(100)", nullable=True),
        ],
    )

    with patch("core.connectors.postgresql.target._postgres_table.create_table"):
        target.reconcile_postgresql_table(schema)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'ALTER TABLE "training"."customers" ADD COLUMN "fullname" character varying(100) NULL' in statements


def test_type_reconciliation_drops_only_managed_dependent_view_before_alter():
    from core.connectors.base import ViewDefinition

    target = PostgresTargetConnector({"database": "target"})
    target._conn = MagicMock()
    cursor = target._conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("fullname", "character varying(50)")],
        [],
        [],
    ]
    schema = Schema(
        name="customers",
        schema_name="training",
        columns=[Column(name="fullname", source_type="character varying(100)")],
    )
    managed_view = ViewDefinition(
        name="vw_ordersummary",
        schema_name="training",
        definition="SELECT fullname AS customername FROM training.customers",
    )

    with patch("core.connectors.postgresql.target._postgres_table.create_table"):
        target.reconcile_postgresql_table(schema, [managed_view])

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    drop_index = statements.index('DROP VIEW IF EXISTS "training"."vw_ordersummary"')
    alter_index = next(i for i, statement in enumerate(statements) if "ALTER COLUMN \"fullname\" TYPE" in statement)
    assert drop_index < alter_index
    assert not any("CASCADE" in statement.upper() for statement in statements)
    assert not any("manual_view" in statement for statement in statements)
