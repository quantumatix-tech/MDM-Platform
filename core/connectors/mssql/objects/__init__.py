"""MSSQL object-level sub-package.

Provides reusable, engine-specific logic for individual object types.

Each module (table, view, trigger, etc.) contains pure functions that
accept an explicit *conn* parameter, avoiding coupling to
``MSSQLSourceConnector`` / ``MSSQLTargetConnector`` and preventing
circular imports.

Currently implemented:
  - table   : Table discovery, DDL, creation, data export/upsert/delete
  - view    : View discovery, DDL creation
  - trigger : Trigger discovery, DDL creation

Future object modules (NOT yet created):
  - function / procedure
  - sequence
  - partition
  - security (roles, users, grants)
"""
from core.connectors.mssql.objects import table
from core.connectors.mssql.objects import view
from core.connectors.mssql.objects import trigger

__all__ = ["table", "view", "trigger"]
