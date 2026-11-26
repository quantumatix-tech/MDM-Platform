"""Explicit Oracle E2E owner provisioning, storage bootstrap, and preflight."""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from core.connectors.base import quote_identifier
from core.connectors.oracle._models import oracle_error_code
from core.connectors.oracle.objects.security import (
    ORACLE_BOOTSTRAP_PRIVILEGES,
    required_owner_privileges,
)
from tests.e2e.acceptance import E2EConfig
from tests.e2e.oracle.config import (
    connect,
    connection_config,
    resolver_for_side,
    validated_oracle_context,
)
from tests.e2e.oracle.owner_policy import is_valid_oracle_identifier

_SYSTEM_STORAGE = {"SYSTEM", "SYSAUX"}
_TABLESPACE_SIZE = re.compile(r"[1-9][0-9]*(?:[KMG])\Z", re.IGNORECASE)


def _query(conn: Any, sql: str, binds: dict[str, Any] | None = None) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, binds or {})
        rows = list(cur.fetchall())
    except BaseException:
        # A cursor-close failure must not replace the Oracle error that explains
        # why this catalog query failed.
        with suppress(Exception):
            cur.close()
        raise
    else:
        cur.close()
        return rows


def _provisioning_oracle_error(
    *, side: str, operation: str, statement: str, exc: Exception, secrets: tuple[str, ...]
) -> RuntimeError:
    """Preserve actionable Oracle operation diagnostics without exposing credentials."""
    code = oracle_error_code(exc)
    code_text = f"ORA-{code:05d}" if code is not None else "unavailable"
    diagnostic = exc.args[0] if getattr(exc, "args", ()) else None
    message = getattr(diagnostic, "message", None)
    if not isinstance(message, str) or not message.strip():
        message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
            message = message.replace(secret.replace('"', '""'), "<redacted>")
    return RuntimeError(
        f"Oracle owner provisioning failed (stage=provisioning.{side}, operation={operation}, "
        f"error_code={code_text}, exception={type(exc).__name__}): {message}; SQL={statement}"
    )


def _identity(conn: Any, *, side: str | None = None) -> tuple[str, str, str]:
    sql = "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
    try:
        rows = _query(conn, sql)
    except Exception as exc:  # noqa: BLE001 - preserve the original Oracle error and query
        raise _provisioning_oracle_error(
            side=side or "connection",
            operation="verify session identity",
            statement=sql,
            exc=exc,
            secrets=(),
        ) from None
    if len(rows) != 1:
        raise RuntimeError("Oracle session identity could not be verified")
    return tuple(str(value).strip() for value in rows[0])


def _check_identity(
    conn: Any, *, username: str, service: str, container: str, side: str | None = None
) -> None:
    actual_user, actual_container, actual_service = _identity(conn, side=side)
    if actual_user.casefold() != username.casefold():
        raise RuntimeError("Oracle connected username does not match configuration")
    if actual_container.casefold() != container.casefold():
        raise RuntimeError("Oracle connected PDB does not match configured expected container")
    if actual_service.casefold() != service.casefold():
        raise RuntimeError("Oracle connected service does not match configuration")


def _assert_local_admin(conn: Any, *, username: str, side: str) -> None:
    sql = "SELECT common FROM dba_users WHERE username = :username"
    try:
        rows = _query(conn, sql, {"username": username.upper()})
    except Exception as exc:  # noqa: BLE001 - preserve the catalog access error
        raise _provisioning_oracle_error(
            side=side,
            operation="verify PDB-local admin catalog visibility",
            statement=sql,
            exc=exc,
            secrets=(),
        ) from None
    if not rows:
        raise RuntimeError(
            f"Oracle {side} admin is not visible in DBA_USERS; verify catalog access and the configured admin account"
        )
    if str(rows[0][0]).upper() != "NO":
        raise RuntimeError(f"Oracle {side} admin must be PDB-local, not a common account")


