"""Expected state of the MSSQL E2E source fixture.

Every value here is derived directly from the SQL scripts in
``tests/e2e/mssql/fixtures/`` so the validator and fixture stay in sync.
If you change a script, update this module too.

The class is intentionally plain data — no database access.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpectedColumn:
    name: str
    base_type: str
    nullable: bool
    is_identity: bool = False
    is_computed: bool = False
    size: int | None = None
    precision: int | None = None
    scale: int | None = None
    udt_name: str | None = None


@dataclass(frozen=True)
class ExpectedTable:
    name: str
    columns: list[ExpectedColumn]
    pk_name: str | None
    pk_columns: list[str]
    row_count: int


@dataclass(frozen=True)
class ExpectedConstraint:
    name: str
    kind: str  # 'PK', 'FK', 'UNIQUE', 'CHECK', 'DEFAULT'


@dataclass(frozen=True)
class ExpectedIndex:
    name: str
    unique: bool
    columns: list[str]


@dataclass(frozen=True)
class ExpectedTrigger:
    name: str
    table: str
    is_disabled: bool


@dataclass(frozen=True)
class ExpectedSequence:
    name: str
    data_type: str
    start_value: int


@dataclass(frozen=True)
class ExpectedSynonym:
    name: str
    base_object: str


@dataclass(frozen=True)
class ExpectedUDT:
    name: str
    base_type: str
    nullable: bool


@dataclass(frozen=True)
class ExpectedExtProp:
    object_type: str  # SCHEMA, TABLE, COLUMN, VIEW, FUNCTION, PROCEDURE
    object_name: str
    property_name: str = "MS_Description"


@dataclass(frozen=True)
class ExpectedGrant:
    privilege: str
    object_type: str
    object_name: str
    grantee: str


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_NAME = "training"

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
TABLES: dict[str, ExpectedTable] = {
    "Customers": ExpectedTable(
        name="Customers",
        columns=[
            ExpectedColumn("CustomerID", "int", False, is_identity=True),
            ExpectedColumn("CustomerCode", "varchar", False, udt_name="training.CustomerCode"),
            ExpectedColumn("FullName", "nvarchar", False, size=100),
            ExpectedColumn("Email", "varchar", False, size=150),
            ExpectedColumn("Phone", "varchar", True, size=20),
            ExpectedColumn("City", "varchar", True, size=100),
            ExpectedColumn("Status", "varchar", False, size=20),
            ExpectedColumn("IsActive", "bit", False),
            ExpectedColumn("CreatedAt", "datetime2", False),
        ],
        pk_name="PK_Customers",
        pk_columns=["CustomerID"],
        row_count=5,
    ),
    "Products": ExpectedTable(
        name="Products",
        columns=[
            ExpectedColumn("ProductID", "int", False, is_identity=True),
            ExpectedColumn("SKU", "varchar", False, size=50),
            ExpectedColumn("Name", "nvarchar", False, size=200),
            ExpectedColumn("Category", "varchar", False, size=100),
            ExpectedColumn("Price", "decimal", False, precision=10, scale=2),
            ExpectedColumn("StockQty", "int", False),
            ExpectedColumn("IsActive", "bit", False),
            ExpectedColumn("CreatedAt", "datetime2", False),
        ],
        pk_name="PK_Products",
        pk_columns=["ProductID"],
        row_count=5,
    ),
    "Orders": ExpectedTable(
        name="Orders",
        columns=[
            ExpectedColumn("OrderID", "int", False, is_identity=True),
            ExpectedColumn("OrderNumber", "bigint", False),
            ExpectedColumn("CustomerID", "int", False),
            ExpectedColumn("TotalAmount", "decimal", True, precision=10, scale=2),
            ExpectedColumn("Status", "varchar", False, size=50),
            ExpectedColumn("OrderDate", "datetime2", False),
            ExpectedColumn("ModifiedAt", "datetime2", True, is_computed=True),
        ],
        pk_name="PK_Orders",
        pk_columns=["OrderID"],
        row_count=5,
    ),
    "OrderDetails": ExpectedTable(
        name="OrderDetails",
        columns=[
            ExpectedColumn("DetailID", "int", False, is_identity=True),
            ExpectedColumn("OrderID", "int", False),
            ExpectedColumn("ProductID", "int", False),
            ExpectedColumn("Quantity", "int", False),
            ExpectedColumn("UnitPrice", "decimal", False, precision=10, scale=2),
            ExpectedColumn("LineTotal", "decimal", True, is_computed=True),
        ],
        pk_name="PK_OrderDetails",
        pk_columns=["DetailID"],
        row_count=8,
    ),
    "OrderAudit": ExpectedTable(
        name="OrderAudit",
        columns=[
            ExpectedColumn("AuditID", "int", False, is_identity=True),
            ExpectedColumn("OrderID", "int", False),
            ExpectedColumn("Action", "varchar", False, size=50),
            ExpectedColumn("AuditTimestamp", "datetime2", False),
        ],
        pk_name="PK_OrderAudit",
        pk_columns=["AuditID"],
        row_count=5,
    ),
    "PK_Name_Test": ExpectedTable(
        name="PK_Name_Test",
        columns=[
            ExpectedColumn("TestID", "int", False, is_identity=True),
            ExpectedColumn("TestName", "varchar", False, size=100),
        ],
        pk_name="PK_TestPrimaryKey",
        pk_columns=["TestID"],
        row_count=2,
    ),
    "PartitionedOrders": ExpectedTable(
        name="PartitionedOrders",
        columns=[
            ExpectedColumn("PartitionID", "int", False, is_identity=True),
            ExpectedColumn("OrderDate", "date", False),
            ExpectedColumn("Amount", "decimal", False, precision=10, scale=2),
            ExpectedColumn("Status", "varchar", False, size=50),
        ],
        pk_name="PK_PartitionedOrders",
        pk_columns=["PartitionID", "OrderDate"],
        row_count=3,
    ),
}

# ---------------------------------------------------------------------------
# Foreign keys: table -> list of (constraint_name, parent_cols, ref_table, ref_cols)
# ---------------------------------------------------------------------------
FOREIGN_KEYS: dict[str, list[tuple[str, list[str], str, list[str]]]] = {
    "Orders": [
        ("FK_Orders_Customers", ["CustomerID"], "Customers", ["CustomerID"]),
    ],
    "OrderDetails": [
        ("FK_OrderDetails_Orders", ["OrderID"], "Orders", ["OrderID"]),
        ("FK_OrderDetails_Products", ["ProductID"], "Products", ["ProductID"]),
    ],
}

# ---------------------------------------------------------------------------
# Unique constraints (non-PK): table -> list of (constraint_name, columns)
# ---------------------------------------------------------------------------
UNIQUE_CONSTRAINTS: dict[str, list[tuple[str, list[str]]]] = {
    "Customers": [("UQ_Customers_Email", ["Email"])],
    "Products": [("UQ_Products_SKU", ["SKU"])],
}

# ---------------------------------------------------------------------------
# Check constraints: table -> names
# ---------------------------------------------------------------------------
CHECK_CONSTRAINTS: dict[str, list[str]] = {
    "Customers": ["CK_Customers_Email", "CK_Customers_Name"],
    "Products": ["CK_Products_Price", "CK_Products_Stock"],
    "Orders": ["CK_Orders_Amount"],
    "OrderDetails": ["CK_OrderDetails_Quantity", "CK_OrderDetails_UnitPrice"],
}

# ---------------------------------------------------------------------------
# Default constraints: table -> names
# ---------------------------------------------------------------------------
DEFAULT_CONSTRAINTS: dict[str, list[str]] = {
    "Customers": ["DF_Customers_CreatedAt", "DF_Customers_IsActive", "DF_Customers_Status"],
    "Products": ["DF_Products_CreatedAt", "DF_Products_IsActive", "DF_Products_Category",
                 "DF_Products_StockQty"],
    "Orders": ["DF_Orders_OrderDate", "DF_Orders_Status"],
    "OrderAudit": ["DF_OrderAudit_AuditTimestamp"],
    "PartitionedOrders": ["DF_PartitionedOrders_Status"],
}

# ---------------------------------------------------------------------------
# Non-PK indexes: table -> list of (name, unique, columns, included)
# ---------------------------------------------------------------------------
INDEXES: dict[str, list[tuple[str, bool, list[str], list[str], bool]]] = {
    "Customers": [
        ("IX_Customers_City", False, ["City"], [], False),
    ],
    "Products": [
        ("IX_Products_Category", False, ["Category"], [], False),
    ],
    "Orders": [
        ("IX_Orders_CustomerID", False, ["CustomerID"], [], False),
    ],
    "OrderDetails": [
        ("IX_OrderDetails_ProductID", False, ["ProductID"], ["Quantity", "UnitPrice"], False),
    ],
}

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
VIEWS: list[str] = ["vw_CustomerOrders", "vw_ProductSales"]

# ---------------------------------------------------------------------------
# Functions: name -> type ('FN' scalar, 'IF' inline TVF, 'TF' table-valued)
# ---------------------------------------------------------------------------
FUNCTIONS: dict[str, str] = {
    "fn_CalculateTax": "FN",
    "fn_CustomerOrderStats": "IF",
}

# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------
PROCEDURES: list[str] = ["usp_GetCustomerOrders"]

# ---------------------------------------------------------------------------
# Triggers: name -> (table, is_disabled)
# ---------------------------------------------------------------------------
TRIGGERS: dict[str, tuple[str, bool]] = {
    "trg_Orders_Insert": ("Orders", False),
    "trg_OrderDetails_Audit": ("OrderDetails", True),
}

# ---------------------------------------------------------------------------
# Sequences: name -> (data_type, start_value)
# ---------------------------------------------------------------------------
SEQUENCES: dict[str, tuple[str, int]] = {
    "Seq_OrderNumber": ("bigint", 1000),
}

# ---------------------------------------------------------------------------
# Synonyms: name -> base_object
# ---------------------------------------------------------------------------
SYNONYMS: dict[str, str] = {
    "syn_Orders": "training.Orders",
    "syn_OrderDetails": "training.OrderDetails",
}

# ---------------------------------------------------------------------------
# User-defined types: name -> (base_type, nullable)
# ---------------------------------------------------------------------------
USER_TYPES: dict[str, tuple[str, bool]] = {
    "CustomerCode": ("varchar", False),
}

# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------
PARTITION_FUNCTION = "PF_OrderDate"
PARTITION_SCHEME = "PS_OrderDate"
PARTITIONED_TABLE = "PartitionedOrders"

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
ROLES: list[str] = ["Role_ReadOnly", "Role_DataWriter"]
USERS: list[str] = ["E2E_TestUser"]
ROLE_MEMBERSHIPS: dict[str, list[str]] = {
    "Role_ReadOnly": ["E2E_TestUser"],
}

# ---------------------------------------------------------------------------
# Extended properties count (MS_Description on schema, 5 tables, 3 columns,
# 1 view, 1 function, 1 procedure = 11... 12 total when schema is counted)
# ---------------------------------------------------------------------------
EXPECTED_EXT_PROP_COUNT = 12

# ---------------------------------------------------------------------------
# Row counts
# ---------------------------------------------------------------------------
ROW_COUNTS: dict[str, int] = {
    "Customers": 5,
    "Products": 5,
    "Orders": 5,
    "OrderDetails": 8,
    "OrderAudit": 5,
    "PK_Name_Test": 2,
    "PartitionedOrders": 3,
}
