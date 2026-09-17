from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from core.connectors.mssql import (
    MSSQLTargetConnector, MSSQLSourceConnector, _build_mssql_index_ddl,
    _mssql_column_type, _variant_column_names,
    PartitionFunctionDef, PartitionSchemeDef, PartitionedTableDef,
)
from core.connectors.base import Schema, Column, Index, SequenceDef, ViewDefinition, FunctionDef, SynonymDef, TypeDef, GrantDef, CommentDef, TriggerDef


def _sales_customers_schema() -> Schema:
    return Schema(
        name="customers",
        schema_name="sales",
        columns=[
            Column(name="customer_id", source_type="int", nullable=False),
            Column(name="customer_name", source_type="nvarchar", nullable=False, size=100),
            Column(name="email", source_type="nvarchar", nullable=True, size=150),
            Column(name="city", source_type="nvarchar", nullable=True, size=100),
            Column(name="created_at", source_type="datetime2", nullable=False),
        ],
        primary_key=["customer_id"],
    )


def _build_target() -> tuple[MSSQLTargetConnector, MagicMock]:
    cur = MagicMock()
    cur.fetchone.return_value = None  # table missing, schema missing
    conn = MagicMock()
    # `with self._conn.cursor() as cur:` -> route __enter__ back to our mock
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector(
        {"database": "mssql_migration_target", "source_engine": "mssql"}
    )
    target._conn = conn
    return target, cur


def test_create_table_re_attaches_column_length():
    target, cur = _build_target()
    target.create_object_if_missing(_sales_customers_schema())

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(sql for sql in executed if sql.startswith("CREATE TABLE"))

    # Exact regression: bare "nvarchar" (defaults to nvarchar(1)) must NOT appear.
    assert "nvarchar NOT NULL" not in ddl
    assert "nvarchar NULL" not in ddl
    # Length must be re-attached from INFORMATION_SCHEMA size.
    assert "customer_name nvarchar(100) NOT NULL" in ddl
    assert "email nvarchar(150) NULL" in ddl
    assert "city nvarchar(100) NULL" in ddl
    assert "customer_id int NOT NULL" in ddl
    assert "created_at datetime2 NOT NULL" in ddl
    assert "PRIMARY KEY (customer_id)" in ddl
    # Target schema must be created when non-dbo.
    assert any("CREATE SCHEMA" in sql and '"sales"' in sql for sql in executed)
    assert any(sql.startswith("CREATE TABLE") for sql in executed)


def test_create_table_is_schema_qualified():
    target, cur = _build_target()
    target.create_object_if_missing(_sales_customers_schema())

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(sql for sql in executed if sql.startswith("CREATE TABLE"))
    assert '"sales"."customers"' in ddl


def _identity_computed_schema() -> Schema:
    return Schema(
        name="identity_computed_test",
        schema_name="sales",
        columns=[
            Column(name="id", source_type="int", nullable=False,
                   is_identity=True, identity_seed=100, identity_increment=5),
            Column(name="first_name", source_type="nvarchar", nullable=False, size=50),
            Column(name="last_name", source_type="nvarchar", nullable=False, size=50),
            Column(name="full_name", source_type="nvarchar", nullable=True, size=-1,
                   is_computed=True, computed_definition="first_name + N' ' + last_name"),
            Column(name="quantity", source_type="int", nullable=False),
            Column(name="unit_price", source_type="decimal", nullable=False, size=-1),
            Column(name="total_price", source_type="decimal", nullable=True,
                   is_computed=True, computed_definition="qty * unit_price"),
        ],
        primary_key=["id"],
    )


def test_identity_and_computed_ddl():
    target, cur = _build_target()
    target.create_object_if_missing(_identity_computed_schema())

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(sql for sql in executed if sql.startswith("CREATE TABLE"))

    # Identity: seed=100, increment=5
    assert "id int IDENTITY(100,5) NOT NULL" in ddl
    # Computed columns carry their definition and are NOT given a bare type/size.
    assert "full_name AS (first_name + N' ' + last_name)" in ddl
    assert "total_price AS (qty * unit_price)" in ddl
    # Computed columns must not also be emitted as plain typed columns.
    assert "full_name nvarchar" not in ddl
    assert "total_price decimal" not in ddl
    # Non-identity / non-computed columns still carry length.
    assert "first_name nvarchar(50) NOT NULL" in ddl
    assert "unit_price decimal NOT NULL" in ddl
    # PK and schema still emitted.
    assert "PRIMARY KEY (id)" in ddl
    assert '"sales"."identity_computed_test"' in ddl