def _side_policy(cfg: E2EConfig, side: str, *, provisioning: bool = True) -> dict[str, Any]:
    block = cfg.raw.get("e2e", {}).get("owner_provisioning", {})
    if not isinstance(block, dict):
        raise TypeError("e2e.owner_provisioning configuration block is required")
    if provisioning and block.get("enabled") is not True:
        raise ValueError(
            "e2e.owner_provisioning.enabled must be true and both SOURCE/TARGET owner provisioning blocks must be configured"
        )
    policy = block.get(side)
    if not isinstance(policy, dict):
        raise TypeError(f"e2e.owner_provisioning.{side} configuration is required")
    container = policy.get("expected_container_name")
    if not is_valid_oracle_identifier(container):
        raise ValueError(
            f"e2e.owner_provisioning.{side}.expected_container_name must be a valid identifier"
        )
    if provisioning:
        tablespace = policy.get("permanent_tablespace")
        quota = policy.get("quota_bytes")
        admin = policy.get("admin_connection")
        if not is_valid_oracle_identifier(tablespace) or tablespace.upper() in _SYSTEM_STORAGE:
            raise ValueError(
                f"e2e.owner_provisioning.{side}.permanent_tablespace must name an approved non-SYSTEM permanent tablespace"
            )
        if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
            raise ValueError(
                f"e2e.owner_provisioning.{side}.quota_bytes must be a positive finite integer"
            )
        if (
            not isinstance(admin, dict)
            or not isinstance(admin.get("username"), str)
            or not admin["username"].strip()
            or not isinstance(admin.get("password_secret"), str)
            or not admin["password_secret"].strip()
        ):
            raise ValueError(
                f"e2e.owner_provisioning.{side}.admin_connection username and password_secret are required"
            )
        if admin.get("auth_mode", "password") != "password":
            raise ValueError(
                f"e2e.owner_provisioning.{side}.admin_connection.auth_mode must be password; local OS SYSDBA is unsupported by the configured Thin-mode driver path"
            )
    return policy


def validate_onboarding_config(cfg: E2EConfig) -> None:
    """Report all missing owner/storage prerequisites in one configuration error."""
    e2e = cfg.raw.get("e2e", {})
    source_mode = e2e.get("source_mode", "fixtures") if isinstance(e2e, dict) else "fixtures"
    if source_mode not in {"fixtures", "preloaded"}:
        raise ValueError("e2e.source_mode must be 'fixtures' or 'preloaded'")
    if source_mode == "preloaded":
        owner_policy = e2e.get("owner_provisioning", {})
        storage = e2e.get("storage_bootstrap", {})
        problems = []
        if isinstance(owner_policy, dict) and owner_policy.get("enabled") is True:
            problems.append("e2e.owner_provisioning.enabled must be false in preloaded mode")
        if isinstance(storage, dict) and storage.get("enabled") is True:
            problems.append("e2e.storage_bootstrap.enabled must be false in preloaded mode")
        if isinstance(e2e.get("dba_bootstrap", {}), dict) and e2e.get("dba_bootstrap", {}).get("enabled") is True:
            problems.append("e2e.dba_bootstrap.enabled must be false in preloaded mode")
        if problems:
            raise ValueError("Preloaded Oracle E2E is read-only on SOURCE and requires existing storage: " + "; ".join(problems))
        return
    owner_policy = e2e.get("owner_provisioning", {}) if isinstance(e2e, dict) else {}
    problems: list[str] = []
    if not isinstance(owner_policy, dict):
        owner_policy = {}
    if owner_policy.get("enabled") is not True:
        problems.append("e2e.owner_provisioning.enabled must be true")
    for side in ("source", "target"):
        policy = owner_policy.get(side)
        if not isinstance(policy, dict):
            policy = {}
            problems.append(f"e2e.owner_provisioning.{side} block is missing")
        if not is_valid_oracle_identifier(policy.get("expected_container_name")):
            problems.append(f"e2e.owner_provisioning.{side}.expected_container_name is required")
        admin = policy.get("admin_connection")
        if (
            not isinstance(admin, dict)
            or not isinstance(admin.get("username"), str)
            or not admin["username"].strip()
        ):
            problems.append(f"e2e.owner_provisioning.{side}.admin_connection.username is required")
        if (
            not isinstance(admin, dict)
            or not isinstance(admin.get("password_secret"), str)
            or not admin["password_secret"].strip()
        ):
            problems.append(
                f"e2e.owner_provisioning.{side}.admin_connection.password_secret is required"
            )
        if isinstance(admin, dict) and admin.get("auth_mode", "password") != "password":
            problems.append(
                f"e2e.owner_provisioning.{side}.admin_connection.auth_mode must be password; local OS SYSDBA is unsupported"
            )
        if (
            not is_valid_oracle_identifier(policy.get("permanent_tablespace"))
            or str(policy.get("permanent_tablespace", "")).upper() in _SYSTEM_STORAGE
        ):
            problems.append(
                f"e2e.owner_provisioning.{side}.permanent_tablespace must be an approved non-SYSTEM permanent tablespace"
            )
        quota = policy.get("quota_bytes")
        if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
            problems.append(
                f"e2e.owner_provisioning.{side}.quota_bytes must be a positive finite byte quota"
            )

    storage = e2e.get("storage_bootstrap", {}) if isinstance(e2e, dict) else {}
    if isinstance(storage, dict) and storage.get("enabled") is True:
        for side in ("source", "target"):
            try:
                _storage_policy(cfg, side)
            except (TypeError, ValueError) as exc:
                problems.append(str(exc))
    if problems:
        raise ValueError(
            "Oracle E2E onboarding configuration is incomplete: " + "; ".join(problems)
        )


