"""Tests for the prepared, placeholder-safe Oracle acceptance YAML."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from tests.e2e.acceptance import E2EConfig, load_e2e_config
from tests.e2e.oracle.config import (
    expected_foreign_key_relations,
    fixture_names_and_expected_tables,
    validated_oracle_context,
)
from tests.e2e.oracle.setup.fixture_loader import resolve_fixture_files


def _oracle_acceptance_config():
    repo_root = Path(__file__).parents[2]
    return load_e2e_config(str(repo_root / "config" / "oracle_e2e_acceptance.yaml"))


def test_acceptance_yaml_defines_separate_oracle_owners_and_safe_patterns():
    cfg = _oracle_acceptance_config()
    owners, services = validated_oracle_context(cfg)
    expected_secret_refs = {
        "source": "oracle_e2e_source_pass",
        "target": "oracle_e2e_target_pass",
    }

    assert cfg.raw["source"]["engine"] == "oracle"
    assert cfg.raw["target"]["engine"] == "oracle"
    assert owners["source"] != owners["target"]
    assert services == {
        side: cfg.raw[side]["connection"]["service_name"] for side in ("source", "target")
    }
    assert services == {"source": "ORACLE_SOURCE_DB", "target": "ORACLE_TARGET_DB"}
    for side in ("source", "target"):
        resource = cfg.raw["e2e"]["resources"][side]
        connection = cfg.raw[side]["connection"]
        assert resource["kind"] == "schema"
        assert connection["username"] == resource["name"]
        if side == "target" and resource["name"] == "ORACLE_USER_TEST":
            assert resource["safe_pattern"] == r"^ORACLE_USER_TEST$"
        else:
            assert resource["safe_pattern"].startswith(
                f"^{cfg.raw['e2e']['oracle_test_owner_prefix']}"
            )
        assert resource["safe_pattern"].endswith("$")
        assert "password" not in connection
        assert connection["password_secret"]
        assert connection["password_secret"] == expected_secret_refs[side]
        assert f"SECRET_{connection['password_secret']}" == f"SECRET_{expected_secret_refs[side]}"
    assert owners["source"] == cfg.raw["source"]["connection"]["username"]
    assert owners["target"] == cfg.raw["target"]["connection"]["username"]
    assert owners == {
        "source": "MIGE2E_SIMPLE_SRC",
        "target": "MIGE2E_SIMPLE_TGT",
    }


def test_owner_provisioning_is_disabled_and_expected_containers_are_explicit():
    cfg = _oracle_acceptance_config()
    policy = cfg.raw["e2e"]["owner_provisioning"]
    assert policy["enabled"] is False
    assert cfg.raw["e2e"]["storage_bootstrap"] == {"enabled": False}
    for side in ("source", "target"):
        assert (
            policy[side]["expected_container_name"] == cfg.raw[side]["connection"]["service_name"]
        )
        assert "admin_connection" not in policy[side]
        assert "permanent_tablespace" not in policy[side]
        assert "quota_bytes" not in policy[side]


@pytest.mark.parametrize("target_service", ["ORACLE_SOURCE_DB", "ORACLE_TARGET_DB"])
def test_oracle_context_accepts_same_or_distinct_endpoint_services(target_service):
    raw = copy.deepcopy(_oracle_acceptance_config().raw)
    raw["target"]["connection"]["service_name"] = target_service

    _owners, services = validated_oracle_context(E2EConfig(raw))

    assert services == {"source": "ORACLE_SOURCE_DB", "target": target_service}


def test_oracle_context_requires_both_endpoint_services():
    raw = copy.deepcopy(_oracle_acceptance_config().raw)
    raw["target"]["connection"]["service_name"] = " "

    with pytest.raises(ValueError, match="Oracle target service_name is required"):
        validated_oracle_context(E2EConfig(raw))


def test_oracle_context_rejects_the_same_owner_even_across_services():
    raw = copy.deepcopy(_oracle_acceptance_config().raw)
    raw["target"]["connection"]["service_name"] = "ORACLE_TARGET_DB"
    raw["target"]["connection"]["username"] = raw["source"]["connection"]["username"]
    raw["e2e"]["resources"]["target"]["name"] = raw["source"]["connection"]["username"]
    raw["e2e"]["resources"]["target"]["safe_pattern"] = raw["e2e"]["resources"]["source"][
        "safe_pattern"
    ]

    with pytest.raises(ValueError, match="owners must be distinct"):
        validated_oracle_context(E2EConfig(raw))


def test_canonical_acceptance_yaml_resolves_supported_relational_fixtures_and_tables():
    cfg = _oracle_acceptance_config()
    fixture_names, tables = fixture_names_and_expected_tables(cfg, allow_source_discovery=True)
    assert cfg.raw["e2e"]["source_mode"] == "fixtures"
    assert fixture_names == [
        "03_create_tables.sql",
        "04_create_foreign_keys.sql",
        "05_create_indexes.sql",
        "15_seed_data.sql",
    ]
    assert tables == ["SIMPLE_CUSTOMER", "SIMPLE_ORDER"]
    assert expected_foreign_key_relations(cfg, tables) == [
        {"table": "SIMPLE_ORDER", "referenced_table": "SIMPLE_CUSTOMER"},
    ]
    assert "include_tables" not in cfg.raw["source"]["connection"]
    assert cfg.raw["e2e"]["fixture_path"] == "tests/e2e/oracle/fixtures/simple_data"
    resolved = resolve_fixture_files(cfg.to_abs_fixture_path("."), fixture_names)
    assert len(resolved) == 4
    assert cfg.raw["e2e"]["oracle_test_owner_prefix"]
    assert cfg.raw["e2e"]["cleanup"]["mode"] == "never"


def test_canonical_yaml_uses_supported_oracle_retry_and_safety_options():
    cfg = _oracle_acceptance_config().raw
    for side in ("source", "target"):
        connection = cfg[side]["connection"]
        assert connection["connection_timeout"] == 5
        assert connection["connection_max_retries"] == 2
        assert connection["connection_retry_delay"] == 1
        assert connection["password_secret"]
        assert "password" not in connection
    assert cfg["secrets"]["provider"] == "env"
    assert cfg["migration"]["mode"] == "full"
    assert cfg["migration"]["reconcile_target_schema"] is True
    assert cfg["migration"]["oracle_schema_replacement"]["enabled"] is True
    profile = cfg["e2e"]["onboarding_profile"]
    assert profile["enabled"] is True
    assert profile["expected_db_unique_name"] == "FREE"
    assert profile["required_owner_privileges"] == "fixture_derived"
    assert profile["source"]["admin_connection"]["password_secret"] == "oracle_e2e_source_admin_pass"
    assert profile["target"]["admin_connection"]["password_secret"] == "oracle_e2e_target_admin_pass"
    permission = cfg["oracle_permission_migration"]
    assert permission["enabled"] is True
    assert permission["executor"] == {
        "username": "ORACLE_PERMISSION_EXECUTOR",
        "password_secret": "oracle_permission_executor_password",
    }
    assert permission["new_user_policy"] == "LOCKED"
    bootstrap = cfg["oracle_privilege_bootstrap"]
    assert bootstrap["enabled"] is True
    assert bootstrap["approved_privileges"] == [
        "CREATE VIEW", "CREATE SEQUENCE", "CREATE TRIGGER"
    ]
    assert bootstrap["admin_connection"]["username"] == "ORACLE_TARGET_ADMIN"
    assert bootstrap["admin_connection"]["password_secret"] == "oracle_e2e_target_admin_pass"
    assert bootstrap["admin_connection"]["service_name"] == cfg["target"]["connection"]["service_name"]


def test_canonical_yaml_has_no_duplicate_mapping_keys():
    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"duplicate YAML key: {key}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    config_path = Path(__file__).parents[2] / "config" / "oracle_e2e_acceptance.yaml"
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.load(stream, Loader=UniqueKeyLoader)
    assert raw["source"]["engine"] == raw["target"]["engine"] == "oracle"


def test_canonical_runtime_keys_match_the_supported_configuration_schema():
    repo_root = Path(__file__).parents[2]
    config = _oracle_acceptance_config().raw
    schema = yaml.safe_load(
        (repo_root / "config" / "migration_config.schema.yaml").read_text(encoding="utf-8")
    )
    assert set(config) - {"e2e"} <= set(schema)
    for side in ("source", "target"):
        assert set(config[side]) <= set(schema[side])
        assert set(config[side]["connection"]) <= set(schema[side]["connection"])
    for section in (
        "secrets",
        "oracle_permission_migration",
        "oracle_privilege_bootstrap",
        "migration",
        "alerting",
        "validation",
        "logging",
    ):
        section_schema = schema[section]
        supported_keys = section_schema.get("properties", section_schema)
        assert set(config[section]) <= set(supported_keys)
    assert config["e2e"]  # consumed by tests/e2e/oracle, not migration runtime


def test_repository_contains_only_the_canonical_oracle_yaml():
    repo_root = Path(__file__).parents[2]
    oracle_yaml_files = sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in repo_root.rglob("*.y*ml")
        if path.name.casefold().startswith("oracle_")
    )
    assert oracle_yaml_files == [
        "config/oracle_e2e_acceptance.yaml",
        "config/oracle_local_to_local.yaml",
    ]


def test_documented_powershell_workflow_gates_migration_on_setup_exit_code():
    repo_root = Path(__file__).parents[2]
    text = (repo_root / "docs" / "oracle" / "ORACLE_TEST_GUIDE.md").read_text(encoding="utf-8")
    setup = text.index(
        "python tests/e2e/oracle/run_setup.py --config config/oracle_e2e_acceptance.yaml"
    )
    gate = text.index(
        'if ($LASTEXITCODE -ne 0) { throw "Oracle E2E setup failed; migration was not started." }'
    )
    migration = text.index(
        "python -m migration_platform --config config/oracle_e2e_acceptance.yaml --mode full"
    )
    assert setup < gate < migration


def test_acceptance_configuration_never_enables_cleanup():
    cfg = _oracle_acceptance_config()
    assert cfg.raw["e2e"]["cleanup"]["mode"] == "never"
    assert cfg.raw["migration"]["mode"] == "full"
    assert "reconcile_mssql_partition_functions" not in cfg.raw["migration"]