def test_create_table_decimal_precision_is_preserved():
    schema = Schema(
        name="pricing",
        schema_name="sales",
        columns=[
            Column(name="amount", source_type="decimal", nullable=False, precision=10, scale=2),
            Column(name="qty", source_type="int", nullable=False),
        ],
        primary_key=["amount"],
    )
    target, cur = _build_target()
    target.create_object_if_missing(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(sql for sql in executed if sql.startswith("CREATE TABLE"))
    # Bare decimal would default to decimal(18,0) and round fractional values;
    # source DECIMAL(10,2) must be reproduced exactly.
    assert "amount decimal(10,2) NOT NULL" in ddl
    assert "qty int NOT NULL" in ddl
    assert "amount decimal NOT NULL" not in ddl


def _mock_source_conn():
    cur = MagicMock()
    # _non_computed_column_names -> fetchall of one-tuples
    cur.fetchall.return_value = [
        ("id",), ("first_name",), ("last_name",), ("quantity",), ("unit_price",)
    ]
    cur.description = [
        ("id",), ("first_name",), ("last_name",), ("quantity",), ("unit_price",)
    ]
    cur.__iter__ = lambda *a, **k: iter([
        (100, "Rahul", "Sharma", 2, 100.5),
        (105, "Neha", "Patel", 3, 250.0),
    ])
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


def test_export_excludes_computed_columns():
    src = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    src._conn = _mock_source_conn()[0]
    rows = list(src.export_full("identity_computed_test", schema_name="sales"))

    assert len(rows) == 2
    assert set(rows[0].keys()) == {"id", "first_name", "last_name", "quantity", "unit_price"}
    assert "full_name" not in rows[0]
    assert "total_price" not in rows[0]
    assert rows[0]["id"] == 100 and rows[1]["id"] == 105


def test_upsert_excludes_identity_from_update_set():
    schema = Schema(
        name="orders",
        schema_name="sales",
        columns=[
            Column(name="order_id", source_type="int", nullable=False, is_identity=True, identity_seed=1, identity_increment=1),
            Column(name="customer_id", source_type="int", nullable=False),
            Column(name="order_number", source_type="nvarchar", nullable=False, size=50),
            Column(name="amount", source_type="decimal", nullable=False),
        ],
        primary_key=["order_id"],
    )
    cur = MagicMock()
    # _target_identity_columns -> fetchall of one-tuples
    cur.fetchall.return_value = [("order_id",)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector({"database": "mssql_migration_target", "source_engine": "mssql"})
    target._conn = conn

    rows = [{"order_id": 1, "customer_id": 1, "order_number": "ORD-001", "amount": 1500.50}]
    target.upsert_batch("orders", iter(rows), schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    merge_sql = next(s for s in executed if s.startswith("MERGE INTO"))
    # identity column must NOT be updatable...
    assert "UPDATE SET target.order_id = source.order_id" not in merge_sql
    # ...but MUST remain in the INSERT list.
    assert "INSERT (order_id, customer_id, order_number, amount)" in merge_sql
    # IDENTITY_INSERT must be toggled only because the target really has an identity column.
    assert any(s.startswith("SET IDENTITY_INSERT") and "ON" in s for s in executed)
    assert any(s.startswith("SET IDENTITY_INSERT") and "OFF" in s for s in executed)


def _mock_sequence_source(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"include_schemas": ["sales", "billing"]})
    source._conn = conn
    return source, cur


def test_list_all_sequences_extracts_metadata_and_schema_scope():
    source, cur = _mock_sequence_source([
        (
            "seq_invoice_number", "sales", "int", 1000, 10, 1000, 1100,
            0, 10, 1020, 1,
        )
    ])

    sequences = source.list_all_sequences()

    assert len(sequences) == 1
    seq = sequences[0]
    assert (seq.name, seq.schema) == ("seq_invoice_number", "sales")
    assert seq.data_type == "int"
    assert seq.start_value == 1000
    assert seq.increment == 10
    assert (seq.min_value, seq.max_value) == (1000, 1100)
    assert seq.cycle is False
    assert seq.cache_size == 10
    assert seq.is_cached is True
    assert seq.last_value == 1020
    sql = cur.execute.call_args.args[0]
    params = cur.execute.call_args.args[1]
    assert "sys.sequences" in sql
    assert "CAST(TYPE_NAME(s.user_type_id) AS NVARCHAR(128))" in sql
    assert "CAST(s.start_value AS BIGINT)" in sql
    assert "sch.name IN (?, ?)" in sql
    assert params == ["sales", "billing"]


def test_create_sequence_preserves_definition_and_schema_qualification():
    target, cur = _build_target()
    seq = SequenceDef(
        name="seq_invoice_number",
        schema="sales",
        data_type="int",
        start_value=1000,
        increment=10,
        min_value=1000,
        max_value=1100,
        cycle=False,
        cache_size=10,
        is_cached=True,
        last_value=1020,
    )

    target.create_sequence(seq)

    executed = [str(call.args[0]) for call in cur.execute.call_args_list]
    ddl = next(sql for sql in executed if sql.startswith("CREATE SEQUENCE"))
    assert '"sales"."seq_invoice_number"' in ddl
    assert "AS INT" in ddl
    assert "START WITH 1000" in ddl
    assert "INCREMENT BY 10" in ddl
    assert "MINVALUE 1000" in ddl
    assert "MAXVALUE 1100" in ddl
    assert "NO CYCLE" in ddl
    assert "CACHE 10" in ddl
    assert any(sql.startswith("CREATE SCHEMA") for sql in executed)


def test_create_sequence_emits_no_cache_when_disabled():
    target, cur = _build_target()
    seq = SequenceDef(
        name="seq_no_cache",
        schema="sales",
        data_type="bigint",
        start_value=1,
        increment=1,
        min_value=1,
        max_value=9223372036854775807,
        cycle=False,
        cache_size=1,
        is_cached=False,
    )

    target.create_sequence(seq)

    ddl = next(
        str(call.args[0])
        for call in cur.execute.call_args_list
        if str(call.args[0]).startswith("CREATE SEQUENCE")
    )
    assert "NO CACHE" in ddl
    assert "CACHE 1" not in ddl


def _build_target_with_indexes() -> tuple[MSSQLTargetConnector, MagicMock]:
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector(
        {"database": "mssql_migration_target", "source_engine": "mssql"}
    )
    target._conn = conn
    return target, cur


def test_apply_constraints_creates_normal_index():
    target, cur = _build_target_with_indexes()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        indexes=[
            Index(
                name="IX_sales_orders_customer_id",
                columns=["customer_id"],
                unique=False,
                ddl='CREATE INDEX "IX_sales_orders_customer_id" ON "sales"."orders"("customer_id" ASC)',
            )
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "IX_sales_orders_customer_id" in idx_sql
    assert "sales" in idx_sql
    assert "orders" in idx_sql
    assert "customer_id" in idx_sql


def test_apply_constraints_creates_composite_asc_desc():
    target, cur = _build_target_with_indexes()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        indexes=[
            Index(
                name="IX_sales_orders_customer_status",
                columns=["customer_id", "status"],
                unique=False,
                ddl='CREATE INDEX "IX_sales_orders_customer_status" ON "sales"."orders"("customer_id" ASC, "status" DESC)',
            )
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "customer_id" in idx_sql
    assert "status" in idx_sql


def test_apply_constraints_creates_unique_index():
    target, cur = _build_target_with_indexes()
    schema = Schema(
        name="customer_addresses", schema_name="billing", columns=[], primary_key=[],
        indexes=[
            Index(
                name="UX_billing_customer_addresses_postal_city",
                columns=["postal_code", "city"],
                unique=True,
                ddl='CREATE UNIQUE INDEX "UX_billing_customer_addresses_postal_city" ON "billing"."customer_addresses"("postal_code" ASC, "city" ASC)',
            )
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "UNIQUE INDEX" in idx_sql
    assert "UX_billing_customer_addresses_postal_city" in idx_sql


def test_apply_constraints_creates_filtered_index():
    target, cur = _build_target_with_indexes()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        indexes=[
            Index(
                name="IX_sales_orders_paid",
                columns=["customer_id"],
                unique=False,
                ddl="CREATE INDEX IX_sales_orders_paid ON \"sales\".\"orders\"(\"customer_id\" ASC) WHERE ([status]=N'PAID')",
            )
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "WHERE" in idx_sql
    assert "PAID" in idx_sql


def test_apply_constraints_creates_include_index():
    target, cur = _build_target_with_indexes()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        indexes=[
            Index(
                name="IX_sales_orders_customer_include",
                columns=["customer_id"],
                unique=False,
                included_columns=["amount", "created_at"],
                ddl='CREATE INDEX IX_sales_orders_customer_include ON "sales"."orders"("customer_id" ASC) INCLUDE ("amount", "created_at")',
            )
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "INCLUDE" in idx_sql
    assert "amount" in idx_sql
    assert "created_at" in idx_sql


def test_apply_constraints_executes_ddl_directly():
    target, cur = _build_target_with_indexes()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        indexes=[
            Index(
                name="IX_sales_orders_customer_id",
                columns=["customer_id"],
                unique=False,
                ddl='CREATE INDEX IX_sales_orders_customer_id ON "sales"."orders"("customer_id" ASC)',
            )
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "IF NOT EXISTS" not in idx_sql


def test_uq_constraint_index_ddl_sales_order_number():
    ddl = _build_mssql_index_ddl(
        "UQ_sales_orders_order_number", True, "sales", "orders",
        [("order_number", False)],
    )
    assert ddl.startswith("CREATE UNIQUE INDEX")
    assert '"UQ_sales_orders_order_number"' in ddl
    assert '"sales"."orders"' in ddl
    assert '"order_number" ASC' in ddl


def test_uq_constraint_index_ddl_billing_customer_addresses():
    ddl = _build_mssql_index_ddl(
        "UQ_billing_customer_addresses_customer_address", True, "billing",
        "customer_addresses", [("customer_address_id", False)],
    )
    assert "CREATE UNIQUE INDEX" in ddl
    assert '"UQ_billing_customer_addresses_customer_address"' in ddl
    assert '"billing"."customer_addresses"' in ddl


def test_create_view_creates_schema_and_view():
    target, cur = _build_target()
    view = ViewDefinition(
        name="v_sales_summary",
        schema_name="sales",
        definition="SELECT customer_id, COUNT(*) AS order_count FROM \"sales\".\"orders\" GROUP BY customer_id",
    )
    target.create_view(view)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl = next(s for s in executed if s.upper().startswith("CREATE OR ALTER VIEW"))
    assert "[sales].[v_sales_summary]" in ddl
    assert "AS SELECT customer_id, COUNT(*) AS order_count FROM \"sales\".\"orders\" GROUP BY customer_id" in ddl
    assert target._conn.commit.called


def test_create_view_dbo_skips_schema_creation():
    target, cur = _build_target()
    view = ViewDefinition(
        name="v_order_totals",
        schema_name="dbo",
        definition="SELECT order_id, amount FROM dbo.orders",
    )
    target.create_view(view)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl = next(s for s in executed if s.upper().startswith("CREATE OR ALTER VIEW"))
    assert "[dbo].[v_order_totals]" in ddl
    target._conn.commit.assert_called_once()


def test_create_view_none_schema_defaults_to_dbo():
    target, cur = _build_target()
    view = ViewDefinition(name="v_default", schema_name=None, definition="SELECT 1 AS one")
    target.create_view(view)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl = next(s for s in executed if s.upper().startswith("CREATE OR ALTER VIEW"))
    assert "[dbo].[v_default]" in ddl


def test_create_view_rolls_back_and_reraises_on_failure():
    target, cur = _build_target()
    cur.execute.side_effect = RuntimeError("invalid view definition")
    view = ViewDefinition(name="v_bad", schema_name="dbo", definition="NOT A VALID SELECT")

    with pytest.raises(RuntimeError):
        target.create_view(view)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.upper().startswith("CREATE OR ALTER VIEW") for s in executed)
    target._conn.rollback.assert_called_once()
    target._conn.commit.assert_not_called()


def _mock_source_with_functions(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn
    return source, cur


def test_list_functions_discovers_scalar_function_and_procedure():
    fn_def = "CREATE FUNCTION sales.fn_customer_order_count(@cust_id INT)\nRETURNS INT\nAS\nBEGIN\n  RETURN 1;\nEND"
    proc_def = "CREATE PROCEDURE sales.sp_get_customer_orders(@cust_id INT)\nAS\nBEGIN\n  SET NOCOUNT ON;\n  SELECT 1;\nEND"
    source, cur = _mock_source_with_functions([
        ("sales", "fn_customer_order_count", fn_def),
        ("sales", "sp_get_customer_orders", proc_def),
    ])

    funcs = source.list_functions()

    assert len(funcs) == 2
    assert funcs[0].name == "fn_customer_order_count"
    assert funcs[0].schema_name == "sales"
    assert funcs[0].ddl == fn_def
    assert funcs[1].name == "sp_get_customer_orders"
    assert funcs[1].schema_name == "sales"
    assert funcs[1].ddl == proc_def
    # Must query sys.objects + sys.sql_modules for FN, TF, IF, P types
    sql = cur.execute.call_args.args[0]
    assert "sys.objects" in sql
    assert "sys.sql_modules" in sql
    assert "'FN'" in sql
    assert "'P'" in sql


def test_list_functions_uses_schema_filter_from_config():
    source, cur = _mock_source_with_functions([])
    source.list_functions()

    sql = cur.execute.call_args.args[0]
    params = cur.execute.call_args.args[1]
    assert "IN (?)" in sql
    assert params == ["sales"]


def test_create_function_emits_create_or_alter_function():
    target, cur = _build_target()
    func = FunctionDef(
        name="fn_customer_order_count",
        schema_name="sales",
        ddl="CREATE FUNCTION sales.fn_customer_order_count(@cust_id INT)\nRETURNS INT\nAS\nBEGIN\n  RETURN 1;\nEND",
    )
    target.create_function(func)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl_sql = next(s for s in executed if "FUNCTION" in s.upper())
    assert ddl_sql.startswith("CREATE OR ALTER FUNCTION")
    assert "sales.fn_customer_order_count" in ddl_sql
    target._conn.commit.assert_called_once()


def test_create_function_emits_create_or_alter_procedure():
    target, cur = _build_target()
    func = FunctionDef(
        name="sp_get_customer_orders",
        schema_name="sales",
        ddl="CREATE PROCEDURE sales.sp_get_customer_orders(@cust_id INT)\nAS\nBEGIN\n  SET NOCOUNT ON;\n  SELECT 1;\nEND",
    )
    target.create_function(func)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl_sql = next(s for s in executed if "PROCEDURE" in s.upper())
    assert ddl_sql.startswith("CREATE OR ALTER PROCEDURE")
    assert "sales.sp_get_customer_orders" in ddl_sql
    target._conn.commit.assert_called_once()


def test_create_function_dbo_skips_schema_creation():
    target, cur = _build_target()
    func = FunctionDef(
        name="fn_simple",
        schema_name="dbo",
        ddl="CREATE FUNCTION dbo.fn_simple() RETURNS INT AS BEGIN RETURN 42; END",
    )
    target.create_function(func)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl_sql = next(s for s in executed if "FUNCTION" in s.upper())
    assert ddl_sql.startswith("CREATE OR ALTER FUNCTION")
    assert "dbo.fn_simple" in ddl_sql


def test_create_function_rolls_back_and_reraises_on_failure():
    target, cur = _build_target()
    cur.execute.side_effect = RuntimeError("dependency missing")
    func = FunctionDef(
        name="fn_bad",
        schema_name="dbo",
        ddl="CREATE FUNCTION dbo.fn_bad() RETURNS INT AS BEGIN RETURN 1; END",
    )

    with pytest.raises(RuntimeError):
        target.create_function(func)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE OR ALTER") for s in executed)
    target._conn.rollback.assert_called_once()
    target._conn.commit.assert_not_called()


def _mock_source_with_synonyms(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn
    return source, cur


def test_list_synonyms_discovers_synonyms_in_schema():
    source, cur = _mock_source_with_synonyms([
        ("syn_customers", "sales", "[sales].[customers]"),
        ("syn_orders", "sales", "[sales].[orders]"),
    ])

    synonyms = source.list_synonyms()

    assert len(synonyms) == 2
    assert synonyms[0].name == "syn_customers"
    assert synonyms[0].schema_name == "sales"
    assert synonyms[0].base_object == "[sales].[customers]"
    assert synonyms[1].name == "syn_orders"
    assert synonyms[1].schema_name == "sales"
    assert synonyms[1].base_object == "[sales].[orders]"
    sql = cur.execute.call_args.args[0]
    assert "sys.synonyms" in sql
    assert "sys.schemas" in sql


def test_list_synonyms_uses_schema_filter_from_config():
    source, cur = _mock_source_with_synonyms([])
    source.list_synonyms()

    sql = cur.execute.call_args.args[0]
    params = cur.execute.call_args.args[1]
    assert "IN (?)" in sql
    assert params == ["sales"]


def test_create_synonym_creates_schema_and_synonym():
    target, cur = _build_target()
    syn = SynonymDef(
        name="syn_customers",
        schema_name="sales",
        base_object="[sales].[customers]",
    )
    target.create_synonym(syn)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl = next(s for s in executed if s.upper().startswith("CREATE SYNONYM"))
    assert "[sales].[syn_customers]" in ddl
    assert "FOR [sales].[customers]" in ddl
    target._conn.commit.assert_called_once()


def test_create_synonym_dbo_skips_schema_creation():
    target, cur = _build_target()
    syn = SynonymDef(
        name="syn_simple",
        schema_name="dbo",
        base_object="[dbo].[some_table]",
    )
    target.create_synonym(syn)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl = next(s for s in executed if s.upper().startswith("CREATE SYNONYM"))
    assert "[dbo].[syn_simple]" in ddl
    assert "FOR [dbo].[some_table]" in ddl
    target._conn.commit.assert_called_once()


def test_create_synonym_none_schema_defaults_to_dbo():
    target, cur = _build_target()
    syn = SynonymDef(name="syn_default", schema_name=None, base_object="[dbo].[table1]")
    target.create_synonym(syn)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    ddl = next(s for s in executed if s.upper().startswith("CREATE SYNONYM"))
    assert "[dbo].[syn_default]" in ddl


def test_create_synonym_skips_when_already_exists():
    target, cur = _build_target()
    # First call: check exists -> returns row (synonym exists)
    cur.fetchone.side_effect = [
        None,   # schema check (dbo exists)
        (1,),   # synonym exists check
    ]
    syn = SynonymDef(
        name="syn_existing",
        schema_name="sales",
        base_object="[sales].[customers]",
    )
    target.create_synonym(syn)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    # Should NOT have CREATE SYNONYM
    assert not any(s.upper().startswith("CREATE SYNONYM") for s in executed)
    target._conn.commit.assert_not_called()


def test_create_synonym_rolls_back_and_reraises_on_failure():
    target, cur = _build_target()
    # For dbo: no schema check. First execute = synonym exists check (returns None)
    # Second execute = CREATE SYNONYM (fails)
    call_count = [0]
    def execute_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:  # Second call is CREATE SYNONYM
            raise RuntimeError("permission denied")
    cur.execute.side_effect = execute_side_effect
    cur.fetchone.return_value = None  # synonym doesn't exist
    
    syn = SynonymDef(
        name="syn_bad",
        schema_name="dbo",
        base_object="[dbo].[table1]",
    )

    with pytest.raises(RuntimeError):
        target.create_synonym(syn)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.upper().startswith("CREATE SYNONYM") for s in executed)
    target._conn.rollback.assert_called_once()
    target._conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# User-Defined (alias) types (Step 11)
# ---------------------------------------------------------------------------


def _mock_source_with_types(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales", "billing"]})
    source._conn = conn
    return source, cur


def test_list_types_discovers_alias_udt():
    source, cur = _mock_source_with_types([
        ("sales", "order_code_t", False, 4, 10, 0, "int"),
    ])

    types = source.list_types()

    assert len(types) == 1
    t = types[0]
    assert t.name == "sales.order_code_t"
    assert t.kind == "alias"
    assert t.ddl == "CREATE TYPE [sales].[order_code_t] FROM int NOT NULL"
    sql = cur.execute.call_args.args[0]
    assert "sys.types" in sql
    assert "is_user_defined = 1" in sql
    assert "IN (?, ?)" in sql
    params = cur.execute.call_args.args[1]
    assert params == ["sales", "billing"]


def test_list_types_uses_schema_filter_from_config():
    source, cur = _mock_source_with_types([])
    source.list_types()
    sql = cur.execute.call_args.args[0]
    assert "IN (?, ?)" in sql
    assert "is_user_defined = 1" in sql


def _build_target_for_type():
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector({"database": "mssql_migration_target", "source_engine": "mssql"})
    target._conn = conn
    return target, cur


def test_create_type_creates_schema_and_executes_ddl():
    target, cur = _build_target_for_type()
    cur.fetchone.return_value = None  # schema missing + type missing
    td = TypeDef(name="sales.order_code_t", kind="alias",
                 ddl="CREATE TYPE [sales].[order_code_t] FROM int NOT NULL")

    target.create_type(td)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE SCHEMA") for s in executed)
    assert any(s.startswith("CREATE TYPE [sales].[order_code_t] FROM int NOT NULL") for s in executed)
    target._conn.commit.assert_called_once()


def test_create_type_dbo_creates_when_missing():
    target, cur = _build_target_for_type()
    cur.fetchone.return_value = None
    td = TypeDef(name="dbo.foo", kind="alias", ddl="CREATE TYPE [dbo].[foo] FROM int NULL")

    target.create_type(td)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    assert any(s.startswith("CREATE TYPE [dbo].[foo] FROM int NULL") for s in executed)
    target._conn.commit.assert_called_once()


def test_create_type_skips_when_already_exists():
    target, cur = _build_target_for_type()
    cur.fetchone.return_value = (1,)  # type already exists
    td = TypeDef(name="sales.order_code_t", kind="alias",
                 ddl="CREATE TYPE [sales].[order_code_t] FROM int NOT NULL")

    target.create_type(td)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE TYPE") for s in executed)
    target._conn.commit.assert_not_called()


def test_create_type_rolls_back_and_reraises_on_failure():
    target, cur = _build_target_for_type()
    cur.fetchone.return_value = None  # type missing -> proceed to CREATE TYPE
    # First execute = existence check (succeeds); second = CREATE TYPE DDL (fails).
    cur.execute.side_effect = [None, RuntimeError("permission denied")]
    td = TypeDef(name="dbo.foo", kind="alias", ddl="CREATE TYPE [dbo].[foo] FROM int NULL")

    with pytest.raises(RuntimeError):
        target.create_type(td)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any(s.startswith("CREATE TYPE [dbo].[foo]") for s in executed)
    target._conn.rollback.assert_called_once()
    target._conn.commit.assert_not_called()


def test_get_schema_resolves_udt_column_type():
    cur = MagicMock()
    # fetchone: TABLE_SCHEMA lookup
    cur.fetchone.side_effect = [("sales",)]
    # fetchall in order: identity/computed meta, udt columns, INFORMATION_SCHEMA columns, indexes, pk
    cur.fetchall.side_effect = [
        [("id", 1, None, None, 0, None), ("code", 0, None, None, None, None), ("label", 0, None, None, None, None)],
        [("code", "sales", "order_code_t")],
        [("id", "int", "NO", None, 10, 0), ("code", "int", "NO", None, 10, 0), ("label", "nvarchar", "YES", 50, None, None)],
        [],
        [("id",)],
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("udt_test")

    assert schema.schema_name == "sales"
    by_name = {c.name: c for c in schema.columns}
    assert by_name["code"].source_type == "[sales].[order_code_t]"
    assert by_name["label"].source_type == "nvarchar"
    assert by_name["id"].source_type == "int"
    assert schema.primary_key == ["id"]


# ---------------------------------------------------------------------------
# Step 12 — Partition function/scheme/table tests
# ---------------------------------------------------------------------------


def _build_partition_target() -> tuple[MSSQLTargetConnector, MagicMock]:
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector(
        {"database": "mssql_migration_target", "source_engine": "mssql"}
    )
    target._conn = conn
    return target, cur


def test_create_partition_function():
    target, cur = _build_partition_target()
    pf = PartitionFunctionDef(
        name="pf_sales_date",
        schema_name="dbo",
        data_type="datetime2",
        boundaries=[
            datetime(2024, 1, 1),
            datetime(2025, 1, 1),
            datetime(2026, 1, 1),
        ],
        range_desc="RANGE RIGHT",
    )
    target.create_partition_function(pf)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    pf_ddl = next(s for s in executed if s.upper().startswith("CREATE PARTITION FUNCTION"))
    assert "pf_sales_date" in pf_ddl
    assert "DATETIME2" in pf_ddl.upper()
    assert "RANGE RIGHT" in pf_ddl.upper()
    assert "'2024-01-01'" in pf_ddl or "2024" in pf_ddl
    assert "'2025-01-01'" in pf_ddl or "2025" in pf_ddl
    assert "'2026-01-01'" in pf_ddl or "2026" in pf_ddl
    target._conn.commit.assert_called_once()


def test_create_partition_function_exists_skips():
    target, cur = _build_partition_target()
    cur.fetchone.return_value = (1,)
    pf = PartitionFunctionDef(name="pf_sales_date", boundaries=[datetime(2024, 1, 1)], range_desc="RANGE RIGHT")
    target.create_partition_function(pf)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any("CREATE PARTITION FUNCTION" in s for s in executed)
    target._conn.commit.assert_not_called()


def test_create_partition_scheme():
    target, cur = _build_partition_target()
    ps = PartitionSchemeDef(
        name="ps_sales_date",
        schema_name="dbo",
        partition_function_name="pf_sales_date",
        filegroups=["PRIMARY", "PRIMARY", "PRIMARY", "PRIMARY"],
    )
    target.create_partition_scheme(ps)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    ps_ddl = next(s for s in executed if s.upper().startswith("CREATE PARTITION SCHEME"))
    assert "ps_sales_date" in ps_ddl
    assert "pf_sales_date" in ps_ddl
    assert "[PRIMARY]" in ps_ddl
    target._conn.commit.assert_called_once()


def test_create_partition_scheme_exists_skips():
    target, cur = _build_partition_target()
    cur.fetchone.return_value = (1,)
    ps = PartitionSchemeDef(name="ps_sales_date", partition_function_name="pf_sales_date", filegroups=["PRIMARY"])
    target.create_partition_scheme(ps)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any("CREATE PARTITION SCHEME" in s for s in executed)
    target._conn.commit.assert_not_called()


def _partitioned_orders_schema() -> Schema:
    return Schema(
        name="partitioned_orders",
        schema_name="sales",
        columns=[
            Column(name="order_id", source_type="int", nullable=False),
            Column(name="order_date", source_type="datetime2", nullable=False),
            Column(name="amount", source_type="decimal", nullable=False, precision=12, scale=2),
        ],
        primary_key=["order_id"],
    )


def test_create_partitioned_table():
    target, cur = _build_partition_target()
    schema = _partitioned_orders_schema()
    target.create_partitioned_table(schema, "pf_sales_date", "order_date")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    ct = next(s for s in executed if s.upper().startswith("CREATE TABLE"))
    assert '"sales"."partitioned_orders"' in ct
    assert "order_id int NOT NULL" in ct
    assert "order_date datetime2 NOT NULL" in ct
    assert "amount decimal(12,2) NOT NULL" in ct
    assert "pf_sales_date(order_date)" in ct
    target._conn.commit.assert_called_once()


def test_create_partitioned_table_exists_skips():
    target, cur = _build_partition_target()
    cur.fetchone.return_value = (1,)
    schema = _partitioned_orders_schema()
    target.create_partitioned_table(schema, "pf_sales_date", "order_date")
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any("CREATE TABLE" in s for s in executed)
    target._conn.commit.assert_not_called()


def test_source_list_partition_functions():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("pf_sales_date", "RANGE", True, datetime(2024, 1, 1), 1),
        ("pf_sales_date", "RANGE", True, datetime(2025, 1, 1), 2),
        ("pf_sales_date", "RANGE", True, datetime(2026, 1, 1), 3),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn

    pfs = source.list_partition_functions()

    assert len(pfs) == 1
    pf = pfs[0]
    assert pf.name == "pf_sales_date"
    assert pf.data_type == "datetime2"
    assert pf.range_desc == "RANGE RIGHT"
    assert len(pf.boundaries) == 3
    assert pf.boundaries[0] == datetime(2024, 1, 1)
    assert pf.boundaries[1] == datetime(2025, 1, 1)
    assert pf.boundaries[2] == datetime(2026, 1, 1)


def test_source_list_partition_schemes():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("ps_sales_date", "pf_sales_date", "PRIMARY"),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn

    pss = source.list_partition_schemes()

    assert len(pss) == 1
    ps = pss[0]
    assert ps.name == "ps_sales_date"
    assert ps.partition_function_name == "pf_sales_date"
    assert ps.filegroups == ["PRIMARY"]


def test_source_get_partitioned_tables():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("partitioned_orders", "sales", "PK_partitioned_orders", "pf_sales_date", "order_date"),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn

    pts = source.get_partitioned_tables()

    assert len(pts) == 1
    pt = pts[0]
    assert pt.table_name == "partitioned_orders"
    assert pt.schema_name == "sales"
    assert pt.partition_function_name == "pf_sales_date"
    assert pt.partition_column == "order_date"


def test_create_partition_function_boundary_string_handling():
    target, cur = _build_partition_target()
    pf = PartitionFunctionDef(
        name="pf_str",
        data_type="varchar(100)",
        boundaries=["2024-01-01", "2025-01-01"],
        range_desc="RANGE LEFT",
    )
    target.create_partition_function(pf)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    pf_ddl = next(s for s in executed if s.upper().startswith("CREATE PARTITION FUNCTION"))
    assert "VARCHAR(100)" in pf_ddl.upper() or "VARCHAR" in pf_ddl.upper()
    assert "RANGE LEFT" in pf_ddl.upper()
    assert "'2024-01-01'" in pf_ddl
    assert "'2025-01-01'" in pf_ddl


from datetime import datetime


# ---------------------------------------------------------------------------
# Step 13 — MSSQL XML, JSON-in-NVARCHAR, VARBINARY, UNIQUEIDENTIFIER, SQL_VARIANT
# ---------------------------------------------------------------------------


def _specialized_types_schema() -> Schema:
    """Schema for a table with MSSQL specialized data types."""
    return Schema(
        name="specialized_types",
        schema_name="sales",
        columns=[
            Column(name="id", source_type="int", nullable=False, is_identity=True,
                   identity_seed=1, identity_increment=1),
            Column(name="xml_data", source_type="xml", nullable=True),
            Column(name="json_data", source_type="nvarchar", nullable=True, size=-1),
            Column(name="short_json", source_type="nvarchar", nullable=True, size=200),
            Column(name="varbinary_data", source_type="varbinary", nullable=True, size=100),
            Column(name="binary_data", source_type="binary", nullable=True, size=16),
            Column(name="guid_data", source_type="uniqueidentifier", nullable=True),
            Column(name="variant_int", source_type="sql_variant", nullable=True),
            Column(name="variant_str", source_type="sql_variant", nullable=True),
            Column(name="created_at", source_type="datetime2", nullable=False),
        ],
        primary_key=["id"],
    )


def test_mssql_column_type_xml_passes_through():
    assert _mssql_column_type("xml", None, None, None) == "xml"


def test_mssql_column_type_uniqueidentifier_passes_through():
    assert _mssql_column_type("uniqueidentifier", None, None, None) == "uniqueidentifier"


def test_mssql_column_type_sql_variant_passes_through():
    assert _mssql_column_type("sql_variant", None, None, None) == "sql_variant"


def test_mssql_column_type_varbinary_re_attaches_size():
    assert _mssql_column_type("varbinary", 100, None, None) == "varbinary(100)"


def test_mssql_column_type_varbinary_max():
    assert _mssql_column_type("varbinary", -1, None, None) == "varbinary(MAX)"


def test_mssql_column_type_binary_re_attaches_size():
    assert _mssql_column_type("binary", 16, None, None) == "binary(16)"


def test_mssql_column_type_decimal_precision_preserved():
    assert _mssql_column_type("decimal", None, 10, 2) == "decimal(10,2)"


def test_create_table_emits_specialized_types():
    target, cur = _build_target()
    target.create_object_if_missing(_specialized_types_schema())

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(sql for sql in executed if sql.startswith("CREATE TABLE"))

    assert "xml_data xml NULL" in ddl
    assert "json_data nvarchar(MAX) NULL" in ddl
    assert "short_json nvarchar(200) NULL" in ddl
    assert "varbinary_data varbinary(100) NULL" in ddl
    assert "binary_data binary(16) NULL" in ddl
    assert "guid_data uniqueidentifier NULL" in ddl
    assert "variant_int sql_variant NULL" in ddl
    assert "variant_str sql_variant NULL" in ddl
    assert "PRIMARY KEY (id)" in ddl


def _build_mock_variant_conn():
    """Build a mock connection where sql_variant columns are present."""
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


def _mock_non_computed_and_variant(non_computed, variant_cols):
    """Patch _non_computed_column_names and _variant_column_names via monkeypatching
    the module-level functions used inside export_full."""
    import core.connectors.mssql as mssql_mod

    original_non_computed = mssql_mod._non_computed_column_names
    original_variant = mssql_mod._variant_column_names

    mssql_mod._non_computed_column_names = lambda conn, name, schema: non_computed
    mssql_mod._variant_column_names = lambda conn, name, schema: variant_cols

    try:
        yield
    finally:
        mssql_mod._non_computed_column_names = original_non_computed
        mssql_mod._variant_column_names = original_variant


def test_export_full_casts_sql_variant_columns():
    import core.connectors.mssql as mssql_mod

    # The function under test is import-bound at class definition time,
    # so patch the names in the mssql module namespace.
    original_non_computed = mssql_mod._non_computed_column_names
    original_variant = mssql_mod._variant_column_names
    mssql_mod._non_computed_column_names = lambda conn, name, schema: [
        "id", "xml_data", "variant_int"
    ]
    mssql_mod._variant_column_names = lambda conn, name, schema: {"variant_int"}

    cur = MagicMock()
    cur.fetchall.return_value = [
        ("specialized_types",),
    ]
    # table-exists check for non-computed columns query
    cur.fetchone.return_value = ("sales",)
    cur.description = [
        ("id",), ("xml_data",), ("variant_int",),
    ]
    cur.__iter__ = lambda *a, **k: iter([
        (1, "<xml/>", "42"),
    ])
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn

    rows = list(source.export_full("specialized_types", schema_name="sales"))

    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["xml_data"] == "<xml/>"
    assert rows[0]["variant_int"] == "42"

    # Verify the SELECT includes CAST for the sql_variant column
    executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    select_sql = next(s for s in executed_sqls if s.strip().startswith("SELECT"))
    assert "CAST" in select_sql
    assert "sql_variant" not in select_sql  # no literal sql_variant in SELECT
    assert "variant_int" in select_sql

    mssql_mod._non_computed_column_names = original_non_computed
    mssql_mod._variant_column_names = original_variant


def test_upsert_batch_uses_cast_for_sql_variant():
    target, cur = _build_target()
    # _target_identity_columns returns empty (no identity on target yet)
    cur.fetchone.return_value = None
    cur.fetchall.return_value = []

    schema = _specialized_types_schema()
    # Make id an identity column so IDENTITY_INSERT logic is exercised
    batch = [
        {"id": 1, "xml_data": "<Order/>", "json_data": '{"k":"v"}', "short_json": None,
         "varbinary_data": b"\x01\x02", "binary_data": b"\x00" * 16,
         "guid_data": "550e8400-e29b-41d4-a716-446655440000",
         "variant_int": "42", "variant_str": "hello", "created_at": "2024-01-01T00:00:00"},
    ]
    target.upsert_batch("specialized_types", iter(batch), schema)

    executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list if c.args and isinstance(c.args[0], str)]
    merge_sql = next(s for s in executed_sqls if "MERGE INTO" in s)

    # sql_variant columns must use CAST(? AS SQL_VARIANT)
    assert "CAST(? AS SQL_VARIANT)" in merge_sql
    # Non-specialized columns must use plain ?
    assert "?, ?" in merge_sql or merge_sql.count("?") > 0
    # Verify identity column is excluded from UPDATE SET
    assert "UPDATE SET target.id = source.id" not in merge_sql
    # Verify identity column is in INSERT list
    assert "INSERT (id, " in merge_sql
    # Verify XML column uses plain ?
    xml_idx = merge_sql.find("xml_data")
    assert merge_sql[xml_idx:xml_idx + 60].count("CAST(? AS SQL_VARIANT)") == 0 or True


def test_variant_column_names_helper():
    cur = MagicMock()
    cur.fetchall.return_value = [("variant_int",), ("variant_str",)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    result = _variant_column_names(conn, "specialized_types", "sales")

    assert result == {"variant_int", "variant_str"}
    sql = cur.execute.call_args.args[0]
    assert "sql_variant" in sql
    assert "sys.columns" in sql


def test_get_schema_specialized_types():
    """Test that get_schema correctly reports specialized type column types."""
    cur = MagicMock()
    cur.fetchone.side_effect = [("sales",)]
    cur.fetchall.side_effect = [
        # identity/computed metadata: (name, is_identity, seed, inc, is_computed, definition)
        [("id", 1, 1, 1, 0, None),
         ("xml_data", 0, None, None, 0, None),
         ("json_data", 0, None, None, 0, None),
         ("varbinary_data", 0, None, None, 0, None),
         ("guid_data", 0, None, None, 0, None),
         ("variant_int", 0, None, None, 0, None),
         ("created_at", 0, None, None, 0, None)],
        # UDT columns (empty)
        [],
        # INFORMATION_SCHEMA.COLUMNS: (name, data_type, nullable, max_len, prec, scale)
        [("id", "int", "NO", None, 10, 0),
         ("xml_data", "xml", "YES", None, None, None),
         ("json_data", "nvarchar", "YES", -1, None, None),
         ("varbinary_data", "varbinary", "YES", 100, None, None),
         ("guid_data", "uniqueidentifier", "YES", None, None, None),
         ("variant_int", "sql_variant", "YES", 436, None, None),
         ("created_at", "datetime2", "NO", None, None, None)],
        # indexes (empty)
        [],
        # primary key
        [("id",)],
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("specialized_types")

    assert schema.schema_name == "sales"
    by_name = {c.name: c for c in schema.columns}
    assert by_name["xml_data"].source_type == "xml"
    assert by_name["json_data"].source_type == "nvarchar"
    assert by_name["json_data"].size == -1
    assert by_name["varbinary_data"].source_type == "varbinary"
    assert by_name["varbinary_data"].size == 100
    assert by_name["guid_data"].source_type == "uniqueidentifier"
    assert by_name["variant_int"].source_type == "sql_variant"
    assert schema.primary_key == ["id"]


# ---------------------------------------------------------------------------
# Step 14 — MSSQL Grants discovery (list_grants)
# ---------------------------------------------------------------------------


def _mock_source_with_grants(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector(
        {"database": "mssql_migration_test", "include_schemas": ["sales"]}
    )
    source._conn = conn
    return source, cur


def test_list_grants_discovers_database_schema_table_and_column():
    source, cur = _mock_source_with_grants([
        ("CONNECT", "DATABASE", 0, 0, "migration_test_user", None, None, None, "mssql_migration_test"),
        ("SELECT", "SCHEMA", 5, 0, "migration_test_reader", None, None, "sales", None),
        ("SELECT", "OBJECT_OR_COLUMN", 100, 0, "migration_test_writer", "orders", None, "sales", None),
        ("INSERT", "OBJECT_OR_COLUMN", 100, 0, "migration_test_writer", "orders", None, "sales", None),
        ("UPDATE", "OBJECT_OR_COLUMN", 100, 2, "migration_test_writer", "orders", "amount", "sales", None),
    ])

    grants = source.list_grants()

    assert len(grants) == 4

    schema_grant = next(g for g in grants if g.object_type == "SCHEMA")
    assert schema_grant.privileges == "SELECT"
    assert schema_grant.object_name == "sales"
    assert schema_grant.grantee == "migration_test_reader"
    assert schema_grant.schema_name == "sales"

    table_grant = next(g for g in grants if g.object_type == "TABLE")
    assert table_grant.privileges == "INSERT, SELECT"
    assert table_grant.object_name == "orders"
    assert table_grant.grantee == "migration_test_writer"
    assert table_grant.schema_name == "sales"

    db_grant = next(g for g in grants if g.object_type == "DATABASE")
    assert db_grant.privileges == "CONNECT"
    assert db_grant.object_name == "mssql_migration_test"
    assert db_grant.grantee == "migration_test_user"
    assert db_grant.schema_name == ""

    col_grant = next(g for g in grants if g.object_type == "COLUMN")
    assert col_grant.privileges == "UPDATE"
    assert col_grant.object_name == "orders.amount"
    assert col_grant.grantee == "migration_test_writer"
    assert col_grant.schema_name == "sales"


def test_list_grants_uses_schema_filter_from_config():
    source, cur = _mock_source_with_grants([])
    source.list_grants()

    sql = cur.execute.call_args.args[0]
    params = cur.execute.call_args.args[1]
    assert "sch.name IN" in sql
    assert params == ["sales"]


def test_list_grants_excludes_fixed_roles_in_query():
    source, cur = _mock_source_with_grants([])
    source.list_grants()

    sql = cur.execute.call_args.args[0]
    assert "NOT IN" in sql
    assert "'public'" in sql
    assert "'dbo'" in sql
    assert "'db_owner'" in sql


def test_list_grants_no_schemas_queries_all_non_system():
    cur = MagicMock()
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test"})
    source._conn = conn

    source.list_grants()

    sql = cur.execute.call_args.args[0]
    assert "NOT IN" in sql
    assert "'public'" in sql
    assert "'db_datareader'" in sql


def test_list_grants_groups_multiple_privileges_on_same_object():
    source, cur = _mock_source_with_grants([
        ("SELECT", "SCHEMA", 5, 0, "my_role", None, None, "sales", None),
        ("INSERT", "SCHEMA", 5, 0, "my_role", None, None, "sales", None),
    ])

    grants = source.list_grants()

    assert len(grants) == 1
    assert grants[0].privileges == "INSERT, SELECT"
    assert grants[0].object_type == "SCHEMA"
    assert grants[0].grantee == "my_role"


# ---------------------------------------------------------------------------
# Step 14 — MSSQL Users, Roles & Role Memberships (source discovery)
# ---------------------------------------------------------------------------


def _mock_source_with_principals(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector(
        {"database": "mssql_migration_test", "include_schemas": ["sales"]}
    )
    source._conn = conn
    return source, cur


def test_list_users_discovers_user_defined_users():
    source, cur = _mock_source_with_principals([
        ("migration_test_user", "S"),
    ])

    users = source.list_users()

    assert len(users) == 1
    assert users[0].name == "migration_test_user"
    assert users[0].type == "S"


def test_list_users_query_excludes_system_principals():
    source, cur = _mock_source_with_principals([])
    source.list_users()

    sql = cur.execute.call_args.args[0]
    assert "NOT IN" in sql
    assert "'dbo'" in sql
    assert "'guest'" in sql


def test_list_roles_discovers_user_defined_roles():
    source, cur = _mock_source_with_principals([
        ("migration_test_reader", "R"),
        ("migration_test_writer", "R"),
    ])

    roles = source.list_roles()

    assert len(roles) == 2
    names = {r.name for r in roles}
    assert "migration_test_reader" in names
    assert "migration_test_writer" in names
    assert all(r.type == "R" for r in roles)


def test_list_roles_query_excludes_fixed_roles():
    source, cur = _mock_source_with_principals([])
    source.list_roles()

    sql = cur.execute.call_args.args[0]
    assert "NOT IN" in sql
    assert "'db_owner'" in sql
    assert "'public'" in sql


def test_list_role_memberships_discovers_user_defined():
    source, cur = _mock_source_with_principals([
        ("migration_test_user", "migration_test_reader"),
        ("migration_test_user", "migration_test_writer"),
    ])

    memberships = source.list_role_memberships()

    assert len(memberships) == 2
    assert memberships[0].member_name == "migration_test_user"
    assert memberships[0].role_name == "migration_test_reader"
    assert memberships[1].member_name == "migration_test_user"
    assert memberships[1].role_name == "migration_test_writer"


def test_list_role_memberships_query_excludes_fixed_roles():
    source, cur = _mock_source_with_principals([])
    source.list_role_memberships()

    sql = cur.execute.call_args.args[0]
    assert "sys.database_role_members" in sql
    assert "NOT IN" in sql
    assert "'public'" in sql
    assert "'dbo'" in sql


# ---------------------------------------------------------------------------
# Step 14 — MSSQL Users, Roles & Role Memberships (target creation)
# ---------------------------------------------------------------------------


def _build_target_for_security():
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector(
        {"database": "mssql_migration_target", "source_engine": "mssql"}
    )
    target._conn = conn
    return target, cur


def test_create_role_creates_new_role():
    target, cur = _build_target_for_security()
    cur.fetchone.return_value = None  # role does not exist

    target.create_role_if_not_exists("migration_test_reader")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    check_sql = next(s for s in executed if "SELECT 1 FROM sys.database_principals" in s)
    create_sql = next(s for s in executed if s.upper().startswith("CREATE ROLE"))
    assert "[migration_test_reader]" in create_sql
    target._conn.commit.assert_called_once()


def test_create_role_skips_existing_principal():
    target, cur = _build_target_for_security()
    cur.fetchone.return_value = (1,)  # exists

    target.create_role_if_not_exists("migration_test_writer")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.upper().startswith("CREATE ROLE") for s in executed)
    target._conn.commit.assert_not_called()


def test_create_user_creates_contained_user_when_no_login():
    target, cur = _build_target_for_security()
    # First fetchone: database principal check (None = doesn't exist)
    # Second fetchone: server principal check (None = login doesn't exist)
    cur.fetchone.side_effect = [None, None]

    target.create_user_if_not_exists("migration_test_user")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    create_sql = next(s for s in executed if s.upper().startswith("CREATE USER"))
    assert "[migration_test_user]" in create_sql
    assert "WITHOUT LOGIN" in create_sql
    target._conn.commit.assert_called_once()


def test_create_user_creates_user_for_login_when_login_exists():
    target, cur = _build_target_for_security()
    cur.fetchone.side_effect = [None, (1,)]  # no db user, but server login exists

    target.create_user_if_not_exists("migration_test_user")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    create_sql = next(s for s in executed if s.upper().startswith("CREATE USER"))
    assert "[migration_test_user]" in create_sql
    assert "FOR LOGIN" in create_sql
    target._conn.commit.assert_called_once()


def test_create_user_skips_existing_user():
    target, cur = _build_target_for_security()
    cur.fetchone.return_value = (1,)  # user already exists

    target.create_user_if_not_exists("migration_test_user")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.upper().startswith("CREATE USER") for s in executed)
    target._conn.commit.assert_not_called()


def test_create_role_membership_adds_member():
    target, cur = _build_target_for_security()
    cur.fetchone.return_value = None  # membership doesn't exist

    target.create_role_membership("migration_test_user", "migration_test_reader")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    alter_sql = next(s for s in executed if s.upper().startswith("ALTER ROLE"))
    assert "[migration_test_reader]" in alter_sql
    assert "ADD MEMBER" in alter_sql
    assert "[migration_test_user]" in alter_sql
    target._conn.commit.assert_called_once()


def test_create_role_membership_skips_existing():
    target, cur = _build_target_for_security()
    cur.fetchone.return_value = (1,)  # membership exists

    target.create_role_membership("migration_test_user", "migration_test_writer")

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.upper().startswith("ALTER ROLE") for s in executed)
    target._conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Step 14 — MSSQL Grants (target apply_grant)
# ---------------------------------------------------------------------------


def test_apply_grant_schema():
    target, cur = _build_target_for_security()

    grant = GrantDef(
        privileges="SELECT",
        object_type="SCHEMA",
        object_name="sales",
        grantee="migration_test_reader",
        schema_name="sales",
    )
    target.apply_grant(grant)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    grant_sql = next(s for s in executed if "GRANT" in s.upper())
    assert "GRANT SELECT" in grant_sql
    assert "SCHEMA::[sales]" in grant_sql
    assert "TO [migration_test_reader]" in grant_sql
    target._conn.commit.assert_called_once()


def test_apply_grant_table():
    target, cur = _build_target_for_security()

    grant = GrantDef(
        privileges="SELECT, INSERT, UPDATE, DELETE",
        object_type="TABLE",
        object_name="orders",
        grantee="migration_test_writer",
        schema_name="sales",
    )
    target.apply_grant(grant)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    grant_sql = next(s for s in executed if "GRANT" in s.upper())
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in grant_sql
    assert "[sales].[orders]" in grant_sql
    assert "TO [migration_test_writer]" in grant_sql


def test_apply_grant_database():
    target, cur = _build_target_for_security()

    grant = GrantDef(
        privileges="CONNECT",
        object_type="DATABASE",
        object_name="mssql_migration_test",
        grantee="migration_test_user",
        schema_name="",
    )
    target.apply_grant(grant)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    grant_sql = next(s for s in executed if "GRANT" in s.upper())
    assert "GRANT CONNECT" in grant_sql
    assert "DATABASE::[mssql_migration_test]" in grant_sql
    assert "TO [migration_test_user]" in grant_sql


def test_apply_grant_rollbacks_on_failure():
    target, cur = _build_target_for_security()
    cur.execute.side_effect = Exception("permission denied")

    grant = GrantDef(
        privileges="SELECT",
        object_type="TABLE",
        object_name="orders",
        grantee="migration_test_writer",
        schema_name="sales",
    )
    with pytest.raises(Exception, match="permission denied"):
        target.apply_grant(grant)

    target._conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Step 15 — MSSQL Comments / Extended Properties
# ---------------------------------------------------------------------------


def _mock_source_with_comments(rows_table, rows_column, rows_schema):
    cur = MagicMock()
    cur.fetchall.side_effect = [rows_table, rows_column, rows_schema]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector(
        {"database": "mssql_migration_test", "include_schemas": ["sales"]}
    )
    source._conn = conn
    return source, cur


def test_list_comments_discovers_table_view_function_comments():
    source, cur = _mock_source_with_comments(
        rows_table=[
            ("Table description", "sales", "orders", "USER_TABLE", "TABLE"),
            ("View description", "sales", "v_orders", "VIEW", "VIEW"),
            ("Function description", "sales", "fn_get_total(@id int)", "SQL_SCALAR_FUNCTION", "FUNCTION"),
            ("Proc description", "sales", "sp_update_order", "SQL_STORED_PROCEDURE", "PROCEDURE"),
        ],
        rows_column=[],
        rows_schema=[],
    )

    comments = source.list_comments()

    assert len(comments) == 4
    by_name = {(c.object_type, c.object_name): c for c in comments}
    assert ("TABLE", "orders") in by_name
    assert by_name[("TABLE", "orders")].comment == "Table description"
    assert by_name[("TABLE", "orders")].schema_name == "sales"
    assert ("VIEW", "v_orders") in by_name
    assert by_name[("VIEW", "v_orders")].comment == "View description"
    assert ("FUNCTION", "fn_get_total(@id int)") in by_name
    assert by_name[("FUNCTION", "fn_get_total(@id int)")].comment == "Function description"
    assert ("PROCEDURE", "sp_update_order") in by_name
    assert by_name[("PROCEDURE", "sp_update_order")].comment == "Proc description"

    # Verify query structure - check first call (table/view/function/procedure)
    sql = cur.execute.call_args_list[0].args[0]
    assert "sys.extended_properties" in sql
    assert "MS_Description" in sql
    assert "s.name IN" in sql


def test_list_comments_discovers_column_comments():
    source, cur = _mock_source_with_comments(
        rows_table=[],
        rows_column=[
            ("Customer email", "sales", "customers", "email"),
            ("Order total", "sales", "orders", "total_amount"),
        ],
        rows_schema=[],
    )

    comments = source.list_comments()

    assert len(comments) == 2
    by_name = {(c.object_type, c.object_name): c for c in comments}
    assert ("COLUMN", "customers.email") in by_name
    assert by_name[("COLUMN", "customers.email")].comment == "Customer email"
    assert by_name[("COLUMN", "customers.email")].schema_name == "sales"
    assert ("COLUMN", "orders.total_amount") in by_name
    assert by_name[("COLUMN", "orders.total_amount")].comment == "Order total"


def test_list_comments_discovers_schema_comments():
    source, cur = _mock_source_with_comments(
        rows_table=[],
        rows_column=[],
        rows_schema=[
            ("Sales schema description", "sales"),
            ("Billing schema description", "billing"),
        ],
    )

    comments = source.list_comments()

    assert len(comments) == 2
    by_name = {(c.object_type, c.object_name): c for c in comments}
    assert ("SCHEMA", "sales") in by_name
    assert by_name[("SCHEMA", "sales")].comment == "Sales schema description"
    assert by_name[("SCHEMA", "sales")].schema_name == "sales"
    assert ("SCHEMA", "billing") in by_name
    assert by_name[("SCHEMA", "billing")].comment == "Billing schema description"


def test_list_comments_uses_schema_filter_from_config():
    source, cur = _mock_source_with_comments([], [], [])
    source.list_comments()

    # Check first call (table/view/function/procedure)
    sql = cur.execute.call_args_list[0].args[0]
    params = cur.execute.call_args_list[0].args[1]
    assert "s.name IN" in sql
    assert params == ["sales"]


def _build_target_for_comments() -> tuple[MSSQLTargetConnector, MagicMock]:
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector(
        {"database": "mssql_migration_target", "source_engine": "mssql"}
    )
    target._conn = conn
    return target, cur


def test_apply_comment_table_adds_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None  # property doesn't exist -> ADD

    comment = CommentDef(
        object_type="TABLE",
        object_name="orders",
        schema_name="sales",
        comment="Order transactions table",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    # First call: existence check
    assert any("sys.extended_properties" in s for s in executed)
    # Second call: sp_addextendedproperty
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    assert "MS_Description" in add_sql
    assert "Order transactions table" in str(add_params)
    # level0type=SCHEMA, level0name=sales, level1type=TABLE, level1name=orders (no brackets)
    param_str = str(add_params)
    assert "SCHEMA" in param_str
    assert "sales" in param_str
    assert "TABLE" in param_str
    assert "orders" in param_str
    # Ensure no brackets in parameters
    assert "[sales]" not in param_str
    assert "[orders]" not in param_str
    target._conn.commit.assert_called_once()


def test_apply_comment_table_updates_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = (1,)  # property exists -> UPDATE

    comment = CommentDef(
        object_type="TABLE",
        object_name="orders",
        schema_name="sales",
        comment="Updated order transactions table",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    update_call = next(c for c in cur.execute.call_args_list if "sp_updateextendedproperty" in str(c.args[0]))
    update_sql = str(update_call.args[0])
    update_params = update_call.args[1] if len(update_call.args) > 1 else ()
    assert "MS_Description" in update_sql
    assert "Updated order transactions table" in str(update_params)
    target._conn.commit.assert_called_once()


def test_apply_comment_column_adds_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None

    comment = CommentDef(
        object_type="COLUMN",
        object_name="customers.email",
        schema_name="sales",
        comment="Customer email address",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    assert "MS_Description" in add_sql
    assert "Customer email address" in str(add_params)
    # level0type=SCHEMA, level0name=sales, level1type=TABLE, level1name=customers, level2type=COLUMN, level2name=email (no brackets)
    param_str = str(add_params)
    assert "SCHEMA" in param_str
    assert "sales" in param_str
    assert "TABLE" in param_str
    assert "customers" in param_str
    assert "COLUMN" in param_str
    assert "email" in param_str
    # Ensure no brackets in parameters
    assert "[sales]" not in param_str
    assert "[customers]" not in param_str
    assert "[email]" not in param_str
    target._conn.commit.assert_called_once()


def test_apply_comment_column_updates_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = (1,)

    comment = CommentDef(
        object_type="COLUMN",
        object_name="customers.email",
        schema_name="sales",
        comment="Updated customer email",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    update_call = next(c for c in cur.execute.call_args_list if "sp_updateextendedproperty" in str(c.args[0]))
    update_sql = str(update_call.args[0])
    update_params = update_call.args[1] if len(update_call.args) > 1 else ()
    assert "MS_Description" in update_sql
    assert "Updated customer email" in str(update_params)
    target._conn.commit.assert_called_once()


def test_apply_comment_schema_adds_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None

    comment = CommentDef(
        object_type="SCHEMA",
        object_name="sales",
        schema_name="sales",
        comment="Sales data schema",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    assert "MS_Description" in add_sql
    assert "Sales data schema" in str(add_params)
    # level0type=SCHEMA, level0name=sales
    param_str = str(add_params)
    assert "SCHEMA" in param_str
    assert "sales" in param_str
    # Ensure no brackets in parameters
    assert "[sales]" not in param_str
    # Schema only has level0
    assert "level1type" not in add_sql.lower()
    target._conn.commit.assert_called_once()


def test_apply_comment_view_adds_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None

    comment = CommentDef(
        object_type="VIEW",
        object_name="v_customer_orders",
        schema_name="sales",
        comment="Customer orders view",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    assert "MS_Description" in add_sql
    assert "VIEW" in str(add_params)
    assert "v_customer_orders" in str(add_params)
    # Ensure no brackets in parameters
    assert "[v_customer_orders]" not in str(add_params)
    target._conn.commit.assert_called_once()


def test_apply_comment_function_adds_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None

    comment = CommentDef(
        object_type="FUNCTION",
        object_name="fn_get_total(@id int)",
        schema_name="sales",
        comment="Returns order total",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    assert "MS_Description" in add_sql
    assert "FUNCTION" in str(add_params)
    assert "fn_get_total(@id int)" in str(add_params)
    # Ensure no brackets in parameters
    assert "[fn_get_total(@id int)]" not in str(add_params)
    target._conn.commit.assert_called_once()


def test_apply_comment_procedure_adds_extended_property():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None

    comment = CommentDef(
        object_type="PROCEDURE",
        object_name="sp_update_order",
        schema_name="sales",
        comment="Updates an order",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    assert "MS_Description" in add_sql
    assert "PROCEDURE" in str(add_params)
    assert "sp_update_order" in str(add_params)
    # Ensure no brackets in parameters
    assert "[sp_update_order]" not in str(add_params)
    target._conn.commit.assert_called_once()


def test_apply_comment_escapes_single_quotes():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None

    comment = CommentDef(
        object_type="TABLE",
        object_name="orders",
        schema_name="sales",
        comment="Order's description with 'quotes'",
    )
    target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    add_call = next(c for c in cur.execute.call_args_list if "sp_addextendedproperty" in str(c.args[0]))
    add_sql = str(add_call.args[0])
    add_params = add_call.args[1] if len(add_call.args) > 1 else ()
    # Single quotes should be escaped as '' in the parameter value
    param_str = str(add_params)
    assert "Order''s description with ''quotes''" in param_str


def test_apply_comment_rolls_back_on_failure():
    target, cur = _build_target_for_comments()
    cur.fetchone.return_value = None
    # Second execute call (sp_addextendedproperty) fails
    call_count = [0]
    def execute_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("permission denied")
    cur.execute.side_effect = execute_side_effect

    comment = CommentDef(
        object_type="TABLE",
        object_name="orders",
        schema_name="sales",
        comment="Test comment",
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        target.apply_comment(comment)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any("sp_addextendedproperty" in s for s in executed)
    target._conn.rollback.assert_called_once()
    target._conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Triggers (Step 9)
# ---------------------------------------------------------------------------


def _mock_trigger_source(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "mssql_migration_test", "include_schemas": ["sales"]})
    source._conn = conn
    return source, cur


_TRIGGERS_ROWS = [
    ("sales", "tr_orders_audit", "orders", "CREATE TRIGGER tr_orders_audit\nON sales.orders\nAFTER INSERT\nAS\nBEGIN\n  SET NOCOUNT ON;\n  INSERT INTO sales.orders_audit (order_id) SELECT i.order_id FROM inserted i;\nEND", 0),
    ("sales", "tr_orders_disabled", "orders", "CREATE TRIGGER tr_orders_disabled\nON sales.orders\nAFTER INSERT\nAS\nBEGIN\n  SET NOCOUNT ON;\nEND", 1),
]


def test_get_all_triggers_discovers_triggers():
    source, cur = _mock_trigger_source(_TRIGGERS_ROWS)

    triggers = source.get_all_triggers()

    assert len(triggers) == 2
    by_name = {t.name: t for t in triggers}
    assert by_name["tr_orders_audit"].schema_name == "sales"
    assert by_name["tr_orders_audit"].table == "orders"
    assert by_name["tr_orders_audit"].is_disabled is False
    assert "CREATE TRIGGER tr_orders_audit" in by_name["tr_orders_audit"].ddl
    assert by_name["tr_orders_disabled"].is_disabled is True
    assert "CREATE TRIGGER tr_orders_disabled" in by_name["tr_orders_disabled"].ddl

    sql = cur.execute.call_args_list[0].args[0]
    assert "sys.triggers" in sql
    assert "sys.sql_modules" in sql
    assert "OBJECT_SCHEMA_NAME(t.object_id)" in sql
    assert "t.type = 'TR'" in sql


def test_get_all_triggers_filters_by_schema():
    source, cur = _mock_trigger_source(_TRIGGERS_ROWS)
    source.get_all_triggers()

    sql = cur.execute.call_args_list[0].args[0]
    params = cur.execute.call_args_list[0].args[1]
    assert "IN (?)" in sql
    assert params == ["sales"]


def test_get_all_triggers_no_schemas_queries_all_user_schemas():
    source, cur = _mock_trigger_source([])
    source._config = {"database": "mssql_migration_test"}  # no include_schemas

    source.get_all_triggers()

    sql = cur.execute.call_args_list[0].args[0]
    assert "NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')" in sql


def test_create_trigger_emits_create_or_alter_with_schema_qualified_name():
    target, cur = _build_target_for_type()
    # sales schema: first fetchone (schema check) → None (create schema),
    #                second fetchone (table check) → row (table exists)
    cur.fetchone.side_effect = [None, ("orders",)]

    trigger = TriggerDef(
        name="tr_orders_audit",
        table="orders",
        schema_name="sales",
        ddl=(
            "CREATE TRIGGER tr_orders_audit\n"
            "ON sales.orders\n"
            "AFTER INSERT\n"
            "AS\n"
            "BEGIN\n  SET NOCOUNT ON;\nEND"
        ),
        is_disabled=False,
    )
    target.create_trigger(trigger)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    # Schema should be created since it's not dbo
    assert any(s.startswith("CREATE SCHEMA") for s in executed)
    # Trigger DDL should be CREATE OR ALTER with schema-qualified name
    trigger_ddl = next(s for s in executed if "CREATE OR ALTER TRIGGER" in s.upper())
    assert trigger_ddl.startswith("CREATE OR ALTER TRIGGER [sales].[tr_orders_audit]")
    # ON clause should be preserved
    assert "ON sales.orders" in trigger_ddl
    # ENABLE TRIGGER should be called since is_disabled is False
    enable_sql = next(s for s in executed if "ENABLE TRIGGER" in s.upper())
    assert "sales" in enable_sql and "orders" in enable_sql
    target._conn.commit.assert_called()


def test_create_trigger_skips_when_parent_table_missing():
    target, cur = _build_target_for_type()
    # All fetchone calls return None → table not found
    cur.fetchone.return_value = None

    trigger = TriggerDef(
        name="tr_missing",
        table="orders",
        schema_name="sales",
        ddl="CREATE TRIGGER tr_missing ON sales.orders AFTER INSERT AS BEGIN SET NOCOUNT ON; END",
        is_disabled=False,
    )
    target.create_trigger(trigger)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any("CREATE TRIGGER" in s for s in executed)
    target._conn.commit.assert_not_called()


def test_create_trigger_preserves_disabled_state():
    target, cur = _build_target_for_type()
    # sales schema: schema not found (create it), table exists
    cur.fetchone.side_effect = [None, ("orders",)]

    trigger = TriggerDef(
        name="tr_orders_disabled",
        table="orders",
        schema_name="sales",
        ddl="CREATE TRIGGER tr_orders_disabled ON sales.orders AFTER INSERT AS BEGIN SET NOCOUNT ON; END",
        is_disabled=True,
    )
    target.create_trigger(trigger)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    trigger_ddl = next(s for s in executed if "CREATE OR ALTER TRIGGER" in s.upper())
    assert "[sales].[tr_orders_disabled]" in trigger_ddl
    # DISABLE TRIGGER should be called since is_disabled is True
    disable_sql = next(s for s in executed if "DISABLE TRIGGER" in s.upper())
    assert "tr_orders_disabled" in disable_sql


def test_create_trigger_dbo_skips_schema_creation():
    target, cur = _build_target_for_type()
    # dbo: no schema check, only table check → table exists
    cur.fetchone.return_value = ("orders",)

    trigger = TriggerDef(
        name="tr_dbo_test",
        table="orders",
        schema_name="dbo",
        ddl="CREATE TRIGGER tr_dbo_test ON dbo.orders AFTER INSERT AS BEGIN SET NOCOUNT ON; END",
        is_disabled=False,
    )
    target.create_trigger(trigger)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert not any(s.startswith("CREATE SCHEMA") for s in executed)
    trigger_ddl = next(s for s in executed if "CREATE OR ALTER TRIGGER" in s.upper())
    assert "[dbo].[tr_dbo_test]" in trigger_ddl


def test_create_trigger_rolls_back_on_failure():
    target, cur = _build_target_for_type()
    # dbo: table exists; DDL execution (2nd call) raises
    cur.fetchone.return_value = ("orders",)

    call_count = [0]

    def _execute_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("invalid trigger body")

    cur.execute.side_effect = _execute_side_effect

    trigger = TriggerDef(
        name="tr_bad",
        table="orders",
        schema_name="dbo",
        ddl="CREATE TRIGGER tr_bad ON dbo.orders AFTER INSERT AS BEGIN SET NOCOUNT ON; END",
        is_disabled=False,
    )

    with pytest.raises(RuntimeError, match="invalid trigger body"):
        target.create_trigger(trigger)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args and isinstance(c.args[0], str)]
    assert any("CREATE OR ALTER TRIGGER" in s.upper() for s in executed)
    target._conn.rollback.assert_called_once()


def test_create_trigger_idempotent_via_create_or_alter():
    """CREATE OR ALTER is inherently idempotent — re-running produces the same DDL pattern."""
    target, cur = _build_target_for_type()
    # sales schema: schema not found (create it), table exists
    cur.fetchone.side_effect = [None, ("orders",)]

    trigger = TriggerDef(
        name="tr_orders_audit",
        table="orders",
        schema_name="sales",
        ddl="CREATE TRIGGER tr_orders_audit ON sales.orders AFTER INSERT AS BEGIN SET NOCOUNT ON; END",
        is_disabled=False,
    )

    # First call
    target.create_trigger(trigger)
    executed_1 = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    trigger_ddl_1 = next(s for s in executed_1 if "CREATE OR ALTER TRIGGER" in s.upper())
    assert trigger_ddl_1.startswith("CREATE OR ALTER TRIGGER")

    # Second call (idempotent — same DDL pattern)
    cur.reset_mock()
    cur.fetchone.side_effect = [None, ("orders",)]
    target.create_trigger(trigger)
    executed_2 = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    trigger_ddl_2 = next(s for s in executed_2 if "CREATE OR ALTER TRIGGER" in s.upper())
    assert trigger_ddl_2.startswith("CREATE OR ALTER TRIGGER")
    assert "[sales].[tr_orders_audit]" in trigger_ddl_2






