from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.acceptance import E2EConfig, load_e2e_config
from tests.e2e.oracle import dba_bootstrap, live_launcher
from tests.unit.oracle_test_settings import load_oracle_test_config


def _generated_plsql_blocks(script: str) -> list[str]:
    blocks: list[str] = []
    lines = script.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip().upper()
        if start is None and stripped.startswith(("DECLARE", "BEGIN")):
            start = index
        elif start is not None and stripped == "/":
            blocks.append("\n".join(lines[start:index]))
            start = None
    assert start is None, "generated anonymous PL/SQL block is missing its SQL*Plus '/' terminator"
    return blocks


def _assert_plsql_blocks_structurally_valid(script: str) -> list[str]:
    """Check every generated anonymous block for balanced, non-empty IF branches."""
    blocks = _generated_plsql_blocks(script)
    assert blocks, "the bootstrap script must contain PL/SQL blocks"
    token_re = re.compile(r"\bEND\s+IF\b|\bIF\b|\bTHEN\b|\bELSE\b")
    for block in blocks:
        # SQL literals contain generated DDL and error text, neither of which
        # is PL/SQL control flow. Remove them before checking the block.
        code = re.sub(r"'(?:''|[^'])*'", "''", block, flags=re.DOTALL)
        assert len(re.findall(r"\bBEGIN\b", code)) == 1, block
        assert len(re.findall(r"\bEND\b(?!\s+IF\b)", code)) == 1, block
        stack: list[dict[str, int | bool]] = []
        for token in token_re.finditer(code):
            word = re.sub(r"\s+", " ", token.group(0).upper())
            if word == "IF":
                stack.append({"then": False, "else": False, "body_start": -1})
            elif word == "THEN":
                assert stack and not stack[-1]["then"], block
                stack[-1]["then"] = True
                stack[-1]["body_start"] = token.end()
            elif word == "ELSE":
                assert stack and stack[-1]["then"] and not stack[-1]["else"], block
                body_start = int(stack[-1]["body_start"])
                assert code[body_start:token.start()].strip().strip(";") , block
                stack[-1]["else"] = True
                stack[-1]["body_start"] = token.end()
            else:  # END IF
                assert stack and stack[-1]["then"], block
                body_start = int(stack[-1]["body_start"])
                assert code[body_start:token.start()].strip().strip(";") , block
                stack.pop()
        assert not stack, block
    return blocks


def _cfg():
    raw = load_oracle_test_config()
    raw["e2e"]["owner_provisioning"] = {
        "enabled": True,
        "source": {
            "expected_container_name": "SOURCE_PDB_X",
            "permanent_tablespace": "APP_DATA_X",
            "quota_bytes": 4096,
            "admin_connection": {"username": "SOURCE_ADMIN_X", "password_secret": "source_admin_x"},
        },
        "target": {
            "expected_container_name": "TARGET_PDB_Y",
            "permanent_tablespace": "APP_DATA_Y",
            "quota_bytes": 8192,
            "admin_connection": {"username": "TARGET_ADMIN_Y", "password_secret": "target_admin_y"},
        },
    }
    raw["e2e"]["storage_bootstrap"] = {"enabled": False}
    raw["e2e"]["dba_bootstrap"] = {
        "enabled": True,
        "expected_db_unique_name": "LOCAL_CDB_TEST",
        "local_oracle_sid": "LOCAL_SID_TEST",
        "approve_admin_creation": True,
        "approve_exact_admin_privilege_grants": True,
        "approve_catalog_read_grants": True,
    }
    raw["source"]["connection"].update(
        {"service_name": "SERVICE_X", "username": "MIGRATION_E2E_SOURCE_OWNER_X", "password_secret": "source_owner_x"}
    )
    raw["target"]["connection"].update(
        {"service_name": "SERVICE_Y", "username": "MIGRATION_E2E_TARGET_OWNER_Y", "password_secret": "target_owner_y"}
    )
    raw["e2e"]["resources"] = {
        "source": {"kind": "schema", "name": "MIGRATION_E2E_SOURCE_OWNER_X", "safe_pattern": "^MIGRATION_E2E_SOURCE_OWNER_X$"},
        "target": {"kind": "schema", "name": "MIGRATION_E2E_TARGET_OWNER_Y", "safe_pattern": "^MIGRATION_E2E_TARGET_OWNER_Y$"},
    }
    return E2EConfig(raw)