def _storage_policy(cfg: E2EConfig, side: str) -> tuple[str, str | None, str]:
    block = cfg.raw.get("e2e", {}).get("storage_bootstrap", {})
    if not isinstance(block, dict) or block.get("enabled") is not True:
        raise ValueError(
            "e2e.storage_bootstrap.enabled must be true before configuring tablespace creation"
        )
    side_config = block.get(side)
    if not isinstance(side_config, dict):
        raise TypeError(f"e2e.storage_bootstrap.{side} configuration is required")
    mode = side_config.get("mode", "explicit_path")
    size = side_config.get("size")
    if mode not in {"reuse_existing", "explicit_path", "oracle_managed_files"}:
        raise ValueError(
            f"e2e.storage_bootstrap.{side}.mode must be reuse_existing, explicit_path, or oracle_managed_files"
        )
    datafile = side_config.get("datafile_path")
    if mode == "reuse_existing":
        if datafile not in (None, "") or side_config.get("size") is not None:
            raise ValueError(
                f"e2e.storage_bootstrap.{side} reuse_existing mode must omit datafile_path and size"
            )
        return mode, None, ""
    if mode == "explicit_path":
        if (
            not isinstance(datafile, str)
            or not datafile.strip()
            or any(ord(ch) < 32 for ch in datafile)
        ):
            raise ValueError(
                f"e2e.storage_bootstrap.{side}.datafile_path must be an explicit absolute path"
            )
        windows_path = PureWindowsPath(datafile)
        posix_path = PurePosixPath(datafile)
        if not windows_path.is_absolute() and not posix_path.is_absolute():
            raise ValueError(f"e2e.storage_bootstrap.{side}.datafile_path must be absolute")
    elif datafile not in (None, ""):
        raise ValueError(
            f"e2e.storage_bootstrap.{side}.datafile_path must be omitted in oracle_managed_files mode"
        )
    if (
        not isinstance(size, (str, int))
        or isinstance(size, bool)
        or not _TABLESPACE_SIZE.fullmatch(str(size).strip())
    ):
        raise ValueError(
            f"e2e.storage_bootstrap.{side}.size must include an explicit Oracle K/M/G unit, such as 512M"
        )
    return (
        mode,
        datafile.strip() if isinstance(datafile, str) and datafile.strip() else None,
        str(size).strip().upper(),
    )


