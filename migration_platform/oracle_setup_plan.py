"""Oracle owner and permission-executor setup planner and approved applier.

PLAN performs SELECT-only inspection. APPLY accepts only validated structured
operations from a freshly regenerated, explicitly approved plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import yaml

from core.connectors.base import quote_identifier
from core.connectors.oracle._models import make_connect_kwargs
from core.connectors.oracle.objects.security import required_owner_privileges
from core.secrets import create_secret_provider

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_SECRET_REFERENCE = re.compile(r"^[A-Za-z0-9_]+$")
_SOURCE_CATALOG = (
    "ALL_TAB_PRIVS_MADE", "ALL_COL_PRIVS_MADE",
    "DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS",
)
_REPLACEMENT_CATALOG = ("DBA_DEPENDENCIES", "DBA_CONSTRAINTS")
_EXECUTOR_CATALOG = (
    "DBA_USERS", "DBA_ROLES", "DBA_ROLE_PRIVS", "DBA_SYS_PRIVS",
    "DBA_TAB_PRIVS", "DBA_COL_PRIVS",
)
_MIB = 1024 * 1024
_QUOTA_FREE_SPACE_SHARE_NUMERATOR = 1
_QUOTA_FREE_SPACE_SHARE_DENOMINATOR = 2
_OPERATION_TYPES = {
    "CREATE_OWNER", "CREATE_EXECUTOR", "ASSIGN_FINITE_QUOTA",
    "GRANT_SYSTEM_PRIVILEGE", "GRANT_CATALOG_ACCESS",
    "GRANT_APPROVED_ADMIN_OPTION",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _validate_operation(operation: dict[str, Any]) -> None:
    required = {
        "operation_id", "operation_type", "scope", "expected_identity", "service",
        "account", "current_state", "desired_state", "parameters", "authority",
        "dependencies", "approval_required", "verification",
    }
    if set(operation) not in (required, required | {"preview_sql"}):
        raise ValueError("structured setup operation has an invalid field set")
    if operation["operation_type"] not in _OPERATION_TYPES:
        raise ValueError("unsupported structured setup operation type")
    if operation["scope"] not in {"SOURCE", "TARGET"}:
        raise ValueError("structured setup operation scope must be SOURCE or TARGET")
    _ident(operation["account"], "operation account")
    if not isinstance(operation["parameters"], dict) or not isinstance(operation["dependencies"], list):
        raise TypeError("structured setup operation parameters/dependencies are malformed")
    if any(key in operation["parameters"] for key in ("sql", "statement", "password")):
        raise ValueError("structured setup operations cannot contain executable SQL or password values")
    if (operation["operation_type"] in {"CREATE_OWNER", "CREATE_EXECUTOR"}
            and not _SECRET_REFERENCE.fullmatch(str(operation["parameters"].get("password_secret_ref", "")))):
        raise ValueError("account creation requires a resolved password secret reference")
    kind = operation["operation_type"]
    params = operation["parameters"]
    expected_parameter_keys = {
        "CREATE_OWNER": {"password_secret_ref", "permanent_tablespace"},
        "CREATE_EXECUTOR": {"password_secret_ref"},
        "ASSIGN_FINITE_QUOTA": {"tablespace", "quota_bytes"},
        "GRANT_SYSTEM_PRIVILEGE": {"privilege"},
        "GRANT_CATALOG_ACCESS": {"view"},
        "GRANT_APPROVED_ADMIN_OPTION": {"privilege"},
    }[kind]
    parameter_keys_ok = (set(params) in ({"privilege"}, {"role"})
                         if kind == "GRANT_APPROVED_ADMIN_OPTION"
                         else set(params) == expected_parameter_keys)
    if not parameter_keys_ok:
        raise ValueError(f"invalid parameters for {kind}")
    if kind == "CREATE_OWNER":
        _ident(params.get("permanent_tablespace"), "CREATE_OWNER permanent_tablespace")
    if kind == "GRANT_SYSTEM_PRIVILEGE" and params.get("privilege") not in (
            set(_full_capability_requirements()) | {"CREATE SESSION", "CREATE USER", "CREATE ROLE"}):
        raise ValueError("unsupported system privilege in setup operation")
    if kind == "GRANT_CATALOG_ACCESS" and params.get("view") not in set(_SOURCE_CATALOG + _REPLACEMENT_CATALOG + _EXECUTOR_CATALOG):
        raise ValueError("unsupported catalog view in setup operation")
    if kind == "GRANT_APPROVED_ADMIN_OPTION":
        if not operation["approval_required"]:
            raise ValueError("admin-option operations must require explicit review")
        if params.get("privilege") not in {None, "CREATE SESSION"}:
            raise ValueError("unsupported privilege in admin-option operation")
        if params.get("role") is not None:
            _ident(params["role"], "admin-option role")
    if operation["operation_type"] == "ASSIGN_FINITE_QUOTA":
        if (not isinstance(operation["parameters"].get("quota_bytes"), int)
                or isinstance(operation["parameters"].get("quota_bytes"), bool)
                or operation["parameters"]["quota_bytes"] <= 0):
            raise ValueError("finite quota must be a positive integer byte count")
        _ident(operation["parameters"].get("tablespace"), "operation tablespace")
    if not isinstance(operation["approval_required"], bool):
        raise TypeError("structured setup operation approval_required must be boolean")
    if "preview_sql" in operation and not isinstance(operation["preview_sql"], str):
        raise ValueError("structured setup operation preview_sql must be text")
    identity = {key: value for key, value in operation.items()
                if key not in {"operation_id", "preview_sql"}}
    expected_id = hashlib.sha256(_canonical(identity)).hexdigest()[:16]
    if operation["operation_id"] != expected_id:
        raise ValueError("structured operation identifier does not match its content")


def _make_operation(*, operation_type: str, side: str, identity: dict[str, str],
                    service: str, account: str, current: Any, desired: Any,
                    parameters: dict[str, Any], authority: dict[str, Any],
                    dependencies: list[str] | None = None, approval_required: bool = True,
                    verification: dict[str, Any]) -> dict[str, Any]:
    content = {
        "operation_type": operation_type, "scope": side.upper(),
        "expected_identity": identity, "service": service, "account": account,
        "current_state": current, "desired_state": desired,
        "parameters": parameters, "authority": authority,
        "dependencies": dependencies or [], "approval_required": approval_required,
        "verification": verification,
    }
    content["operation_id"] = hashlib.sha256(_canonical(content)).hexdigest()[:16]
    _validate_operation(content)
    return content


def validate_plan_document(plan: dict[str, Any]) -> None:
    """Validate exported structured plans without executing them."""
    if not isinstance(plan, dict) or plan.get("schema") != "oracle-setup-plan/v1":
        raise ValueError("unsupported Oracle setup plan document")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise TypeError("plan operations must be a list")
    ids: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise TypeError("plan operation must be an object")
        _validate_operation(operation)
        if operation["operation_id"] in ids:
            raise ValueError("duplicate operation identifier")
        ids.add(operation["operation_id"])
    for operation in operations:
        if any(dependency not in ids for dependency in operation["dependencies"]):
            raise ValueError("structured setup operation has an unresolved dependency")
    expected = hashlib.sha256(_canonical(plan.get("fingerprint_payload"))).hexdigest()
    if plan.get("fingerprint") != expected:
        raise ValueError("plan fingerprint does not match its structured payload")


def _execute_setup_operation(conn: Any, operation: dict[str, Any], provider: Any) -> None:
    """Render and run one allow-listed operation; never consume preview_sql."""
    _validate_operation(operation)
    kind, params = operation["operation_type"], operation["parameters"]
    account = quote_identifier(_ident(operation["account"], "operation account"))
    if kind in {"CREATE_OWNER", "CREATE_EXECUTOR"}:
        password, state = _secret(provider, params["password_secret_ref"], "account password")
        if state != "AVAILABLE" or password is None:
            raise LookupError("required account secret is unavailable")
        password_literal = '"' + password.replace('"', '""') + '"'
        statement = f"CREATE USER {account} IDENTIFIED BY {password_literal}"
        if kind == "CREATE_OWNER":
            statement += f" DEFAULT TABLESPACE {quote_identifier(_ident(params['permanent_tablespace'], 'tablespace'))}"
    elif kind == "ASSIGN_FINITE_QUOTA":
        statement = (f"ALTER USER {account} QUOTA {int(params['quota_bytes'])} ON "
                     f"{quote_identifier(_ident(params['tablespace'], 'tablespace'))}")
    elif kind == "GRANT_SYSTEM_PRIVILEGE":
        statement = f"GRANT {params['privilege']} TO {account}"
    elif kind == "GRANT_CATALOG_ACCESS":
        statement = f"GRANT SELECT ON SYS.{params['view']} TO {account}"
    elif kind == "GRANT_APPROVED_ADMIN_OPTION":
        name = params.get("privilege") or quote_identifier(_ident(params["role"], "role"))
        statement = f"GRANT {name} TO {account} WITH ADMIN OPTION"
    else:  # _validate_operation already checks this, retained as defense in depth.
        raise ValueError("unsupported Oracle setup operation")
    with conn.cursor() as cursor:
        cursor.execute(statement)
    conn.commit()


def _verify_setup_operation(conn: Any, operation: dict[str, Any]) -> bool:
    """Check the exact postcondition for an operation through read-only queries."""
    account = operation["account"]
    kind, params = operation["operation_type"], operation["parameters"]
    if kind in {"CREATE_OWNER", "CREATE_EXECUTOR"}:
        rows = _run(conn, "SELECT account_status FROM dba_users WHERE username=:username",
                    {"username": account})
        return bool(rows and str(rows[0][0]).upper() == "OPEN")
    if kind == "ASSIGN_FINITE_QUOTA":
        rows = _run(conn, "SELECT max_bytes FROM dba_ts_quotas WHERE username=:username AND tablespace_name=:tablespace",
                    {"username": account, "tablespace": params["tablespace"]})
        return bool(rows and int(rows[0][0]) == params["quota_bytes"])
    if kind == "GRANT_SYSTEM_PRIVILEGE":
        rows = _run(conn, "SELECT privilege FROM dba_sys_privs WHERE grantee=:username AND privilege=:privilege",
                    {"username": account, "privilege": params["privilege"]})
        return bool(rows)
    if kind == "GRANT_CATALOG_ACCESS":
        rows = _run(conn, "SELECT 1 FROM dba_tab_privs WHERE owner='SYS' AND table_name=:catalog_name "
                    "AND grantee=:username AND privilege='SELECT'",
                    {"username": account, "catalog_name": params["view"]})
        return bool(rows)
    if kind == "GRANT_APPROVED_ADMIN_OPTION":
        if params.get("privilege"):
            rows = _run(conn, "SELECT admin_option FROM dba_sys_privs WHERE grantee=:username AND privilege=:privilege",
                        {"username": account, "privilege": params["privilege"]})
            return bool(rows and str(rows[0][0]).upper() == "YES")
        rows = _run(conn, "SELECT admin_option FROM dba_role_privs WHERE grantee=:username AND granted_role=:role",
                    {"username": account, "role": params["role"]})
        return bool(rows and str(rows[0][0]).upper() == "YES")
    return False


def _assert_operation_precondition(conn: Any, operation: dict[str, Any]) -> None:
    """Reject material changes from the reviewed current state before DDL."""
    if operation["operation_type"] != "ASSIGN_FINITE_QUOTA":
        return
    params = operation["parameters"]
    rows = _run(conn,
        "SELECT max_bytes FROM dba_ts_quotas WHERE username=:username AND tablespace_name=:tablespace",
        {"username": operation["account"], "tablespace": params["tablespace"]})
    actual = int(rows[0][0]) if rows and rows[0][0] is not None else 0
    state = operation["current_state"]
    expected = state.get("quota_bytes", 0)
    if expected == "NOT CREATED":
        expected = 0
    if actual != expected:
        raise RuntimeError("approved operation state changed; obtain a fresh plan")


def _assert_live_operation_authority(conn: Any, operation: dict[str, Any]) -> None:
    """Recheck the executing session's exact grant authority immediately pre-DDL."""
    if operation["authority"].get("requires_sysdba"):
        rows = _run(conn, "SELECT SYS_CONTEXT('USERENV','ISDBA') FROM dual")
        if len(rows) != 1 or str(rows[0][0]).upper() != "TRUE":
            raise RuntimeError("SYSDBA execution authority was not verified")
        return
    if operation["operation_type"] == "GRANT_CATALOG_ACCESS":
        rows = _run(conn,
            "SELECT 1 FROM user_tab_privs_recd WHERE owner='SYS' AND table_name=:catalog_name "
            "AND privilege='SELECT' AND grantable='YES'",
            {"catalog_name": operation["parameters"]["view"]})
        if not rows:
            raise RuntimeError("exact direct SYS object grant option is no longer present")
        return
    privileges = {str(row[0]).upper() for row in _run(conn, "SELECT privilege FROM session_privs")}
    roles = {str(row[0]).upper() for row in _run(conn, "SELECT role FROM session_roles")}
    sys_options = {str(row[0]).upper() for row in _run(
        conn, "SELECT privilege FROM user_sys_privs WHERE admin_option='YES'")}
    role_options = {str(row[0]).upper() for row in _run(
        conn, "SELECT granted_role FROM user_role_privs WHERE admin_option='YES'")}
    elevated = bool(roles & {"DBA", "PDB_DBA"})
    kind, params = operation["operation_type"], operation["parameters"]
    if kind in {"CREATE_OWNER", "CREATE_EXECUTOR"}:
        allowed = elevated or "CREATE USER" in privileges
    elif kind == "ASSIGN_FINITE_QUOTA":
        allowed = elevated or "ALTER USER" in privileges
    elif kind == "GRANT_CATALOG_ACCESS":
        # Oracle's ANY authority does not extend to SYS-owned dictionary
        # objects. These exact GRANT SELECT ON SYS.* operations use SYSDBA.
        allowed = False
    elif kind == "GRANT_APPROVED_ADMIN_OPTION":
        allowed = elevated or (
            params.get("privilege") in sys_options if params.get("privilege")
            else params.get("role") in role_options
        )
    else:
        privilege = params["privilege"]
        allowed = elevated or "GRANT ANY PRIVILEGE" in privileges or privilege in sys_options
    if not allowed:
        raise RuntimeError("administrator execution authority changed or is insufficient")


