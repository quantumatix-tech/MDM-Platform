from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle import inspect_bootstrap_state, live_launcher
from tests.unit.oracle_test_settings import load_oracle_test_config


def _profile():
    return {
        "enabled": True,
        "required_owner_privileges": "fixture_derived",
        "expected_db_unique_name": "CDB_LOCAL",
        "local_oracle_sid": "CDBSID_X",
        "source": {
            "admin_connection": {"username": "SOURCE_ADMIN_X", "password_secret": "source_admin_x", "secret_provider": "env"},
            "permanent_tablespace": "APP_SOURCE_DATA",
            "quota_bytes": 4096,
            "storage": {"mode": "reuse_existing"},
        },
        "target": {
            "admin_connection": {"username": "TARGET_ADMIN_Y", "password_secret": "target_admin_y", "secret_provider": "env"},
            "permanent_tablespace": "APP_TARGET_DATA",
            "quota_bytes": 8192,
            "storage": {"mode": "reuse_existing"},
        },
    }


def _config():
    raw = load_oracle_test_config()
    raw["source"]["connection"]["service_name"] = "APP_SOURCE_SERVICE"
    raw["target"]["connection"]["service_name"] = "APP_TARGET_SERVICE"
    raw["e2e"]["owner_provisioning"] = {
        "enabled": False,
        "source": {"expected_container_name": "SOURCE_CONTAINER_X"},
        "target": {"expected_container_name": "TARGET_CONTAINER_Y"},
    }
    raw["e2e"]["onboarding_profile"] = _profile()
    return E2EConfig(raw)


def _facts(cfg=None):
    cfg = cfg or _config()
    result = {
        "db_unique_name": "CDB_LOCAL",
        "pdbs": {},
        "contexts": {},
        "admins": {},
        "owners": {},
        "omf": {},
        "tablespaces": {"source": [], "target": []},
        "datafiles": {"source": [], "target": []},
        "privileges": {"source": set(), "target": set()},
        "catalog_views": {"source": set(), "target": set()},
        "source_owner_catalog_views": set(),
        "target_owner_catalog_views": set(),
        "owner_privileges": {"source": set(), "target": set()},
        "quotas": {"source": None, "target": None},
    }
    for side in ("source", "target"):
        container = cfg.raw["e2e"]["owner_provisioning"][side]["expected_container_name"]
        service = cfg.raw[side]["connection"]["service_name"]
        admin = cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"]
        owner = cfg.raw["e2e"]["resources"][side]["name"]
        ts = cfg.raw["e2e"]["onboarding_profile"][side]["permanent_tablespace"]
        result["pdbs"][side] = {"name": container, "mode": "READ WRITE", "service": service}
        result["contexts"][side] = container
        result["admins"][side] = {"username": admin, "status": "OPEN", "common": "NO"}
        result["owners"][side] = {"username": owner, "status": "MISSING", "common": "NO", "objects": 0, "default_tablespace": ""}
        result["quotas"][side] = {"owner": owner, "tablespace": ts, "max_bytes": "MISSING"}
        result["tablespaces"][side] = [{"name": ts, "contents": "PERMANENT", "status": "ONLINE", "file_count": 1}]
        result["datafiles"][side] = [{"tablespace": ts, "path": f"/approved/{side}.dbf", "status": "AVAILABLE"}]
        result["omf"][side] = ""
    if cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True:
        username = cfg.raw["oracle_permission_migration"]["executor"]["username"]
        result["permission_executor"] = {"username": username, "status": "MISSING", "common": "NO"}
        result["executor_privileges"] = set()
        result["executor_catalog_views"] = set()
    return result


