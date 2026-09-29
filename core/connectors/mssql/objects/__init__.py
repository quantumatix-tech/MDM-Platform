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
  - function: Function/procedure discovery, DDL creation
  - sequence: Sequence discovery, DDL creation
  - synonym : Synonym discovery, DDL creation
  - type    : Type (alias) discovery, DDL creation
  - comment : Comment/extended property discovery, application
  - partition: Partition function/scheme discovery + creation, partitioned table creation
  - security: Grant/user/role/membership discovery, role/user/membership/grant creation
"""
from core.connectors.mssql.objects import table
from core.connectors.mssql.objects import view
from core.connectors.mssql.objects import trigger
from core.connectors.mssql.objects import function
from core.connectors.mssql.objects import sequence
from core.connectors.mssql.objects import synonym
from core.connectors.mssql.objects import type
from core.connectors.mssql.objects import comment
from core.connectors.mssql.objects import partition
from core.connectors.mssql.objects import security

__all__ = ["table", "view", "trigger", "function", "sequence", "synonym", "type", "comment", "partition", "security"]
