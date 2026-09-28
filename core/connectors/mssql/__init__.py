"""MSSQL connector package.

Compatibility surface that preserves the historical import paths from the
former monolithic ``core/connectors/mssql.py`` module:

    from core.connectors.mssql import MSSQLSourceConnector
    from core.connectors.mssql import _qualify
    ...

The implementation lives in sub-modules:
  - ``_models``    constants, partition dataclasses, and DDL helpers
  - ``source``     ``MSSQLSourceConnector``
  - ``target``     ``MSSQLTargetConnector``
  - ``cdc``        ``MSSQLCDCEngine``

``_models`` is imported first (it has no intra-package dependencies), then the
class-bearing sub-modules so that there are no circular imports.
"""
from __future__ import annotations

from core.connectors.mssql._models import (
    _build_mssql_index_ddl,
    _mssql_column_type,
    _non_computed_column_names,
    _qualify,
    _resolve_mssql_schemas,
    _target_identity_columns,
    _variant_column_names,
    PartitionFunctionDef,
    PartitionSchemeDef,
    PartitionedTableDef,
)
from core.connectors.mssql.source import MSSQLSourceConnector
from core.connectors.mssql.target import MSSQLTargetConnector
from core.connectors.mssql.cdc import MSSQLCDCEngine

__all__ = [
    "MSSQLSourceConnector",
    "MSSQLTargetConnector",
    "MSSQLCDCEngine",
    "PartitionFunctionDef",
    "PartitionSchemeDef",
    "PartitionedTableDef",
    # internal helpers (re-exported for backwards compatibility / tests)
    "_resolve_mssql_schemas",
    "_qualify",
    "_build_mssql_index_ddl",
    "_mssql_column_type",
    "_non_computed_column_names",
    "_target_identity_columns",
    "_variant_column_names",
]