def _discovery_output(
    cfg,
    *,
    include_source_admin=True,
    owner_states=None,
    owner_privileges=None,
    quota_values=None,
    executor_state=("MISSING", "NO"),
    include_tablespaces=True,
):
    owner_states = owner_states or {}
    owner_privileges = owner_privileges or {}
    quota_values = quota_values or {}
    parts = ["SQL*Plus: Release 23.0.0.0.0 - Production", "CDB|CDB_LOCAL"]
    for side in ("source", "target"):
        profile = cfg.raw["e2e"]["onboarding_profile"][side]
        container = cfg.raw["e2e"]["owner_provisioning"][side]["expected_container_name"]
        service = cfg.raw[side]["connection"]["service_name"]
        owner = cfg.raw["e2e"]["resources"][side]["name"]
        admin = profile["admin_connection"]["username"]
        ts = profile["permanent_tablespace"]
        owner_state = owner_states.get(side, {})
        owner_status = owner_state.get("status", "MISSING")
        owner_common = owner_state.get("common", "NO")
        object_count = owner_state.get("objects", 0)
        default_tablespace = owner_state.get("default_tablespace", "")
        parts.extend([f"PDB|{side}|{container}|READ WRITE|{service}", f"CTX|{side}|{container}"])
        if side != "source" or include_source_admin:
            parts.append(f"ADMIN|{side}|{admin}|OPEN|NO")
        if side == "target" and cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True:
            executor = cfg.raw["oracle_permission_migration"]["executor"]["username"]
            parts.append(f"EXECUTOR|target|{executor}|{executor_state[0]}|{executor_state[1]}")
        parts.extend([
            f"OWNER|{side}|{owner}|{owner_status}|{owner_common}|{object_count}|{default_tablespace}",
            f"QUOTA|{side}|{owner}|{ts}|{quota_values.get(side, 'MISSING')}",
            f"OMF|{side}|",
            f"PRIV|{side}|CREATE USER|NO",
        ])
        parts.extend(
            f"CAT|{side}|{view}|SELECT"
            for view in live_launcher._ADMIN_CATALOG_VIEWS
        )
        if include_tablespaces:
            parts.extend([
                f"TS|{side}|{ts}|PERMANENT|ONLINE|1",
                f"DF|{side}|{ts}|/approved/{side}.dbf|AVAILABLE",
            ])
        parts.extend(f"OPRIV|{side}|{owner}|{privilege}" for privilege in owner_privileges.get(side, ()))
        if side == "source":
            parts.extend(
                f"OCAT|source|{view}"
                for view in cfg.raw.get("_test_source_owner_catalog_views", ())
            )
        else:
            parts.extend(
                f"TCAT|target|{view}"
                for view in cfg.raw.get("_test_target_owner_catalog_views", ())
            )
        if side == "target" and cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True:
            executor = cfg.raw["oracle_permission_migration"]["executor"]["username"]
            parts.append(f"EXPRIV|target|{executor}|CREATE SESSION")
            parts.append(f"ECAT|target|{executor}|DBA_USERS")
    parts.append("ORACLE_READ_ONLY_DISCOVERY_COMPLETE")
    return "\r\n".join(parts) + "\r\n"


def test_profile_requires_explicit_approved_storage_quota_and_fixture_derived_privileges():
    cfg = _config()
    cfg.raw["e2e"]["onboarding_profile"]["source"]["quota_bytes"] = None
    with pytest.raises(RuntimeError, match="source.quota_bytes"):
        live_launcher._onboarding_profile(cfg)
    cfg.raw["e2e"]["onboarding_profile"] = _profile()
    cfg.raw["e2e"]["onboarding_profile"]["required_owner_privileges"] = ["CREATE TABLE"]
    with pytest.raises(RuntimeError, match="fixture_derived"):
        live_launcher._onboarding_profile(cfg)
    cfg.raw["e2e"]["onboarding_profile"] = _profile()
    cfg.raw["e2e"]["onboarding_profile"].pop("expected_db_unique_name")
    with pytest.raises(RuntimeError, match="expected_db_unique_name"):
        live_launcher._onboarding_profile(cfg)


def test_read_only_discovery_is_scoped_and_contains_no_database_ddl():
    cfg = _config()
    admins = {side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"] for side in ("source", "target")}
    sql = live_launcher._discovery_sql(cfg, admins)
    upper = sql.upper()
    for forbidden in ("CREATE ", "DROP ", "GRANT ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in upper
    assert "SOURCE_CONTAINER_X" in sql and "TARGET_CONTAINER_Y" in sql
    assert "APP_SOURCE_SERVICE" in sql and "APP_TARGET_SERVICE" in sql
    assert "APP_SOURCE_DATA" in sql and "APP_TARGET_DATA" in sql
    assert "CDB_LOCAL" in sql and "db_unique_name" in sql.lower()
    assert "dba_ts_quotas" in sql.lower() and "common" in sql.lower() and "default_tablespace" in sql.lower()
    assert "FREEPDB1" not in upper and "MIGRATION_DB" not in upper


def test_discovery_checks_every_catalog_view_required_for_owner_recovery():
    cfg = _config()
    admins = {
        side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"]
        for side in ("source", "target")
    }
    sql = live_launcher._discovery_sql(cfg, admins)
    catalog_query = inspect_bootstrap_state._statement_for_section(
        sql, "source.admin_catalog"
    )
    assert catalog_query is not None
    for view in live_launcher._ADMIN_CATALOG_VIEWS:
        assert f"'{view}'" in catalog_query

    facts = _facts(cfg)
    facts["catalog_views"]["source"] = {
        "DBA_USERS", "DBA_TABLESPACES", "DBA_DATA_FILES", "DBA_OBJECTS"
    }
    facts["catalog_views"]["target"] = set(live_launcher._ADMIN_CATALOG_VIEWS)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["admin_catalog_reads_to_grant"] == ["DBA_SYS_PRIVS", "DBA_TS_QUOTAS"]
    assert plan["target"]["admin_catalog_reads_to_grant"] == []


def test_source_owner_permission_catalog_grants_are_in_exact_approval_plan():
    cfg = _config()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "EXECUTOR_X", "password_secret": "executor_x_secret"},
    }
    facts = _facts(cfg)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["owner_catalog_reads_to_grant"] == [
        "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS", "DBA_USERS"
    ]
    facts["source_owner_catalog_views"] = {"DBA_USERS", "DBA_ROLES"}
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["owner_catalog_reads_to_grant"] == [
        "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS"
    ]
    facts["source_owner_catalog_views"] = set(live_launcher._SOURCE_OWNER_PERMISSION_CATALOG_VIEWS)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["owner_catalog_reads_to_grant"] == []
    facts["source_owner_catalog_views"] = set()
    facts["privileges"]["source"] = {("SELECT ANY DICTIONARY", "NO")}
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["owner_catalog_reads_to_grant"] == [
        "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS", "DBA_USERS"
    ]
    facts["owner_privileges"]["source"] = {"SELECT ANY DICTIONARY"}
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["owner_catalog_reads_to_grant"] == []


