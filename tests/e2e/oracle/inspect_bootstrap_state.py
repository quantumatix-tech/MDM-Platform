"""Read-only inventory of configured Oracle E2E PDBs after DBA bootstrap.

Uses local SQL*Plus OS authentication and only SELECT/session-context SQL. It
never switches to a PDB unless both configured PDBs and services have first
passed the read-only CDB identity checks.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.e2e.acceptance import E2EConfig, load_e2e_config
from tests.e2e.oracle.config import validated_oracle_context
from tests.e2e.oracle.owner_policy import is_valid_oracle_identifier

_PROHIBITED_PDBS = {"freepdb1", "migration_db"}
_ERROR = re.compile(r"\b(?:ORA-\d{5}|SP2-\d{4}|PLS-\d{5}|TNS-\d{5}|LRM-\d{4})\b", re.IGNORECASE)
_SECTION = re.compile(r"(?im)^\s*PROMPT ORACLE_INVENTORY_SECTION\|([a-z0-9_.-]+)\s*$")
_OUTPUT_SECTION = re.compile(r"(?im)^\s*ORACLE_INVENTORY_SECTION\|([a-z0-9_.-]+)\s*$")
_REAL_SUBPROCESS_RUN = subprocess.run


def _literal(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("Oracle inventory configuration contains control characters")
    return "'" + value.replace("'", "''") + "'"


def _identifier(value: str, label: str) -> str:
    if not is_valid_oracle_identifier(value):
        raise ValueError(f"Configured {label} must be a simple Oracle identifier")
    return '"' + value.upper() + '"'


def _tagged(section: str, statement: str) -> str:
    """Label the next SQL*Plus statement so Oracle errors identify their query."""
    if not re.fullmatch(r"[a-z0-9_.-]+", section):
        raise ValueError("Invalid Oracle inventory section label")
    return f"PROMPT ORACLE_INVENTORY_SECTION|{section}\n{statement}"


def _statement_for_section(script: str, section: str | None) -> str | None:
    if not section:
        return None
    match = next((m for m in _SECTION.finditer(script) if m.group(1).lower() == section.lower()), None)
    if match is None:
        return None
    remainder = script[match.end():]
    next_marker = re.search(
        r"(?im)^\s*PROMPT ORACLE_(?:INVENTORY_SECTION\||[A-Z0-9_]*COMPLETE)",
        remainder,
    )
    statement = remainder[: next_marker.start()] if next_marker else remainder
    statement = "\n".join(
        line for line in statement.splitlines()
        if line.strip() and not line.lstrip().upper().startswith("PROMPT ")
    ).strip()
    return statement or None


def _sqlplus_environment(command: str, base_environment: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child-only local Oracle environment without guessing an SID."""
    environment = dict(base_environment) if base_environment is not None else os.environ.copy()
    executable = shutil.which(command) or command
    candidate = Path(executable)
    if candidate.is_file():
        oracle_home = candidate.resolve().parent.parent
        if (oracle_home / "sqlplus" / "mesg").is_dir() and (oracle_home / "bin").is_dir():
            environment["ORACLE_HOME"] = str(oracle_home)
            environment["PATH"] = str(oracle_home / "bin") + os.pathsep + environment.get("PATH", "")
    sid = environment.get("ORACLE_SID", "").strip()
    if not sid:
        raise RuntimeError(
            "Local SQL*Plus SYSDBA requires ORACLE_SID; set it in the current process or configure "
            "e2e.onboarding_profile.local_oracle_sid. No connection was attempted."
        )
    if not re.fullmatch(r"[A-Za-z0-9_$#.-]{1,128}", sid):
        raise RuntimeError("ORACLE_SID must contain only letters, digits, dot, underscore, '$', or '#'")
    return environment


