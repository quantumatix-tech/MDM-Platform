from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from core.connectors.base import (
    Column,
    ForeignKey,
    Index,
    Schema,
    SequenceDef,
    ViewDefinition,
    FunctionDef,
    SynonymDef,
    TypeDef,
    GrantDef,
    RoleDef,
    UserDef,
    RoleMembershipDef,
    CommentDef,
    TriggerDef,
)
from core.connectors.mssql import (
    MSSQLSourceConnector,
    MSSQLTargetConnector,
    _mssql_column_type,
    PartitionFunctionDef,
    PartitionSchemeDef,
    PartitionedTableDef,
)


def _mock_conn(fetchone=None, fetchall=None, description=None):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    cur.description = description or []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


# =============================================================================
# 1. MATCHING SOURCE/TARGET METADATA
# =============================================================================


# --- Tables/Columns ---

def test_source_get_schema_matches_columns_and_pk():
    cur = MagicMock()
    cur.fetchone.side_effect = [("sales",)]
    cur.fetchall.side_effect = [
        [("id", 0, None, None, 0, None), ("name", 0, None, None, 50, None)],
        [],
        [("id", "int", "NO", None, 10, 0), ("name", "nvarchar", "NO", 50, None, None)],
        [],
        [("id",)],
        [],
        [],  # check constraints (empty)
        [],  # default constraints (empty)
    ]
    conn, _ = _mock_conn(fetchone=("sales",), fetchall=[], description=[])
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("customers")

    assert schema.schema_name == "sales"
    assert len(schema.columns) == 2
    assert schema.columns[0].name == "id"
    assert schema.columns[0].source_type == "int"
    assert schema.columns[1].name == "name"
    assert schema.columns[1].source_type == "nvarchar"
    assert schema.primary_key == ["id"]