def test_source_owner_catalog_grant_discovery_is_read_only_and_dynamic():
    cfg = _config()
    cfg.raw["_test_source_owner_catalog_views"] = ["DBA_USERS"]
    output = _discovery_output(cfg)
    parsed = live_launcher._parse_discovery_records(output)
    assert [row for row in parsed if row[0] == "OCAT"] == [["OCAT", "source", "DBA_USERS"]]
    admins = {side: f"{side}_ADMIN" for side in ("source", "target")}
    sql = live_launcher._discovery_sql(cfg, admins)
    statement = inspect_bootstrap_state._statement_for_section(sql, "source.owner_catalog")
    assert statement is not None
    assert "GRANTEE='MIGRATION_E2E_SOURCE'" in statement.upper()
    assert "DBA_USERS" in statement and "DBA_ROLE_PRIVS" in statement
    assert "GRANT " not in statement.upper()


def test_target_replacement_catalog_grants_are_discovered_and_included_in_approval_plan():
    cfg = _config()
    cfg.raw["_test_target_owner_catalog_views"] = ["DBA_CONSTRAINTS"]
    output = _discovery_output(cfg)
    parsed = live_launcher._parse_discovery_records(output)
    assert [row for row in parsed if row[0] == "TCAT"] == [
        ["TCAT", "target", "DBA_CONSTRAINTS"]
    ]
    admins = {side: f"{side.upper()}_ADMIN" for side in ("source", "target")}
    sql = live_launcher._discovery_sql(cfg, admins)
    statement = inspect_bootstrap_state._statement_for_section(sql, "target.owner_catalog")
    assert statement is not None
    assert "GRANTEE='MIGRATION_E2E_TARGET'" in statement.upper()
    assert "DBA_CONSTRAINTS" in statement and "DBA_DEPENDENCIES" in statement
    assert "GRANT " not in statement.upper()

    facts = _facts(cfg)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["target"]["owner_catalog_reads_to_grant"] == [
        "DBA_CONSTRAINTS", "DBA_DEPENDENCIES"
    ]

    facts["target_owner_catalog_views"] = {"DBA_CONSTRAINTS"}
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["target"]["owner_catalog_reads_to_grant"] == ["DBA_DEPENDENCIES"]

    facts["target_owner_catalog_views"] = set(live_launcher._TARGET_REPLACEMENT_CATALOG_VIEWS)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["target"]["owner_catalog_reads_to_grant"] == []


def test_discovery_owner_executor_and_quota_records_are_single_row_without_mixed_aggregates():
    cfg = _config()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "EXECUTOR_X", "password_secret": "executor_x_secret"},
    }
    admins = {side: f"{side.upper()}_ADMIN" for side in ("source", "target")}
    sql = live_launcher._discovery_sql(cfg, admins)

    sections = {
        name: inspect_bootstrap_state._statement_for_section(sql, name)
        for name in (
            "source.owner",
            "target.owner",
            "target.executor",
            "source.owner_quota",
            "target.owner_quota",
        )
    }
    assert all(statement for statement in sections.values())
    for statement in sections.values():
        assert "FROM dual" in statement
        assert "MAX(" not in statement.upper()
    assert "NVL((SELECT u.account_status" in sections["source.owner"]
    assert "NVL((SELECT u.default_tablespace" in sections["source.owner"]
    assert "TO_CHAR((SELECT COUNT(*) FROM dba_objects" in sections["source.owner"]
    assert "'MISSING'" in sections["source.owner"]
    assert "'MISSING'" in sections["target.executor"]
    assert "'MISSING'" in sections["source.owner_quota"]


def test_every_generated_discovery_section_has_a_statement_and_valid_aggregate_shape():
    cfg = _config()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "EXECUTOR_X", "password_secret": "executor_x_secret"},
    }
    admins = {side: f"{side.upper()}_ADMIN" for side in ("source", "target")}
    sql = live_launcher._discovery_sql(cfg, admins)
    section_names = re.findall(r"(?m)^PROMPT ORACLE_INVENTORY_SECTION\|([a-z0-9_.-]+)$", sql)
    assert section_names
    assert len(section_names) == len(set(section_names))

    statements = {
        name: inspect_bootstrap_state._statement_for_section(sql, name)
        for name in section_names
    }
    assert all(statement for statement in statements.values())
    for name, statement in statements.items():
        upper = statement.upper()
        if name.endswith("tablespaces"):
            assert "COUNT(D.FILE_ID)" in upper
            assert "GROUP BY T.TABLESPACE_NAME,T.CONTENTS,T.STATUS" in upper
        elif "MAX(" in upper:
            # Any future aggregate must be grouped; scalar-subquery metadata
            # records intentionally avoid aggregates so a missing principal
            # still produces exactly one DUAL row.
            assert "GROUP BY" in upper, name
        if upper.lstrip().startswith("SELECT"):
            assert not re.search(r"\b(?:CREATE|DROP|GRANT|REVOKE|INSERT|UPDATE|DELETE|TRUNCATE)\b", upper)


