from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, Schema
from core.connectors.mssql.objects.table import reconcile_table_columns


def _connection(fetchall_results: list[list[tuple]]):
    cursor = MagicMock()
    cursor.fetchall.side_effect = fetchall_results
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cursor


def _customer_schema(
    *, source_type: str = "nvarchar", size: int = 100, nullable: bool = False
) -> Schema:
    return Schema(
        name="Customers",
        schema_name="training",
        columns=[
            Column(
                name="FullName",
                source_type=source_type,
                size=size,
                nullable=nullable,
            )
        ],
    )


def _no_dependencies_and_constraints() -> list[list[tuple]]:
    # Indexes, foreign keys, schema-bound views, computed dependencies,
    # affected CHECK constraints, affected DEFAULT constraints, views to refresh.
    return [[], [], [], [], [], [], [("training", "vw_CustomerOrders")]]


def _orders_schema(expression: str = "DATEADD(SECOND, 1, OrderDate)") -> Schema:
    return Schema(
        name="Orders",
        schema_name="training",
        columns=[
            Column(
                name="ModifiedAt",
                source_type="datetime2",
                nullable=True,
                precision=27,
                scale=7,
                is_computed=True,
                computed_definition=expression,
            )
        ],
    )


def _computed_modified_at_metadata(expression: str) -> tuple:
    return (
        "ModifiedAt", "datetime2", 8, 27, 7, True, True, False, False,
        "sys", expression,
    )


def test_mssql_full_reconciliation_restores_nvarchar_150_to_100_not_null():
    conn, cursor = _connection(
        [[("FullName", "nvarchar", 300, 0, 0, False, False, False, False, "sys")]]
        + _no_dependencies_and_constraints()
    )

    result = reconcile_table_columns(conn, _customer_schema())

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    alter = (
        'ALTER TABLE "training"."Customers" ALTER COLUMN "FullName" '
        "nvarchar(100) NOT NULL"
    )
    assert alter in statements
    assert result == {"dropped_columns": [], "altered_columns": ["FullName"]}
    assert not any(statement.startswith("DELETE ") for statement in statements)
    assert 'EXEC sys.sp_refreshview N' in next(
        statement for statement in statements if "sp_refreshview" in statement
    )
    conn.commit.assert_called_once_with()
    conn.rollback.assert_not_called()


def test_mssql_full_reconciliation_does_not_alter_matching_definition():
    conn, cursor = _connection(
        [[("FullName", "nvarchar", 200, 0, 0, False, False, False, False, "sys")]]
    )

    result = reconcile_table_columns(conn, _customer_schema())

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert result == {"dropped_columns": [], "altered_columns": []}
    assert not any("ALTER COLUMN" in statement for statement in statements)
    assert not any("DELETE " in statement for statement in statements)
    conn.commit.assert_called_once_with()


def test_equivalent_is_active_with_filtered_city_index_does_not_alter_column():
    # The source IsActive BIT reports precision=1 through information_schema;
    # the target catalog value may represent that non-defining field as 0.
    # IX_Customers_City's filter depends on IsActive, so a false ALTER would
    # fail (or risk losing the index if a caller tried to drop it).
    conn, cursor = _connection(
        [[("IsActive", "bit", 1, 0, 0, False, False, False, False, "sys", None)]]
    )
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[Column(name="IsActive", source_type="bit", precision=1, nullable=False)],
    )

    result = reconcile_table_columns(conn, schema)

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert result == {"dropped_columns": [], "altered_columns": []}
    assert not any("ALTER COLUMN" in statement for statement in statements)
    assert not any("DROP INDEX" in statement for statement in statements)
    conn.commit.assert_called_once_with()


def test_real_is_active_mismatch_reports_filtered_index_dependency():
    conn, cursor = _connection(
        [[("IsActive", "bit", 1, 0, 0, True, False, False, False, "sys", None)],
         [("IX_Customers_City", "([IsActive]=(1))", False)]]
    )
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[Column(name="IsActive", source_type="bit", nullable=False)],
    )

    with pytest.raises(
        RuntimeError,
        match="dependent indexes.*filtered-index predicates.*IX_Customers_City",
    ):
        reconcile_table_columns(conn, schema)

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert not any("ALTER COLUMN" in statement or "DROP INDEX" in statement for statement in statements)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_equivalent_modified_at_computed_expression_is_already_reconciled():
    conn, cursor = _connection(
        [[_computed_modified_at_metadata("(dateadd(second, 1, OrderDate))")]]
    )

    result = reconcile_table_columns(
        conn, _orders_schema("DATEADD(SECOND, (1), [OrderDate])")
    )

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert result == {"dropped_columns": [], "altered_columns": []}
    assert not any("ALTER COLUMN" in statement for statement in statements)
    conn.commit.assert_called_once_with()
    conn.rollback.assert_not_called()


