from unittest.mock import MagicMock

from core.connectors.base import MaterializedViewDef, SequenceDef
from core.connectors.postgresql.objects.sequence import (
    apply_sequence_ownership,
    create_sequence,
    discover_sequences,
    restore_standalone_sequence_state,
)
from core.connectors.postgresql.objects.view import reconcile_materialized_view


def _connection():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    return conn, cursor


def test_sequence_discovery_includes_definition_and_is_called_state():
    conn, cursor = _connection()
    cursor.fetchall.return_value = [
        ("training", "acceptance_seq", "bigint", 10, 1, 999999, 2, False, 4, 16, None)
    ]
    cursor.fetchone.return_value = (True,)

    [sequence] = discover_sequences(conn, ("training",))

    assert sequence.start_value == 10
    assert sequence.increment == 2
    assert sequence.min_value == 1
    assert sequence.max_value == 999999
    assert sequence.cycle is False
    assert sequence.cache_size == 4
    assert sequence.data_type == "bigint"
    assert sequence.last_value == 16
    assert sequence.is_called is True


def test_existing_sequence_definition_is_altered_to_source_properties():
    conn, cursor = _connection()
    sequence = SequenceDef(
        name="acceptance_seq",
        schema="training",
        data_type="bigint",
        start_value=10,
        increment=2,
        min_value=1,
        max_value=999999,
        cycle=True,
        cache_size=4,
    )

    create_sequence(conn, sequence)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any(statement.startswith('CREATE SEQUENCE IF NOT EXISTS "training"."acceptance_seq"') for statement in statements)
    alter = next(statement for statement in statements if statement.startswith('ALTER SEQUENCE "training"."acceptance_seq"'))
    assert "AS bigint" in alter
    assert "START WITH 10" in alter
    assert "INCREMENT BY 2" in alter
    assert "MINVALUE 1" in alter
    assert "MAXVALUE 999999" in alter
    assert "CACHE 4" in alter
    assert "CYCLE" in alter


def test_standalone_sequence_state_preserves_last_value_and_is_called():
    conn, cursor = _connection()
    sequence = SequenceDef(
        name="acceptance_seq",
        schema="training",
        start_value=10,
        min_value=1,
        max_value=999999,
        increment=2,
        cycle=False,
        last_value=16,
        is_called=True,
    )

    restore_standalone_sequence_state(conn, sequence)

    cursor.execute.assert_called_once_with(
        "SELECT setval(%s::regclass, %s, %s)",
        ('"training"."acceptance_seq"', 16, True),
    )
    conn.commit.assert_called_once_with()


def test_unconsumed_sequence_state_uses_start_value_and_false_is_called():
    conn, cursor = _connection()
    sequence = SequenceDef(
        name="acceptance_seq",
        schema="training",
        start_value=10,
        min_value=1,
        max_value=999999,
        increment=2,
        cycle=False,
        last_value=None,
        is_called=False,
    )

    restore_standalone_sequence_state(conn, sequence)

    assert cursor.execute.call_args.args[1] == ('"training"."acceptance_seq"', 10, False)


def test_sequence_ownership_reconciliation_can_remove_target_only_ownership():
    conn, cursor = _connection()
    sequence = SequenceDef(
        name="acceptance_seq", schema="training", start_value=1,
        min_value=1, max_value=999999, increment=1, cycle=False,
    )

    apply_sequence_ownership(conn, sequence)

    assert cursor.execute.call_args.args[0] == (
        'ALTER SEQUENCE "training"."acceptance_seq" OWNED BY NONE'
    )


def test_source_managed_materialized_view_definition_is_restored_restrictively():
    conn, cursor = _connection()
    cursor.fetchone.return_value = ("SELECT 999 AS result",)
    mv = MaterializedViewDef(
        name="acceptance_mv",
        schema_name="training",
        definition="SELECT 5 AS result",
    )

    reconcile_materialized_view(conn, mv)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert 'DROP MATERIALIZED VIEW "training"."acceptance_mv"' in statements
    assert any(
        statement == (
            'CREATE MATERIALIZED VIEW "training"."acceptance_mv" AS '
            "SELECT 5 AS result WITH NO DATA"
        )
        for statement in statements
    )
    assert not any("CASCADE" in statement.upper() for statement in statements)
    conn.commit.assert_called_once_with()


def test_matching_materialized_view_is_not_dropped():
    conn, cursor = _connection()
    cursor.fetchone.return_value = ("SELECT 5 AS result",)
    mv = MaterializedViewDef(
        name="acceptance_mv",
        schema_name="training",
        definition="SELECT 5 AS result",
    )

    reconcile_materialized_view(conn, mv)

    assert not any("DROP MATERIALIZED VIEW" in call.args[0] for call in cursor.execute.call_args_list)
    assert not any("CREATE MATERIALIZED VIEW" in call.args[0] for call in cursor.execute.call_args_list)
    conn.commit.assert_called_once_with()
