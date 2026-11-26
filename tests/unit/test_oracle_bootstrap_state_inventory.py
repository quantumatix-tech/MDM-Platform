from __future__ import annotations

import os
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.oracle import inspect_bootstrap_state as inventory_cli
from tests.e2e.oracle.inspect_bootstrap_state import (
    _sqlplus_environment,
    _statement_for_section,
    build_inventory_sql,
    format_inventory_summary,
    run_inventory,
    run_sqlplus_script,
)


def _cfg():
    cfg = load_e2e_config("config/oracle_e2e_acceptance.yaml")
    cfg.raw["e2e"]["onboarding_profile"]["expected_db_unique_name"] = "LOCAL_CDB"
    cfg.raw["e2e"]["onboarding_profile"]["local_oracle_sid"] = "LOCAL_SID_TEST"
    return cfg


def test_inventory_is_read_only_and_checks_both_configured_pdbs():
    cfg = _cfg()
    sql = build_inventory_sql(cfg)
    assert 'ALTER SESSION SET CONTAINER="ORACLE_SOURCE_DB"' in sql
    assert 'ALTER SESSION SET CONTAINER="ORACLE_TARGET_DB"' in sql
    for view in ("dba_ts_quotas", "dba_data_files", "dba_sys_privs", "dba_role_privs", "dba_tab_privs", "dba_objects"):
        assert view in sql
    # Configured principal allowlists are the only grant/user scope. No global
    # CREATE USER/admin-option scan may pull SYS and unrelated system grants.
    assert "grantee IN (SELECT" not in sql
    assert "OR admin_option='YES'" not in sql
    assert "OR username IN" not in sql
    assert "OWNER_OBJECT|source|'||e.owner_name||'|'||NVL(o.object_type,'NONE')" in sql
    source_owner = cfg.raw["e2e"]["resources"]["source"]["name"]
    assert f"AS principal_label, '{source_owner}' AS principal_username" in sql
    assert "p.principal_label||'|'||p.principal_username" in sql
    assert re.search(r"(?im)^\s*(?:CREATE|GRANT|DROP|INSERT|UPDATE|DELETE)\b", sql) is None


def test_generated_catalog_sections_use_valid_26ai_dictionary_columns_and_explicit_aliases():
    sql = build_inventory_sql(
        _cfg(), source_admin="ORACLE_SOURCE_ADMIN", target_admin="ORACLE_TARGET_ADMIN",
        tablespaces=("E2E_APP_DATA",),
    )
    expected_sections = {
        "cdb_identity", "cdb_pdb_service_preflight", "source.pdb_status", "target.pdb_status",
        "source.switch_container", "source.context_assert", "source.context", "source.users",
        "source.tablespaces", "source.datafiles", "source.quotas", "source.system_grants",
        "source.role_grants", "source.object_grants", "source.owner_objects",
        "target.switch_container", "target.context_assert", "target.context", "target.users",
        "target.tablespaces", "target.datafiles", "target.quotas", "target.system_grants",
        "target.role_grants", "target.object_grants", "target.owner_objects",
    }
    found = set(re.findall(r"(?m)^PROMPT ORACLE_INVENTORY_SECTION\|([^\r\n]+)", sql))
    assert found == expected_sections
    # The principal inline view defines both columns that the outer query joins
    # and selects. USERNAME remains correct for DBA_USERS/DBA_TS_QUOTAS; grant
    # dictionary views are scoped by their documented GRANTEE column.
    source_users = _statement_for_section(sql, "source.users") or ""
    assert "AS principal_label" in source_users and "AS principal_username" in source_users
    assert "u.username=p.principal_username" in source_users
    assert "dba_sys_privs WHERE grantee IN" in sql
    assert "dba_role_privs WHERE grantee IN" in sql
    assert "dba_tab_privs WHERE grantee IN" in sql
    assert "dba_ts_quotas q" in sql and "q.username=e.username" in sql
    assert "WHERE ts.tablespace_name IN ('E2E_APP_DATA')" in sql
    assert "dba_data_files WHERE tablespace_name IN ('E2E_APP_DATA')" in sql
    assert "dba_tablespaces t ON t.tablespace_name=e.tablespace_name" in sql
    assert "dba_objects o ON o.owner=e.owner" in sql
    assert 'p."USERNAME"' not in sql.upper()