def test_different_modified_at_expression_fails_actionably_without_recreation():
    conn, cursor = _connection(
        [[_computed_modified_at_metadata("DATEADD(SECOND, 1, OrderDate)")]]
    )

    with pytest.raises(
        RuntimeError,
        match="source and target expressions differ.*automatic computed-column recreation is unsupported",
    ):
        reconcile_table_columns(conn, _orders_schema("DATEADD(SECOND, 2, OrderDate)"))

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert not any("ALTER COLUMN" in statement or "DROP CONSTRAINT" in statement for statement in statements)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_reconciliation_ignores_non_defining_metadata_representation_differences():
    conn, cursor = _connection(
        [[("FullName", "nvarchar", 200, 0, 0, False, False, False, False, "sys", None)]]
    )

    result = reconcile_table_columns(
        conn,
        Schema(
            name="Customers",
            schema_name="training",
            columns=[Column(name="FullName", source_type="nvarchar", size=100,
                            precision=100, nullable=False)],
        ),
    )

    assert result == {"dropped_columns": [], "altered_columns": []}
    assert not any("ALTER COLUMN" in str(call.args[0]) for call in cursor.execute.call_args_list)


def test_datetime2_default_scale_matches_catalog_scale_seven():
    conn, cursor = _connection(
        [[("CreatedAt", "datetime2", 8, 27, 7, False, False, False, False, "sys", None)]]
    )
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[Column(name="CreatedAt", source_type="datetime2", nullable=False)],
    )

    result = reconcile_table_columns(conn, schema)

    assert result == {"dropped_columns": [], "altered_columns": []}
    assert not any("ALTER COLUMN" in str(call.args[0]) for call in cursor.execute.call_args_list)


def test_mssql_reconciliation_adds_missing_nullable_source_column():
    conn, cursor = _connection(
        [[("CustomerID", "int", 4, 10, 0, False, False, False, False, "sys", None)]]
    )
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[
            Column(name="CustomerID", source_type="int", nullable=False),
            Column(name="Email", source_type="nvarchar", size=120, nullable=True),
        ],
    )

    result = reconcile_table_columns(conn, schema)

    assert result == {
        "dropped_columns": [],
        "altered_columns": [],
        "added_columns": ["Email"],
    }
    assert any(
        'ALTER TABLE "training"."Customers" ADD "Email" nvarchar(120) NULL' == str(call.args[0])
        for call in cursor.execute.call_args_list
    )
    conn.commit.assert_called_once_with()


def test_mssql_reconciliation_stages_missing_not_null_column_for_full_sync():
    conn, cursor = _connection(
        [[("CustomerID", "int", 4, 10, 0, False, False, False, False, "sys", None)]]
    )
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[
            Column(name="CustomerID", source_type="int", nullable=False),
            Column(name="Status", source_type="nvarchar", size=20, nullable=False),
        ],
    )

    result = reconcile_table_columns(conn, schema)

    assert result["added_columns"] == ["Status"]
    assert result["deferred_not_null_columns"] == ["Status"]
    assert any(
        'ALTER TABLE "training"."Customers" ADD "Status" nvarchar(20) NULL' == str(call.args[0])
        for call in cursor.execute.call_args_list
    )


def test_mssql_reconciliation_rejects_missing_identity_column_without_ddl():
    conn, cursor = _connection(
        [[("Email", "nvarchar", 240, 0, 0, True, False, False, False, "sys", None)]]
    )
    schema = Schema(
        name="Customers",
        schema_name="training",
        columns=[Column(name="CustomerID", source_type="int", is_identity=True)],
    )

    with pytest.raises(RuntimeError, match="missing IDENTITY column"):
        reconcile_table_columns(conn, schema)

    assert not any("ALTER TABLE" in str(call.args[0]) for call in cursor.execute.call_args_list)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_second_reconciliation_is_idempotent_after_first_alter():
    conn, cursor = _connection(
        [[("FullName", "varchar", 100, 0, 0, False, False, False, False, "sys", None)]]
        + _no_dependencies_and_constraints()
        + [[("FullName", "nvarchar", 200, 0, 0, False, False, False, False, "sys", None)]]
    )

    first = reconcile_table_columns(conn, _customer_schema())
    second = reconcile_table_columns(conn, _customer_schema())

    alters = [
        str(call.args[0]) for call in cursor.execute.call_args_list
        if "ALTER COLUMN" in str(call.args[0])
    ]
    assert first["altered_columns"] == ["FullName"]
    assert second == {"dropped_columns": [], "altered_columns": []}
    assert len(alters) == 1
    assert conn.commit.call_count == 2


