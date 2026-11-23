from typing import Any

from core.connectors.base import (
    ApplyResult,
    CDCEngine,
    ChangeEvent,
    Column,
    PartitionDef,
    Schema,
    SequenceDef,
    SourceConnector,
    TargetConnector,
    UpsertResult,
    validate_identifier,
)
from core.connectors.cosmos_mongo import (
    CosmosMongoCDCEngine,
    CosmosMongoTargetConnector,
)
from core.connectors.mongodb import (
    MongoCDCEngine,
    MongoSourceConnector,
    MongoTargetConnector,
)
from core.connectors.mssql import (
    MSSQLCDCEngine,
    MSSQLSourceConnector,
    MSSQLTargetConnector,
)
from core.connectors.mysql import (
    MySQLCDCEngine,
    MySQLSourceConnector,
    MySQLTargetConnector,
)
from core.connectors.oracle import OracleSourceConnector, OracleTargetConnector
from core.connectors.postgresql import (
    PostgresCDCEngine,
    PostgresSourceConnector,
    PostgresTargetConnector,
)

__all__ = [
    "ApplyResult",
    "CDCEngine",
    "ChangeEvent",
    "Column",
    "CosmosMongoCDCEngine",
    "CosmosMongoTargetConnector",
    "MSSQLCDCEngine",
    "MSSQLSourceConnector",
    "MSSQLTargetConnector",
    "MongoCDCEngine",
    "MongoSourceConnector",
    "MongoTargetConnector",
    "MySQLCDCEngine",
    "MySQLSourceConnector",
    "MySQLTargetConnector",
    "OracleSourceConnector",
    "OracleTargetConnector",
    "PartitionDef",
    "PostgresCDCEngine",
    "PostgresSourceConnector",
    "PostgresTargetConnector",
    "Schema",
    "SequenceDef",
    "SourceConnector",
    "TargetConnector",
    "UpsertResult",
    "create_cdc_engine",
    "validate_identifier",
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