def _executor_cfg(*, creation_approved=True, username="PERMISSION_EXECUTOR_X", secret_ref="executor_x_secret"):
    cfg = _cfg()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "secret_provider": "env",
        "executor": {"username": username, "password_secret": secret_ref},
    }
    cfg.raw["e2e"]["dba_bootstrap"]["approve_permission_executor_creation"] = creation_approved
    return cfg


def test_dba_bootstrap_script_checks_both_pdbs_before_any_ddl_and_is_narrow():
    script = dba_bootstrap.build_sqlplus_script(
        _cfg(), {"source": "sourcesecret", "target": "targetsecret"}
    )
    identity_end = script.index("ORACLE_E2E_PDB_IDENTITY_PREFLIGHT_PASS")
    first_ddl = script.index("CREATE TABLESPACE") if "CREATE TABLESPACE" in script else script.index("CREATE USER")
    assert identity_end < first_ddl
    assert "CDB$ROOT" in script
    assert "SOURCE_PDB_X" in script and "TARGET_PDB_Y" in script
    assert "SERVICE_X" in script and "SERVICE_Y" in script
    assert "EXECUTE IMMEDIATE 'GRANT ANY PRIVILEGE" not in script


def test_owner_admin_catalog_grants_are_narrow_and_cover_partial_recovery_views():
    script = dba_bootstrap.build_sqlplus_script(
        _cfg(), {"source": "source-secret", "target": "target-secret"}
    )
    for view in dba_bootstrap._CATALOG_VIEWS:
        assert f"GRANT SELECT ON SYS.{view} TO \"SOURCE_ADMIN_X\"" in script
        assert f"GRANT SELECT ON SYS.{view} TO \"TARGET_ADMIN_Y\"" in script
    assert "GRANT SELECT ON SYS.DBA_TS_QUOTAS TO \"SOURCE_ADMIN_X\"" in script
    assert "GRANT SELECT ON SYS.DBA_SYS_PRIVS TO \"SOURCE_ADMIN_X\"" in script
    assert "GRANT DBA TO \"SOURCE_ADMIN_X\"" not in script
    assert "GRANT SELECT ANY DICTIONARY TO \"SOURCE_ADMIN_X\"" not in script
    assert script.count("GRANT SELECT ON SYS.DBA_TS_QUOTAS TO \"SOURCE_ADMIN_X\"") == 1
    assert script.count("GRANT SELECT ON SYS.DBA_SYS_PRIVS TO \"SOURCE_ADMIN_X\"") == 1
    assert "WITH ADMIN OPTION" in script
    assert "SELECT_CATALOG_ROLE" not in script
    assert "ORACLE_SOURCE_DB" not in script
    assert "ORACLE_TARGET_DB" not in script
    assert "FREEPDB1" not in script and "MIGRATION_DB" not in script
    _assert_plsql_blocks_structurally_valid(script)


def test_source_owner_catalog_grants_are_plan_approved_dynamic_and_idempotent():
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_source_owner_catalog_grants": True,
        "source_owner_catalog_views_to_grant": ["DBA_USERS", "DBA_ROLES"],
    })
    script = dba_bootstrap.build_source_owner_catalog_grant_sql(cfg)
    assert "LOCAL_CDB_TEST" in script
    assert "SOURCE_PDB_X" in script and "SERVICE_X" in script
    assert '"MIGRATION_E2E_SOURCE_OWNER_X"' in script
    assert "GRANT SELECT ON SYS.DBA_USERS TO " in script
    assert "GRANT SELECT ON SYS.DBA_ROLES TO " in script
    assert "GRANT SELECT ON SYS.DBA_ROLE_PRIVS TO " not in script
    assert "GRANT SELECT ON SYS.DBA_SYS_PRIVS TO " not in script
    assert "DBA_TARGET" not in script and "TARGET_PDB_Y" not in script
    assert "IF v_count = 0 THEN EXECUTE IMMEDIATE" in script
    assert "SOURCE owner must exist, be OPEN, and be PDB-local" in script
    assert "Expected CDB$ROOT" in script
    _assert_plsql_blocks_structurally_valid(script)