def _same_datafile_path(actual: str, configured: str) -> bool:
    actual_windows = PureWindowsPath(actual)
    configured_windows = PureWindowsPath(configured)
    if actual_windows.is_absolute() or configured_windows.is_absolute():
        return actual_windows.as_posix().casefold() == configured_windows.as_posix().casefold()
    return PurePosixPath(actual).as_posix() == PurePosixPath(configured).as_posix()


def bootstrap_storage(cfg: E2EConfig, *, driver: Any, apply_storage: bool) -> dict[str, str]:
    """Ensure approved tablespaces exist; create them only with config and CLI opt-in."""
    block = cfg.raw.get("e2e", {}).get("storage_bootstrap", {})
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return {
            "status": "SKIP",
            "reason": "storage bootstrap disabled; provisioned storage will be checked by owner provisioning",
        }
    _owners, services = validated_oracle_context(cfg)
    prepared: list[dict[str, Any]] = []
    connections: list[Any] = []
    try:
        # Validate all side configuration before connecting or issuing DDL.
        for side in ("source", "target"):
            owner_policy = _side_policy(cfg, side)
            mode, datafile, size = _storage_policy(cfg, side)
            admin = owner_policy["admin_connection"]
            resolver = resolver_for_side(cfg.raw, side, owner_policy.get("secret_provider"))
            try:
                password = resolver.resolve(admin["password_secret"])
            except Exception:  # noqa: BLE001 - provider errors may include secret details
                raise RuntimeError(
                    f"Oracle {side} storage administrator credential could not be resolved"
                ) from None
            if not isinstance(password, str) or not password:
                raise RuntimeError(f"Oracle {side} storage administrator credential is unavailable")
            endpoint = cfg.raw[side]["connection"]
            conn = connect(
                {**endpoint, "username": admin["username"], "password": password}, driver
            )
            connections.append(conn)
            _check_identity(
                conn,
                username=admin["username"],
                service=services[side],
                container=owner_policy["expected_container_name"],
                side=side,
            )
            _assert_local_admin(conn, username=admin["username"], side=side)
            tablespace = owner_policy["permanent_tablespace"].upper()
            if tablespace in _SYSTEM_STORAGE:
                raise ValueError("Application tablespaces may not use SYSTEM or SYSAUX")
            existing = _query(
                conn,
                "SELECT contents, status FROM dba_tablespaces WHERE tablespace_name = :ts",
                {"ts": tablespace},
            )
            file_rows = _query(
                conn,
                "SELECT file_name FROM dba_data_files WHERE tablespace_name = :ts AND status = 'AVAILABLE'",
                {"ts": tablespace},
            )
            if existing:
                if (
                    len(existing) != 1
                    or str(existing[0][0]).upper() != "PERMANENT"
                    or str(existing[0][1]).upper() != "ONLINE"
                ):
                    raise RuntimeError(
                        f"Oracle {side} configured tablespace exists but is not an online permanent tablespace"
                    )
                if not file_rows:
                    raise RuntimeError(
                        f"Oracle {side} configured tablespace has no available datafiles"
                    )
                if mode == "explicit_path" and not any(
                    _same_datafile_path(str(row[0]), datafile or "") for row in file_rows
                ):
                    raise RuntimeError(
                        f"Oracle {side} configured tablespace datafile does not match the explicitly configured path"
                    )
                prepared.append(
                    {
                        "side": side,
                        "conn": conn,
                        "tablespace": tablespace,
                        "mode": mode,
                        "exists": True,
                        "datafile": datafile,
                        "size": size,
                    }
                )
                continue
            if not apply_storage:
                raise RuntimeError(
                    f"Oracle {side} tablespace is missing. Approve/configure storage and rerun the E2E command with --apply; no tablespace DDL was executed."
                )
            if mode == "reuse_existing":
                raise RuntimeError(
                    f"Oracle {side} was configured to reuse an existing tablespace, but the configured permanent tablespace is missing; ask the DBA to create approved storage or select a creation mode"
                )
            if mode == "oracle_managed_files":
                destination_rows = _query(
                    conn,
                    "SELECT value FROM v$parameter WHERE name = 'db_create_file_dest'",
                )
                if not destination_rows or not str(destination_rows[0][0] or "").strip():
                    raise RuntimeError(
                        f"Oracle {side} has no configured db_create_file_dest for Oracle Managed Files; ask the DBA to configure a PDB-local OMF destination or use an explicitly approved datafile path"
                    )
            privileges = {
                str(row[0]).upper() for row in _query(conn, "SELECT privilege FROM session_privs")
            }
            if "CREATE TABLESPACE" not in privileges:
                raise RuntimeError(
                    f"Oracle {side} storage administrator lacks CREATE TABLESPACE; ask a DBA to provision the configured permanent tablespace"
                )
            prepared.append(
                {
                    "side": side,
                    "conn": conn,
                    "tablespace": tablespace,
                    "mode": mode,
                    "exists": False,
                    "datafile": datafile,
                    "size": size,
                }
            )
        created: list[str] = []
        for plan in prepared:
            if plan["exists"]:
                continue
            ts_q = quote_identifier(plan["tablespace"])
            if plan["mode"] == "oracle_managed_files":
                statement = (
                    f"CREATE TABLESPACE {ts_q} DATAFILE SIZE {plan['size']} PERMANENT ONLINE"
                )
            else:
                datafile = plan["datafile"].replace("'", "''")
                statement = f"CREATE TABLESPACE {ts_q} DATAFILE '{datafile}' SIZE {plan['size']} PERMANENT ONLINE"
            cur = plan["conn"].cursor()
            try:
                cur.execute(statement)
            except Exception as exc:  # noqa: BLE001 - driver errors may include the SQL text
                raise RuntimeError(
                    f"Oracle {plan['side']} tablespace creation failed ({type(exc).__name__}); ask the DBA to inspect storage before retrying"
                ) from None
            finally:
                cur.close()
            created.append(plan["side"])
        return {"status": "PASS", "created_sides": ",".join(created) or "none"}
    finally:
        for conn in connections:
            with suppress(Exception):
                conn.close()


