"""Shared YAML parsing and connection setup for Oracle E2E runners."""

from __future__ import annotations

from typing import Any

from core.connectors.oracle._models import (
    OracleConnectionError,
    make_connect_kwargs,
    safe_connection_error_message,
)
from core.secrets import create_secret_provider
from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle.owner_policy import (
    is_valid_oracle_identifier,
    validate_oracle_owner_resources,
)


class OracleE2EError(RuntimeError):
    """A deliberately sanitized, user-facing Oracle acceptance failure."""


def validated_oracle_context(cfg: E2EConfig) -> tuple[dict[str, str], dict[str, str]]:
    """Return independently validated owners and endpoint service names."""
    owners = validate_oracle_owner_resources(cfg)
    if owners["source"].casefold() == owners["target"].casefold():
        raise ValueError("Oracle source and target schema owners must be distinct")
    services: dict[str, str] = {}
    for side in ("source", "target"):
        endpoint = cfg.raw.get(side, {})
        connection = endpoint.get("connection", {}) if isinstance(endpoint, dict) else {}
        service_name = connection.get("service_name") if isinstance(connection, dict) else None
        if not isinstance(service_name, str) or not service_name.strip():
            raise ValueError(f"Oracle {side} service_name is required")
        services[side] = service_name.strip()
    return owners, services


def secret_provider_for_side(
    config: dict[str, Any], side: str, provider_override: str | None = None
) -> str:
    """Return the provider selected by the shared orchestrator policy."""
    endpoint = config.get(side, {})
    endpoint_provider = endpoint.get("secret_provider") if isinstance(endpoint, dict) else None
    if provider_override is not None:
        return provider_override
    if endpoint_provider is not None:
        return str(endpoint_provider)
    secrets_config = config.get("secrets", {})
    if not isinstance(secrets_config, dict):
        raise TypeError("secrets configuration must be a mapping")
    return str(secrets_config.get("provider", "env"))


def resolver_for_side(config: dict[str, Any], side: str, provider_override: str | None = None):
    """Build the same endpoint secret resolver policy used by the orchestrator."""
    provider_name = secret_provider_for_side(config, side, provider_override)
    return create_secret_provider(config, provider_name)


