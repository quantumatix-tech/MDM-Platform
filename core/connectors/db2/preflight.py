from __future__ import annotations

from typing import Any


class Db2SchemaValidationError(Exception):
    pass


class Db2SchemaNotFoundError(Db2SchemaValidationError):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"SCHEMA_NOT_FOUND: Configured source schema(s) do not exist in DB2SRC: {missing}")


class Db2SchemaDuplicateError(Db2SchemaValidationError):
    def __init__(self, duplicates: list[str]):
        self.duplicates = duplicates
        super().__init__(f"SCHEMA_DUPLICATE: Duplicate schema names configured: {duplicates}")


def _normalize_schema_name(name: str) -> str:
    if name is None:
        return ""
    s = str(name).strip()
    # Preserve case as-is but normalize for comparison? Db2 is case-sensitive in some contexts;
    # compare case-insensitively for detection but report original configured names
    return s


def validate_source_schemas(conn: Any, configured_schemas: list[str]) -> dict[str, Any]:
    if configured_schemas is None:
        configured_schemas = []
    if len(configured_schemas) == 0:
        raise Db2SchemaValidationError("No source schemas configured for Db2; explicit schema selection is required")

    # Check duplicates
    seen = set()
    dups = []
    for s in configured_schemas:
        ns = _normalize_schema_name(s).upper()
        if ns in seen:
            dups.append(s)
        seen.add(ns)
    if dups:
        raise Db2SchemaDuplicateError(list(set(dups)))

    # Get existing schemas
    from core.connectors.db2.objects import table as db2_table
    existing = db2_table.discover_schemas(conn)
    existing_set = set(x.upper() for x in existing)
    missing = []
    for s in configured_schemas:
        ns = _normalize_schema_name(s).upper()
        if ns not in existing_set:
            missing.append(s)
    if missing:
        raise Db2SchemaNotFoundError(missing)

    return {
        "configured": configured_schemas,
        "existing_validated": [s for s in configured_schemas if _normalize_schema_name(s).upper() in existing_set],
    }