def test_source_owner_catalog_grants_require_exact_allowlist_and_approval():
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_source_owner_catalog_grants": True,
        "source_owner_catalog_views_to_grant": ["DBA_USERS", "DBA_TAB_PRIVS"],
    })
    with pytest.raises(ValueError, match="only unique approved"):
        dba_bootstrap.build_source_owner_catalog_grant_sql(cfg)
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_source_owner_catalog_grants": False,
        "source_owner_catalog_views_to_grant": ["DBA_USERS"],
    })
    with pytest.raises(ValueError, match="explicit execution-plan approval"):
        dba_bootstrap.build_source_owner_catalog_grant_sql(cfg)


def test_source_owner_catalog_grant_runner_is_opt_in_and_reuses_sqlplus(monkeypatch):
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_source_owner_catalog_grants": True,
        "source_owner_catalog_views_to_grant": ["DBA_USERS"],
    })
    captured = {}

    def fake_run(script, **kwargs):
        captured["script"] = script
        captured.update(kwargs)
        return "ORACLE_SOURCE_OWNER_CATALOG_GRANTS_COMPLETE"

    monkeypatch.setattr(dba_bootstrap, "run_sqlplus_script", fake_run)
    with pytest.raises(ValueError, match="explicit --apply"):
        dba_bootstrap.grant_source_owner_catalog_access(cfg, apply=False)
    result = dba_bootstrap.grant_source_owner_catalog_access(cfg, apply=True)
    assert result["status"] == "PASS"
    assert result["views"] == ["DBA_USERS"]
    assert captured["operation"] == "Oracle SOURCE owner permission catalog grants"
    assert captured["oracle_sid"] == "LOCAL_SID_TEST"
    assert captured["completion_marker"] == "ORACLE_SOURCE_OWNER_CATALOG_GRANTS_COMPLETE"


def test_target_replacement_catalog_grants_are_exact_plan_approved_and_pdb_local():
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_target_owner_catalog_grants": True,
        "target_owner_catalog_views_to_grant": ["DBA_CONSTRAINTS", "DBA_DEPENDENCIES"],
    })
    script = dba_bootstrap.build_target_owner_catalog_grant_sql(cfg)

    assert "TARGET_PDB_Y" in script and "SERVICE_Y" in script
    assert '"MIGRATION_E2E_TARGET_OWNER_Y"' in script
    assert "GRANT SELECT ON SYS.DBA_CONSTRAINTS TO " in script
    assert "GRANT SELECT ON SYS.DBA_DEPENDENCIES TO " in script
    assert "GRANT SELECT ON SYS.DBA_USERS TO " not in script
    assert "GRANT SELECT ANY DICTIONARY" not in script
    assert "TARGET owner must exist, be OPEN, and be PDB-local" in script
    assert "SELECT ANY DICTIONARY" in script
    _assert_plsql_blocks_structurally_valid(script)


def test_target_replacement_catalog_grants_reject_unapproved_views_and_missing_approval():
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_target_owner_catalog_grants": True,
        "target_owner_catalog_views_to_grant": ["DBA_USERS"],
    })
    with pytest.raises(ValueError, match="only unique approved"):
        dba_bootstrap.build_target_owner_catalog_grant_sql(cfg)
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_target_owner_catalog_grants": False,
        "target_owner_catalog_views_to_grant": ["DBA_CONSTRAINTS"],
    })
    with pytest.raises(ValueError, match="explicit execution-plan approval"):
        dba_bootstrap.build_target_owner_catalog_grant_sql(cfg)


