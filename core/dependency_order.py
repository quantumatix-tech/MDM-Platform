"""Foreign-key dependency ordering for data loading.

This module provides a deterministic topological sort of tables based on
foreign-key relationships discovered from source database metadata.

The ordering ensures parent tables are loaded before child tables, handles
cross-schema references, detects cycles, and preserves the original discovery
order as a stable tie-breaker for unrelated tables.
"""
from __future__ import annotations

from typing import Any


class DependencyCycleError(ValueError):
    """Raised when a foreign-key dependency cycle prevents safe data loading."""

    def __init__(self, blocked: list[str]) -> None:
        self.blocked = blocked
        super().__init__(
            "Foreign-key dependency cycle prevents safe data loading; affected "
            f"objects (including dependents blocked by the cycle): {', '.join(blocked)}. "
            "Resolve or defer the cyclic constraints before retrying."
        )


def build_dependency_graph(
    schemas: dict[str, Any],
    object_order: list[str] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, int]]:
    """Build the foreign-key dependency graph from schema metadata.

    Args:
        schemas: Mapping from object name to Schema DTO.
        object_order: Optional initial order (discovery order) used as a tie-breaker.

    Returns:
        Tuple of (dependencies, dependents, positions):
        - dependencies[child] = set of parent object names that must be loaded first
        - dependents[parent] = set of child object names that depend on this parent
        - positions[object] = original position in object_order (for stable sorting)
    """
    order = list(object_order) if object_order is not None else list(schemas)
    order = [name for name in order if name in schemas]
    order.extend(name for name in schemas if name not in order)
    positions = {name: index for index, name in enumerate(order)}

    # Build lookup tables for resolving FK references
    # Maps (schema_name, table_name) -> object_name (case-insensitive)
    qualified_names: dict[tuple[str, str], str] = {}
    # Maps table_name -> list of object_names (for unqualified resolution)
    names_by_table: dict[str, list[str]] = {}
    for name, schema in schemas.items():
        table_name = getattr(schema, "name", name).casefold()
        schema_name = getattr(schema, "schema_name", None)
        names_by_table.setdefault(table_name, []).append(name)
        if schema_name:
            qualified_names[(schema_name.casefold(), table_name)] = name

    dependencies: dict[str, set[str]] = {name: set() for name in order}
    dependents: dict[str, set[str]] = {name: set() for name in order}

    for child, schema in schemas.items():
        child_schema = getattr(schema, "schema_name", None)
        for foreign_key in getattr(schema, "foreign_keys", ()) or ():
            referenced_table = getattr(foreign_key, "ref_table", None)
            if not referenced_table:
                continue
            referenced_schema = getattr(foreign_key, "ref_schema", None) or child_schema
            parent = None
            if referenced_schema:
                parent = qualified_names.get(
                    (referenced_schema.casefold(), referenced_table.casefold())
                )
            if parent is None:
                candidates = names_by_table.get(referenced_table.casefold(), [])
                if len(candidates) == 1:
                    parent = candidates[0]
            if parent is not None and parent != child:
                dependencies[child].add(parent)
                dependents[parent].add(child)

    return dependencies, dependents, positions


def topological_sort(
    dependencies: dict[str, set[str]],
    dependents: dict[str, set[str]],
    positions: dict[str, int],
) -> list[str]:
    """Perform Kahn's topological sort with stable tie-breaking.

    Args:
        dependencies: child -> set of parents
        dependents: parent -> set of children
        positions: object -> original position for tie-breaking

    Returns:
        Ordered list of object names.

    Raises:
        DependencyCycleError: If a cycle is detected.
    """
    # Initialize ready queue with nodes that have no dependencies
    ready = [name for name in positions if not dependencies[name]]
    # Sort ready queue by original position for determinism
    ready.sort(key=positions.__getitem__)

    result: list[str] = []

    while ready:
        current = ready.pop(0)
        result.append(current)

        # Process dependents in original order for determinism
        for dependent in sorted(dependents[current], key=positions.__getitem__):
            dependencies[dependent].discard(current)
            if not dependencies[dependent] and dependent not in result and dependent not in ready:
                # Insert at position that preserves original ordering
                insertion_point = next(
                    (i for i, candidate in enumerate(ready)
                     if positions[candidate] > positions[dependent]),
                    len(ready),
                )
                ready.insert(insertion_point, dependent)

    if len(result) != len(positions):
        blocked = [name for name in positions if name not in result]
        raise DependencyCycleError(blocked)

    return result


def order_data_load_objects(
    schemas: dict[str, Any],
    object_order: list[str] | None = None,
) -> list[str]:
    """Order data-load objects using their Schema FK metadata.

    This is intentionally scoped to data loading: table creation and the
    later constraint/object phases retain their existing ordering. The
    discovery order is the stable tie-breaker for unrelated tables.

    Args:
        schemas: Mapping from object name to Schema DTO (with foreign_keys).
        object_order: Optional initial order (discovery order) used as a tie-breaker.

    Returns:
        List of object names in dependency-safe loading order.

    Raises:
        DependencyCycleError: If a cycle is detected.
    """
    dependencies, dependents, positions = build_dependency_graph(schemas, object_order)
    return topological_sort(dependencies, dependents, positions)