def _verify_local_windows_service(command: str, environment: dict[str, str]) -> None:
    """Check OracleService<SID> read-only when SQL*Plus belongs to a local home."""
    if os.name != "nt":
        return
    executable = Path(shutil.which(command) or command)
    home = environment.get("ORACLE_HOME")
    if not home or not executable.is_file():
        return
    try:
        executable_bin = os.path.normcase(str(executable.resolve().parent))
        home_bin = os.path.normcase(str((Path(home) / "bin").resolve()))
    except OSError:
        return
    if executable_bin != home_bin:
        return
    service = f"OracleService{environment.get('ORACLE_SID', '').strip()}"
    sc = shutil.which("sc.exe") or "sc.exe"
    try:
        result = subprocess.run(
            [sc, "query", service], capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Windows Oracle service query failed ({type(exc).__name__}) for {service}") from None
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 or not re.search(r"(?im)^\s*STATE\s*:\s*\d+\s+RUNNING\b", output):
        raise RuntimeError(
            f"Windows local Oracle service {service} is not RUNNING "
            f"(sc exit={result.returncode}; {_short_sqlplus_output(output)})"
        )


def _short_sqlplus_output(value: str | None, secrets: tuple[str, ...] = ()) -> str:
    text = (value or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    if not text:
        return "<empty>"
    text = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:1600] + ("…" if len(text) > 1600 else "")


def run_sqlplus_script(
    script: str,
    *,
    operation: str,
    completion_marker: str,
    executable: str | None = None,
    oracle_sid: str | None = None,
    runner: Callable[..., Any] | None = None,
    timeout: int = 90,
    secrets: tuple[str, ...] = (),
) -> str:
    """Execute a local SYSDBA script with shared env and sanitized diagnostics."""
    command = executable or shutil.which("sqlplus")
    if not command:
        raise RuntimeError(f"{operation}: SQL*Plus is unavailable; no database was contacted")
    if oracle_sid and not re.fullmatch(r"[A-Za-z0-9_$#.-]{1,128}", oracle_sid.strip()):
        raise RuntimeError(f"{operation}: configured local_oracle_sid is invalid")
    env = os.environ.copy()
    if oracle_sid:
        env["ORACLE_SID"] = oracle_sid.strip()
    try:
        # Resolve Oracle Home and require an explicit local instance SID. This
        # prevents SQL*Plus silently falling back to OracleServiceORCL.
        child_env = _sqlplus_environment(str(command), env)
        if runner is None and subprocess.run is _REAL_SUBPROCESS_RUN:
            _verify_local_windows_service(str(command), child_env)
    except (OSError, RuntimeError, ValueError) as exc:
        info = _sqlplus_environment_summary(str(command), env)
        raise RuntimeError(f"{operation}: local SQL*Plus environment preflight failed: {exc}; {info}") from None

    args = [str(command), "-L", "-S", "/ as sysdba"]
    run_process = runner or subprocess.run
    try:
        result = run_process(
            args,
            input=script,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        info = _sqlplus_environment_summary(str(command), child_env)
        raise RuntimeError(
            f"{operation} timed out after {timeout}s (section=unknown; SQL*Plus exit=timeout); "
            f"stdout={_short_sqlplus_output(stdout, secrets)}; "
            f"stderr={_short_sqlplus_output(stderr, secrets)}; {info}"
        ) from None
    except (OSError, subprocess.SubprocessError) as exc:
        info = _sqlplus_environment_summary(str(command), child_env)
        detail = str(exc)
        for secret in secrets:
            if secret:
                detail = detail.replace(secret, "<redacted>")
        raise RuntimeError(
            f"{operation}: SQL*Plus subprocess could not start ({type(exc).__name__}: {detail}); {info}"
        ) from None

    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    combined = stdout + "\n" + stderr
    if int(result.returncode) != 0 or completion_marker not in combined:
        markers = _OUTPUT_SECTION.findall(combined)
        section = markers[-1] if markers else "sqlplus_startup"
        statement = _statement_for_section(script, section) if markers else None
        location = f"section={section}"
        if statement:
            location += f"; SQL={_short_sqlplus_output(statement, secrets)}"
        info = _sqlplus_environment_summary(str(command), child_env)
        raise RuntimeError(
            f"{operation} failed ({location}; SQL*Plus exit={result.returncode}; "
            f"completion_marker={'present' if completion_marker in combined else 'missing'}); "
            f"stdout={_short_sqlplus_output(stdout, secrets)}; "
            f"stderr={_short_sqlplus_output(stderr, secrets)}; {info}"
        )
    return combined


def _sqlplus_environment_summary(command: str, environment: dict[str, str]) -> str:
    selected = shutil.which(command) or command
    return (
        f"SQL*Plus={selected}; ORACLE_HOME={environment.get('ORACLE_HOME', '<unset>')}; "
        f"ORACLE_SID={environment.get('ORACLE_SID', '<unset>')}; "
        f"LOCAL={environment.get('LOCAL', '<unset>')}; "
        f"TNS_ADMIN={environment.get('TNS_ADMIN', '<unset>')}"
    )


def build_inventory_sql(
    cfg: E2EConfig,
    *,
    source_admin: str | None = None,
    target_admin: str | None = None,
    tablespaces: tuple[str, ...] = (),
) -> str:
    """Build SELECT-only inventory SQL from configured services and owners."""
    owners, services = validated_oracle_context(cfg)
    owner_policy = cfg.raw.get("e2e", {}).get("owner_provisioning", {})
    containers: dict[str, str] = {}
    for side in ("source", "target"):
        policy = owner_policy.get(side, {}) if isinstance(owner_policy, dict) else {}
        container = policy.get("expected_container_name") if isinstance(policy, dict) else None
        container = container or services[side]
        _identifier(container, f"{side.upper()} PDB")
        if container.casefold() in _PROHIBITED_PDBS or services[side].casefold() in _PROHIBITED_PDBS:
            raise ValueError(f"Refusing prohibited PDB/service for {side.upper()}")
        containers[side] = container.upper()
    if containers["source"].casefold() == containers["target"].casefold():
        raise ValueError("Configured SOURCE and TARGET PDBs must be distinct")

    permission = cfg.raw.get("oracle_permission_migration", {})
    profile = cfg.raw.get("e2e", {}).get("onboarding_profile", {})
    dba_policy = cfg.raw.get("e2e", {}).get("dba_bootstrap", {})
    expected_cdb = (
        profile.get("expected_db_unique_name")
        if isinstance(profile, dict)
        else None
    ) or (dba_policy.get("expected_db_unique_name") if isinstance(dba_policy, dict) else None)
    executor_cfg = permission.get("executor", {}) if isinstance(permission, dict) else {}
    executor = str(executor_cfg.get("username", "")).upper()
    admins: dict[str, str | None] = {"source": source_admin, "target": target_admin}
    for side in ("source", "target"):
        side_cfg = owner_policy.get(side, {}) if isinstance(owner_policy, dict) else {}
        admin_cfg = side_cfg.get("admin_connection", {}) if isinstance(side_cfg, dict) else {}
        admins[side] = admins[side] or (admin_cfg.get("username") if isinstance(admin_cfg, dict) else None)
        if admins[side]:
            _identifier(str(admins[side]), f"{side.upper()} provisioning admin")

    if executor:
        _identifier(executor, "permission executor")
    expected_tablespaces: dict[str, set[str]] = {"source": set(), "target": set()}
    for side in ("source", "target"):
        side_cfg = owner_policy.get(side, {}) if isinstance(owner_policy, dict) else {}
        configured = side_cfg.get("permanent_tablespace") if isinstance(side_cfg, dict) else None
        if configured:
            _identifier(str(configured), f"{side.upper()} application tablespace")
            expected_tablespaces[side].add(str(configured).upper())
        for name in tablespaces:
            _identifier(name, "application tablespace")
            expected_tablespaces[side].add(name.upper())

    sql = [
        "WHENEVER OSERROR EXIT FAILURE\n",
        "WHENEVER SQLERROR EXIT SQL.SQLCODE\n",
        "SET DEFINE OFF\nSET ECHO OFF\nSET FEEDBACK OFF\nSET HEADING OFF\nSET VERIFY OFF\nSET PAGESIZE 0\nSET LINESIZE 32767\nSET TRIMSPOOL ON\n",
        _tagged("cdb_identity", "SELECT 'CDB|'||SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')||'|'||SYS_CONTEXT('USERENV','CON_NAME') FROM dual;\n"),
        _tagged("cdb_pdb_service_preflight", "DECLARE n NUMBER; BEGIN\n"),
        " IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> 'CDB$ROOT' THEN RAISE_APPLICATION_ERROR(-20200,'Expected local CDB$ROOT'); END IF;\n",
    ]
    if expected_cdb:
        sql.extend([
            f" SELECT COUNT(*) INTO n FROM v$database WHERE UPPER(db_unique_name)=UPPER({_literal(str(expected_cdb).strip())});\n",
            " IF n<>1 THEN RAISE_APPLICATION_ERROR(-20204,'Connected local CDB does not match configured DB_UNIQUE_NAME'); END IF;\n",
        ])
    for side in ("source", "target"):
        pdb = containers[side]
        service = services[side]
        sql.extend([
            f" SELECT COUNT(*) INTO n FROM v$pdbs WHERE name={_literal(pdb)} AND open_mode='READ WRITE';\n",
            f" IF n<>1 THEN RAISE_APPLICATION_ERROR(-20201,'Configured {side.upper()} PDB is missing or not READ WRITE'); END IF;\n",
            (
                " SELECT COUNT(*) INTO n FROM v$active_services s JOIN v$pdbs p ON p.con_id=s.con_id "
                f"WHERE p.name={_literal(pdb)} AND (UPPER(s.name)=UPPER({_literal(service)}) OR UPPER(s.network_name)=UPPER({_literal(service)}));\n"
            ),
            f" IF n=0 THEN RAISE_APPLICATION_ERROR(-20202,'Configured {side.upper()} service does not map to its PDB'); END IF;\n",
        ])
    sql.extend(["END;\n/\n",
                _tagged("source.pdb_status", "SELECT 'PDB|source|'||name||'|'||open_mode FROM v$pdbs WHERE name=" + _literal(containers["source"]) + ";\n"),
                _tagged("target.pdb_status", "SELECT 'PDB|target|'||name||'|'||open_mode FROM v$pdbs WHERE name=" + _literal(containers["target"]) + ";\n")])

    for side in ("source", "target"):
        pdb_q = _identifier(containers[side], f"{side.upper()} PDB")
        side_principals = [(f"{side.upper()}_OWNER", owners[side].upper())]
        if admins[side]:
            side_principals.append((f"{side.upper()}_ADMIN", str(admins[side]).upper()))
        if side == "target" and executor:
            side_principals.append(("PERMISSION_EXECUTOR", executor))
        principal_rows = " UNION ALL ".join(
            f"SELECT {_literal(label)} AS principal_label, "
            f"{_literal(name)} AS principal_username FROM dual"
            for label, name in side_principals
        )
        principal_names = ",".join(_literal(name) for _label, name in side_principals)
        expected_ts = expected_tablespaces[side]
        if expected_ts:
            expected_ts_rows = " UNION ALL ".join(
                f"SELECT {_literal(name)} tablespace_name FROM dual"
                for name in sorted(expected_ts)
            )
            tablespace_query = (
                "WITH expected AS (" + expected_ts_rows + ") "
                "SELECT 'TABLESPACE|" + side + "|'||e.tablespace_name||'|'||"
                "NVL(t.contents,'MISSING')||'|'||NVL(t.status,'MISSING') "
                "FROM expected e LEFT JOIN dba_tablespaces t ON t.tablespace_name=e.tablespace_name "
                "ORDER BY e.tablespace_name;\n"
            )
            datafile_filter = f"tablespace_name IN ({','.join(_literal(name) for name in sorted(expected_ts))})"
            # Filter the preserved outer row by expected tablespace, not by
            # q.tablespace_name; filtering the nullable side would hide the
            # missing-quota rows this inventory is meant to report.
            quota_filter = f"ts.tablespace_name IN ({','.join(_literal(name) for name in sorted(expected_ts))})"
            quota_query = (
                "WITH expected AS (SELECT " + _literal(owners[side].upper()) + " username FROM dual) "
                "SELECT 'QUOTA|" + side + "|'||e.username||'|'||ts.tablespace_name||'|'||"
                "NVL(TO_CHAR(q.bytes),'MISSING')||'|'||NVL(TO_CHAR(q.max_bytes),'MISSING') "
                "FROM expected e CROSS JOIN dba_tablespaces ts LEFT JOIN dba_ts_quotas q "
                "ON q.username=e.username AND q.tablespace_name=ts.tablespace_name WHERE "
                + quota_filter + " ORDER BY e.username,ts.tablespace_name;\n"
            )
        else:
            tablespace_query = (
                "SELECT 'TABLESPACE|" + side + "|'||tablespace_name||'|'||contents||'|'||status "
                "FROM dba_tablespaces WHERE contents='PERMANENT' AND tablespace_name NOT IN ('SYSTEM','SYSAUX') "
                "ORDER BY tablespace_name;\n"
            )
            datafile_filter = (
                "tablespace_name IN (SELECT tablespace_name FROM dba_tablespaces "
                "WHERE contents='PERMANENT' AND tablespace_name NOT IN ('SYSTEM','SYSAUX'))"
            )
            quota_query = (
                f"SELECT 'QUOTA|{side}|'||username||'|'||tablespace_name||'|'||bytes||'|'||max_bytes "
                f"FROM dba_ts_quotas WHERE username={_literal(owners[side].upper())} "
                "ORDER BY username,tablespace_name;\n"
            )
        sql.extend([
            _tagged(f"{side}.switch_container", f"ALTER SESSION SET CONTAINER={pdb_q};\n"),
            _tagged(f"{side}.context_assert", "DECLARE BEGIN IF UPPER(SYS_CONTEXT('USERENV','CON_NAME')) <> "
            f"{_literal(containers[side])} THEN RAISE_APPLICATION_ERROR(-20203,'PDB session context mismatch'); END IF; END;\n/\n",
            ),
            _tagged(f"{side}.context", f"SELECT 'CTX|{side}|'||SYS_CONTEXT('USERENV','CON_NAME') FROM dual;\n"),
            _tagged(f"{side}.users", f"SELECT 'USER|{side}|'||p.principal_label||'|'||p.principal_username||'|'||NVL(u.account_status,'MISSING')||'|'||NVL(u.common,'-')||'|'||NVL(u.default_tablespace,'-')||'|'||NVL(u.temporary_tablespace,'-') FROM ({principal_rows}) p LEFT JOIN dba_users u ON u.username=p.principal_username ORDER BY p.principal_label;\n"),
            _tagged(f"{side}.tablespaces", tablespace_query),
            _tagged(f"{side}.datafiles", f"SELECT 'DATAFILE|{side}|'||tablespace_name||'|'||REPLACE(file_name,'|','/')||'|'||bytes||'|'||status FROM dba_data_files WHERE {datafile_filter} ORDER BY tablespace_name,file_name;\n"),
            _tagged(f"{side}.quotas", quota_query),
            _tagged(f"{side}.system_grants", f"SELECT 'SYS_PRIV|{side}|'||grantee||'|'||privilege||'|'||admin_option FROM dba_sys_privs WHERE grantee IN ({principal_names}) ORDER BY grantee,privilege;\n"),
            _tagged(f"{side}.role_grants", f"SELECT 'ROLE_GRANT|{side}|'||grantee||'|'||granted_role||'|'||admin_option||'|'||default_role FROM dba_role_privs WHERE grantee IN ({principal_names}) ORDER BY grantee,granted_role;\n"),
            _tagged(f"{side}.object_grants", f"SELECT 'OBJECT_GRANT|{side}|'||grantee||'|'||owner||'|'||table_name||'|'||privilege||'|'||grantable FROM dba_tab_privs WHERE grantee IN ({principal_names}) ORDER BY grantee,owner,table_name,privilege;\n"),
            _tagged(f"{side}.owner_objects", f"SELECT 'OWNER_OBJECT|{side}|'||e.owner_name||'|'||NVL(o.object_type,'NONE')||'|'||NVL(o.status,'NONE')||'|'||COUNT(o.object_name) FROM (SELECT {_literal(owners[side].upper())} AS owner_name FROM dual) e LEFT JOIN dba_objects o ON o.owner=e.owner_name GROUP BY e.owner_name,o.object_type,o.status ORDER BY o.object_type,o.status;\n"),
        ])
    sql.extend(["PROMPT ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE\n", "EXIT SUCCESS\n"])
    rendered = "".join(sql)
    if re.search(r"(?im)^\s*(?:CREATE|ALTER\s+(?!SESSION)|DROP|GRANT|REVOKE|INSERT|UPDATE|DELETE|TRUNCATE)\b", rendered):
        raise AssertionError("Read-only inventory generated a non-read-only statement")
    return rendered


def run_inventory(
    cfg: E2EConfig,
    *,
    executable: str | None = None,
    runner: Callable[..., Any] | None = None,
    source_admin: str | None = None,
    target_admin: str | None = None,
    tablespaces: tuple[str, ...] = (),
) -> str:
    """Run inventory through local OS-authenticated SQL*Plus; return records."""
    e2e = cfg.raw.get("e2e", {})
    profile = e2e.get("onboarding_profile", {}) if isinstance(e2e, dict) else {}
    dba_policy = e2e.get("dba_bootstrap", {}) if isinstance(e2e, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    dba_policy = dba_policy if isinstance(dba_policy, dict) else {}
    inventory_sql = build_inventory_sql(
        cfg,
        source_admin=source_admin,
        target_admin=target_admin,
        tablespaces=tablespaces,
    )
    command = executable or profile.get("sqlplus_executable") or dba_policy.get("sqlplus_executable")
    oracle_sid = profile.get("local_oracle_sid") or dba_policy.get("local_oracle_sid")
    output = run_sqlplus_script(
        inventory_sql,
        operation="Read-only SQL*Plus inventory",
        completion_marker="ORACLE_READ_ONLY_BOOTSTRAP_INVENTORY_COMPLETE",
        executable=command,
        oracle_sid=oracle_sid,
        runner=runner,
        timeout=90,
    )
    expected = {"source", "target"}
    records = "\n".join(line.strip() for line in output.splitlines())
    cdb_records = [line for line in records.splitlines() if line.startswith("CDB|")]
    if len(cdb_records) != 1 or not cdb_records[0].split("|", 2)[1] or not cdb_records[0].endswith("|CDB$ROOT"):
        raise RuntimeError("Read-only inventory did not confirm its local CDB$ROOT identity")
    expected_cdb = profile.get("expected_db_unique_name") or dba_policy.get("expected_db_unique_name")
    if expected_cdb and cdb_records[0].split("|", 2)[1].casefold() != str(expected_cdb).casefold():
        raise RuntimeError("Read-only inventory connected to a different CDB than configured")
    found_contexts = {match.group(1).lower() for match in re.finditer(r"(?m)^CTX\|(source|target)\|", records)}
    found_pdbs = {match.group(1).lower() for match in re.finditer(r"(?m)^PDB\|(source|target)\|", records)}
    if found_contexts != expected or found_pdbs != expected:
        raise RuntimeError("Read-only inventory omitted a configured PDB identity/context record")
    _owners, services = validated_oracle_context(cfg)
    policy = cfg.raw.get("e2e", {}).get("owner_provisioning", {})
    for side in expected:
        expected_pdb = str(policy.get(side, {}).get("expected_container_name") or services[side]).upper()
        if f"CTX|{side}|{expected_pdb}" not in records:
            raise RuntimeError(f"Read-only inventory did not confirm configured {side.upper()} PDB context")
        pdb_record = next((line for line in records.splitlines() if line.startswith(f"PDB|{side}|")), "")
        if pdb_record != f"PDB|{side}|{expected_pdb}|READ WRITE":
            raise RuntimeError(f"Read-only inventory did not confirm configured {side.upper()} PDB is READ WRITE")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Oracle DBA bootstrap state inventory")
    parser.add_argument("--config", required=True)
    parser.add_argument("--sqlplus", help="Optional configured SQL*Plus executable")
    parser.add_argument("--source-admin", help="Configured PDB-local SOURCE admin to include in the inventory")
    parser.add_argument("--target-admin", help="Configured PDB-local TARGET admin to include in the inventory")
    parser.add_argument("--tablespace", action="append", default=[], help="Application tablespace to inspect (repeatable)")
    args = parser.parse_args(argv)
    try:
        cfg = load_e2e_config(args.config)
        output = run_inventory(
            cfg,
            executable=args.sqlplus,
            source_admin=args.source_admin,
            target_admin=args.target_admin,
            tablespaces=tuple(args.tablespace),
        )
    except Exception as exc:  # noqa: BLE001 - user-facing CLI never prints credentials
        print(f"[ORACLE READ-ONLY INVENTORY] FAIL: {exc}", file=sys.stderr)
        return 1
    print(format_inventory_summary(output))
    return 0


def format_inventory_summary(output: str, *, detail_limit: int = 20) -> str:
    """Render the relevant inventory compactly, with explicit bootstrap gaps."""
    lines = [line.strip() for line in output.splitlines() if "|" in line]
    identity = [line for line in lines if line.startswith(("CDB|", "PDB|", "CTX|", "USER|"))]
    categories = ("TABLESPACE", "DATAFILE", "QUOTA", "SYS_PRIV", "ROLE_GRANT", "OBJECT_GRANT", "OWNER_OBJECT")
    gaps: list[str] = []
    for side in ("source", "target"):
        if not any(line.startswith(f"TABLESPACE|{side}|") for line in lines):
            gaps.append(f"{side.upper()} has no non-SYSTEM permanent application tablespace")
    for line in lines:
        fields = line.split("|")
        if fields[0] == "USER" and fields[4] != "OPEN":
            gaps.append(f"{fields[2]} {fields[3]} account={fields[4]}")
        elif fields[0] == "TABLESPACE" and fields[3] == "MISSING":
            gaps.append(f"{fields[2]} application tablespace missing")
        elif fields[0] == "QUOTA" and fields[4] == "MISSING":
            gaps.append(f"{fields[2]} quota missing on {fields[3]}")
        elif fields[0] == "QUOTA" and len(fields) > 5 and fields[5] == "-1":
            gaps.append(f"{fields[2]} quota on {fields[3]} is unlimited")
    result = ["ORACLE READ-ONLY BOOTSTRAP INVENTORY", *identity]
    for category in categories:
        items = [line for line in lines if line.startswith(category + "|")]
        result.append(f"{category}: {len(items)} record(s)")
        result.extend(items[:detail_limit])
        if len(items) > detail_limit:
            result.append(f"  ... {len(items) - detail_limit} more {category} record(s)")
    result.append("BOOTSTRAP STATUS: " + ("PARTIAL — " + "; ".join(gaps) if gaps else "no missing configured owner/storage/quota detected"))
    result.append("Oracle grants are not inferred from inventory; review the listed configured-principal grants against the approved policy.")
    return "\n".join(result)


if __name__ == "__main__":
    raise SystemExit(main())
