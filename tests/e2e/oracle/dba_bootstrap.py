"""Explicit local SQL*Plus OS-SYSDBA bootstrap for Oracle E2E owners.

This path is deliberately separate from normal migration. It validates both
configured PDBs in CDB$ROOT before any DDL, then grants only the exact
fixture-derived delegation and catalog-read privileges needed by the existing
owner provisioner. It never provisions application users or roles.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any

from core.connectors.oracle._models import make_connect_kwargs
from core.connectors.oracle.permissions import (
    PERMISSION_CATALOG_VIEWS,
)
from core.secrets import create_secret_provider
from tests.e2e.oracle.config import resolver_for_side, validated_oracle_context
from tests.e2e.oracle.inspect_bootstrap_state import _tagged, run_sqlplus_script
from tests.e2e.oracle.owner_policy import is_valid_oracle_identifier
from tests.e2e.oracle.owner_provisioning import (
    _fixture_privileges,
    _side_policy,
    _storage_policy,
)

_CATALOG_VIEWS = (
    "DBA_USERS",
    "DBA_TABLESPACES",
    "DBA_DATA_FILES",
    "DBA_OBJECTS",
    "DBA_TS_QUOTAS",
    "DBA_SYS_PRIVS",
)
_SOURCE_PERMISSION_CATALOG_VIEWS = PERMISSION_CATALOG_VIEWS[:4]
_TARGET_REPLACEMENT_CATALOG_VIEWS = ("DBA_DEPENDENCIES", "DBA_CONSTRAINTS")
_SIZE = re.compile(r"[1-9][0-9]*(?:K|M|G)\Z", re.IGNORECASE)


def _sql_text(value: str) -> str:
    """Return a SQL string literal; control characters are never accepted."""
    if any(ord(char) < 32 for char in value):
        raise ValueError("Oracle DBA bootstrap values may not contain control characters")
    return "'" + value.replace("'", "''") + "'"


def _quoted_identifier(value: str, label: str) -> str:
    if not is_valid_oracle_identifier(value):
        raise ValueError(f"Oracle DBA bootstrap {label} must be a simple Oracle identifier")
    return f'"{value.upper()}"'


def _password_assignment(bind_name: str, password: str) -> str:
    if not isinstance(password, str) or not password or any(ord(c) < 32 for c in password):
        raise ValueError("Oracle DBA bootstrap requires a non-empty password without control characters")
    return f"VARIABLE {bind_name} VARCHAR2(4000)\nBEGIN :{bind_name} := {_sql_text(password)}; END;\n/\n"


def validate_dba_bootstrap_config(cfg: Any) -> None:
    block = cfg.raw.get("e2e", {}).get("dba_bootstrap", {})
    if not isinstance(block, dict) or block.get("enabled") is not True:
        raise ValueError("e2e.dba_bootstrap.enabled must be true for --dba-bootstrap")
    required_approvals = (
        "approve_exact_admin_privilege_grants",
        "approve_catalog_read_grants",
    )
    missing = [key for key in required_approvals if block.get(key) is not True]
    if missing:
        raise ValueError(
            "Oracle DBA bootstrap requires explicit approval for: " + ", ".join(missing)
        )
    expected_cdb = block.get("expected_db_unique_name")
    if (
        not isinstance(expected_cdb, str)
        or not expected_cdb.strip()
        or len(expected_cdb) > 128
        or any(ord(char) < 32 for char in expected_cdb)
    ):
        raise ValueError(
            "e2e.dba_bootstrap.expected_db_unique_name must identify the local CDB used by SQL*Plus SYSDBA"
        )
    create_user_sides = block.get("create_user_for_sides", ["source", "target"])
    if (
        not isinstance(create_user_sides, list)
        or any(side not in {"source", "target"} for side in create_user_sides)
        or len(set(create_user_sides)) != len(create_user_sides)
    ):
        raise ValueError("e2e.dba_bootstrap.create_user_for_sides must contain unique source/target entries")
    grant_policy = block.get("required_owner_privileges_by_side")
    if grant_policy is not None:
        allowed_privileges = set(_fixture_privileges(cfg))
        if not isinstance(grant_policy, dict) or set(grant_policy) != {"source", "target"}:
            raise ValueError("e2e.dba_bootstrap.required_owner_privileges_by_side must define SOURCE and TARGET")
        for side in ("source", "target"):
            values = grant_policy.get(side)
            if (
                not isinstance(values, list)
                or len(set(values)) != len(values)
                or any(not isinstance(value, str) or value not in allowed_privileges for value in values)
            ):
                raise ValueError(
                    f"e2e.dba_bootstrap.required_owner_privileges_by_side.{side} must contain only unique fixture-derived privileges"
                )
    owners, services = validated_oracle_context(cfg)
    if owners["source"].casefold() == owners["target"].casefold():
        raise ValueError("Oracle DBA bootstrap SOURCE and TARGET owners must differ")
    if services["source"].casefold() == services["target"].casefold():
        raise ValueError("Oracle DBA bootstrap SOURCE and TARGET services must differ")
    permission_policy = cfg.raw.get("oracle_permission_migration", {})
    executor = permission_policy.get("executor") if isinstance(permission_policy, dict) else None
    if permission_policy.get("enabled") is True:
        if not isinstance(executor, dict) or not executor.get("username") or not executor.get("password_secret"):
            raise ValueError(
                "Enabled Oracle permission migration requires executor.username and executor.password_secret for explicit bootstrap"
            )
        _quoted_identifier(executor["username"], "permission executor")
        if executor["username"].upper() in {"SYS", "SYSTEM"}:
            raise ValueError("Oracle permission executor may not use SYS or SYSTEM")
        if executor["username"].casefold() in {
            owners["source"].casefold(), owners["target"].casefold()
        }:
            raise ValueError("Oracle permission executor must be separate from SOURCE and TARGET owners")
        for side in ("source", "target"):
            admin_user = _side_policy(cfg, side)["admin_connection"]["username"]
            if executor["username"].casefold() == admin_user.casefold():
                raise ValueError("Oracle permission executor must be separate from SOURCE/TARGET provisioning admins")
        approval = block.get("approve_permission_executor_creation", False)
        if not isinstance(approval, bool):
            raise ValueError("e2e.dba_bootstrap.approve_permission_executor_creation must be boolean")
        grant_approval = block.get("approve_permission_executor_grants", False)
        if not isinstance(grant_approval, bool):
            raise ValueError("e2e.dba_bootstrap.approve_permission_executor_grants must be boolean")
    elif block.get("approve_permission_executor_creation") is True:
        raise ValueError("Permission executor creation approval requires oracle_permission_migration.enabled: true")
    elif block.get("approve_permission_executor_grants") is True:
        raise ValueError("Permission executor grant approval requires oracle_permission_migration.enabled: true")
    source_catalog_views = block.get("source_owner_catalog_views_to_grant", [])
    if (
        not isinstance(source_catalog_views, list)
        or any(not isinstance(view, str) for view in source_catalog_views)
        or len(set(source_catalog_views)) != len(source_catalog_views)
        or any(view not in _SOURCE_PERMISSION_CATALOG_VIEWS for view in source_catalog_views)
    ):
        raise ValueError(
            "e2e.dba_bootstrap.source_owner_catalog_views_to_grant must contain only unique approved permission catalog views"
        )
    if source_catalog_views and block.get("approve_source_owner_catalog_grants") is not True:
        raise ValueError(
            "Oracle SOURCE owner catalog grants require explicit execution-plan approval"
        )
    source_catalog_approval = block.get("approve_source_owner_catalog_grants")
    if source_catalog_approval is not None and not isinstance(source_catalog_approval, bool):
        raise ValueError("e2e.dba_bootstrap.approve_source_owner_catalog_grants must be boolean")
    target_catalog_views = block.get("target_owner_catalog_views_to_grant", [])
    if (
        not isinstance(target_catalog_views, list)
        or any(not isinstance(view, str) for view in target_catalog_views)
        or len(set(target_catalog_views)) != len(target_catalog_views)
        or any(view not in _TARGET_REPLACEMENT_CATALOG_VIEWS for view in target_catalog_views)
    ):
        raise ValueError(
            "e2e.dba_bootstrap.target_owner_catalog_views_to_grant must contain only unique "
            "approved Oracle replacement catalog views"
        )
    target_catalog_approval = block.get("approve_target_owner_catalog_grants")
    if target_catalog_views and target_catalog_approval is not True:
        raise ValueError(
            "Oracle TARGET owner catalog grants require explicit execution-plan approval"
        )
    if target_catalog_approval is not None and not isinstance(target_catalog_approval, bool):
        raise ValueError("e2e.dba_bootstrap.approve_target_owner_catalog_grants must be boolean")
    for side in ("source", "target"):
        policy = _side_policy(cfg, side)
        admin = policy["admin_connection"]
        _quoted_identifier(admin["username"], f"{side} administrator")
        if cfg.raw.get("e2e", {}).get("storage_bootstrap", {}).get("enabled") is True:
            mode, _path, size = _storage_policy(cfg, side)
            storage_approved = block.get("approve_storage_creation") is True
            if mode != "reuse_existing" and not storage_approved:
                raise ValueError(
                    "Oracle DBA bootstrap requires e2e.dba_bootstrap.approve_storage_creation: true"
                )
            if size and not _SIZE.fullmatch(size):
                raise ValueError("Oracle DBA bootstrap storage size is invalid")
        elif block.get("approve_storage_creation") is True:
            raise ValueError(
                "Storage creation approval requires e2e.storage_bootstrap.enabled: true"
            )


def build_sqlplus_script(cfg: Any, passwords: dict[str, str]) -> str:
    """Build a no-prompt SQL*Plus script; caller must not log its contents."""
    validate_dba_bootstrap_config(cfg)
    owners, services = validated_oracle_context(cfg)
    all_required = sorted(_fixture_privileges(cfg))
    if any(not re.fullmatch(r"CREATE [A-Z ]+", privilege) for privilege in all_required):
        raise ValueError("Fixture privilege policy produced an unsupported Oracle privilege")
    configured_required = cfg.raw["e2e"]["dba_bootstrap"].get("required_owner_privileges_by_side")
    required_by_side = (
        {side: sorted(configured_required[side]) for side in ("source", "target")}
        if isinstance(configured_required, dict)
        else {side: all_required for side in ("source", "target")}
    )

    source_container = _quoted_identifier(
        _side_policy(cfg, "source")["expected_container_name"], "SOURCE PDB"
    )
    target_container = _quoted_identifier(
        _side_policy(cfg, "target")["expected_container_name"], "TARGET PDB"
    )
    if source_container.casefold() == target_container.casefold():
        raise ValueError("Oracle DBA bootstrap SOURCE and TARGET PDBs must differ")

    permission_policy = cfg.raw.get("oracle_permission_migration", {})
    executor = permission_policy.get("executor") if isinstance(permission_policy, dict) else None
    executor_enabled = permission_policy.get("enabled") is True
    executor_identifier = (
        _quoted_identifier(executor["username"], "permission executor")
        if executor_enabled and isinstance(executor, dict) else None
    )
    executor_user = str(executor.get("username", "")).upper() if executor_enabled else ""
    executor_create_approved = (
        cfg.raw["e2e"]["dba_bootstrap"].get("approve_permission_executor_creation") is True
    )
    executor_grants_approved = (
        executor_create_approved
        or cfg.raw["e2e"]["dba_bootstrap"].get("approve_permission_executor_grants") is True
    )
    create_user_sides = set(
        cfg.raw["e2e"]["dba_bootstrap"].get("create_user_for_sides", ["source", "target"])
    )

    script = [
        "WHENEVER OSERROR EXIT FAILURE\n",
        "WHENEVER SQLERROR EXIT SQL.SQLCODE\n",
        "SET DEFINE OFF\n",
        "SET ECHO OFF\n",
        "SET FEEDBACK OFF\n",
        "SET HEADING OFF\n",
        "SET VERIFY OFF\n",
        "SET SERVEROUTPUT ON\n",
        _password_assignment("source_admin_pw", passwords["source"]),
        _password_assignment("target_admin_pw", passwords["target"]),
        *([_password_assignment("permission_executor_pw", passwords["permission_executor"])] if executor_enabled else []),
        _tagged(
            "dba.cdb_identity",
            "DECLARE v_count NUMBER; BEGIN\n"
            "  IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> 'CDB$ROOT' THEN RAISE_APPLICATION_ERROR(-20001, 'Local DBA bootstrap must start in CDB$ROOT'); END IF;\n"
            f"  IF UPPER(SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')) <> UPPER({_sql_text(cfg.raw['e2e']['dba_bootstrap']['expected_db_unique_name'].strip())}) THEN RAISE_APPLICATION_ERROR(-20010, 'Connected local CDB does not match configured DB_UNIQUE_NAME'); END IF; END;\n/\n",
        ),
    ]
    for side in ("source", "target"):
        policy = _side_policy(cfg, side)
        required = required_by_side[side]
        pdb = policy["expected_container_name"].upper()
        service = services[side]
        script.append(_tagged(
            f"dba.{side}.pdb_service_preflight",
            "DECLARE v_count NUMBER; BEGIN\n"
            f"SELECT COUNT(*) INTO v_count FROM v$pdbs WHERE name = {_sql_text(pdb)} AND open_mode = 'READ WRITE';\n"
            f"IF v_count <> 1 THEN RAISE_APPLICATION_ERROR(-20002, 'Configured {side.upper()} PDB is missing or not READ WRITE'); END IF;\n"
            "SELECT COUNT(*) INTO v_count FROM v$active_services s JOIN v$pdbs p ON p.con_id = s.con_id "
            f"WHERE p.name = {_sql_text(pdb)} AND (UPPER(s.name) = UPPER({_sql_text(service)}) "
            f"OR UPPER(s.network_name) = UPPER({_sql_text(service)}));\n"
            f"IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20003, 'Configured {side.upper()} service does not identify the expected PDB'); END IF; END;\n/\n",
        ))

    script.append("PROMPT ORACLE_E2E_PDB_IDENTITY_PREFLIGHT_PASS\n")

    # Complete all PDB-local read-only checks before the first DDL.
    for side in ("source", "target"):
        policy = _side_policy(cfg, side)
        admin = policy["admin_connection"]["username"].upper()
        owner = owners[side].upper()
        tablespace = policy["permanent_tablespace"].upper()
        pdb = _quoted_identifier(policy["expected_container_name"], f"{side} PDB")
        storage_enabled = cfg.raw.get("e2e", {}).get("storage_bootstrap", {}).get("enabled") is True
        storage_mode, storage_path, _storage_size = (
            _storage_policy(cfg, side) if storage_enabled else ("reuse_existing", None, "")
        )
        script.append(_tagged(f"dba.{side}.resource_preflight", f"ALTER SESSION SET CONTAINER = {pdb};\n"))
        script.append(f"PROMPT ORACLE_INVENTORY_SECTION|dba.{side}.resource_checks\n")
        script.extend(
            [
                "DECLARE\n  v_count NUMBER;\n  v_status VARCHAR2(30);\n  v_common VARCHAR2(3);\nBEGIN\n",
                f"  IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> {_sql_text(policy['expected_container_name'].upper())} THEN RAISE_APPLICATION_ERROR(-20004, 'PDB identity mismatch'); END IF;\n",
                f"  SELECT COUNT(*), MAX(account_status), MAX(common) INTO v_count, v_status, v_common FROM dba_users WHERE username = {_sql_text(admin)};\n",
                "  IF v_count > 0 AND (v_common <> 'NO' OR v_status <> 'OPEN') THEN RAISE_APPLICATION_ERROR(-20005, 'Configured provisioning account must be an open PDB-local user'); END IF;\n",
                *(
                    ["  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20012, 'Configured provisioning account is missing and admin creation was not approved'); END IF;\n"]
                    if cfg.raw["e2e"]["dba_bootstrap"].get("approve_admin_creation") is not True
                    else []
                ),
                f"  SELECT COUNT(*) INTO v_count FROM dba_tablespaces WHERE tablespace_name = {_sql_text(tablespace)} AND contents = 'PERMANENT' AND status = 'ONLINE';\n",
                "  IF v_count = 1 THEN\n",
                f"    SELECT COUNT(*) INTO v_count FROM dba_data_files WHERE tablespace_name = {_sql_text(tablespace)} AND status = 'AVAILABLE';\n",
                "    IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20006, 'Configured tablespace has no available datafile'); END IF;\n",
            ]
        )

        if side == "target" and executor_enabled:
            script.extend(
                [
                    f"  SELECT COUNT(*), MAX(account_status), MAX(common) INTO v_count, v_status, v_common FROM dba_users WHERE username = {_sql_text(executor_user)};\n",
                    "  IF v_count > 0 AND (v_status <> 'OPEN' OR v_common <> 'NO') THEN RAISE_APPLICATION_ERROR(-20020, 'Configured permission executor must be OPEN and PDB-local; existing account was not changed'); END IF;\n",
                    *(
                        ["  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20021, 'Missing permission executor requires explicit creation approval'); END IF;\n"]
                        if not executor_create_approved else []
                    ),
                    *(
                        [
                            f"  SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee = {_sql_text(executor_user)} AND privilege = 'CREATE SESSION';\n",
                            "  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20023, 'Permission executor lacks CREATE SESSION and executor grant approval is missing'); END IF;\n",
                        ] if not executor_grants_approved else []
                    ),
                    f"  SELECT COUNT(*) INTO v_count FROM dba_roles WHERE role = {_sql_text(executor_user)};\n",
                    "  IF v_count > 0 THEN RAISE_APPLICATION_ERROR(-20022, 'Configured permission executor name conflicts with an existing TARGET role'); END IF;\n",
                ]
            )
            if not executor_grants_approved:
                for view in PERMISSION_CATALOG_VIEWS:
                    script.extend(
                        [
                            f"  SELECT COUNT(*) INTO v_count FROM dba_tab_privs WHERE grantee = {_sql_text(executor_user)} AND owner = 'SYS' AND table_name = {_sql_text(view)} AND privilege = 'SELECT';\n",
                            f"  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20024, 'Permission executor lacks required catalog access to {view} and executor grant approval is missing'); END IF;\n",
                        ]
                    )

        if storage_mode == "explicit_path" and storage_path:
            path_predicate = (
                f"UPPER(file_name) = UPPER({_sql_text(storage_path)})"
                if PureWindowsPath(storage_path).is_absolute()
                else f"file_name = {_sql_text(storage_path)}"
            )
            script.extend(
                [
                    f"    SELECT COUNT(*) INTO v_count FROM dba_data_files WHERE tablespace_name = {_sql_text(tablespace)} AND status = 'AVAILABLE' AND {path_predicate};\n",
                    "    IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20011, 'Configured datafile path does not match the existing tablespace'); END IF;\n",
                ]
            )
        script.append("  ELSE\n")
        if not storage_enabled:
            script.append(
                "    RAISE_APPLICATION_ERROR(-20007, 'Approved permanent tablespace is missing; configure storage bootstrap');\n"
            )
        else:
            mode, _path, _size = _storage_policy(cfg, side)
            if mode == "reuse_existing":
                script.append(
                    "    RAISE_APPLICATION_ERROR(-20007, 'Configured reuse-existing tablespace is missing');\n"
                )
            elif mode == "oracle_managed_files":
                script.extend(
                    [
                        "    SELECT COUNT(*) INTO v_count FROM v$parameter WHERE name = 'db_create_file_dest' AND value IS NOT NULL;\n",
                        "    IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20008, 'Oracle Managed Files destination is not configured'); END IF;\n",
                    ]
                )
            else:
                # A missing tablespace may only use an explicitly approved
                # path. Refuse to create the tablespace if that file is
                # already registered to any tablespace in this PDB.
                assert mode == "explicit_path" and _path is not None
                path_predicate = (
                    f"UPPER(file_name) = UPPER({_sql_text(_path)})"
                    if PureWindowsPath(_path).is_absolute()
                    else f"file_name = {_sql_text(_path)}"
                )
                script.extend(
                    [
                        f"    SELECT COUNT(*) INTO v_count FROM dba_data_files WHERE {path_predicate};\n",
                        "    IF v_count > 0 THEN RAISE_APPLICATION_ERROR(-20026, 'Configured datafile path is already registered to a tablespace'); END IF;\n",
                    ]
                )
        script.extend(
            [
                "  END IF;\n",
                f"  SELECT COUNT(*) INTO v_count FROM dba_roles WHERE role = {_sql_text(owner)};\n",
                f"  IF v_count > 0 THEN RAISE_APPLICATION_ERROR(-20027, 'Configured {side.upper()} owner name conflicts with an existing PDB role'); END IF;\n",
                f"  SELECT COUNT(*), MAX(account_status), MAX(common) INTO v_count, v_status, v_common FROM dba_users WHERE username = {_sql_text(owner)};\n",
                f"  IF v_count > 0 AND (v_status <> 'OPEN' OR v_common <> 'NO') THEN RAISE_APPLICATION_ERROR(-20025, 'Existing {side.upper()} fixture owner must be OPEN and PDB-local; it was not changed'); END IF;\n",
            ]
        )
        if side == "target" or cfg.raw.get("e2e", {}).get("source_mode", "fixtures") == "fixtures":
            script.extend(
                [
                    "  IF v_count > 0 THEN\n",
                    f"    SELECT COUNT(*) INTO v_count FROM dba_objects WHERE owner = {_sql_text(owner)};\n",
                    f"    IF v_count > 0 THEN RAISE_APPLICATION_ERROR(-20009, '{side.upper()} fixture owner is not empty'); END IF;\n",
                    "  END IF;\n",
                ]
            )
        script.extend(["END;\n/\n"])

    # Only after all identity, storage, local-account, and empty-owner checks.
    for side in ("source", "target"):
        policy = _side_policy(cfg, side)
        required = required_by_side[side]
        admin_cfg = policy["admin_connection"]
        admin = _quoted_identifier(admin_cfg["username"], f"{side} administrator")
        tablespace = _quoted_identifier(policy["permanent_tablespace"], f"{side} tablespace")
        owner_password_bind = f":{side}_admin_pw"
        pdb = _quoted_identifier(policy["expected_container_name"], f"{side} PDB")
        script.append(_tagged(f"dba.{side}.apply", f"ALTER SESSION SET CONTAINER = {pdb};\n"))
        script.append("DECLARE\n  v_count NUMBER;\n  v_sql VARCHAR2(4000);\nBEGIN\n")

        storage_enabled = cfg.raw.get("e2e", {}).get("storage_bootstrap", {}).get("enabled") is True
        if storage_enabled:
            mode, datafile, size = _storage_policy(cfg, side)
            if mode != "reuse_existing":
                if mode == "oracle_managed_files":
                    ddl = f"CREATE TABLESPACE {tablespace} DATAFILE SIZE {size} PERMANENT ONLINE"
                else:
                    assert datafile is not None
                    ddl = (
                        f"CREATE TABLESPACE {tablespace} DATAFILE {_sql_text(datafile)} "
                        f"SIZE {size} PERMANENT ONLINE"
                    )
                script.extend(
                    [
                        f"  SELECT COUNT(*) INTO v_count FROM dba_tablespaces WHERE tablespace_name = {_sql_text(policy['permanent_tablespace'].upper())};\n",
                        f"  IF v_count = 0 THEN EXECUTE IMMEDIATE {_sql_text(ddl)}; END IF;\n",
                    ]
                )
        script.append(
            f"  SELECT COUNT(*) INTO v_count FROM dba_users WHERE username = {_sql_text(admin_cfg['username'].upper())};\n"
        )
        if cfg.raw["e2e"]["dba_bootstrap"].get("approve_admin_creation") is True:
            script.extend(
                [
                    "  IF v_count = 0 THEN\n",
                "    v_sql := 'CREATE USER "
                + admin
                + " IDENTIFIED BY \"' || REPLACE("
                + owner_password_bind
                + ", '\"', '\"\"') || '\" DEFAULT TABLESPACE "
                + tablespace
                + "';\n",
                    "    EXECUTE IMMEDIATE v_sql;\n",
                    "  END IF;\n",
                ]
            )
        if side in create_user_sides:
            script.extend(
                [
                    f"  SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee = {_sql_text(admin_cfg['username'].upper())} AND privilege = 'CREATE USER';\n",
                    f"  IF v_count = 0 THEN EXECUTE IMMEDIATE 'GRANT CREATE USER TO {admin}'; END IF;\n",
                ]
            )
        for privilege in required:
            script.extend(
                [
                    f"  SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee = {_sql_text(admin_cfg['username'].upper())} AND privilege = 'GRANT ANY PRIVILEGE';\n",
                    "  IF v_count = 0 THEN\n",
                    f"  SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee = {_sql_text(admin_cfg['username'].upper())} AND privilege = {_sql_text(privilege)} AND admin_option = 'YES';\n",
                    f"    IF v_count = 0 THEN EXECUTE IMMEDIATE 'GRANT {privilege} TO {admin} WITH ADMIN OPTION'; END IF;\n",
                    "  END IF;\n",
                ]
            )
        for view in _CATALOG_VIEWS:
            script.extend(
                [
                    f"  SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee = {_sql_text(admin_cfg['username'].upper())} AND privilege = 'SELECT ANY DICTIONARY';\n",
                    "  IF v_count = 0 THEN\n",
                    f"  SELECT COUNT(*) INTO v_count FROM dba_tab_privs WHERE grantee = {_sql_text(admin_cfg['username'].upper())} AND owner = 'SYS' AND table_name = {_sql_text(view)} AND privilege = 'SELECT';\n",
                    f"    IF v_count = 0 THEN EXECUTE IMMEDIATE 'GRANT SELECT ON SYS.{view} TO {admin}'; END IF;\n",
                    "  END IF;\n",
                ]
            )
        if side == "target" and executor_enabled and executor_create_approved:
            assert executor_identifier is not None
            script.extend(
                [
                    f"  SELECT COUNT(*) INTO v_count FROM dba_users WHERE username = {_sql_text(executor_user)};\n",
                    "  IF v_count = 0 THEN\n",
                    "    v_sql := 'CREATE USER " + executor_identifier + " IDENTIFIED BY \"' || REPLACE(:permission_executor_pw, '\"', '\"\"') || '\" DEFAULT TABLESPACE " + tablespace + "';\n",
                    "    EXECUTE IMMEDIATE v_sql;\n",
                    "  END IF;\n",
                ]
            )
        if side == "target" and executor_enabled and executor_grants_approved:
            assert executor_identifier is not None
            script.extend(
                [
                    f"  SELECT COUNT(*) INTO v_count FROM dba_users WHERE username = {_sql_text(executor_user)};\n",
                    "  IF v_count > 0 THEN\n",
                    f"    SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee = {_sql_text(executor_user)} AND privilege = 'CREATE SESSION';\n",
                    "    IF v_count = 0 THEN EXECUTE IMMEDIATE 'GRANT CREATE SESSION TO " + executor_identifier + "'; END IF;\n",
                    *[
                        f"    SELECT COUNT(*) INTO v_count FROM dba_tab_privs WHERE grantee = {_sql_text(executor_user)} AND owner = 'SYS' AND table_name = {_sql_text(view)} AND privilege = 'SELECT';\n"
                        + "    IF v_count = 0 THEN EXECUTE IMMEDIATE 'GRANT SELECT ON SYS." + view + " TO " + executor_identifier + "'; END IF;\n"
                        for view in PERMISSION_CATALOG_VIEWS
                    ],
                    "  END IF;\n",
                ]
            )
        script.extend(["END;\n/\n"])
    script.append("PROMPT ORACLE_E2E_DBA_BOOTSTRAP_COMPLETE\nEXIT SUCCESS\n")
    return "".join(script)


def run_dba_bootstrap(
    cfg: Any,
    *,
    apply: bool,
    executable: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, str]:
    if not apply:
        raise ValueError("Oracle DBA bootstrap requires the explicit --apply option")
    validate_dba_bootstrap_config(cfg)
    passwords: dict[str, str] = {}
    for side in ("source", "target"):
        admin = _side_policy(cfg, side)["admin_connection"]
        resolver = resolver_for_side(cfg.raw, side, _side_policy(cfg, side).get("secret_provider"))
        try:
            passwords[side] = resolver.resolve(admin["password_secret"])
        except Exception:  # noqa: BLE001 - secret providers can expose sensitive backend details
            raise RuntimeError(f"Oracle {side} provisioning secret could not be resolved") from None
        if not passwords[side]:
            raise RuntimeError(f"Oracle {side} provisioning secret is empty")
    permission_policy = cfg.raw.get("oracle_permission_migration", {})
    if isinstance(permission_policy, dict) and permission_policy.get("enabled") is True:
        executor = permission_policy.get("executor", {})
        try:
            resolver = create_secret_provider(cfg.raw, permission_policy.get("secret_provider"))
            passwords["permission_executor"] = resolver.resolve(executor["password_secret"])
        except Exception:  # noqa: BLE001 - provider errors may expose sensitive backend details
            raise RuntimeError("Oracle permission-executor secret could not be resolved for approved bootstrap") from None
        if not passwords["permission_executor"]:
            raise RuntimeError("Oracle permission-executor secret is empty")
    script = build_sqlplus_script(cfg, passwords)
    policy = cfg.raw.get("e2e", {}).get("dba_bootstrap", {})
    policy = policy if isinstance(policy, dict) else {}
    command = executable or policy.get("sqlplus_executable")
    try:
        run_sqlplus_script(
            script,
            operation="Oracle DBA bootstrap",
            completion_marker="ORACLE_E2E_DBA_BOOTSTRAP_COMPLETE",
            executable=command,
            oracle_sid=policy.get("local_oracle_sid"),
            runner=runner,
            timeout=120,
            secrets=tuple(passwords.values()),
        )
    except RuntimeError as exc:
        message = str(exc)
        if "SQL*Plus exit=" in message or "timed out after" in message:
            message += ". Earlier DDL may have committed; inspect both configured PDBs before retrying."
        raise RuntimeError(message) from None
    return {"status": "PASS", "transport": "local SQL*Plus OS-authenticated SYSDBA"}


def _build_owner_catalog_grant_sql(cfg: Any, side: str) -> str:
    """Build a narrowly scoped post-provisioning owner catalog grant script."""
    if side not in {"source", "target"}:
        raise ValueError("Oracle owner catalog grant side must be SOURCE or TARGET")
    validate_dba_bootstrap_config(cfg)
    block = cfg.raw["e2e"]["dba_bootstrap"]
    view_key = f"{side}_owner_catalog_views_to_grant"
    requested = tuple(block.get(view_key, []))
    if not requested:
        return ""
    owners, services = validated_oracle_context(cfg)
    side_policy = _side_policy(cfg, side)
    pdb = side_policy["expected_container_name"].upper()
    owner = _quoted_identifier(owners[side].upper(), f"{side.upper()} owner")
    expected_cdb = str(block["expected_db_unique_name"]).strip()
    service = services[side]
    marker = f"{side}_owner_catalog"
    views = (
        _SOURCE_PERMISSION_CATALOG_VIEWS
        if side == "source" else _TARGET_REPLACEMENT_CATALOG_VIEWS
    )
    script = [
        "WHENEVER OSERROR EXIT FAILURE\n",
        "WHENEVER SQLERROR EXIT SQL.SQLCODE\n",
        "SET DEFINE OFF\nSET ECHO OFF\nSET FEEDBACK OFF\nSET HEADING OFF\nSET VERIFY OFF\nSET PAGESIZE 0\nSET LINESIZE 32767\n",
        _tagged(
            f"{marker}.cdb_identity",
            "DECLARE v_count NUMBER; BEGIN "
            "IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> 'CDB$ROOT' THEN "
            f"RAISE_APPLICATION_ERROR(-20200,'Expected CDB$ROOT for approved {side.upper()} catalog grants'); END IF; "
            f"SELECT COUNT(*) INTO v_count FROM v$database WHERE UPPER(db_unique_name)=UPPER({_sql_text(expected_cdb)}); "
            "IF v_count <> 1 THEN RAISE_APPLICATION_ERROR(-20201,'Connected CDB identity mismatch'); END IF; "
            f"SELECT COUNT(*) INTO v_count FROM v$pdbs p JOIN v$active_services s ON s.con_id=p.con_id "
            f"WHERE p.name={_sql_text(pdb)} AND p.open_mode='READ WRITE' AND "
            f"(UPPER(s.name)=UPPER({_sql_text(service)}) OR UPPER(s.network_name)=UPPER({_sql_text(service)})); "
            f"IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20202,'Configured {side.upper()} PDB/service identity mismatch'); END IF; END;\n/\n",
        ),
        _tagged(f"{marker}.switch_container", f"ALTER SESSION SET CONTAINER={_quoted_identifier(pdb, f'{side.upper()} PDB')};\n"),
        _tagged(
            f"{marker}.grants",
            "DECLARE v_count NUMBER; v_status VARCHAR2(32); v_common VARCHAR2(3); v_grantee VARCHAR2(261); "
            "BEGIN "
            f"IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> UPPER({_sql_text(pdb)}) THEN "
            f"RAISE_APPLICATION_ERROR(-20203,'{side.upper()} PDB context mismatch'); END IF; "
            f"SELECT account_status, common INTO v_status, v_common FROM dba_users WHERE username={_sql_text(owners[side].upper())}; "
            f"IF v_status <> 'OPEN' OR v_common <> 'NO' THEN RAISE_APPLICATION_ERROR(-20204,'{side.upper()} owner must exist, be OPEN, and be PDB-local'); END IF; "
            f"v_grantee := { _sql_text(owner) }; "
            + " ".join(
                f"SELECT COUNT(*) INTO v_count FROM dba_sys_privs WHERE grantee={_sql_text(owners[side].upper())} AND privilege='SELECT ANY DICTIONARY'; "
                f"IF v_count = 0 THEN SELECT COUNT(*) INTO v_count FROM dba_tab_privs WHERE grantee={_sql_text(owners[side].upper())} AND owner='SYS' AND table_name={_sql_text(view)} AND privilege='SELECT'; END IF; "
                + (f"IF v_count = 0 THEN EXECUTE IMMEDIATE 'GRANT SELECT ON SYS.{view} TO ' || v_grantee; END IF; "
                   if view in requested else f"IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20205,'{side.upper()} owner catalog grant plan changed; fresh approval required'); END IF; ")
                for view in views
            )
            + "END;\n/\n",
        ),
        f"PROMPT ORACLE_{side.upper()}_OWNER_CATALOG_GRANTS_COMPLETE\nEXIT SUCCESS\n",
    ]
    return "".join(script)


def build_source_owner_catalog_grant_sql(cfg: Any) -> str:
    """Build approved SOURCE permission-discovery grants after owner creation."""
    return _build_owner_catalog_grant_sql(cfg, "source")


def build_target_owner_catalog_grant_sql(cfg: Any) -> str:
    """Build approved TARGET replacement-safety grants after owner creation."""
    return _build_owner_catalog_grant_sql(cfg, "target")


def grant_source_owner_catalog_access(
    cfg: Any,
    *,
    apply: bool,
    executable: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Apply only launcher-approved, missing SOURCE dictionary object grants."""
    if not apply:
        raise ValueError("SOURCE owner catalog grants require explicit --apply authorization")
    script = build_source_owner_catalog_grant_sql(cfg)
    if not script:
        return {"status": "SKIP", "reason": "SOURCE owner catalog reads are already available"}
    policy = cfg.raw["e2e"]["dba_bootstrap"]
    run_sqlplus_script(
        script,
        operation="Oracle SOURCE owner permission catalog grants",
        completion_marker="ORACLE_SOURCE_OWNER_CATALOG_GRANTS_COMPLETE",
        executable=executable or policy.get("sqlplus_executable"),
        oracle_sid=policy.get("local_oracle_sid"),
        runner=runner,
        timeout=90,
    )
    return {"status": "PASS", "views": list(policy["source_owner_catalog_views_to_grant"])}