def _fixture_privileges(cfg: E2EConfig) -> set[str]:
    """Derive only fixture-required object privileges from reviewed fixture SQL."""
    from pathlib import Path

    from tests.e2e.oracle.config import fixture_names_and_expected_tables
    from tests.e2e.oracle.setup.fixture_loader import parse_fixture_sql, resolve_fixture_files

    names, _ = fixture_names_and_expected_tables(cfg, allow_source_discovery=True)
    if cfg.raw.get("e2e", {}).get("source_mode") == "preloaded" and not names:
        return {"CREATE SESSION"}
    configured = cfg.raw.get("e2e", {}).get("fixtures", [])
    inline = [
        item for item in configured if isinstance(item, dict) and isinstance(item.get("sql"), str)
    ]
    if inline and len(inline) == len(configured):
        statements = [
            statement
            for item in inline
            for statement in parse_fixture_sql(item["sql"], filename=item.get("file", "<fixture>"))
        ]
    else:
        directory = Path(cfg.to_abs_fixture_path(__file__))
        files = resolve_fixture_files(directory, names)
        statements = [
            statement
            for path in files
            for statement in parse_fixture_sql(path.read_text(encoding="utf-8"), filename=path.name)
        ]
    kinds: set[str] = set()
    for prefix, privilege in (
        ("TABLE", "CREATE TABLE"),
        ("TYPE", "CREATE TYPE"),
        ("VIEW", "CREATE VIEW"),
        (r"MATERIALIZED\s+VIEW", "CREATE MATERIALIZED VIEW"),
        ("SEQUENCE", "CREATE SEQUENCE"),
        ("PROCEDURE", "CREATE PROCEDURE"),
        ("FUNCTION", "CREATE PROCEDURE"),
        ("TRIGGER", "CREATE TRIGGER"),
        ("SYNONYM", "CREATE SYNONYM"),
    ):
        if any(
            re.match(r"(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?" + prefix + r"\b", statement)
            for statement in statements
        ):
            kinds.add(privilege)
    policy = required_owner_privileges(
        tables={"fixture"} if "CREATE TABLE" in kinds else None,
        types=[object()] if "CREATE TYPE" in kinds else None,
        views=[object()] if "CREATE VIEW" in kinds else None,
        materialized_views=[object()] if "CREATE MATERIALIZED VIEW" in kinds else None,
        sequences=[object()] if "CREATE SEQUENCE" in kinds else None,
        routines=[object()] if "CREATE PROCEDURE" in kinds else None,
        triggers=[object()] if "CREATE TRIGGER" in kinds else None,
        synonyms=[object()] if "CREATE SYNONYM" in kinds else None,
    )
    required = set(policy) | {"CREATE SESSION"}
    unsupported = required - (set(ORACLE_BOOTSTRAP_PRIVILEGES) | {"CREATE SESSION"})
    if unsupported:
        raise RuntimeError("Fixture requires privileges outside the Oracle owner policy")
    return required