def test_oracle_00904_reports_exact_failed_inventory_section_statement_and_exit_code():
    runner = Mock(return_value=SimpleNamespace(
        returncode=904,
        stdout=(
            "CDB|LOCAL_CDB|CDB$ROOT\n"
            "PDB|source|ORACLE_SOURCE_DB|READ WRITE\n"
            "PDB|target|ORACLE_TARGET_DB|READ WRITE\n"
            "CTX|source|ORACLE_SOURCE_DB\n"
            "CTX|target|ORACLE_TARGET_DB\n"
            "ORACLE_INVENTORY_SECTION|source.users\n"
            'ORA-00904: "P"."USERNAME": invalid identifier\n'
        ),
        stderr="",
    ))
    with pytest.raises(RuntimeError) as error:
        run_inventory(_cfg(), executable="sqlplus", source_admin="ORACLE_SOURCE_ADMIN", runner=runner)
    message = str(error.value)
    assert "section=source.users" in message
    assert "SQL*Plus exit=904" in message
    assert 'ORA-00904: "P"."USERNAME": invalid identifier' in message
    assert "AS principal_username" in message
    assert "FROM (SELECT 'SOURCE_OWNER' AS principal_label" in message


def test_inventory_includes_runtime_verified_admins_and_expected_application_tablespace():
    sql = build_inventory_sql(
        _cfg(), source_admin="ORACLE_SOURCE_ADMIN", target_admin="ORACLE_TARGET_ADMIN",
        tablespaces=("E2E_APP_DATA",),
    )
    assert "ORACLE_SOURCE_ADMIN" in sql
    assert "ORACLE_TARGET_ADMIN" in sql
    assert "E2E_APP_DATA" in sql
    assert "NVL(t.contents,'MISSING')" in sql
    assert "NVL(TO_CHAR(q.bytes),'MISSING')" in sql
    assert "WHERE ts.tablespace_name IN ('E2E_APP_DATA')" in sql
    # Datafile filtering must use columns present in DBA_DATA_FILES.
    assert "FROM dba_data_files WHERE tablespace_name IN ('E2E_APP_DATA')" in sql
    assert "FROM dba_data_files WHERE contents" not in sql


def test_inventory_summary_reports_missing_owner_tablespace_and_quota_compactly():
    report = format_inventory_summary(
        "CDB|LOCAL_CDB|CDB$ROOT\n"
        "PDB|source|APP_SRC|READ WRITE\n"
        "CTX|source|APP_SRC\n"
        "USER|source|SOURCE_OWNER|MIGRATION_OWNER|MISSING|-|USERS|TEMP\n"
        "TABLESPACE|source|APP_DATA|MISSING|MISSING\n"
        "QUOTA|source|MIGRATION_OWNER|APP_DATA|MISSING|MISSING\n"
        "SYS_PRIV|source|MIGRATION_OWNER|CREATE SESSION|NO\n"
        "ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE\n"
    )
    assert "BOOTSTRAP STATUS: PARTIAL" in report
    assert "MIGRATION_OWNER account=MISSING" in report
    assert "APP_DATA application tablespace missing" in report
    assert "MIGRATION_OWNER quota missing on APP_DATA" in report
    assert "SYS_PRIV: 1 record(s)" in report


def test_inventory_admin_arguments_are_identifier_validated():
    with pytest.raises(ValueError, match="provisioning admin"):
        build_inventory_sql(_cfg(), source_admin="ADMIN; DROP USER X")