def grant_target_owner_catalog_access(
    cfg: Any,
    *,
    apply: bool,
    executable: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Apply only launcher-approved TARGET catalog reads for replacement safety."""
    if not apply:
        raise ValueError("TARGET owner catalog grants require explicit --apply authorization")
    script = build_target_owner_catalog_grant_sql(cfg)
    if not script:
        return {"status": "SKIP", "reason": "TARGET replacement catalog reads are already available"}
    policy = cfg.raw["e2e"]["dba_bootstrap"]
    run_sqlplus_script(
        script,
        operation="Oracle TARGET replacement catalog grants",
        completion_marker="ORACLE_TARGET_OWNER_CATALOG_GRANTS_COMPLETE",
        executable=executable or policy.get("sqlplus_executable"),
        oracle_sid=policy.get("local_oracle_sid"),
        runner=runner,
        timeout=90,
    )
    return {"status": "PASS", "views": list(policy["target_owner_catalog_views_to_grant"])}


def verify_permission_executor(cfg: Any, *, driver: Any) -> dict[str, str]:
    """Authenticate the configured executor after DBA bootstrap and verify its scope."""
    policy = cfg.raw.get("oracle_permission_migration", {})
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return {"status": "SKIP"}
    executor = policy.get("executor", {})
    if not isinstance(executor, dict) or not executor.get("username") or not executor.get("password_secret"):
        raise RuntimeError("Configured permission executor requires username and password_secret")
    try:
        resolver = create_secret_provider(cfg.raw, policy.get("secret_provider"))
        password = resolver.resolve(executor["password_secret"])
    except Exception:  # noqa: BLE001 - do not expose provider details
        raise RuntimeError("Permission-executor secret is unavailable after bootstrap") from None
    endpoint = dict(cfg.raw["target"]["connection"])
    endpoint.update(username=executor["username"], password=password)
    try:
        conn = driver.connect(**make_connect_kwargs(endpoint, driver))
    except Exception as exc:  # noqa: BLE001 - no secret-bearing driver text
        error_code = getattr(exc, "code", None)
        suffix = f" (ORA-{error_code})" if error_code else ""
        raise RuntimeError(f"Permission executor could not authenticate after bootstrap{suffix}") from None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT USER, SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME'), SYS_CONTEXT('USERENV','DB_UNIQUE_NAME') FROM dual")
            row = cur.fetchone()
        expected_pdb = cfg.raw["e2e"]["owner_provisioning"]["target"]["expected_container_name"]
        expected_service = cfg.raw["target"]["connection"]["service_name"]
        expected_cdb = cfg.raw.get("e2e", {}).get("dba_bootstrap", {}).get("expected_db_unique_name")
        if not row or str(row[0]).casefold() != str(executor["username"]).casefold() or str(row[1]).casefold() != expected_pdb.casefold() or str(row[2]).casefold() != expected_service.casefold() or (expected_cdb and str(row[3]).casefold() != expected_cdb.casefold()):
            raise RuntimeError("Permission-executor identity does not match configured TARGET PDB/service/CDB")
        from core.connectors.oracle.permissions import verify_executor_catalog_access

        catalog_gaps = verify_executor_catalog_access(conn)
        if catalog_gaps:
            raise RuntimeError(
                "Permission executor is authenticated but lacks required read-only catalog access: "
                + ", ".join(catalog_gaps)
            )
        return {
            "status": "PASS",
            "username": str(row[0]),
            "container": str(row[1]),
            "service": str(row[2]),
            "catalog_access": "PASS",
        }
    finally:
        conn.close()