def provision(cfg: E2EConfig, *, driver: Any, apply: bool) -> dict[str, Any]:
    """Preflight both sides, then create or complete only empty configured owners."""
    if not apply:
        raise ValueError("Provisioning requires the explicit --apply option")
    owners, services = validated_oracle_context(cfg)
    requirements = _fixture_privileges(cfg)
    plans: list[dict[str, Any]] = []
    connections: list[Any] = []
    try:
        for side in ("source", "target"):
            policy = _side_policy(cfg, side)
            endpoint = cfg.raw[side]["connection"]
            admin = policy["admin_connection"]
            secret_ref = admin["password_secret"]
            resolver = resolver_for_side(cfg.raw, side, policy.get("secret_provider"))
            try:
                password = resolver.resolve(secret_ref)
            except Exception:  # noqa: BLE001 - secret-provider errors may contain sensitive material
                raise RuntimeError(
                    f"Oracle {side} provisioning credential could not be resolved"
                ) from None
            if not isinstance(password, str) or not password:
                raise RuntimeError(f"Oracle {side} provisioning credential is unavailable")
            admin_cfg = {**endpoint, "username": admin["username"], "password": password}
            conn = connect(admin_cfg, driver)
            connections.append(conn)
            _check_identity(
                conn,
                username=admin["username"],
                service=services[side],
                container=policy["expected_container_name"],
                side=side,
            )
            _assert_local_admin(conn, username=admin["username"], side=side)

            def read(
                operation: str,
                sql: str,
                binds: dict[str, Any] | None = None,
                *,
                _conn: Any = conn,
                _side: str = side,
            ) -> list[tuple]:
                try:
                    return _query(_conn, sql, binds)
                except Exception as exc:  # noqa: BLE001 - retain precise catalog query failure
                    raise _provisioning_oracle_error(
                        side=_side,
                        operation=f"read {operation}",
                        statement=sql,
                        exc=exc,
                        secrets=(),
                    ) from None

            owner_rows = read(
                "owner account",
                "SELECT account_status, common, default_tablespace FROM dba_users WHERE username = :owner",
                {"owner": owners[side].upper()},
            )
            exists = bool(owner_rows)
            ts = read(
                "application tablespace",
                "SELECT contents, status FROM dba_tablespaces WHERE tablespace_name = :ts",
                {"ts": policy["permanent_tablespace"].upper()},
            )
            files = read(
                "available tablespace datafiles",
                "SELECT COUNT(*) FROM dba_data_files WHERE tablespace_name = :ts AND status = 'AVAILABLE'",
                {"ts": policy["permanent_tablespace"].upper()},
            )
            if (
                len(ts) != 1
                or str(ts[0][0]).upper() != "PERMANENT"
                or str(ts[0][1]).upper() != "ONLINE"
                or not files
                or int(files[0][0]) < 1
            ):
                raise RuntimeError(
                    f"Oracle {side} requires an online permanent tablespace with available datafiles; ask the DBA to provision approved storage first"
                )
            existing_owner_privileges: set[str] = set()
            current_quota: int | None = None
            if exists:
                status, common, default_tablespace = (str(value).upper() for value in owner_rows[0])
                if status != "OPEN" or common != "NO":
                    raise RuntimeError(
                        f"Existing Oracle {side} owner must be OPEN and PDB-local; it will not be altered"
                    )
                existing_objects = read(
                    "existing owner object count",
                    "SELECT COUNT(*) FROM dba_objects WHERE owner = :owner",
                    {"owner": owners[side].upper()},
                )
                if (
                    cfg.raw.get("e2e", {}).get("source_mode", "fixtures") == "fixtures" or side == "target"
                ) and (not existing_objects or int(existing_objects[0][0]) != 0):
                    raise RuntimeError(
                        f"Existing Oracle {side.upper()} fixture owner is not empty; no cleanup is performed"
                    )
                if default_tablespace != policy["permanent_tablespace"].upper():
                    raise RuntimeError(
                        f"Existing Oracle {side} owner default tablespace differs from the approved profile; it will not be altered"
                    )
                quota_rows = read(
                    "existing owner tablespace quota",
                    "SELECT max_bytes FROM dba_ts_quotas WHERE username = :owner AND tablespace_name = :ts",
                    {"owner": owners[side].upper(), "ts": policy["permanent_tablespace"].upper()},
                )
                current_quota = int(quota_rows[0][0]) if quota_rows else None
                if current_quota != int(policy["quota_bytes"]):
                    raise RuntimeError(
                        f"Existing Oracle {side} owner quota differs from the approved finite quota; it will not be altered"
                    )
                existing_owner_privileges = {
                    str(row[0]).upper()
                    for row in read(
                        "existing owner system privileges",
                        "SELECT privilege FROM dba_sys_privs WHERE grantee = :owner",
                        {"owner": owners[side].upper()},
                    )
                }

            missing_owner_privileges = requirements - existing_owner_privileges if exists else requirements
            if not exists or missing_owner_privileges:
                direct_rows = read("admin direct system privileges", "SELECT privilege, admin_option FROM user_sys_privs")
                grantable = {
                    str(row[0]).upper() for row in direct_rows if str(row[1]).upper() == "YES"
                }
                direct = {str(row[0]).upper() for row in direct_rows}
                session = {
                    str(row[0]).upper()
                    for row in read("admin effective privileges", "SELECT privilege FROM session_privs")
                }
                if (not exists and "CREATE USER" not in session) or not (
                    "GRANT ANY PRIVILEGE" in direct or missing_owner_privileges <= grantable
                ):
                    raise RuntimeError(
                        f"Oracle {side} provisioning identity lacks CREATE USER or direct grant authority for the exact missing owner privileges"
                    )
            plans.append(
                {
                    "side": side,
                    "conn": conn,
                    "owner": owners[side],
                    "exists": bool(exists),
                    "policy": policy,
                    "owner_secret": None,
                    "missing_privileges": sorted(missing_owner_privileges),
                }
            )
            # Resolve owner password only after every PDB/storage/authority check succeeds.
        for plan in plans:
            if plan["exists"]:
                continue
            side = plan["side"]
            owner_cfg = connection_config(
                cfg, side, plan["owner"], resolver_for_side(cfg.raw, side)
            )
            plan["owner_secret"] = owner_cfg["password"]
        created: list[str] = []
        completed: list[str] = []
        for plan in plans:
            owner_q = quote_identifier(plan["owner"])
            ts_q = quote_identifier(plan["policy"]["permanent_tablespace"])
            cur = plan["conn"].cursor()
            operation = "no-op"
            statement = "<none>"
            failure: RuntimeError | None = None
            try:
                if not plan["exists"]:
                    password = plan["owner_secret"].replace('"', '""')
                    quota = plan["policy"]["quota_bytes"]
                    operation = f"CREATE USER {plan['owner']}"
                    statement = f'CREATE USER {owner_q} IDENTIFIED BY "<redacted>" DEFAULT TABLESPACE {ts_q} QUOTA {quota} ON {ts_q}'
                    ddl = f'CREATE USER {owner_q} IDENTIFIED BY "{password}" DEFAULT TABLESPACE {ts_q} QUOTA {quota} ON {ts_q}'
                    cur.execute(ddl)
                for privilege in plan["missing_privileges"]:
                    operation = f"GRANT {privilege} TO {plan['owner']}"
                    statement = f"GRANT {privilege} TO {owner_q}"
                    cur.execute(statement)
            except Exception as exc:  # noqa: BLE001 - Oracle errors can echo the statement
                failure = _provisioning_oracle_error(
                    side=plan["side"],
                    operation=operation,
                    statement=statement,
                    exc=exc,
                    secrets=(str(plan.get("owner_secret") or ""),),
                )
            try:
                cur.close()
            except Exception as exc:  # noqa: BLE001 - do not mask the DDL error
                if failure is None:
                    failure = _provisioning_oracle_error(
                        side=plan["side"],
                        operation="close cursor",
                        statement="<cursor.close()>",
                        exc=exc,
                        secrets=(str(plan.get("owner_secret") or ""),),
                    )
            if failure is not None:
                raise failure from None
            if plan["exists"]:
                if plan["missing_privileges"]:
                    completed.append(plan["owner"])
            else:
                created.append(plan["owner"])
        return {
            "created": created,
            "existing": [plan["owner"] for plan in plans if plan["exists"]],
            "completed_existing": completed,
        }
    finally:
        for conn in connections:
            with suppress(Exception):
                conn.close()