def test_target_replacement_catalog_grant_runner_is_opt_in_and_reuses_sqlplus(monkeypatch):
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"].update({
        "approve_target_owner_catalog_grants": True,
        "target_owner_catalog_views_to_grant": ["DBA_CONSTRAINTS"],
    })
    captured = {}

    def fake_run(script, **kwargs):
        captured["script"] = script
        captured.update(kwargs)
        return "ORACLE_TARGET_OWNER_CATALOG_GRANTS_COMPLETE"

    monkeypatch.setattr(dba_bootstrap, "run_sqlplus_script", fake_run)
    with pytest.raises(ValueError, match="explicit --apply"):
        dba_bootstrap.grant_target_owner_catalog_access(cfg, apply=False)
    result = dba_bootstrap.grant_target_owner_catalog_access(cfg, apply=True)
    assert result["status"] == "PASS"
    assert result["views"] == ["DBA_CONSTRAINTS"]
    assert captured["operation"] == "Oracle TARGET replacement catalog grants"
    assert captured["oracle_sid"] == "LOCAL_SID_TEST"
    assert captured["completion_marker"] == "ORACLE_TARGET_OWNER_CATALOG_GRANTS_COMPLETE"


def test_canonical_acceptance_runtime_bootstrap_sql_has_valid_complete_plsql(monkeypatch):
    config_path = Path(__file__).parents[2] / "config" / "oracle_e2e_acceptance.yaml"
    cfg = load_e2e_config(str(config_path))
    profile = cfg.raw["e2e"]["onboarding_profile"]
    facts = {
        "db_unique_name": profile["expected_db_unique_name"],
        "owners": {
            side: {"status": "MISSING", "common": "NO", "objects": 0, "default_tablespace": ""}
            for side in ("source", "target")
        },
        "owner_privileges": {"source": set(), "target": set()},
        "target_owner_catalog_views": set(),
        "permission_executor": {
            "username": "ORACLE_PERMISSION_EXECUTOR",
            "status": "MISSING",
            "common": "NO",
        },
    }
    monkeypatch.setattr(live_launcher, "_resolve_secret", lambda *_a, **_kw: ("offline-secret", False))
    monkeypatch.setattr(live_launcher, "_probe_login", lambda *_a, **_kw: None)
    raw = live_launcher._new_runtime_config(cfg, facts, object(), allow_secret_prompt=False)
    runtime_cfg = E2EConfig(raw)
    live_launcher.validate_onboarding_config(runtime_cfg)
    dba_bootstrap.validate_dba_bootstrap_config(runtime_cfg)

    script = dba_bootstrap.build_sqlplus_script(
        runtime_cfg,
        {
            "source": "source-secret",
            "target": "target-secret",
            "permission_executor": "executor-secret",
        },
    )
    blocks = _assert_plsql_blocks_structurally_valid(script)

    assert "dba.source.resource_checks" in script
    assert "dba.target.resource_checks" in script
    # Reuse-existing storage must not generate explicit-path collision checks
    # or CREATE TABLESPACE DDL in the canonical acceptance profile.
    assert "Configured datafile path is already registered to a tablespace" not in script
    assert "CREATE TABLESPACE E2E_APP_DATA" not in script
    assert len(blocks) >= 9


def test_dba_bootstrap_refuses_to_run_without_independent_approval():
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"]["approve_exact_admin_privilege_grants"] = False
    runner = Mock(side_effect=AssertionError("SQL*Plus must not start"))
    with pytest.raises(ValueError, match="approve_exact_admin_privilege_grants"):
        dba_bootstrap.run_dba_bootstrap(
            cfg,
            apply=True,
            executable="sqlplus",
            runner=runner,
        )
    runner.assert_not_called()


