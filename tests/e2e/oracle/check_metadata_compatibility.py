"""Read-only Oracle compatibility audit of FULL migration catalog paths.

The checker drives the real Oracle source/target discovery and replacement
planner methods through a SELECT-only cursor proxy. Failed catalog statements
are recorded and the remaining independent metadata checks continue. It never
calls migration, fixture, bootstrap, DDL, or permission-application methods.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from types import TracebackType
from typing import Any, Self

from core.connectors.oracle import permissions as oracle_permissions
from core.connectors.oracle._models import make_connect_kwargs
from core.connectors.oracle.precision import oracle_full_unsupported_datatypes
from core.connectors.oracle.replacement import _target_inventory
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import OracleTargetConnector
from tests.e2e.acceptance import E2EConfig, load_e2e_config
from tests.e2e.oracle.config import (
    OracleE2EError,
    connect,
    connection_config,
    resolver_for_side,
    secret_provider_for_side,
    validated_oracle_context,
)
from tests.e2e.oracle.run_validation import _permission_executor_connection_config

_PROHIBITED_PDBS = {"freepdb1", "migration_db"}
_ERROR_CODE = re.compile(r"\b(?:ORA-\d{5}|DPY-\d{4})\b", re.IGNORECASE)
_CATALOG_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:SYS\.)?[A-Z_$#][A-Z0-9_$#]*)\b", re.IGNORECASE
)
_READ_ONLY_START = re.compile(r"\s*SELECT\b", re.IGNORECASE)
_SESSION_METADATA_TRANSFORM = re.compile(
    r"\s*BEGIN\s+DBMS_METADATA\.SET_TRANSFORM_PARAM\s*\(\s*"
    r"DBMS_METADATA\.SESSION_TRANSFORM\s*,\s*'EMIT_SCHEMA'\s*,\s*FALSE\s*\)\s*;\s*END\s*;\s*",
    re.IGNORECASE,
)


class OracleIdentityError(RuntimeError):
    """Connection or owner/PDB identity failed; metadata probing must stop."""


class QueryAudit:
    def __init__(self, side: str, secrets: tuple[str, ...] = ()) -> None:
        self.side = side
        self.stage = "initialization"
        self.secrets = tuple(item for item in secrets if item)
        self.queries: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def record(self, sql: str, error: Exception | None = None) -> int:
        views = sorted({name.upper() for name in _CATALOG_REF.findall(sql)})
        item = {"side": self.side, "stage": self.stage, "views": views, "statement": " ".join(sql.split())}
        if error is None:
            self.queries.append(item)
            return len(self.queries) - 1
        message = str(error)
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        code = _ERROR_CODE.search(message)
        item.update({
            "error_code": code.group(0).upper() if code else None,
            "error_type": type(error).__name__,
            "error": " ".join(message.split())[:1200],
        })
        self.failures.append(item)
        return len(self.failures) - 1

    def redact(self, value: Any) -> str:
        message = str(value)
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        return " ".join(message.split())[:1200]


class ReadOnlyConnection:
    """Proxy that allows production metadata SELECTs and collects query errors."""
    def __init__(self, connection: Any, audit: QueryAudit) -> None:
        self._connection = connection
        self._audit = audit

    def cursor(self, *args: Any, **kwargs: Any) -> ReadOnlyCursor:
        return ReadOnlyCursor(self._connection.cursor(*args, **kwargs), self._audit)

    def __getattr__(self, name: str) -> Any:
        if name.casefold() in {"commit", "rollback", "begin", "start_transaction"}:
            raise AttributeError(f"read-only compatibility checker blocks connection.{name}")
        return getattr(self._connection, name)


class ReadOnlyCursor:
    def __init__(self, cursor: Any, audit: QueryAudit) -> None:
        self._cursor = cursor
        self._audit = audit
        self._last_failed = False
        self._last_error: Exception | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        close = getattr(self._cursor, "close", None)
        if callable(close):
            close()
        return False

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        self._last_failed = False
        self._last_error = None
        # This exact DBMS_METADATA setting is session-local and is used by the
        # production DDL reader to return stable object definitions. No other
        # PL/SQL statement is allowed through the read-only wrapper.
        session_transform = bool(_SESSION_METADATA_TRANSFORM.fullmatch(sql))
        if not _READ_ONLY_START.match(sql) and not session_transform:
            error = RuntimeError("read-only compatibility checker blocked non-SELECT SQL")
            self._audit.record(sql, error)
            self._last_failed = True
            self._last_error = error
            raise error
        if re.search(r"\bFOR\s+UPDATE\b", sql, re.IGNORECASE):
            error = RuntimeError("read-only compatibility checker blocked SELECT FOR UPDATE")
            self._audit.record(sql, error)
            self._last_failed = True
            raise error
        try:
            result = self._cursor.execute(statement, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - record every failed catalog statement
            self._audit.record(sql, exc)
            self._last_failed = True
            self._last_error = exc
            return None
        self._audit.record(sql)
        return result

    def fetchall(self) -> list[Any]:
        self._raise_failed_query()
        return list(self._cursor.fetchall())

    def fetchone(self) -> Any:
        self._raise_failed_query()
        return self._cursor.fetchone()

    def fetchmany(self, *args: Any, **kwargs: Any) -> list[Any]:
        self._raise_failed_query()
        return list(self._cursor.fetchmany(*args, **kwargs))

    def _raise_failed_query(self) -> None:
        if self._last_failed:
            # Do not turn a failed metadata query into an empty result. The
            # enclosing checker catches this at the feature boundary, records
            # the stage, and continues with independent checks.
            if self._last_error is not None:
                raise self._last_error
            raise RuntimeError("Oracle read-only metadata query failed")

    def executemany(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("read-only compatibility checker blocked executemany")

    def callproc(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("read-only compatibility checker blocked stored procedure execution")

    def callfunc(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("read-only compatibility checker blocked stored function execution")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def _identity(connection: Any) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT USER, SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), "
            "SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
        )
        row = cursor.fetchone()
    if row is None or len(row) != 4 or any(value is None for value in row):
        raise OracleIdentityError("Oracle connection identity query returned incomplete data")
    return dict(zip(("user", "db_unique_name", "container_name", "service_name"), map(str, row)))


def _verify_identity(
    identity: dict[str, str], *, side: str, owner: str, service: str,
    expected_container: str, expected_cdb: str | None,
) -> None:
    actual_container = identity["container_name"].upper()
    actual_service = identity["service_name"].casefold()
    expected_service = service.casefold()
    if actual_container.casefold() in _PROHIBITED_PDBS:
        raise OracleIdentityError(f"{side.upper()} connected to a prohibited PDB; metadata checks stopped")
    if actual_container.casefold() == "cdb$root":
        raise OracleIdentityError(f"{side.upper()} connected to CDB$ROOT instead of a PDB; metadata checks stopped")
    if actual_container.casefold() != expected_container.casefold():
        raise OracleIdentityError(
            f"{side.upper()} connection PDB identity mismatch: expected {expected_container}, "
            f"received {identity['container_name']}"
        )
    if identity["user"].casefold() != owner.casefold():
        raise OracleIdentityError(
            f"{side.upper()} connection user mismatch: expected configured owner {owner}, "
            f"received {identity['user']}"
        )
    if not (actual_service == expected_service or actual_service.startswith(expected_service + ".")):
        raise OracleIdentityError(
            f"{side.upper()} service identity mismatch: expected {service}, "
            f"received {identity['service_name']}"
        )
    if expected_cdb and identity["db_unique_name"].casefold() != expected_cdb.casefold():
        raise OracleIdentityError(
            f"{side.upper()} CDB identity mismatch: expected {expected_cdb}, "
            f"received {identity['db_unique_name']}"
        )


def _safe_feature(
    report: dict[str, Any], audit: QueryAudit, stage: str,
    operation: Callable[[], Any], summarize: Callable[[Any], Any] | None = None,
) -> Any:
    audit.stage = stage
    try:
        result = operation()
        report[stage] = summarize(result) if summarize else {"status": "PASS"}
        return result
    except Exception as exc:  # noqa: BLE001 - continue independent compatibility probes
        report[stage] = {"status": "FAIL", "error_type": type(exc).__name__,
                         "error": audit.redact(exc)}
        return None


def _configure_expected_identity(cfg: E2EConfig, services: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    for side, service in services.items():
        if service.casefold() in _PROHIBITED_PDBS:
            raise OracleIdentityError(
                f"Configured {side.upper()} service is prohibited; no connection was attempted"
            )
    e2e = cfg.raw.get("e2e", {})
    profile = e2e.get("onboarding_profile", {}) if isinstance(e2e, dict) else {}
    dba = e2e.get("dba_bootstrap", {}) if isinstance(e2e, dict) else {}
    expected_cdb = (
        profile.get("expected_db_unique_name") if isinstance(profile, dict) else None
    ) or (dba.get("expected_db_unique_name") if isinstance(dba, dict) else None)
    owner_policy = e2e.get("owner_provisioning", {}) if isinstance(e2e, dict) else {}
    containers: dict[str, str] = {}
    for side in ("source", "target"):
        side_policy = owner_policy.get(side, {}) if isinstance(owner_policy, dict) else {}
        container = side_policy.get("expected_container_name") if isinstance(side_policy, dict) else None
        container = str(container or services[side]).strip()
        if (
            not container
            or container.casefold() in _PROHIBITED_PDBS
            or container.casefold() == "cdb$root"
        ):
            raise OracleIdentityError(f"Configured {side.upper()} container is missing or prohibited")
        containers[side] = container
    if containers["source"].casefold() == containers["target"].casefold():
        raise OracleIdentityError("Configured SOURCE and TARGET PDBs must be distinct")
    return (str(expected_cdb).strip() if expected_cdb else None), containers


def _connect_owner(cfg: E2EConfig, side: str, owner: str, driver: Any) -> tuple[Any, dict[str, Any]]:
    endpoint = connection_config(cfg, side, owner, resolver_for_side(cfg.raw, side))
    try:
        return connect(endpoint, driver), endpoint
    except Exception as exc:  # noqa: BLE001 - do not expose secret values
        code = _ERROR_CODE.search(str(exc))
        suffix = f" ({code.group(0).upper()})" if code else f" ({type(exc).__name__})"
        raise OracleIdentityError(f"{side.upper()} owner connection failed{suffix}") from None


def run_auth_diagnostics(cfg: E2EConfig, *, driver: Any) -> dict[str, Any]:
    """Resolve and authenticate both configured owners without catalog scans.

    The comparison against environment variables is performed only in memory
    and only when the effective provider is ``env``. Secret values are never
    included in the returned report or exception text.
    """
    owners, services = validated_oracle_context(cfg)
    expected_cdb, containers = _configure_expected_identity(cfg, services)
    sides: dict[str, Any] = {}
    for side in ("source", "target"):
        entry: dict[str, Any] = {
            "username": owners[side],
            "service_name": services[side],
            "expected_container": containers[side],
        }
        sides[side] = entry
        connection = None
        try:
            provider_name = secret_provider_for_side(cfg.raw, side)
            entry["provider"] = provider_name
            resolver = resolver_for_side(cfg.raw, side)
            endpoint = connection_config(cfg, side, owners[side], resolver)
            connect_kwargs = make_connect_kwargs(endpoint, driver)
            entry["connect_arguments"] = {
                "user": connect_kwargs.get("user"),
                "dsn": connect_kwargs.get("dsn"),
                "host": endpoint.get("host"),
                "port": endpoint.get("port"),
                "service_name": endpoint.get("service_name"),
                "ssl": bool(endpoint.get("ssl", False)),
            }
            resolved_password = endpoint["password"]
            secret_ref = str(cfg.raw[side]["connection"]["password_secret"])
            if provider_name == "env":
                env_value = os.environ.get(f"SECRET_{secret_ref}")
                entry["environment_secret_match"] = (
                    env_value is not None
                    and hmac.compare_digest(
                        resolved_password.encode("utf-8"), env_value.encode("utf-8")
                    )
                )
            else:
                entry["environment_secret_match"] = None
            entry["credential_resolution"] = "PASS"
        except Exception as exc:  # noqa: BLE001 - report stage without secret-bearing details
            entry["credential_resolution"] = "FAIL"
            entry["stage"] = "credential_resolution"
            entry["error"] = (
                str(exc) if isinstance(exc, OracleE2EError)
                else f"credential resolution failed ({type(exc).__name__})"
            )
            continue

        try:
            connection = connect(endpoint, driver)
            entry["authentication"] = "PASS"
        except Exception as exc:  # noqa: BLE001 - keep each side independently diagnosable
            code = _ERROR_CODE.search(str(exc))
            entry["authentication"] = "FAIL"
            entry["stage"] = "connection"
            entry["error"] = code.group(0).upper() if code else type(exc).__name__
            continue

        try:
            identity = _identity(connection)
            _verify_identity(
                identity, side=side, owner=owners[side], service=services[side],
                expected_container=containers[side], expected_cdb=expected_cdb,
            )
            entry["identity"] = {"status": "PASS", **identity}
        except Exception as exc:  # noqa: BLE001 - no metadata scan after identity failure
            code = _ERROR_CODE.search(str(exc))
            entry["identity"] = {"status": "FAIL"}
            entry["stage"] = "connection_identity"
            entry["error"] = (
                str(exc) if isinstance(exc, OracleIdentityError)
                else code.group(0).upper() if code else type(exc).__name__
            )
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()

    passed = all(
        item.get("credential_resolution") == "PASS"
        and item.get("authentication") == "PASS"
        and item.get("identity", {}).get("status") == "PASS"
        and item.get("environment_secret_match") is not False
        for item in sides.values()
    )
    return {"status": "PASS" if passed else "FAIL", "read_only": True, "sides": sides}


def _inventory_summary(inventory: Any) -> dict[str, Any]:
    (objects, constraints, blockers, parent_managed, inspection_errors,
     dependency_impact, grants_impact, partitioning) = inventory
    return {
        "objects": objects,
        "object_count": len(objects),
        "constraints": constraints,
        "constraint_count": len(constraints),
        "parent_managed": parent_managed,
        "blockers": blockers,
        "inspection_errors": inspection_errors,
        "dependency_impact": dependency_impact,
        "grants_impact": grants_impact,
        "partitioned_tables": sorted(partitioning),
    }


def run_compatibility_check(cfg: E2EConfig, *, driver: Any) -> dict[str, Any]:
    """Exercise read-only production Oracle discovery and FULL preflight paths."""
    owners, services = validated_oracle_context(cfg)
    expected_cdb, containers = _configure_expected_identity(cfg, services)
    raw_connections: dict[str, Any] = {}
    owner_configs: dict[str, dict[str, Any]] = {}
    identities: dict[str, dict[str, str]] = {}
    audits: dict[str, QueryAudit] = {}
    source: OracleSourceConnector | None = None
    target: OracleTargetConnector | None = None
    executor_connection = None
    try:
        # Establish each connection identity before any catalog scan. A mismatch
        # is terminal; no metadata checks are attempted in an unintended PDB.
        for side in ("source", "target"):
            raw, endpoint = _connect_owner(cfg, side, owners[side], driver)
            raw_connections[side] = raw
            owner_configs[side] = endpoint
            try:
                identity = _identity(raw)
            except OracleIdentityError:
                raise
            except Exception as exc:  # noqa: BLE001 - identity failures stop all metadata probes
                code = _ERROR_CODE.search(str(exc))
                detail = code.group(0).upper() if code else type(exc).__name__
                raise OracleIdentityError(
                    f"{side.upper()} connection identity query failed ({detail}); metadata checks stopped"
                ) from None
            _verify_identity(
                identity, side=side, owner=owners[side], service=services[side],
                expected_container=containers[side], expected_cdb=expected_cdb,
            )
            identities[side] = identity
        report: dict[str, Any] = {
            "status": "PASS",
            "read_only": True,
            "identity": {
                side: identities[side] for side in ("source", "target")
            },
            "source_owner": owners["source"],
            "target_owner": owners["target"],
            "executed_catalog_queries": {},
            "query_failures": [],
            "feature_failures": [],
            "unexercised_paths": [],
        }
        for side in ("source", "target"):
            password = owner_configs[side].get("password")
            audits[side] = QueryAudit(side, (str(password),) if password else ())
            raw_connections[side] = ReadOnlyConnection(raw_connections[side], audits[side])

        source_config = owner_configs["source"]
        target_config = {**owner_configs["target"], "source_engine": "oracle"}
        source = OracleSourceConnector(source_config)
        source._conn = raw_connections["source"]
        source.configure_permission_migration(cfg.raw.get("oracle_permission_migration", {}))
        source.enable_precision_safe_full_load()
        target = OracleTargetConnector(target_config)
        target._conn = raw_connections["target"]

        def safe(side: str, stage: str, operation: Callable[[], Any], summarize=None):
            value = _safe_feature(report[side], audits[side], stage, operation, summarize)
            if isinstance(report[side].get(stage), dict) and report[side][stage].get("status") == "FAIL":
                report["feature_failures"].append({"side": side, "stage": stage,
                    **report[side][stage]})
            return value

        report["source"] = {}
        report["target"] = {}
        source_tables = safe("source", "source.list_objects", source.list_objects,
                             lambda result: {"status": "PASS", "count": len(result), "objects": result}) or []
        safe("source", "source.constraint_states", lambda: source.validate_constraint_states(source_tables))
        safe("source", "source.replacement_object_support", source.validate_schema_replacement_objects)

        schemas = []
        schema_failures = 0
        for table in source_tables:
            schema = safe(
                "source", f"source.get_schema[{table}]", lambda table=table: source.get_schema(table),
                lambda item: {"status": "PASS", "columns": len(item.columns),
                              "indexes": len(item.indexes), "partitioned": bool(item.partition_method)},
            )
            if schema is None:
                schema_failures += 1
            else:
                schemas.append(schema)

        type_defs = safe("source", "source.types", source.list_types,
                         lambda result: {"status": "PASS", "count": len(result),
                                         "types": [{"name": item.name, "kind": item.kind}
                                                   for item in result]}) or []
        comments = safe("source", "source.comments", source.list_comments,
                        lambda result: {"status": "PASS", "count": len(result)}) or []
        source_views = safe("source", "source.views", source.list_views,
                            lambda result: {"status": "PASS", "count": len(result)}) or []
        materialized_views = safe("source", "source.materialized_views", source.list_materialized_views,
                                  lambda result: {"status": "PASS", "count": len(result)}) or []
        sequences = safe("source", "source.sequences", source.list_all_sequences,
                         lambda result: {"status": "PASS", "count": len(result)}) or []
        routines = safe("source", "source.routines", source.list_functions,
                        lambda result: {"status": "PASS", "count": len(result)}) or []
        triggers = safe("source", "source.triggers", source.get_all_triggers,
                        lambda result: {"status": "PASS", "count": len(result)}) or []
        synonyms = safe("source", "source.synonyms", source.list_synonyms,
                         lambda result: {"status": "PASS", "count": len(result)}) or []
        permission_policy = cfg.raw.get("oracle_permission_migration", {})
        permission_enabled = (
            isinstance(permission_policy, dict)
            and permission_policy.get("enabled") is True
        )
        if not permission_enabled:
            safe("source", "source.replacement_grants", source.validate_schema_replacement_grants)
        else:
            # With permission migration enabled, its inventory is the
            # authoritative source-grant preflight. Avoid running the same
            # catalog scan twice when its visibility probe fails.
            report["source"]["source.replacement_grants"] = {
                "status": "PENDING",
                "covered_by": "source.permission_inventory",
            }

        unsupported_datatypes = oracle_full_unsupported_datatypes(schemas)
        report["source"]["datatype_classification"] = {
            "status": "FAIL" if unsupported_datatypes else "PASS",
            "unsupported": [list(item) for item in unsupported_datatypes],
        }
        if unsupported_datatypes:
            report["feature_failures"].append({"side": "source", "stage": "source.datatype_classification",
                "status": "FAIL", "unsupported": [list(item) for item in unsupported_datatypes]})

        source_permission_inventory = None
        if permission_enabled:
            source_permission_inventory = safe(
                "source", "source.permission_inventory", source.discover_permission_inventory,
                lambda result: {"status": "PASS", "users": len(result.get("users", [])),
                                "roles": len(result.get("roles", [])),
                                "memberships": len(result.get("memberships", [])),
                                "grants": len(result.get("grants", []))},
            )
        else:
            report["source"]["source.permission_inventory"] = {
                "status": "SKIPPED", "reason": "permission migration disabled by configuration"
            }
        if permission_enabled:
            grant_stage = report["source"].get("source.permission_inventory", {})
            report["source"]["source.replacement_grants"] = {
                "status": "PASS" if source_permission_inventory is not None else "FAIL",
                "covered_by": "source.permission_inventory",
                **({"error": grant_stage.get("error")}
                   if source_permission_inventory is None else {}),
            }

        target_inventory = None
        target_plan = None
        source_metadata_complete = (
            not schema_failures
            and len(schemas) == len(source_tables)
            and all(report["source"].get(stage, {}).get("status") == "PASS" for stage in (
                "source.replacement_object_support", "source.types", "source.comments",
                "source.views", "source.materialized_views", "source.sequences",
                "source.routines", "source.triggers", "source.synonyms",
                "source.replacement_grants",
            ))
            and not audits["source"].failures
        )
        if source_metadata_complete:
            collection_kinds = {
                str(item.name).casefold(): item.kind.casefold()
                for item in type_defs if item.kind.casefold() in {"varray", "nested_table"}
            }
            for schema in schemas:
                schema.options["oracle_collection_kinds"] = collection_kinds
            source_inventory = {
                "tables": set(source_tables), "schemas": schemas, "types": type_defs,
                "comments": comments, "views": source_views, "materialized_views": materialized_views,
                "sequences": sequences, "routines": routines, "triggers": triggers,
                "synonyms": synonyms,
            }
            target_plan = safe(
                "target", "target.replacement_plan",
                lambda: target.plan_schema_replacement(preserve_target_only=True, **source_inventory),
                lambda plan: plan.as_dict(),
            )
            if target_plan is not None:
                target_inventory = {
                    "objects": list(target_plan.inventory),
                    "object_count": len(target_plan.inventory),
                    "inspection_errors": list(target_plan.inspection_errors),
                    "blockers": list(target_plan.blockers),
                    "plan_status": "inspection_incomplete" if target_plan.inspection_errors
                    else "blocked" if target_plan.blockers else "ready",
                }
                if target_plan.inspection_errors or target_plan.blockers:
                    report["feature_failures"].append({
                        "side": "target", "stage": "target.replacement_plan",
                        "status": "FAIL",
                        "inspection_errors": list(target_plan.inspection_errors),
                        "blockers": list(target_plan.blockers),
                    })
        else:
            report["target"]["target.replacement_plan"] = {
                "status": "SKIPPED", "reason": "SOURCE metadata discovery was incomplete"
            }
            target_inventory = safe(
                "target", "target.read_only_inventory",
                lambda: _target_inventory(raw_connections["target"], owners["target"]),
                lambda result: _inventory_summary(result),
            )

        # Independent TARGET schema inspection exercises the target connector
        # path even when the full replacement plan is blocked by SOURCE metadata.
        for table in source_tables:
            safe("target", f"target.inspect_schema[{table}]", lambda table=table: target.inspect_schema(table),
                 lambda schema: {"status": "PASS", "exists": schema is not None,
                                 "columns": len(schema.columns) if schema else 0})
        safe("target", "target.constraint_states", lambda: target.validate_constraint_states(source_tables))
        safe("target", "target.all_constraint_states", target.validate_all_constraint_states)
        target_tables = []
        audits["target"].stage = "target.owner_object_emptiness"
        try:
            with raw_connections["target"].cursor() as cursor:
                cursor.execute(
                    "SELECT object_name, object_type, subobject_name FROM user_objects "
                    "ORDER BY object_type, object_name, subobject_name"
                )
                target_tables = [
                    tuple(str(item) if item is not None else None for item in row)
                    for row in cursor.fetchall()
                ]
            report["target"]["target.owner_object_emptiness"] = {
                "status": "PASS", "empty": not target_tables,
                "object_count": len(target_tables), "objects": target_tables,
            }
            if target_tables:
                report["feature_failures"].append({
                    "side": "target", "stage": "target.owner_object_emptiness",
                    "status": "FAIL",
                    "error": "TARGET owner contains existing objects; FULL retry is not approved by this read-only check",
                })
        except Exception as exc:  # noqa: BLE001 - inventory must fail closed
            report["target"]["target.owner_object_emptiness"] = {
                "status": "FAIL", "error_type": type(exc).__name__,
                "error": " ".join(str(exc).split())[:1000],
            }
            report["feature_failures"].append({"side": "target", "stage": "target.owner_object_emptiness",
                **report["target"]["target.owner_object_emptiness"]})

        # Permission paths are inspection only. In particular, this checker
        # never calls preflight_permission_migration(), whose optional onboarding
        # branch can issue grants, nor any permission apply/prepare method.
        if isinstance(permission_policy, dict) and permission_policy.get("enabled") is True:
            if source_permission_inventory is not None:
                try:
                    target_endpoint = owner_configs["target"]
                    executor_config = _permission_executor_connection_config(
                        cfg, target_endpoint
                    )
                    executor_raw = connect(executor_config, driver)
                    executor_connection = executor_raw
                    executor_identity = _identity(executor_raw)
                    _verify_identity(
                        executor_identity, side="target permission executor",
                        owner=str(executor_config["username"]), service=services["target"],
                        expected_container=containers["target"], expected_cdb=expected_cdb,
                    )
                    executor_audit = QueryAudit(
                        "target.permission_executor",
                        (str(executor_config.get("password", "")),),
                    )
                    audits["target.permission_executor"] = executor_audit
                    executor_connection = ReadOnlyConnection(executor_raw, executor_audit)
                    report["identity"]["permission_executor"] = executor_identity
                    inaccessible_views = oracle_permissions.verify_executor_catalog_access(executor_connection)
                    authority_gaps = oracle_permissions.executor_authority_gaps(
                        executor_connection, permission_policy, source_permission_inventory
                    )
                    report["target"]["permission_executor_catalog"] = {
                        "status": "FAIL" if inaccessible_views or authority_gaps else "PASS",
                        "inaccessible_views": inaccessible_views,
                        "authority_gaps": authority_gaps,
                    }
                    if inaccessible_views or authority_gaps:
                        report["feature_failures"].append({
                            "side": "target", "stage": "target.permission_executor_catalog",
                            "status": "FAIL", "inaccessible_views": inaccessible_views,
                            "authority_gaps": authority_gaps,
                        })
                    target_permission_inventory = oracle_permissions.discover(
                        executor_connection, owners["target"], permission_policy,
                        catalog_scope="dba",
                    )
                    report["target"]["permission_inventory"] = {
                        "status": "PASS", "users": len(target_permission_inventory.get("users", [])),
                        "roles": len(target_permission_inventory.get("roles", [])),
                        "memberships": len(target_permission_inventory.get("memberships", [])),
                        "grants": len(target_permission_inventory.get("grants", [])),
                    }
                    report["target"]["permission_parity"] = oracle_permissions.compare_inventories(
                        source_permission_inventory, target_permission_inventory,
                        source_owner=owners["source"], target_owner=owners["target"],
                    )
                    if not report["target"]["permission_parity"]["match"]:
                        report["feature_failures"].append({
                            "side": "target", "stage": "target.permission_parity",
                            "status": "FAIL", "error": "source and target permission inventories differ",
                        })
                    audits["target"].stage = "target.permission_preflight"
                    target_permission_scope = {
                        **source_permission_inventory,
                        "tables": set(source_tables), "views": source_views,
                        "materialized_views": materialized_views,
                    }
                    permission_plan = oracle_permissions.preflight_target(
                        raw_connections["target"], permission_policy,
                        target_permission_scope,
                        source_owner=owners["source"], target_owner=owners["target"],
                        target_ssl=bool(owner_configs["target"].get("ssl")),
                        executor_conn=executor_connection,
                        scope_id=oracle_permissions.permission_scope_id(
                            report["identity"]["source"], report["identity"]["target"]
                        ),
                    )
                    report["target"]["permission_preflight"] = {
                        "status": "PASS", "new_users": len(permission_plan.get("new_users", [])),
                        "new_roles": len(permission_plan.get("new_roles", [])),
                        "grants": len(permission_plan.get("object_grants", []))
                        + len(permission_plan.get("system_grants", [])),
                        "memberships": len(permission_plan.get("memberships", [])),
                    }
                except Exception as exc:  # noqa: BLE001 - continue remaining connector metadata checks
                    report["target"]["permission_path"] = {
                        "status": "FAIL", "error_type": type(exc).__name__,
                        "error": (
                            executor_audit.redact(exc) if "executor_audit" in locals()
                            else audits["target"].redact(exc)
                        ),
                    }
                    report["feature_failures"].append({
                        "side": "target", "stage": "target.permission_path",
                        **report["target"]["permission_path"],
                    })
        else:
            report["target"]["permission_path"] = {
                "status": "SKIPPED", "reason": "permission migration disabled by configuration"
            }

        all_audits = [*audits.values()]
        failures = [failure for audit in all_audits for failure in audit.failures]
        for failure in failures:
            report_side = "target" if failure["side"].startswith("target") else failure["side"]
            stage_report = report.get(report_side, {}).get(failure["stage"])
            if isinstance(stage_report, dict):
                stage_report["status"] = "FAIL"
                stage_report.setdefault("query_failures", []).append(failure)
                if failure["stage"] == "target.owner_object_emptiness":
                    stage_report["empty"] = None
                if not any(
                    item.get("side") == failure["side"]
                    and item.get("stage") == failure["stage"]
                    for item in report["feature_failures"]
                ):
                    report["feature_failures"].append({
                        "side": failure["side"], "stage": failure["stage"],
                        "status": "FAIL", "query_failure": failure,
                    })
        query_view_counts: dict[str, int] = {}
        for audit in all_audits:
            for query in audit.queries:
                for view in query["views"]:
                    query_view_counts[view] = query_view_counts.get(view, 0) + 1
        report["executed_catalog_queries"] = {
            "count": sum(len(audit.queries) for audit in all_audits),
            "by_view": dict(sorted(query_view_counts.items())),
            "failed_count": len(failures),
        }
        report["query_inventory"] = [
            query for audit in all_audits for query in audit.queries
        ]
        report["query_failures"] = failures
        report["unexercised_paths"] = _unexercised_paths(
            source_tables, schemas, type_defs, source_views, materialized_views,
            sequences, routines, triggers, synonyms, comments,
        )
        if isinstance(target_inventory, dict):
            target_inventory_objects = target_inventory.get("objects", [])
        elif isinstance(target_inventory, tuple) and target_inventory:
            target_inventory_objects = target_inventory[0]
        else:
            target_inventory_objects = []
        target_type_inventory_present = any(
            isinstance(item, dict) and str(item.get("type", "")).upper() == "TYPE"
            for item in target_inventory_objects
        )
        if type_defs and not target_type_inventory_present:
            report["unexercised_paths"].append(
                "TARGET comparison of pre-existing custom TYPE definitions (no target TYPE exists)"
            )
        target_state = report["target"].get("target.owner_object_emptiness", {})
        target_inventory_ok = (
            target_state.get("status") == "PASS"
            and not audits["target"].failures
            and not report["target"].get("target.replacement_plan", {}).get("inspection_errors", [])
        )
        report["target"]["emptiness_verdict"] = (
            "EMPTY" if target_inventory_ok and target_state.get("empty")
            else "POPULATED" if target_inventory_ok else "UNVERIFIED"
        )
        report["status"] = "PASS" if not failures and not report["feature_failures"] else "FAIL"
        report["limitations"] = [
            "Source DBMS_METADATA definitions were read; target DDL was not generated or executed, and no objects or rows were changed.",
            "Paths absent from the current owner inventory are listed as unexercised; their SQL cannot be LIVE-verified by this run.",
            "Data-row extraction/insertion/comparison, DDL application, cleanup execution, sequence adjustment, and permission application are intentionally never executed by this read-only checker.",
        ]
        return report
    finally:
        for connection in (executor_connection, raw_connections.get("target"), raw_connections.get("source")):
            if connection is not None:
                with suppress(Exception):
                    connection.close()


def _unexercised_paths(
    tables: list[str], schemas: list[Any], types: list[Any], views: list[Any],
    materialized_views: list[Any], sequences: list[Any], routines: list[Any],
    triggers: list[Any], synonyms: list[Any], comments: list[Any],
) -> list[str]:
    paths = {
        "table schema / constraints / indexes / partitions / column LOB metadata": bool(tables),
        "custom TYPE catalog and DBMS_METADATA DDL discovery": bool(types),
        "views and view dependency metadata": bool(views),
        "materialized view catalog and dependencies": bool(materialized_views),
        "sequence option catalogs and identity sequence mapping": bool(sequences),
        "routines and package metadata": bool(routines),
        "trigger metadata": bool(triggers),
        "synonym dependency metadata": bool(synonyms),
        "table/column comments": bool(comments),
    }
    return [name for name, exercised in paths.items() if not exercised]


def _target_inventory_summary(report: dict[str, Any]) -> dict[str, Any]:
    target = report.get("target", {})
    emptiness = target.get("target.owner_object_emptiness", {})
    inventory = target.get("target.replacement_plan", {})
    if not inventory:
        inventory = target.get("target.read_only_inventory", {})
    objects = inventory.get("target_inventory", inventory.get("objects", []))
    if isinstance(objects, dict):
        objects = list(objects.values())
    by_type: dict[str, int] = {}
    for item in objects if isinstance(objects, list) else []:
        if isinstance(item, dict):
            kind = str(item.get("type", "UNKNOWN")).upper()
        elif isinstance(item, (list, tuple)) and len(item) > 1:
            kind = str(item[1]).upper()
        else:
            kind = "UNKNOWN"
        by_type[kind] = by_type.get(kind, 0) + 1
    object_count = emptiness.get("object_count")
    if object_count is None:
        object_count = inventory.get("object_count", len(objects) if isinstance(objects, list) else None)
    return {
        "status": "PASS" if emptiness.get("status") == "PASS" else "FAIL",
        "verdict": target.get("emptiness_verdict", "UNVERIFIED"),
        "empty": emptiness.get("empty"),
        "object_count": object_count,
        "objects_by_type": dict(sorted(by_type.items())),
    }


def _compact_report(report: dict[str, Any], details_file: str | None) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    for failure in report.get("feature_failures", []):
        blocker = {
            key: failure[key]
            for key in ("side", "stage", "error", "blockers", "inspection_errors", "unsupported")
            if failure.get(key) not in (None, [], "")
        }
        blockers.append(blocker)
    grouped_queries: dict[tuple[str, str, str], int] = {}
    for failure in report.get("query_failures", []):
        views = failure.get("views") or ["unknown"]
        for view in views:
            key = (
                str(failure.get("stage", "unknown")),
                str(view),
                str(failure.get("error_code", "query_error")),
            )
            grouped_queries[key] = grouped_queries.get(key, 0) + 1
    if grouped_queries:
        blockers.append({
            "stage": "catalog_queries",
            "failures": [
                {"stage": stage, "view": view, "code": code, "count": count}
                for (stage, view, code), count in sorted(grouped_queries.items())
            ],
        })
    result = {
        "status": "PASS" if report.get("status") == "PASS" else "FAIL",
        "blockers": blockers,
        "target_inventory": _target_inventory_summary(report),
    }
    if details_file:
        result["details_file"] = details_file
    return result


def _write_details(report: dict[str, Any], configured_path: str | None) -> str:
    if configured_path:
        path = os.path.abspath(configured_path)
        if os.path.exists(path):
            raise FileExistsError("Details file already exists; choose a new path to preserve prior evidence")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, default=str)
            stream.write("\n")
        return path
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="oracle_metadata_compatibility_",
        suffix=".json", delete=False,
    ) as stream:
        json.dump(report, stream, indent=2, default=str)
        stream.write("\n")
        return stream.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Oracle FULL migration catalog compatibility audit"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--auth-only", action="store_true",
        help="Safely diagnose configured SOURCE/TARGET owner credential resolution and identity",
    )
    parser.add_argument(
        "--details-file",
        help="Write the complete sanitized report and SQL inventory to a new file (never overwrite)",
    )
    args = parser.parse_args(argv)
    try:
        import oracledb

        cfg = load_e2e_config(args.config)
        if args.auth_only:
            result = run_auth_diagnostics(cfg, driver=oracledb)
            print(json.dumps(result, indent=2, default=str))
            return 0 if result["status"] == "PASS" else 1
        result = run_compatibility_check(cfg, driver=oracledb)
    except OracleE2EError as exc:
        print(json.dumps({
            "status": "FAIL", "blockers": [{
                "stage": "credential_resolution", "error": str(exc),
            }],
            "target_inventory": {"status": "FAIL", "verdict": "UNVERIFIED"},
        }, indent=2))
        return 2
    except OracleIdentityError as exc:
        print(json.dumps({
            "status": "FAIL", "blockers": [{
                "stage": "connection_identity", "error": str(exc),
            }],
            "target_inventory": {"status": "FAIL", "verdict": "UNVERIFIED"},
        }, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must not print credentials or traceback data
        error = _ERROR_CODE.search(str(exc))
        info = f" ({error.group(0).upper()})" if error else f" ({type(exc).__name__})"
        print(json.dumps({
            "status": "FAIL", "blockers": [{
                "stage": "configuration_or_connect", "error": info,
            }],
            "target_inventory": {"status": "FAIL", "verdict": "UNVERIFIED"},
        }, indent=2))
        return 2
    try:
        details_file = _write_details(result, args.details_file)
    except Exception as exc:  # noqa: BLE001 - do not lose a completed read-only report
        print(json.dumps({
            "status": "FAIL", "blockers": [{
                "stage": "details_report", "error": f"{type(exc).__name__}: {exc}"
            }],
            "target_inventory": _target_inventory_summary(result),
        }, indent=2))
        return 1
    print(json.dumps(_compact_report(result, details_file), indent=2, default=str))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
