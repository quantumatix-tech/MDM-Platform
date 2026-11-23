"""Tests for the pure Oracle E2E schema-owner safety policy."""
from __future__ import annotations

import copy
import re

import pytest

from tests.e2e.acceptance import E2EConfig, E2EResource
from tests.e2e.oracle.owner_policy import (
    validate_oracle_owner_resource,
    validate_oracle_owner_resources,
)
from tests.unit.oracle_test_settings import load_oracle_test_config


def _config(raw: dict | None = None) -> E2EConfig:
    return E2EConfig(copy.deepcopy(raw or load_oracle_test_config()))


def test_valid_source_and_target_owners_are_returned_independently():
    raw = load_oracle_test_config()
    raw["e2e"]["resources"]["source"]["kind"] = "owner"
    cfg = E2EConfig(raw)

    assert validate_oracle_owner_resources(cfg) == {
        side: raw["e2e"]["resources"][side]["name"]
        for side in ("source", "target")
    }


def test_invalid_oracle_identifiers_are_rejected():
    config = _config()
    prefix = config.raw["e2e"]["oracle_test_owner_prefix"]
    pattern = config.raw["e2e"]["resources"]["source"]["safe_pattern"]
    invalid_names = ("", f"1{prefix}BAD", "BAD-NAME", "A" * 129, "BAD NAME")
    for name in invalid_names:
        with pytest.raises(ValueError, match="valid unquoted Oracle identifier"):
            validate_oracle_owner_resource(
                E2EResource("schema", name, pattern),
                side="source",
                test_owner_prefix=prefix,
            )


def test_service_and_protected_accounts_are_rejected():
    config = _config()
    prefix = config.raw["e2e"]["oracle_test_owner_prefix"]
    source = config.raw["e2e"]["resources"]["source"]
    service_name = config.raw["source"]["connection"]["service_name"]
    for name in (service_name, "SYS", "SYSTEM", "SYSBACKUP", "XDB", "PDBADMIN"):
        with pytest.raises(ValueError, match="service/PDB|protected Oracle account"):
            validate_oracle_owner_resource(
                E2EResource("schema", name, source["safe_pattern"]),
                side="source",
                test_owner_prefix=prefix,
                service_name=service_name,
            )


def test_missing_unanchored_broad_or_malformed_patterns_are_rejected():
    config = _config()
    prefix = config.raw["e2e"]["oracle_test_owner_prefix"]
    owner = config.raw["e2e"]["resources"]["source"]["name"]
    anchored_prefix = f"^{re.escape(prefix)}"
    patterns = (
        None,
        "",
        f"{re.escape(prefix)}[A-Z]+$",
        f"{anchored_prefix}[A-Z]+",
        r"^.+$",
        f"{anchored_prefix}[A-Z]+($",
    )
    for pattern in patterns:
        with pytest.raises(ValueError, match="safe_pattern"):
            validate_oracle_owner_resource(
                E2EResource("schema", owner, pattern),
                side="target",
                test_owner_prefix=prefix,
            )


def test_nonmatching_test_only_pattern_is_rejected():
    config = _config()
    prefix = config.raw["e2e"]["oracle_test_owner_prefix"]
    owner = config.raw["e2e"]["resources"]["source"]["name"]
    with pytest.raises(ValueError, match="does not match"):
        validate_oracle_owner_resource(
            E2EResource("schema", owner, f"^{re.escape(prefix)}NONMATCHING_OWNER$"),
            side="source",
            test_owner_prefix=prefix,
        )


def test_database_resource_is_rejected():
    config = _config()
    resource = config.raw["e2e"]["resources"]["source"]
    with pytest.raises(ValueError, match="kind 'schema'"):
        validate_oracle_owner_resource(
            E2EResource("database", resource["name"], resource["safe_pattern"]),
            side="source",
            test_owner_prefix=config.raw["e2e"]["oracle_test_owner_prefix"],
        )


def test_source_and_target_patterns_are_validated_separately():
    raw = load_oracle_test_config()
    prefix = re.escape(raw["e2e"]["oracle_test_owner_prefix"])
    raw["e2e"]["resources"]["target"]["safe_pattern"] = f"^{prefix}NONMATCHING_OWNER$"

    with pytest.raises(ValueError, match="Oracle target owner.*does not match"):
        validate_oracle_owner_resources(E2EConfig(raw))


def test_exact_approved_target_owner_is_allowed():
    raw = load_oracle_test_config()
    target = raw["e2e"]["resources"]["target"]
    target["name"] = "ORACLE_USER_TEST"
    target["safe_pattern"] = r"^ORACLE_USER_TEST$"
    target["allow_exact_owner"] = True
    raw["target"]["connection"]["username"] = "ORACLE_USER_TEST"

    assert validate_oracle_owner_resources(E2EConfig(raw)) == {
        "source": raw["e2e"]["resources"]["source"]["name"],
        "target": "ORACLE_USER_TEST",
    }


def test_existing_exact_source_owner_requires_preloaded_mode():
    raw = load_oracle_test_config()
    raw["e2e"]["source_mode"] = "preloaded"
    source = raw["e2e"]["resources"]["source"]
    source["name"] = "CLIENT_EXISTING_SOURCE"
    source["safe_pattern"] = r"^CLIENT_EXISTING_SOURCE$"
    source["allow_exact_owner"] = True
    raw["source"]["connection"]["username"] = "CLIENT_EXISTING_SOURCE"
    assert validate_oracle_owner_resources(E2EConfig(raw))["source"] == "CLIENT_EXISTING_SOURCE"

    raw["e2e"]["source_mode"] = "fixtures"
    with pytest.raises(ValueError, match="only when e2e.source_mode is 'preloaded'"):
        validate_oracle_owner_resources(E2EConfig(raw))


@pytest.mark.parametrize(
    ("name", "pattern", "side", "error"),
    [
        ("ORACLE_USER_TEST_COPY", r"^ORACLE_USER_TEST_COPY$", "target", "safe_pattern"),
        ("ORACLE_USER_TEST", r"^ORACLE_USER_.*$", "target", "exactly match"),
        ("ORACLE_USER_TEST", r"^ORACLE_USER_TEST$", "source", "safe_pattern"),
        ("SYS", r"^SYS$", "target", "protected Oracle account"),
    ],
)
def test_approved_target_exception_rejects_unsafe_owners_and_patterns(
    name, pattern, side, error
):
    config = _config()
    prefix = config.raw["e2e"]["oracle_test_owner_prefix"]

    with pytest.raises(ValueError, match=error):
        validate_oracle_owner_resource(
            E2EResource("schema", name, pattern),
            side=side,
            test_owner_prefix=prefix,
            allow_exact_target_owner=(name == "ORACLE_USER_TEST" and side == "target"),
        )


def test_test_only_prefix_and_patterns_are_configurable():
    raw = load_oracle_test_config()
    original_prefix = raw["e2e"]["oracle_test_owner_prefix"]
    prefix = f"ALT_{original_prefix}"
    original_pattern = raw["e2e"]["resources"]["source"]["safe_pattern"]
    raw["e2e"]["oracle_test_owner_prefix"] = prefix
    for side in ("source", "target"):
        resource = raw["e2e"]["resources"][side]
        suffix = resource["name"].removeprefix(original_prefix)
        owner = f"{prefix}{suffix}"
        resource["name"] = owner
        resource["safe_pattern"] = original_pattern.replace(original_prefix, prefix)
        raw[side]["connection"]["username"] = owner

    assert validate_oracle_owner_resources(E2EConfig(raw)) == {
        side: raw["e2e"]["resources"][side]["name"]
        for side in ("source", "target")
    }
