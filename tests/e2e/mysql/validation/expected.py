"""Expected state of the MySQL E2E source fixture.

Every value here is derived directly from the SQL scripts in
``tests/e2e/mysql/fixtures/`` and verified against the live
``MigrationE2E_MySQL_Source`` database.

If you change a script, update this module too.

Note: MySQL on Windows folds unquoted identifiers to lowercase
(``lower_case_table_names=1``).  All names below use lowercase to match
the catalog's lowercase output across platforms.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpectedColumn:
    name: str
    base_type: str
    nullable: bool
    is_identity: bool = False
    is_generated: bool = False
    size: int | None = None
    precision: int | None = None
    scale: int | None = None
    default_value: str | None = None
    column_key: str = ""


@dataclass(frozen=True)
class ExpectedTable:
    name: str
    columns: list[ExpectedColumn]
    pk_columns: list[str]
    row_count: int


@dataclass(frozen=True)
class ExpectedForeignKey:
    constraint_name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"


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
class ExpectedRoutine:
    name: str
    kind: str  # 'function' or 'procedure'


@dataclass(frozen=True)
class ExpectedTrigger:
    name: str
    table: str
    is_disabled: bool


@dataclass(frozen=True)
class ExpectedPartition:
    parent_table: str
    partitions: list[str]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
TABLES: dict[str, ExpectedTable] = {
    "customers": ExpectedTable(
        name="customers",
        columns=[
            ExpectedColumn("customerid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("customercode", "varchar(50)", False, column_key=""),
            ExpectedColumn("fullname", "varchar(100)", False, column_key=""),
            ExpectedColumn("email", "varchar(150)", False, column_key=""),
            ExpectedColumn("phone", "varchar(20)", True, column_key=""),
            ExpectedColumn("city", "varchar(100)", True, column_key=""),
            ExpectedColumn("status", "varchar(20)", False, default_value="'active'", column_key=""),
            ExpectedColumn("isactive", "tinyint(1)", False, default_value="1", column_key=""),
            ExpectedColumn("createdat", "datetime", False, default_value="CURRENT_TIMESTAMP", column_key=""),
        ],
        pk_columns=["customerid"],
        row_count=5,
    ),
    "products": ExpectedTable(
        name="products",
        columns=[
            ExpectedColumn("productid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("sku", "varchar(50)", False, column_key=""),
            ExpectedColumn("name", "varchar(200)", False, column_key=""),
            ExpectedColumn("category", "varchar(100)", False, default_value="'General'", column_key=""),
            ExpectedColumn("price", "decimal(10,2)", False, column_key=""),
            ExpectedColumn("stockqty", "int", False, default_value="0", column_key=""),
            ExpectedColumn("isactive", "tinyint(1)", False, default_value="1", column_key=""),
            ExpectedColumn("createdat", "datetime", False, default_value="CURRENT_TIMESTAMP", column_key=""),
        ],
        pk_columns=["productid"],
        row_count=5,
    ),
    "orders": ExpectedTable(
        name="orders",
        columns=[
            ExpectedColumn("orderid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("ordernumber", "bigint", False, column_key=""),
            ExpectedColumn("customerid", "int", False, column_key="MUL"),
            ExpectedColumn("totalamount", "decimal(10,2)", True, column_key=""),
            ExpectedColumn("orderstatus", "enum('pending','processing','shipped','delivered','cancelled')", False,
                           default_value="'pending'", column_key=""),
            ExpectedColumn("orderdate", "datetime", False, default_value="CURRENT_TIMESTAMP", column_key=""),
            ExpectedColumn("modifiedat", "datetime", True, is_generated=True, column_key=""),
        ],
        pk_columns=["orderid"],
        row_count=5,
    ),
    "orderdetails": ExpectedTable(
        name="orderdetails",
        columns=[
            ExpectedColumn("detailid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("orderid", "int", False, column_key="MUL"),
            ExpectedColumn("productid", "int", False, column_key="MUL"),
            ExpectedColumn("quantity", "int", False, column_key=""),
            ExpectedColumn("unitprice", "decimal(10,2)", False, column_key=""),
            ExpectedColumn("linetotal", "decimal(10,2)", True, is_generated=True, column_key=""),
        ],
        pk_columns=["detailid"],
        row_count=8,
    ),
    "orderaudit": ExpectedTable(
        name="orderaudit",
        columns=[
            ExpectedColumn("auditid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("orderid", "int", False, column_key=""),
            ExpectedColumn("action", "varchar(50)", False, column_key=""),
            ExpectedColumn("audittimestamp", "datetime", False, default_value="CURRENT_TIMESTAMP", column_key=""),
        ],
        pk_columns=["auditid"],
        row_count=5,
    ),
    "partitionedorders": ExpectedTable(
        name="partitionedorders",
        columns=[
            ExpectedColumn("partitionid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("orderdate", "date", False, column_key=""),
            ExpectedColumn("amount", "decimal(10,2)", False, column_key=""),
            ExpectedColumn("status", "varchar(50)", False, default_value="'pending'", column_key=""),
        ],
        pk_columns=["partitionid", "orderdate"],
        row_count=3,
    ),
    "pk_name_test": ExpectedTable(
        name="pk_name_test",
        columns=[
            ExpectedColumn("testid", "int", False, is_identity=True, column_key="PRI"),
            ExpectedColumn("testname", "varchar(100)", False, column_key=""),
        ],
        pk_columns=["testid"],
        row_count=2,
    ),
}

# ---------------------------------------------------------------------------
# Foreign keys (names lowercased by MySQL)
# ---------------------------------------------------------------------------
FOREIGN_KEYS: dict[str, list[ExpectedForeignKey]] = {
    "orders": [
        ExpectedForeignKey(
            "fk_orders_customers", ["customerid"], "customers", ["customerid"],
        ),
    ],
    "orderdetails": [
        ExpectedForeignKey(
            "fk_orderdetails_orders", ["orderid"], "orders", ["orderid"],
        ),
        ExpectedForeignKey(
            "fk_orderdetails_products", ["productid"], "products", ["productid"],
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
        ExpectedIndex("idx_orders_customerid", False, ["customerid"]),
        ExpectedIndex("idx_orders_orderdate", False, ["orderdate"]),
    ],
    "orderdetails": [
        ExpectedIndex("idx_orderdetails_orderid", False, ["orderid"]),
        ExpectedIndex("idx_orderdetails_productid", False, ["productid"]),
    ],
}

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
VIEWS: list[ExpectedView] = [ExpectedView("vw_ordersummary")]

# ---------------------------------------------------------------------------
# Functions and procedures
# ---------------------------------------------------------------------------
FUNCTIONS: dict[str, ExpectedRoutine] = {
    "fn_GetOrderTotal": ExpectedRoutine("fn_GetOrderTotal", kind="function"),
}

PROCEDURES: list[str] = ["sp_UpdateProductStock"]

# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
TRIGGERS: dict[str, ExpectedTrigger] = {
    "tr_AuditOrder": ExpectedTrigger(
        name="tr_AuditOrder", table="orders", is_disabled=False,
    ),
}

# ---------------------------------------------------------------------------
# Partitions: parent_table -> list of partition names
# ---------------------------------------------------------------------------
PARTITIONS: dict[str, ExpectedPartition] = {
    "partitionedorders": ExpectedPartition(
        parent_table="partitionedorders",
        partitions=["p2024", "p2025_h1", "p2025_h2", "p_default"],
    ),
}

# ---------------------------------------------------------------------------
# ENUM types
# ---------------------------------------------------------------------------
ENUM_TYPES: dict[str, list[str]] = {
    "orders.orderstatus": ['pending', 'processing', 'shipped', 'delivered', 'cancelled'],
}

# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
COMMENT_COUNT = 9  # 7 table comments + 2 column comments

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
