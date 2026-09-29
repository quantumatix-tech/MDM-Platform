"""PostgreSQL connector package.

This package replaces the former monolithic ``core/connectors/postgresql.py``
module and preserves its public import surface exactly, so no caller, test or
entry point had to change:

    from core.connectors.postgresql import PostgresSourceConnector
    from core.connectors.postgresql import PostgresTargetConnector
    from core.connectors.postgresql import PostgresCDCEngine
    from core.connectors.postgresql import _make_conn_kwargs
    ...

Architecture:
  - ``_models``  shared, connector-independent helpers only — connection
                 kwargs, discovery-scope constants and resolution, and
                 identifier qualification. Contains no object logic.
  - ``source``   ``PostgresSourceConnector`` — the connector-facing source
                 API. Owns the connection, the ``Schema`` DTO composition
                 point, and bootstrap discovery; delegates object discovery.
  - ``target``   ``PostgresTargetConnector`` — the connector-facing target
                 API. Owns the connection, database bootstrap, and the ordered
                 constraint application layer; delegates object application.
  - ``cdc``      ``PostgresCDCEngine`` — pgoutput logical replication. Depends
                 on the target only through the ``TargetConnector`` interface.
  - ``objects``  per-object implementations that ``source`` and ``target``
                 delegate to: ``table``, ``sequence``, ``view``, ``type``,
                 ``function``, ``security``, ``comment``, ``trigger`` and
                 ``partition``.

``_models`` is imported first (it has no intra-package dependencies), then the
class-bearing sub-modules, so there are no circular imports.
"""
from __future__ import annotations

from core.connectors.postgresql._models import (
    _make_conn_kwargs,
    _resolve_include_schemas,
    DEFAULT_INCLUDE_SCHEMAS,
)
from core.connectors.postgresql.source import PostgresSourceConnector
from core.connectors.postgresql.target import PostgresTargetConnector
from core.connectors.postgresql.cdc import PostgresCDCEngine

__all__ = [
    "PostgresSourceConnector",
    "PostgresTargetConnector",
    "PostgresCDCEngine",
    # internal helpers (re-exported for backwards compatibility / tests)
    "_make_conn_kwargs",
    "_resolve_include_schemas",
    "DEFAULT_INCLUDE_SCHEMAS",
]
