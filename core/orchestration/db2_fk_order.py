from __future__ import annotations


def order_tables_for_full(selected_tables, foreign_keys):
    """Return (insert_order, delete_order) for selected schema-qualified tables.

    foreign_keys: list of (child, parent) tuples.
    """
    tables = list(selected_tables)
    table_set = set(tables)
    if len(table_set) != len(tables):
        raise ValueError("Duplicate selected table identifiers")
    # adjacency: child depends on parent
    deps = {t: set() for t in tables}
    for child, parent in foreign_keys:
        if child not in table_set:
            raise ValueError(f"Unknown child table in foreign key: {child}")
        if parent not in table_set:
            raise ValueError(f"Missing selected parent table in foreign key: {parent}")
        if child == parent:
            raise ValueError(f"Self-referencing foreign key is not supported: {child}")
        deps[child].add(parent)
    # Kahn's algorithm (parents first)
    remaining = set(tables)
    insert_order = []
    # in-degree = number of unprocessed parents
    while remaining:
        ready = [t for t in remaining if not (deps[t] & remaining)]
        if not ready:
            raise ValueError("Foreign key dependency cycle detected")
        ready.sort()
        for t in ready:
            insert_order.append(t)
            remaining.discard(t)
    delete_order = list(reversed(insert_order))
    return insert_order, delete_order
