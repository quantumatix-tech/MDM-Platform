from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from core.connectors.mssql import MSSQLTargetConnector, MSSQLSourceConnector, _build_mssql_index_ddl, PartitionFunctionDef, PartitionSchemeDef, PartitionedTableDef
from core.connectors.base import Schema, Column, Index, SequenceDef, ViewDefinition, FunctionDef, SynonymDef, TypeDef


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

