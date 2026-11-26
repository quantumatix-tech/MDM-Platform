"""Offline coverage for the single-command Oracle E2E orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle import run_e2e
from tests.e2e.oracle.config import validated_oracle_context
from tests.e2e.oracle.owner_provisioning import validate_onboarding_config
from tests.unit.oracle_test_settings import load_oracle_test_config


def _configured_workflow():
    raw = load_oracle_test_config()
    raw["migration"] = {"mode": "full"}
    raw["e2e"]["owner_provisioning"] = {
        "enabled": True,
        "source": {
            "expected_container_name": "SOURCE_PDB",
            "permanent_tablespace": "APP_SOURCE",
            "quota_bytes": 8192,
            "admin_connection": {"username": "PROVISIONER", "password_secret": "source_admin"},
        },
        "target": {
            "expected_container_name": "TARGET_PDB",
            "permanent_tablespace": "APP_TARGET",
            "quota_bytes": 8192,
            "admin_connection": {"username": "PROVISIONER", "password_secret": "target_admin"},
        },
    }
    raw["e2e"]["storage_bootstrap"] = {"enabled": False}
    return E2EConfig(raw)


def test_one_command_runs_existing_stages_in_order(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    events = []
    monkeypatch.setattr(run_e2e, "bootstrap_storage", lambda *_a, **_k: events.append("storage"))
    monkeypatch.setattr(run_e2e, "provision", lambda *_a, **_k: events.append("provision"))
    monkeypatch.setattr(run_e2e.run_preflight, "run", lambda *_a, **_k: events.append("preflight"))
    monkeypatch.setattr(
        run_e2e.run_setup,
        "run_setup",
        lambda *_a, **_k: events.append("setup") or {"setup_complete": True},
    )
    monkeypatch.setattr(run_e2e, "write_acceptance_report", lambda *_a, **_k: None)
    monkeypatch.setattr(
        run_e2e.run_validation,
        "run_validation",
        lambda *_a, **_k: events.append("validation") or {"result": "PASS"},
    )

    def command_runner(command, **kwargs):
        events.append("migration")
        assert command[1:3] == ["-m", "migration_platform"]
        assert "--exit-after-run" in command
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=0)

    result = run_e2e.run_workflow(
        cfg,
        config_path=tmp_path / "acceptance.yaml",
        driver=object(),
        apply=True,
        command_runner=command_runner,
    )

    assert events == ["storage", "provision", "preflight", "setup", "migration", "validation"]
    assert result["result"] == "PASS"
    assert result["stages"] == [
        "configuration",
        "storage",
        "provisioning",
        "preflight",
        "setup",
        "migration",
        "validation",
    ]


def test_preloaded_existing_schemas_skip_dba_storage_and_owner_provisioning(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    cfg.raw["e2e"]["source_mode"] = "preloaded"
    cfg.raw["e2e"]["fixtures"] = []
    cfg.raw["e2e"]["oracle_test_owner_prefix"] = "CLIENT_SCHEMA_"
    cfg.raw["source"]["connection"].update(
        {"service_name": "CLIENT_ALPHA_SERVICE", "username": "CLIENT_ALPHA_SCHEMA"}
    )
    cfg.raw["target"]["connection"].update(
        {"service_name": "CLIENT_BETA_SERVICE", "username": "CLIENT_BETA_SCHEMA"}
    )
    cfg.raw["e2e"]["resources"]["source"].update(
        {"name": "CLIENT_ALPHA_SCHEMA", "safe_pattern": "^CLIENT_ALPHA_SCHEMA$", "allow_exact_owner": True}
    )
    cfg.raw["e2e"]["resources"]["target"].update(
        {"name": "CLIENT_BETA_SCHEMA", "safe_pattern": "^CLIENT_BETA_SCHEMA$", "allow_exact_owner": True}
    )
    cfg.raw["e2e"]["owner_provisioning"] = {
        "enabled": False,
        "source": {"expected_container_name": "CLIENT_ALPHA_PDB"},
        "target": {"expected_container_name": "CLIENT_BETA_PDB"},
    }
    events = []
    storage = Mock(side_effect=AssertionError("preloaded mode must reuse storage"))
    provision = Mock(side_effect=AssertionError("preloaded mode must not create owners"))
    monkeypatch.setattr(run_e2e, "bootstrap_storage", storage)
    monkeypatch.setattr(run_e2e, "provision", provision)
    monkeypatch.setattr(run_e2e.run_preflight, "run", lambda *_a, **_k: events.append("preflight"))
    monkeypatch.setattr(
        run_e2e.run_setup,
        "run_setup",
        lambda *_a, **_k: events.append("setup") or {"setup_complete": True},
    )
    monkeypatch.setattr(run_e2e, "write_acceptance_report", lambda *_a, **_k: None)
    monkeypatch.setattr(
        run_e2e.run_validation,
        "run_validation",
        lambda *_a, **_k: events.append("validation") or {"result": "PASS"},
    )
    result = run_e2e.run_workflow(
        cfg,
        config_path=tmp_path / "existing-schema.yaml",
        driver=object(),
        apply=True,
        command_runner=lambda *_a, **_k: events.append("migration") or SimpleNamespace(returncode=0),
    )
    assert events == ["preflight", "setup", "migration", "validation"]
    assert result["result"] == "PASS"
    assert result["stages"] == [
        "configuration", "storage", "provisioning", "preflight", "setup", "migration", "validation"
    ]
    storage.assert_not_called()
    provision.assert_not_called()


def test_preloaded_mode_rejects_dba_bootstrap_opt_in(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    cfg.raw["e2e"]["source_mode"] = "preloaded"
    cfg.raw["e2e"]["fixtures"] = []
    cfg.raw["e2e"]["owner_provisioning"]["enabled"] = False
    with pytest.raises(run_e2e.WorkflowStageError, match="--dba-bootstrap is unavailable in preloaded mode"):
        run_e2e.run_workflow(
            cfg,
            config_path=tmp_path / "existing-schema.yaml",
            driver=object(),
            apply=True,
            dba_bootstrap=True,
        )


def test_explicit_dba_bootstrap_runs_before_storage_and_owner_provisioning(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    cfg.raw["source"]["connection"]["service_name"] = "SOURCE_SERVICE"
    cfg.raw["target"]["connection"]["service_name"] = "TARGET_SERVICE"
    cfg.raw["e2e"]["dba_bootstrap"] = {
        "enabled": True,
        "expected_db_unique_name": "LOCAL_CDB_TEST",
        "approve_admin_creation": True,
        "approve_exact_admin_privilege_grants": True,
        "approve_catalog_read_grants": True,
        "approve_source_owner_catalog_grants": True,
        "source_owner_catalog_views_to_grant": ["DBA_USERS", "DBA_ROLES"],
    }
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "PERMISSION_EXECUTOR_X", "password_secret": "executor_secret"},
    }
    events = []
    monkeypatch.setattr(
        run_e2e, "execute_dba_bootstrap", lambda *_a, **_k: events.append("dba")
    )
    monkeypatch.setattr(
        run_e2e, "verify_permission_executor", lambda *_a, **_k: events.append("executor") or {"status": "PASS"}
    )
    monkeypatch.setattr(run_e2e, "bootstrap_storage", lambda *_a, **_k: events.append("storage"))
    monkeypatch.setattr(run_e2e, "provision", lambda *_a, **_k: events.append("provision"))
    monkeypatch.setattr(
        run_e2e, "grant_source_owner_catalog_access",
        lambda *_a, **_k: events.append("source_catalog"),
    )
    monkeypatch.setattr(run_e2e.run_preflight, "run", lambda *_a, **_k: events.append("preflight"))
    monkeypatch.setattr(
        run_e2e.run_setup,
        "run_setup",
        lambda *_a, **_k: events.append("setup") or {"setup_complete": True},
    )
    monkeypatch.setattr(run_e2e, "write_acceptance_report", lambda *_a, **_k: None)
    monkeypatch.setattr(
        run_e2e.run_validation,
        "run_validation",
        lambda *_a, **_k: events.append("validation") or {"result": "PASS"},
    )
    result = run_e2e.run_workflow(
        cfg,
        config_path=tmp_path / "acceptance.yaml",
        driver=object(),
        apply=True,
        dba_bootstrap=True,
        command_runner=lambda *_a, **_k: events.append("migration") or SimpleNamespace(returncode=0),
    )
    assert events == ["dba", "executor", "storage", "provision", "source_catalog", "preflight", "setup", "migration", "validation"]
    assert result["stages"][:4] == ["configuration", "dba_bootstrap", "permission_executor", "storage"]


def test_configured_dba_bootstrap_requires_cli_opt_in(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    cfg.raw["e2e"]["dba_bootstrap"] = {
        "enabled": True,
        "expected_db_unique_name": "LOCAL_CDB_TEST",
        "approve_admin_creation": True,
        "approve_exact_admin_privilege_grants": True,
        "approve_catalog_read_grants": True,
    }
    dba = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr(run_e2e, "execute_dba_bootstrap", dba)
    with pytest.raises(
        run_e2e.WorkflowStageError,
        match="configuration: e2e.dba_bootstrap.enabled requires the separate --dba-bootstrap opt-in",
    ):
        run_e2e.run_workflow(
            cfg,
            config_path=tmp_path / "acceptance.yaml",
            driver=object(),
            apply=True,
        )
    dba.assert_not_called()


def test_workflow_requires_explicit_apply_before_any_stage(monkeypatch):
    storage = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr(run_e2e, "bootstrap_storage", storage)
    with pytest.raises(ValueError, match="explicit --apply"):
        run_e2e.run_workflow(
            _configured_workflow(),
            config_path="acceptance.yaml",
            driver=object(),
            apply=False,
        )
    storage.assert_not_called()


def test_missing_owner_admin_configuration_stops_at_config_stage(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    cfg.raw["e2e"]["owner_provisioning"]["enabled"] = False
    storage = Mock(side_effect=AssertionError("must not check or create storage"))
    monkeypatch.setattr(run_e2e, "bootstrap_storage", storage)
    with pytest.raises(
        run_e2e.WorkflowStageError,
        match="configuration: Oracle E2E onboarding configuration is incomplete.*e2e.owner_provisioning.enabled",
    ):
        run_e2e.run_workflow(
            cfg,
            config_path=tmp_path / "acceptance.yaml",
            driver=object(),
            apply=True,
        )
    storage.assert_not_called()


def test_missing_onboarding_values_are_reported_together():
    cfg = _configured_workflow()
    cfg.raw["e2e"]["owner_provisioning"] = {"enabled": False}
    with pytest.raises(ValueError) as exc:
        validate_onboarding_config(cfg)
    message = str(exc.value)
    for field in (
        "e2e.owner_provisioning.enabled",
        "e2e.owner_provisioning.source.admin_connection.username",
        "e2e.owner_provisioning.source.admin_connection.password_secret",
        "e2e.owner_provisioning.source.permanent_tablespace",
        "e2e.owner_provisioning.source.quota_bytes",
        "e2e.owner_provisioning.target.admin_connection.username",
        "e2e.owner_provisioning.target.admin_connection.password_secret",
        "e2e.owner_provisioning.target.permanent_tablespace",
        "e2e.owner_provisioning.target.quota_bytes",
    ):
        assert field in message


def test_onboarding_policy_accepts_different_configured_pdbs_and_schema_owners():
    cfg = _configured_workflow()
    cfg.raw["source"]["connection"]["service_name"] = "CLIENT_ALPHA_SERVICE"
    cfg.raw["target"]["connection"]["service_name"] = "CLIENT_BETA_SERVICE"
    cfg.raw["source"]["connection"]["username"] = "CLIENT_E2E_SOURCE_OWNER"
    cfg.raw["target"]["connection"]["username"] = "CLIENT_E2E_TARGET_OWNER"
    cfg.raw["e2e"]["oracle_test_owner_prefix"] = "CLIENT_E2E_"
    cfg.raw["e2e"]["resources"]["source"].update(
        {"name": "CLIENT_E2E_SOURCE_OWNER", "safe_pattern": r"^CLIENT_E2E_[A-Z_]+$"}
    )
    cfg.raw["e2e"]["resources"]["target"].update(
        {"name": "CLIENT_E2E_TARGET_OWNER", "safe_pattern": r"^CLIENT_E2E_[A-Z_]+$"}
    )
    cfg.raw["e2e"]["owner_provisioning"]["source"]["expected_container_name"] = "ALPHA_PDB"
    cfg.raw["e2e"]["owner_provisioning"]["target"]["expected_container_name"] = "BETA_PDB"

    validate_onboarding_config(cfg)
    owners, services = validated_oracle_context(cfg)

    assert owners == {
        "source": "CLIENT_E2E_SOURCE_OWNER",
        "target": "CLIENT_E2E_TARGET_OWNER",
    }
    assert services == {
        "source": "CLIENT_ALPHA_SERVICE",
        "target": "CLIENT_BETA_SERVICE",
    }


def test_workflow_stops_at_first_failed_stage(monkeypatch, tmp_path):
    provision = Mock(side_effect=AssertionError("must not provision"))
    monkeypatch.setattr(
        run_e2e, "bootstrap_storage", Mock(side_effect=RuntimeError("storage absent"))
    )
    monkeypatch.setattr(run_e2e, "provision", provision)
    with pytest.raises(run_e2e.WorkflowStageError, match="storage: storage absent") as exc:
        run_e2e.run_workflow(
            _configured_workflow(),
            config_path=tmp_path / "acceptance.yaml",
            driver=object(),
            apply=True,
        )
    assert exc.value.stage == "storage"
    provision.assert_not_called()


def test_workflow_stops_after_nonzero_migration_without_validation(monkeypatch, tmp_path):
    cfg = _configured_workflow()
    monkeypatch.setattr(run_e2e, "bootstrap_storage", lambda *_a, **_k: None)
    monkeypatch.setattr(run_e2e, "provision", lambda *_a, **_k: None)
    monkeypatch.setattr(run_e2e.run_preflight, "run", lambda *_a, **_k: {})
    monkeypatch.setattr(run_e2e.run_setup, "run_setup", lambda *_a, **_k: {"setup_complete": True})
    monkeypatch.setattr(run_e2e, "write_acceptance_report", lambda *_a, **_k: None)
    validation = Mock(side_effect=AssertionError("must not validate"))
    monkeypatch.setattr(run_e2e.run_validation, "run_validation", validation)

    with pytest.raises(
        run_e2e.WorkflowStageError, match="migration: migration CLI exited with status 4"
    ):
        run_e2e.run_workflow(
            cfg,
            config_path=tmp_path / "acceptance.yaml",
            driver=object(),
            apply=True,
            command_runner=lambda *_a, **_k: SimpleNamespace(returncode=4),
        )
    validation.assert_not_called()