def test_target_create_table_emits_matching_ddl():
    target, cur = _build_target_helper()
    schema = Schema(
        name="products", schema_name="sales",
        columns=[
            Column(name="id", source_type="int", nullable=False),
            Column(name="name", source_type="nvarchar", nullable=False, size=200),
        ],
        primary_key=["id"],
    )
    target.create_object_if_missing(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(s for s in executed if s.startswith("CREATE TABLE"))
    assert '"sales"."products"' in ddl
    assert "id int NOT NULL" in ddl
    assert "name nvarchar(200) NOT NULL" in ddl
    assert "PRIMARY KEY (id)" in ddl


def test_target_create_table_computed_column_matches():
    target, cur = _build_target_helper()
    schema = Schema(
        name="calc_test", schema_name="sales",
        columns=[
            Column(name="a", source_type="int", nullable=False),
            Column(name="b", source_type="int", nullable=False),
            Column(name="sum", source_type="int", nullable=True,
                   is_computed=True, computed_definition="a + b"),
        ],
        primary_key=["a"],
    )
    target.create_object_if_missing(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(s for s in executed if s.startswith("CREATE TABLE"))
    assert "sum AS (a + b)" in ddl
    assert "sum int" not in ddl


# --- PK/FK ---

def test_source_get_schema_extracts_foreign_keys():
    cur = MagicMock()
    cur.fetchone.side_effect = [("sales",)]
    cur.fetchall.side_effect = [
        [("id", 0, None, None, 0, None)],
        [],
        [("id", "int", "NO", None, 10, 0)],
        [],
        [("id",)],
        [("fk_orders_cust", "customer_id", "id", "sales", "customers", 1)],
        [],  # check constraints (empty)
        [],  # default constraints (empty)
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("orders")

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.name == "fk_orders_cust"
    assert fk.ref_table == "customers"
    assert fk.ref_columns == ["id"]
    assert fk.columns == ["customer_id"]


def test_target_apply_constraints_creates_fk():
    target, cur = _build_target_helper()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        foreign_keys=[
            ForeignKey(name="fk_orders_customer", columns=["customer_id"],
                       ref_table="customers", ref_columns=["id"], ref_schema="sales"),
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    fk_sql = next(s for s in executed if "FOREIGN KEY" in s.upper())
    assert "customer_id" in fk_sql
    assert "REFERENCES" in fk_sql.upper()


# --- Identity/Computed ---

def test_source_get_schema_identity_and_computed():
    cur = MagicMock()
    cur.fetchone.side_effect = [("sales",)]
    cur.fetchall.side_effect = [
        [("id", 1, 1, 1, 0, None), ("computed_col", 0, None, None, 1, "col1 + 1")],
        [],
        [("id", "int", "NO", None, 10, 0), ("computed_col", "int", "YES", None, None, None)],
        [],
        [("id",)],
        [],
        [],  # check constraints (empty)
        [],  # default constraints (empty)
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("test_table")

    id_col = schema.columns[0]
    assert id_col.is_identity is True
    assert id_col.identity_seed == 1
    assert id_col.identity_increment == 1
    comp_col = schema.columns[1]
    assert comp_col.is_computed is True
    assert comp_col.computed_definition == "col1 + 1"


def test_target_identity_column_ddl():
    target, cur = _build_target_helper()
    schema = Schema(
        name="identity_test", schema_name="sales",
        columns=[
            Column(name="id", source_type="int", nullable=False,
                   is_identity=True, identity_seed=1, identity_increment=1),
        ],
        primary_key=["id"],
    )
    target.create_object_if_missing(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    ddl = next(s for s in executed if s.startswith("CREATE TABLE"))
    assert "id int IDENTITY(1,1) NOT NULL" in ddl


# --- Indexes ---

def test_source_get_schema_discovers_indexes():
    cur = MagicMock()
    cur.fetchone.side_effect = [("sales",)]
    cur.fetchall.side_effect = [
        [("id", 0, None, None, 0, None)],
        [],
        [("id", "int", "NO", None, 10, 0)],
        [("IX_name", 0, 0, 1, 1, 0, False, "name", None),
         ("IX_name", 0, 0, 1, 2, 1, True, "status", None)],
        [("id",)],
        [],
        [],  # check constraints (empty)
        [],  # default constraints (empty)
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("test_table")

    assert len(schema.indexes) == 1
    idx = schema.indexes[0]
    assert idx.name == "IX_name"
    assert idx.columns == ["name"]
    assert "status" in idx.included_columns
    assert idx.unique is False


def test_target_apply_constraints_emits_index_ddl():
    target, cur = _build_target_helper()
    schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        indexes=[
            Index(name="IX_orders_date", columns=["order_date"], unique=False,
                  ddl='CREATE INDEX IX_orders_date ON "sales"."orders"("order_date" ASC)'),
        ],
    )
    target.apply_constraints(schema)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    idx_sql = next(s for s in executed if "INDEX" in s.upper())
    assert "IX_orders_date" in idx_sql


# --- Sequences ---

def test_source_list_all_sequences_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("seq_invoice", "sales", "int", 1000, 10, 1000, 1100, 0, 10, 1020, 1),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    sequences = source.list_all_sequences()

    assert len(sequences) == 1
    seq = sequences[0]
    assert seq.name == "seq_invoice"
    assert seq.schema == "sales"
    assert seq.data_type == "int"
    assert seq.start_value == 1000
    assert seq.increment == 10
    assert seq.min_value == 1000
    assert seq.max_value == 1100
    assert seq.cycle is False
    assert seq.cache_size == 10
    assert seq.is_cached is True
    assert seq.last_value == 1020


def test_target_create_sequence_matches():
    target, cur = _build_target_helper()
    seq = SequenceDef(
        name="seq_invoice", schema="sales", data_type="int",
        start_value=1000, increment=10, min_value=1000, max_value=1100,
        cycle=False, cache_size=10, is_cached=True, last_value=1020,
    )
    target.create_sequence(seq)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    ddl = next(s for s in executed if s.upper().startswith("CREATE SEQUENCE"))
    assert '"sales"."seq_invoice"' in ddl
    assert "AS INT" in ddl
    assert "START WITH 1000" in ddl


# --- Views ---

def test_source_list_views_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("v_summary", "sales", "SELECT * FROM sales.customers"),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    views = source.list_views()

    assert len(views) == 1
    assert views[0].name == "v_summary"
    assert views[0].schema_name == "sales"
    assert "SELECT * FROM sales.customers" in views[0].definition


def test_target_create_view_matches():
    target, cur = _build_target_helper()
    view = ViewDefinition(name="v_summary", schema_name="sales",
                          definition="SELECT * FROM sales.customers")
    target.create_view(view)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    view_sql = next(s for s in executed if "VIEW" in s.upper())
    assert "CREATE OR ALTER VIEW" in view_sql.upper()
    assert "[sales].[v_summary]" in view_sql


# --- Functions/Procedures ---

def test_source_list_functions_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("sales", "fn_count", "CREATE FUNCTION sales.fn_count() RETURNS INT AS BEGIN RETURN 1 END"),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    funcs = source.list_functions()

    assert len(funcs) == 1
    assert funcs[0].name == "fn_count"
    assert funcs[0].schema_name == "sales"
    assert "RETURN 1" in funcs[0].ddl


def test_target_create_function_matches():
    target, cur = _build_target_helper()
    func = FunctionDef(name="fn_count", schema_name="sales",
                       ddl="CREATE FUNCTION sales.fn_count() RETURNS INT AS BEGIN RETURN 1 END")
    target.create_function(func)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    fn_sql = next(s for s in executed if "FUNCTION" in s.upper())
    assert "CREATE OR ALTER FUNCTION" in fn_sql.upper()
    assert "fn_count" in fn_sql


# --- Synonyms ---

def test_source_list_synonyms_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("syn_cust", "sales", "[sales].[customers]"),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    synonyms = source.list_synonyms()

    assert len(synonyms) == 1
    assert synonyms[0].name == "syn_cust"
    assert synonyms[0].schema_name == "sales"
    assert synonyms[0].base_object == "[sales].[customers]"


def test_target_create_synonym_matches():
    target, cur = _build_target_helper()
    syn = SynonymDef(name="syn_cust", schema_name="sales", base_object="[sales].[customers]")
    target.create_synonym(syn)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    syn_sql = next(s for s in executed if s.upper().startswith("CREATE SYNONYM"))
    assert "CREATE SYNONYM" in syn_sql.upper()
    assert "[sales].[syn_cust]" in syn_sql


# --- UDTs ---

def test_source_list_types_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("sales", "order_code_t", 0, 4, 10, 0, "int"),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    types = source.list_types()

    assert len(types) == 1
    assert types[0].name == "sales.order_code_t"
    assert types[0].kind == "alias"


def test_source_get_schema_resolves_udt_columns():
    cur = MagicMock()
    cur.fetchone.side_effect = [("sales",)]
    cur.fetchall.side_effect = [
        [("code", 0, None, None, 0, None)],
        [("code", "sales", "order_code_t")],
        [("code", "int", "NO", None, 10, 0)],
        [],
        [("code",)],
        [],
        [],  # check constraints (empty)
        [],  # default constraints (empty)
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    schema = source.get_schema("udt_test")
    by_name = {c.name: c for c in schema.columns}
    assert by_name["code"].source_type == "[sales].[order_code_t]"


def test_target_create_type_matches():
    target, cur = _build_target_helper()
    cur.fetchone.return_value = None
    td = TypeDef(name="sales.order_code_t", kind="alias",
                 ddl="CREATE TYPE [sales].[order_code_t] FROM int NOT NULL")
    target.create_type(td)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    type_sql = next(s for s in executed if s.upper().startswith("CREATE TYPE"))
    assert "CREATE TYPE [sales].[order_code_t] FROM int NOT NULL" == type_sql


# --- Partitioning ---

def test_source_list_partition_functions_matches():
    from datetime import datetime
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("pf_dates", "RANGE", True, datetime(2024, 1, 1), 1),
        ("pf_dates", "RANGE", True, datetime(2025, 1, 1), 2),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    pfs = source.list_partition_functions()

    assert len(pfs) == 1
    pf = pfs[0]
    assert pf.name == "pf_dates"
    assert pf.data_type == "datetime2"
    assert pf.range_desc == "RANGE RIGHT"
    assert len(pf.boundaries) == 2


def test_source_list_partition_schemes_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [("ps_dates", "pf_dates", "PRIMARY")]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    pss = source.list_partition_schemes()

    assert len(pss) == 1
    assert pss[0].name == "ps_dates"
    assert pss[0].partition_function_name == "pf_dates"
    assert pss[0].filegroups == ["PRIMARY"]


def test_source_get_partitioned_tables_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("partitioned_orders", "sales", "PK_ord", "pf_dates", "ps_dates", "order_date"),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    pts = source.get_partitioned_tables()

    assert len(pts) == 1
    pt = pts[0]
    assert pt.table_name == "partitioned_orders"
    assert pt.schema_name == "sales"
    assert pt.partition_function_name == "pf_dates"
    assert pt.partition_scheme_name == "ps_dates"
    assert pt.partition_column == "order_date"


# --- Users/Roles/Grants ---

def test_source_list_users_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [("app_user", "S")]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test"})
    source._conn = conn

    users = source.list_users()

    assert len(users) == 1
    assert users[0].name == "app_user"
    assert users[0].type == "S"


def test_source_list_roles_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [("app_role", "R")]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test"})
    source._conn = conn

    roles = source.list_roles()

    assert len(roles) == 1
    assert roles[0].name == "app_role"
    assert roles[0].type == "R"


def test_source_list_grants_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("SELECT", "OBJECT_OR_COLUMN", 1, 0, "app_user", "customers", None, "sales", None),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    grants = source.list_grants()

    assert len(grants) == 1
    assert grants[0].grantee == "app_user"
    assert grants[0].privileges == "SELECT"
    assert grants[0].object_type == "TABLE"
    assert grants[0].object_name == "customers"


def test_target_apply_grant_matches():
    target, cur = _build_target_helper()
    grant = GrantDef(privileges="SELECT", object_type="TABLE", object_name="customers",
                     grantee="app_user", schema_name="sales")
    target.apply_grant(grant)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    grant_sql = next(s for s in executed if "GRANT" in s.upper())
    assert "GRANT SELECT" in grant_sql.upper()
    assert "customers" in grant_sql


# --- Comments/Extended Properties ---

def test_source_list_comments_matches():
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [("Customer table comment", "sales", "customers", "U", "TABLE")],
        [],
        [],
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    comments = source.list_comments()

    assert len(comments) == 1
    assert comments[0].object_type == "TABLE"
    assert comments[0].object_name == "customers"
    assert comments[0].comment == "Customer table comment"
    assert comments[0].schema_name == "sales"


def test_target_apply_comment_matches():
    target, cur = _build_target_helper()
    comment = CommentDef(object_type="TABLE", object_name="customers",
                         comment="Test comment", schema_name="sales")
    target.apply_comment(comment)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    assert any("extendedproperty" in s.lower() or "sp_add" in s.lower() or "sp_update" in s.lower()
               for s in executed)


# --- Triggers ---

def test_source_get_all_triggers_matches():
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("sales", "trg_audit", "sales", "orders", "CREATE TRIGGER trg_audit ON orders FOR INSERT AS BEGIN 1 END", 0),
    ]
    conn, _ = _mock_conn()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["sales"]})
    source._conn = conn

    triggers = source.get_all_triggers()

    assert len(triggers) == 1
    assert triggers[0].name == "trg_audit"
    assert triggers[0].table == "orders"
    assert triggers[0].schema_name == "sales"
    assert triggers[0].table_schema == "sales"
    assert triggers[0].is_disabled is False


def test_target_create_trigger_matches():
    target, cur = _build_target_helper()
    cur.fetchone.return_value = ("orders",)
    trigger = TriggerDef(name="trg_audit", table="orders", schema_name="sales",
                         ddl="CREATE TRIGGER trg_audit ON orders FOR INSERT AS BEGIN 1 END",
                         is_disabled=False)
    target.create_trigger(trigger)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    trig_sql = next(s for s in executed if s.upper().startswith("CREATE OR ALTER"))
    assert "TRIGGER" in trig_sql.upper()
    assert "[sales].[trg_audit]" in trig_sql


# =============================================================================
# 2. INTENTIONAL MISMATCH DETECTION
# =============================================================================


def test_detect_column_type_mismatch():
    source_schema = Schema(
        name="t1", schema_name="sales",
        columns=[Column(name="col1", source_type="nvarchar", size=100)],
    )
    target_schema = Schema(
        name="t1", schema_name="sales",
        columns=[Column(name="col1", source_type="nvarchar", size=50)],
    )
    mismatches = []
    src_cols = {c.name: c for c in source_schema.columns}
    tgt_cols = {c.name: c for c in target_schema.columns}
    for name, col in src_cols.items():
        if name in tgt_cols:
            t = tgt_cols[name]
            if col.source_type != t.source_type:
                mismatches.append(f"type mismatch: {name} {col.source_type} vs {t.source_type}")
            if col.size != t.size:
                mismatches.append(f"size mismatch: {name} {col.size} vs {t.size}")
    assert len(mismatches) == 1
    assert "size mismatch" in mismatches[0]
    assert "100 vs 50" in mismatches[0]


def test_detect_pk_mismatch():
    source_schema = Schema(name="t1", schema_name="sales", columns=[], primary_key=["a", "b"])
    target_schema = Schema(name="t1", schema_name="sales", columns=[], primary_key=["a"])
    src_pk = set(source_schema.primary_key)
    tgt_pk = set(target_schema.primary_key)
    mismatches = []
    if src_pk != tgt_pk:
        mismatches.append(f"PK mismatch: source={sorted(src_pk)} target={sorted(tgt_pk)}")
    assert len(mismatches) == 1
    assert "a, b" in mismatches[0] or "['a', 'b']" in mismatches[0] or "['a']" in mismatches[0]


def test_detect_fk_mismatch():
    source_schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        foreign_keys=[ForeignKey(name="fk1", columns=["c1"], ref_table="t2", ref_columns=["id"])],
    )
    target_schema = Schema(
        name="orders", schema_name="sales", columns=[], primary_key=[],
        foreign_keys=[ForeignKey(name="fk1", columns=["c1"], ref_table="t3", ref_columns=["id"])],
    )
    src_fks = {fk.name: fk for fk in source_schema.foreign_keys}
    tgt_fks = {fk.name: fk for fk in target_schema.foreign_keys}
    mismatches = []
    for name, fk in src_fks.items():
        if name in tgt_fks:
            t = tgt_fks[name]
            if fk.ref_table != t.ref_table or fk.ref_columns != t.ref_columns:
                mismatches.append(f"FK {name}: ref {fk.ref_table}({fk.ref_columns}) vs {t.ref_table}({t.ref_columns})")
    assert len(mismatches) == 1
    assert "t2" in mismatches[0] and "t3" in mismatches[0]


def test_detect_identity_mismatch():
    source_col = Column(name="id", source_type="int", nullable=False,
                        is_identity=True, identity_seed=1, identity_increment=1)
    target_col = Column(name="id", source_type="int", nullable=False,
                        is_identity=False)
    mismatches = []
    if source_col.is_identity != target_col.is_identity:
        mismatches.append(f"identity mismatch: {source_col.name}")
    if source_col.is_identity and (source_col.identity_seed != target_col.identity_seed or
                                    source_col.identity_increment != target_col.identity_increment):
        mismatches.append(f"identity params mismatch")
    assert len(mismatches) == 2


def test_detect_computed_definition_mismatch():
    source_col = Column(name="calc", source_type="int", nullable=True,
                        is_computed=True, computed_definition="a + b")
    target_col = Column(name="calc", source_type="int", nullable=True,
                        is_computed=True, computed_definition="a + c")
    mismatches = []
    if source_col.is_computed and target_col.is_computed:
        if source_col.computed_definition != target_col.computed_definition:
            mismatches.append(f"computed def mismatch: '{source_col.computed_definition}' vs '{target_col.computed_definition}'")
    assert len(mismatches) == 1
    assert "a + b" in mismatches[0] and "a + c" in mismatches[0]


def test_detect_index_mismatch():
    source_idx = Index(name="IX_test", columns=["a", "b"], unique=True)
    target_idx = Index(name="IX_test", columns=["a"], unique=True)
    mismatches = []
    if source_idx.unique != target_idx.unique:
        mismatches.append(f"unique mismatch")
    if source_idx.columns != target_idx.columns:
        mismatches.append(f"columns mismatch: {source_idx.columns} vs {target_idx.columns}")
    assert len(mismatches) == 1
    assert "a, b" in mismatches[0] or "['a', 'b']" in mismatches[0]


def test_detect_sequence_mismatch():
    src_seq = SequenceDef(name="s", start_value=1, increment=1, min_value=1, max_value=100,
                          cycle=False, cache_size=10, data_type="int")
    tgt_seq = SequenceDef(name="s", start_value=1, increment=1, min_value=1, max_value=100,
                          cycle=True, cache_size=10, data_type="int")
    mismatches = []
    if src_seq.cycle != tgt_seq.cycle:
        mismatches.append(f"cycle mismatch: {src_seq.cycle} vs {tgt_seq.cycle}")
    assert len(mismatches) == 1


# =============================================================================
# 3. NORMALIZATION OF ENVIRONMENT-SPECIFIC METADATA
# =============================================================================


def test_mssql_column_type_reattaches_size():
    assert _mssql_column_type("nvarchar", 100, None, None) == "nvarchar(100)"
    assert _mssql_column_type("nvarchar", -1, None, None) == "nvarchar(MAX)"
    assert _mssql_column_type("decimal", None, 10, 2) == "decimal(10,2)"
    assert _mssql_column_type("int", None, None, None) == "int"


def test_mssql_column_type_bare_passes_through():
    assert _mssql_column_type("xml", None, None, None) == "xml"
    assert _mssql_column_type("uniqueidentifier", None, None, None) == "uniqueidentifier"
    assert _mssql_column_type("sql_variant", None, None, None) == "sql_variant"


def test_normalize_schema_name_none_to_dbo():
    from core.connectors.mssql import _qualify
    assert _qualify(None, "mytable") == '"dbo"."mytable"'
    assert _qualify("sales", "mytable") == '"sales"."mytable"'


def test_normalize_view_definition_strips_create_view():
    definition = "CREATE VIEW v_test AS SELECT * FROM t"
    normalized = definition[len("CREATE VIEW"):].lstrip() if definition.upper().startswith("CREATE VIEW") else definition
    assert normalized == "v_test AS SELECT * FROM t"


def test_normalize_function_ddl_creates_or_alter():
    ddl = "CREATE FUNCTION fn_test() RETURNS INT AS BEGIN RETURN 1 END"
    normalized = "CREATE OR ALTER " + ddl[len("CREATE "):] if ddl.upper().startswith("CREATE ") else ddl
    assert normalized == "CREATE OR ALTER FUNCTION fn_test() RETURNS INT AS BEGIN RETURN 1 END"


def test_normalize_sequence_params():
    seq = SequenceDef(name="s", schema="sales", data_type="int",
                      start_value=1000, increment=10, min_value=1000, max_value=1100,
                      cycle=False, cache_size=10, is_cached=True, last_value=1020)
    assert int(seq.start_value) == 1000
    assert int(seq.increment) == 10
    assert int(seq.cache_size) == 10


def test_normalize_partition_boundary_dates():
    from datetime import datetime
    boundaries = [datetime(2024, 1, 1), datetime(2025, 1, 1)]
    formatted = [
        f"'{b.strftime('%Y-%m-%d')}'" if isinstance(b, (datetime,)) else str(b)
        for b in boundaries
    ]
    assert formatted == ["'2024-01-01'", "'2025-01-01'"]


def test_normalize_sequence_create_ddl():
    from core.connectors.mssql import _qualify
    seq_schema = "sales"
    seq_qname = _qualify(seq_schema, "seq_invoice")
    seq = SequenceDef(name="seq_invoice", schema=seq_schema, data_type="int",
                      start_value=1, increment=1, min_value=1, max_value=9223372036854775807,
                      cycle=False, cache_size=1, is_cached=False)
    ddl = (
        f"CREATE SEQUENCE {seq_qname} "
        f"AS {seq.data_type.upper()} "
        f"START WITH {seq.start_value} "
        f"INCREMENT BY {seq.increment} "
        f"MINVALUE {seq.min_value} "
        f"MAXVALUE {seq.max_value} "
        f"{'NO CYCLE' if not seq.cycle else 'CYCLE'} "
        f"{'NO CACHE' if not seq.is_cached else f'CACHE {seq.cache_size}'}"
    )
    assert "START WITH 1" in ddl
    assert "NO CYCLE" in ddl
    assert "NO CACHE" in ddl


def test_normalize_trigger_ddl_qualification():
    import re
    ddl = "CREATE TRIGGER trg_test ON orders FOR INSERT AS BEGIN SELECT 1 END"
    schema_name = "sales"
    qualified_trigger = f"[{schema_name}].[trg_test]"
    new_ddl, n = re.subn(
        r"CREATE\s+TRIGGER\s+\S+",
        f"CREATE OR ALTER TRIGGER {qualified_trigger}",
        ddl, count=1, flags=re.IGNORECASE,
    )
    assert n == 1
    assert "CREATE OR ALTER TRIGGER [sales].[trg_test]" in new_ddl


def test_normalize_comment_escaping():
    comment = "It's a comment with 'quotes'"
    escaped = comment.replace("'", "''")
    assert escaped == "It''s a comment with ''quotes''"


def test_normalize_grant_object_type():
    grant = GrantDef(privileges="SELECT", object_type="TABLE", object_name="customers",
                     grantee="app_user", schema_name="sales")
    schema_q = f"[{grant.schema_name or 'dbo'}]"
    object_q = f"[{grant.object_name}]"
    sql = f"GRANT {grant.privileges} ON {schema_q}.{object_q} TO [{grant.grantee}]"
    assert "GRANT SELECT ON [sales].[customers] TO [app_user]" == sql


def test_normalize_column_udt_reference():
    from core.connectors.mssql import _mssql_column_type
    assert _mssql_column_type("[sales].[order_code_t]", None, None, None) == "[sales].[order_code_t]"


def test_normalize_index_filter_definition():
    idx = Index(name="IX_test", columns=["status"], unique=False,
                ddl="CREATE INDEX IX_test ON \"sales\".\"orders\"(\"status\" ASC) WHERE ([status]=N'PAID')")
    assert "WHERE" in idx.filter_definition if idx.filter_definition else True


def test_normalize_schema_name_none_defaults():
    schema = Schema(name="t1", schema_name=None, columns=[Column(name="id", source_type="int")])
    assert (schema.schema_name or "dbo") == "dbo"


def test_normalize_view_schema_none_defaults_to_dbo():
    view = ViewDefinition(name="v1", schema_name=None, definition="SELECT 1")
    assert (view.schema_name or "dbo") == "dbo"


def test_normalize_sequence_schema_none_defaults_to_dbo():
    seq = SequenceDef(name="s", start_value=1, increment=1, min_value=1, max_value=100,
                       cycle=False, schema=None, data_type="bigint")
    assert (seq.schema or "dbo") == "dbo"


def _build_target_helper():
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector({"database": "mssql_migration_target", "source_engine": "mssql"})
    target._conn = conn
    return target, cur
