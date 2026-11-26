"""Oracle object-wise discovery, validation, and application helpers.

Functions are re-exported here to retain the historical
``core.connectors.oracle.objects`` import surface.
"""

from __future__ import annotations

from core.connectors.oracle.objects import (
    comment,
    function,
    partition,
    security,
    sequence,
    synonym,
    trigger,
    type,
    view,
)
from core.connectors.oracle.objects._common import (
    _SYSTEM_OWNERS,
    _assert_no_unmanaged_dependents,
    _assert_target_type,
    _check_compile_status,
    _check_local_dependencies,
    _dependency_order,
    _get_ddl,
    _mask_literals_and_comments,
    _normalize_materialized_view_query,
    _remap_owner_qualifiers,
    _strip_trigger_metadata_state,
    _validate_single_create,
    _value,
    managed_source_objects,
)
from core.connectors.oracle.objects.comment import (
    discover_comments,
)
from core.connectors.oracle.objects.function import (
    create_function,
    discover_routines,
)
from core.connectors.oracle.objects.partition import (
    _validate_partition_bound_expression,
    discover_table_partitioning,
)
from core.connectors.oracle.objects.security import (
    _DIRECT_OWNER_PRIVILEGES,
    ORACLE_BOOTSTRAP_PRIVILEGES,
    inspect_privileges,
    required_owner_privileges,
    validate_privileges,
)
from core.connectors.oracle.objects.sequence import (
    advance_sequence,
    create_sequence,
    discover_sequences,
    restore_standalone_sequence_state,
    sequence_name,
)
from core.connectors.oracle.objects.synonym import (
    discover_synonyms,
)
from core.connectors.oracle.objects.trigger import (
    create_trigger,
    discover_triggers,
    suspend_triggers_for_data_load,
)
from core.connectors.oracle.objects.type import (
    create_type,
    discover_types,
    hydrate_object,
    serialize_object,
)
from core.connectors.oracle.objects.view import (
    _filter_view_order_to_tables,
    create_materialized_view,
    create_view,
    discover_views,
    refresh_materialized_view,
)

__all__ = [
    "ORACLE_BOOTSTRAP_PRIVILEGES",
    "_DIRECT_OWNER_PRIVILEGES",
    "_SYSTEM_OWNERS",
    "_assert_no_unmanaged_dependents",
    "_assert_target_type",
    "_check_compile_status",
    "_check_local_dependencies",
    "_dependency_order",
    "_filter_view_order_to_tables",
    "_get_ddl",
    "_mask_literals_and_comments",
    "_normalize_materialized_view_query",
    "_remap_owner_qualifiers",
    "_strip_trigger_metadata_state",
    "_validate_partition_bound_expression",
    "_validate_single_create",
    "_value",
    "advance_sequence",
    "comment",
    "create_function",
    "create_materialized_view",
    "create_sequence",
    "create_trigger",
    "create_type",
    "create_view",
    "discover_comments",
    "discover_routines",
    "discover_sequences",
    "discover_synonyms",
    "discover_table_partitioning",
    "discover_triggers",
    "discover_types",
    "discover_views",
    "function",
    "hydrate_object",
    "inspect_privileges",
    "managed_source_objects",
    "partition",
    "refresh_materialized_view",
    "required_owner_privileges",
    "restore_standalone_sequence_state",
    "security",
    "sequence",
    "sequence_name",
    "serialize_object",
    "suspend_triggers_for_data_load",
    "synonym",
    "trigger",
    "type",
    "validate_privileges",
    "view",
]