def test_cli_passes_admins_and_tablespace_through_to_read_only_inventory(monkeypatch, capsys):
    monkeypatch.setenv("ORACLE_SID", "FREE")
    cfg = _cfg()
    runner = Mock(return_value=SimpleNamespace(
        returncode=0,
        stdout=(
            "CDB|FREE|CDB$ROOT\n"
            "PDB|source|ORACLE_SOURCE_DB|READ WRITE\n"
            "PDB|target|ORACLE_TARGET_DB|READ WRITE\n"
            "CTX|source|ORACLE_SOURCE_DB\n"
            "CTX|target|ORACLE_TARGET_DB\n"
            "USER|source|SOURCE_ADMIN|ORACLE_SOURCE_ADMIN|OPEN|NO|E2E_APP_DATA|TEMP\n"
            "USER|target|TARGET_ADMIN|ORACLE_TARGET_ADMIN|OPEN|NO|E2E_APP_DATA|TEMP\n"
            "TABLESPACE|source|E2E_APP_DATA|PERMANENT|ONLINE\n"
            "TABLESPACE|target|E2E_APP_DATA|PERMANENT|ONLINE\n"
            "SYS_PRIV|source|ORACLE_SOURCE_ADMIN|CREATE USER|NO\n"
            "SYS_PRIV|target|ORACLE_TARGET_ADMIN|CREATE USER|NO\n"
            "ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE\n"
        ),
        stderr="",
    ))
    monkeypatch.setattr(inventory_cli.subprocess, "run", runner)

    result = inventory_cli.main([
        "--config", "config/oracle_e2e_acceptance.yaml",
        "--sqlplus", "sqlplus",
        "--source-admin", "ORACLE_SOURCE_ADMIN",
        "--target-admin", "ORACLE_TARGET_ADMIN",
        "--tablespace", "E2E_APP_DATA",
    ])

    assert result == 0
    sql = runner.call_args.kwargs["input"]
    assert "ORACLE_SOURCE_ADMIN" in sql
    assert "ORACLE_TARGET_ADMIN" in sql
    assert "E2E_APP_DATA" in sql
    source_owner = cfg.raw["e2e"]["resources"]["source"]["name"]
    target_owner = cfg.raw["e2e"]["resources"]["target"]["name"]
    assert f"grantee IN ('{source_owner}','ORACLE_SOURCE_ADMIN')" in sql
    executor = cfg.raw["oracle_permission_migration"]["executor"]["username"]
    assert f"grantee IN ('{target_owner}','ORACLE_TARGET_ADMIN','{executor}')" in sql
    assert "OR grantee IN (SELECT" not in sql
    assert "OR ADMIN_OPTION='YES'" not in sql.upper()
    assert re.search(r"(?im)^\s*(?:CREATE|GRANT|DROP|INSERT|UPDATE|DELETE|TRUNCATE)\b", sql) is None
    output = capsys.readouterr().out
    assert "ORACLE READ-ONLY BOOTSTRAP INVENTORY" in output
    assert "ORACLE_SOURCE_ADMIN" in output
    assert "ORACLE_TARGET_ADMIN" in output
    assert "E2E_APP_DATA" in output
    assert "SYS_PRIV|source|ORACLE_SOURCE_ADMIN|CREATE USER|NO" in output


def test_inventory_is_generic_for_other_configured_names_and_refuses_prohibited_pdb():
    cfg = _cfg()
    cfg.raw["e2e"]["oracle_test_owner_prefix"] = "MIGRATIONE2E_ORACLE_"
    cfg.raw["source"]["connection"]["service_name"] = "APP_SRC_DEV"
    cfg.raw["target"]["connection"]["service_name"] = "APP_TGT_DEV"
    cfg.raw["e2e"]["resources"]["source"]["name"] = "MIGRATIONE2E_ORACLE_APP_SRC"
    cfg.raw["e2e"]["resources"]["source"]["safe_pattern"] = "^MIGRATIONE2E_ORACLE_APP_SRC$"
    cfg.raw["e2e"]["resources"]["target"]["name"] = "MIGRATIONE2E_ORACLE_APP_TGT"
    cfg.raw["e2e"]["resources"]["target"]["safe_pattern"] = "^MIGRATIONE2E_ORACLE_APP_TGT$"
    cfg.raw["e2e"]["owner_provisioning"]["source"]["expected_container_name"] = "APP_SRC_DEV"
    cfg.raw["e2e"]["owner_provisioning"]["target"]["expected_container_name"] = "APP_TGT_DEV"
    sql = build_inventory_sql(cfg)
    assert "APP_SRC_DEV" in sql and "APP_TGT_DEV" in sql
    assert "MIGRATIONE2E_ORACLE_APP_SRC" in sql and "MIGRATIONE2E_ORACLE_APP_TGT" in sql
    cfg.raw["target"]["connection"]["service_name"] = "FREEPDB1"
    with pytest.raises(ValueError, match="prohibited"):
        build_inventory_sql(cfg)