@pytest.mark.parametrize(
    ("target_computed", "target_identity", "source_computed", "source_identity", "error"),
    [
        (False, True, False, True, "IDENTITY"),
    ],
)
def test_mssql_full_reconciliation_fails_explicitly_for_special_column_mismatch(
    target_computed, target_identity, source_computed, source_identity, error
):
    conn, cursor = _connection(
        [[(
            "FullName", "nvarchar", 300, 0, 0, False,
            target_computed, target_identity, False, "sys",
            "FullName" if target_computed else None,
        )]]
    )
    schema = _customer_schema()
    schema.columns[0].is_computed = source_computed
    schema.columns[0].is_identity = source_identity

    with pytest.raises(RuntimeError, match=error):
        reconcile_table_columns(conn, schema)

    assert not any("ALTER COLUMN" in str(call.args[0]) for call in cursor.execute.call_args_list)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


@pytest.mark.parametrize(
    ("target_type", "target_length", "target_nullable", "source_nullable", "source_type", "source_size"),
    [
        ("nvarchar", 300, False, False, "nvarchar", 100),  # length
        ("varchar", 100, False, False, "nvarchar", 100),  # compatible type change
        ("nvarchar", 200, True, False, "nvarchar", 100),  # nullability
    ],
)
def test_mssql_full_reconciliation_alters_changed_definition(
    target_type, target_length, target_nullable, source_nullable, source_type, source_size
):
    conn, cursor = _connection(
        [[(
            "FullName", target_type, target_length, 0, 0,
            target_nullable, False, False, False, "sys",
        )]]
        + _no_dependencies_and_constraints()
    )

    reconcile_table_columns(
        conn,
        _customer_schema(
            source_type=source_type, size=source_size, nullable=source_nullable
        ),
    )

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    alter = next(statement for statement in statements if "ALTER COLUMN" in statement)
    assert alter.endswith("nvarchar(100) NOT NULL")


def test_mssql_full_reconciliation_restores_check_constraint_after_alter():
    conn, cursor = _connection(
        [[("FullName", "nvarchar", 300, 0, 0, False, False, False, False, "sys")],
         [], [], [], [],
         [("CK_Customers_Name", "([FullName] <> N'')", False, False)],
         [],
         [("training", "vw_CustomerOrders")]]
    )

    reconcile_table_columns(conn, _customer_schema())

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    drop = 'ALTER TABLE "training"."Customers" DROP CONSTRAINT "CK_Customers_Name"'
    alter = next(i for i, statement in enumerate(statements) if "ALTER COLUMN" in statement)
    restore = next(i for i, statement in enumerate(statements) if "ADD CONSTRAINT" in statement)
    assert drop in statements
    assert statements.index(drop) < alter < restore
    assert "WITH CHECK ADD CONSTRAINT \"CK_Customers_Name\" CHECK ([FullName] <> N'')" in statements[restore]
    conn.commit.assert_called_once_with()


@pytest.mark.parametrize(
    ("dependency_result_index", "dependency_rows", "expected_message"),
    [
        (1, [("IX_Customers_FullName",)], "dependent indexes"),
        (2, [("FK_Customers_FullName",)], "dependent foreign keys"),
        (3, [("vw_schema_bound",)], "schema-bound views"),
        (4, [("ComputedFullName",)], "computed columns"),
    ],
)
def test_mssql_full_reconciliation_fails_safely_on_blocking_dependencies(
    dependency_result_index, dependency_rows, expected_message
):
    dependency_results = [[], [], [], []]
    dependency_results[dependency_result_index - 1] = dependency_rows
    conn, cursor = _connection(
        [[("FullName", "nvarchar", 300, 0, 0, False, False, False, False, "sys")]]
        + dependency_results
    )

    with pytest.raises(RuntimeError, match=expected_message):
        reconcile_table_columns(conn, _customer_schema())

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert not any("ALTER COLUMN" in statement for statement in statements)
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_mssql_full_reconciliation_rolls_back_failed_alter_and_propagates():
    conn, cursor = _connection(
        [[("FullName", "nvarchar", 300, 0, 0, False, False, False, False, "sys")]]
        + _no_dependencies_and_constraints()
    )
    cursor.execute.side_effect = [None] * 7 + [RuntimeError("data conversion failed")]

    with pytest.raises(RuntimeError, match="data conversion failed"):
        reconcile_table_columns(conn, _customer_schema())

    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()
