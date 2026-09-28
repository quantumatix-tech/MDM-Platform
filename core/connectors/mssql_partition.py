"""MSSQL Partition Function/Scheme definitions for Step 12.

Compatibility shim.  The canonical definitions now live in
:mod:`core.connectors.mssql._models`.  This module re-exports them so that
existing ``from core.connectors.mssql_partition import PartitionFunctionDef``
imports keep resolving to the same class objects as
``from core.connectors.mssql import PartitionFunctionDef``.
"""
from __future__ import annotations

from core.connectors.mssql._models import (
    PartitionFunctionDef,
    PartitionSchemeDef,
    PartitionedTableDef,
)

__all__ = [
    "PartitionFunctionDef",
    "PartitionSchemeDef",
    "PartitionedTableDef",
]
