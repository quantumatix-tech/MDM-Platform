"""MSSQL Synonym object implementation.

Reusable Synonym-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    SynonymDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _resolve_mssql_schemas,
)
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE SYNONYM OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_synonyms.
# Discovery of user synonyms from the source database.
# ============================================================================

def discover_synonyms(conn: Any, config: dict[str, Any]) -> list[SynonymDef]:
    """Discover user synonyms in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_synonyms``.
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[SynonymDef] = []
    with conn.cursor() as cur:
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT syn.name, sch.name, syn.base_object_name "
                "FROM sys.synonyms syn "
                "JOIN sys.schemas sch ON syn.schema_id = sch.schema_id "
                f"WHERE sch.name IN ({placeholders}) "
                "ORDER BY sch.name, syn.name",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT syn.name, sch.name, syn.base_object_name "
                "FROM sys.synonyms syn "
                "JOIN sys.schemas sch ON syn.schema_id = sch.schema_id "
                "WHERE sch.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "ORDER BY sch.name, syn.name"
            )
        for syn_name, syn_schema, base_object in cur.fetchall():
            validate_identifier(syn_name, "synonym")
            validate_identifier(syn_schema, "schema")
            results.append(
                SynonymDef(
                    name=syn_name,
                    schema_name=syn_schema,
                    base_object=base_object,
                )
            )
    return results


# ============================================================================
# TARGET-SIDE SYNONYM OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_synonym.
# Synonym creation on the target database.
# ============================================================================

def create_synonym(conn: Any, synonym: SynonymDef) -> None:
    """Create a synonym on the target database.

    Replaces ``MSSQLTargetConnector.create_synonym``.
    """
    validate_identifier(synonym.name, "synonym")
    schema_name = synonym.schema_name or "dbo"
    validate_identifier(schema_name, "schema")
    base_object = synonym.base_object

    with conn.cursor() as cur:
        if schema_name != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (schema_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(schema_name)}")
                audit_log(
                    phase="create_schema", status="created",
                    details={"schema": schema_name},
                )
        try:
            cur.execute(
                "SELECT 1 FROM sys.synonyms WHERE name = ? AND schema_id = SCHEMA_ID(?)",
                (synonym.name, schema_name),
            )
            if cur.fetchone() is not None:
                return
            syn_qname = f"[{schema_name}].[{synonym.name}]"
            ddl = f"CREATE SYNONYM {syn_qname} FOR {base_object}"
            cur.execute(ddl)
            conn.commit()
            audit_log(
                phase="create_synonym", status="created",
                details={"synonym": f"{schema_name}.{synonym.name}", "base_object": base_object},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_synonym", status="failed",
                details={"synonym": f"{schema_name}.{synonym.name}", "reason": str(exc)},
            )
            raise
