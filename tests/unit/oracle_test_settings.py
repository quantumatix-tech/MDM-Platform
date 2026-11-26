"""Shared YAML-backed settings for Oracle unit tests."""
from __future__ import annotations

import copy
import secrets
from pathlib import Path
from typing import Any

import yaml

from core.secrets import create_secret_provider

_ORACLE_TEST_CONFIG: dict[str, Any] = {
    "source": {
        "engine": "oracle",
        "secret_provider": "env",
        "connection": {
            "host": "localhost",
            "port": 1521,
            "service_name": "FREEPDB1",
            "username": "MIGRATION_E2E_SOURCE",
            "password_secret": "oracle_source_test",
        },
    },
    "target": {
        "engine": "oracle",
        "secret_provider": "env",
        "connection": {
            "host": "localhost",
            "port": 1521,
            "service_name": "FREEPDB1",
            "username": "MIGRATION_E2E_TARGET",
            "password_secret": "oracle_target_test",
        },
    },
    "secrets": {"provider": "env"},
    "e2e": {
        "engine": "oracle",
        "oracle_test_owner_prefix": "MIGRATION_E2E_",
        "fixture_path": "tests/e2e/oracle/fixtures",
        "resources": {
            "source": {
                "kind": "schema",
                "name": "MIGRATION_E2E_SOURCE",
                "safe_pattern": r"^MIGRATION_E2E_[A-Z0-9_]+$",
            },
            "target": {
                "kind": "schema",
                "name": "MIGRATION_E2E_TARGET",
                "safe_pattern": r"^MIGRATION_E2E_[A-Z0-9_]+$",
            },
        },
        "fixtures": [
            {
                "file": "01_create_sample.sql",
                "sql": "CREATE TABLE MIGRATION_E2E_SAMPLE (ID NUMBER PRIMARY KEY)",
            },
            {
                "file": "02_seed_sample.sql",
                "sql": "INSERT INTO MIGRATION_E2E_SAMPLE VALUES (1)",
            },
        ],
        "expected": {
            "objects": ["MIGRATION_E2E_SAMPLE"],
            "row_counts": {"MIGRATION_E2E_SAMPLE": 1},
        },
    },
}


def load_oracle_test_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Return an isolated test config, or load a test's temporary YAML file."""
    if config_path is None:
        return copy.deepcopy(_ORACLE_TEST_CONFIG)
    with Path(config_path).open(encoding="utf-8") as stream:
        return copy.deepcopy(yaml.safe_load(stream))


def seed_oracle_test_secrets(config: dict[str, Any], monkeypatch) -> dict[str, str]:
    """Seed environment-provider test values for configured secret references."""
    values: dict[str, str] = {}
    for side in ("source", "target"):
        secret_ref = config[side]["connection"]["password_secret"]
        value = secrets.token_urlsafe(24)
        values[side] = value
        monkeypatch.setenv(f"SECRET_{secret_ref}", value)
    return values


def resolve_oracle_test_connection(
    config: dict[str, Any], side: str
) -> dict[str, Any]:
    """Resolve the configured password reference through the normal provider."""
    endpoint = copy.deepcopy(config[side])
    resolver = create_secret_provider(config, endpoint.get("secret_provider"))
    connection = endpoint["connection"]
    connection["password"] = resolver.resolve(connection["password_secret"])
    return connection