def preflight_owner(
    conn: Any,
    *,
    owner: str,
    service: str,
    container: str,
    required: set[str],
    tablespace: str | None,
    quota_bytes: int | None,
    require_empty: bool,
    require_quota: bool = True,
) -> dict[str, Any]:
    """Read-only owner checks; reports privilege/quota/empty failures closed."""
    _check_identity(conn, username=owner, service=service, container=container)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM user_objects")
        object_count = int(cur.fetchone()[0])
        if require_empty and object_count:
            raise RuntimeError(
                f"Oracle owner is not empty ({object_count} objects); no cleanup is performed"
            )
        cur.execute("SELECT privilege FROM user_sys_privs")
        direct = {str(row[0]).upper() for row in cur.fetchall()}
        cur.execute("SELECT privilege FROM session_privs")
        effective = {str(row[0]).upper() for row in cur.fetchall()}
        if tablespace:
            cur.execute(
                "SELECT max_bytes FROM user_ts_quotas WHERE tablespace_name = :ts",
                {"ts": tablespace.upper()},
            )
        else:
            cur.execute("SELECT max_bytes FROM user_ts_quotas")
        quota_rows = cur.fetchall()
    missing = sorted(
        priv
        for priv in required
        if priv
        not in (
            direct
            if priv in {"CREATE PROCEDURE", "CREATE TRIGGER", "CREATE MATERIALIZED VIEW"}
            else effective
        )
    )
    quota_ok = any(
        int(row[0]) > 0 and (quota_bytes is None or int(row[0]) == quota_bytes)
        for row in quota_rows
    )
    if missing:
        raise RuntimeError("Oracle owner lacks required privileges: " + ", ".join(missing))
    if require_quota and not quota_ok:
        raise RuntimeError("Oracle owner quota does not match the configured limited quota")
    return {
        "owner": owner,
        "object_count": object_count,
        "privileges": sorted(required),
        "quota_ok": quota_ok,
    }