def test_dba_bootstrap_requires_apply_before_secret_resolution_or_sqlplus():
    runner = Mock(side_effect=AssertionError("SQL*Plus must not start"))
    with pytest.raises(ValueError, match="explicit --apply"):
        dba_bootstrap.run_dba_bootstrap(
            _cfg(), apply=False, executable="sqlplus", runner=runner
        )
    runner.assert_not_called()


def test_dba_bootstrap_resolves_secrets_and_never_puts_them_in_process_args(monkeypatch):
    cfg = _cfg()

    class Resolver:
        def resolve(self, reference):
            return f"secret-for-{reference}"

    monkeypatch.setattr(dba_bootstrap, "resolver_for_side", lambda *_a, **_k: Resolver())
    captured = {}

    def runner(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout="ORACLE_E2E_DBA_BOOTSTRAP_COMPLETE")

    result = dba_bootstrap.run_dba_bootstrap(
        cfg, apply=True, executable="sqlplus", runner=runner
    )
    assert result["status"] == "PASS"
    assert captured["args"] == ["sqlplus", "-L", "-S", "/ as sysdba"]
    assert "source_admin_x" not in repr(captured["args"])
    assert "secret-for-source_admin_x" not in repr(captured["args"])
    assert "secret-for-source_admin_x" in captured["input"]
    assert "SET ECHO OFF" in captured["input"]


def test_dba_bootstrap_does_not_leak_sqlplus_output_on_failure(monkeypatch):
    cfg = _cfg()

    class Resolver:
        def resolve(self, _reference):
            return "hidden-secret"

    monkeypatch.setattr(dba_bootstrap, "resolver_for_side", lambda *_a, **_k: Resolver())
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=1,
            stdout="hidden-secret ORA-01031: insufficient privileges\n  Help: https://docs.oracle.com/error-help/db/ora-01031/",
            stderr="SP2-0734: unknown command beginning hidden-secret",
        )
    )
    with pytest.raises(RuntimeError, match=r"SQL\*Plus exit=1") as exc:
        dba_bootstrap.run_dba_bootstrap(
            cfg, apply=True, executable="sqlplus", runner=runner
        )
    assert "hidden-secret" not in str(exc.value)
    assert "ORA-01031" in str(exc.value)
    assert "SP2-0734" in str(exc.value)
    assert "stdout=" in str(exc.value) and "stderr=" in str(exc.value)
    assert "ORACLE_SID=LOCAL_SID_TEST" in str(exc.value)
    assert "Earlier DDL may have committed" in str(exc.value)


def test_dba_bootstrap_rejects_configured_sql_injection_identifiers():
    cfg = _cfg()
    cfg.raw["e2e"]["owner_provisioning"]["source"]["admin_connection"]["username"] = (
        'ADMIN"; DROP USER X--'
    )
    with pytest.raises(ValueError, match="simple Oracle identifier"):
        dba_bootstrap.build_sqlplus_script(
            cfg, {"source": "secret", "target": "secret"}
        )


def test_configured_storage_path_is_escaped_and_storage_creation_requires_approval():
    cfg = _cfg()
    cfg.raw["e2e"]["storage_bootstrap"] = {
        "enabled": True,
        "source": {"mode": "explicit_path", "datafile_path": "C:\\Oracle Data\\a'b.dbf", "size": "64M"},
        "target": {"mode": "explicit_path", "datafile_path": "C:\\Oracle Data\\b.dbf", "size": "64M"},
    }
    cfg.raw["e2e"]["dba_bootstrap"]["approve_storage_creation"] = True
    script = dba_bootstrap.build_sqlplus_script(
        cfg, {"source": "secret1", "target": "secret2"}
    )
    assert "C:\\Oracle Data\\a''''b.dbf" in script
    assert "CREATE TABLESPACE" in script
    assert "Configured datafile path is already registered to a tablespace" in script
    _assert_plsql_blocks_structurally_valid(script)

    cfg.raw["e2e"]["dba_bootstrap"]["approve_storage_creation"] = False
    with pytest.raises(ValueError, match="approve_storage_creation"):
        dba_bootstrap.build_sqlplus_script(
            cfg, {"source": "secret1", "target": "secret2"}
        )


