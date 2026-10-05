"""Expected state of the PostgreSQL E2E source fixture.

Every value here is derived directly from the SQL scripts in
``tests/e2e/postgresql/fixtures/`` and verified against the live
``MigrationE2E_PostgreSQL_Source`` database via ``test_pg_discovery.py``.

If you change a script, update this module too.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpectedColumn:
    name: str
    base_type: str
    nullable: bool
    is_identity: bool = False
    identity_kind: str | None = None
    identity_seed: int | None = None
    identity_increment: int | None = None
    is_generated: bool = False
    size: int | None = None
    precision: int | None = None
    scale: int | None = None
    udt_name: str | None = None
    default_value: str | None = None


@dataclass(frozen=True)
class ExpectedTable:
    name: str
    columns: list[ExpectedColumn]
    pk_name: str | None
    pk_columns: list[str]
    row_count: int
    is_partitioned: bool = False
    partition_key: str | None = None
    rls_enabled: bool = False


@dataclass(frozen=True)
class ExpectedForeignKey:
    constraint_name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    on_delete: str = "NO ACTION"
    on_update: str = "NO ACTION"


@dataclass(frozen=True)
class ExpectedUniqueConstraint:
    name: str
    columns: list[str]


@dataclass(frozen=True)
class ExpectedCheckConstraint:
    name: str


@dataclass(frozen=True)
class ExpectedIndex:
    name: str
    unique: bool
    columns: list[str]


@dataclass(frozen=True)
class ExpectedView:
    name: str


@dataclass(frozen=True)
class ExpectedType:
    name: str
    kind: str  # 'enum'
    label: str | None = None


@dataclass(frozen=True)
class ExpectedFunction:
    name: str
    kind: str  # 'function' or 'procedure'


@dataclass(frozen=True)
class ExpectedTrigger:
    name: str
    table: str
    is_disabled: bool


@dataclass(frozen=True)
class ExpectedRLS:
    table: str
    policies: list[str]


@dataclass(frozen=True)
class ExpectedGrant:
    grantee: str
    privilege: str
    object_type: str
    object_name: str


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_NAME = "training"

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
TABLES: dict[str, ExpectedTable] = {
    "customers": ExpectedTable(
        name="customers",
        columns=[
            ExpectedColumn("customerid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("customercode", "character varying(50)", False, size=50),
            ExpectedColumn("fullname", "character varying(100)", False, size=100),
            ExpectedColumn("email", "character varying(150)", False, size=150),
            ExpectedColumn("phone", "character varying(20)", True, size=20),
            ExpectedColumn("city", "character varying(100)", True, size=100),
            ExpectedColumn("status", "character varying(20)", False, size=20,
                           default_value="'active'"),
            ExpectedColumn("isactive", "boolean", False,
                           default_value="true"),
            ExpectedColumn("createdat", "timestamp without time zone", False,
                           default_value="now()"),
        ],
        pk_name="pk_customers",
        pk_columns=["customerid"],
        row_count=5,
        rls_enabled=True,
    ),
    "orderaudit": ExpectedTable(
        name="orderaudit",
        columns=[
            ExpectedColumn("auditid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("orderid", "integer", False),
            ExpectedColumn("action", "character varying(50)", False, size=50),
            ExpectedColumn("audittimestamp", "timestamp without time zone", False,
                           default_value="now()"),
        ],
        pk_name="pk_orderaudit",
        pk_columns=["auditid"],
        row_count=5,
    ),
    "orderdetails": ExpectedTable(
        name="orderdetails",
        columns=[
            ExpectedColumn("detailid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("orderid", "integer", False),
            ExpectedColumn("productid", "integer", False),
            ExpectedColumn("quantity", "integer", False),
            ExpectedColumn("unitprice", "numeric(10,2)", False, precision=10, scale=2),
            ExpectedColumn("linetotal", "numeric(10,2)", True, precision=10, scale=2,
                           is_generated=True),
        ],
        pk_name="pk_orderdetails",
        pk_columns=["detailid"],
        row_count=8,
    ),
    "orders": ExpectedTable(
        name="orders",
        columns=[
            ExpectedColumn("orderid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("ordernumber", "bigint", False,
                           default_value="nextval('training.seq_ordernumber'::regclass)"),
            ExpectedColumn("customerid", "integer", False),
            ExpectedColumn("totalamount", "numeric(10,2)", True, precision=10, scale=2),
            ExpectedColumn("orderstatus", "training.orderstatus", False,
                           udt_name="orderstatus",
                           default_value="'pending'"),
            ExpectedColumn("orderdate", "timestamp without time zone", False,
                           default_value="now()"),
            ExpectedColumn("modifiedat", "timestamp without time zone", True,
                           is_generated=True),
        ],
        pk_name="pk_orders",
        pk_columns=["orderid"],
        row_count=5,
    ),
    "partitionedorders": ExpectedTable(
        name="partitionedorders",
        columns=[
            ExpectedColumn("partitionid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("orderdate", "date", False),
            ExpectedColumn("amount", "numeric(10,2)", False, precision=10, scale=2),
            ExpectedColumn("status", "character varying(50)", False, size=50,
                           default_value="'pending'"),
        ],
        pk_name="pk_partitionedorders",
        pk_columns=["partitionid", "orderdate"],
        row_count=3,
        is_partitioned=True,
        partition_key="RANGE (orderdate)",
    ),
    "pk_name_test": ExpectedTable(
        name="pk_name_test",
        columns=[
            ExpectedColumn("testid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("testname", "character varying(100)", False, size=100),
        ],
        pk_name="pk_testprimarykey",
        pk_columns=["testid"],
        row_count=2,
    ),
    "products": ExpectedTable(
        name="products",
        columns=[
            ExpectedColumn("productid", "integer", False, is_identity=True,
                           identity_kind="BY DEFAULT", identity_seed=1, identity_increment=1),
            ExpectedColumn("sku", "character varying(50)", False, size=50),
            ExpectedColumn("name", "character varying(200)", False, size=200),
            ExpectedColumn("category", "character varying(100)", False, size=100,
                           default_value="'General'"),
            ExpectedColumn("price", "numeric(10,2)", False, precision=10, scale=2),
            ExpectedColumn("stockqty", "integer", False, default_value="0"),
            ExpectedColumn("isactive", "boolean", False, default_value="true"),
            ExpectedColumn("createdat", "timestamp without time zone", False,
                           default_value="now()"),
        ],
        pk_name="pk_products",
        pk_columns=["productid"],
        row_count=5,
    ),
}

# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------
FOREIGN_KEYS: dict[str, list[ExpectedForeignKey]] = {
    "orders": [
        ExpectedForeignKey(
            "fk_orders_customers", ["customerid"], "customers", ["customerid"],
            on_delete="NO ACTION", on_update="NO ACTION",
        ),
    ],
    "orderdetails": [
        ExpectedForeignKey(
            "fk_orderdetails_orders", ["orderid"], "orders", ["orderid"],
            on_delete="NO ACTION", on_update="NO ACTION",
        ),
        ExpectedForeignKey(
            "fk_orderdetails_products", ["productid"], "products", ["productid"],
            on_delete="NO ACTION", on_update="NO ACTION",
        ),
    ],
}

# ---------------------------------------------------------------------------
# Unique constraints (non-PK)
# ---------------------------------------------------------------------------
UNIQUE_CONSTRAINTS: dict[str, list[ExpectedUniqueConstraint]] = {
    "customers": [ExpectedUniqueConstraint("uq_customers_email", ["email"])],
    "products": [ExpectedUniqueConstraint("uq_products_sku", ["sku"])],
}

# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------
CHECK_CONSTRAINTS: dict[str, list[str]] = {
    "customers": ["ck_customers_email", "ck_customers_name"],
    "orderdetails": ["ck_orderdetails_quantity", "ck_orderdetails_unitprice"],
    "orders": ["ck_orders_amount"],
    "products": ["ck_products_price", "ck_products_stock"],
}

# ---------------------------------------------------------------------------
# Non-PK indexes
# ---------------------------------------------------------------------------
INDEXES: dict[str, list[ExpectedIndex]] = {
    "orders": [
        ExpectedIndex("ix_orders_customerid", False, ["customerid"]),
        ExpectedIndex("ix_orders_orderdate", False, ["orderdate"]),
    ],
    "orderdetails": [
        ExpectedIndex("ix_orderdetails_orderid", False, ["orderid"]),
        ExpectedIndex("ix_orderdetails_productid", False, ["productid"]),
    ],
}

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
VIEWS: list[ExpectedView] = [ExpectedView("vw_ordersummary")]

# ---------------------------------------------------------------------------
# User-defined types (ENUM)
# ---------------------------------------------------------------------------
USER_TYPES: dict[str, ExpectedType] = {
    "orderstatus": ExpectedType("orderstatus", kind="enum"),
}

# ---------------------------------------------------------------------------
# Functions and procedures
# ---------------------------------------------------------------------------
FUNCTIONS: dict[str, ExpectedFunction] = {
    "fn_getordertotal": ExpectedFunction("fn_getordertotal", kind="function"),
    "trg_auditorder": ExpectedFunction("trg_auditorder", kind="function"),
}

PROCEDURES: list[str] = ["sp_updateproductstock"]

# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
TRIGGERS: dict[str, ExpectedTrigger] = {
    "tr_auditorder": ExpectedTrigger(
        name="tr_auditorder", table="orders", is_disabled=False,
    ),
}

# ---------------------------------------------------------------------------
# Standalone sequences (not owned by identity columns)
# ---------------------------------------------------------------------------
SEQUENCES: dict[str, dict[str, int | str]] = {
    "seq_ordernumber": {"start_value": 1000, "increment": 1, "data_type": "bigint"},
}

# ---------------------------------------------------------------------------
# Partitions: parent_table -> list of partition names
# ---------------------------------------------------------------------------
PARTITIONS: dict[str, list[str]] = {
    "partitionedorders": [
        "partitionedorders_2024",
        "partitionedorders_2025_h1",
        "partitionedorders_2025_h2",
        "partitionedorders_default",
    ],
}

# ---------------------------------------------------------------------------
# RLS policies
# ---------------------------------------------------------------------------
RLS_POLICIES: dict[str, list[str]] = {
    "customers": ["p_customers_city_isolation"],
}

# ---------------------------------------------------------------------------
# Security: roles and grants
# ---------------------------------------------------------------------------
ROLES: list[str] = ["migration_role"]

GRANTS: list[ExpectedGrant] = [
    # Table grants for migration_role
    ExpectedGrant("migration_role", "DELETE", "TABLE", "customers"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "customers"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "customers"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "customers"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "orderaudit"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "orderaudit"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "orderaudit"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "orderaudit"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "orderdetails"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "orderdetails"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "orderdetails"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "orderdetails"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "orders"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "orders"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "orders"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "orders"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "partitionedorders"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "partitionedorders"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "partitionedorders"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "partitionedorders"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "partitionedorders_2024"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "partitionedorders_2024"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "partitionedorders_2024"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "partitionedorders_2024"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "partitionedorders_2025_h1"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "partitionedorders_2025_h1"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "partitionedorders_2025_h1"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "partitionedorders_2025_h1"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "partitionedorders_2025_h2"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "partitionedorders_2025_h2"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "partitionedorders_2025_h2"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "partitionedorders_2025_h2"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "partitionedorders_default"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "partitionedorders_default"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "partitionedorders_default"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "partitionedorders_default"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "pk_name_test"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "pk_name_test"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "pk_name_test"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "pk_name_test"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "products"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "products"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "products"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "products"),
    ExpectedGrant("migration_role", "DELETE", "TABLE", "vw_ordersummary"),
    ExpectedGrant("migration_role", "INSERT", "TABLE", "vw_ordersummary"),
    ExpectedGrant("migration_role", "SELECT", "TABLE", "vw_ordersummary"),
    ExpectedGrant("migration_role", "UPDATE", "TABLE", "vw_ordersummary"),
    # Sequence grants for migration_role
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "customers_customerid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "orderaudit_auditid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "orderdetails_detailid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "orders_orderid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "partitionedorders_partitionid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "pk_name_test_testid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "products_productid_seq"),
    ExpectedGrant("migration_role", "USAGE", "SEQUENCE", "seq_ordernumber"),
]

# ---------------------------------------------------------------------------
# Row counts
# ---------------------------------------------------------------------------
ROW_COUNTS: dict[str, int] = {
    "customers": 5,
    "orderaudit": 5,
    "orderdetails": 8,
    "orders": 5,
    "partitionedorders": 3,
    "pk_name_test": 2,
    "products": 5,
}

# ---------------------------------------------------------------------------
# Comments / descriptions
# ---------------------------------------------------------------------------
COMMENT_COUNT = 8