def test_discovery_classifies_existing_owner_and_executor_accounts(monkeypatch):
    cfg = _config()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "EXECUTOR_X", "password_secret": "executor_x_secret"},
    }
    owners = {
        "source": {
            "status": "OPEN", "common": "NO", "objects": 0,
            "default_tablespace": "APP_SOURCE_DATA",
        },
        "target": {
            "status": "MISSING", "common": "NO", "objects": 0,
            "default_tablespace": "",
        },
    }
    output = _discovery_output(
        cfg,
        owner_states=owners,
        owner_privileges={"source": {"CREATE SESSION", "CREATE TABLE"}},
        quota_values={"source": "4096"},
        executor_state=("OPEN", "NO"),
    )
    monkeypatch.setattr(inspect_bootstrap_state.shutil, "which", lambda _name: "sqlplus")
    monkeypatch.setattr(live_launcher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=output, stderr=""))
    admins = {side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"] for side in ("source", "target")}

    facts = live_launcher._discover(cfg, admins)

    assert facts["owners"]["source"] == {
        "username": "MIGRATION_E2E_SOURCE", "status": "OPEN", "common": "NO",
        "objects": 0, "default_tablespace": "APP_SOURCE_DATA",
    }
    assert facts["owners"]["target"]["status"] == "MISSING"
    assert facts["permission_executor"]["status"] == "OPEN"
    assert len(facts["tablespaces"]["source"]) == len(facts["tablespaces"]["target"]) == 1
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["owner_setup"]["action"] == "reuse_empty_owner"
    assert plan["target"]["owner_setup"]["action"] == "create_missing_owner"
    assert plan["source"]["storage"]["action"] == "reuse"
    assert plan["target"]["storage"]["action"] == "reuse"


def test_discovery_classifies_partial_owner_provisioning_without_reset(monkeypatch):
    cfg = _config()
    source_owner = cfg.raw["e2e"]["resources"]["source"]["name"]
    output = _discovery_output(
        cfg,
        owner_states={"source": {
            "status": "OPEN", "common": "NO", "objects": 0,
            "default_tablespace": "APP_SOURCE_DATA",
        }},
        owner_privileges={"source": {"CREATE SESSION"}},
        quota_values={"source": "4096"},
    )
    monkeypatch.setattr(inspect_bootstrap_state.shutil, "which", lambda _name: "sqlplus")
    monkeypatch.setattr(live_launcher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=output, stderr=""))
    admins = {side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"] for side in ("source", "target")}

    facts = live_launcher._discover(cfg, admins)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)

    assert facts["owners"]["source"]["username"] == source_owner
    assert plan["source"]["owner_setup"]["action"] == "complete_empty_owner_setup"
    assert plan["source"]["owner_setup"]["missing_privileges_to_grant"] == ["CREATE TABLE"]
    assert plan["target"]["owner_setup"]["action"] == "create_missing_owner"


def test_empty_tablespace_discovery_plans_only_approved_storage_creation(monkeypatch):
    cfg = _config()
    for side in ("source", "target"):
        cfg.raw["e2e"]["onboarding_profile"][side]["storage"] = {
            "mode": "explicit_path",
            "size": "1M",
            "datafile_path": f"/approved/{side}.dbf",
        }
    output = _discovery_output(cfg, include_tablespaces=False)
    monkeypatch.setattr(inspect_bootstrap_state.shutil, "which", lambda _name: "sqlplus")
    monkeypatch.setattr(live_launcher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=output, stderr=""))
    admins = {side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"] for side in ("source", "target")}

    facts = live_launcher._discover(cfg, admins)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)

    assert facts["tablespaces"] == {"source": [], "target": []}
    assert plan["source"]["storage"]["action"] == "create_if_missing"
    assert plan["target"]["storage"]["action"] == "create_if_missing"
    assert plan["source"]["owner_setup"]["action"] == "create_missing_owner"
    assert plan["target"]["owner_setup"]["action"] == "create_missing_owner"


def test_read_only_discovery_refuses_prohibited_pdb_names():
    cfg = _config()
    cfg.raw["e2e"]["owner_provisioning"]["source"]["expected_container_name"] = "FREEPDB1"
    admins = {"source": "SOURCE_ADMIN_X", "target": "TARGET_ADMIN_Y"}
    with pytest.raises(RuntimeError, match="prohibited PDB"):
        live_launcher._discovery_sql(cfg, admins)


