"""MySQL connector package.

Replaces the former monolithic ``core/connectors/mysql.py`` module while
preserving every public import path::

    from core.connectors.mysql import MySQLSourceConnector
    from core.connectors.mysql import MySQLTargetConnector
    from core.connectors.mysql import MySQLCDCEngine

The implementation lives in sub-modules:

  - ``_models``  shared, engine-specific helpers, constants and the
                 DEFINER / datatype / view-rewrite helpers used by more than
                 one connector surface, plus the three public MySQL
                 exceptions
  - ``_schema``  schema inspection and ``Schema`` composition
  - ``source``   ``MySQLSourceConnector`` — source connection lifecycle and
                 the source discovery API, including ``get_schema()``
  - ``target``   ``MySQLTargetConnector`` — target connection lifecycle and
                 the target creation API, including ``ensure_database_exists()``
                 and ``apply_constraints()``
  - ``cdc``      ``MySQLCDCEngine`` — binary-log change capture
  - ``objects``  per-object-type implementations, one module each:
                 ``table``, ``view``, ``comment``, ``function``,
                 ``trigger``, ``event``, ``partition`` and ``security``

``_models`` is imported first because it has no intra-package dependencies,
then the class-bearing sub-modules, so there are no circular imports.

Object creation and discovery are delegated to ``core.connectors.mysql.objects``.
Each object module depends only on ``_models``, the cross-engine DTOs and the
standard library; none of them import ``source``, ``target`` or ``cdc``. Two
narrow lower-level dependencies exist and are intentional: ``objects/table.py``
uses ``objects/partition.py`` for the ``PARTITION BY`` clause, and both
``_schema.py`` and ``objects/table.py`` use it for partition metadata
discovery. The partition helpers ``_mysql_partition_boundary`` and
``_mysql_partition_clause`` therefore live in
``core.connectors.mysql.objects.partition`` rather than in ``_models``.

A few connector-level responsibilities are deliberately retained in ``source``
and ``target`` rather than split into per-object modules:

``get_schema()``
    A schema *composition point*: it assembles the cross-engine ``Schema``
    DTO from several metadata families (columns with generated/auto-increment
    attributes and comments, primary key, indexes, foreign keys, CHECK
    constraints, table comment, partitions) in a single pass, so it belongs
    to no single object type.

``ensure_database_exists()``
    Database bootstrap infrastructure that must complete before any schema,
    table or object deployment.

``apply_constraints()``
    A single ordered constraint layer (indexes, then foreign keys, then
    CHECK) whose families share one existence-probe / idempotency / audit /
    rollback pattern and must be applied in dependency order.

Public import path remains: ``from core.connectors.mysql import ...``

The public surface is exactly the contents of ``__all__``: the three connector
classes, the three MySQL exceptions, the shared helpers the former monolith
defined, ``ensure_driver``, and the cross-engine DTOs and ABCs that the former
monolith imported at module level. Those DTOs are re-exported unchanged from
``core.connectors.base`` — the same objects, not wrappers. ``MySQLPartitionDef``
is intentionally absent: it was a MySQL-prefixed DTO superseded by the
engine-neutral ``TablePartition``.

``ensure_driver`` is re-exported for the same backward-compatibility reason.
It was imported at module level by the former monolith. Note that ``connect()``
resolves its own module-local ``ensure_driver`` binding, so patching the
package attribute does not intercept the call; ``ensure_driver`` is a no-op
when the driver is already importable, which is why this is harmless.
"""
from core.driver_installer import ensure_driver

# Re-exported for backward compatibility with the former monolithic
# ``core/connectors/mysql.py`` module, which imported these names at module
# level and therefore made them reachable as ``core.connectors.mysql.<name>``.
# These are the very same objects as ``core.connectors.base``; no wrapper or
# subclass is introduced and no behaviour differs.  ``MySQLPartitionDef`` is
# deliberately *not* re-exported: it was a MySQL-prefixed DTO removed during
# modularization, superseded by the engine-neutral ``TablePartition``.
from core.connectors.base import (
    CDCEngine,
    ApplyResult,
    ChangeEvent,
    CheckConstraint,
    Column,
    CommentDef,
    EventDef,
    ForeignKey,
    FunctionDef,
    GrantDef,
    Index,
    Schema,
    SourceConnector,
    TargetConnector,
    TriggerDef,
    UnmappedTypeError,
    UpsertResult,
    UserDef,
    ViewDefinition,
    validate_identifier,
)

from core.connectors.mysql._models import (
    MySQLEventTimingSafetyError,
    MySQLRoutineCreationPolicyError,
    MySQLSecurityMetadataNotVisible,
    _MYSQL_ACCOUNT_PART,
    _MYSQL_DEFINER_RE,
    _connection_options,
    _default_sql,
    _mysql_definer_identity,
    _mysql_routine_policy_error,
    _mysql_set_members,
    _normalize_mysql_set_value,
    _q,
    _qaccount,
    _qname,
    _rewrite_mysql_definer,
    _rewrite_view_database_references,
)
from core.connectors.mysql.objects.security import (
    _MYSQL_SYSTEM_USERS,
    _grantee_key,
    _is_system_account,
)
from core.connectors.mysql.objects.partition import (
    _mysql_partition_boundary,
    _mysql_partition_clause,
)
from core.connectors.mysql.cdc import MySQLCDCEngine
from core.connectors.mysql.source import MySQLSourceConnector
from core.connectors.mysql.target import MySQLTargetConnector

__all__ = [
    "MySQLSourceConnector",
    "MySQLTargetConnector",
    "MySQLCDCEngine",
    "MySQLRoutineCreationPolicyError",
    "MySQLEventTimingSafetyError",
    "MySQLSecurityMetadataNotVisible",
    # cross-engine DTOs and ABCs re-exported for backward compatibility
    "CDCEngine",
    "ApplyResult",
    "ChangeEvent",
    "CheckConstraint",
    "Column",
    "CommentDef",
    "EventDef",
    "ForeignKey",
    "FunctionDef",
    "GrantDef",
    "Index",
    "Schema",
    "SourceConnector",
    "TargetConnector",
    "TriggerDef",
    "UnmappedTypeError",
    "UpsertResult",
    "UserDef",
    "ViewDefinition",
    "validate_identifier",
    # shared helpers re-exported for backward compatibility
    "_MYSQL_ACCOUNT_PART",
    "_MYSQL_DEFINER_RE",
    "_MYSQL_SYSTEM_USERS",
    "_connection_options",
    "_default_sql",
    "_grantee_key",
    "_is_system_account",
    "_mysql_definer_identity",
    "_mysql_partition_boundary",
    "_mysql_partition_clause",
    "_mysql_routine_policy_error",
    "_mysql_set_members",
    "_normalize_mysql_set_value",
    "_q",
    "_qaccount",
    "_qname",
    "_rewrite_mysql_definer",
    "_rewrite_view_database_references",
    # re-exported for backward compatibility with the former monolith
    "ensure_driver",
]
