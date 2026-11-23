"""Shared unit-test fixtures."""
from __future__ import annotations

import pytest

from tests.unit.oracle_test_settings import load_oracle_test_config, seed_oracle_test_secrets


@pytest.fixture
def oracle_test_config():
    return load_oracle_test_config()


@pytest.fixture
def oracle_test_secrets(oracle_test_config, monkeypatch):
    return seed_oracle_test_secrets(oracle_test_config, monkeypatch)