def test_discovery_parser_requires_both_verified_pdbs(monkeypatch):
    cfg = _config()
    admins = {side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"] for side in ("source", "target")}
    output = _discovery_output(cfg)
    monkeypatch.setattr(inspect_bootstrap_state.shutil, "which", lambda _name: "sqlplus")
    monkeypatch.setattr(live_launcher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=output, stderr=""))
    result = live_launcher._discover(cfg, admins)
    assert result["db_unique_name"] == "CDB_LOCAL"
    assert result["owners"]["source"]["status"] == "MISSING"
    assert result["owners"]["source"]["default_tablespace"] == ""
    assert result["quotas"]["source"]["max_bytes"] == "MISSING"
    assert result["tablespaces"]["target"][0]["name"] == "APP_TARGET_DATA"
    assert ("CREATE USER", "NO") in result["privileges"]["source"]
    assert "DBA_USERS" in result["catalog_views"]["source"]
    assert result["catalog_views"]["source"] == set(live_launcher._ADMIN_CATALOG_VIEWS)


def test_discovery_fails_closed_when_admin_record_is_missing(monkeypatch):
    cfg = _config()
    admins = {side: cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"]["username"] for side in ("source", "target")}
    monkeypatch.setattr(inspect_bootstrap_state.shutil, "which", lambda _name: "sqlplus")
    monkeypatch.setattr(live_launcher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=_discovery_output(cfg, include_source_admin=False), stderr=""))
    with pytest.raises(RuntimeError, match="both configured ADMIN records"):
        live_launcher._discover(cfg, admins)


def test_alternate_pdb_admin_and_service_names_flow_into_discovery_sql():
    cfg = _config()
    cfg.raw["e2e"]["owner_provisioning"]["source"]["expected_container_name"] = "CLIENT_SRC_PDB"
    cfg.raw["e2e"]["owner_provisioning"]["target"]["expected_container_name"] = "CLIENT_DST_PDB"
    cfg.raw["source"]["connection"]["service_name"] = "CLIENT_SRC_SERVICE"
    cfg.raw["target"]["connection"]["service_name"] = "CLIENT_DST_SERVICE"
    cfg.raw["e2e"]["onboarding_profile"]["source"]["admin_connection"]["username"] = "CLIENT_SRC_ADMIN"
    cfg.raw["e2e"]["onboarding_profile"]["target"]["admin_connection"]["username"] = "CLIENT_DST_ADMIN"
    cfg.raw["e2e"]["onboarding_profile"]["expected_db_unique_name"] = "CLIENT_CDB"
    admins = {"source": "CLIENT_SRC_ADMIN", "target": "CLIENT_DST_ADMIN"}
    sql = live_launcher._discovery_sql(cfg, admins)
    assert 'ALTER SESSION SET CONTAINER="CLIENT_SRC_PDB"' in sql
    assert 'ALTER SESSION SET CONTAINER="CLIENT_DST_PDB"' in sql
    assert "CLIENT_SRC_ADMIN" in sql and "CLIENT_DST_ADMIN" in sql
    assert "CLIENT_SRC_SERVICE" in sql and "CLIENT_DST_SERVICE" in sql
    assert "CLIENT_CDB" in sql and "FREE" not in sql


def test_plan_rejects_a_cdb_identity_that_differs_from_configuration():
    cfg = _config()
    facts = _facts(cfg)
    facts["db_unique_name"] = "UNEXPECTED_CDB"
    with pytest.raises(RuntimeError, match="does not match configured CDB"):
        live_launcher._build_execution_plan(cfg, cfg.raw, facts)


def test_plan_lists_exact_missing_resources_without_secrets():
    cfg = _config()
    raw = cfg.raw
    facts = _facts(cfg)
    plan = live_launcher._build_execution_plan(cfg, raw, facts)
    assert plan["source"]["storage"]["action"] == "reuse"
    assert plan["target"]["storage"]["action"] == "reuse"
    assert plan["source"]["owner_setup"]["action"] == "create_missing_owner"
    assert plan["source"]["owner_setup"]["required_privileges"] == ["CREATE SESSION", "CREATE TABLE"]
    assert "password" not in str(plan).casefold()
    assert "SOURCE_CONTAINER_X" in str(plan) and "TARGET_CONTAINER_Y" in str(plan)
    assert live_launcher._plan_token(plan) == live_launcher._plan_token(live_launcher._build_execution_plan(cfg, raw, facts))


