"""PostgreSQL object-level sub-package.

Provides reusable, engine-specific logic for individual object types.

Each module contains plain functions that accept an explicit ``conn``
parameter, avoiding coupling to ``PostgresSourceConnector`` /
``PostgresTargetConnector`` and preventing circular imports.

Implemented:
  - table   : table discovery, row counts, data export, DDL creation,
              upsert/delete
  - sequence: sequence discovery, creation, advancement, ownership
  - view    : view and materialized view discovery and creation
  - type    : ENUM / DOMAIN / COMPOSITE discovery and creation
  - function: function and procedure discovery and creation
  - security: RLS policy and grant discovery, grant/role application
  - comment : comment discovery and application
  - trigger : trigger discovery and creation
  - partition: partition child discovery and creation

The only object-to-object dependency is ``security -> sequence``, for the
shared ``owned_sequences()`` ownership lookup.

Cross-object constraint assembly (``Schema`` composition on the source side,
index / CHECK / foreign-key application on the target side) intentionally
stays in ``source.py`` and ``target.py`` — see those modules for the rationale.
"""
from core.connectors.postgresql.objects import table
from core.connectors.postgresql.objects import sequence
from core.connectors.postgresql.objects import view
from core.connectors.postgresql.objects import type
from core.connectors.postgresql.objects import function
from core.connectors.postgresql.objects import security
from core.connectors.postgresql.objects import comment
from core.connectors.postgresql.objects import trigger
from core.connectors.postgresql.objects import partition

__all__ = [
    "table", "sequence", "view", "type", "function",
    "security", "comment", "trigger", "partition",
]