@pytest.mark.parametrize("storage_mode", ["disabled", "reuse_existing", "explicit_path", "oracle_managed_files"])
@pytest.mark.parametrize("executor_mode", ["disabled", "create", "grants_only", "preexisting_unapproved"])
def test_complete_generated_plsql_is_structurally_valid_for_every_onboarding_branch(storage_mode, executor_mode):
    cfg = _executor_cfg(creation_approved=executor_mode == "create") if executor_mode != "disabled" else _cfg()
    bootstrap = cfg.raw["e2e"]["dba_bootstrap"]
    bootstrap["approve_admin_creation"] = executor_mode in {"create", "grants_only"}
    if executor_mode == "grants_only":
        bootstrap["approve_permission_executor_grants"] = True
    elif executor_mode == "preexisting_unapproved":
        bootstrap["approve_permission_executor_grants"] = False

    if storage_mode == "disabled":
        cfg.raw["e2e"]["storage_bootstrap"] = {"enabled": False}
        passwords = {"source": "source-secret", "target": "target-secret"}
    else:
        storage = {"enabled": True}
        for side in ("source", "target"):
            if storage_mode == "reuse_existing":
                storage[side] = {"mode": "reuse_existing"}
            elif storage_mode == "explicit_path":
                storage[side] = {
                    "mode": "explicit_path", "size": "64M",
                    "datafile_path": f"C:\\Approved\\{side}.dbf",
                }
            else:
                storage[side] = {"mode": "oracle_managed_files", "size": "64M"}
        cfg.raw["e2e"]["storage_bootstrap"] = storage
        bootstrap["approve_storage_creation"] = True
        passwords = {"source": "source-secret", "target": "target-secret"}

    if executor_mode != "disabled":
        passwords["permission_executor"] = "executor-secret"

    script = dba_bootstrap.build_sqlplus_script(cfg, passwords)
    blocks = _assert_plsql_blocks_structurally_valid(script)
    assert len(blocks) >= 7
    assert not re.search(r"(?im)^\s*ELSE\s*\n\s*END\s+IF\s*;", script)


@pytest.mark.parametrize("size,valid", [("512", False), ("512M", True), ("2G", True), ("2T", False)])
def test_dba_bootstrap_requires_explicit_oracle_size_unit(size, valid):
    cfg = _cfg()
    cfg.raw["e2e"]["storage_bootstrap"] = {
        "enabled": True,
        "source": {"mode": "oracle_managed_files", "size": size},
        "target": {"mode": "oracle_managed_files", "size": "512M"},
    }
    cfg.raw["e2e"]["dba_bootstrap"]["approve_storage_creation"] = True
    if valid:
        dba_bootstrap.validate_dba_bootstrap_config(cfg)
    else:
        with pytest.raises(ValueError, match="explicit Oracle K/M/G unit"):
            dba_bootstrap.validate_dba_bootstrap_config(cfg)


def test_missing_permission_executor_is_created_only_with_explicit_approval_and_minimal_login_grant():
    cfg = _executor_cfg(creation_approved=True)
    script = dba_bootstrap.build_sqlplus_script(
        cfg,
        {"source": "source-secret", "target": "target-secret", "permission_executor": "executor-secret"},
    )
    read_check = script.index("Configured permission executor must be OPEN and PDB-local")
    create_user = script.index('CREATE USER "PERMISSION_EXECUTOR_X"')
    assert read_check < create_user
    assert "IF v_count = 0 THEN" in script[create_user - 80 : create_user]
    assert "GRANT CREATE SESSION TO \"PERMISSION_EXECUTOR_X\"" in script
    for view in ("DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS", "DBA_TAB_PRIVS", "DBA_COL_PRIVS"):
        assert f"GRANT SELECT ON SYS.{view} TO \"PERMISSION_EXECUTOR_X\"" in script
    assert "CREATE ANY USER" not in script
    assert "GRANT DBA TO \"PERMISSION_EXECUTOR_X\"" not in script
    target_preflight = script.index('ALTER SESSION SET CONTAINER = "TARGET_PDB_Y"')
    target_ddl = script.index('ALTER SESSION SET CONTAINER = "TARGET_PDB_Y"', target_preflight + 1)
    assert create_user > target_ddl
    assert "VARIABLE permission_executor_pw VARCHAR2(4000)" in script
    assert "REPLACE(:permission_executor_pw" in script


