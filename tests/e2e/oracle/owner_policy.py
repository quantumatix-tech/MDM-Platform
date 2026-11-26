"""Fail-closed validation for Oracle acceptance schema owners.

The policy is intentionally pure. It validates configured resources and does
not connect to Oracle or issue SQL.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from tests.e2e.acceptance import E2EConfig, E2EResource

_ORACLE_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}\Z")
_PROTECTED_OWNERS = frozenset(
    {
        "ANONYMOUS",
        "APEX_PUBLIC_USER",
        "APPQOSSYS",
        "AUDSYS",
        "CTXSYS",
        "DBSNMP",
        "DIP",
        "DVSYS",
        "GGSYS",
        "LBACSYS",
        "MDSYS",
        "OJVMSYS",
        "OLAPSYS",
        "ORACLE_OCM",
        "ORDDATA",
        "ORDPLUGINS",
        "ORDSYS",
        "OUTLN",
        "PDBADMIN",
        "REMOTE_SCHEDULER_AGENT",
        "SYS",
        "SYSTEM",
        "WMSYS",
        "XDB",
        "XS$NULL",
    }
)


def is_valid_oracle_identifier(value: object) -> bool:
    """Return whether value is a valid unquoted Oracle identifier."""
    return isinstance(value, str) and _ORACLE_IDENTIFIER.fullmatch(value) is not None


def validate_oracle_owner_resource(
    resource: E2EResource,
    *,
    side: str,
    test_owner_prefix: str,
    service_name: str | None = None,
    allow_exact_target_owner: bool = False,
    allow_exact_source_owner: bool = False,
) -> str:
    """Validate one schema-owner resource and return its owner name.

    Anchored test-prefix patterns are the default. A target may opt into one
    explicitly configured exact-owner pattern for legacy acceptance fixtures.
    This function deliberately does not authorize or perform destructive work.
    """
    label = f"Oracle {side} owner"
    if side not in {"source", "target"}:
        raise ValueError("side must be 'source' or 'target'")
    if resource.kind != "schema":
        raise ValueError(f"{label} resource must have kind 'schema'")
    if not isinstance(test_owner_prefix, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_$#]*_", test_owner_prefix
    ):
        raise ValueError("e2e.oracle_test_owner_prefix must be a valid test-only owner prefix")

    name = resource.name
    if not is_valid_oracle_identifier(name):
        raise ValueError(f"{label} {name!r} is not a valid unquoted Oracle identifier")

    normalized = name.upper()
    if service_name and normalized == service_name.upper():
        raise ValueError(f"{label} must not equal the Oracle service/PDB name")
    if normalized in _PROTECTED_OWNERS or normalized.startswith("SYS"):
        raise ValueError(f"{label} {name!r} is a protected Oracle account")

    pattern = resource.safe_pattern
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{label} requires an explicit test-only safe_pattern")
    if allow_exact_target_owner or allow_exact_source_owner:
        if side == "target" and not allow_exact_target_owner:
            raise ValueError(f"{label} exact-owner approval is valid only for the target")
        if side == "source" and not allow_exact_source_owner:
            raise ValueError(
                f"{label} exact-owner approval is valid only for preloaded SOURCE mode"
            )
        if pattern != f"^{re.escape(name)}$":
            raise ValueError(
                f"{label} safe_pattern must exactly match the configured owner"
            )
    elif not pattern.startswith(f"^{re.escape(test_owner_prefix)}") or not pattern.endswith("$"):
        raise ValueError(
            f"{label} safe_pattern must be anchored and start with "
            f"^{re.escape(test_owner_prefix)}"
        )
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{label} safe_pattern is malformed") from exc
    if compiled.fullmatch(name) is None:
        raise ValueError(f"{label} {name!r} does not match its test-only safe_pattern")
    return name


def validate_oracle_owner_resources(config: E2EConfig) -> dict[str, str]:
    """Validate source and target owner resources independently."""
    e2e_config = config.raw.get("e2e", {})
    test_owner_prefix = e2e_config.get("oracle_test_owner_prefix")
    source_mode = e2e_config.get("source_mode", "fixtures")
    if source_mode not in {"fixtures", "preloaded"}:
        raise ValueError("e2e.source_mode must be 'fixtures' or 'preloaded'")
    raw_resources = e2e_config.get("resources", {})
    validated: dict[str, str] = {}
    for side in ("source", "target"):
        resource = config.source_resource if side == "source" else config.target_resource
        raw_resource = raw_resources.get(side, {}) if isinstance(raw_resources, Mapping) else {}
        exact_owner_approval = raw_resource.get("allow_exact_owner", False)
        if not isinstance(exact_owner_approval, bool):
            raise ValueError(f"e2e.resources.{side}.allow_exact_owner must be a boolean")
        if side == "source" and exact_owner_approval and source_mode != "preloaded":
            raise ValueError(
                "e2e.resources.source.allow_exact_owner is allowed only when e2e.source_mode is 'preloaded'"
            )
        endpoint = config.raw.get(side, {})
        connection = endpoint.get("connection", {}) if isinstance(endpoint, Mapping) else {}
        service_name = connection.get("service_name") if isinstance(connection, Mapping) else None
        validated[side] = validate_oracle_owner_resource(
            resource,
            side=side,
            test_owner_prefix=test_owner_prefix,
            service_name=service_name if isinstance(service_name, str) else None,
            allow_exact_target_owner=(side == "target" and exact_owner_approval),
            allow_exact_source_owner=(side == "source" and exact_owner_approval),
        )
    return validated
