from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Column,
    Schema,
    PartitionDef,
    SequenceDef,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    validate_identifier,
)
from typing import Any
from core.connectors.postgresql import (
    PostgresSourceConnector,
    PostgresTargetConnector,
    PostgresCDCEngine,
)
from core.connectors.mongodb import (
    MongoSourceConnector,
    MongoTargetConnector,
    MongoCDCEngine,
)
from core.connectors.mysql import (
    MySQLSourceConnector,
    MySQLTargetConnector,
    MySQLCDCEngine,
)
from core.connectors.mssql import (
    MSSQLSourceConnector,
    MSSQLTargetConnector,
    MSSQLCDCEngine,
)
from core.connectors.cosmos_mongo import (
    CosmosMongoTargetConnector,
    CosmosMongoCDCEngine,
)
from core.connectors.db2 import (
    DB2SourceConnector,
    DB2TargetConnector,
)

__all__ = [
    "SourceConnector",
    "TargetConnector",
    "CDCEngine",
    "Column",
    "Schema",
    "PartitionDef",
    "SequenceDef",
    "UpsertResult",
    "ApplyResult",
    "ChangeEvent",
    "validate_identifier",
    "PostgresSourceConnector",
    "PostgresTargetConnector",
    "PostgresCDCEngine",
    "MongoSourceConnector",
    "MongoTargetConnector",
    "MongoCDCEngine",
    "MySQLSourceConnector",
    "MySQLTargetConnector",
    "MySQLCDCEngine",
    "MSSQLSourceConnector",
    "MSSQLTargetConnector",
    "MSSQLCDCEngine",
    "CosmosMongoTargetConnector",
    "CosmosMongoCDCEngine",
    "DB2SourceConnector",
    "DB2TargetConnector",
    "create_cdc_engine",
]

_CDC_ENGINE_MAP = {
    "postgresql": PostgresCDCEngine,
    "mongodb": MongoCDCEngine,
    "mysql": MySQLCDCEngine,
    "mssql": MSSQLCDCEngine,
    "cosmos_mongo": CosmosMongoCDCEngine,
}


def create_cdc_engine(source_type: str, config: dict[str, Any]) -> CDCEngine:
    cls = _CDC_ENGINE_MAP.get(source_type)
    if cls is None:
        raise ValueError(f"Unknown CDC source type: {source_type!r}")
    return cls(config)
