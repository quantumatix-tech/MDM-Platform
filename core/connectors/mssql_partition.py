"""MSSQL Partition Function/Scheme definitions for Step 12."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PartitionFunctionDef:
    """MSSQL partition function metadata."""
    name: str
    schema_name: str = "dbo"
    data_type: str = "datetime2"
    boundaries: list[Any] = field(default_factory=list)
    range_desc: str = "RANGE RIGHT"


@dataclass
class PartitionSchemeDef:
    """MSSQL partition scheme metadata."""
    name: str
    schema_name: str = "dbo"
    partition_function_name: str = ""
    filegroups: list[str] = field(default_factory=list)


@dataclass
class PartitionedTableDef:
    """MSSQL partitioned table/index metadata."""
    table_name: str
    schema_name: str = "dbo"
    index_name: str | None = None
    partition_function_name: str = ""
    partition_column: str = ""