def test_partial_bootstrap_plan_contains_only_resources_still_missing():
    cfg = _config()
    facts = _facts(cfg)
    facts["privileges"]["source"] = {("CREATE TABLE", "YES"), ("CREATE SESSION", "YES")}
    facts["privileges"]["target"] = {("CREATE USER", "NO"), ("CREATE TABLE", "YES"), ("CREATE SESSION", "YES")}
    facts["catalog_views"]["source"] = set(live_launcher._ADMIN_CATALOG_VIEWS)
    facts["catalog_views"]["target"] = set(live_launcher._ADMIN_CATALOG_VIEWS)
    source = cfg.raw["e2e"]["resources"]["source"]["name"]
    facts["owners"]["source"] = {"username": source, "status": "OPEN", "common": "NO", "objects": 0, "default_tablespace": "APP_SOURCE_DATA"}
    facts["owner_privileges"]["source"] = {"CREATE TABLE", "CREATE SESSION"}
    facts["quotas"]["source"]["max_bytes"] = "4096"
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["admin_privileges_to_grant"] == []
    assert plan["source"]["admin_catalog_reads_to_grant"] == []
    assert plan["source"]["owner_setup"]["action"] == "reuse_empty_owner"
    assert plan["source"]["owner_setup"]["missing_privileges_to_grant"] == []
    assert plan["target"]["owner_setup"]["action"] == "create_missing_owner"
    facts["owner_privileges"]["source"] = {"CREATE SESSION"}
    partial_owner_plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert partial_owner_plan["source"]["owner_setup"]["action"] == "complete_empty_owner_setup"
    assert partial_owner_plan["source"]["owner_setup"]["missing_privileges_to_grant"] == ["CREATE TABLE"]


def test_existing_broad_admin_authority_is_reported_but_not_granted_again():
    cfg = _config()
    facts = _facts(cfg)
    for side in ("source", "target"):
        facts["privileges"][side] = {("CREATE USER", "NO"), ("GRANT ANY PRIVILEGE", "NO"), ("SELECT ANY DICTIONARY", "NO")}
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    assert plan["source"]["admin_privileges_to_grant"] == []
    assert plan["source"]["admin_catalog_reads_to_grant"] == []
    assert plan["source"]["existing_grant_any_privilege_preserved"] is True
    assert plan["source"]["existing_select_any_dictionary_preserved"] is True


def test_permission_migration_scope_and_executor_gaps_are_in_approved_plan():
    cfg = _config()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "executor": {"username": "CLIENT_SECURITY_EXECUTOR", "password_secret": "executor_secret"},
        "new_user_policy": "LOCKED",
        "approve_role_creation_admin_option": True,
        "approve_create_session": True,
    }
    facts = _facts(cfg)
    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    permissions = plan["permission_migration"]
    assert permissions["executor_action"] == "create_missing_executor"
    assert permissions["executor_system_privileges_to_grant"] == ["CREATE SESSION"]
    assert permissions["executor_catalog_reads_to_grant"] == sorted(live_launcher._PERMISSION_CATALOG_VIEWS)
    assert permissions["source_owner_scope"] == cfg.raw["e2e"]["resources"]["source"]["name"]
    assert "role memberships" in permissions["migration_scope"]


def test_plan_change_after_approval_requires_new_approval():
    plan = {"source": {"owner": "ONE"}}
    changed = {"source": {"owner": "TWO"}}
    with pytest.raises(RuntimeError, match="fresh approval"):
        live_launcher._assert_plan_unchanged(plan, changed)


