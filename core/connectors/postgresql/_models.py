"""PostgreSQL connector internal constants and helper functions.

Holds the shared, connector-independent building blocks used by
``source.py``, ``target.py``, ``cdc.py`` and ``objects/*``:

  - connection kwargs         ``_make_conn_kwargs``
  - schema scope constants    ``DEFAULT_INCLUDE_SCHEMAS``, ``_SYSTEM_SCHEMAS``
  - schema scope resolution   ``_resolve_include_schemas``
  - identifier qualification  ``_qualify``

No dataclasses are defined here. Every object model the PostgreSQL connector
uses (``Column``, ``Schema``, ``SequenceDef``, ``TypeDef``, ``PartitionDef``,
``ViewDefinition``, ``FunctionDef``, ...) is a cross-engine contract and stays
in ``core/connectors/base.py``. This module is deliberately model-free so that
it can be imported first, with zero intra-package dependencies.

``_make_conn_kwargs`` is re-exported from the package ``__init__`` because it
is part of the connector's de-facto public surface: callers and tests import it
as ``core.connectors.postgresql._make_conn_kwargs``.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import quote_identifier


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _make_conn_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Build psycopg connection kwargs from connector config.

    Shared by the source connector, the target connector and the CDC engine so
    that TLS and keepalive behaviour cannot drift between the three.

    TLS is selected by a three-step ladder, strongest first:

      * ``verify-full`` when an ``ssl_ca_cert`` is configured (the CA bundle is
        passed through as ``sslrootcert``),
      * otherwise ``require`` whenever SSL is enabled — which is the default,
      * otherwise ``disable``.

    When ``keepalives_idle`` is configured, TCP keepalive probes are enabled.
    Long-running CDC and export connections to a managed cloud PostgreSQL (or
    to any host behind a NAT) otherwise drop silently mid-transfer.
    """
    kwargs: dict[str, Any] = {
        "host": config["host"],
        "port": config.get("port", 5432),
        "dbname": config.get("database", "postgres"),
        "user": config["username"],
        "password": config.get("password", ""),
        "sslmode": (
            "verify-full" if config.get("ssl_ca_cert")
            else "require" if config.get("ssl", True)
            else "disable"
        ),
    }
    if config.get("ssl_ca_cert"):
        kwargs["sslrootcert"] = config["ssl_ca_cert"]
    if config.get("keepalives_idle") is not None:
        kwargs["keepalives"] = 1
        kwargs["keepalives_idle"] = int(config["keepalives_idle"])
        kwargs["keepalives_interval"] = int(config.get("keepalives_interval", 10))
        kwargs["keepalives_count"] = int(config.get("keepalives_count", 5))
    return kwargs


# ---------------------------------------------------------------------------
# Schema scope
# ---------------------------------------------------------------------------
# Discovery scope: backward-compatible with every test/config/migration that
# existed before the schema-scope feature. Callers that omit include_schemas
# get exactly the original public-only behaviour.

DEFAULT_INCLUDE_SCHEMAS: tuple[str, ...] = ("public",)

# PostgreSQL internal schemas that must NEVER be enumerated as user metadata,
# regardless of what the operator puts in include_schemas. Mirrors the
# exclusion list applied by PostgresSourceConnector.list_schemas().
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog",
    "information_schema",
    "pg_toast",
})


def _resolve_include_schemas(config: dict[str, Any]) -> tuple[str, ...]:
    """
    Return the normalized, system-schema-free list of schemas that
    metadata discovery should consult.

    Precedence:
      1. ``config["include_schemas"]`` — list of strings (the
         orchestrator injects this from ``migration.include_schemas``).
      2. ``DEFAULT_INCLUDE_SCHEMAS`` ("public") when absent.

    Validation:
      * Must be a non-empty list/tuple of non-empty strings.
      * PostgreSQL internal schemas (``pg_catalog``, ``information_schema``,
        ``pg_toast``, anything starting with ``pg_``) are filtered out
        defensively so an explicit include cannot accidentally enumerate
        system objects.
    """
    raw = config.get("include_schemas")
    if raw is None:
        return DEFAULT_INCLUDE_SCHEMAS
    if not isinstance(raw, (list, tuple)) or not raw:
        return DEFAULT_INCLUDE_SCHEMAS
    cleaned: list[str] = []
    for s in raw:
        if not isinstance(s, str) or not s:
            continue
        if s in _SYSTEM_SCHEMAS or s.startswith("pg_"):
            continue
        cleaned.append(s)
    if not cleaned:
        return DEFAULT_INCLUDE_SCHEMAS
    return tuple(cleaned)


# ---------------------------------------------------------------------------
# Identifier qualification
# ---------------------------------------------------------------------------

def _qualify(schema_name: str | None, object_name: str) -> str:
    """Return the name to use in a DDL/DML statement for *object_name*.

    The ``public`` schema is deliberately returned **unqualified**. PostgreSQL
    resolves an unqualified name against ``search_path``, which puts ``public``
    first, so emitting ``"public"."orders"`` is unnecessary noise and differs
    from the SQL the connector has always produced for public objects. Every
    non-public schema is quoted on both halves, so hyphenated, spaced or
    reserved-word identifiers survive intact.

    This asymmetry is intentional and is relied upon by callers that compare
    generated DDL against expected text.
    """
    if not schema_name or schema_name == "public":
        return object_name
    return f"{quote_identifier(schema_name)}.{quote_identifier(object_name)}"