def apply_approved_plan(plan: dict[str, Any], *, approved_fingerprint: str,
                        config: dict[str, Any], provider: Any,
                        admin_credentials: dict[str, dict[str, str]],
                        privileged_credentials: dict[str, dict[str, str]] | None = None,
                        connect: Callable[..., Any], fresh_plan: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Apply a current, fingerprint-approved plan with per-operation identity checks.

    `connect` must be a test double in offline tests; production CLI supplies the
    Oracle driver connector. The caller is responsible for collecting explicit
    interactive approval before invoking this function.
    """
    validate_plan_document(plan)
    if plan.get("approval_ready") is not True or plan.get("operations") is None:
        raise ValueError("plan is not ready for approval")
    if approved_fingerprint != plan["fingerprint"]:
        raise ValueError("approval fingerprint does not match plan")
    current = fresh_plan()
    validate_plan_document(current)
    if current["fingerprint"] != plan["fingerprint"]:
        raise RuntimeError("stale plan; obtain a fresh plan and approval")
    completed: list[str] = []
    privileged_credentials = privileged_credentials or {}
    operations = _ordered_operations(plan["operations"])
    privileged_by_side = {
        operation["scope"].lower(): operation for operation in operations
        if operation["authority"].get("requires_sysdba") is True
    }
    # Validate every required privileged endpoint before the first DDL. This
    # prevents ordinary owner setup from being partially applied when the
    # additional SYSDBA credential is absent or belongs to another PDB.
    for side, operation in privileged_by_side.items():
        credentials = privileged_credentials.get(side, {})
        if not credentials.get("username") or not credentials.get("password"):
            return {"status": "SETUP FAILED", "completed": [],
                    "failed_operation": operation["operation_id"],
                    "failed_description": _operation_description(operation),
                    "failed_scope": operation["scope"], "execution_stage": "SYSDBA credential preflight",
                    "remaining": [item["operation_id"] for item in operations],
                    "completed_count": 0, "remaining_count": len(operations) - 1,
                    "reason": "required SYSDBA credentials were not supplied",
                    "corrective_action": "Rerun the same APPLY command and provide an authorized SYSDBA credential for this PDB service."}
        endpoint = dict(config[side]["connection"])
        conn = None
        try:
            conn = _connect_sysdba_runtime(endpoint, credentials["username"], credentials["password"],
                                           connect, provider)
            identity = _identity(conn)
            expected = operation["expected_identity"]
            if (any(identity.get(key, "").casefold() != str(expected.get(key, "")).casefold()
                           for key in ("db_unique_name", "container_name", "service_name"))):
                raise RuntimeError("administrator endpoint identity changed")
            _assert_live_operation_authority(conn, operation)
        except Exception as exc:  # noqa: BLE001 - sanitized before console reporting.
            error_code, safe_message = _safe_error_details(exc, [str(credentials["password"])])
            return {"status": "SETUP FAILED", "completed": [],
                    "failed_operation": operation["operation_id"],
                    "failed_description": _operation_description(operation),
                    "failed_scope": operation["scope"], "execution_stage": "SYSDBA credential preflight",
                    "remaining": [item["operation_id"] for item in operations],
                    "completed_count": 0, "remaining_count": len(operations) - 1,
                    "oracle_error_code": error_code, "reason": safe_message,
                    "corrective_action": "No setup DDL was executed. Confirm this credential can authenticate AS SYSDBA on the displayed configured PDB service, then rerun APPLY."}
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()
    for index, operation in enumerate(operations):
        _validate_operation(operation)
        if any(dep not in completed for dep in operation["dependencies"]):
            return {"status": "SETUP FAILED", "completed": completed,
                    "failed_operation": operation["operation_id"],
                    "failed_description": _operation_description(operation),
                    "failed_scope": operation["scope"], "execution_stage": "dependency validation",
                    "remaining": [op["operation_id"] for op in operations[index:]],
                    "completed_count": len(completed),
                    "remaining_count": len(operations[index + 1:]),
                    "reason": "a required setup operation was not completed",
                    "corrective_action": "Rerun the same APPLY command to inspect current state and review a fresh dependency-ordered plan."}
        side = operation["scope"].lower()
        requires_sysdba = operation["authority"].get("requires_sysdba") is True
        admin = privileged_credentials.get(side, {}) if requires_sysdba else admin_credentials.get(side, {})
        if not admin.get("username") or not admin.get("password"):
            return {"status": "SETUP FAILED", "completed": completed,
                    "failed_operation": operation["operation_id"],
                    "failed_description": _operation_description(operation),
                    "failed_scope": operation["scope"], "execution_stage": "credential preflight",
                    "remaining": [op["operation_id"] for op in operations[index:]],
                    "completed_count": len(completed),
                    "remaining_count": len(operations[index + 1:]),
                    "reason": ("required SYSDBA credentials were not supplied" if requires_sysdba
                               else "runtime PDB administrator credentials unavailable"),
                    "corrective_action": "Rerun the same APPLY command and provide credentials for the configured PDB endpoint."}
        admin_cfg = dict(config[side]["connection"])
        admin_cfg["username"] = admin["username"]
        conn = None
        stage = "connection and endpoint identity verification"
        try:
            if requires_sysdba:
                conn = _connect_sysdba_runtime(admin_cfg, admin["username"], admin["password"], connect, provider)
            else:
                conn = _connect_runtime(admin_cfg, admin["password"], connect, provider)
            identity = _identity(conn)
            expected = operation["expected_identity"]
            if ((not requires_sysdba
                 and identity["session_user"].casefold() != str(admin["username"]).casefold())
                    or any(identity.get(key, "").casefold() != str(expected.get(key, "")).casefold()
                           for key in ("db_unique_name", "container_name", "service_name"))):
                raise RuntimeError("administrator endpoint identity changed")
            stage = "operation authority validation"
            _assert_live_operation_authority(conn, operation)
            # Recheck whether the approved operation is still needed. An already
            # satisfied state is an idempotent skip; unexpected state is stale.
            stage = "current-state precondition inspection"
            if operation["operation_type"] in {"CREATE_OWNER", "CREATE_EXECUTOR"}:
                if _verify_setup_operation(conn, operation):
                    raise RuntimeError("approved account creation became stale; obtain a fresh plan")
            elif _verify_setup_operation(conn, operation):
                completed.append(operation["operation_id"])
                continue
            _assert_operation_precondition(conn, operation)
            stage = "operation execution"
            _execute_setup_operation(conn, operation, provider)
            stage = "postcondition verification"
            if not _verify_setup_operation(conn, operation):
                raise RuntimeError("postcondition verification failed")
            completed.append(operation["operation_id"])
        except Exception as exc:  # noqa: BLE001 - driver details are sanitized below.
            secret_values = [str(credential["password"]) for credentials in
                             (admin_credentials, privileged_credentials) for credential in credentials.values()
                             if isinstance(credential, dict) and credential.get("password")]
            overlay_values = getattr(provider, "values", {})
            if isinstance(overlay_values, dict):
                secret_values.extend(str(value) for value in overlay_values.values())
            error_code, safe_message = _safe_error_details(exc, secret_values)
            if error_code == "ORA-01031" and requires_sysdba:
                corrective_action = ("Confirm the supplied credential is authorized for SYSDBA on this PDB service, "
                                     "then regenerate and review the plan before retrying.")
            elif stage == "postcondition verification":
                corrective_action = ("Rerun the same APPLY command. It will inspect current accounts, quotas, "
                                     "privileges and grants, then propose only missing setup operations.")
            else:
                corrective_action = ("Correct the reported endpoint or authority issue, then rerun the same APPLY "
                                     "command; the new plan will inspect current state before proposing changes.")
            return {"status": "SETUP FAILED", "completed": completed,
                    "failed_operation": operation["operation_id"],
                    "failed_description": _operation_description(operation),
                    "failed_scope": operation["scope"], "execution_stage": stage,
                    "remaining": [op["operation_id"] for op in operations[index + 1:]],
                    "completed_count": len(completed),
                    "remaining_count": len(operations[index + 1:]),
                    "oracle_error_code": error_code,
                    "reason": safe_message,
                    "corrective_action": corrective_action}
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()
    verified = fresh_plan()
    validate_plan_document(verified)
    status = "SETUP COMPLETE" if verified.get("plan_status") == "NO CHANGES REQUIRED" else "SETUP INCOMPLETE"
    return {"status": status, "completed": completed,
            "remaining_requirements": verified.get("blockers", []),
            "verification_fingerprint": verified.get("fingerprint")}


def _status(status: str, current: str, required: str, *, action: str | None = None,
            authority: str | None = None, approval: bool = False,
            security: str | None = None, verify: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status, "current": current, "required": required,
    }
    if action:
        result["proposed_operation"] = action
    if authority:
        result["executor"] = authority
    if approval:
        result["approval_required"] = True
    if security:
        result["security_implication"] = security
    if verify:
        result["verification"] = verify
    return result


def _ident(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.strip()):
        raise ValueError(f"{label} must be a simple Oracle identifier")
    return value.strip().upper()


def _read_secret_value(provider: Any, ref: str) -> str:
    """Read through the repository resolver or provider interface."""
    resolve = getattr(provider, "resolve", None)
    if callable(resolve):
        return resolve(ref)
    get_secret = getattr(provider, "get_secret", None)
    if callable(get_secret):
        return get_secret(ref)
    raise TypeError("configured secret resolver does not expose resolve/get_secret")


class _SecretState(str):
    """String-compatible status with a sanitized, non-secret cause for UNKNOWN."""
    def __new__(cls, status: str, reason: str | None = None):
        instance = super().__new__(cls, status)
        instance.reason = reason
        return instance


def _secret(provider: Any, ref: object, label: str) -> tuple[str | None, str]:
    if not isinstance(ref, str) or not _SECRET_REFERENCE.fullmatch(ref.strip()):
        return None, _SecretState("UNKNOWN", "secret reference is invalid")
    try:
        value = _read_secret_value(provider, ref.strip())
    except KeyError:
        return None, "MISSING"
    except Exception as exc:  # noqa: BLE001 - provider failure is distinct from an absent secret
        return None, _SecretState("UNKNOWN", f"secret provider lookup failed ({type(exc).__name__})")
    if value is None or value == "":
        return None, "MISSING"
    if not isinstance(value, str):
        return None, _SecretState("UNKNOWN", "secret provider returned a non-text value")
    return value, "AVAILABLE"


class _RuntimeSecretOverlay:
    """Read-only provider overlay for secrets supplied through hidden prompts."""
    def __init__(self, base: Any):
        self.base = base
        self.values: dict[str, str] = {}

    def get_secret(self, name: str) -> str:
        if name in self.values:
            return self.values[name]
        return _read_secret_value(self.base, name)


def _run(conn: Any, sql: str, binds: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    """Execute one SELECT and return rows; deliberately rejects other SQL."""
    if not sql.lstrip().upper().startswith("SELECT "):
        raise ValueError("Oracle setup PLAN permits SELECT statements only")
    with conn.cursor() as cursor:
        cursor.execute(sql, binds or {})
        return list(cursor.fetchall())


def _connection_config(side_cfg: dict[str, Any], username: str, password: str) -> dict[str, Any]:
    result = dict(side_cfg.get("connection", {}))
    result["username"] = username
    result["password"] = password
    return result


def _connect(config: dict[str, Any], provider: Any, connector: Callable[..., Any] | None = None) -> Any:
    import oracledb

    resolved = dict(config)
    # Runtime checks may connect as a different account (for example, the
    # permission executor) while reusing the endpoint's host/service fields.
    # In that case _connection_config supplies the account's already-resolved
    # password. Prefer it over the endpoint owner's secret reference.
    password = config.get("password")
    if password is None:
        password, _ = _secret(provider, config.get("password_secret"), "password")
    if password is None:
        raise LookupError("configured password secret is unavailable")
    resolved["password"] = password
    wallet_ref = config.get("ssl_wallet_password_secret")
    if wallet_ref:
        wallet, _ = _secret(provider, wallet_ref, "wallet password")
        if wallet is None:
            raise LookupError("configured wallet secret is unavailable")
        resolved["_ssl_wallet_password"] = wallet
    kwargs = make_connect_kwargs(resolved, oracledb)
    return (connector or oracledb.connect)(**kwargs)


def _identity(conn: Any) -> dict[str, str]:
    rows = _run(
        conn,
        "SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CURRENT_SCHEMA'), "
        "SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), "
        "SYS_CONTEXT('USERENV','DB_NAME'), SYS_CONTEXT('USERENV','INSTANCE_NAME'), "
        "SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME'), "
        "SYS_CONTEXT('USERENV','SERVER_HOST') FROM dual",
    )
    if len(rows) != 1 or any(rows[0][index] is None for index in (0, 2, 5, 6)):
        raise RuntimeError("session/PDB identity could not be verified")
    return dict(zip(("session_user", "current_schema", "db_unique_name", "db_name",
                     "instance_name", "container_name", "service_name", "server_host"),
                    (str(item).strip() for item in rows[0])))


def _connect_runtime(config: dict[str, Any], password: str,
                     connector: Callable[..., Any] | None = None,
                     provider: Any | None = None) -> Any:
    """Connect using a runtime-only password without placing it in config/report."""
    import oracledb

    resolved = dict(config)
    resolved["password"] = password
    wallet_ref = resolved.get("ssl_wallet_password_secret")
    if wallet_ref:
        wallet, _ = _secret(provider, wallet_ref, "wallet password")
        if wallet is None:
            raise LookupError("configured wallet secret is unavailable")
        resolved["_ssl_wallet_password"] = wallet
    kwargs = make_connect_kwargs(resolved, oracledb)
    return (connector or oracledb.connect)(**kwargs)


def _connect_sysdba_runtime(config: dict[str, Any], username: str, password: str,
                            connector: Callable[..., Any] | None = None,
                            provider: Any | None = None) -> Any:
    """Connect to the configured PDB service with explicit SYSDBA privilege."""
    import oracledb

    resolved = dict(config)
    resolved["username"] = username
    resolved["password"] = password
    wallet_ref = resolved.get("ssl_wallet_password_secret")
    if wallet_ref:
        wallet, _ = _secret(provider, wallet_ref, "wallet password")
        if wallet is None:
            raise LookupError("configured wallet secret is unavailable")
        resolved["_ssl_wallet_password"] = wallet
    kwargs = make_connect_kwargs(resolved, oracledb)
    mode = getattr(oracledb, "AUTH_MODE_SYSDBA", getattr(oracledb, "SYSDBA", None))
    if mode is None:
        raise RuntimeError("installed python-oracledb driver does not expose SYSDBA mode")
    kwargs["mode"] = mode
    return (connector or oracledb.connect)(**kwargs)


def _operation_description(operation: dict[str, Any]) -> str:
    kind, account, params = operation["operation_type"], operation["account"], operation["parameters"]
    if kind == "GRANT_CATALOG_ACCESS":
        suffix = (f" (requires SYSDBA in {operation['scope']})"
                  if operation["authority"].get("requires_sysdba") else " (direct grant option verified)")
        return f"grant SELECT on SYS.{params['view']} to {account}{suffix}"
    if kind == "CREATE_OWNER":
        return f"create owner {account}"
    if kind == "CREATE_EXECUTOR":
        return f"create permission executor {account}"
    if kind == "ASSIGN_FINITE_QUOTA":
        return f"assign finite quota to {account} on {params['tablespace']}"
    if kind == "GRANT_SYSTEM_PRIVILEGE":
        return f"grant {params['privilege']} to {account}"
    if kind == "GRANT_APPROVED_ADMIN_OPTION":
        return f"grant approved admin option to {account}"
    return f"{kind.lower().replace('_', ' ')} for {account}"


def _safe_error_details(exc: Exception, secrets: list[str]) -> tuple[str | None, str]:
    """Return a sanitized Oracle code/message without echoing SQL or credentials."""
    error = exc.args[0] if exc.args else exc
    code_value = getattr(error, "code", None)
    message_value = getattr(error, "message", None)
    if not isinstance(message_value, str):
        message_value = error if isinstance(error, str) else None
    code = None
    if isinstance(code_value, int) and code_value > 0:
        code = f"ORA-{code_value:05d}"
    elif isinstance(code_value, str):
        match = re.search(r"ORA-\d{5}", code_value, re.IGNORECASE)
        code = match.group(0).upper() if match else None
    if message_value:
        match = re.search(r"ORA-\d{5}:\s*[^\r\n]+", message_value, re.IGNORECASE)
        if match:
            safe_message = match.group(0)
            code = code or safe_message[:9].upper()
        elif type(exc) is RuntimeError and message_value in {
                "administrator endpoint identity changed",
                "SYSDBA execution authority was not verified",
                "exact direct SYS object grant option is no longer present",
                "approved operation state changed; obtain a fresh plan",
                "postcondition verification failed",
                "administrator execution authority changed or is insufficient"}:
            safe_message = message_value
        else:
            safe_message = f"database operation failed ({type(exc).__name__})"
    else:
        safe_message = f"database operation failed ({type(exc).__name__})"
    for secret in secrets:
        if secret:
            safe_message = safe_message.replace(secret, "<redacted>")
    safe_message = " ".join(safe_message.split())[:400]
    return code, safe_message


def _full_capability_requirements() -> dict[str, bool]:
    """Ask the connector mapping for its complete supported DDL capability set."""
    return required_owner_privileges(
        tables={"<future-table>"}, views=[object()], sequences=[object()],
        routines=[object()], triggers=[object()], materialized_views=[object()],
        synonyms=[object()], types=[object()],
    )


def _recommend_finite_quota(free_bytes: int | None) -> int | None:
    """Recommend half of currently free extents, rounded down to whole MiB.

    This reserves at least half of the observed free extents for shared PDB
    use. Small storage regions that cannot support a 1 MiB quota are blocked.
    """
    if not isinstance(free_bytes, int) or isinstance(free_bytes, bool) or free_bytes <= 0:
        return None
    share = (free_bytes * _QUOTA_FREE_SPACE_SHARE_NUMERATOR
             // _QUOTA_FREE_SPACE_SHARE_DENOMINATOR)
    quota = share // _MIB * _MIB
    return quota if quota >= _MIB else None


def _format_size_mib(value: int | None) -> str:
    if value is None:
        return "unavailable"
    if value % _MIB == 0:
        return f"{value // _MIB} MiB"
    return f"{value / _MIB:.1f} MiB"


def _ordered_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a stable dependency-respecting order for both display and APPLY."""
    pending = list(operations)
    ordered: list[dict[str, Any]] = []
    completed: set[str] = set()
    while pending:
        ready_index = next((index for index, op in enumerate(pending)
                            if set(op["dependencies"]) <= completed), None)
        if ready_index is None:
            raise ValueError("setup operation dependencies contain a cycle")
        operation = pending.pop(ready_index)
        ordered.append(operation)
        completed.add(operation["operation_id"])
    return ordered


def _blocked_diagnostics(plan: dict[str, Any]) -> list[str]:
    """Render actionable blocked/unknown component details without raw errors."""
    findings: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if value.get("status") in {"BLOCKED", "UNKNOWN"}:
                reason = value.get("current") or value.get("reason") or "State could not be verified"
                detail = f"{path}: {reason}"
                required = value.get("required")
                if required and required != reason:
                    detail += f"; required: {required}"
                if value.get("remediation"):
                    detail += f"; action: {value['remediation']}"
                findings.append(detail)
            for key, nested in value.items():
                if key in {"status", "current", "required", "remediation"}:
                    continue
                visit(nested, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]")

    for section in ("sides", "permission_executor"):
        if section in plan:
            visit(plan[section], section)
    for blocker in plan.get("blockers", []):
        detail = f"{blocker.get('code', 'BLOCKED')}: {blocker.get('message', 'Setup requirement is blocked')}"
        if blocker.get("remediation"):
            detail += f"; action: {blocker['remediation']}"
        findings.append(detail)
    return list(dict.fromkeys(findings))


def _safe_error_code(exc: BaseException) -> str:
    """Return only a standard Oracle/driver code; never echo driver details."""
    match = re.search(r"\b(ORA|DPY|TNS)-(\d{5})\b", str(exc), re.IGNORECASE)
    return f"{match.group(1).upper()}-{match.group(2)}" if match else type(exc).__name__


def _identity_data(side_report: dict[str, Any]) -> dict[str, str]:
    raw = side_report.get("identity", {}).get("current")
    try:
        value = json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _format_plan_console(plan: dict[str, Any]) -> str:
    """Render concise human review output without dumping authority inventories."""
    lines = ["ORACLE SETUP PLAN"]
    for side in ("source", "target"):
        report = plan.get("sides", {}).get(side, {})
        ident = _identity_data(report)
        identity_status = report.get("identity", {}).get("status", "UNKNOWN")
        connection = "PASS" if identity_status == "READY" else "FAIL — " + str(report.get("identity", {}).get("current", "unverified"))
        owner_status = report.get("owner", {}).get("status", "UNKNOWN")
        owner = "Missing — will create" if owner_status == "MISSING" else str(report.get("owner", {}).get("current", owner_status))
        tablespace = report.get("tablespace", {}).get("current", "unavailable")
        if tablespace == "Owner not yet created" or not tablespace:
            tablespace = report.get("storage_discovery", {}).get("default_permanent_tablespace") or tablespace
        quota_report = report.get("quota", {})
        quota_bytes = quota_report.get("approved_quota_bytes", quota_report.get("recommendation_bytes"))
        if quota_bytes is not None and quota_report.get("status") in {"MISSING", "UNKNOWN", "BLOCKED"}:
            quota = _format_size_mib(quota_bytes)
        else:
            quota = str(quota_report.get("current", quota_report.get("status", "unavailable")))
            size_match = re.fullmatch(r"(\d+) bytes", quota)
            if size_match:
                quota = _format_size_mib(int(size_match.group(1)))
        missing_privileges = sum(
            1 for key, item in report.get("object_privileges", {}).items()
            if key not in {"scope", "inventory"} and item.get("status") == "MISSING")
        missing_catalogs = sum(1 for item in report.get("catalog_visibility", {}).values()
                               if item.get("status") == "MISSING")
        unknown_catalogs = sum(1 for item in report.get("catalog_visibility", {}).values()
                               if item.get("status") == "UNKNOWN")
        database = ident.get("container_name") or ident.get("db_name") or "unverified"
        lines.extend([
            "", side.upper(),
            f"  Connection: {connection}",
            f"  Database: {database}",
            f"  Owner: {owner}",
            f"  Tablespace: {tablespace}",
            f"  Quota: {quota}",
            f"  Missing privileges: {missing_privileges}",
            f"  Missing catalog grants: {missing_catalogs}",
            f"  Unverified catalog grants: {unknown_catalogs}",
        ])
    executor = plan.get("permission_executor", {})
    executor_account = executor.get("account", {}).get("status", "NOT APPLICABLE")
    executor_catalogs = sum(1 for item in executor.get("catalog_visibility", {}).values()
                            if item.get("status") == "MISSING")
    executor_unknown_catalogs = sum(1 for item in executor.get("catalog_visibility", {}).values()
                                    if item.get("status") == "UNKNOWN")
    executor_label = "Missing — will create" if executor_account == "MISSING" else executor_account
    lines.extend(["", "PERMISSION EXECUTOR",
                  f"  Account: {executor_label}",
                  f"  Required catalog grants: {executor_catalogs}",
                  f"  Unverified catalog grants: {executor_unknown_catalogs}"])
    operations = _ordered_operations(plan.get("operations", []))
    count = lambda kinds: sum(1 for op in operations if op["operation_type"] in kinds)
    lines.extend(["", "PROPOSED OPERATIONS",
                  f"  Create accounts: {count({'CREATE_OWNER', 'CREATE_EXECUTOR'})}",
                  f"  Assign quotas: {count({'ASSIGN_FINITE_QUOTA'})}",
                  f"  Grant privileges: {count({'GRANT_SYSTEM_PRIVILEGE'})}",
                  f"  Grant catalog access: {count({'GRANT_CATALOG_ACCESS'})}",
                  f"  Conditional admin-option grants: {count({'GRANT_APPROVED_ADMIN_OPTION'})}"])
    if operations:
        lines.append("  Review exact operations:")
        for op in operations:
            kind, params = op["operation_type"], op["parameters"]
            if kind == "CREATE_OWNER":
                detail = f"create owner {op['account']} in {params['permanent_tablespace']}"
            elif kind == "CREATE_EXECUTOR":
                detail = f"create permission executor {op['account']}"
            elif kind == "ASSIGN_FINITE_QUOTA":
                detail = f"set {op['account']} quota to {_format_size_mib(params['quota_bytes'])} on {params['tablespace']}"
            elif kind == "GRANT_SYSTEM_PRIVILEGE":
                detail = f"grant {params['privilege']} to {op['account']}"
            elif kind == "GRANT_CATALOG_ACCESS":
                suffix = (f"(SYSDBA required on {op['scope']} PDB service)"
                          if op["authority"].get("requires_sysdba")
                          else "(direct grant option verified)")
                detail = f"grant SELECT on SYS.{params['view']} to {op['account']} {suffix}"
            else:
                detail = _preview_operation(op).rstrip(";")
            lines.append(f"    {op['scope']}: {detail}")
    diagnostics = _blocked_diagnostics(plan)
    if diagnostics:
        lines.append("\nBLOCKED / UNKNOWN REQUIREMENTS")
        lines.extend(f"  {detail}" for detail in diagnostics)
    lines.extend(["", f"PLAN STATUS: {plan.get('plan_status', 'UNKNOWN')}",
                  f"PLAN FINGERPRINT: {plan.get('fingerprint', 'UNKNOWN')}"])
    inventory = plan.get("source_permission_inventory", {}).get("status")
    if isinstance(inventory, str) and inventory.startswith("NOT YET APPLICABLE"):
        lines.append(f"SOURCE SECURITY FOLLOW-UP: {inventory}")
    return "\n".join(lines)


def _catalog_probe(conn: Any, view: str) -> dict[str, Any]:
    try:
        reference = f"SYS.{view}" if view.startswith("DBA_") else view
        _run(conn, f"SELECT 1 FROM {reference} WHERE 1=0")
        return {"readable": True, "error_code": None, "reason": "Catalog query succeeded"}
    except Exception as exc:  # noqa: BLE001 - query failures must remain UNKNOWN, never MISSING
        code, message = _safe_error_details(exc, [])
        label = code or type(exc).__name__
        return {"readable": None, "error_code": label,
                "reason": f"Catalog probe failed ({message}); grant state is not inferred from the failed query"}


def _try_catalog(conn: Any, view: str) -> bool | None:
    """Compatibility helper for callers that only need the tri-state result."""
    return _catalog_probe(conn, view)["readable"]


def _owner_requirements(conn: Any, include_tables: list[str] | None = None) -> dict[str, bool]:
    rows = _run(conn, "SELECT object_type, COUNT(*) FROM user_objects GROUP BY object_type")
    counts = {str(kind).upper(): int(count) for kind, count in rows}
    if include_tables is not None:
        if not include_tables:
            raise ValueError("Oracle source include_tables must be a non-empty list when configured")
        names = [_ident(name, "source.connection.include_tables item") for name in include_tables]
        binds = {f"name_{index}": name for index, name in enumerate(names)}
        placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
        table_rows = _run(conn, f"SELECT table_name FROM user_tables WHERE table_name IN ({placeholders})", binds)
        found = {str(row[0]).upper() for row in table_rows}
        missing = sorted(set(names) - found)
        if missing:
            raise ValueError("configured source include_tables were not found in the connected owner")
        counts["TABLE"] = len(found)
    return required_owner_privileges(
        tables={"_"} if counts.get("TABLE", 0) else set(),
        views=[object()] if counts.get("VIEW", 0) else [],
        sequences=[object()] if counts.get("SEQUENCE", 0) else [],
        routines=[object()] if counts.get("PROCEDURE", 0) + counts.get("FUNCTION", 0)
        + counts.get("PACKAGE", 0) else [],
        triggers=[object()] if counts.get("TRIGGER", 0) else [],
        materialized_views=[object()] if counts.get("MATERIALIZED VIEW", 0) else [],
        synonyms=[object()] if counts.get("SYNONYM", 0) else [],
        types=[object()] if counts.get("TYPE", 0) else [],
    )


def _privileges(conn: Any, required: dict[str, bool]) -> tuple[set[str], set[str]]:
    direct = {str(row[0]).upper() for row in _run(conn, "SELECT privilege FROM user_sys_privs")}
    effective = {str(row[0]).upper() for row in _run(conn, "SELECT privilege FROM session_privs")}
    missing = {
        privilege for privilege, direct_only in required.items()
        if privilege not in (direct if direct_only else effective)
    }
    return direct, missing


def _catalog_sql(view: str, grantee: str) -> str:
    return f"GRANT SELECT ON SYS.{view} TO \"{grantee}\";"


def _preview_operation(operation: dict[str, Any]) -> str:
    p = operation["parameters"]
    kind = operation["operation_type"]
    account = quote_identifier(operation["account"])
    if kind == "CREATE_OWNER":
        return (f"CREATE USER {account} IDENTIFIED BY <SECRET:{p['password_secret_ref']}> "
                f"DEFAULT TABLESPACE {quote_identifier(p['permanent_tablespace'])};")
    if kind == "CREATE_EXECUTOR":
        return f"CREATE USER {account} IDENTIFIED BY <SECRET:{p['password_secret_ref']}>;"
    if kind == "ASSIGN_FINITE_QUOTA":
        return f"ALTER USER {account} QUOTA {p['quota_bytes']} ON {quote_identifier(p['tablespace'])};"
    if kind == "GRANT_SYSTEM_PRIVILEGE":
        return f"GRANT {p['privilege']} TO {account};"
    if kind == "GRANT_CATALOG_ACCESS":
        return _catalog_sql(p["view"], operation["account"])
    if kind == "GRANT_APPROVED_ADMIN_OPTION":
        privilege = p.get("privilege")
        role = p.get("role")
        return (f"GRANT {privilege} TO {account} WITH ADMIN OPTION;" if privilege
                else f"GRANT {quote_identifier(role)} TO {account} WITH ADMIN OPTION;")
    raise ValueError("cannot render an unsupported operation preview")


def build_plan(config: dict[str, Any], *, provider: Any | None = None,
               connect: Callable[..., Any] | None = None,
               admin_credentials: dict[str, dict[str, str]] | None = None,
               privileged_credentials: dict[str, dict[str, str]] | None = None,
               confirmed_identities: dict[str, str] | None = None,
               decisions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read configured owners/executor and return a deterministic setup plan."""
    provider = provider or create_secret_provider(config)
    admin_credentials = admin_credentials or {}
    privileged_credentials = privileged_credentials or {}
    confirmed_identities = confirmed_identities or {}
    decisions = decisions or {}
    report: dict[str, Any] = {
        "schema": "oracle-setup-plan/v1",
        "plan_type": "oracle_setup_read_only",
        "execution": "NO SQL DDL OR DML IS EXECUTED",
        "sides": {},
        "permission_executor": {},
        "secrets": {},
        "approval_policy": {},
        "operations": [],
        "blockers": [],
    }
    endpoints: dict[str, dict[str, Any]] = {}
    source_requirements: dict[str, bool] | None = None
    source_object_inventory_known = False
    source_permission_inventory: dict[str, Any] | None = None
    source_permission_inventory_state = "NOT APPLICABLE"
    admin_observations: dict[str, dict[str, Any]] = {}
    capability_requirements = _full_capability_requirements()
    report["supported_object_scope"] = [
        "TABLE", "VIEW", "SEQUENCE", "TRIGGER", "FUNCTION", "PROCEDURE",
        "PACKAGE", "SYNONYM", "TYPE", "MATERIALIZED VIEW",
    ]
    report["approved_capability_privilege_details"] = {
        "required": sorted(capability_requirements),
        "direct_only": sorted(privilege for privilege, direct_only in capability_requirements.items() if direct_only),
        "effective_allowed": sorted(privilege for privilege, direct_only in capability_requirements.items() if not direct_only),
    }
    report["privileged_read_only_inspection"] = {}
    def add_operation(*, operation_type: str, side: str, account: str,
                      current: Any, desired: Any, parameters: dict[str, Any],
                      authority_name: str | None, authority_secret_ref: str | None,
                      required_authority: list[str], dependencies: list[str] | None = None,
                      approval_required: bool = True, verification: dict[str, Any]) -> dict[str, Any]:
        side_report = report["sides"].get(side, {})
        identity_item = side_report.get("identity", {})
        try:
            expected_identity = json.loads(identity_item.get("current", "{}"))
        except (TypeError, json.JSONDecodeError):
            expected_identity = {}
        configured_admin = admin_credentials.get(side, {})
        observed_admin = admin_observations.get(side, {})
        if isinstance(configured_admin, dict):
            authority_name = configured_admin.get("username") or authority_name
        admin_privs = set(observed_admin.get("admin_privileges", []))
        admin_options = set(observed_admin.get("admin_privilege_options", []))
        admin_roles = set(observed_admin.get("admin_roles", []))
        admin_role_options = set(observed_admin.get("admin_role_options", []))
        elevated_role = bool(admin_roles & {"DBA", "PDB_DBA"})
        direct_object_grant_options = observed_admin.get("direct_object_grant_options", {})
        execution_user = str(observed_admin.get("admin_username", "")).upper()
        direct_grant_option = (
            operation_type == "GRANT_CATALOG_ACCESS"
            and direct_object_grant_options.get(execution_user, {}).get(parameters.get("view")) is True
        )
        requires_sysdba = operation_type == "GRANT_CATALOG_ACCESS" and not direct_grant_option
        if operation_type in {"CREATE_OWNER", "CREATE_EXECUTOR"}:
            exact_authority = elevated_role or "CREATE USER" in admin_privs
        elif operation_type == "ASSIGN_FINITE_QUOTA":
            exact_authority = elevated_role or "ALTER USER" in admin_privs
        elif operation_type == "GRANT_CATALOG_ACCESS":
            # SYS-owned dictionary objects are outside ordinary ANY-object
            # authority. Only an exact direct object grant with GRANT OPTION
            # or the SYSDBA path establishes authority for this operation.
            exact_authority = direct_grant_option
        elif operation_type == "GRANT_APPROVED_ADMIN_OPTION":
            if parameters.get("privilege"):
                exact_authority = (elevated_role or "GRANT ANY PRIVILEGE" in admin_privs
                                   or parameters["privilege"] in admin_options)
            else:
                exact_authority = elevated_role or parameters.get("role") in admin_role_options
        else:
            privilege = parameters.get("privilege")
            exact_authority = (elevated_role or "GRANT ANY PRIVILEGE" in admin_privs
                               or privilege in admin_options)
        op = _make_operation(
            operation_type=operation_type, side=side, identity=expected_identity,
            service=str(endpoints.get(side, {}).get("service") or ""), account=account,
            current=current, desired=desired, parameters=parameters,
            authority={"username": authority_name, "credential_source": "runtime_prompt" if authority_name else None,
                       "required_authority": required_authority,
                       "requires_sysdba": requires_sysdba,
                       "execution_identity": ("SYSDBA session on the configured PDB service"
                                              if requires_sysdba else authority_name),
                       "execution_authority_status": (
                           "REQUIRES SYSDBA PREFLIGHT BEFORE DDL" if requires_sysdba
                           else "VERIFIED" if exact_authority else "NOT VERIFIED"),
                       "observed_identity": observed_admin.get("identity"),
                       "observed_system_privileges": observed_admin.get("admin_privileges", []),
                       "observed_roles": observed_admin.get("admin_roles", []),
                       "identity_verified": bool(authority_name and observed_admin.get("identity")),
                       "exact_execution_authority_verified": bool(authority_name and exact_authority),
                       "future_apply_must_revalidate": True},
            dependencies=dependencies, approval_required=approval_required,
            verification=verification,
        )
        preview = _preview_operation(op)
        op["preview_sql"] = preview
        report["operations"].append(op)
        return op

    def add_catalog_operations_for_new_account(side: str, account: str,
                                               views: tuple[str, ...],
                                               dependency: str,
                                               catalog_report: dict[str, Any] | None = None) -> None:
        catalog_report = catalog_report if catalog_report is not None else report["sides"][side]["catalog_visibility"]
        for view in views:
            # ALL_*_MADE views are owner-scoped views available to the owner;
            # DBA_* views require the explicit direct grant shown here.
            if not view.startswith("DBA_"):
                catalog_report[view] = _status(
                    "READY", "Oracle owner-scoped ALL_* catalog view",
                    "Owner-visible grant inventory after account creation",
                    verify=f"SELECT 1 FROM {view} WHERE 1=0;")
                continue
            catalog_report[view] = _status(
                "MISSING", "Account confirmed absent; direct catalog grant required",
                "Direct SELECT visibility after account creation",
                action=_catalog_sql(view, account), authority="PDB-local DBA",
                approval=True, security="Enables the explicitly supported Oracle catalog inventory",
                verify=f"SELECT 1 FROM SYS.{view} WHERE 1=0;")
            add_operation(
                operation_type="GRANT_CATALOG_ACCESS", side=side, account=account,
                current={"access": "MISSING"},
                desired={"view": view, "privilege": "SELECT"},
                parameters={"view": view}, authority_name=None,
                authority_secret_ref=None,
                required_authority=["SYSDBA session on the configured PDB service"],
                dependencies=[dependency],
                verification={"query": view, "expected": "SELECT readable"})

    def set_detected_identity(side: str, identity: dict[str, str]) -> str:
        identity_json = json.dumps(identity, sort_keys=True)
        # A missing owner's initial identity comes from the DBA session. Keep
        # confirmation bound to the database/PDB/service endpoint, not username,
        # so verification remains confirmed after creating that owner.
        endpoint = {key: identity.get(key) for key in
                    ("db_unique_name", "db_name", "instance_name", "container_name", "service_name", "server_host")}
        token = hashlib.sha256(_canonical(endpoint)).hexdigest()[:12]
        report["sides"][side]["identity"] = _status(
            "READY", identity_json, "Database, PDB, service, session user, and current schema discovered")
        report["sides"][side]["identity_confirmation"] = _status(
            "READY" if confirmed_identities.get(side) == token else "UNKNOWN", token,
            "Developer confirms the displayed identity token")
        return token
    # Runtime administrator credentials are optional and never persisted. Their
    # connection is used only for SELECT-based inventory and metadata discovery.
    for side in ("source", "target"):
        admin_cfg = admin_credentials.get(side)
        if not isinstance(admin_cfg, dict):
            continue
        owner_cfg = config.get(side, {})
        owner_conn_cfg = owner_cfg.get("connection", {}) if isinstance(owner_cfg, dict) else {}
        owner_name = owner_conn_cfg.get("username") if isinstance(owner_conn_cfg, dict) else None
        service = owner_conn_cfg.get("service_name") if isinstance(owner_conn_cfg, dict) else None
        admin_name = admin_cfg.get("username")
        admin_password = admin_cfg.get("password")
        admin_observations[side] = {"status": "UNKNOWN", "reason": "not inspected"}
        if not admin_name or not service or str(admin_name).startswith("<") or str(service).startswith("<") or not admin_password:
            admin_observations[side] = {"status": "BLOCKED", "reason": "admin username or service is unresolved"}
            continue
        try:
            _ident(admin_name, f"runtime {side.upper()} administrator.username")
        except ValueError:
            admin_observations[side] = {"status": "BLOCKED", "reason": "setup admin username is not a supported Oracle identifier"}
            continue
        if str(admin_name).upper() in {"SYS", "SYSTEM"}:
            admin_observations[side] = {"status": "BLOCKED", "reason": "SYS/SYSTEM is not accepted for setup administration"}
            continue
        admin_conn = None
        try:
            admin_connection_cfg = dict(owner_conn_cfg)
            admin_connection_cfg["username"] = admin_name
            admin_conn = _connect_runtime(admin_connection_cfg, str(admin_password), connect, provider)
            admin_identity = _identity(admin_conn)
            if (admin_identity["session_user"].casefold() != str(admin_name).casefold()
                    or admin_identity["service_name"].casefold() != str(service).casefold()):
                raise RuntimeError("runtime administrator connected to an unexpected service")
            admin_privileges = {str(row[0]).upper() for row in _run(admin_conn, "SELECT privilege FROM session_privs")}
            admin_roles = {str(row[0]).upper() for row in _run(admin_conn, "SELECT role FROM session_roles")}
            admin_privilege_options = {str(row[0]).upper() for row in _run(
                admin_conn, "SELECT privilege FROM user_sys_privs WHERE admin_option='YES'")}
            admin_role_options = {str(row[0]).upper() for row in _run(
                admin_conn, "SELECT granted_role FROM user_role_privs WHERE admin_option='YES'")}
            storage_rows = _run(admin_conn,
                "SELECT tablespace_name, status, contents FROM dba_tablespaces "
                "WHERE contents='PERMANENT' AND tablespace_name NOT IN ('SYSTEM','SYSAUX') "
                "ORDER BY tablespace_name")
            default_rows = _run(admin_conn,
                "SELECT property_value FROM database_properties "
                "WHERE property_name='DEFAULT_PERMANENT_TABLESPACE'")
            default_tablespace = str(default_rows[0][0]).strip() if default_rows and default_rows[0][0] else None
            free_rows = _run(admin_conn,
                "SELECT tablespace_name, SUM(bytes) FROM dba_free_space GROUP BY tablespace_name")
            free_bytes = {str(row[0]).strip().upper(): int(row[1] or 0) for row in free_rows}
            suitable_tablespaces = [str(row[0]).strip().upper() for row in storage_rows
                if str(row[1]).strip().upper() == "ONLINE"
                and str(row[2]).strip().upper() == "PERMANENT"
                and free_bytes.get(str(row[0]).strip().upper(), 0) > 0]
            storage_info = {"default_permanent_tablespace": default_tablespace.upper() if default_tablespace else None,
                "available_tablespaces": [[str(v).strip() for v in row] for row in storage_rows],
                "suitable_tablespaces": suitable_tablespaces,
                "free_bytes": {name: free_bytes[name] for name in suitable_tablespaces}}
            catalog_access: dict[str, bool | None] = {}
            catalog_access_errors: dict[str, str] = {}
            direct_object_grant_options: dict[str, dict[str, bool | None]] = {str(admin_name).upper(): {}}
            for view in set(_SOURCE_CATALOG + _REPLACEMENT_CATALOG + _EXECUTOR_CATALOG):
                try:
                    grant_rows = _run(admin_conn,
                        "SELECT 1 FROM dba_tab_privs p WHERE p.owner='SYS' AND p.table_name=:catalog_name "
                        "AND p.privilege='SELECT' AND (p.grantee=:username OR p.grantee IN "
                        "(SELECT granted_role FROM dba_role_privs WHERE grantee=:username))",
                        {"catalog_name": view, "username": str(owner_name).upper()})
                    catalog_access[view] = bool(grant_rows)
                    option_rows = _run(admin_conn,
                        "SELECT 1 FROM dba_tab_privs WHERE owner='SYS' AND table_name=:catalog_name "
                        "AND grantee=:username AND privilege='SELECT' AND grantable='YES'",
                        {"catalog_name": view, "username": str(admin_name).upper()})
                    direct_object_grant_options[str(admin_name).upper()][view] = bool(option_rows)
                except Exception as exc:  # noqa: BLE001 - failed grant inspection stays UNKNOWN
                    catalog_access[view] = None
                    catalog_access_errors[view] = _safe_error_details(exc, [])[1]
                    direct_object_grant_options[str(admin_name).upper()][view] = None
            rows = _run(admin_conn,
                        "SELECT account_status, default_tablespace, common FROM dba_users WHERE username=:username",
                        {"username": str(owner_name).upper()})
            executor_status = None
            executor_catalog_access: dict[str, bool | None] = {}
            executor_catalog_errors: dict[str, str] = {}
            if side == "target" and config.get("oracle_permission_migration", {}).get("enabled") is True:
                executor_cfg = config.get("oracle_permission_migration", {}).get("executor", {})
                executor_name = executor_cfg.get("username") if isinstance(executor_cfg, dict) else None
                if executor_name and not str(executor_name).startswith("<"):
                    user_rows = _run(admin_conn, "SELECT username FROM dba_users WHERE username=:username",
                                     {"username": str(executor_name).upper()})
                    role_rows = _run(admin_conn, "SELECT role FROM dba_roles WHERE role=:role",
                                     {"role": str(executor_name).upper()})
                    executor_status = "PRESENT" if user_rows else "ROLE_CONFLICT" if role_rows else "MISSING"
                    if user_rows:
                        for view in _EXECUTOR_CATALOG:
                            try:
                                grant_rows = _run(admin_conn,
                                    "SELECT 1 FROM dba_tab_privs p WHERE p.owner='SYS' AND p.table_name=:catalog_name "
                                    "AND p.privilege='SELECT' AND (p.grantee=:username OR p.grantee IN "
                                    "(SELECT granted_role FROM dba_role_privs WHERE grantee=:username))",
                                    {"catalog_name": view, "username": str(executor_name).upper()})
                                executor_catalog_access[view] = bool(grant_rows)
                            except Exception as exc:  # noqa: BLE001 - unknown remains conditional
                                executor_catalog_access[view] = None
                                executor_catalog_errors[view] = _safe_error_details(exc, [])[1]
            report["secrets"][f"{side}_administrator"] = {"status": "PROVIDED_AT_RUNTIME", "reference": None}
            if not rows:
                admin_observations[side] = {"status": "MISSING", "reason": "DBA_USERS confirms owner account is absent",
                    "identity": admin_identity, "executor_status": executor_status, "storage": storage_info,
                    "admin_username": str(admin_name), "admin_privileges": sorted(admin_privileges),
                    "admin_roles": sorted(admin_roles), "admin_privilege_options": sorted(admin_privilege_options),
                    "admin_role_options": sorted(admin_role_options), "catalog_access": catalog_access,
                    "catalog_access_errors": catalog_access_errors,
                    "direct_object_grant_options": direct_object_grant_options,
                    "executor_catalog_access": executor_catalog_access,
                    "executor_catalog_errors": executor_catalog_errors}
            else:
                owner_tablespace = str(rows[0][1]).strip()
                owner_quota_rows = _run(admin_conn,
                    "SELECT max_bytes FROM dba_ts_quotas WHERE username=:username AND tablespace_name=:tablespace",
                    {"username": str(owner_name).upper(), "tablespace": owner_tablespace.upper()})
                owner_quota = int(owner_quota_rows[0][0]) if owner_quota_rows and owner_quota_rows[0][0] is not None else 0
                admin_observations[side] = {"status": "PRESENT", "reason": "DBA_USERS confirms owner account exists",
                    "account_status": str(rows[0][0]), "default_tablespace": owner_tablespace,
                    "owner_quota_bytes": owner_quota,
                    "common": str(rows[0][2]), "identity": admin_identity,
                    "executor_status": executor_status, "storage": storage_info,
                    "admin_username": str(admin_name), "admin_privileges": sorted(admin_privileges),
                    "admin_roles": sorted(admin_roles), "admin_privilege_options": sorted(admin_privilege_options),
                    "admin_role_options": sorted(admin_role_options), "catalog_access": catalog_access,
                    "catalog_access_errors": catalog_access_errors,
                    "direct_object_grant_options": direct_object_grant_options,
                    "executor_catalog_access": executor_catalog_access,
                    "executor_catalog_errors": executor_catalog_errors}
        except Exception as exc:  # noqa: BLE001 - Oracle driver errors are backend-specific
            error_code = _safe_error_code(exc)
            category = ("administrator authentication failed" if error_code in {"ORA-01017", "ORA-28000"}
                        else "administrator connection failed" if error_code.startswith(("ORA-12", "DPY-60"))
                        else "administrator metadata inspection failed")
            admin_observations[side] = {"status": "UNKNOWN", "reason": f"{category} ({error_code})"}
        finally:
            if admin_conn is not None:
                with suppress(Exception):
                    admin_conn.close()
    # A hidden SYSDBA credential may be requested by the interactive PLAN when
    # ordinary PDB metadata cannot establish a catalog grant. This stage is
    # strictly SELECT-only and is deliberately completed before approval.
    for side, credentials in privileged_credentials.items():
        if side not in {"source", "target"} or not isinstance(credentials, dict):
            continue
        # A normal PDB administrator already proved that a fresh owner is
        # absent. New-account catalog operations are planned from that fact;
        # inspecting grants for a nonexistent account adds no evidence.
        if admin_observations.get(side, {}).get("status") == "MISSING":
            continue
        owner_cfg = config.get(side, {}).get("connection", {})
        owner_name = str(owner_cfg.get("username", "")).strip().upper()
        service = str(owner_cfg.get("service_name", "")).strip()
        if not owner_name or not service or not credentials.get("username") or not credentials.get("password"):
            report["privileged_read_only_inspection"][side] = {
                "status": "BLOCKED", "reason": "configured service or privileged credentials are unavailable"}
            continue
        expected_identity = admin_observations.get(side, {}).get("identity")
        sys_conn = None
        try:
            if not expected_identity:
                raise RuntimeError("PDB-local administrator identity must be verified before privileged catalog inspection")
            sys_conn = _connect_sysdba_runtime(owner_cfg, credentials["username"],
                                               credentials["password"], connect, provider)
            sys_identity = _identity(sys_conn)
            isdba_rows = _run(sys_conn, "SELECT SYS_CONTEXT('USERENV','ISDBA') FROM dual")
            if (len(isdba_rows) != 1 or str(isdba_rows[0][0]).upper() != "TRUE"
                    or sys_identity["service_name"].casefold() != service.casefold()
                    or any(
                        sys_identity.get(key, "").casefold() != str(expected_identity.get(key, "")).casefold()
                        for key in ("db_unique_name", "container_name", "service_name"))):
                raise RuntimeError("privileged read-only identity does not match the configured PDB service")
            views = set(_SOURCE_CATALOG if side == "source" and config.get(
                "oracle_permission_migration", {}).get("enabled") is True else ())
            views.update(_REPLACEMENT_CATALOG if side == "target" and config.get(
                "migration", {}).get("oracle_schema_replacement", {}).get("enabled") is True else ())
            accounts = {owner_name}
            executor_cfg = config.get("oracle_permission_migration", {}).get("executor", {})
            executor_name = (str(executor_cfg.get("username", "")).strip().upper()
                             if side == "target" and isinstance(executor_cfg, dict) else "")
            if side == "target" and executor_name:
                accounts.add(executor_name)
                views.update(_EXECUTOR_CATALOG)
            grants: dict[str, dict[str, bool]] = {}
            for account in sorted(accounts):
                grants[account] = {}
                for view in sorted(views):
                    rows = _run(sys_conn,
                        "SELECT 1 FROM SYS.DBA_TAB_PRIVS WHERE owner='SYS' AND table_name=:catalog_name "
                        "AND privilege='SELECT' AND grantee=:username",
                        {"catalog_name": view, "username": account})
                    grants[account][view] = bool(rows)
                    if account == owner_name:
                        admin_observations.setdefault(side, {}).setdefault("catalog_access", {})[view] = bool(rows)
                    if account == executor_name:
                        admin_observations.setdefault(side, {}).setdefault("executor_catalog_access", {})[view] = bool(rows)
            report["privileged_read_only_inspection"][side] = {
                "status": "READY", "identity": sys_identity, "direct_select_grants": grants}
        except Exception as exc:  # noqa: BLE001 - only sanitized diagnostics enter the plan
            code, reason = _safe_error_details(exc, [str(credentials.get("password", ""))])
            report["privileged_read_only_inspection"][side] = {
                "status": "BLOCKED", "reason": f"privileged read-only inspection failed ({code or type(exc).__name__}): {reason}"}
        finally:
            if sys_conn is not None:
                with suppress(Exception):
                    sys_conn.close()
    for side in ("source", "target"):
        side_cfg = config.get(side, {})
        conn_cfg = side_cfg.get("connection", {}) if isinstance(side_cfg, dict) else {}
        if not isinstance(conn_cfg, dict):
            conn_cfg = {}
        username = conn_cfg.get("username")
        service = conn_cfg.get("service_name")
        secret_ref = conn_cfg.get("password_secret")
        _, secret_status = _secret(provider, secret_ref, f"{side} owner password")
        report["secrets"][side] = {"reference": secret_ref or "UNKNOWN", "status": secret_status}
        if getattr(secret_status, "reason", None):
            report["secrets"][side]["reason"] = secret_status.reason
        side_result: dict[str, Any] = {"identity": _status("UNKNOWN", "Not checked", "Configured PDB service and owner"),
                                       "owner": _status("UNKNOWN", "Not checked", str(username or "Configured owner")),
                                        "account_discovery": _status("UNKNOWN", "Owner existence not inspected", "Read-only PDB account metadata"),
                                       "tablespace": _status("UNKNOWN", "Not checked", "Open, permanent, non-SYSTEM tablespace"),
                                       "quota": _status("UNKNOWN", "Not checked", "Positive finite quota or explicitly approved unlimited quota"),
                                       "object_privileges": {}, "observed_object_privileges": {},
                                       "approved_capability_privileges": dict(capability_requirements),
                                       "catalog_visibility": {}}
        report["sides"][side] = side_result
        if admin_observations.get(side, {}).get("storage"):
            side_result["storage_discovery"] = admin_observations[side]["storage"]
        if admin_observations.get(side, {}).get("identity"):
            side_result["administrator_inspection"] = _status(
                "READY", json.dumps(admin_observations[side]["identity"], sort_keys=True),
                "Runtime-authenticated administrator; this PLAN used SELECT statements only",
            )
        elif admin_observations.get(side, {}).get("status") == "UNKNOWN":
            side_result["administrator_inspection"] = _status(
                "UNKNOWN", admin_observations[side].get("reason", "administrator inspection unavailable"),
                "Verified runtime administrator identity and read-only metadata access")
        endpoints[side] = {"cfg": side_cfg, "connection": conn_cfg, "username": username,
                           "service": service, "secret_ref": secret_ref}
        if isinstance(username, str) and not username.startswith("<"):
            try:
                _ident(username, f"{side}.connection.username")
            except ValueError:
                side_result["identity"] = _status("BLOCKED", "Configured owner name is not a supported Oracle identifier", "Supported PDB-local schema owner")
                side_result["owner"] = _status("BLOCKED", "Unsafe owner name cannot be included in a review SQL plan", "Valid Oracle identifier")
                continue
        if isinstance(username, str) and username.strip().upper() in {"SYS", "SYSTEM"}:
            side_result["identity"] = _status("BLOCKED", "SYS/SYSTEM cannot be an application owner", "PDB-local schema owner identity")
            side_result["owner"] = _status("BLOCKED", "SYS/SYSTEM is prohibited as migration owner", "Separate SOURCE/TARGET schema owner")
            continue
        if side in admin_observations:
            observation = admin_observations[side]
            if observation["status"] == "MISSING":
                side_result["account_discovery"] = _status("MISSING", observation["reason"], "PDB-local user account")
                owner_name = str(username).upper() if username else "<OWNER>"
                if side == "source":
                    # DBA_USERS proves there is no schema whose object or
                    # permission inventory can be inspected yet. Treat this as
                    # an empty, not-applicable inventory; connector capabilities
                    # below provide the planned future owner privileges.
                    source_object_inventory_known = True
                    source_requirements = {}
                    source_permission_inventory_state = (
                        "NOT YET APPLICABLE — SOURCE OWNER WILL BE CREATED; "
                        "RERUN AFTER SOURCE OBJECTS AND GRANTS ARE PREPARED"
                    )
                if observation.get("identity"):
                    set_detected_identity(side, observation["identity"])
                side_result["owner"] = _status(
                    "MISSING", observation["reason"], "PDB-local schema owner",
                    authority="PDB-local DBA", approval=True,
                    security="Creates a new schema owner; structured operations require complete setup values",
                    verify=f"SELECT username, account_status, default_tablespace FROM dba_users WHERE username='{owner_name}';",
                )
                storage = observation.get("storage", {})
                side_result["storage_discovery"] = storage
                chosen_tablespace = decisions.get("tablespaces", {}).get(side)
                candidates = storage.get("suitable_tablespaces", [])
                default_ts = storage.get("default_permanent_tablespace")
                tablespace = chosen_tablespace
                if tablespace is None and len(candidates) == 1 and candidates[0] == default_ts:
                    tablespace = default_ts
                if tablespace not in candidates:
                    tablespace = None
                free_bytes = storage.get("free_bytes", {}).get(str(tablespace or "").upper())
                recommended_quota_bytes = _recommend_finite_quota(free_bytes)
                quota_override = decisions.get("quota_bytes", {}).get(side)
                quota_bytes = quota_override if quota_override is not None else recommended_quota_bytes
                tablespace_ready = bool(tablespace and tablespace in candidates)
                side_result["tablespace"] = _status(
                    "READY" if tablespace_ready else "UNKNOWN",
                    f"Selected {tablespace}" if tablespace_ready else "No safe tablespace choice confirmed",
                    "Discovered online permanent non-SYSTEM tablespace",
                )
                quota_ready = (isinstance(quota_bytes, int) and not isinstance(quota_bytes, bool)
                               and quota_bytes > 0 and isinstance(free_bytes, int)
                               and quota_bytes <= free_bytes)
                side_result["quota"] = _status(
                    "MISSING" if quota_ready else "UNKNOWN",
                    "Owner not yet created" if quota_ready else
                    "Insufficient discovered free space for a safe 1 MiB finite quota recommendation",
                    f"Recommended { _format_size_mib(quota_bytes if quota_ready else recommended_quota_bytes) } "
                    f"within {_format_size_mib(free_bytes)} discovered free space",
                )
                side_result["quota"]["recommendation_bytes"] = recommended_quota_bytes
                if quota_ready:
                    side_result["quota"]["approved_quota_bytes"] = quota_bytes
                    side_result["quota"]["quota_source"] = "user_override" if quota_override is not None else "automatic_recommendation"
                create_op = None
                if (tablespace and isinstance(quota_bytes, int) and quota_bytes > 0
                        and isinstance(free_bytes, int) and quota_bytes <= free_bytes
                        and secret_ref and not str(secret_ref).startswith("<")
                        and secret_status == "AVAILABLE"):
                    _ident(tablespace, f"{side} discovered/confirmed tablespace")
                    create_op = add_operation(
                        operation_type="CREATE_OWNER", side=side, account=owner_name,
                        current={"account": "ABSENT"}, desired={"account_status": "OPEN",
                            "default_tablespace": str(tablespace).upper(), "finite_quota_bytes": quota_bytes},
                        parameters={"password_secret_ref": str(secret_ref),
                                    "permanent_tablespace": _ident(tablespace, "permanent_tablespace")},
                        authority_name=admin_credentials.get(side, {}).get("username"),
                        authority_secret_ref=None,
                        required_authority=["CREATE USER"], verification={"query": "DBA_USERS", "expected": "OPEN"})
                    add_operation(operation_type="ASSIGN_FINITE_QUOTA", side=side, account=owner_name,
                        current={"quota": "NOT CREATED"}, desired={"tablespace": _ident(tablespace, "tablespace"), "quota_bytes": quota_bytes},
                        parameters={"tablespace": _ident(tablespace, "tablespace"), "quota_bytes": quota_bytes},
                        authority_name=admin_credentials.get(side, {}).get("username"),
                        authority_secret_ref=None,
                        required_authority=["ALTER USER"], dependencies=[create_op["operation_id"]],
                        verification={"query": "DBA_TS_QUOTAS", "expected_bytes": quota_bytes})
                    add_operation(operation_type="GRANT_SYSTEM_PRIVILEGE", side=side, account=owner_name,
                        current={"privilege": "MISSING"}, desired={"privilege": "CREATE SESSION"},
                        parameters={"privilege": "CREATE SESSION"},
                        authority_name=admin_credentials.get(side, {}).get("username"),
                        authority_secret_ref=None,
                        required_authority=["GRANT CREATE SESSION"], dependencies=[create_op["operation_id"]],
                        verification={"query": "DBA_SYS_PRIVS", "expected": "CREATE SESSION"})
                    side_result["object_privileges"]["CREATE SESSION"] = _status(
                        "MISSING", "Owner account absent", "Direct CREATE SESSION",
                        authority="PDB-local DBA", approval=True,
                        security="Allows the owner to authenticate",
                        verify="SELECT privilege FROM DBA_SYS_PRIVS WHERE grantee=:owner AND privilege='CREATE SESSION';")
                    setup_requirements = dict(capability_requirements)
                    if side == "target" and source_object_inventory_known:
                        for privilege, source_direct_only in (source_requirements or {}).items():
                            setup_requirements[privilege] = setup_requirements.get(privilege, False) or source_direct_only
                    for privilege, direct_only in sorted(setup_requirements.items()):
                        side_result["object_privileges"][privilege] = _status(
                            "MISSING", "Owner account absent", "Direct grant" if direct_only else "Effective privilege",
                            approval=True, authority="PDB-local DBA",
                            security="Enables only an explicitly approved future object capability")
                        add_operation(operation_type="GRANT_SYSTEM_PRIVILEGE", side=side, account=owner_name,
                            current={"privilege": "MISSING"}, desired={"privilege": privilege, "direct_required": direct_only},
                            parameters={"privilege": privilege}, authority_name=None, authority_secret_ref=None,
                        required_authority=[f"GRANT {privilege}"], dependencies=[create_op["operation_id"]],
                            verification={"query": "DBA_SYS_PRIVS", "expected": privilege,
                                          "direct_required": direct_only})
                    if side == "source" and config.get("oracle_permission_migration", {}).get("enabled") is True:
                        add_catalog_operations_for_new_account(
                            side, owner_name, _SOURCE_CATALOG, create_op["operation_id"])
                    if (side == "target"
                            and config.get("migration", {}).get("oracle_schema_replacement", {}).get("enabled") is True):
                        add_catalog_operations_for_new_account(
                            side, owner_name, _REPLACEMENT_CATALOG, create_op["operation_id"])
                else:
                    unresolved = []
                    actions = []
                    if not admin_observations.get(side, {}).get("identity"):
                        unresolved.append("authorized PDB administrator identity or metadata inspection is unavailable")
                        actions.append("provide valid PDB-local administrator credentials with required metadata visibility")
                    if not tablespace_ready:
                        unresolved.append("no discovered suitable permanent tablespace is selected")
                        actions.append("select an online permanent non-SYSTEM tablespace with reported free space")
                    if not quota_ready:
                        unresolved.append("no safe positive finite quota fits discovered free space")
                        actions.append("free tablespace capacity or approve a finite quota within reported free space")
                    if not secret_ref or str(secret_ref).startswith("<"):
                        unresolved.append("owner password secret reference is missing or invalid")
                        actions.append("configure a valid password_secret reference in the normal migration YAML")
                    elif secret_status == "MISSING":
                        unresolved.append("owner password secret is missing from the configured provider")
                        actions.append("supply the new owner password at the hidden APPLY prompt or provision the referenced secret")
                    elif secret_status == "UNKNOWN":
                        cause = getattr(secret_status, "reason", "secret provider could not resolve the reference")
                        unresolved.append(f"owner password secret resolution failed ({cause})")
                        actions.append("repair secret-provider access or the configured password_secret reference")
                    report["blockers"].append({
                        "code": f"{side.upper()}_OWNER_DECISIONS",
                        "message": "Owner absence is confirmed, but " + "; ".join(unresolved) + ".",
                        "remediation": "; ".join(actions),
                    })
                if side == "target" and not source_object_inventory_known:
                    side_result["object_privileges"]["scope"] = _status(
                        "UNKNOWN", "SOURCE object inventory unavailable",
                        "Target DDL privileges required by supported SOURCE objects",
                    )
                continue
            if observation["status"] == "PRESENT":
                side_result["account_discovery"] = _status("READY", observation["account_status"], "Existing PDB-local user account")
                if observation.get("identity"):
                    set_detected_identity(side, observation["identity"])
                owner_state = str(observation.get("account_status", "UNKNOWN"))
                side_result["owner"] = _status(
                    "READY" if owner_state.upper() == "OPEN" else "BLOCKED",
                    f"{username} / {owner_state} / common={observation.get('common')}",
                    "OPEN PDB-local schema owner",
                )
                default_ts = str(observation.get("default_tablespace", "")).upper()
                storage_rows = observation.get("storage", {}).get("available_tablespaces", [])
                tablespace_row = next((row for row in storage_rows if str(row[0]).upper() == default_ts), None)
                if tablespace_row:
                    ts_ready = str(tablespace_row[1]).upper() == "ONLINE" and str(tablespace_row[2]).upper() == "PERMANENT" and default_ts not in {"SYSTEM", "SYSAUX"}
                    side_result["tablespace"] = _status("READY" if ts_ready else "BLOCKED",
                        f"{tablespace_row[0]} / {tablespace_row[1]} / {tablespace_row[2]}",
                        "Existing online permanent non-SYSTEM tablespace")
                owner_quota = int(observation.get("owner_quota_bytes", 0))
                side_result["quota"] = _status("READY" if owner_quota > 0 else "BLOCKED",
                    "UNLIMITED" if owner_quota == -1 else f"{owner_quota} bytes" if owner_quota else "No quota row",
                    "Positive finite owner quota")
        if secret_status == "UNKNOWN":
            cause = getattr(secret_status, "reason", "secret provider could not resolve the reference")
            side_result["secret"] = _status(
                "BLOCKED", f"Owner password secret resolution failed ({cause})",
                "Available configured owner password",
                security="The existing owner password will not be changed",
                verify="Resolve the configured password_secret through the selected provider")
            side_result["owner"] = _status(
                "UNKNOWN", f"Owner authentication was not attempted because {cause}",
                "Authenticated configured owner session")
            side_result["object_privileges"]["inventory"] = _status(
                "UNKNOWN", "Owner connection unavailable", "Observed schema inventory and direct/effective privileges")
            continue
        if secret_status == "MISSING":
            side_result["owner"] = _status(
                "UNKNOWN", "Runtime owner secret unavailable; owner session and schema inventory cannot be verified",
                "Available runtime owner secret and authenticated schema inspection",
            )
            side_result["object_privileges"]["inventory"] = _status(
                "UNKNOWN", "Owner connection unavailable", "Observed schema inventory and direct/effective privileges")
            continue
        if not username or not service or str(username).startswith("<") or str(service).startswith("<"):
            side_result["identity"] = _status("BLOCKED", "Profile contains unresolved placeholders", "Configured service and PDB identity")
            side_result["owner"] = _status("UNKNOWN", "Not checked", str(username or "Configured owner"))
            continue
        password, _ = _secret(provider, secret_ref, f"{side} owner password")
        if password is None:
            continue
        connection = None
        try:
            connection = _connect(_connection_config(side_cfg, str(username), password), provider, connect)
            identity = _identity(connection)
            expected_user = str(username).strip().upper()
            expected_service = str(service).strip().upper()
            if (identity["session_user"].upper() != expected_user
                    or identity["current_schema"].upper() != expected_user
                    or identity["service_name"].upper() != expected_service):
                side_result["identity"] = _status("BLOCKED", json.dumps(identity, sort_keys=True),
                                                   f"SESSION_USER=CURRENT_SCHEMA={expected_user}; SERVICE={expected_service}")
                side_result["owner"] = _status("BLOCKED", "Connected identity does not match configured owner", "Exact configured owner")
                continue
            admin_identity = admin_observations.get(side, {}).get("identity")
            if admin_identity and any(identity[key].casefold() != admin_identity[key].casefold()
                    for key in ("db_unique_name", "container_name", "service_name")):
                side_result["identity"] = _status("BLOCKED", json.dumps(identity, sort_keys=True),
                    "Owner and runtime administrator must resolve to the same database, PDB, and service")
                side_result["owner"] = _status("BLOCKED", "Owner and administrator endpoints disagree", "One confirmed Oracle endpoint")
                continue
            side_result["account_discovery"] = _status("READY", "Configured owner authentication succeeded", "Existing PDB-local user account")
            side_result["identity"] = _status("READY", json.dumps(identity, sort_keys=True),
                                               f"Discovered configured service {expected_service}")
            set_detected_identity(side, identity)
            side_result["object_privileges"]["CREATE SESSION"] = _status(
                "READY", "Authenticated owner session", "Owner must be able to log in",
            )
            owner_rows = _run(connection, "SELECT username, account_status, default_tablespace FROM user_users")
            if len(owner_rows) != 1:
                side_result["owner"] = _status("UNKNOWN", "USER_USERS did not return exactly one account", "One open local schema owner")
                continue
            owner_name, account_status, default_ts = (str(x).strip() if x is not None else "" for x in owner_rows[0])
            owner_ready = account_status.upper() == "OPEN"
            side_result["owner"] = _status("READY" if owner_ready else "BLOCKED",
                                           f"{owner_name} / {account_status}", "OPEN owner account")
            if not owner_ready:
                continue
            ts_rows = _run(connection, "SELECT tablespace_name, status, contents FROM user_tablespaces WHERE tablespace_name=:ts",
                           {"ts": default_ts})
            if len(ts_rows) == 1:
                ts_name, ts_status, ts_contents = (str(x).strip() if x is not None else "" for x in ts_rows[0])
                ts_ready = (ts_status.upper() == "ONLINE" and ts_contents.upper() == "PERMANENT"
                            and ts_name.upper() not in {"SYSTEM", "SYSAUX"})
                side_result["tablespace"] = _status("READY" if ts_ready else "BLOCKED",
                                                     f"{ts_name} / {ts_status} / {ts_contents}",
                                                     "Existing online permanent non-SYSTEM tablespace")
                if not ts_ready:
                    report["blockers"].append({"code": f"{side.upper()}_TABLESPACE_MISMATCH",
                        "message": "Existing owner default tablespace is not online, permanent, or is SYSTEM/SYSAUX.",
                        "remediation": "Review the account and storage manually; PLAN will not alter an existing owner's default tablespace."})
            else:
                side_result["tablespace"] = _status("UNKNOWN", "Default tablespace not visible in USER_TABLESPACES", "Online permanent non-SYSTEM tablespace")
            quota_rows = _run(connection, "SELECT max_bytes FROM user_ts_quotas WHERE tablespace_name=:ts", {"ts": default_ts})
            max_bytes = int(quota_rows[0][0]) if quota_rows and quota_rows[0][0] is not None else 0
            current = "UNLIMITED" if max_bytes == -1 else f"{max_bytes} bytes" if max_bytes else "No quota row"
            ts_literal = str(default_ts).replace("'", "''")
            quota_ready = max_bytes > 0
            quota_decision = decisions.get("quota_bytes", {}).get(side)
            storage = admin_observations.get(side, {}).get("storage", {})
            free_limit = storage.get("free_bytes", {}).get(str(default_ts).upper())
            if quota_ready:
                side_result["quota"] = _status("READY", current,
                    "Preserve existing quota; no automatic quota change",
                    verify=f"SELECT max_bytes FROM user_ts_quotas WHERE tablespace_name='{ts_literal}';")
            else:
                recommended_quota_bytes = _recommend_finite_quota(free_limit)
                requested_quota_bytes = quota_decision if quota_decision is not None else recommended_quota_bytes
                quota_safe = (isinstance(requested_quota_bytes, int)
                              and not isinstance(requested_quota_bytes, bool)
                              and requested_quota_bytes > max(0, max_bytes)
                              and isinstance(free_limit, int)
                              and requested_quota_bytes <= free_limit)
                if quota_safe and side in admin_credentials:
                    quota_op = add_operation(operation_type="ASSIGN_FINITE_QUOTA", side=side,
                        account=expected_user, current={"quota_bytes": max_bytes},
                        desired={"tablespace": str(default_ts).upper(), "quota_bytes": requested_quota_bytes},
                        parameters={"tablespace": str(default_ts).upper(), "quota_bytes": requested_quota_bytes},
                        authority_name=admin_credentials[side].get("username"), authority_secret_ref=None,
                        required_authority=["ALTER USER"],
                        verification={"query": "DBA_TS_QUOTAS", "expected_bytes": requested_quota_bytes})
                    side_result["quota"] = _status("MISSING", current,
                        f"Recommended {_format_size_mib(requested_quota_bytes)} finite quota",
                        action=quota_op["preview_sql"], authority="Runtime-authenticated PDB administrator", approval=True)
                    side_result["quota"]["recommendation_bytes"] = recommended_quota_bytes
                    side_result["quota"]["approved_quota_bytes"] = requested_quota_bytes
                    side_result["quota"]["quota_source"] = "user_override" if quota_decision is not None else "automatic_recommendation"
                else:
                    side_result["quota"] = _status("BLOCKED", current,
                        "Finite quota recommendation unavailable or override exceeds discovered free space",
                        verify=f"SELECT max_bytes FROM user_ts_quotas WHERE tablespace_name='{ts_literal}';")
                    side_result["quota"]["recommendation_bytes"] = recommended_quota_bytes
                    report["blockers"].append({"code": f"{side.upper()}_QUOTA_DECISION",
                        "message": "A finite quota is missing and the discovered free space cannot support a safe recommendation, or the override exceeds free space.",
                        "remediation": "Review the selected tablespace/storage, or enter a finite quota override in MiB that is greater than the current quota and no greater than the discovered free extents."})
            source_connection_cfg = endpoints["source"]["connection"]
            include_tables = source_connection_cfg.get("include_tables") if side == "source" else None
            own_requirements = _owner_requirements(connection, include_tables)
            side_result["observed_object_privileges"] = dict(own_requirements)
            if side == "source":
                source_requirements = own_requirements
                source_object_inventory_known = True
            required = {**own_requirements}
            for privilege, required_by_capability in capability_requirements.items():
                required[privilege] = required.get(privilege, False) or required_by_capability
            if side == "target":
                if source_object_inventory_known:
                    required = {**(source_requirements or {})}
                    for privilege, required_by_capability in capability_requirements.items():
                        required[privilege] = required.get(privilege, False) or required_by_capability
                else:
                    side_result["object_privileges"]["scope"] = _status(
                        "UNKNOWN", "SOURCE object inventory unavailable",
                        "Privileges required by discovered SOURCE objects",
                    )
                    required = {}
            direct, missing = _privileges(connection, required)
            for privilege, direct_only in sorted(required.items()):
                absent = privilege in missing
                item = _status("MISSING" if absent else "READY",
                               "Missing" if absent else ("Direct" if privilege in direct else "Effective"),
                               "Direct grant" if direct_only else "Effective privilege",
                               action=(f"GRANT {privilege} TO \"{expected_user}\";" if absent else None),
                               authority="PDB-local DBA", approval=absent,
                               security="Adds schema object-creation authority" if absent else None,
                               verify=f"SELECT privilege FROM user_sys_privs WHERE privilege='{privilege}';")
                side_result["object_privileges"][privilege] = item
                if absent:
                    add_operation(operation_type="GRANT_SYSTEM_PRIVILEGE", side=side, account=expected_user,
                        current={"privilege": "MISSING"}, desired={"privilege": privilege},
                        parameters={"privilege": privilege}, authority_name=None, authority_secret_ref=None,
                        required_authority=[f"GRANT {privilege}"], verification={"query": "USER_SYS_PRIVS", "expected": privilege})
            permission_enabled = config.get("oracle_permission_migration", {}).get("enabled") is True
            replacement_enabled = config.get("migration", {}).get("oracle_schema_replacement", {}).get("enabled") is True
            catalog_views = (
                _SOURCE_CATALOG if side == "source" and permission_enabled else
                _REPLACEMENT_CATALOG if side == "target" and replacement_enabled else ()
            )
            for view in catalog_views:
                probe = _catalog_probe(connection, view)
                readable = probe["readable"]
                admin_grant_state = admin_observations.get(side, {}).get("catalog_access", {}).get(view) if view.startswith("DBA_") else None
                status = "READY" if readable else "MISSING" if admin_grant_state is False else "UNKNOWN"
                probe_reason = ("Readable" if readable else
                    "Administrator confirmed SELECT grant absent" if admin_grant_state is False else probe["reason"])
                metadata_error = admin_observations.get(side, {}).get("catalog_access_errors", {}).get(view)
                if status == "UNKNOWN" and metadata_error:
                    probe_reason += f"; PDB administrator grant metadata query also failed ({metadata_error})"
                side_result["catalog_visibility"][view] = _status(status,
                    probe_reason,
                    "Direct SELECT visibility when feature requires it",
                    action=_catalog_sql(view, expected_user) if status == "MISSING" else None,
                    authority="PDB-local DBA" if view.startswith("DBA_") else None,
                    approval=status == "MISSING",
                    verify=f"SELECT 1 FROM {'SYS.' if view.startswith('DBA_') else ''}{view} WHERE 1=0;")
                # Failed catalog probes are UNKNOWN, never proof that a GRANT is absent.
                if status == "MISSING":
                    add_operation(operation_type="GRANT_CATALOG_ACCESS", side=side, account=expected_user,
                        current={"access": "MISSING"}, desired={"view": view, "privilege": "SELECT"},
                        parameters={"view": view}, authority_name=None, authority_secret_ref=None,
                        required_authority=["SYSDBA session on the configured PDB service"],
                        verification={"query": view, "expected": "SELECT readable"})
            if side == "source" and permission_enabled:
                if all(item["status"] == "READY" for item in side_result["catalog_visibility"].values()):
                    try:
                        from core.connectors.oracle.permissions import discover
                        source_permission_inventory = discover(
                            connection, expected_user, config["oracle_permission_migration"]
                        )
                        source_permission_inventory_state = "READY"
                    except Exception as exc:  # noqa: BLE001 - security inventory failure is reported as UNKNOWN
                        source_permission_inventory_state = f"UNKNOWN ({type(exc).__name__})"
                else:
                    source_permission_inventory_state = "UNKNOWN — required source catalogs are not fully readable"
        except Exception as exc:  # noqa: BLE001 - never echo raw driver details
            # Driver errors can include connection details. Never echo their message.
            side_result["identity"] = _status("UNKNOWN", f"Owner connection/read-only inspection failed ({_safe_error_code(exc)})",
                                               "Verified service, PDB, and owner")
            side_result["owner"] = _status("UNKNOWN", f"Owner authentication or metadata inspection failed ({_safe_error_code(exc)})",
                                            "Existing OPEN schema owner; login failure is not proof of absence")
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()

    source_identity = report["sides"].get("source", {}).get("identity", {}).get("current")
    target_identity = report["sides"].get("target", {}).get("identity", {}).get("current")
    if source_identity and target_identity:
        with suppress(TypeError, json.JSONDecodeError):
            source_endpoint = json.loads(source_identity)
            target_endpoint = json.loads(target_identity)
            overlap = all(source_endpoint.get(key, "").casefold() == target_endpoint.get(key, "").casefold()
                          for key in ("db_unique_name", "container_name"))
            if overlap:
                report["blockers"].append({"code": "SOURCE_TARGET_ENDPOINT_OVERLAP",
                    "message": "SOURCE and TARGET resolve to the same database and PDB.",
                    "remediation": "Correct one normal migration service endpoint so SOURCE and TARGET are isolated."})
                for side in ("source", "target"):
                    report["sides"][side]["identity"]["status"] = "BLOCKED"

    policy = config.get("oracle_permission_migration", {})
    enabled = isinstance(policy, dict) and policy.get("enabled") is True
    report["approval_policy"] = {
        "permission_migration_enabled": enabled,
        "approve_role_creation_admin_option": bool(policy.get("approve_role_creation_admin_option", False)) if enabled else False,
        "approve_create_session": bool(policy.get("approve_create_session", False)) if enabled else False,
        "warning": "Configuration flags describe migration policy; they do not authorize this read-only PLAN or a future APPLY.",
    }
    report["source_permission_inventory"] = {
        "status": source_permission_inventory_state,
        "users": sorted((source_permission_inventory or {}).get("user_mappings", {})),
        "roles": sorted((source_permission_inventory or {}).get("role_mappings", {})),
        "create_session_grants": [
            str(getattr(grant, "grantee", ""))
            for grant in (source_permission_inventory or {}).get("grants", [])
            if str(getattr(grant, "object_type", "")).upper() == "SYSTEM"
            and str(getattr(grant, "privileges", "")).upper() == "CREATE SESSION"
        ],
    }
    role_mapping = (source_permission_inventory or {}).get("role_mappings", {})
    role_admin_prerequisites = sorted({
        str(role_mapping.get(item.role_name, item.role_name)).upper()
        for item in (source_permission_inventory or {}).get("memberships", [])
        if getattr(item, "role_name", None)
    })
    report["conditional_security_prerequisites"] = {
        "role_admin_option": [{"target_role": role, "status": "CONDITIONAL_NOT_VERIFIED",
            "requires_policy_approval": True,
            "approved_by_configuration": report["approval_policy"]["approve_role_creation_admin_option"]}
            for role in role_admin_prerequisites],
        "source_create_session_grants": {
            "grantees": report["source_permission_inventory"]["create_session_grants"],
            "status": "CONDITIONAL_REQUIRES_REVIEW" if report["source_permission_inventory"]["create_session_grants"] else "NOT REQUIRED",
            "approved_by_configuration": report["approval_policy"]["approve_create_session"],
        },
    }
    # Role authority is target-state dependent. Roles absent from TARGET are
    # created by the permission executor during FULL; Oracle gives their
    # creator ADMIN OPTION. Existing roles need an exact executor grant option
    # and, when absent, a narrowly scoped approved setup operation below.
    if enabled:
        ex_cfg = policy.get("executor", {}) if isinstance(policy, dict) else {}
        ex_user = ex_cfg.get("username") if isinstance(ex_cfg, dict) else None
        ex_ref = ex_cfg.get("password_secret") if isinstance(ex_cfg, dict) else None
        _, ex_secret_state = _secret(provider, ex_ref, "executor password")
        ex_item: dict[str, Any] = {
            "account": _status("UNKNOWN", "Not inspected", str(ex_user or "Configured executor")),
            "account_discovery": _status("UNKNOWN", "Executor existence not inspected", "Read-only target PDB account metadata"),
            "catalog_visibility": {}, "authority": {},
            "secret": {"reference": ex_ref or "UNKNOWN", "status": ex_secret_state},
        }
        report["permission_executor"] = ex_item
        tgt = endpoints["target"]
        admin_executor_state = admin_observations.get("target", {}).get("executor_status")
        if admin_executor_state == "MISSING":
            ex_item["account_discovery"] = _status("MISSING", "DBA_USERS and DBA_ROLES confirm executor is absent", "Target-PDB user account")
        elif admin_executor_state == "PRESENT":
            ex_item["account_discovery"] = _status("READY", "DBA_USERS confirms executor exists", "Existing target-PDB user account")
        elif admin_executor_state == "ROLE_CONFLICT":
            ex_item["account_discovery"] = _status("BLOCKED", "Configured executor name is a role", "Distinct target-PDB user account")
        if isinstance(ex_user, str) and ex_user.strip().upper() in {"SYS", "SYSTEM"}:
            ex_item["account"] = _status("BLOCKED", "SYS/SYSTEM cannot be the permission executor", "Separate target-PDB executor")
        elif not ex_user or str(ex_user).startswith("<"):
            ex_item["account"] = _status("BLOCKED", "Executor username is unresolved", "Separate target-PDB executor")
        elif not _IDENTIFIER.fullmatch(str(ex_user).strip()):
            ex_item["account"] = _status("BLOCKED", "Executor username is not a supported Oracle identifier", "Separate target-PDB executor")
        elif admin_executor_state == "ROLE_CONFLICT":
            ex_item["account"] = _status("BLOCKED", "Configured executor name is already a role", "Distinct target-PDB user account")
        elif admin_executor_state == "MISSING":
            ex_name = str(ex_user).upper()
            ex_item["account"] = _status(
                "MISSING", "DBA_USERS and DBA_ROLES confirm executor is absent", "Separate target-PDB permission executor",
                authority="Target-PDB DBA", approval=True,
                security="Creates a separate account with login and read-only permission-catalog access",
                verify=f"SELECT username, account_status FROM dba_users WHERE username='{ex_name}';",
            )
            executor_secret_ref = ex_ref if isinstance(ex_ref, str) else None
            if executor_secret_ref and not executor_secret_ref.startswith("<") and ex_secret_state == "AVAILABLE":
                ex_create = add_operation(operation_type="CREATE_EXECUTOR", side="target", account=str(ex_user).upper(),
                    current={"account": "ABSENT"}, desired={"account_status": "OPEN", "catalog_access": list(_EXECUTOR_CATALOG)},
                    parameters={"password_secret_ref": executor_secret_ref},
                    authority_name=admin_credentials.get("target", {}).get("username"),
                    authority_secret_ref=None,
                    required_authority=["CREATE USER", "GRANT SELECT ON SYS catalog views"],
                    verification={"query": "DBA_USERS", "expected": "OPEN"})
                source_session_needed = bool(report.get("source_permission_inventory", {}).get("create_session_grants"))
                if source_session_needed and report["approval_policy"]["approve_create_session"]:
                    login_grant = add_operation(operation_type="GRANT_APPROVED_ADMIN_OPTION", side="target",
                        account=str(ex_user).upper(), current={"privilege": "MISSING ADMIN OPTION"},
                        desired={"privilege": "CREATE SESSION", "admin_option": True},
                        parameters={"privilege": "CREATE SESSION"}, authority_name=None,
                        authority_secret_ref=None,
                        required_authority=["GRANT CREATE SESSION WITH ADMIN OPTION"],
                        dependencies=[ex_create["operation_id"]], approval_required=True,
                        verification={"query": "USER_SYS_PRIVS", "expected_admin_option": "YES"})
                    report["conditional_security_prerequisites"]["source_create_session_grants"]["status"] = "MISSING BUT REMEDIABLE"
                    report["conditional_security_prerequisites"]["source_create_session_grants"]["proposed_operation_id"] = login_grant["operation_id"]
                else:
                    login_grant = add_operation(operation_type="GRANT_SYSTEM_PRIVILEGE", side="target", account=str(ex_user).upper(),
                        current={"privilege": "MISSING"}, desired={"privilege": "CREATE SESSION"},
                        parameters={"privilege": "CREATE SESSION"},
                        authority_name=None, authority_secret_ref=None,
                        required_authority=["GRANT CREATE SESSION"], dependencies=[ex_create["operation_id"]],
                        verification={"query": "DBA_SYS_PRIVS", "expected": "CREATE SESSION"})
                ex_item["authority"]["CREATE SESSION"] = _status(
                    "MISSING BUT REMEDIABLE", "Executor is new; approved CREATE SESSION grant is proposed",
                    "Direct CREATE SESSION to allow executor login", action=login_grant["preview_sql"],
                    authority="PDB-local DBA", approval=True,
                    verify="SELECT privilege FROM user_sys_privs WHERE privilege='CREATE SESSION';",
                )
                ex_item["authority"]["CREATE SESSION"]["proposed_operation_id"] = login_grant["operation_id"]
                source_users_needed = bool((source_permission_inventory or {}).get("user_mappings", {}))
                source_roles_needed = bool((source_permission_inventory or {}).get("role_mappings", {}))
                for privilege, needed in (("CREATE USER", source_users_needed), ("CREATE ROLE", source_roles_needed)):
                    if needed:
                        operation = add_operation(operation_type="GRANT_SYSTEM_PRIVILEGE", side="target",
                            account=str(ex_user).upper(), current={"privilege": "MISSING"},
                            desired={"privilege": privilege}, parameters={"privilege": privilege},
                            authority_name=None, authority_secret_ref=None,
                            required_authority=[f"GRANT {privilege}"], dependencies=[ex_create["operation_id"]],
                            verification={"query": "USER_SYS_PRIVS", "expected": privilege})
                        ex_item["authority"][privilege] = _status(
                            "MISSING BUT REMEDIABLE", "Source inventory requires this executor capability",
                            "Direct grant", action=operation["preview_sql"], authority="PDB-local DBA",
                            approval=True,
                            verify=f"SELECT privilege FROM user_sys_privs WHERE privilege='{privilege}';",
                        )
                        ex_item["authority"][privilege]["proposed_operation_id"] = operation["operation_id"]
                    elif source_object_inventory_known:
                        ex_item["authority"][privilege] = _status(
                            "NOT REQUIRED", "No source-mapped principals require this executor capability",
                            "Not required by discovered SOURCE permission inventory",
                        )
                if source_session_needed and not report["approval_policy"]["approve_create_session"]:
                    report["blockers"].append({"code": "CREATE_SESSION_ADMIN_OPTION_APPROVAL",
                        "message": "Source inventory contains CREATE SESSION grants, but the configured permission policy does not approve CREATE SESSION WITH ADMIN OPTION.",
                        "remediation": "Review the permission migration policy explicitly; no executor authority is proposed."})
                    report["conditional_security_prerequisites"]["source_create_session_grants"]["status"] = "BLOCKED: POLICY NOT APPROVED"
                else:
                    report["conditional_security_prerequisites"]["source_create_session_grants"]["status"] = "NOT REQUIRED"
                add_catalog_operations_for_new_account(
                    "target", str(ex_user).upper(), _EXECUTOR_CATALOG,
                    ex_create["operation_id"], catalog_report=ex_item["catalog_visibility"])
                if not source_object_inventory_known:
                    for privilege in ("CREATE USER", "CREATE ROLE"):
                        ex_item["authority"][privilege] = _status(
                            "CONDITIONAL_NOT_EVALUATED",
                            "SOURCE permission inventory is not yet available",
                            "Rerun PLAN after SOURCE objects and grants are prepared")
            else:
                # Keep the confirmed account state accurate so APPLY can
                # securely prompt for the missing creation secret. The
                # blocked secret status still prevents approval until supplied.
                ex_item["secret"] = _status(
                    "BLOCKED",
                    ("Executor account is absent; configured password secret is unavailable"
                     if ex_secret_state == "MISSING" else
                     "Executor account is absent; " + getattr(
                         ex_secret_state, "reason", "configured secret provider could not resolve its password reference")),
                    ("Available runtime executor password for approved account creation"
                     if ex_secret_state == "MISSING" else
                     "Repair secret-provider access for the configured executor reference"),
                    security="The password is used only by the approved CREATE USER operation")
                ex_item["account"] = _status(
                    "MISSING", "DBA_USERS and DBA_ROLES confirm executor is absent",
                    "Separate target-PDB permission executor")
        elif ex_secret_state == "UNKNOWN":
            secret_cause = getattr(ex_secret_state, "reason", "secret provider lookup failed")
            secret_reason = f"Configured executor secret resolution failed ({secret_cause})"
            ex_item["secret"] = _status(
                "BLOCKED", secret_reason,
                "Repair secret-provider access; do not replace an existing executor password")
            if admin_executor_state == "PRESENT":
                ex_item["account"] = _status(
                    "BLOCKED", secret_reason,
                    "Resolve the configured executor secret and authenticate as the existing account")
            else:
                admin_reason = admin_observations.get("target", {}).get("reason")
                ex_item["account"] = _status(
                    "UNKNOWN", f"Executor existence is not verified; {admin_reason or 'target-PDB administrator metadata was not inspected'}",
                    "Authorized target-PDB metadata inspection")
        elif ex_secret_state == "MISSING":
            admin_reason = admin_observations.get("target", {}).get("reason")
            if admin_executor_state is None:
                admin_reason = admin_reason or "target-PDB administrator metadata was not inspected"
                ex_item["account"] = _status(
                    "UNKNOWN", f"Executor existence was not verified: {admin_reason}",
                    "Authorized target-PDB metadata inspection and configured executor secret")
            else:
                ex_item["secret"] = _status(
                    "BLOCKED", "Configured executor password secret is missing",
                    "Set the referenced secret; an existing executor password is never reset automatically")
                ex_item["account"] = _status(
                    "BLOCKED", "Configured executor password secret is unavailable",
                    "Set the referenced secret; an existing executor password is never reset automatically")
        elif tgt["service"] and str(tgt["service"]).startswith("<"):
            ex_item["account"] = _status("UNKNOWN", "Target service unresolved", "Executor in target PDB")
        else:
            ex_pass, _ = _secret(provider, ex_ref, "executor password")
            if ex_pass:
                ex_conn = None
                try:
                    ex_conn = _connect(_connection_config(tgt["cfg"], str(ex_user), ex_pass), provider, connect)
                    ex_identity = _identity(ex_conn)
                    correct = (ex_identity["session_user"].casefold() == str(ex_user).casefold()
                               and ex_identity["current_schema"].casefold() == str(ex_user).casefold()
                               and ex_identity["service_name"].casefold() == str(tgt["service"]).casefold())
                    ex_item["account"] = _status("READY" if correct else "BLOCKED", json.dumps(ex_identity, sort_keys=True), "Exact executor user and target PDB service")
                    if correct:
                        ex_item["account_discovery"] = _status("READY", "Configured executor authentication succeeded", "Existing target-PDB permission executor")
                        for view in _EXECUTOR_CATALOG:
                            probe = _catalog_probe(ex_conn, view)
                            readable = probe["readable"]
                            admin_grant_state = admin_observations.get("target", {}).get("executor_catalog_access", {}).get(view)
                            catalog_status = "READY" if readable else "MISSING" if admin_grant_state is False else "UNKNOWN"
                            probe_reason = ("Readable" if readable else
                                "Administrator confirmed SELECT grant absent" if admin_grant_state is False else probe["reason"])
                            metadata_error = admin_observations.get("target", {}).get("executor_catalog_errors", {}).get(view)
                            if catalog_status == "UNKNOWN" and metadata_error:
                                probe_reason += f"; PDB administrator executor grant metadata query also failed ({metadata_error})"
                            ex_item["catalog_visibility"][view] = _status(catalog_status,
                                probe_reason,
                                "Read access for permission inventory",
                                action=_catalog_sql(view, str(ex_user).upper()) if catalog_status == "MISSING" else None,
                                authority="Runtime-authenticated PDB administrator", approval=catalog_status == "MISSING",
                                verify=f"SELECT 1 FROM SYS.{view} WHERE 1=0;")
                            if catalog_status == "MISSING":
                                add_operation(operation_type="GRANT_CATALOG_ACCESS", side="target",
                                    account=str(ex_user).upper(), current={"access": "MISSING"},
                                    desired={"view": view, "privilege": "SELECT"}, parameters={"view": view},
                                    authority_name=None, authority_secret_ref=None,
                                    required_authority=["SYSDBA session on the configured PDB service"],
                                    verification={"query": view, "expected": "SELECT readable"})
                        for privilege in ("CREATE USER", "CREATE ROLE", "CREATE SESSION"):
                            rows = _run(ex_conn, "SELECT admin_option FROM user_sys_privs WHERE privilege=:priv", {"priv": privilege})
                            direct = bool(rows)
                            admin_option = bool(rows and str(rows[0][0]).upper() == "YES")
                            has = direct and (privilege != "CREATE SESSION" or admin_option)
                            source_users = bool((source_permission_inventory or {}).get("user_mappings", {}))
                            source_roles = bool((source_permission_inventory or {}).get("role_mappings", {}))
                            source_session_grants = bool(report["source_permission_inventory"]["create_session_grants"])
                            conditional = (source_users if privilege == "CREATE USER" else
                                source_roles if privilege == "CREATE ROLE" else
                                source_session_grants and bool(report["approval_policy"]["approve_create_session"]))
                            authority_status = "READY" if has else "MISSING BUT REMEDIABLE" if conditional else "NOT REQUIRED"
                            ex_item["authority"][privilege] = _status(authority_status,
                                ("Direct grant with ADMIN OPTION" if privilege == "CREATE SESSION" and admin_option else "Direct grant") if direct else "Not visible",
                                ("Direct grant" + (" with ADMIN OPTION" if privilege == "CREATE SESSION" else "")
                                 if conditional else "Not needed for the discovered source permission inventory"),
                                action=None if has or not conditional else (
                                    f"GRANT {privilege} TO \"{str(ex_user).upper()}\""
                                    + (" WITH ADMIN OPTION" if privilege == "CREATE SESSION" else "") + ";"
                                ),
                                authority="PDB-local DBA" if conditional else None,
                                approval=not has and conditional,
                                security="Enables creation of users/roles or delegation of logins" if not has and conditional else None,
                                verify=f"SELECT privilege, admin_option FROM user_sys_privs WHERE privilege='{privilege}';")
                            if not has and conditional:
                                if privilege == "CREATE SESSION":
                                    operation = add_operation(operation_type="GRANT_APPROVED_ADMIN_OPTION", side="target",
                                        account=str(ex_user).upper(), current={"privilege": "MISSING ADMIN OPTION"},
                                        desired={"privilege": privilege, "admin_option": True},
                                        parameters={"privilege": privilege}, authority_name=None,
                                        authority_secret_ref=None, required_authority=["GRANT CREATE SESSION WITH ADMIN OPTION"],
                                        approval_required=True,
                                        verification={"query": "USER_SYS_PRIVS", "expected_admin_option": "YES"})
                                    report["conditional_security_prerequisites"]["source_create_session_grants"]["status"] = "MISSING BUT REMEDIABLE"
                                    report["conditional_security_prerequisites"]["source_create_session_grants"]["proposed_operation_id"] = operation["operation_id"]
                                else:
                                    operation = add_operation(operation_type="GRANT_SYSTEM_PRIVILEGE", side="target",
                                        account=str(ex_user).upper(), current={"privilege": "MISSING"},
                                        desired={"privilege": privilege}, parameters={"privilege": privilege},
                                        authority_name=None, authority_secret_ref=None,
                                        required_authority=[f"GRANT {privilege}"],
                                        verification={"query": "USER_SYS_PRIVS", "expected": privilege})
                                ex_item["authority"][privilege]["proposed_operation_id"] = operation["operation_id"]
                            elif not has and privilege == "CREATE SESSION" and source_session_grants:
                                report["blockers"].append({
                                    "code": "CREATE_SESSION_ADMIN_OPTION_APPROVAL",
                                    "message": "Source permission inventory contains CREATE SESSION, but approve_create_session is false.",
                                    "remediation": "Review the security policy explicitly; no executor ADMIN OPTION is proposed while approval is false.",
                                })
                                report["conditional_security_prerequisites"]["source_create_session_grants"]["status"] = "BLOCKED: POLICY NOT APPROVED"
                            elif not source_session_grants:
                                report["conditional_security_prerequisites"]["source_create_session_grants"]["status"] = "NOT REQUIRED"
                        role_authority_results = []
                        executor_role_options: set[str] = set()
                        try:
                            executor_role_options = {
                                str(row[0]).upper() for row in _run(
                                    ex_conn,
                                    "SELECT granted_role FROM user_role_privs WHERE admin_option='YES'",
                                )
                            }
                        except Exception as exc:  # noqa: BLE001 - failed authority inspection stays unknown
                            role_authority_error = _safe_error_details(exc, [ex_pass])[0]
                        else:
                            role_authority_error = None
                        memberships = (source_permission_inventory or {}).get("memberships", [])
                        required_roles = sorted({
                            str(role_mapping.get(item.role_name, item.role_name)).upper()
                            for item in memberships if getattr(item, "role_name", None)
                        })
                        for role in required_roles:
                            role_state: dict[str, Any] = {"target_role": role}
                            if role_authority_error:
                                role_state.update(status="UNKNOWN", reason=f"executor role authority inspection failed ({role_authority_error})")
                            else:
                                try:
                                    target_role_rows = _run(
                                        ex_conn,
                                        "SELECT role, oracle_maintained, common, authentication_type "
                                        "FROM dba_roles WHERE role=:role",
                                        {"role": role},
                                    )
                                    target_user_rows = _run(
                                        ex_conn, "SELECT username FROM dba_users WHERE username=:username",
                                        {"username": role},
                                    ) if not target_role_rows else []
                                except Exception as exc:  # noqa: BLE001 - catalog failures must stay unknown
                                    code = _safe_error_details(exc, [ex_pass])[0]
                                    role_state.update(status="UNKNOWN", reason=f"target role state inspection failed ({code})")
                                else:
                                    if target_user_rows:
                                        role_state.update(status="BLOCKED", reason="mapped TARGET role name conflicts with an existing user")
                                        report["blockers"].append({
                                            "code": "TARGET_ROLE_USER_NAME_CONFLICT",
                                            "message": f"Mapped TARGET role {role} conflicts with an existing user.",
                                            "remediation": "Review the mapping; the setup planner will not alter or replace an existing principal.",
                                        })
                                    elif target_role_rows:
                                        role_row = target_role_rows[0]
                                        if (str(role_row[1]).upper() != "N" or str(role_row[2]).upper() != "NO"
                                                or str(role_row[3]).upper() != "NONE"):
                                            role_state.update(status="BLOCKED", reason="existing target role is not a local NOT IDENTIFIED application role")
                                            report["blockers"].append({
                                                "code": "UNSUPPORTED_TARGET_ROLE",
                                                "message": f"Mapped TARGET role {role} is not a supported local NOT IDENTIFIED role.",
                                                "remediation": "Preserve the role and review its authentication/ownership outside this migration scope.",
                                            })
                                        elif role in executor_role_options:
                                            role_state.update(status="READY", reason="executor has direct ADMIN OPTION on existing TARGET role")
                                        elif report["approval_policy"]["approve_role_creation_admin_option"]:
                                            operation = add_operation(
                                                operation_type="GRANT_APPROVED_ADMIN_OPTION", side="target",
                                                account=str(ex_user).upper(),
                                                current={"role": role, "admin_option": False},
                                                desired={"role": role, "admin_option": True},
                                                parameters={"role": role}, authority_name=None,
                                                authority_secret_ref=None,
                                                required_authority=[f"GRANT {role} WITH ADMIN OPTION"],
                                                approval_required=True,
                                                verification={"query": "DBA_ROLE_PRIVS", "expected_admin_option": "YES", "role": role},
                                            )
                                            role_state.update(
                                                status="MISSING BUT REMEDIABLE",
                                                reason="existing TARGET role lacks executor ADMIN OPTION; exact grant is proposed",
                                                proposed_operation_id=operation["operation_id"],
                                            )
                                        else:
                                            role_state.update(status="BLOCKED", reason="existing TARGET role lacks executor ADMIN OPTION and role admin-option policy is not approved")
                                            report["blockers"].append({
                                                "code": "TARGET_ROLE_ADMIN_OPTION_NOT_APPROVED",
                                                "message": f"Existing TARGET role {role} lacks executor ADMIN OPTION and the configured policy does not approve it.",
                                                "remediation": "Review approve_role_creation_admin_option explicitly; no role grant is proposed while approval is false.",
                                            })
                                    else:
                                        if report["approval_policy"]["approve_role_creation_admin_option"]:
                                            role_state.update(
                                                status="MISSING BUT REMEDIABLE",
                                                reason="TARGET role is absent; the permission executor creates it during FULL and Oracle grants its creator ADMIN OPTION",
                                            )
                                        else:
                                            role_state.update(status="BLOCKED", reason="target role creation ADMIN OPTION is not approved by policy")
                                            report["blockers"].append({
                                                "code": "TARGET_ROLE_CREATION_NOT_APPROVED",
                                                "message": f"Source-mapped TARGET role {role} is absent, but role creation ADMIN OPTION is not approved.",
                                                "remediation": "Review approve_role_creation_admin_option explicitly before migrating this role.",
                                            })
                            role_authority_results.append(role_state)
                        report["conditional_security_prerequisites"]["role_admin_option"] = role_authority_results
                        for role_state in role_authority_results:
                            if role_state["status"] == "UNKNOWN":
                                report["blockers"].append({
                                    "code": "TARGET_ROLE_AUTHORITY_UNKNOWN",
                                    "message": f"Required role authority for {role_state['target_role']} could not be verified.",
                                    "remediation": "Resolve target role and executor authority metadata visibility, then rerun PLAN.",
                                })
                        report["approval_policy"]["role_admin_option_effect"] = (
                            "true: new target roles receive ADMIN OPTION if migration creates them"
                            if report["approval_policy"]["approve_role_creation_admin_option"] else
                            "false: role creation requiring ADMIN OPTION must stop"
                        )
                        if not report["approval_policy"]["approve_create_session"]:
                            report["approval_policy"]["create_session_effect"] = "false: source CREATE SESSION grants will not be approved for propagation"
                        else:
                            report["approval_policy"]["create_session_effect"] = "true: source CREATE SESSION grants are approved by configuration; review before any future APPLY"
                except Exception as exc:  # noqa: BLE001 - never echo raw driver details
                    ex_item["account"] = _status("UNKNOWN", f"Executor connection/read-only inspection failed ({_safe_error_code(exc)})",
                                                  "Existing OPEN target-PDB executor; failure does not prove absence")
                finally:
                    if ex_conn is not None:
                        with suppress(Exception):
                            ex_conn.close()
    else:
        report["permission_executor"] = {"status": "NOT APPLICABLE", "reason": "oracle_permission_migration.enabled is not true"}

    for operation in report["operations"]:
        authority = operation["authority"]
        side = operation["scope"].lower()
        admin = admin_credentials.get(side)
        observed_admin = admin_observations.get(side, {})
        if not isinstance(admin, dict) or not observed_admin.get("identity"):
            report["blockers"].append({
                "code": f"{operation['scope']}_ADMIN_REQUIRED",
                "message": f"Operation {operation['operation_id']} requires an administrator authenticated to the discovered PDB.",
                "remediation": "Rerun PLAN and provide an authorized PDB-local administrator at the secure prompt.",
            })
        else:
            if not authority.get("identity_verified"):
                report["blockers"].append({"code": f"{operation['scope']}_ADMIN_IDENTITY_UNVERIFIED",
                    "message": f"Administrator identity for {operation['operation_id']} was not verified in the expected service.",
                    "remediation": "Re-enter credentials for the matching PDB-local administrator."})
            elif (not authority.get("exact_execution_authority_verified")
                  and not authority.get("requires_sysdba")):
                report["blockers"].append({"code": f"{operation['scope']}_EXACT_AUTHORITY_MISSING",
                    "message": f"The authenticated administrator lacks verified authority for operation {operation['operation_id']}.",
                    "remediation": "Use an organization-authorized PDB administrator with the exact existing grant authority needed; the planner will not grant DBA or ANY privileges."})
    report["operations"] = _ordered_operations(report["operations"])
    statuses: list[str] = []
    def collect_statuses(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("status"), str):
                statuses.append(value["status"])
            for nested in value.values():
                collect_statuses(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_statuses(nested)
    collect_statuses(report)
    uncertain = any(status in {"BLOCKED", "UNKNOWN"} for status in statuses)
    approval_ready = not uncertain and not report["blockers"]
    if report["operations"]:
        report["summary"] = "READY FOR REVIEW" if approval_ready else "INCOMPLETE / BLOCKED PLAN"
    elif uncertain:
        report["summary"] = "BLOCKED / UNKNOWN REQUIREMENTS PRESENT"
    elif all(side["owner"]["status"] == "READY" and side["identity"]["status"] == "READY"
             for side in report["sides"].values()):
        report["summary"] = "NO CHANGES REQUIRED"
    else:
        report["summary"] = "BLOCKED / UNKNOWN REQUIREMENTS PRESENT"
    report["plan_status"] = report["summary"]
    report["approval_ready"] = bool(approval_ready and (report["operations"] or report["summary"] == "NO CHANGES REQUIRED"))
    # Include only non-secret references and reviewed setup inputs in the hash.
    report["fingerprint_payload"] = {
        "schema": report["schema"], "scope": {
            side: {"username": endpoints.get(side, {}).get("username"),
                   "service": endpoints.get(side, {}).get("service"),
                   "host": endpoints.get(side, {}).get("connection", {}).get("host"),
                   "port": endpoints.get(side, {}).get("connection", {}).get("port"),
                   "owner_password_secret_ref": endpoints.get(side, {}).get("secret_ref"),
                   "identity": report["sides"].get(side, {}).get("identity", {}).get("current"),
                   "identity_confirmation": report["sides"].get(side, {}).get("identity_confirmation", {}).get("status"),
                   "observed": report["sides"].get(side)}
            for side in ("source", "target")
        },
        "supported_object_scope": report["supported_object_scope"],
        "approval_policy": report["approval_policy"],
        "feature_scope": {
            "oracle_permission_migration_enabled": enabled,
            "permission_executor_username": (policy.get("executor", {}).get("username")
                if isinstance(policy, dict) and isinstance(policy.get("executor", {}), dict) else None),
            "permission_executor_secret_ref": (policy.get("executor", {}).get("password_secret")
                if isinstance(policy, dict) and isinstance(policy.get("executor", {}), dict) else None),
            "permission_new_user_policy": policy.get("new_user_policy") if isinstance(policy, dict) else None,
            "oracle_schema_replacement_enabled": config.get("migration", {}).get("oracle_schema_replacement", {}).get("enabled") is True,
        },
        "conditional_security_prerequisites": report["conditional_security_prerequisites"],
        "operations": report["operations"], "blockers": report["blockers"],
        "plan_status": report["plan_status"],
    }
    report["fingerprint"] = hashlib.sha256(_canonical(report["fingerprint_payload"])).hexdigest()
    validate_plan_document(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan Oracle setup, optionally applying explicitly approved operations")
    parser.add_argument("--config", required=True, help="Oracle local-to-local YAML config")
    parser.add_argument("--export-plan", help="Write the structured read-only plan as JSON")
    parser.add_argument("--apply", action="store_true", help="After review, apply operations in the current plan")
    args = parser.parse_args(argv)
    try:
        from getpass import getpass

        with open(args.config, encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        if not isinstance(config, dict):
            raise TypeError("configuration root must be a YAML mapping")
        if config.get("source", {}).get("engine") != "oracle" or config.get("target", {}).get("engine") != "oracle":
            raise ValueError("Oracle setup PLAN requires Oracle SOURCE and TARGET")
        admins: dict[str, dict[str, str]] = {}
        confirmations: dict[str, str] = {}
        decisions: dict[str, Any] = {"tablespaces": {}, "quota_bytes": {}}
        secret_provider = _RuntimeSecretOverlay(create_secret_provider(config))
        privileged_credentials: dict[str, dict[str, str]] = {}
        result = build_plan(config, provider=secret_provider)

        def needs_admin(side: str, plan: dict[str, Any]) -> bool:
            side_report = plan["sides"].get(side, {})
            if any(op.get("scope", "").casefold() == side for op in plan.get("operations", [])):
                return True
            if side_report.get("owner", {}).get("status") in {"UNKNOWN", "MISSING"}:
                return True
            if side_report.get("tablespace", {}).get("status") == "UNKNOWN" or side_report.get("quota", {}).get("status") == "BLOCKED":
                return True
            if any(item.get("status") in {"UNKNOWN", "MISSING"}
                   for item in side_report.get("catalog_visibility", {}).values()):
                return True
            return side == "target" and plan.get("permission_executor", {}).get("account", {}).get("status") in {"UNKNOWN", "MISSING", "BLOCKED"}

        for side in ("source", "target"):
            if needs_admin(side, result):
                admin_name = input(f"{side.upper()} PDB administrator username (blank to skip): ").strip()
                if admin_name:
                    admin_password = getpass(f"{side.upper()} PDB administrator password (hidden): ")
                    if admin_password:
                        admins[side] = {"username": admin_name, "password": admin_password}
        if admins:
            result = build_plan(config, provider=secret_provider, admin_credentials=admins)

        # Existing account logins are needed for actual owner/executor
        # inspection. Prompt only when discovery did not prove an account is
        # absent; a blank response leaves PLAN read-only and continues other
        # independent metadata discovery.
        runtime_accounts = [
            (side, config.get(side, {}).get("connection", {}).get("password_secret"),
             result.get("sides", {}).get(side, {}).get("account_discovery", {}).get("status"),
             f"Existing {side.upper()} owner")
            for side in ("source", "target")
        ]
        configured_executor = config.get("oracle_permission_migration", {}).get("executor", {})
        if isinstance(configured_executor, dict):
            runtime_accounts.append(("executor", configured_executor.get("password_secret"),
                result.get("permission_executor", {}).get("account_discovery", {}).get("status"),
                "Existing permission executor"))
        runtime_secret_added = False
        for _key, ref, discovery_status, label in runtime_accounts:
            if (discovery_status not in {"READY", "UNKNOWN"} or not isinstance(ref, str)
                    or not _SECRET_REFERENCE.fullmatch(ref.strip())):
                continue
            value, _state = _secret(secret_provider, ref, f"{label} password")
            if value is not None:
                continue
            value = getpass(f"{label} password for secret {ref} (hidden; Enter to skip): ")
            if value:
                secret_provider.values[ref] = value
                runtime_secret_added = True
        if runtime_secret_added:
            result = build_plan(config, provider=secret_provider, admin_credentials=admins)

        def unverified_catalog_sides(plan: dict[str, Any]) -> dict[str, bool]:
            needed = {"source": False, "target": False}
            for side in ("source", "target"):
                side_report = plan.get("sides", {}).get(side, {})
                if (side_report.get("account_discovery", {}).get("status") != "MISSING"
                        and any(item.get("status") == "UNKNOWN"
                                for item in side_report.get("catalog_visibility", {}).values())):
                    needed[side] = True
            executor = plan.get("permission_executor", {})
            if (executor.get("account_discovery", {}).get("status") != "MISSING"
                    and any(item.get("status") == "UNKNOWN"
                            for item in executor.get("catalog_visibility", {}).values())):
                needed["target"] = True
            return needed

        for side, needed in unverified_catalog_sides(result).items():
            if not needed:
                continue
            print(f"{side.upper()} catalog grant state remains unknown after normal read-only inspection.")
            name = input(f"{side.upper()} authorized SYSDBA username for read-only inspection (blank to skip): ").strip()
            if name:
                password = getpass(f"{side.upper()} SYSDBA password for read-only inspection (hidden): ")
                if password:
                    privileged_credentials[side] = {"username": name, "password": password}
        if privileged_credentials:
            result = build_plan(config, provider=secret_provider, admin_credentials=admins,
                                privileged_credentials=privileged_credentials)

        if args.apply:
            for side in ("source", "target"):
                side_report = result["sides"].get(side, {})
                owner = side_report.get("owner", {})
                missing_owner = owner.get("status") == "MISSING"
                existing_owner_needs_password = (
                    owner.get("status") == "UNKNOWN"
                    and str(owner.get("current", "")).startswith("Runtime owner secret unavailable")
                )
                if not (missing_owner or existing_owner_needs_password):
                    continue
                ref = config.get(side, {}).get("connection", {}).get("password_secret")
                if not ref:
                    continue
                if _secret(secret_provider, ref, "owner password")[1] == "MISSING":
                    account_label = "New" if missing_owner else "Existing"
                    value = getpass(f"{account_label} {side.upper()} owner password for secret {ref} (hidden): ")
                    if value:
                        secret_provider.values[ref] = value
            ex_cfg = config.get("oracle_permission_migration", {}).get("executor", {})
            executor_account = result.get("permission_executor", {}).get("account", {})
            executor_is_missing = executor_account.get("status") == "MISSING"
            existing_executor_needs_password = (
                executor_account.get("status") == "BLOCKED"
                and str(executor_account.get("current", "")).startswith(
                    "Configured executor password secret is unavailable")
            )
            if ((executor_is_missing or existing_executor_needs_password)
                    and isinstance(ex_cfg, dict) and ex_cfg.get("password_secret")):
                ref = ex_cfg["password_secret"]
                if _secret(secret_provider, ref, "executor password")[1] == "MISSING":
                    account_label = "New" if executor_is_missing else "Existing"
                    value = getpass(f"{account_label} permission executor password for secret {ref} (hidden): ")
                    if value:
                        secret_provider.values[ref] = value

        print("\nDiscovered Oracle endpoint identities:")
        for side in ("source", "target"):
            identity_text = result["sides"].get(side, {}).get("identity", {}).get("current", "Not available")
            print(f"{side.upper()}: {identity_text}")
        for side in ("source", "target"):
            identity_confirmation = result["sides"].get(side, {}).get("identity_confirmation", {})
            token = identity_confirmation.get("current")
            if token and identity_confirmation.get("status") != "READY":
                entered = input(f"Review {side.upper()} identity above; enter confirmation token {token}: ").strip()
                if entered == token:
                    confirmations[side] = token

        if confirmations:
            result = build_plan(config, provider=secret_provider, admin_credentials=admins,
                                privileged_credentials=privileged_credentials,
                                confirmed_identities=confirmations, decisions=decisions)

        for side in ("source", "target"):
            side_report = result["sides"].get(side, {})
            if side_report.get("owner", {}).get("status") == "MISSING":
                storage = side_report.get("storage_discovery", {})
                suitable = storage.get("suitable_tablespaces", [])
                default_ts = storage.get("default_permanent_tablespace")
                if default_ts in suitable:
                    decisions["tablespaces"][side] = default_ts
                else:
                    if suitable:
                        print(f"{side.upper()} suitable tablespaces: {', '.join(suitable)}")
                        choice = input(f"Choose {side.upper()} permanent tablespace: ").strip().upper()
                        if choice in suitable:
                            decisions["tablespaces"][side] = choice
        result = build_plan(config, provider=secret_provider, admin_credentials=admins,
                            privileged_credentials=privileged_credentials,
                            confirmed_identities=confirmations, decisions=decisions)
        if args.apply:
            for side in ("source", "target"):
                side_report = result["sides"].get(side, {})
                quota_report = side_report.get("quota", {})
                recommended = quota_report.get("recommendation_bytes")
                if (side not in admins or recommended is None
                        or quota_report.get("status") not in {"MISSING", "BLOCKED"}):
                    continue
                tablespace = decisions.get("tablespaces", {}).get(side)
                if not tablespace:
                    tablespace = side_report.get("tablespace", {}).get("current", "")
                free_bytes = side_report.get("storage_discovery", {}).get("free_bytes", {}).get(str(tablespace).upper())
                print(f"{side.upper()} finite quota recommendation: {_format_size_mib(recommended)} "
                      f"({_format_size_mib(free_bytes)} currently free on {tablespace}).")
                raw_override = input("Press Enter to accept, or enter an override in MiB: ").strip()
                if raw_override:
                    if not raw_override.isdigit() or int(raw_override) <= 0:
                        raise ValueError("quota override must be a positive whole number of MiB")
                    decisions["quota_bytes"][side] = int(raw_override) * _MIB
            result = build_plan(config, provider=secret_provider, admin_credentials=admins,
                                privileged_credentials=privileged_credentials,
                                confirmed_identities=confirmations, decisions=decisions)

        print(_format_plan_console(result))
        serialized = json.dumps(result, indent=2, sort_keys=True)
        if args.export_plan:
            from pathlib import Path
            export_path = Path(args.export_plan)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            export_path.write_text(serialized + "\n", encoding="utf-8")
        if not args.apply:
            return 0 if result["approval_ready"] else 2
        if not result["operations"]:
            if result["plan_status"] == "NO CHANGES REQUIRED":
                print("NO CHANGES REQUIRED")
                return 0
            print("SETUP INCOMPLETE: plan contains no executable operations.", file=sys.stderr)
            return 2
        if not result["approval_ready"]:
            print("SETUP INCOMPLETE: the plan is blocked; no changes were applied.", file=sys.stderr)
            return 2
        approval = input(f"To apply exactly this plan, type APPLY {result['fingerprint']}: ").strip()
        if approval != f"APPLY {result['fingerprint']}":
            print("SETUP FAILED: plan approval was not provided; no changes were applied.")
            return 2
        sysdba_sides = sorted({operation["scope"].lower() for operation in result["operations"]
                              if operation.get("authority", {}).get("requires_sysdba") is True})
        for side in sysdba_sides:
            print(f"{side.upper()} SYS-owned catalog grants require an authorized SYSDBA session "
                  "on that configured PDB service.")
            privileged_username = input(f"{side.upper()} SYSDBA username (blank to cancel APPLY): ").strip()
            if not privileged_username:
                print("SETUP FAILED: SYSDBA credentials were not supplied; no changes were applied.")
                return 2
            privileged_password = getpass(f"{side.upper()} SYSDBA password (hidden): ")
            if not privileged_password:
                print("SETUP FAILED: SYSDBA credentials were not supplied; no changes were applied.")
                return 2
            privileged_credentials[side] = {"username": privileged_username, "password": privileged_password}
        applied = apply_approved_plan(
            result, approved_fingerprint=result["fingerprint"], config=config,
            provider=secret_provider, admin_credentials=admins,
            privileged_credentials=privileged_credentials,
            connect=None, fresh_plan=lambda: build_plan(
                config, provider=secret_provider, admin_credentials=admins,
                privileged_credentials=privileged_credentials, confirmed_identities=confirmations,
                decisions=decisions),
        )
        print(applied["status"])
        if applied.get("completed") and applied.get("completed_count") is None:
            print(f"Completed operations: {len(applied['completed'])}")
        if applied.get("failed_operation"):
            print(f"Failed operation: {applied['failed_operation']}"
                  + (f" ({applied['failed_description']})" if applied.get("failed_description") else ""))
        if applied.get("failed_scope"):
            print(f"Endpoint: {applied['failed_scope']}")
        if applied.get("execution_stage"):
            print(f"Stage: {applied['execution_stage']}")
        if applied.get("remaining_count") is not None:
            print(f"Remaining operations: {applied['remaining_count']}")
        if applied.get("completed_count") is not None:
            print(f"Completed operations: {applied['completed_count']}")
        if applied.get("oracle_error_code"):
            print(f"Oracle error: {applied['oracle_error_code']}")
        if applied.get("reason"):
            print(f"Reason: {applied['reason']}")
        if applied.get("corrective_action"):
            print(f"Next step: {applied['corrective_action']}")
        if applied.get("remaining_requirements"):
            print(f"Unresolved requirements: {len(applied['remaining_requirements'])}")
        return 2 if applied["status"] in {"SETUP FAILED", "SETUP INCOMPLETE"} else 0
    except Exception as exc:  # noqa: BLE001 - CLI errors are redacted to type and safe message
        print(f"Oracle setup PLAN failed safely ({type(exc).__name__}); details withheld.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
