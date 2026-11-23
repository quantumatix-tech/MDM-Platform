"""Offline contract tests for the isolated, relational-only Oracle acceptance case."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.oracle.config import (
    expected_foreign_key_relations,
    fixture_names_and_expected_tables,
    validated_oracle_context,
)
from tests.e2e.oracle.live_launcher import _onboarding_profile
from tests.e2e.oracle.owner_provisioning import _fixture_privileges
from tests.e2e.oracle.setup.fixture_loader import parse_fixture_sql, resolve_fixture_files

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "config" / "oracle_e2e_acceptance.yaml"


def test_simple_acceptance_config_is_isolated_and_data_only():
    cfg = load_e2e_config(str(CONFIG_PATH))
    owners, services = validated_oracle_context(cfg)
    _fixtures, tables = fixture_names_and_expected_tables(cfg)

    assert owners == {"source": "MIGE2E_SIMPLE_SRC", "target": "MIGE2E_SIMPLE_TGT"}
    assert services == {"source": "ORACLE_SOURCE_DB", "target": "ORACLE_TARGET_DB"}
    assert _onboarding_profile(cfg)["required_owner_privileges"] == "fixture_derived"
    permission = cfg.raw["oracle_permission_migration"]
    assert permission["enabled"] is True
    assert permission["executor"]["username"] == "ORACLE_PERMISSION_EXECUTOR"
    assert permission["executor"]["password_secret"] == "oracle_permission_executor_password"
    bootstrap = cfg.raw["oracle_privilege_bootstrap"]
    assert bootstrap["enabled"] is True
    assert bootstrap["approved_privileges"] == [
        "CREATE VIEW", "CREATE SEQUENCE", "CREATE TRIGGER"
    ]
    assert "include_tables" not in cfg.raw["source"]["connection"]
    assert tables == ["SIMPLE_CUSTOMER", "SIMPLE_ORDER"]
    assert cfg.raw["migration"]["mode"] == "full"
    assert cfg.raw["e2e"]["cleanup"]["mode"] == "never"
    assert cfg.raw["e2e"]["onboarding_profile"]["source"]["storage"] == {
        "mode": "reuse_existing"
    }
    assert cfg.raw["e2e"]["onboarding_profile"]["target"]["storage"] == {
        "mode": "reuse_existing"
    }
    assert expected_foreign_key_relations(cfg, tables) == [
        {"table": "SIMPLE_ORDER", "referenced_table": "SIMPLE_CUSTOMER"}
    ]
    assert cfg.raw["e2e"]["expected"]["row_counts"] == {
        "SIMPLE_CUSTOMER": 2,
        "SIMPLE_ORDER": 3,
    }


def test_simple_fixtures_are_ordered_safe_and_require_only_relational_privileges():
    cfg = load_e2e_config(str(CONFIG_PATH))
    fixture_names, _tables = fixture_names_and_expected_tables(cfg)
    fixture_dir = cfg.to_abs_fixture_path(".")
    files = resolve_fixture_files(fixture_dir, fixture_names)

    assert [path.name for path in files] == [
        "03_create_tables.sql",
        "04_create_foreign_keys.sql",
        "05_create_indexes.sql",
        "15_seed_data.sql",
    ]
    parsed = {
        path.name: parse_fixture_sql(path.read_text(encoding="utf-8"), filename=path.name)
        for path in files
    }
    assert len(parsed["03_create_tables.sql"]) == 2
    assert len(parsed["04_create_foreign_keys.sql"]) == 1
    assert len(parsed["05_create_indexes.sql"]) == 1
    assert len(parsed["15_seed_data.sql"]) == 5
    all_sql = "\n".join(statement for statements in parsed.values() for statement in statements)
    assert "PRIMARY KEY" in all_sql
    assert "REFERENCES SIMPLE_CUSTOMER (CUSTOMER_ID)" in all_sql
    assert "SIMPLE_ORDER_CUSTOMER_IX" in all_sql
    assert "99999999999999.9999" in all_sql
    assert "TIMESTAMP WITH TIME ZONE" not in all_sql.upper()
    assert "CREATE TYPE" not in all_sql.upper()
    assert _fixture_privileges(cfg) == {"CREATE SESSION", "CREATE TABLE"}


def test_simple_config_runtime_fields_match_shared_configuration_schema():
    cfg = load_e2e_config(str(CONFIG_PATH)).raw
    schema = yaml.safe_load(
        (ROOT / "config" / "migration_config.schema.yaml").read_text(encoding="utf-8")
    )
    assert set(cfg) - {"e2e"} <= set(schema)
    for side in ("source", "target"):
        assert set(cfg[side]) <= set(schema[side])
        assert set(cfg[side]["connection"]) <= set(schema[side]["connection"])
    for section in (
        "secrets",
        "oracle_permission_migration",
        "oracle_privilege_bootstrap",
        "migration",
        "alerting",
        "validation",
        "logging",
    ):
        properties = schema[section].get("properties", schema[section])
        assert set(cfg[section]) <= set(properties)