def connection_config(cfg: E2EConfig, side: str, owner: str, resolver: Any) -> dict[str, Any]:
    endpoint = cfg.raw.get(side)
    if (
        not isinstance(endpoint, dict)
        or str(endpoint.get("engine", "oracle")).casefold() != "oracle"
    ):
        raise ValueError(f"Oracle {side} endpoint must use engine 'oracle'")
    conn = endpoint.get("connection")
    if not isinstance(conn, dict):
        raise TypeError(f"Oracle {side} connection configuration is missing")
    host = conn.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"Oracle {side} connection host must be configured")
    if "port" not in conn:
        raise ValueError(f"Oracle {side} connection port must be configured")
    try:
        port = int(conn["port"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Oracle {side} connection port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Oracle {side} connection port must be between 1 and 65535")
    username = conn.get("username")
    if not isinstance(username, str) or username.upper() != owner.upper():
        raise ValueError(f"Oracle {side} username must match its validated schema owner")
    secret_ref = conn.get("password_secret")
    if not isinstance(secret_ref, str) or not secret_ref:
        raise ValueError(f"Oracle {side} password_secret is required")
    try:
        password = resolver.resolve(secret_ref) if resolver is not None else None
    except Exception:  # noqa: BLE001 - provider errors may expose secret details
        raise OracleE2EError(f"Oracle {side} credential could not be resolved") from None
    if not isinstance(password, str) or not password:
        raise OracleE2EError(f"Oracle {side} credential is missing")
    return {
        **conn,
        "host": host.strip(),
        "port": port,
        "username": username,
        "service_name": conn.get("service_name"),
        "password": password,
    }


def connect(config: dict[str, Any], driver: Any):
    """Connect using the configured service and already-resolved credentials."""
    kwargs = make_connect_kwargs(config, driver)
    try:
        return driver.connect(**kwargs)
    except Exception as exc:
        driver_error = getattr(driver, "Error", ())
        if isinstance(driver_error, type) and isinstance(exc, driver_error):
            raise OracleConnectionError(safe_connection_error_message(exc)) from None
        raise


def fixture_names_and_expected_tables(
    cfg: E2EConfig, *, allow_source_discovery: bool = False
) -> tuple[list[str], list[str]]:
    """Return fixture and optional expected table names.

    Preloaded schema-wide acceptance can omit expected table names entirely;
    the setup/validation runners then discover them from the connected owner.
    """
    e2e = cfg.raw.get("e2e", {})
    raw_fixtures = e2e.get("fixtures")
    source_mode = e2e.get("source_mode", "fixtures")
    if source_mode not in {"fixtures", "preloaded"}:
        raise ValueError("e2e.source_mode must be 'fixtures' or 'preloaded'")
    if source_mode == "fixtures" and (not isinstance(raw_fixtures, list) or not raw_fixtures):
        raise ValueError("e2e.fixtures must list at least one configured SQL file")
    if source_mode == "preloaded" and raw_fixtures not in (None, []):
        raise ValueError("e2e.fixtures must be omitted when e2e.source_mode is 'preloaded'")
    fixture_names: list[str] = []
    for item in raw_fixtures or []:
        name = (
            item if isinstance(item, str) else item.get("file") if isinstance(item, dict) else None
        )
        if not isinstance(name, str) or not name:
            raise ValueError("Each e2e.fixtures entry must be a SQL filename")
        fixture_names.append(name)

    expected = e2e.get("expected", {})
    tables = expected.get("objects") if isinstance(expected, dict) else None
    if allow_source_discovery and source_mode == "preloaded" and tables is None:
        return fixture_names, []
    if (
        not isinstance(tables, list)
        or not tables
        or any(not is_valid_oracle_identifier(name) for name in tables)
        or len({name.upper() for name in tables}) != len(tables)
    ):
        raise ValueError("e2e.expected.objects must list unique valid Oracle table names")
    return fixture_names, tables


def configured_source_table_allowlist(
    cfg: E2EConfig, expected_tables: list[str]
) -> list[str] | None:
    """Validate an optional source include list; None means schema-wide scope."""
    connection = cfg.raw.get("source", {}).get("connection", {})
    raw_tables = connection.get("include_tables") if isinstance(connection, dict) else None
    if raw_tables is None:
        return None
    if not isinstance(raw_tables, list) or not raw_tables:
        raise ValueError("source.connection.include_tables must be a non-empty list")
    if any(not is_valid_oracle_identifier(name) for name in raw_tables):
        raise ValueError("source.connection.include_tables contains an invalid Oracle table name")
    normalized = [name.upper() for name in raw_tables]
    if len(set(normalized)) != len(normalized):
        raise ValueError("source.connection.include_tables contains duplicate table names")
    expected_normalized = {name.upper() for name in expected_tables}
    if expected_normalized and set(normalized) != expected_normalized:
        raise ValueError("source.connection.include_tables must match e2e.expected.objects")
    if not expected_tables:
        return normalized
    configured_names = {name.upper(): name for name in expected_tables}
    return [configured_names[name] for name in normalized]


def expected_row_counts(cfg: E2EConfig, tables: list[str]) -> dict[str, int]:
    expected = cfg.raw.get("e2e", {}).get("expected", {})
    raw_counts = expected.get("row_counts") if isinstance(expected, dict) else None
    if not isinstance(raw_counts, dict) or not raw_counts:
        raise ValueError("e2e.expected.row_counts must configure a row count for each table")
    normalized = {table.upper(): table for table in tables}
    counts: dict[str, int] = {}
    for name, count in raw_counts.items():
        if not is_valid_oracle_identifier(name) or name.upper() not in normalized:
            raise ValueError("e2e.expected.row_counts keys must be configured expected tables")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("e2e.expected.row_counts values must be non-negative integers")
        counts[normalized[name.upper()]] = count
    if {name.upper() for name in counts} != set(normalized):
        raise ValueError("e2e.expected.row_counts must cover every configured expected table")
    return counts


def expected_foreign_key_relations(cfg: E2EConfig, tables: list[str]) -> list[dict[str, str]]:
    """Read configured parent/child table expectations without fixing column names."""
    expected = cfg.raw.get("e2e", {}).get("expected", {})
    raw_relations = expected.get("foreign_keys", []) if isinstance(expected, dict) else None
    if not isinstance(raw_relations, list):
        raise TypeError("e2e.expected.foreign_keys must be a list")
    table_names = {table.upper(): table for table in tables}
    relations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for relation in raw_relations:
        if not isinstance(relation, dict):
            raise TypeError("Each e2e.expected.foreign_keys entry must be a mapping")
        child = relation.get("table")
        parent = relation.get("referenced_table")
        if (
            not is_valid_oracle_identifier(child)
            or not is_valid_oracle_identifier(parent)
            or child.upper() not in table_names
            or parent.upper() not in table_names
        ):
            raise ValueError(
                "Expected foreign-key table names must be valid configured expected objects"
            )
        key = (child.upper(), parent.upper())
        if key in seen:
            raise ValueError("e2e.expected.foreign_keys must not contain duplicate relationships")
        seen.add(key)
        relations.append({"table": table_names[key[0]], "referenced_table": table_names[key[1]]})
    return relations