def test_existing_permission_executor_is_not_reset_or_granted_again():
    cfg = _executor_cfg(creation_approved=False)
    script = dba_bootstrap.build_sqlplus_script(
        cfg, {"source": "source-secret", "target": "target-secret", "permission_executor": "executor-secret"}
    )
    assert "existing account was not changed" in script
    assert "Missing permission executor requires explicit creation approval" in script
    assert 'CREATE USER "PERMISSION_EXECUTOR_X"' not in script
    assert 'GRANT CREATE SESSION TO "PERMISSION_EXECUTOR_X"' not in script
    assert "ALTER USER \"PERMISSION_EXECUTOR_X\"" not in script


def test_existing_executor_missing_catalog_grants_is_completed_only_when_exactly_approved():
    cfg = _executor_cfg(creation_approved=False)
    cfg.raw["e2e"]["dba_bootstrap"]["approve_permission_executor_grants"] = True
    script = dba_bootstrap.build_sqlplus_script(
        cfg,
        {"source": "source-secret", "target": "target-secret", "permission_executor": "executor-secret"},
    )
    assert "SELECT COUNT(*) INTO v_count FROM dba_tab_privs WHERE grantee = 'PERMISSION_EXECUTOR_X'" in script
    assert "GRANT SELECT ON SYS.DBA_USERS TO \"PERMISSION_EXECUTOR_X\"" in script
    assert "GRANT CREATE SESSION TO \"PERMISSION_EXECUTOR_X\"" in script
    assert "ALTER USER \"PERMISSION_EXECUTOR_X\"" not in script


def test_existing_admin_delegation_and_catalog_reads_are_not_regranted_when_broad_authority_exists():
    cfg = _cfg()
    script = dba_bootstrap.build_sqlplus_script(
        cfg, {"source": "source-secret", "target": "target-secret"}
    )
    assert "privilege = 'GRANT ANY PRIVILEGE'" in script
    assert "privilege = 'SELECT ANY DICTIONARY'" in script
    assert "IF v_count = 0 THEN\n  SELECT COUNT(*) INTO v_count FROM dba_tab_privs" in script


def test_create_user_authority_is_granted_only_to_sides_with_missing_owners():
    cfg = _cfg()
    cfg.raw["e2e"]["dba_bootstrap"]["create_user_for_sides"] = ["target"]
    script = dba_bootstrap.build_sqlplus_script(
        cfg, {"source": "source-secret", "target": "target-secret"}
    )
    assert 'GRANT CREATE USER TO "TARGET_ADMIN_Y"' in script
    assert 'GRANT CREATE USER TO "SOURCE_ADMIN_X"' not in script


def test_side_specific_owner_privilege_delegation_uses_each_configured_side():
    cfg = _cfg()
    available = sorted(dba_bootstrap._fixture_privileges(cfg))
    assert len(available) >= 2
    source_privilege, target_privilege = available[:2]
    cfg.raw["e2e"]["dba_bootstrap"]["required_owner_privileges_by_side"] = {
        "source": [source_privilege],
        "target": [target_privilege],
    }

    script = dba_bootstrap.build_sqlplus_script(
        cfg, {"source": "source-secret", "target": "target-secret"}
    )
    container_switches = [
        index
        for index in range(len(script))
        if script.startswith('ALTER SESSION SET CONTAINER = "', index)
    ]
    # The first two switches are preflight; the next two are the DDL phase.
    source_ddl_start, target_ddl_start = container_switches[2:4]
    source_ddl = script[source_ddl_start:target_ddl_start]
    target_ddl = script[target_ddl_start:]

    assert f"GRANT {source_privilege} TO \"SOURCE_ADMIN_X\" WITH ADMIN OPTION" in source_ddl
    assert f"GRANT {target_privilege} TO \"TARGET_ADMIN_Y\" WITH ADMIN OPTION" in target_ddl
    assert f"GRANT {target_privilege} TO \"SOURCE_ADMIN_X\" WITH ADMIN OPTION" not in source_ddl
    assert f"GRANT {source_privilege} TO \"TARGET_ADMIN_Y\" WITH ADMIN OPTION" not in target_ddl


