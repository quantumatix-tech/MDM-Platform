"""MySQL view object operations.

Owns view discovery and view creation. This module must not import
``source``, ``target`` or ``cdc``: it depends only on ``_models``, the
cross-engine DTOs and the standard library.

Every SQL statement, parameter order, commit boundary and the existing
source-to-target database reference rewrite are carried over unchanged
from the former ``MySQLSourceConnector.list_views`` and
``MySQLTargetConnector.create_view`` implementations.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import ViewDefinition

from core.connectors.mysql._models import (
    _q,
    _rewrite_view_database_references,
)


def list_views(conn: Any, database: str) -> list[ViewDefinition]:
    """Discover every view defined in ``database``, in catalog order."""
    with conn.cursor() as cur:
        cur.execute("SELECT TABLE_NAME,VIEW_DEFINITION FROM INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA=%s", (database,))
        return [ViewDefinition(name=n, definition=d, schema_name=database) for n, d in cur.fetchall()]


def create_view(conn: Any, database: str, view: ViewDefinition) -> None:
    """Create or replace ``view`` on the target.

    The stored definition is rewritten so that references to the source
    database are re-pointed at the target database before execution.
    """
    definition = _rewrite_view_database_references(
        view.definition,
        source_database=view.schema_name,
        target_database=database,
    )
    with conn.cursor() as cur:
        cur.execute(f"CREATE OR REPLACE VIEW {_q(view.name)} AS {definition.rstrip().rstrip(';')}")
        conn.commit()