def test_launcher_stops_before_runner_when_read_only_plan_changes(monkeypatch):
    cfg = _config()
    facts = _facts(cfg)
    approvals = []
    plan_calls = iter([{"plan": "first"}, {"plan": "first"}, {"plan": "changed"}])
    monkeypatch.setattr(live_launcher.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(live_launcher.sys, "argv", ["live_launcher.py", "acceptance.yaml"])
    monkeypatch.setattr(live_launcher, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setattr(live_launcher.os, "chdir", lambda _path: None)
    monkeypatch.setitem(live_launcher.sys.modules, "oracledb", object())
    monkeypatch.setattr(live_launcher, "_discover", lambda *_a, **_kw: facts)
    monkeypatch.setattr(live_launcher, "_new_runtime_config", lambda *_a, **_kw: cfg.raw)
    monkeypatch.setattr(live_launcher, "validate_onboarding_config", lambda _cfg: None)
    monkeypatch.setattr(live_launcher, "validate_dba_bootstrap_config", lambda _cfg: None)
    monkeypatch.setattr(live_launcher, "_build_execution_plan", lambda *_a: next(plan_calls))
    monkeypatch.setattr(live_launcher, "_approve_execution_plan", lambda plan: approvals.append(plan))
    monkeypatch.setattr(live_launcher.subprocess, "run", lambda *_a, **_kw: pytest.fail("E2E runner must not execute"))
    with pytest.raises(RuntimeError, match="plan hash changed"):
        live_launcher.main()
    assert approvals == [{"plan": "first"}]


def test_populated_target_catalog_grant_mode_only_issues_reviewed_missing_grants(monkeypatch):
    cfg = _config()
    facts_before = _facts(cfg)
    owner = cfg.raw["e2e"]["resources"]["target"]["name"]
    facts_before["owners"]["target"] = {
        "username": owner, "status": "OPEN", "common": "NO", "objects": 2,
        "default_tablespace": "APP_TARGET_DATA",
    }
    # OPRIV discovery emits one privilege-name string per row, unlike PRIV,
    # which carries (privilege, admin_option) pairs.
    facts_before["owner_privileges"] = {
        "source": {"CREATE TABLE"},
        "target": {"CREATE TABLE"},
    }
    facts_before["target_owner_catalog_views"] = set()
    facts_after = _facts(cfg)
    facts_after["owners"]["target"] = dict(facts_before["owners"]["target"])
    facts_after["target_owner_catalog_views"] = set(
        live_launcher._TARGET_REPLACEMENT_CATALOG_VIEWS
    )
    discoveries = iter([facts_before, facts_before, facts_after])
    approvals = []
    grants = []

    class Driver:
        pass

    monkeypatch.setattr(live_launcher.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(live_launcher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(live_launcher, "load_e2e_config", lambda _path: cfg)
    monkeypatch.setitem(live_launcher.sys.modules, "oracledb", Driver())
    monkeypatch.setattr(live_launcher, "_discover", lambda *_a, **_kw: next(discoveries))
    monkeypatch.setattr(
        live_launcher, "_resolve_secret",
        lambda *_a, **_kw: ("offline-secret", False),
    )
    monkeypatch.setattr(live_launcher, "_probe_login", lambda *_a, **_kw: None)
    monkeypatch.setattr(live_launcher, "_approve_execution_plan", lambda plan: approvals.append(plan))

    def apply_target_grants(_runtime_cfg, *, apply, executable=None):
        assert apply is True
        assert executable is None
        grants.append("target-catalog-only")

    monkeypatch.setattr(
        "tests.e2e.oracle.dba_bootstrap.grant_target_owner_catalog_access",
        apply_target_grants,
    )

    assert live_launcher.main([
        "--config", "config/oracle_e2e_acceptance.yaml",
        "--target-catalog-grants-only",
    ]) == 0
    assert len(approvals) == 1
    assert approvals[0]["target"]["direct_select_grants_to_add"] == [
        "SELECT ON SYS.DBA_CONSTRAINTS", "SELECT ON SYS.DBA_DEPENDENCIES",
    ]
    assert approvals[0]["target"]["pdb"] == "TARGET_CONTAINER_Y"
    assert approvals[0]["target"]["owner"] == owner
    assert "source" not in approvals[0]
    assert grants == ["target-catalog-only"]


def test_target_catalog_grant_mode_rejects_missing_or_nonlocal_owner():
    cfg = _config()
    facts = _facts(cfg)
    owner = cfg.raw["e2e"]["resources"]["target"]["name"]
    facts["owners"]["target"] = {
        "username": owner, "status": "MISSING", "common": "NO", "objects": 0,
    }
    with pytest.raises(RuntimeError, match="exist, be OPEN, and be PDB-local"):
        live_launcher._target_catalog_grant_plan(cfg, facts)


def test_owner_dictionary_privilege_fact_uses_oracle_privilege_name_shape():
    cfg = _config()
    facts = _facts(cfg)
    facts["owner_privileges"]["source"] = {"CREATE TABLE", "SELECT ANY DICTIONARY"}
    facts["owner_privileges"]["target"] = {"CREATE TABLE", "SELECT ANY DICTIONARY"}

    plan = live_launcher._build_execution_plan(cfg, cfg.raw, facts)

    assert plan["source"]["owner_catalog_reads_to_grant"] == []
    assert plan["target"]["owner_catalog_reads_to_grant"] == []


def test_incorrect_pdb_identity_fails_closed():
    cfg = _config()
    facts = _facts(cfg)
    facts["pdbs"]["source"]["name"] = "UNEXPECTED_PDB"
    with pytest.raises(RuntimeError, match="identity mismatch on SOURCE"):
        live_launcher._build_execution_plan(cfg, cfg.raw, facts)


def test_owner_nonempty_or_locked_account_is_never_planned_for_mutation():
    cfg = _config()
    facts = _facts(cfg)
    facts["owners"]["target"] = {"username": "MIGRATION_E2E_TARGET", "status": "OPEN", "common": "NO", "objects": 1, "default_tablespace": "APP_TARGET_DATA"}
    with pytest.raises(RuntimeError, match="not empty"):
        live_launcher._build_execution_plan(cfg, cfg.raw, facts)
    facts["owners"]["target"]["objects"] = 0
    facts["owners"]["target"]["status"] = "LOCKED"
    with pytest.raises(RuntimeError, match="OPEN and PDB-local"):
        live_launcher._build_execution_plan(cfg, cfg.raw, facts)


def test_executor_account_state_preserves_open_and_rejects_locked_or_common():
    assert live_launcher._permission_executor_creation_required({"username": "EXEC_X", "status": "MISSING", "common": "NO"}, "EXEC_X")
    assert not live_launcher._permission_executor_creation_required({"username": "exec_x", "status": "OPEN", "common": "NO"}, "EXEC_X")
    with pytest.raises(RuntimeError, match="not OPEN"):
        live_launcher._permission_executor_creation_required({"username": "EXEC_X", "status": "LOCKED", "common": "NO"}, "EXEC_X")


def test_exact_plan_requires_one_explicit_approval(monkeypatch, capsys):
    plan = {"source": {"pdb": "PDB_A"}, "target": {"pdb": "PDB_B"}}
    token = f"APPLY-{live_launcher._plan_token(plan)}"
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or token)
    assert live_launcher._approve_execution_plan(plan) == token
    assert len(prompts) == 1
    assert token in prompts[0]
    assert "PDB_A" in capsys.readouterr().out


def test_runtime_profile_resolves_existing_secrets_and_uses_configured_admin_names(monkeypatch):
    cfg = _config()
    for side in ("source", "target"):
        cfg.raw["e2e"]["onboarding_profile"][side]["admin_connection"].pop("secret_provider")
        cfg.raw[side]["secret_provider"] = "env"
    for ref in ("source_admin_x", "target_admin_y", "oracle_source_test", "oracle_target_test"):
        monkeypatch.setenv(f"SECRET_{ref}", "offline-secret")
    facts = _facts(cfg)
    seen = []
    monkeypatch.setattr(live_launcher, "_probe_login", lambda raw, side, username, password, cdb, _driver: seen.append((side, username, raw["e2e"]["owner_provisioning"][side]["expected_container_name"])))
    raw = live_launcher._new_runtime_config(cfg, facts, object())
    assert seen == [("source", "SOURCE_ADMIN_X", "SOURCE_CONTAINER_X"), ("target", "TARGET_ADMIN_Y", "TARGET_CONTAINER_Y")]
    assert raw["e2e"]["owner_provisioning"]["source"]["admin_connection"]["username"] == "SOURCE_ADMIN_X"
    assert raw["e2e"]["owner_provisioning"]["source"]["secret_provider"] == "env"
    assert raw["e2e"]["dba_bootstrap"]["enabled"] is True
    assert raw["e2e"]["dba_bootstrap"]["approve_exact_admin_privilege_grants"] is True
    assert raw["e2e"]["dba_bootstrap"]["create_user_for_sides"] == ["source", "target"]
    assert raw["e2e"]["dba_bootstrap"]["expected_db_unique_name"] == "CDB_LOCAL"
    assert raw["e2e"]["dba_bootstrap"]["local_oracle_sid"] == "CDBSID_X"
    assert raw["e2e"]["dba_bootstrap"]["target_owner_catalog_views_to_grant"] == [
        "DBA_CONSTRAINTS", "DBA_DEPENDENCIES"
    ]
    assert raw["e2e"]["dba_bootstrap"]["approve_target_owner_catalog_grants"] is True
    live_launcher.validate_onboarding_config(E2EConfig(raw))
    live_launcher.validate_dba_bootstrap_config(E2EConfig(raw))


def test_secure_secret_prompt_occurs_only_when_reference_is_missing(monkeypatch):
    cfg = _config()
    monkeypatch.setenv("SECRET_existing_ref", "configured")
    assert live_launcher._resolve_secret(cfg.raw, "source", "existing_ref", "password") == ("configured", False)
    monkeypatch.delenv("SECRET_missing_ref", raising=False)
    prompts = []
    monkeypatch.setattr(live_launcher, "_ask_secret", lambda prompt: prompts.append(prompt) or "secure-prompt-value")
    value, prompted = live_launcher._resolve_secret(cfg.raw, "source", "missing_ref", "SOURCE password: ")
    assert (value, prompted) == ("secure-prompt-value", True)
    assert prompts == ["SOURCE password: "]
    assert "secure-prompt-value" not in str(cfg.raw)


def test_secret_provider_failure_does_not_trigger_password_prompt(monkeypatch):
    cfg = _config()
    ask = []
    monkeypatch.setattr(live_launcher, "create_secret_provider", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("provider unavailable")))
    monkeypatch.setattr(live_launcher, "_ask_secret", lambda _message: ask.append(True) or "value")
    with pytest.raises(RuntimeError, match="no password prompt was attempted"):
        live_launcher._resolve_secret(cfg.raw, "target", "secret_ref", "password")
    assert ask == []


def test_existing_permission_executor_authenticates_read_only_before_any_runner_stage(monkeypatch):
    cfg = _config()
    cfg.raw["oracle_permission_migration"] = {
        "enabled": True,
        "secret_provider": "env",
        "executor": {"username": "CLIENT_EXECUTOR", "password_secret": "client_executor_secret"},
    }
    facts = _facts(cfg)
    facts["permission_executor"] = {"username": "CLIENT_EXECUTOR", "status": "OPEN", "common": "NO"}
    for ref in ("source_admin_x", "target_admin_y", "oracle_source_test", "oracle_target_test", "client_executor_secret"):
        monkeypatch.setenv(f"SECRET_{ref}", "offline-secret")
    probes = []
    monkeypatch.setattr(
        live_launcher,
        "_probe_login",
        lambda raw, side, username, _password, _cdb, _driver, **kwargs: probes.append((side, username, kwargs.get("identity_label", "provisioning admin"))),
    )
    runtime = live_launcher._new_runtime_config(cfg, facts, object())
    assert probes[-1] == ("target", "CLIENT_EXECUTOR", "permission executor")
    assert runtime["e2e"]["dba_bootstrap"]["approve_permission_executor_creation"] is False
    assert runtime["e2e"]["dba_bootstrap"]["approve_permission_executor_grants"] is True