@pytest.mark.parametrize("bad_status,bad_common", [("LOCKED", "NO"), ("OPEN", "YES")])
def test_locked_or_common_permission_executor_fails_closed_without_account_changes(bad_status, bad_common):
    cfg = _executor_cfg(creation_approved=True)
    script = dba_bootstrap.build_sqlplus_script(
        cfg, {"source": "source-secret", "target": "target-secret", "permission_executor": "executor-secret"}
    )
    assert "v_status <> 'OPEN' OR v_common <> 'NO'" in script
    assert "existing account was not changed" in script
    assert "MAX(account_status), MAX(common)" in script
    assert "IF v_count > 0 AND (v_status <> 'OPEN' OR v_common <> 'NO')" in script


def test_permission_executor_must_be_distinct_and_have_secret_reference():
    cfg = _executor_cfg(username="MIGRATION_E2E_TARGET_OWNER_Y")
    with pytest.raises(ValueError, match="separate from SOURCE and TARGET owners"):
        dba_bootstrap.validate_dba_bootstrap_config(cfg)
    cfg = _executor_cfg()
    del cfg.raw["oracle_permission_migration"]["executor"]["password_secret"]
    with pytest.raises(ValueError, match="executor.username and executor.password_secret"):
        dba_bootstrap.validate_dba_bootstrap_config(cfg)


def test_permission_executor_authentication_is_verified_after_bootstrap_without_secret_output(monkeypatch):
    cfg = _executor_cfg()

    class Resolver:
        def resolve(self, name):
            assert name == "executor_x_secret"
            return "never-print-this"

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql):
            return None

        def fetchone(self):
            return ("PERMISSION_EXECUTOR_X", "TARGET_PDB_Y", "SERVICE_Y", "LOCAL_CDB_TEST")

        def fetchall(self):
            return []

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    class Driver:
        def connect(self, **kwargs):
            assert kwargs["user"] == "PERMISSION_EXECUTOR_X"
            assert kwargs["password"] == "never-print-this"
            return Conn()

        def makedsn(self, host, port, service_name):
            return f"{host}:{port}/{service_name}"

    monkeypatch.setattr(dba_bootstrap, "create_secret_provider", lambda *_a, **_k: Resolver())
    result = dba_bootstrap.verify_permission_executor(cfg, driver=Driver())
    assert result == {
        "status": "PASS",
        "username": "PERMISSION_EXECUTOR_X",
        "container": "TARGET_PDB_Y",
        "service": "SERVICE_Y",
        "catalog_access": "PASS",
    }


def test_permission_executor_bad_secret_is_reported_without_driver_secret_details(monkeypatch):
    cfg = _executor_cfg()

    class Resolver:
        def resolve(self, _name):
            return "hidden-password"

    class Driver:
        def connect(self, **_kwargs):
            raise RuntimeError("ORA-01017: hidden-password was rejected")

        def makedsn(self, *_args, **_kwargs):
            return "configured-target-dsn"

    monkeypatch.setattr(dba_bootstrap, "create_secret_provider", lambda *_a, **_k: Resolver())
    with pytest.raises(RuntimeError, match="could not authenticate after bootstrap") as exc:
        dba_bootstrap.verify_permission_executor(cfg, driver=Driver())
    assert "hidden-password" not in str(exc.value)
