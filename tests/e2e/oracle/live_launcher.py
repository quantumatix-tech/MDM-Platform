"""Interactive, fail-closed local launcher for the existing Oracle E2E flow.

All discovery is read-only. Configuration is written to a temporary runtime
YAML so the canonical acceptance file and its comments remain untouched.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from core.connectors.oracle._models import make_connect_kwargs, safe_connection_error_message
from core.connectors.oracle.permissions import PERMISSION_CATALOG_VIEWS
from core.secrets import create_secret_provider
from tests.e2e.acceptance import E2EConfig, load_e2e_config
from tests.e2e.oracle.config import validated_oracle_context
from tests.e2e.oracle.dba_bootstrap import (
    _quoted_identifier,
    _sql_text,
    validate_dba_bootstrap_config,
)
from tests.e2e.oracle.inspect_bootstrap_state import (
    _tagged,
    run_sqlplus_script,
)
from tests.e2e.oracle.owner_policy import is_valid_oracle_identifier
from tests.e2e.oracle.owner_provisioning import (
    _fixture_privileges,
    _same_datafile_path,
    validate_onboarding_config,
)

_PROHIBITED_PDBS = {"freepdb1", "migration_db"}
_SIZE = re.compile(r"[1-9][0-9]*(?:K|M|G)\Z", re.IGNORECASE)
_SECRET_REF = re.compile(r"[A-Za-z0-9_]+\Z")
_LOCAL_ORACLE_NAME = re.compile(r"[A-Za-z0-9_$#.-]{1,128}\Z")
_DISCOVERY_FIELDS = {
    "CDB": 2,
    "PDB": 5,
    "CTX": 3,
    "ADMIN": 5,
    "EXECUTOR": 5,
    "OWNER": 7,
    "OMF": 3,
    "TS": 6,
    "DF": 5,
    "PRIV": 4,
    "CAT": 4,
    "OPRIV": 4,
    "QUOTA": 5,
    "EXPRIV": 4,
    "ECAT": 4,
    "OCAT": 3,
    "TCAT": 3,
}
_PERMISSION_CATALOG_VIEWS = (
    "DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS",
    "DBA_TAB_PRIVS", "DBA_COL_PRIVS",
)
_ADMIN_CATALOG_VIEWS = (
    "DBA_USERS",
    "DBA_TABLESPACES",
    "DBA_DATA_FILES",
    "DBA_OBJECTS",
    "DBA_TS_QUOTAS",
    "DBA_SYS_PRIVS",
)
_SOURCE_OWNER_PERMISSION_CATALOG_VIEWS = PERMISSION_CATALOG_VIEWS[:4]
_TARGET_REPLACEMENT_CATALOG_VIEWS = ("DBA_DEPENDENCIES", "DBA_CONSTRAINTS")


def _ask_secret(prompt: str) -> str:
    value = getpass.getpass(prompt)
    if not value or any(ord(char) < 32 for char in value):
        raise RuntimeError("A non-empty password without control characters is required")
    return value


def _onboarding_profile(cfg: E2EConfig) -> dict[str, Any]:
    if cfg.raw.get("e2e", {}).get("source_mode", "fixtures") != "fixtures":
        raise RuntimeError("The LIVE E2E onboarding launcher is for fixture mode; use the existing FULL CLI directly for preloaded schemas")
    profile = cfg.raw.get("e2e", {}).get("onboarding_profile")
    if not isinstance(profile, dict) or profile.get("enabled") is not True:
        raise RuntimeError(
            "Configure e2e.onboarding_profile from config/e2e_onboarding_profile.example.yaml, "
            "fill approved tablespace/quota/storage values, then set enabled: true"
        )
    problems: list[str] = []
    if profile.get("required_owner_privileges") != "fixture_derived":
        problems.append("required_owner_privileges must be fixture_derived")
    expected_cdb = profile.get("expected_db_unique_name")
    if not isinstance(expected_cdb, str) or not _LOCAL_ORACLE_NAME.fullmatch(expected_cdb.strip()):
        problems.append("expected_db_unique_name must be the DBA-verified local CDB identity")
    local_sid = profile.get("local_oracle_sid")
    if local_sid is not None and (
        not isinstance(local_sid, str) or not _LOCAL_ORACLE_NAME.fullmatch(local_sid.strip())
    ):
        problems.append("local_oracle_sid must contain only letters, digits, dot, underscore, '$', or '#'")
    sqlplus_executable = profile.get("sqlplus_executable")
    if sqlplus_executable is not None and (
        not isinstance(sqlplus_executable, str) or not sqlplus_executable.strip()
    ):
        problems.append("sqlplus_executable must be a non-empty executable path or command")
    dba_config = cfg.raw.get("e2e", {}).get("dba_bootstrap", {})
    if isinstance(dba_config, dict):
        configured_cdb = dba_config.get("expected_db_unique_name")
        if configured_cdb and str(configured_cdb).casefold() != str(expected_cdb).casefold():
            problems.append("dba_bootstrap.expected_db_unique_name must match onboarding_profile.expected_db_unique_name")
        configured_sid = dba_config.get("local_oracle_sid")
        if local_sid and configured_sid and str(local_sid).casefold() != str(configured_sid).casefold():
            problems.append("dba_bootstrap.local_oracle_sid conflicts with onboarding_profile.local_oracle_sid")
        configured_sqlplus = dba_config.get("sqlplus_executable")
        if sqlplus_executable and configured_sqlplus and str(sqlplus_executable).casefold() != str(configured_sqlplus).casefold():
            problems.append("dba_bootstrap.sqlplus_executable conflicts with onboarding_profile.sqlplus_executable")
    for side in ("source", "target"):
        item = profile.get(side)
        if not isinstance(item, dict):
            problems.append(f"{side} profile is missing")
            continue
        admin = item.get("admin_connection")
        if not isinstance(admin, dict):
            admin = {}
        username = admin.get("username")
        secret_ref = admin.get("password_secret")
        if not is_valid_oracle_identifier(username):
            problems.append(f"{side}.admin_connection.username must be a valid PDB-local Oracle account")
        if not isinstance(secret_ref, str) or not _SECRET_REF.fullmatch(secret_ref):
            problems.append(f"{side}.admin_connection.password_secret must be a valid secret reference")
        if admin.get("secret_provider") is not None and not isinstance(admin.get("secret_provider"), str):
            problems.append(f"{side}.admin_connection.secret_provider must be a provider name")
        tablespace = item.get("permanent_tablespace")
        if not is_valid_oracle_identifier(tablespace) or str(tablespace).upper() in {"SYSTEM", "SYSAUX"}:
            problems.append(f"{side}.permanent_tablespace must be an approved non-SYSTEM permanent tablespace")
        quota = item.get("quota_bytes")
        if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
            problems.append(f"{side}.quota_bytes must be an approved finite positive integer")
        storage = item.get("storage")
        if not isinstance(storage, dict):
            problems.append(f"{side}.storage is required")
        else:
            mode = storage.get("mode")
            if mode not in {"reuse_existing", "oracle_managed_files", "explicit_path"}:
                problems.append(f"{side}.storage.mode must be reuse_existing, oracle_managed_files, or explicit_path")
            elif mode == "reuse_existing":
                if storage.get("size") not in (None, "") or storage.get("datafile_path") not in (None, ""):
                    problems.append(f"{side}.reuse_existing must omit size and datafile_path")
            else:
                if not isinstance(storage.get("size"), str) or not _SIZE.fullmatch(storage["size"].strip()):
                    problems.append(f"{side}.storage.size must include an explicit K/M/G unit")
                path = storage.get("datafile_path")
                if mode == "explicit_path":
                    if (
                        not isinstance(path, str)
                        or not path.strip()
                        or "|" in path
                        or any(ord(char) < 32 for char in path)
                        or not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute())
                    ):
                        problems.append(f"{side}.explicit_path requires a DBA-approved absolute datafile_path")
                elif path not in (None, ""):
                    problems.append(f"{side}.oracle_managed_files must omit datafile_path")
    if problems:
        raise RuntimeError("Oracle onboarding profile is incomplete: " + "; ".join(problems))
    owners, _services = validated_oracle_context(cfg)
    source_admin = str(profile["source"]["admin_connection"]["username"]).upper()
    target_admin = str(profile["target"]["admin_connection"]["username"]).upper()
    if source_admin.casefold() == target_admin.casefold():
        raise RuntimeError("SOURCE and TARGET provisioning admins must be distinct PDB-local accounts")
    for side, admin in (("source", source_admin), ("target", target_admin)):
        if admin in {"SYS", "SYSTEM"} or admin.casefold() in {owner.casefold() for owner in owners.values()}:
            raise RuntimeError(f"Configured {side.upper()} provisioning admin must be separate from SYS/SYSTEM and migration owners")
        owner_secret = cfg.raw[side].get("connection", {}).get("password_secret")
        if not isinstance(owner_secret, str) or not _SECRET_REF.fullmatch(owner_secret):
            raise RuntimeError(f"Configured {side.upper()} schema owner requires a valid password_secret reference")
    permission = cfg.raw.get("oracle_permission_migration", {})
    if isinstance(permission, dict) and permission.get("enabled") is True:
        executor = permission.get("executor")
        if not isinstance(executor, dict) or not is_valid_oracle_identifier(executor.get("username")):
            raise RuntimeError("Enabled Oracle permission migration requires a valid configured executor username")
        if not isinstance(executor.get("password_secret"), str) or not _SECRET_REF.fullmatch(executor["password_secret"]):
            raise RuntimeError("Enabled Oracle permission migration requires an executor password_secret reference")
        executor_name = executor["username"].upper()
        if executor_name in {"SYS", "SYSTEM"} or executor_name.casefold() in {
            owner.casefold() for owner in owners.values()
        } | {source_admin.casefold(), target_admin.casefold()}:
            raise RuntimeError("Permission executor must be separate from SYS/SYSTEM, provisioning admins, and migration owners")
    return profile


def _discovery_sql(cfg: E2EConfig, admins: dict[str, str], profile: dict[str, Any] | None = None) -> str:
    owners, services = validated_oracle_context(cfg)
    profile = profile or _onboarding_profile(cfg)
    sides = ("source", "target")
    containers = {
        side: cfg.raw["e2e"]["owner_provisioning"][side]["expected_container_name"].upper()
        for side in sides
    }
    for side in sides:
        if containers[side].casefold() in _PROHIBITED_PDBS:
            raise RuntimeError(f"Refusing prohibited PDB identity configured for {side.upper()}")
        if services[side].casefold() in _PROHIBITED_PDBS:
            raise RuntimeError(f"Refusing prohibited service configured for {side.upper()}")
    if containers["source"].casefold() == containers["target"].casefold():
        raise RuntimeError("SOURCE and TARGET PDBs must be distinct")

    expected_cdb = str(profile["expected_db_unique_name"]).strip()
    script = [
        "WHENEVER OSERROR EXIT FAILURE\n",
        "WHENEVER SQLERROR EXIT SQL.SQLCODE\n",
        "SET DEFINE OFF\nSET ECHO OFF\nSET FEEDBACK OFF\nSET HEADING OFF\nSET VERIFY OFF\nSET PAGESIZE 0\nSET TRIMSPOOL ON\nSET TAB OFF\nSET LINESIZE 32767\n",
        _tagged(
            "cdb.context_assert",
            "DECLARE BEGIN IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> 'CDB$ROOT' "
            "THEN RAISE_APPLICATION_ERROR(-20100,'Expected CDB$ROOT'); END IF; END;\n/\n",
        ),
        _tagged("cdb.identity", "SELECT 'CDB|' || SYS_CONTEXT('USERENV','DB_UNIQUE_NAME') FROM dual;\n"),
        _tagged(
            "cdb.expected_identity",
            "DECLARE v_count NUMBER; BEGIN SELECT COUNT(*) INTO v_count FROM v$database "
            f"WHERE UPPER(db_unique_name)=UPPER({_sql_text(expected_cdb)}); "
            "IF v_count <> 1 THEN RAISE_APPLICATION_ERROR(-20103,'Connected CDB does not match configured DB_UNIQUE_NAME'); END IF; END;\n/\n",
        ),
    ]
    for side in sides:
        pdb = containers[side]
        service = services[side]
        script.extend([
            _tagged(
                f"{side}.pdb_service_preflight",
                "DECLARE v_count NUMBER; BEGIN\n"
                f" SELECT COUNT(*) INTO v_count FROM v$pdbs WHERE name={_sql_text(pdb)} AND open_mode='READ WRITE';\n"
                f" IF v_count <> 1 THEN RAISE_APPLICATION_ERROR(-20101,'{side.upper()} PDB is absent or not READ WRITE'); END IF;\n"
                f" SELECT COUNT(*) INTO v_count FROM v$active_services s JOIN v$pdbs p ON p.con_id=s.con_id WHERE p.name={_sql_text(pdb)} AND (UPPER(s.name)=UPPER({_sql_text(service)}) OR UPPER(s.network_name)=UPPER({_sql_text(service)}));\n"
                f" IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20102,'{side.upper()} configured service does not map to expected PDB'); END IF; END;\n/\n",
            ),
            _tagged(
                f"{side}.pdb_identity",
                f"SELECT 'PDB|{side}|' || p.name || '|' || p.open_mode || '|' || {_sql_text(service)} FROM v$pdbs p WHERE p.name={_sql_text(pdb)} AND p.open_mode='READ WRITE';\n",
            ),
        ])

    for side in sides:
        pdb = _quoted_identifier(containers[side], f"{side} PDB")
        admin = admins[side].upper()
        owner = owners[side].upper()
        script.append(_tagged(f"{side}.switch_container", f"ALTER SESSION SET CONTAINER={pdb};\n"))
        script.append(_tagged(
            f"{side}.context_assert",
            "DECLARE BEGIN IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> "
            f"{_sql_text(containers[side])} THEN RAISE_APPLICATION_ERROR(-20104,'{side.upper()} PDB session context mismatch'); END IF; END;\n/\n",
        ))
        query_sections = [
            ("context", f"SELECT 'CTX|{side}|' || SYS_CONTEXT('USERENV','CON_NAME') FROM dual;\n"),
            ("admin", f"SELECT 'ADMIN|{side}|' || username || '|' || account_status || '|' || common FROM dba_users WHERE username={_sql_text(admin)};\n"),
        ]
        if side == "target" and cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True:
            executor = str(cfg.raw["oracle_permission_migration"]["executor"]["username"]).upper()
            query_sections.append((
                "executor",
                (f"SELECT 'EXECUTOR|target|' || {_sql_text(executor)} || '|' "
                f"|| NVL((SELECT u.account_status FROM dba_users u WHERE u.username={_sql_text(executor)}),'MISSING') || '|' "
                f"|| NVL((SELECT u.common FROM dba_users u WHERE u.username={_sql_text(executor)}),'NO') FROM dual;\n"),
            ))
        if side == "source":
            query_sections.append((
                "owner_catalog",
                (f"SELECT 'OCAT|source|' || table_name FROM dba_tab_privs "
                 f"WHERE grantee={_sql_text(owner)} AND owner='SYS' "
                 f"AND table_name IN ({','.join(_sql_text(view) for view in _SOURCE_OWNER_PERMISSION_CATALOG_VIEWS)}) "
                 "AND privilege='SELECT' ORDER BY table_name;\n"),
            ))
        elif side == "target":
            query_sections.append((
                "owner_catalog",
                (f"SELECT 'TCAT|target|' || table_name FROM dba_tab_privs "
                 f"WHERE grantee={_sql_text(owner)} AND owner='SYS' "
                 f"AND table_name IN ({','.join(_sql_text(view) for view in _TARGET_REPLACEMENT_CATALOG_VIEWS)}) "
                 "AND privilege='SELECT' ORDER BY table_name;\n"),
            ))
        query_sections.extend([
            (
                "owner",
                (f"SELECT 'OWNER|{side}|' || {_sql_text(owner)} || '|' "
                f"|| NVL((SELECT u.account_status FROM dba_users u WHERE u.username={_sql_text(owner)}),'MISSING') || '|' "
                f"|| NVL((SELECT u.common FROM dba_users u WHERE u.username={_sql_text(owner)}),'NO') || '|' "
                f"|| TO_CHAR((SELECT COUNT(*) FROM dba_objects o WHERE o.owner={_sql_text(owner)})) || '|' "
                f"|| NVL((SELECT u.default_tablespace FROM dba_users u WHERE u.username={_sql_text(owner)}),'') FROM dual;\n"),
            ),
            ("owner_privileges", f"SELECT 'OPRIV|{side}|{owner}|' || privilege FROM dba_sys_privs WHERE grantee={_sql_text(owner)} ORDER BY privilege;\n"),
            (
                "owner_quota",
                (f"SELECT 'QUOTA|{side}|{owner}|{str(profile[side]['permanent_tablespace']).upper()}|' "
                f"|| NVL((SELECT TO_CHAR(q.max_bytes) FROM dba_ts_quotas q WHERE q.username={_sql_text(owner)} "
                f"AND q.tablespace_name={_sql_text(str(profile[side]['permanent_tablespace']).upper())}),'MISSING') FROM dual;\n"),
            ),
            ("omf", f"SELECT 'OMF|{side}|' || NVL((SELECT value FROM v$parameter WHERE name='db_create_file_dest'), '') FROM dual;\n"),
            ("tablespaces", f"SELECT 'TS|{side}|' || t.tablespace_name || '|' || t.contents || '|' || t.status || '|' || TO_CHAR(COUNT(d.file_id)) FROM dba_tablespaces t LEFT JOIN dba_data_files d ON d.tablespace_name=t.tablespace_name GROUP BY t.tablespace_name,t.contents,t.status ORDER BY t.tablespace_name;\n"),
            ("datafiles", f"SELECT 'DF|{side}|' || tablespace_name || '|' || REPLACE(file_name,'|','/') || '|' || status FROM dba_data_files ORDER BY tablespace_name,file_name;\n"),
            ("admin_privileges", f"SELECT 'PRIV|{side}|' || privilege || '|' || admin_option FROM dba_sys_privs WHERE grantee={_sql_text(admin)} ORDER BY privilege;\n"),
            ("admin_catalog", f"SELECT 'CAT|{side}|' || table_name || '|' || privilege FROM dba_tab_privs WHERE grantee={_sql_text(admin)} AND owner='SYS' AND table_name IN ({','.join(_sql_text(view) for view in _ADMIN_CATALOG_VIEWS)}) AND privilege='SELECT' ORDER BY table_name;\n"),
        ])
        if side == "target" and cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True:
            executor = str(cfg.raw["oracle_permission_migration"]["executor"]["username"]).upper()
            query_sections.extend([
                ("executor_privileges", f"SELECT 'EXPRIV|target|{executor}|' || privilege FROM dba_sys_privs WHERE grantee={_sql_text(executor)} ORDER BY privilege;\n"),
                ("executor_catalog", f"SELECT 'ECAT|target|{executor}|' || table_name FROM dba_tab_privs WHERE grantee={_sql_text(executor)} AND owner='SYS' AND table_name IN ({','.join(_sql_text(view) for view in _PERMISSION_CATALOG_VIEWS)}) AND privilege='SELECT' ORDER BY table_name;\n"),
            ])
        script.extend(_tagged(f"{side}.{name}", query) for name, query in query_sections)
    script.extend(["PROMPT ORACLE_READ_ONLY_DISCOVERY_COMPLETE\nEXIT SUCCESS\n"])
    return "".join(script)


def _discover(
    cfg: E2EConfig,
    admins: dict[str, str],
    profile: dict[str, Any] | None = None,
    *,
    runner: Any = None,
    executable_override: str | None = None,
) -> dict[str, Any]:
    profile = profile or _onboarding_profile(cfg)
    dba_policy = cfg.raw.get("e2e", {}).get("dba_bootstrap", {})
    dba_policy = dba_policy if isinstance(dba_policy, dict) else {}
    executable = executable_override or (
        profile.get("sqlplus_executable")
        or dba_policy.get("sqlplus_executable")
    )
    oracle_sid = profile.get("local_oracle_sid") or dba_policy.get("local_oracle_sid")
    output = run_sqlplus_script(
        _discovery_sql(cfg, admins, profile),
        operation="Oracle LIVE E2E read-only discovery",
        completion_marker="ORACLE_READ_ONLY_DISCOVERY_COMPLETE",
        executable=executable,
        oracle_sid=oracle_sid,
        runner=runner,
        timeout=90,
    )
    facts: dict[str, Any] = {"tablespaces": {"source": [], "target": []}, "datafiles": {"source": [], "target": []}, "privileges": {"source": set(), "target": set()}, "owner_privileges": {"source": set(), "target": set()}, "quotas": {"source": None, "target": None}}
    facts["catalog_views"] = {"source": set(), "target": set()}
    facts["source_owner_catalog_views"] = set()
    facts["target_owner_catalog_views"] = set()
    for row in _parse_discovery_records(output):
        if row[0] == "CDB" and len(row) >= 2:
            facts["db_unique_name"] = row[1].strip()
        elif row[0] == "PDB" and len(row) >= 5:
            facts.setdefault("pdbs", {})[row[1]] = {"name": row[2], "mode": row[3], "service": row[4]}
        elif row[0] == "CTX" and len(row) >= 3:
            facts.setdefault("contexts", {})[row[1]] = row[2]
        elif row[0] == "ADMIN" and len(row) >= 5:
            facts.setdefault("admins", {})[row[1]] = {"username": row[2], "status": row[3], "common": row[4]}
        elif row[0] == "EXECUTOR" and len(row) >= 5:
            facts["permission_executor"] = {"username": row[2], "status": row[3], "common": row[4]}
        elif row[0] == "OWNER" and len(row) >= 5:
            facts.setdefault("owners", {})[row[1]] = {"username": row[2], "status": row[3], "common": row[4], "objects": int(row[5]), "default_tablespace": row[6]}
        elif row[0] == "OMF" and len(row) >= 3:
            facts.setdefault("omf", {})[row[1]] = row[2]
        elif row[0] == "TS" and len(row) >= 6:
            facts["tablespaces"][row[1]].append({"name": row[2], "contents": row[3], "status": row[4], "file_count": int(row[5])})
        elif row[0] == "DF" and len(row) >= 5:
            facts["datafiles"][row[1]].append({"tablespace": row[2], "path": row[3], "status": row[4]})
        elif row[0] == "PRIV" and len(row) >= 4:
            facts["privileges"][row[1]].add((row[2], row[3]))
        elif row[0] == "CAT" and len(row) >= 4:
            facts["catalog_views"][row[1]].add(row[2])
        elif row[0] == "OPRIV" and len(row) >= 4:
            facts["owner_privileges"][row[1]].add(row[3].upper())
        elif row[0] == "QUOTA" and len(row) >= 5:
            facts["quotas"][row[1]] = {"owner": row[2], "tablespace": row[3], "max_bytes": row[4]}
        elif row[0] == "EXPRIV" and len(row) >= 4:
            facts.setdefault("executor_privileges", set()).add(row[3].upper())
        elif row[0] == "ECAT" and len(row) >= 4:
            facts.setdefault("executor_catalog_views", set()).add(row[3].upper())
        elif row[0] == "OCAT" and len(row) >= 3:
            facts["source_owner_catalog_views"].add(row[2].upper())
        elif row[0] == "TCAT" and len(row) >= 3:
            facts["target_owner_catalog_views"].add(row[2].upper())
    required_sides = {"source", "target"}
    if not facts.get("db_unique_name") or set(facts.get("pdbs", {})) != required_sides:
        raise RuntimeError("Read-only discovery did not verify both configured PDB identities")
    expected_cdb = str(profile.get("expected_db_unique_name", "")).strip()
    if facts["db_unique_name"].casefold() != expected_cdb.casefold():
        raise RuntimeError(
            f"Read-only discovery connected to CDB {facts['db_unique_name']!r}, "
            f"not configured CDB {expected_cdb!r}"
        )
    if set(facts.get("contexts", {})) != required_sides:
        raise RuntimeError("Read-only discovery did not return both PDB session-context records")
    if set(facts.get("admins", {})) != required_sides:
        raise RuntimeError("Read-only discovery did not return both configured ADMIN records")
    if set(facts.get("owners", {})) != required_sides or set(facts.get("omf", {})) != required_sides or any(facts.get("quotas", {}).get(side) is None for side in required_sides):
        raise RuntimeError("Read-only discovery did not return required owner/storage records for both PDBs")
    if cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True and "permission_executor" not in facts:
        raise RuntimeError("Read-only discovery did not return the configured permission-executor account status")
    for side in required_sides:
        expected = cfg.raw["e2e"]["owner_provisioning"][side]["expected_container_name"]
        if facts["contexts"][side].casefold() != expected.casefold():
            raise RuntimeError(f"Read-only discovery session context mismatch for {side.upper()}")
    return facts


def _parse_discovery_records(output: str) -> list[list[str]]:
    """Parse deterministic SELECT-generated records from SQL*Plus output."""
    records: list[list[str]] = []
    pending = ""
    for raw_line in output.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        first = line.split("|", 1)[0].strip().upper()
        if first in _DISCOVERY_FIELDS:
            if pending:
                pending = ""
            pending = line
        elif pending:
            # SQL*Plus can wrap a selected row at its configured width.
            # Remove only line-boundary whitespace before normalizing fields.
            pending += line
        else:
            continue

        fields = [field.strip() for field in pending.split("|")]
        tag = fields[0].upper() if fields else ""
        if tag not in _DISCOVERY_FIELDS:
            pending = ""
            continue
        complete_fields = fields[1:_DISCOVERY_FIELDS[tag]]
        if (
            len(fields) >= _DISCOVERY_FIELDS[tag]
            and (tag in {"OMF", "OWNER"} or all(field for field in complete_fields))
        ):
            fields[0] = tag
            records.append(fields)
            pending = ""
    return records


def _resolve_secret(
    raw: dict[str, Any],
    side: str,
    ref: str,
    prompt: str,
    provider: str | None = None,
    *,
    allow_prompt: bool = True,
) -> tuple[str, bool]:
    if not isinstance(ref, str) or not _SECRET_REF.fullmatch(ref):
        raise RuntimeError(f"{side.upper()} secret reference must contain letters, digits, and underscores")
    try:
        resolver = create_secret_provider(raw, provider)
        value = resolver.resolve(ref) if resolver is not None else ""
    except KeyError:
        value = None
    except Exception as exc:  # noqa: BLE001 - provider exceptions may contain backend details
        response = getattr(exc, "response", None)
        response_code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        exception_name = type(exc).__name__.casefold()
        is_missing = (
            response_code in {"ResourceNotFoundException", "ResourceNotFound", "SecretNotFound"}
            or str(status) == "404"
            or exception_name in {"resourcenotfounderror", "notfound", "invalidpath"}
        )
        if is_missing:
            value = None
        else:
            raise RuntimeError(
                f"Configured {side.upper()} secret provider could not resolve the credential; no password prompt was attempted"
            ) from None
    if value is not None:
        if not value:
            raise RuntimeError(f"Configured {side.upper()} secret reference resolved to an empty value")
        return value, False
    if not allow_prompt:
        raise RuntimeError(
            f"Required {side.upper()} secret reference SECRET_{ref} is unavailable; "
            "preflight-only mode does not prompt for credentials"
        )
    value = _ask_secret(prompt)
    os.environ[f"SECRET_{ref}"] = value
    return value, True


def _permission_executor_creation_required(account: dict[str, str], expected_username: str) -> bool:
    """Return true only for a missing executor; preserve all existing accounts."""
    if account.get("username", "").casefold() != expected_username.casefold():
        raise RuntimeError("Read-only discovery permission-executor name does not match configuration")
    status = account.get("status", "").strip().upper()
    common = account.get("common", "").strip().upper()
    if status == "MISSING":
        return True
    if status != "OPEN" or common != "NO":
        raise RuntimeError("Configured permission executor exists but is not OPEN and PDB-local; it will not be changed")
    return False


def _owner_has_system_privilege(facts: dict[str, Any], side: str, privilege: str) -> bool:
    """Check owner grants from OPRIV rows, which contain privilege names only."""
    values = facts.get("owner_privileges", {}).get(side, set())
    if isinstance(values, str):
        values = (values,)
    return str(privilege).upper() in {str(value).upper() for value in values}


def _probe_login(
    raw: dict[str, Any], side: str, username: str, password: str,
    expected_db_unique_name: str, driver: Any, *, identity_label: str = "provisioning admin",
) -> None:
    endpoint = dict(raw[side]["connection"])
    endpoint.update(username=username, password=password)
    try:
        conn = driver.connect(**make_connect_kwargs(endpoint, driver))
    except Exception as exc:  # noqa: BLE001 - retain only the shared sanitized Oracle category/code
        detail = safe_connection_error_message(exc)
        raise RuntimeError(
            f"Configured {side.upper()} {identity_label} connection failed: {detail}"
        ) from None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT USER, SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME'), SYS_CONTEXT('USERENV','DB_UNIQUE_NAME') FROM dual")
            row = cur.fetchone()
        expected_pdb = raw["e2e"]["owner_provisioning"][side]["expected_container_name"]
        expected_service = raw[side]["connection"]["service_name"]
        if not row or str(row[0]).casefold() != username.casefold() or str(row[1]).casefold() != expected_pdb.casefold() or str(row[2]).casefold() != expected_service.casefold() or str(row[3]).casefold() != expected_db_unique_name.casefold():
            raise RuntimeError(f"{side.upper()} {identity_label} login did not match configured user/PDB/service")
    finally:
        conn.close()


def _new_runtime_config(
    cfg: E2EConfig,
    facts: dict[str, Any],
    driver: Any,
    *,
    allow_secret_prompt: bool = True,
) -> dict[str, Any]:
    profile = _onboarding_profile(cfg)
    raw = json.loads(json.dumps(cfg.raw))
    existing_dba_config = cfg.raw.get("e2e", {}).get("dba_bootstrap", {})
    if not isinstance(existing_dba_config, dict):
        existing_dba_config = {}
    configured_containers = {
        side: cfg.raw["e2e"]["owner_provisioning"][side]["expected_container_name"]
        for side in ("source", "target")
    }
    raw["e2e"]["owner_provisioning"] = {
        "enabled": True,
        **{side: {"expected_container_name": configured_containers[side]} for side in ("source", "target")},
    }
    storage_enabled = False
    admins: dict[str, str] = {}
    for side in ("source", "target"):
        entry = profile[side]
        admin_cfg = entry["admin_connection"]
        username = str(admin_cfg["username"]).upper()
        secret_ref = str(admin_cfg["password_secret"])
        provider = admin_cfg.get("secret_provider") or raw[side].get("secret_provider")
        raw["e2e"]["owner_provisioning"][side]["secret_provider"] = provider
        raw["e2e"]["owner_provisioning"][side]["admin_connection"] = {
            "username": username,
            "password_secret": secret_ref,
        }
        raw["e2e"]["owner_provisioning"][side]["permanent_tablespace"] = str(entry["permanent_tablespace"]).upper()
        raw["e2e"]["owner_provisioning"][side]["quota_bytes"] = int(entry["quota_bytes"])
        admins[side] = username
        password, prompted = _resolve_secret(raw, side, secret_ref, f"{side.upper()} PDB-local admin password: ", provider, allow_prompt=allow_secret_prompt)
        if prompted:
            raw["e2e"]["owner_provisioning"][side]["secret_provider"] = "env"
        try:
            _probe_login(raw, side, username, password, facts["db_unique_name"], driver)
        except Exception as exc:  # noqa: BLE001 - add stage context without exposing credentials
            if isinstance(exc, RuntimeError):
                raise RuntimeError(f"{side.upper()} admin login preflight failed: {exc}") from None  # noqa: TRY004
            raise RuntimeError(
                f"{side.upper()} admin login preflight failed ({type(exc).__name__})"
            ) from None
        storage = dict(entry["storage"])
        storage["mode"] = str(storage["mode"])
        if storage.get("size"):
            storage["size"] = str(storage["size"]).upper()
        raw["e2e"].setdefault("storage_bootstrap", {})[side] = storage
        storage_enabled = storage_enabled or storage["mode"] != "reuse_existing"

    raw["e2e"].setdefault("storage_bootstrap", {})["enabled"] = storage_enabled
    permission = raw.get("oracle_permission_migration", {})
    executor_creation = False
    if isinstance(permission, dict) and permission.get("enabled") is True:
        executor_cfg = permission.get("executor", {})
        executor_creation = _permission_executor_creation_required(
            facts["permission_executor"], str(executor_cfg.get("username", ""))
        )
        ref = executor_cfg.get("password_secret")
        executor_password, prompted = _resolve_secret(
            raw, "target", str(ref), "TARGET permission-executor password: ", permission.get("secret_provider"), allow_prompt=allow_secret_prompt
        )
        if prompted:
            permission["secret_provider"] = "env"
        if not executor_creation:
            _probe_login(
                raw,
                "target",
                str(executor_cfg["username"]).upper(),
                executor_password,
                facts["db_unique_name"],
                driver,
                identity_label="permission executor",
            )

    for side in ("source", "target"):
        ref = raw[side]["connection"].get("password_secret")
        _, prompted = _resolve_secret(raw, side, str(ref), f"{side.upper()} schema-owner password: ", raw[side].get("secret_provider"), allow_prompt=allow_secret_prompt)
        if prompted:
            raw[side]["secret_provider"] = "env"

    fixture_privileges = _fixture_privileges(cfg)
    source_catalog_views = set(facts.get("source_owner_catalog_views", set()))
    source_has_dictionary_read = _owner_has_system_privilege(
        facts, "source", "SELECT ANY DICTIONARY"
    )
    source_owner_catalog_grants = (
        [] if (
            source_has_dictionary_read
            or raw.get("oracle_permission_migration", {}).get("enabled") is not True
        ) else sorted(
            set(_SOURCE_OWNER_PERMISSION_CATALOG_VIEWS) - source_catalog_views
        )
    )
    target_has_dictionary_read = _owner_has_system_privilege(
        facts, "target", "SELECT ANY DICTIONARY"
    )
    target_owner_catalog_grants = (
        [] if target_has_dictionary_read else sorted(
            set(_TARGET_REPLACEMENT_CATALOG_VIEWS)
            - set(facts.get("target_owner_catalog_views", set()))
        )
    )
    raw["e2e"]["dba_bootstrap"] = {
        "enabled": True,
        "expected_db_unique_name": str(profile["expected_db_unique_name"]).strip(),
        "local_oracle_sid": profile.get("local_oracle_sid") or existing_dba_config.get("local_oracle_sid"),
        "sqlplus_executable": profile.get("sqlplus_executable") or existing_dba_config.get("sqlplus_executable"),
        "approve_admin_creation": False,
        "approve_exact_admin_privilege_grants": True,
        "approve_catalog_read_grants": True,
        "create_user_for_sides": [
            side for side in ("source", "target")
            if facts["owners"][side]["status"].upper() == "MISSING"
        ],
        "required_owner_privileges_by_side": {
            side: sorted(
                fixture_privileges
                if facts["owners"][side]["status"].upper() == "MISSING"
                else fixture_privileges - facts["owner_privileges"][side]
            )
            for side in ("source", "target")
        },
        "approve_storage_creation": storage_enabled,
        "approve_permission_executor_creation": executor_creation,
        "approve_permission_executor_grants": bool(permission.get("enabled") is True),
        "approve_source_owner_catalog_grants": bool(source_owner_catalog_grants),
        "source_owner_catalog_views_to_grant": source_owner_catalog_grants,
        "approve_target_owner_catalog_grants": bool(target_owner_catalog_grants),
        "target_owner_catalog_views_to_grant": target_owner_catalog_grants,
    }
    return raw


def _expected_admin_grants(
    facts: dict[str, Any], side: str, required: set[str], *, create_user_required: bool
) -> list[str]:
    current = facts["privileges"][side]
    exact = {privilege for privilege, _admin_option in current}
    delegated = {privilege for privilege, admin_option in current if admin_option.upper() == "YES"}
    missing: list[str] = []
    if create_user_required and "CREATE USER" not in exact:
        missing.append("CREATE USER")
    if "GRANT ANY PRIVILEGE" not in exact:
        missing.extend(sorted(required - delegated))
    return sorted(set(missing))


def _build_execution_plan(cfg: E2EConfig, raw: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    profile = _onboarding_profile(cfg)
    owners, services = validated_oracle_context(cfg)
    required = _fixture_privileges(cfg)
    plan: dict[str, Any] = {
        "cdb": facts["db_unique_name"],
        "source": {},
        "target": {},
        "permission_migration": {},
        "workflow": {},
    }
    expected_cdb = str(profile["expected_db_unique_name"]).strip()
    if facts.get("db_unique_name", "").casefold() != expected_cdb.casefold():
        raise RuntimeError(
            f"Read-only discovery CDB {facts.get('db_unique_name')!r} does not match configured CDB {expected_cdb!r}"
        )
    for side in ("source", "target"):
        configured = profile[side]
        owner = owners[side].upper()
        admin = str(configured["admin_connection"]["username"]).upper()
        pdb = facts["pdbs"][side]
        if pdb["mode"].upper() != "READ WRITE" or pdb["name"].casefold() != cfg.raw["e2e"]["owner_provisioning"][side]["expected_container_name"].casefold() or pdb["service"].casefold() != services[side].casefold():
            raise RuntimeError(f"Read-only discovery identity mismatch on {side.upper()}")
        admin_state = facts["admins"][side]
        if admin_state["username"].casefold() != admin.casefold() or admin_state["status"].upper() != "OPEN" or admin_state["common"].upper() != "NO":
            raise RuntimeError(f"Configured {side.upper()} admin must exist, be OPEN, and be PDB-local")
        storage_profile = configured["storage"]
        tablespace_name = str(configured["permanent_tablespace"]).upper()
        tablespaces = facts["tablespaces"][side]
        existing_ts = next((item for item in tablespaces if item["name"].upper() == tablespace_name), None)
        available_files = [item for item in facts["datafiles"][side] if item["tablespace"].upper() == tablespace_name and item["status"].upper() == "AVAILABLE"]
        if existing_ts and (existing_ts["contents"].upper() != "PERMANENT" or existing_ts["status"].upper() != "ONLINE" or not available_files):
            raise RuntimeError(f"Configured {side.upper()} tablespace exists but is not an online permanent tablespace with an available datafile")
        storage_action: dict[str, Any]
        if existing_ts:
            files = [item["path"] for item in available_files]
            if storage_profile["mode"] == "explicit_path" and not any(_same_datafile_path(path, str(storage_profile["datafile_path"])) for path in files):
                raise RuntimeError(f"Existing {side.upper()} tablespace does not use the configured approved datafile path")
            storage_action = {"action": "reuse", "tablespace": tablespace_name, "datafiles": sorted(files)}
        elif storage_profile["mode"] == "reuse_existing":
            raise RuntimeError(f"Configured {side.upper()} tablespace is missing; configure an approved creation mode in onboarding_profile.storage")
        else:
            if storage_profile["mode"] == "oracle_managed_files" and not facts["omf"].get(side, "").strip():
                raise RuntimeError(f"{side.upper()} OMF storage mode requires a configured db_create_file_dest")
            path = storage_profile.get("datafile_path")
            if path and any(_same_datafile_path(item["path"], str(path)) for item in facts["datafiles"][side]):
                raise RuntimeError(f"Configured {side.upper()} datafile path already exists outside the approved tablespace")
            storage_action = {
                "action": "create_if_missing",
                "tablespace": tablespace_name,
                "mode": storage_profile["mode"],
                "size": str(storage_profile["size"]).upper(),
            }
            if path:
                storage_action["datafile_path"] = path
            if storage_profile["mode"] == "oracle_managed_files":
                storage_action["omf_destination"] = facts["omf"][side]

        owner_state = facts["owners"][side]
        owner_status = owner_state["status"].upper()
        owner_action: dict[str, Any] = {
            "owner": owner,
            "tablespace": tablespace_name,
            "quota_bytes": int(configured["quota_bytes"]),
            "required_privileges": sorted(required),
        }
        if owner_status == "MISSING":
            owner_action["action"] = "create_missing_owner"
            missing_owner_privs = set(required)
        elif owner_status == "OPEN" and owner_state["common"].upper() == "NO":
            if int(owner_state["objects"]) != 0:
                raise RuntimeError(f"Existing {side.upper()} owner is not empty; no reset or cleanup is performed")
            if owner_state["default_tablespace"].upper() != tablespace_name:
                raise RuntimeError(f"Existing {side.upper()} owner has a different default tablespace; it will not be altered")
            quota = facts["quotas"][side]
            if quota["max_bytes"] == "MISSING" or int(quota["max_bytes"]) != int(configured["quota_bytes"]):
                raise RuntimeError(f"Existing {side.upper()} owner quota differs from the approved finite quota; it will not be altered")
            missing_owner_privs = sorted(required - facts["owner_privileges"][side])
            owner_action["action"] = "complete_empty_owner_setup" if missing_owner_privs else "reuse_empty_owner"
            owner_action["missing_privileges_to_grant"] = missing_owner_privs
        else:
            raise RuntimeError(f"Existing {side.upper()} owner must be OPEN and PDB-local; it will not be changed")

        admin_missing = _expected_admin_grants(
            facts,
            side,
            set(missing_owner_privs),
            create_user_required=owner_status == "MISSING",
        )
        has_dictionary_read = "SELECT ANY DICTIONARY" in {privilege for privilege, _option in facts["privileges"][side]}
        missing_catalog = [] if has_dictionary_read else sorted(set(_ADMIN_CATALOG_VIEWS) - facts["catalog_views"][side])
        plan[side] = {
            "pdb": pdb["name"],
            "pdb_mode": pdb["mode"],
            "service": services[side],
            "admin": admin,
            "admin_account_state": {"status": admin_state["status"], "common": admin_state["common"]},
            "admin_privileges_to_grant": admin_missing,
            "existing_grant_any_privilege_preserved": "GRANT ANY PRIVILEGE" in {privilege for privilege, _option in facts["privileges"][side]},
            "existing_select_any_dictionary_preserved": has_dictionary_read,
            "admin_catalog_reads_to_grant": missing_catalog,
            "storage": storage_action,
            "owner_setup": owner_action,
        }
        if side == "source":
            owner_has_dictionary_read = _owner_has_system_privilege(
                facts, side, "SELECT ANY DICTIONARY"
            )
            plan[side]["owner_catalog_reads_to_grant"] = (
                [] if (
                    owner_has_dictionary_read
                    or raw.get("oracle_permission_migration", {}).get("enabled") is not True
                ) else sorted(
                    set(_SOURCE_OWNER_PERMISSION_CATALOG_VIEWS)
                    - facts.get("source_owner_catalog_views", set())
                )
            )
        else:
            owner_has_dictionary_read = _owner_has_system_privilege(
                facts, "target", "SELECT ANY DICTIONARY"
            )
            plan[side]["owner_catalog_reads_to_grant"] = (
                [] if owner_has_dictionary_read else sorted(
                    set(_TARGET_REPLACEMENT_CATALOG_VIEWS)
                    - facts.get("target_owner_catalog_views", set())
                )
            )

    permission = raw.get("oracle_permission_migration", {})
    if isinstance(permission, dict) and permission.get("enabled") is True:
        executor_cfg = permission.get("executor", {})
        executor = facts["permission_executor"]
        expected_executor = str(executor_cfg.get("username", "")).upper()
        create_executor = _permission_executor_creation_required(executor, expected_executor)
        missing_executor_privs = sorted({"CREATE SESSION"} - facts.get("executor_privileges", set()))
        missing_executor_catalog = sorted(set(_PERMISSION_CATALOG_VIEWS) - facts.get("executor_catalog_views", set()))
        plan["permission_migration"] = {
            "enabled": True,
            "executor": expected_executor,
            "executor_account_state": {"status": executor["status"], "common": executor["common"]},
            "executor_action": "create_missing_executor" if create_executor else "preserve_existing_executor",
            "executor_system_privileges_to_grant": missing_executor_privs,
            "executor_catalog_reads_to_grant": missing_executor_catalog,
            "migration_scope": (
                "Grantees of SOURCE-owner object/column grants plus connected local default-role memberships "
                "without admin option; supported CREATE SESSION grants only; same-name mapping; "
                f"missing users follow {str(permission.get('new_user_policy', 'LOCKED')).upper()} policy; "
                "passwords and hashes are not copied"
            ),
            "source_owner_scope": owners["source"],
            "target_owner_scope": owners["target"],
            "policy": {
                key: permission.get(key)
                for key in ("new_user_policy", "approve_role_creation_admin_option", "approve_create_session")
                if key in permission
            },
        }
    else:
        plan["permission_migration"] = {"enabled": False}

    fixture_names = cfg.raw.get("e2e", {}).get("fixtures", [])
    plan["workflow"] = {
        "source_mode": cfg.raw.get("e2e", {}).get("source_mode", "fixtures"),
        "source_fixtures": [item if isinstance(item, str) else item.get("file") for item in fixture_names],
        "full_migration": True,
        "target_requirement": "empty configured target owner; no reset or overwrite",
        "validation": "existing Oracle SOURCE/TARGET acceptance validator",
    }
    return plan


def _target_catalog_grant_plan(cfg: E2EConfig, facts: dict[str, Any]) -> dict[str, Any]:
    """Create a read-only plan limited to replacement-safety catalog grants.

    This targeted bootstrap path is for an already-populated migration target.
    It deliberately does not run fixture setup, owner provisioning, or FULL.
    """
    owners, services = validated_oracle_context(cfg)
    owner = owners["target"].upper()
    state = facts.get("owners", {}).get("target", {})
    if str(state.get("username", "")).casefold() != owner.casefold():
        raise RuntimeError("Configured TARGET migration owner was not verified by discovery")
    if str(state.get("status", "")).upper() != "OPEN" or str(
        state.get("common", "")
    ).upper() != "NO":
        raise RuntimeError("TARGET migration owner must exist, be OPEN, and be PDB-local")
    owner_privileges = {
        str(privilege).upper()
        for privilege in facts.get("owner_privileges", {}).get("target", set())
    }
    has_dictionary_read = "SELECT ANY DICTIONARY" in owner_privileges
    missing = [] if has_dictionary_read else sorted(
        set(_TARGET_REPLACEMENT_CATALOG_VIEWS)
        - set(facts.get("target_owner_catalog_views", set()))
    )
    pdb = facts["pdbs"]["target"]
    return {
        "operation": "target_replacement_catalog_visibility",
        "cdb": facts["db_unique_name"],
        "target": {
            "pdb": pdb["name"],
            "service": services["target"],
            "owner": owner,
            "owner_status": state["status"],
            "direct_select_grants_to_add": [
                f"SELECT ON SYS.{view}" for view in missing
            ],
            "existing_select_any_dictionary_preserved": has_dictionary_read,
        },
        "scope": "Only missing direct SELECT grants needed for Oracle cross-owner replacement safety; no fixture, owner, storage, or migration actions.",
    }


def _plan_token(plan: dict[str, Any]) -> str:
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12].upper()


def _approve_execution_plan(plan: dict[str, Any]) -> str:
    token = f"APPLY-{_plan_token(plan)}"
    print("\nOracle LIVE E2E execution plan (read-only discovery; no DDL has run):")
    print(json.dumps(plan, indent=2, sort_keys=True))
    answer = input(f"Approve this exact plan once by typing {token}: ").strip()
    if answer != token:
        raise RuntimeError("Exact execution-plan approval was not granted; no DDL was executed")
    return token


def _assert_plan_unchanged(approved: dict[str, Any], current: dict[str, Any]) -> None:
    if _plan_token(current) != _plan_token(approved):
        raise RuntimeError(
            "Read-only state changed after plan approval; the plan hash changed and fresh approval is required"
        )


def _verify_reports(started: datetime, reports_dir: Path, *, require_permissions: bool = True) -> None:
    candidates = []
    for path in reports_dir.glob("*.json"):
        if path.name.startswith(("setup_report", "validation_report", "manifest_")):
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if report.get("mode") == "full" and report.get("run_id"):
            candidates.append((path.stat().st_mtime, path, report))
    candidates = [item for item in candidates if datetime.fromtimestamp(item[0], UTC) >= started]
    if not candidates:
        raise RuntimeError("No new FULL migration JSON report was produced")
    _mtime, path, report = max(candidates)
    phases = report.get("phases", {})
    if report.get("status") not in {"success", "completed"} or report.get("errors"):
        raise RuntimeError(f"FULL migration report {path} is not successful; inspect its errors")
    permission = phases.get("oracle_permission_verification", {})
    if require_permissions and permission.get("status") != "success":
        raise RuntimeError("FULL report does not show successful Oracle permission verification")
    validation_paths = sorted(reports_dir.glob("validation_report*.json"), key=lambda item: item.stat().st_mtime)
    validation = None
    for candidate in reversed(validation_paths):
        if datetime.fromtimestamp(candidate.stat().st_mtime, UTC) >= started:
            validation = candidate
            break
    if validation is None:
        raise RuntimeError("No new Oracle SOURCE/TARGET validation report was produced")
    validated = json.loads(validation.read_text(encoding="utf-8"))
    if validated.get("result") != "PASS" or any(c.get("status") != "PASS" for c in validated.get("checks", [])):
        raise RuntimeError("Oracle SOURCE/TARGET validation report contains failed checks")
    row_checks = [c for c in validated.get("checks", []) if str(c.get("name", "")).startswith("row_count:")]
    source_rows = sum(int(c.get("source", 0)) for c in row_checks)
    target_rows = sum(int(c.get("target", 0)) for c in row_checks)
    print(f"LIVE E2E PASS: run_id={report['run_id']} tables={len(row_checks)} source_rows={source_rows} target_rows={target_rows}")
    print(f"Migration JSON: {path}")
    print(f"Migration HTML: {path.with_suffix('.html')}")
    print(f"Validation JSON: {validation}")
    print(f"Audit JSONL: {REPOSITORY_ROOT / 'logs' / (str(report['run_id']) + '.jsonl')}")


_driver: Any = None


def main(argv: list[str] | None = None) -> int:
    global _driver
    parser = argparse.ArgumentParser(description="Single-approval Oracle LIVE E2E onboarding and migration")
    parser.add_argument("config_positional", nargs="?", help="Acceptance YAML (legacy positional form)")
    parser.add_argument("--config", dest="config_option", help="Acceptance YAML")
    parser.add_argument("--sqlplus", help="Override the configured SQL*Plus executable for this process")
    parser.add_argument("--preflight-only", action="store_true", help="Run read-only discovery/auth checks and print the plan; do not prompt for plan approval or execute DDL")
    parser.add_argument(
        "--target-catalog-grants-only",
        action="store_true",
        help="Approve and apply only missing TARGET replacement-catalog SELECT grants; do not run fixtures or migration",
    )
    args = parser.parse_args(argv)
    if args.config_positional and args.config_option:
        raise RuntimeError("Specify the configuration once, either positionally or with --config")
    if args.preflight_only and args.target_catalog_grants_only:
        raise RuntimeError("--preflight-only cannot be combined with --target-catalog-grants-only")
    if not args.preflight_only and not sys.stdin.isatty():
        raise RuntimeError("Run the LIVE workflow from an interactive VS Code PowerShell terminal")
    os.chdir(REPOSITORY_ROOT)
    config_path = Path(args.config_option or args.config_positional or "config/oracle_e2e_acceptance.yaml")
    try:
        cfg = load_e2e_config(str(config_path))
        profile = _onboarding_profile(cfg)
    except Exception as exc:  # noqa: BLE001 - keep configuration errors stage-specific
        raise RuntimeError(f"configuration/profile preflight failed: {exc}") from None
    try:
        import oracledb
        _driver = oracledb
    except Exception as exc:  # noqa: BLE001 - no credentials are involved
        raise RuntimeError(f"Oracle driver preflight failed ({type(exc).__name__})") from None
    admins = {side: str(profile[side]["admin_connection"]["username"]).upper() for side in ("source", "target")}
    try:
        facts = _discover(cfg, admins, profile, executable_override=args.sqlplus)
        discovered_plan = (
            _target_catalog_grant_plan(cfg, facts)
            if args.target_catalog_grants_only
            else _build_execution_plan(cfg, cfg.raw, facts)
        )
    except Exception as exc:  # noqa: BLE001 - SQL*Plus helper includes sanitized process diagnostics
        raise RuntimeError(f"read-only DBA/PDB discovery preflight failed: {exc}") from None
    try:
        raw = _new_runtime_config(cfg, facts, _driver, allow_secret_prompt=not args.preflight_only)
        runtime_cfg = E2EConfig(raw)
        validate_onboarding_config(runtime_cfg)
        validate_dba_bootstrap_config(runtime_cfg)
        plan = (
            _target_catalog_grant_plan(cfg, facts)
            if args.target_catalog_grants_only
            else _build_execution_plan(cfg, raw, facts)
        )
        _assert_plan_unchanged(discovered_plan, plan)
    except Exception as exc:  # noqa: BLE001 - secret/login failures are sanitized by the helpers
        raise RuntimeError(f"credential/resource/privilege preflight failed: {exc}") from None

    if args.preflight_only:
        print("\nOracle LIVE E2E READ-ONLY PREFLIGHT PASSED; no DDL, fixture, migration, or approval was executed.")
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    if args.target_catalog_grants_only:
        missing = raw["e2e"]["dba_bootstrap"].get(
            "target_owner_catalog_views_to_grant", []
        )
        if not missing:
            print("TARGET replacement-catalog access already exists; no DDL was needed.")
            return 0
        _approve_execution_plan(plan)
        try:
            refreshed_facts = _discover(
                cfg, admins, profile, executable_override=args.sqlplus
            )
            refreshed_plan = _target_catalog_grant_plan(cfg, refreshed_facts)
            _assert_plan_unchanged(plan, refreshed_plan)
        except Exception as exc:  # noqa: BLE001 - plan drift must stop before grants
            raise RuntimeError(
                f"Read-only target grant plan changed before execution; no grant was issued ({type(exc).__name__}: {exc})"
            ) from None
        from tests.e2e.oracle.dba_bootstrap import grant_target_owner_catalog_access

        grant_target_owner_catalog_access(
            runtime_cfg, apply=True, executable=args.sqlplus
        )
        verified_facts = _discover(
            cfg, admins, profile, executable_override=args.sqlplus
        )
        verified_plan = _target_catalog_grant_plan(cfg, verified_facts)
        if verified_plan["target"]["direct_select_grants_to_add"]:
            raise RuntimeError(
                "TARGET replacement-catalog grant verification failed; inspect the grant stage before retrying"
            )
        print("TARGET replacement-catalog grants verified; no fixtures or migration were run.")
        return 0

    _approve_execution_plan(plan)

    # Re-discover immediately before execution. Any inventory/config-derived
    # plan delta invalidates the approval and stops before the runner can issue DDL.
    try:
        refreshed_facts = _discover(cfg, admins, profile, executable_override=args.sqlplus)
        refreshed_plan = _build_execution_plan(cfg, raw, refreshed_facts)
    except Exception as exc:  # noqa: BLE001 - drift must fail closed without exposing secrets
        raise RuntimeError(
            f"Read-only state changed after plan approval; fresh approval is required ({type(exc).__name__}: {exc})"
        ) from None
    _assert_plan_unchanged(plan, refreshed_plan)

    temp_config = REPOSITORY_ROOT / "config" / f".oracle_e2e_runtime_{os.getpid()}.yaml"
    try:
        import yaml
        temp_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        started = datetime.now(UTC)
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tests/e2e/oracle/run_e2e.py"), "--config", str(temp_config), "--apply", "--dba-bootstrap"],
            cwd=str(REPOSITORY_ROOT),
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Existing Oracle E2E runner stopped with exit code {result.returncode}; inspect its stage report before retrying")
        _verify_reports(
            started,
            REPOSITORY_ROOT / "reports",
            require_permissions=raw.get("oracle_permission_migration", {}).get("enabled") is True,
        )
        return 0
    finally:
        if temp_config.exists():
            temp_config.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Oracle E2E launcher cancelled; inspect database/report state before retrying.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 - top-level launcher must stop safely
        print(f"Oracle LIVE E2E STOPPED: {exc}", file=sys.stderr)
        raise SystemExit(1)