def test_inventory_requires_actual_pdb_and_context_records():
    cfg = _cfg()
    runner = Mock(return_value=SimpleNamespace(
        returncode=0,
        stdout=(
            "CDB|LOCAL_CDB|CDB$ROOT\n"
            "PDB|source|ORACLE_SOURCE_DB|READ WRITE\n"
            "PDB|target|ORACLE_TARGET_DB|READ WRITE\n"
            "CTX|source|ORACLE_SOURCE_DB\n"
            "CTX|target|ORACLE_TARGET_DB\n"
            "ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE\n"
        ),
        stderr="",
    ))
    output = run_inventory(cfg, executable="sqlplus", runner=runner)
    assert "ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE" in output
    sql = runner.call_args.kwargs["input"]
    assert re.search(r"(?im)^\s*(?:CREATE|GRANT|DROP|INSERT|UPDATE|DELETE)\b", sql) is None
    runner.return_value.stdout = (
        "CDB|LOCAL_CDB|CDB$ROOT\nORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE\n"
    )
    with pytest.raises(RuntimeError, match="omitted a configured PDB"):
        run_inventory(cfg, executable="sqlplus", runner=runner)


def test_inventory_surfaces_sanitized_sqlplus_failure():
    cfg = _cfg()
    runner = Mock(return_value=SimpleNamespace(
        returncode=942,
        stdout=(
            "ORACLE_INVENTORY_SECTION|source.users\n"
            'ORA-00942: table or view "SYS"."DBA_USERS" does not exist\n'
        ),
        stderr="",
    ))
    with pytest.raises(RuntimeError, match="ORA-00942") as caught:
        run_inventory(cfg, executable="sqlplus", runner=runner)
    assert "section=source.users" in str(caught.value)
    assert "SQL*Plus exit=942" in str(caught.value)
    assert "AS principal_username" in str(caught.value)


def test_inventory_cli_counts_catalog_query_failure_as_failure(monkeypatch, capsys):
    monkeypatch.setenv("ORACLE_SID", "FREE")
    monkeypatch.setattr(inventory_cli.shutil, "which", lambda _name: "sqlplus")
    monkeypatch.setattr(
        inventory_cli.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=942,
                stdout=(
                    "ORACLE_INVENTORY_SECTION|source.users\n"
                    'ORA-00942: table or view "SYS"."DBA_USERS" does not exist\n'
                ),
                stderr="",
            )
        ),
    )

    result = inventory_cli.main(
        ["--config", "config/oracle_e2e_acceptance.yaml", "--sqlplus", "sqlplus"]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "FAIL" in error
    assert "ORA-00942" in error
    assert "section=source.users" in error
    assert "SQL*Plus exit=942" in error


def test_sqlplus_home_is_derived_from_detected_install_only_for_child(monkeypatch, tmp_path):
    oracle_home = tmp_path / "oracle-home"
    executable = oracle_home / "bin" / "sqlplus.exe"
    (oracle_home / "bin").mkdir(parents=True)
    (oracle_home / "sqlplus" / "mesg").mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    monkeypatch.delenv("ORACLE_HOME", raising=False)
    monkeypatch.setenv("ORACLE_SID", "LOCAL_SID_TEST")
    monkeypatch.setenv("PATH", "")
    env = _sqlplus_environment(str(executable))
    assert env["ORACLE_HOME"] == str(oracle_home)
    assert env["PATH"].startswith(str(oracle_home / "bin"))
    assert "ORACLE_HOME" not in os.environ


def test_local_sqlplus_does_not_fall_back_to_orcl_when_instance_sid_is_missing(monkeypatch):
    monkeypatch.delenv("ORACLE_SID", raising=False)
    with pytest.raises(RuntimeError, match="requires ORACLE_SID") as exc:
        _sqlplus_environment("sqlplus")
    assert "OracleServiceORCL" not in str(exc.value)


def test_configured_sid_is_child_only_and_sqlplus_context_is_preserved(monkeypatch):
    monkeypatch.delenv("ORACLE_SID", raising=False)
    monkeypatch.setenv("LOCAL", "configured-local-alias")
    monkeypatch.setenv("TNS_ADMIN", "configured-tns-directory")
    captured = {}

    def runner(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE\n",
            stderr="",
        )

    result = run_sqlplus_script(
        "PROMPT ORACLE_INVENTORY_SECTION|cdb_identity\nSELECT 1 FROM dual;\n",
        operation="read-only test",
        completion_marker="ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE",
        executable="sqlplus",
        oracle_sid="APPROVED_SID_X",
        runner=runner,
    )
    assert "ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE" in result
    assert captured["env"]["ORACLE_SID"] == "APPROVED_SID_X"
    assert captured["env"]["LOCAL"] == "configured-local-alias"
    assert captured["env"]["TNS_ADMIN"] == "configured-tns-directory"
    assert "ORACLE_SID" not in os.environ
