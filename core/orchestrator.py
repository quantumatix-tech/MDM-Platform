from __future__ import annotations

import itertools
import json
import pathlib
import platform
import re
import subprocess
import time
import traceback
from collections.abc import Iterator
from typing import Any

from core.alerting import create_notifier
from core.assessment.report_generator import AssessmentReport, AssessmentReportGenerator
from core.audit_logger import audit_log, get_run_id, set_run_id
from core.connectors import create_cdc_engine
from core.connectors.oracle import permissions as oracle_permissions
from core.connectors.base import (
    CDCEngine,
    Schema,
    SourceConnector,
    TargetConnector,
    UpsertResult,
)
from core.schema_mapping.registry import TypeMappingRegistry
from core.dependency_order import build_dependency_graph, order_data_load_objects
from core.secrets import create_secret_provider
from core.status_server import StatusServer
from core.validator import Validator
from core.connectors.mssql.objects.partition import PartitionDefinitionMismatch


def _safe_error_text(value: Any, secret_values: Any = ()) -> str:
    """Redact common credential formats before exposing errors to reports/UI."""
    raw = str(value)
    for secret in sorted(
        {str(item) for item in secret_values if item is not None and str(item)},
        key=len, reverse=True,
    ):
        raw = raw.replace(secret, "<redacted>")
    text = " ".join(raw.split())
    text = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|token|access_key)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>", text,
    )
    return re.sub(r"(://[^:/\s]+:)[^@/\s]+@", r"\1<redacted>@", text)


def _oracle_exception_diagnostic(
    error: BaseException, *, phase: str, object_name: str,
    connector: str | None = None, secret_values: Any = (),
) -> dict[str, Any]:
    """Build credential-safe location and cause details for an Oracle failure."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    frame = frames[-1] if frames else None

    def error_code(exc: BaseException) -> str | None:
        match = re.search(r"\b(?:DPY-\d{4}|ORA-\d{5})\b", str(exc), re.IGNORECASE)
        return match.group(0).upper() if match else None

    chain_text = [
        _safe_error_text(f"{type(exc).__name__}: {exc}", secret_values) for exc in chain
    ]
    location = None
    if frame:
        location = {
            "file": frame.filename,
            "function": frame.name,
            "line": frame.lineno,
        }
    if connector is None:
        filename = frame.filename.replace("\\", "/").lower() if frame else ""
        connector = (
            "Oracle source" if filename.endswith("/oracle/source.py")
            else "Oracle target"
        )
    stack = _safe_error_text(
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        secret_values,
    ) if error.__traceback__ else None
    code = next((error_code(exc) for exc in chain if error_code(exc)), None)
    data_complete = phase in {
        "apply_constraints", "oracle_schema_replacement_verification", "data_validation"
    }
    return {
        "phase": phase,
        "operation": frame.name if frame else phase,
        "connector": connector,
        "object": object_name,
        "exception_class": type(error).__name__,
        "error_code": code,
        "location": location,
        "cause_chain": chain_text,
        "stack_trace": stack,
        "data_migration_completed": data_complete,
        "target_state_may_have_changed": True,
        "action": "Inspect target data and schema before rerunning FULL migration.",
    }


class _PartitionDefinitionError(RuntimeError):
    """Raised when stop_on_error is enabled for partition validation failures."""


def _mssql_partition_comparison(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Compare source-managed SQL Server partition objects from two inventories."""
    result: dict[str, Any] = {"definition_matches": True, "mismatches": []}

    def compare_group(kind: str, source_items: dict, target_items: dict) -> None:
        for key in sorted(source_items, key=str):
            source_def = source_items[key]
            target_def = target_items.get(key)
            if target_def is None:
                result["definition_matches"] = False
                mismatch = {
                    "object": ".".join(str(part) for part in key) if isinstance(key, tuple) else key,
                    "kind": kind, "source": source_def,
                    "target": None, "reason": f"target {kind} is missing",
                }
                if kind == "partitioned_table_index":
                    mismatch["table_name"] = source_def["table_name"]
                    mismatch["schema_name"] = source_def["schema_name"]
                result["mismatches"].append(mismatch)
            elif source_def != target_def:
                result["definition_matches"] = False
                mismatch = {
                    "object": ".".join(str(part) for part in key) if isinstance(key, tuple) else key,
                    "kind": kind, "source": source_def,
                    "target": target_def, "reason": f"source and target {kind} definitions differ",
                }
                if kind == "partitioned_table_index":
                    mismatch["table_name"] = source_def["table_name"]
                    mismatch["schema_name"] = source_def["schema_name"]
                result["mismatches"].append(mismatch)

    source_functions = source.get("functions", {})
    source_schemes = source.get("schemes", {})
    source_tables = source.get("tables", {})
    target_functions = target.get("functions", {})
    target_schemes = target.get("schemes", {})
    target_tables = target.get("tables", {})
    compare_group("partition_function", source_functions, target_functions)
    for name, definition in source_functions.items():
        if not definition.get("catalog_consistent", True):
            result["definition_matches"] = False
            result["mismatches"].append({
                "object": name, "kind": "partition_function", "source": definition,
                "target": target_functions.get(name),
                "reason": definition.get("catalog_error", "source partition function metadata is inconsistent"),
            })
    for name, definition in target_functions.items():
        if name in source_functions and not definition.get("catalog_consistent", True):
            result["definition_matches"] = False
            result["mismatches"].append({
                "object": name, "kind": "partition_function", "source": source_functions[name],
                "target": definition,
                "reason": definition.get("catalog_error", "target partition function metadata is inconsistent"),
            })
    compare_group("partition_scheme", source_schemes, target_schemes)
    compare_group("partitioned_table_index", source_tables, target_tables)
    return result


class MigrationOrchestrator:
    def __init__(
        self,
        source: SourceConnector,
        target: TargetConnector,
        config: dict[str, Any],
        status_server: StatusServer | None = None,
    ) -> None:
        self._source = source
        self._target = target
        self._config = config
        self._registry = TypeMappingRegistry()
        self._assessment_gen = AssessmentReportGenerator(self._registry)
        self._batch_size = config.get("migration", {}).get("batch_size", 1000)
        self._mode = config.get("migration", {}).get("mode", "full")
        self._stop_on_error = config.get("migration", {}).get("stop_on_error", False)
        self._allow_source_service_restart = config.get("cdc", {}).get(
            "allow_source_service_restart", False
        )
        self._field_mappings = config.get("migration", {}).get("field_mappings", [])
        self._notifier = create_notifier(config.get("alerting", {}))
        source_provider = config.get("source", {}).get("secret_provider")
        target_provider = config.get("target", {}).get("secret_provider")
        self._secret_resolver = (
            create_secret_provider(config)
            if source_provider is None or target_provider is None
            else None
        )
        self._source_secret_resolver = (
            create_secret_provider(config, source_provider)
            if source_provider is not None
            else self._secret_resolver
        )
        self._target_secret_resolver = (
            create_secret_provider(config, target_provider)
            if target_provider is not None
            else self._secret_resolver
        )
        self._status = status_server
        self._redaction_secrets: set[str] = set()

    def _resolve_secrets(self, config: dict[str, Any]) -> dict[str, Any]:
        if self._secret_resolver is None:
            return config
        resolved = dict(config)
        if "password_secret" in resolved:
            resolved["password"] = self._secret_resolver.resolve(resolved["password_secret"])
        return resolved

    def _update_status(self, phase: str, progress: int, errors: list[str]) -> None:
        if self._status is not None:
            self._status.update_status(
                phase, progress, [self._redact_error(error) for error in errors]
            )

    def _redaction_values(self) -> tuple[str, ...]:
        # Some diagnostic-only callers/tests construct an orchestrator without
        # running __init__; keep redaction safe for those instances too.
        values = set(getattr(self, "_redaction_secrets", set()))
        for connector in (getattr(self, "_source", None), getattr(self, "_target", None)):
            config = getattr(connector, "_config", {})
            if not isinstance(config, dict):
                continue
            for key in ("password", "_ssl_wallet_password"):
                value = config.get(key)
                if isinstance(value, str) and value:
                    values.add(value)
        return tuple(values)

    def _redact_error(self, value: Any) -> str:
        return _safe_error_text(value, self._redaction_values())

    def _estimate_total_rows(self, objects: list[str], schema_map: dict[str, str | None] | None = None) -> int:
        total = 0
        for obj_name in objects:
            try:
                schema_name = schema_map.get(obj_name) if schema_map else None
                total += self._source.get_object_count(obj_name, schema_name=schema_name)
            except Exception:
                pass
        return total

    def _record_object_failure(
        self,
        failures: list[dict[str, Any]],
        all_errors: list[str],
        phase: str,
        object_name: str,
        error: Exception | str,
        *,
        connector: str | None = None,
    ) -> str:
        safe_error = self._redact_error(error)
        oracle_failure = (
            self._config.get("target", {}).get("engine") == "oracle"
            and isinstance(error, BaseException)
        )
        diagnostic = (
            _oracle_exception_diagnostic(
                error, phase=phase, object_name=object_name, connector=connector,
                secret_values=self._redaction_values(),
            )
            if oracle_failure else None
        )
        if diagnostic:
            connector_label = diagnostic["connector"]
            location = diagnostic.get("location") or {}
            code = f" ({diagnostic['error_code']})" if diagnostic.get("error_code") else ""
            at = (
                f" at {pathlib.Path(location['file']).name}:{location['line']} "
                f"in {location['function']}()"
                if location else ""
            )
            cause_chain = diagnostic.get("cause_chain") or []
            root_cause = (
                f" Root cause: {cause_chain[-1]}"
                if len(cause_chain) > 1 else ""
            )
            message = (
                f"{phase} failed for object {object_name!r} on {connector_label}: "
                f"{diagnostic['exception_class']}{code}{at}: {safe_error}.{root_cause} "
                + (
                    "Data load had completed before this failure. "
                    if diagnostic["data_migration_completed"] else ""
                )
                + f"{diagnostic['action']}"
            )
        else:
            message = f"{phase} failed for object {object_name!r}: {safe_error}"
        failure: dict[str, Any] = {
            "phase": phase,
            "object": object_name,
            "error": message if diagnostic else safe_error,
            **({"exception": safe_error} if diagnostic else {}),
        }
        if diagnostic:
            failure["diagnostic"] = {
                key: value for key, value in diagnostic.items() if key != "stack_trace"
            }
        failures.append(failure)
        all_errors.append(message)
        audit_details: dict[str, Any] = {"object": object_name, "error": safe_error}
        if diagnostic:
            # Full sanitized trace belongs in the diagnostic log, not the user report.
            audit_details["diagnostic"] = diagnostic
        audit_log(
            phase=phase,
            status="error",
            details=audit_details,
        )
        return message

    def _oracle_replacement_inventory(
        self, objects: list[str]
    ) -> dict[str, Any]:
        """Discover the complete supported source inventory used by owner cleanup."""
        source_owner = str(
            self._config.get("source", {}).get("connection", {}).get("username", "")
        ).strip().upper()
        target_owner = str(
            self._config.get("target", {}).get("connection", {}).get("username", "")
        ).strip().upper()
        if source_owner in {"SYS", "SYSTEM"} or target_owner in {"SYS", "SYSTEM"}:
            raise RuntimeError("Oracle schema replacement refuses SYS and SYSTEM owners")
        schemas = [self._source.get_schema(name) for name in objects]
        types = list(self._source.list_types())
        discovered_type_names = {str(item.name).casefold() for item in types}
        collection_kinds = {
            str(item.name).casefold(): item.kind.casefold()
            for item in types if item.kind.casefold() in {"varray", "nested_table"}
        }
        for schema in schemas:
            schema.options["oracle_collection_kinds"] = collection_kinds
            for column_name, metadata in schema.options.get("oracle_object_types", {}).items():
                type_owner = str(metadata.get("owner", "")).casefold()
                if type_owner != source_owner.casefold() or str(metadata.get("name", "")).casefold() not in discovered_type_names:
                    raise RuntimeError(
                        f"Oracle OBJECT column {schema.name}.{column_name} references a TYPE "
                        "that is not included in the validated source TYPE inventory"
                    )
        comments = list(self._source.list_comments())
        schema_by_name = {schema.name.casefold(): schema for schema in schemas}
        unsupported_comments = []
        for comment in comments:
            schema = schema_by_name.get(str(comment.object_name).casefold())
            if schema is None:
                unsupported_comments.append(f"{comment.object_type} {comment.object_name}")
                continue
            if str(comment.object_type).upper() == "COLUMN" and not any(
                column.name.casefold() == str(comment.column_name or "").casefold()
                for column in schema.columns
            ):
                unsupported_comments.append(
                    f"COLUMN {comment.object_name}.{comment.column_name}"
                )
        if unsupported_comments:
            raise RuntimeError(
                "Oracle FULL cannot preserve comments for unsupported source target(s): "
                + ", ".join(unsupported_comments)
            )
        cross_owner_foreign_keys = [
            f"{schema.name}.{foreign.name} references {foreign.ref_schema}.{foreign.ref_table}"
            for schema in schemas
            for foreign in schema.foreign_keys
            if foreign.ref_schema
            and str(foreign.ref_schema).casefold() != source_owner.casefold()
        ]
        if cross_owner_foreign_keys:
            raise RuntimeError(
                "Oracle FULL cannot recreate source cross-owner foreign key(s): "
                + "; ".join(cross_owner_foreign_keys)
            )
        return {
            "tables": set(objects),
            "schemas": schemas,
            "views": list(self._source.list_views()),
            "materialized_views": list(self._source.list_materialized_views()),
            "sequences": list(self._source.list_all_sequences()),
            "routines": list(self._source.list_functions()),
            "types": types,
            "triggers": list(self._source.get_all_triggers()),
            "synonyms": list(self._source.list_synonyms()),
            "comments": comments,
        }

    @staticmethod
    def _oracle_privilege_definitions(inventory: dict[str, Any]) -> dict[str, Any]:
        return {
            "tables": inventory["tables"],
            "views": inventory["views"],
            "materialized_views": inventory["materialized_views"],
            "sequences": inventory["sequences"],
            "routines": inventory["routines"],
            "types": inventory.get("types", []),
            "triggers": inventory["triggers"],
            "synonyms": inventory["synonyms"],
        }

    def _inspect_oracle_privileges(self, inventory: dict[str, Any]) -> dict[str, Any]:
        inspect = getattr(self._target, "inspect_object_privileges", None)
        definitions = self._oracle_privilege_definitions(inventory)
        if callable(inspect):
            return inspect(**definitions)
        # Compatibility for test doubles or older custom Oracle targets.
        self._target.validate_object_privileges(**definitions)
        return {"status": "inspected", "missing": [], "proposed_grants": []}

    @staticmethod
    def _oracle_privilege_blocker(report: dict[str, Any]) -> str:
        missing = report.get("missing", [])
        commands = report.get("proposed_grants", [])
        message = "Oracle target privilege preflight is missing: " + ", ".join(missing)
        if commands:
            message += "\nTarget DBA grant commands:\n" + "\n".join(commands)
        return message

    def _resolve_oracle_bootstrap_admin_connection(self) -> dict[str, Any]:
        bootstrap = self._config.get("oracle_privilege_bootstrap", {})
        connection = bootstrap.get("admin_connection") if isinstance(bootstrap, dict) else None
        if not isinstance(connection, dict) or not connection:
            raise RuntimeError(
                "Oracle privilege bootstrap is enabled but admin_connection is missing"
            )
        if "password" in connection or not connection.get("password_secret"):
            raise RuntimeError(
                "Oracle bootstrap admin_connection must use a separate password_secret; "
                "plaintext passwords are not accepted"
            )
        admin = dict(connection)
        provider_name = bootstrap.get("secret_provider")
        if provider_name is not None:
            resolver = create_secret_provider(self._config, provider_name)
        else:
            resolver = self._target_secret_resolver or self._secret_resolver
        if resolver is None:
            raise RuntimeError("Oracle bootstrap secret provider is not configured")
        admin["password"] = resolver.resolve(admin["password_secret"])
        if "ssl_wallet_password_secret" in admin:
            admin["_ssl_wallet_password"] = resolver.resolve(
                admin["ssl_wallet_password_secret"]
            )
        if not str(admin.get("password", "")):
            raise RuntimeError("Oracle bootstrap admin password secret resolved to an empty value")
        return admin

    def _resolve_oracle_permission_executor_connection(self) -> dict[str, Any]:
        """Build a PDB-local executor endpoint from target endpoint + secret ref."""
        policy = self._config.get("oracle_permission_migration", {})
        executor = policy.get("executor") if isinstance(policy, dict) else None
        if not isinstance(executor, dict) or not executor.get("username"):
            raise RuntimeError("Oracle permission migration requires executor.username")
        if "password" in executor:
            raise RuntimeError(
                "Oracle permission executor plaintext passwords are not accepted; use password_secret or target_credentials: true"
            )
        connection = dict(getattr(self._target, "_config", {}))
        connection["username"] = str(executor["username"]).strip()
        connection.pop("password", None)
        provider_name = policy.get("secret_provider")
        resolver = (
            create_secret_provider(self._config, provider_name)
            if provider_name is not None
            else self._target_secret_resolver or self._secret_resolver
        )
        if executor.get("target_credentials") is True:
            target_config = getattr(self._target, "_config", {})
            target_username = str(target_config.get("username", ""))
            if target_username.casefold() != str(executor["username"]).strip().casefold():
                raise RuntimeError("Oracle executor target_credentials requires executor.username to match target.username")
            secret = str(target_config.get("password", ""))
        elif executor.get("password_secret"):
            if resolver is None:
                raise RuntimeError("Oracle permission executor secret provider is not configured")
            secret_name = str(executor["password_secret"])
            try:
                secret = resolver.resolve(secret_name)
            except KeyError:
                executor_provider = (
                    provider_name
                    or self._config.get("target", {}).get("secret_provider")
                    or self._config.get("secrets", {}).get("provider", "env")
                )
                if executor_provider != "env":
                    raise
                # Oracle executor credentials may be persisted in the existing
                # encrypted Local Store while endpoint credentials remain env.
                # This fallback is executor-only and never changes SOURCE/TARGET
                # provider resolution.
                local_store = create_secret_provider(self._config, "local_encrypted_file")
                if local_store is None:
                    raise
                secret = local_store.resolve(secret_name)
        else:
            raise RuntimeError("Oracle permission executor requires password_secret or explicit target_credentials: true")
        if not secret:
            raise RuntimeError("Oracle permission executor password secret resolved to an empty value")
        connection["password"] = secret
        self._redaction_secrets.add(secret)
        connection.pop("password_secret", None)
        return connection

    def _resolve_oracle_permission_onboarding_admin(self) -> dict[str, Any] | None:
        policy = self._config.get("oracle_permission_migration", {})
        onboarding = policy.get("onboarding", {}) if isinstance(policy, dict) else {}
        if not isinstance(onboarding, dict) or onboarding.get("enabled") is not True:
            return None
        admin = onboarding.get("admin_connection")
        if not isinstance(admin, dict) or not admin.get("username") or not admin.get("password_secret"):
            raise RuntimeError(
                "Oracle permission onboarding requires onboarding.admin_connection.username and password_secret"
            )
        connection = dict(getattr(self._target, "_config", {}))
        connection["username"] = str(admin["username"]).strip()
        connection.pop("password", None)
        provider_name = policy.get("secret_provider")
        resolver = (
            create_secret_provider(self._config, provider_name)
            if provider_name is not None
            else self._target_secret_resolver or self._secret_resolver
        )
        if resolver is None:
            raise RuntimeError("Oracle permission onboarding secret provider is not configured")
        secret = resolver.resolve(str(admin["password_secret"]))
        if not secret:
            raise RuntimeError("Oracle permission onboarding admin secret resolved to an empty value")
        self._redaction_secrets.add(secret)
        connection["password"] = secret
        return connection

    def _bootstrap_oracle_privileges(
        self, inventory: dict[str, Any], report: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply only explicitly approved privileges, then verify the target again."""
        missing = sorted({str(item).upper() for item in report.get("missing", [])})
        if not missing:
            return {"status": "not_required", "granted": []}
        bootstrap = self._config.get("oracle_privilege_bootstrap", {})
        if not isinstance(bootstrap, dict) or bootstrap.get("enabled") is not True:
            raise RuntimeError(self._oracle_privilege_blocker(report))
        from core.connectors.oracle.objects import ORACLE_BOOTSTRAP_PRIVILEGES

        approved_value = bootstrap.get("approved_privileges", [])
        if not isinstance(approved_value, (list, tuple)):
            raise TypeError("oracle_privilege_bootstrap.approved_privileges must be a list")
        approved = [str(value).strip().upper() for value in approved_value]
        invalid = sorted(set(approved) - ORACLE_BOOTSTRAP_PRIVILEGES)
        if invalid:
            raise RuntimeError(
                "Oracle privilege bootstrap refuses unsupported approved privilege(s): "
                + ", ".join(invalid)
            )
        not_approved = sorted(set(missing) - set(approved))
        if not_approved:
            raise RuntimeError(
                self._oracle_privilege_blocker(report)
                + "\nBootstrap is not authorized for: " + ", ".join(not_approved)
            )
        admin_connection = self._resolve_oracle_bootstrap_admin_connection()
        target_owner = str(
            self._config.get("target", {}).get("connection", {}).get("username", "")
        ).strip()
        bootstrap_method = getattr(self._target, "bootstrap_object_privileges", None)
        if not callable(bootstrap_method):
            raise RuntimeError(
                self._oracle_privilege_blocker(report)
                + "\nThe configured Oracle target does not support authorized privilege bootstrap"
            )
        granted = bootstrap_method(
            missing, admin_connection, target_owner=target_owner
        )
        verified = self._inspect_oracle_privileges(inventory)
        if verified.get("missing"):
            raise RuntimeError(
                "Oracle target privilege re-verification still reports missing privileges: "
                + ", ".join(verified["missing"])
                + ("\n" + "\n".join(verified.get("proposed_grants", []))
                   if verified.get("proposed_grants") else "")
            )
        audit_log(
            phase="oracle_privilege_bootstrap",
            status="completed",
            details={"target_owner": target_owner, "privileges": sorted(granted or [])},
        )
        return {"status": "completed", "granted": sorted(granted or []), "verified": verified}

    def plan_oracle_schema_replacement(self, *, mode: str = "full") -> dict[str, Any]:
        """Connect, inspect, and print a read-only Oracle replacement plan."""
        if not (
            self._config.get("source", {}).get("engine") == "oracle"
            and self._config.get("target", {}).get("engine") == "oracle"
            and mode == "full"
        ):
            raise RuntimeError("Oracle schema replacement planning requires Oracle-to-Oracle FULL mode")
        self._resolve_connector_secrets(
            self._source, self._config.get("source", {}), self._source_secret_resolver
        )
        self._resolve_connector_secrets(
            self._target, self._config.get("target", {}), self._target_secret_resolver
        )
        self._apply_schema_scope(self._source)
        self._source.connect()
        self._target.connect()
        try:
            # list_objects() is owner-wide by default and applies the optional
            # include_tables filter when explicitly configured.
            objects = self._source.list_objects()
            preflight_blockers: list[str] = []
            for label, check in (
                ("source supported-object preflight", self._source.validate_schema_replacement_objects),
                ("source constraint-state preflight", lambda: self._source.validate_constraint_states(objects)),
                ("source grant preflight", self._source.validate_schema_replacement_grants),
            ):
                try:
                    check()
                except Exception as exc:
                    preflight_blockers.append(f"{label}: {exc}")
            inventory = self._oracle_replacement_inventory(objects)
            from core.connectors.oracle.precision import oracle_full_unsupported_datatypes

            datatype_blockers = [
                "source datatype compatibility preflight: "
                f"Unsupported datatype {datatype} in {table}.{column}"
                for table, column, datatype in oracle_full_unsupported_datatypes(
                    inventory["schemas"]
                )
            ]
            preflight_blockers.extend(datatype_blockers)
            privilege_report: dict[str, Any] | None = None
            for label, check in (
                ("target constraint-state preflight", self._target.validate_all_constraint_states),
            ):
                try:
                    check()
                except Exception as exc:
                    preflight_blockers.append(f"{label}: {exc}")
            try:
                privilege_report = self._inspect_oracle_privileges(inventory)
                if privilege_report.get("missing"):
                    preflight_blockers.append(self._oracle_privilege_blocker(privilege_report))
            except Exception as exc:
                preflight_blockers.append(f"target object privilege preflight: {exc}")
            plan = self._target.plan_schema_replacement(
                preserve_target_only=True, **inventory
            )
            report = plan.as_dict()
            from core.connectors.oracle.replacement import overwrite_authorization

            replacement_policy = self._config.get("migration", {}).get(
                "oracle_schema_replacement", {}
            )
            replacement_policy = replacement_policy if isinstance(replacement_policy, dict) else {}
            overwrite = overwrite_authorization(
                plan, source_inventory=inventory, policy=replacement_policy
            )
            report["overwrite_authorization"] = overwrite
            if overwrite["status"] == "blocked":
                report["blockers"] = sorted(set([
                    *report.get("blockers", []),
                    str(overwrite["reason"]),
                ]))
                if report.get("status") == "ready":
                    report["status"] = "blocked"
            if privilege_report is not None:
                report["privilege_preflight"] = privilege_report
            if preflight_blockers:
                report["blockers"] = sorted(set(report["blockers"] + preflight_blockers))
                report["status"] = (
                    "blocked" if datatype_blockers else
                    "inspection_incomplete" if report.get("inspection_errors") else "blocked"
                )
            return report
        finally:
            try:
                self._source.close()
            finally:
                self._target.close()

    def run_full(self, *, approve_legacy_permissions: bool = False) -> dict[str, Any]:
        run_id = get_run_id()
        set_run_id(run_id)
        start_time = time.time()
        audit_log(phase="run_full", status="started", details={"run_id": run_id})

        result: dict[str, Any] = {
            "run_id": run_id,
            "mode": "full",
            "phases": {},
        }

        all_errors: list[str] = []
        object_failures: list[dict[str, Any]] = []
        failed_objects: set[str] = set()
        result["phases"]["object_failures"] = object_failures
        oracle_full_objects: dict[str, list[Any]] | None = None
        oracle_replacement_inventory: dict[str, Any] | None = None
        oracle_permission_plan: dict[str, Any] | None = None
        oracle_permission_application_failed = False
        oracle_permission_policy = self._config.get("oracle_permission_migration", {})
        oracle_permissions_enabled = (
            isinstance(oracle_permission_policy, dict)
            and oracle_permission_policy.get("enabled") is True
        )
        oracle_target_unavailable = False
        run_full_failure_connector: str | None = None
        oracle_full_replace = (
            self._config.get("source", {}).get("engine") == "oracle"
            and self._config.get("target", {}).get("engine") == "oracle"
            and callable(getattr(self._target, "plan_schema_replacement", None))
            and callable(getattr(self._target, "apply_schema_replacement", None))
        )
        oracle_object_preparation_complete = True
        oracle_object_phase_results: dict[str, dict[str, str]] = {
            "views": {}, "routines": {}, "triggers": {}, "synonyms": {},
            "materialized_views": {},
        }

        def _run_phase(name: str, fn: Any, progress: int, critical: bool = False) -> Any:
            """Run a migration phase, log result, update progress."""
            try:
                result_val = fn()
                result["phases"][name] = result_val if result_val is not None else "success"
                self._update_status(name, progress, all_errors)
                return result_val
            except Exception as exc:
                msg = str(exc)
                result["phases"][name] = f"error: {msg}"
                all_errors.append(msg)
                audit_log(phase=name, status="error", details={"error": msg})
                self._update_status(name, progress, all_errors)
                if critical:
                    raise
                return None

        try:
            # ---------- Connect ----------
            if "oracle_permission_migration" in self._config:
                if not isinstance(oracle_permission_policy, dict):
                    raise RuntimeError("oracle_permission_migration must be an object")
                if "enabled" in oracle_permission_policy and not isinstance(
                    oracle_permission_policy["enabled"], bool
                ):
                    raise RuntimeError("oracle_permission_migration.enabled must be boolean")
            if approve_legacy_permissions and isinstance(oracle_permission_policy, dict):
                onboarding = oracle_permission_policy.get("onboarding", {})
                if isinstance(onboarding, dict) and onboarding.get("enabled") is True:
                    raise RuntimeError(
                        "Legacy-grant approval cannot be combined with Oracle executor onboarding; "
                        "complete the separately authorized executor setup first"
                    )
            self._resolve_connector_secrets(
                self._source,
                self._config.get("source", {}),
                self._source_secret_resolver,
            )
            self._resolve_connector_secrets(
                self._target,
                self._config.get("target", {}),
                self._target_secret_resolver,
            )
            if oracle_permissions_enabled:
                if not oracle_full_replace:
                    raise RuntimeError(
                        "Oracle permission migration is supported only for Oracle-to-Oracle FULL"
                    )
                configure_permissions = getattr(
                    self._source, "configure_permission_migration", None
                )
                if not callable(configure_permissions):
                    raise RuntimeError(
                        "Oracle source lacks explicit permission-migration discovery"
                    )
                configure_permissions(oracle_permission_policy)
            self._apply_schema_scope(self._source)
            self._source.connect()
            self._target.connect()
            if oracle_full_replace:
                for connector in (self._source, self._target):
                    enable_precision = getattr(
                        connector, "enable_precision_safe_full_load", None
                    )
                    if callable(enable_precision):
                        enable_precision()
                # Oracle discovery is owner-wide by default. An explicitly
                # configured include_tables filter remains an intentional
                # source-table scope for discovery, replacement and validation.
            result["phases"]["connect"] = "success"
            self._update_status("connect", 2, [])

            self._target.ensure_database_exists()
            result["phases"]["ensure_database"] = "success"
            self._update_status("ensure_database", 4, [])

            objects = self._source.list_objects()
            result["phases"]["discover"] = {"objects": objects}
            if (
                self._config.get("source", {}).get("engine") == "oracle"
                and self._config.get("target", {}).get("engine") == "oracle"
            ):
                validate_constraint_states = getattr(
                    self._source, "validate_constraint_states", None
                )
                validate_target_constraint_states = getattr(
                    self._target, "validate_constraint_states", None
                )
                try:
                    if callable(validate_constraint_states):
                        validate_constraint_states(objects)
                    if callable(validate_target_constraint_states):
                        validate_target_constraint_states(objects)
                    if callable(validate_constraint_states) or callable(
                        validate_target_constraint_states
                    ):
                        result["phases"]["constraint_state_preflight"] = "success"
                except Exception as exc:
                    result["phases"]["constraint_state_preflight"] = "failed"
                    self._record_object_failure(
                        object_failures,
                        all_errors,
                        "constraint_state_preflight",
                        "<source or target constraints>",
                        exc,
                    )
                    raise

            if oracle_full_replace:
                self._source.validate_schema_replacement_objects()
                # Discover table types before applying the owner replacement:
                # the plan may drop target objects, so unsupported native
                # Oracle storage must fail before any destructive DDL.
                for object_name in objects:
                    preflight_schema = self._source.get_schema(object_name)
                    for column in preflight_schema.columns:
                        kind = str(column.source_type).upper()
                        if kind == "XMLTYPE":
                            from core.connectors.oracle.precision import (
                                oracle_xml_storage_supported,
                            )

                            metadata = preflight_schema.options.get(
                                "oracle_xml_storage", {}
                            ).get(column.name)
                            storage = " ".join(
                                str(metadata.get("storage_type", "")).upper().split()
                            ) if metadata else ""
                            if not oracle_xml_storage_supported(metadata):
                                mode = storage or "unreported storage mode"
                                raise RuntimeError(
                                    f"Oracle FULL migration cannot safely transport XMLTYPE "
                                    f"column {object_name}.{column.name} with {mode}; "
                                    "expected non-schema BINARY, TRANSPORTABLE BINARY, or CLOB "
                                    "metadata. Target replacement was not applied"
                                )
                replacement_inventory = self._oracle_replacement_inventory(objects)
                oracle_replacement_inventory = replacement_inventory
                from core.connectors.oracle.precision import oracle_full_unsupported_datatypes

                unsupported_columns = oracle_full_unsupported_datatypes(
                    replacement_inventory["schemas"]
                )
                if unsupported_columns:
                    blockers = [
                        f"Unsupported datatype {datatype} in {table}.{column}"
                        for table, column, datatype in unsupported_columns
                    ]
                    result["phases"]["oracle_source_datatype_preflight"] = {
                        "status": "blocked", "blockers": blockers,
                    }
                    for table, column, datatype in unsupported_columns:
                        self._record_object_failure(
                            object_failures,
                            all_errors,
                            "oracle_source_datatype_preflight",
                            table,
                            f"Unsupported datatype {datatype} in {table}.{column}",
                        )
                    raise RuntimeError(
                        "Oracle FULL source datatype preflight blocked: "
                        + "; ".join(blockers)
                    )
                result["phases"]["oracle_source_datatype_preflight"] = {
                    "status": "success",
                }
                self._source.validate_schema_replacement_grants()
                self._target.validate_all_constraint_states()
                # Complete every read-only replacement check before granting
                # privileges, so a blocked plan cannot cause avoidable grants.
                replacement_plan = self._target.plan_schema_replacement(
                    preserve_target_only=True, **replacement_inventory
                )
                if not replacement_plan.blockers and not replacement_plan.inspection_errors:
                    materialized_views = replacement_inventory["materialized_views"]
                    if materialized_views:
                        revalidate_mviews = getattr(
                            self._target, "revalidate_existing_materialized_views", None
                        )
                        if not callable(revalidate_mviews):
                            raise RuntimeError(
                                "Oracle target lacks safe materialized-view revalidation"
                            )
                        # This is non-refreshing validation. It runs before
                        # replacement cleanup so an unrecoverable matching MV
                        # cannot be discovered only after target data changes.
                        revalidate_mviews(materialized_views)
                        replacement_plan = self._target.plan_schema_replacement(
                            preserve_target_only=True, **replacement_inventory
                        )
                result["phases"]["oracle_schema_replacement_plan"] = replacement_plan.as_dict()
                if replacement_plan.blockers or replacement_plan.inspection_errors:
                    raise RuntimeError(
                        "Oracle schema replacement plan is not safe to apply: "
                        + "; ".join([
                            *replacement_plan.blockers,
                            *replacement_plan.inspection_errors,
                        ])
                    )
                from core.connectors.oracle.replacement import overwrite_authorization

                replacement_policy = self._config.get("migration", {}).get(
                    "oracle_schema_replacement", {}
                )
                replacement_policy = (
                    replacement_policy if isinstance(replacement_policy, dict) else {}
                )
                overwrite = overwrite_authorization(
                    replacement_plan,
                    source_inventory=replacement_inventory,
                    policy=replacement_policy,
                )
                result["phases"]["oracle_overwrite_authorization"] = overwrite
                if overwrite["status"] == "blocked":
                    raise RuntimeError(
                        "Oracle FULL target overwrite was stopped before DDL or data deletion: "
                        + str(overwrite["reason"])
                    )
                if oracle_permissions_enabled:
                    discover_permissions = getattr(
                        self._source, "discover_permission_inventory", None
                    )
                    preflight_permissions = getattr(
                        self._target, "preflight_permission_migration", None
                    )
                    if not callable(discover_permissions) or not callable(preflight_permissions):
                        raise RuntimeError(
                            "Oracle permission migration requires source discovery and target preflight"
                        )
                    permission_inventory = discover_permissions()
                    executor_connection = self._resolve_oracle_permission_executor_connection()
                    onboarding_admin_connection = self._resolve_oracle_permission_onboarding_admin()
                    source_scope_identity = getattr(
                        self._source,
                        "permission_scope_identity",
                        lambda: {
                            "user": str(self._source._config.get("username", "")),
                            "db_unique_name": str(self._source._config.get("host", "")),
                            "container_name": str(self._source._config.get("service_name", "")),
                            "service_name": str(self._source._config.get("service_name", "")),
                        },
                    )()
                    permission_preflight_kwargs = {
                        "executor_connection": executor_connection,
                        "onboarding_admin_connection": onboarding_admin_connection,
                        "permit_legacy_candidates": approve_legacy_permissions,
                        "source_scope_identity": source_scope_identity,
                        "source_owner": str(
                            self._config.get("source", {}).get("connection", {}).get("username", "")
                        ).strip(),
                    }
                    oracle_permission_plan = preflight_permissions(
                        oracle_permission_policy,
                        permission_inventory,
                        replacement_inventory,
                        **permission_preflight_kwargs,
                    )
                    legacy_candidates = oracle_permission_plan[
                        "permission_reconciliation"
                    ].get("adopted_legacy", [])
                    if legacy_candidates:
                        if not approve_legacy_permissions:
                            raise RuntimeError(
                                "Oracle legacy grant approval is required; rerun with "
                                "--oracle-permission-adopt-legacy to review the exact TARGET grants"
                            )
                        confirmation_prompt = getattr(self._status, "prompt", None)
                        if not callable(confirmation_prompt):
                            raise RuntimeError(
                                "Oracle legacy permission approval requires an interactive terminal reporter"
                            )
                        approval_details = json.dumps(
                            legacy_candidates, indent=2, sort_keys=True
                        )
                        confirmation = confirmation_prompt(
                            "Legacy Oracle TARGET permissions requiring one-time approval:\n"
                            f"{approval_details}\n"
                            f"Type APPROVE {len(legacy_candidates)} LEGACY PERMISSIONS "
                            "to record these exact grants in TARGET: "
                        )
                        if confirmation.strip() != f"APPROVE {len(legacy_candidates)} LEGACY PERMISSIONS":
                            raise RuntimeError(
                                "Legacy Oracle permission approval was not confirmed; no permission changes were made"
                            )
                        permission_preflight_kwargs["permit_legacy_candidates"] = False
                        permission_preflight_kwargs["approved_legacy_keys"] = {
                            oracle_permissions._permission_key(item)
                            for item in legacy_candidates
                        }
                        oracle_permission_plan = preflight_permissions(
                            oracle_permission_policy,
                            permission_inventory,
                            replacement_inventory,
                            **permission_preflight_kwargs,
                        )
                        legacy_candidates = oracle_permission_plan[
                            "permission_reconciliation"
                        ].get("adopted_legacy", [])
                        record_approvals = getattr(
                            self._target, "record_legacy_permission_approvals", None
                        )
                        if not callable(record_approvals):
                            raise RuntimeError(
                                "Oracle target lacks durable legacy permission approval support"
                            )
                        record_approvals(oracle_permission_plan)
                        ledger = oracle_permission_plan["permission_reconciliation"].setdefault(
                            "ledger", {}
                        )
                        for item in legacy_candidates:
                            ledger[oracle_permissions._permission_key(item)] = {
                                **item, "state": "APPROVED_LEGACY"
                            }
                        oracle_permission_plan["permission_reconciliation"][
                            "adopted_legacy"
                        ] = []
                    result["phases"]["oracle_permission_preflight"] = {
                        "status": "ready",
                        "mapped_users": len(oracle_permission_plan["policy"]["user_mappings"]),
                        "mapped_roles": len(oracle_permission_plan["roles"]),
                        "object_and_column_grants": len(oracle_permission_plan["object_grants"]),
                        "system_grants": len(oracle_permission_plan["system_grants"]),
                        "role_memberships": len(oracle_permission_plan["memberships"]),
                        "permissions_to_grant": oracle_permission_plan[
                            "permission_reconciliation"
                        ]["to_grant"],
                        "permissions_to_revoke": oracle_permission_plan[
                            "permission_reconciliation"
                        ]["to_revoke"],
                        "independent_target_permissions_preserved": oracle_permission_plan[
                            "permission_reconciliation"
                        ]["preserved_independent"],
                    }
                privilege_report = self._inspect_oracle_privileges(replacement_inventory)
                result["phases"]["oracle_privilege_preflight"] = privilege_report
                bootstrap_result = self._bootstrap_oracle_privileges(
                    replacement_inventory, privilege_report
                )
                result["phases"]["oracle_privilege_bootstrap"] = bootstrap_result
                result["phases"]["oracle_schema_replacement"] = self._target.apply_schema_replacement(
                    replacement_plan, preserve_target_only=True,
                    **replacement_inventory
                )

            # Identify partitioned tables to exclude from Phase 4 (created in Phase 4.5 instead)
            partitioned_table_keys: set[tuple[str, str]] = set()
            try:
                for pt in self._source.get_partitioned_tables():
                    schema_name = pt.schema_name or "dbo"
                    partitioned_table_keys.add((schema_name, pt.table_name))
            except AttributeError:
                pass

            # ---------- Phase 1: Extensions ----------
            ext_results: dict[str, str] = {}
            try:
                for ext in self._source.list_extensions():
                    try:
                        self._target.create_extension(ext)
                        ext_results[ext.name] = "created"
                    except Exception as exc:
                        ext_results[ext.name] = f"skipped: {exc}"
                        all_errors.append(f"extension {ext.name}: {exc}")
            except Exception as exc:
                ext_results["_error"] = str(exc)
                all_errors.append(f"extension discovery: {exc}")
            result["phases"]["extensions"] = ext_results
            self._update_status("extensions", 6, all_errors)

            # ---------- MySQL DEFINER account prerequisites ----------
            # Snapshot scheduled events before table/data work: MySQL flips a
            # preserved one-time event to DISABLED once it fires, so a late
            # rediscovery cannot represent migration-start state.
            source_events: list[Any] = []
            source_routines: list[Any] = []
            source_triggers: list[Any] = []
            mysql_user_results: dict[str, str] = {}
            mysql_definer_dependency = (
                self._config.get("source", {}).get("engine") == "mysql"
                and self._config.get("target", {}).get("engine") == "mysql"
            )
            if mysql_definer_dependency:
                from core.connectors.mysql import (
                    _mysql_definer_identity,
                    MySQLSecurityMetadataNotVisible,
                )

                try:
                    source_events = list(self._source.list_events())
                except Exception as exc:
                    mysql_user_results["_error"] = f"event discovery: {exc}"
                    all_errors.append(f"event discovery: {exc}")
                    source_events = []
                try:
                    source_routines = list(self._source.list_functions())
                except Exception as exc:
                    mysql_user_results["_error"] = f"routine discovery: {exc}"
                    all_errors.append(f"routine discovery: {exc}")
                    source_routines = []
                try:
                    source_triggers = list(self._source.get_all_triggers())
                except Exception as exc:
                    mysql_user_results["_error"] = f"trigger discovery: {exc}"
                    all_errors.append(f"trigger discovery: {exc}")
                    source_triggers = []

                # MySQL stored objects may refer to source accounts in their
                # DEFINER clause. Create those accounts before any dependent
                # routine, trigger, or event can create an orphan DEFINER ref.
                required_definers = {
                    identity
                    for identity in (
                        _mysql_definer_identity(getattr(obj, "ddl", ""))
                        for obj in [*source_routines, *source_triggers, *source_events]
                    )
                    if identity is not None
                }
                try:
                    discovered_users = self._source.list_users()
                except MySQLSecurityMetadataNotVisible as exc:
                    mysql_user_results["_error"] = str(exc)
                    all_errors.append(f"source account discovery: {exc}")
                    discovered_users = []
                except Exception as exc:
                    mysql_user_results["_error"] = str(exc)
                    all_errors.append(f"source account discovery: {exc}")
                    discovered_users = []
                for user in discovered_users:
                    host = getattr(user, "host", None)
                    if (user.name, host) not in required_definers:
                        continue
                    label = f"{user.name}@{host}" if host is not None else user.name
                    try:
                        if host is None:
                            self._target.create_user_if_not_exists(user.name)
                        else:
                            self._target.create_user_if_not_exists(user.name, host)
                        mysql_user_results[f"USER {label}"] = "created"
                    except Exception as exc:
                        mysql_user_results[f"USER {label}"] = f"failed: {exc}"
                        all_errors.append(f"CREATE USER {label}: {exc}")
                result["phases"]["security_users_pre_objects"] = mysql_user_results
                self._update_status("security_users_pre_objects", 4, all_errors)
            # ---------- Phase 2: Schemas ----------
            schema_results: dict[str, str] = {}
            try:
                for s in self._source.list_schemas():
                    try:
                        self._target.create_schema(s)
                        schema_results[s.name] = "created"
                    except Exception as exc:
                        schema_results[s.name] = f"skipped: {exc}"
                        all_errors.append(f"schema {s.name}: {exc}")
            except Exception as exc:
                schema_results["_error"] = str(exc)
                all_errors.append(f"schema discovery: {exc}")
            result["phases"]["schemas"] = schema_results
            self._update_status("schemas", 8, all_errors)

            # ---------- Phase 3: Custom Types ----------
            type_results: dict[str, str] = {}
            source_types: list[Any] = []
            try:
                source_types = list(self._source.list_types())
                for t in source_types:
                    try:
                        self._target.create_type(t)
                        type_results[t.name] = f"created ({t.kind})"
                    except Exception as exc:
                        type_results[t.name] = f"skipped: {exc}"
                        all_errors.append(f"type {t.name}: {exc}")
            except Exception as exc:
                type_results["_error"] = str(exc)
                all_errors.append(f"type discovery: {exc}")
            result["phases"]["custom_types"] = type_results
            self._update_status("custom_types", 10, all_errors)

            # ---------- Phase 3.5: Create Sequences (before tables need them) ----------
            seq_create_results: dict[str, str] = {}
            try:
                all_sequences = self._source.list_all_sequences()
                for seq in all_sequences:
                    try:
                        self._target.create_sequence(seq)
                        seq_create_results[seq.name] = "created"
                    except Exception as exc:
                        seq_create_results[seq.name] = f"skipped: {exc}"
                        if self._config.get("target", {}).get("engine") == "oracle":
                            oracle_object_preparation_complete = False
                            failed_objects.add(seq.name)
                            self._record_object_failure(
                                object_failures, all_errors, "create_sequence", seq.name, exc
                            )
                        else:
                            all_errors.append(f"sequence {seq.name}: {exc}")
            except AttributeError:
                # Non-PostgreSQL sources don't have list_all_sequences — skip silently
                pass
            except Exception as exc:
                seq_create_results["_error"] = str(exc)
                if self._config.get("target", {}).get("engine") == "oracle":
                    oracle_object_preparation_complete = False
                    failed_objects.add("<source sequences>")
                    self._record_object_failure(
                        object_failures, all_errors, "discover_sequences", "<source>", exc
                    )
                else:
                    all_errors.append(f"sequence discovery: {exc}")
            result["phases"]["create_sequences"] = seq_create_results
            self._update_status("create_sequences", 12, all_errors)

            # ---------- Phase 4: Create Tables ----------
            all_schemas: dict[str, Any] = {}
            schema_reconciliation_enabled = (
                (
                    self._config.get("migration", {}).get("reconcile_target_schema", False)
                    or oracle_full_replace
                )
                and self._config.get("target", {}).get("engine")
                in {"mysql", "postgresql", "oracle"}
            )
            mssql_full_reconciliation = (
                self._config.get("target", {}).get("engine") == "mssql"
            )
            postgres_source_views: list[Any] | None = None
            if (
                schema_reconciliation_enabled
                and self._config.get("target", {}).get("engine") == "postgresql"
            ):
                try:
                    postgres_source_views = list(self._source.list_views())
                except Exception:
                    # The normal views phase will report discovery errors. If
                    # type reconciliation needs a dependent view meanwhile,
                    # PostgreSQL's RESTRICT behavior safely blocks the ALTER.
                    postgres_source_views = None
            reconciliation_results: dict[str, Any] = {}
            for obj_name in objects:
                try:
                    schema = self._source.get_schema(obj_name)
                    schema_name = schema.schema_name or "dbo"
                    if (
                        self._config.get("source", {}).get("engine") == "oracle"
                        and self._config.get("target", {}).get("engine") != "oracle"
                    ):
                        if any(column.generated_kind == "VIRTUAL" for column in schema.columns):
                            raise RuntimeError(
                                f"Oracle virtual columns on {schema.name!r} are supported only "
                                "for Oracle targets"
                            )
                        from core.connectors.oracle.type_mapping import map_oracle_source_column

                        target_engine = self._config.get("target", {}).get("engine", "unknown")
                        for column in schema.columns:
                            column.target_type = map_oracle_source_column(
                                column, target_engine, schema.name
                            )
                            if target_engine == "mysql" and column.is_identity:
                                column.auto_increment = True
                    if self._config.get("source", {}).get("engine") == "oracle":
                        target_engine = self._config.get("target", {}).get("engine")
                        if target_engine == "oracle":
                            target_schema_name = str(
                                self._config.get("target", {}).get("connection", {}).get("username", "")
                            ).upper()
                        elif target_engine == "postgresql":
                            target_schema_name = "public"
                        elif target_engine == "mssql":
                            target_schema_name = "dbo"
                        elif target_engine == "mysql":
                            target_schema_name = str(
                                self._config.get("target", {}).get("connection", {}).get("database", "")
                            )
                        else:
                            target_schema_name = schema.schema_name
                        source_owner = schema.schema_name
                        schema.schema_name = target_schema_name
                        if target_engine == "oracle":
                            for metadata in schema.options.get("oracle_object_types", {}).values():
                                metadata["owner"] = target_schema_name
                        for foreign_key in schema.foreign_keys:
                            if str(foreign_key.ref_schema).casefold() == str(source_owner).casefold():
                                foreign_key.ref_schema = target_schema_name
                    if (schema_name, obj_name) in partitioned_table_keys:
                        # Partitioned tables are created in Phase 4.5 with partition scheme applied
                        all_schemas[obj_name] = schema
                        continue
                    self._apply_field_mappings(schema)
                    if mssql_full_reconciliation:
                        reconciliation_results[obj_name] = self._target.reconcile_mssql_table(
                            schema
                        )
                    elif schema_reconciliation_enabled:
                        target_engine = self._config.get("target", {}).get("engine")
                        if target_engine == "mysql":
                            outcome = self._target.reconcile_mysql_table(schema, set(objects))
                        elif target_engine == "oracle":
                            outcome = self._target.reconcile_oracle_table(schema)
                        else:
                            outcome = self._target.reconcile_postgresql_table(
                                schema, postgres_source_views or []
                            )
                        reconciliation_results[obj_name] = outcome
                    else:
                        self._target.create_object_if_missing(schema)
                    all_schemas[obj_name] = schema
                except Exception as exc:
                    failed_objects.add(obj_name)
                    message = self._record_object_failure(
                        object_failures, all_errors, "create_table", obj_name, exc
                    )
                    result["phases"][obj_name] = {
                        "status": "creation_failed",
                        "success": 0,
                        "failure": 1,
                        "errors": [message],
                    }
                    if self._stop_on_error:
                        raise RuntimeError(message) from exc
            result["phases"]["create_tables"] = (
                "partial_success" if failed_objects else "success"
            )
            if schema_reconciliation_enabled:
                result["phases"]["schema_reconciliation"] = reconciliation_results
            self._update_status("create_tables", 16, all_errors)

            # ---------- Phase 4.5: Create Partition Children ----------
            partition_results: dict[str, str] = {}
            partitions: list[Any] = []
            partition_inventory_loaded = False
            partition_source_metadata: dict[str, Any] | None = None
            partition_reconcile_errors: dict[str, str] = {}
            partition_definition_validation_passed = True
            partition_reconciliation_enabled = bool(
                self._config.get("migration", {}).get("reconcile_mssql_partition_functions", False)
            )
            try:
                partitions = self._source.list_partitions()
                partition_inventory_loaded = True
                for part in partitions:
                    try:
                        self._target.create_partition(part)
                        partition_results[part.name] = f"created (parent: {part.parent_table})"
                    except Exception as exc:
                        partition_results[part.name] = f"skipped: {exc}"
            except AttributeError:
                try:
                    for pf in self._source.list_partition_functions():
                        try:
                            self._target.create_partition_function(pf)
                            partition_results[pf.name] = f"partition function created"
                        except Exception as exc:
                            if (partition_reconciliation_enabled
                                    and isinstance(exc, PartitionDefinitionMismatch)
                                    and hasattr(self._source, "inspect_partition_metadata")
                                    and hasattr(self._target, "reconcile_partition_function")):
                                try:
                                    if partition_source_metadata is None:
                                        partition_source_metadata = self._source.inspect_partition_metadata()
                                    self._target.reconcile_partition_function(pf, partition_source_metadata)
                                    partition_results[pf.name] = "reconciled"
                                except Exception as reconcile_exc:
                                    partition_reconcile_errors[pf.name] = str(reconcile_exc)
                                    partition_results[pf.name] = f"reconciliation_failed: {reconcile_exc}"
                            else:
                                partition_results[pf.name] = f"mismatch_pending_validation: {exc}"
                    for ps in self._source.list_partition_schemes():
                        if ps.partition_function_name in partition_reconcile_errors:
                            partition_results[ps.name] = {
                                "status": "blocked",
                                "reason": (
                                    f"partition function {ps.partition_function_name} reconciliation failed; "
                                    "the dependent scheme was preserved"
                                ),
                            }
                            continue
                        try:
                            self._target.create_partition_scheme(ps)
                            partition_results[ps.name] = f"partition scheme created"
                        except Exception as exc:
                            if (partition_reconciliation_enabled
                                    and isinstance(exc, PartitionDefinitionMismatch)
                                    and hasattr(self._source, "inspect_partition_metadata")
                                    and hasattr(self._target, "reconcile_partition_scheme")):
                                try:
                                    if partition_source_metadata is None:
                                        partition_source_metadata = self._source.inspect_partition_metadata()
                                    self._target.reconcile_partition_scheme(ps, partition_source_metadata)
                                    # Re-run the ordinary create/compare path so the
                                    # reconciled scheme is accepted only if it now
                                    # exactly matches source catalog metadata.
                                    self._target.create_partition_scheme(ps)
                                    partition_results[ps.name] = "reconciled"
                                    continue
                                except Exception as reconcile_exc:
                                    partition_results[ps.name] = f"reconciliation_failed: {reconcile_exc}"
                                    partition_reconcile_errors[ps.name] = str(reconcile_exc)
                                    continue
                            partition_results[ps.name] = f"skipped: {exc}"
                            partition_reconcile_errors[ps.name] = str(exc)
                    for pt in self._source.get_partitioned_tables():
                        try:
                            schema = self._source.get_schema(pt.table_name)
                            self._target.create_partitioned_table(schema, pt.partition_scheme_name, pt.partition_column)
                            partition_results[pt.table_name] = f"partitioned table created"
                        except Exception as exc:
                            partition_results[pt.table_name] = f"skipped: {exc}"
                            partition_reconcile_errors[pt.table_name] = str(exc)
                except AttributeError:
                    pass
            except Exception as exc:
                partition_results["_error"] = str(exc)
            if (
                schema_reconciliation_enabled
                and self._config.get("target", {}).get("engine") == "postgresql"
                and partition_inventory_loaded
                and hasattr(self._target, "reconcile_partitions")
            ):
                try:
                    removed_partitions = self._target.reconcile_partitions(
                        partitions,
                        list(all_schemas.values()),
                    )
                    partition_results["_reconciled"] = {
                        "removed": removed_partitions,
                    }
                except Exception as exc:
                    partition_results["_reconcile_error"] = str(exc)
            result["phases"]["create_partitions"] = partition_results
            if (
                self._config.get("source", {}).get("engine") == "mssql"
                and self._config.get("target", {}).get("engine") == "mssql"
                and hasattr(self._source, "inspect_partition_metadata")
                and hasattr(self._target, "inspect_partition_metadata")
            ):
                try:
                    if partition_source_metadata is None:
                        partition_source_metadata = self._source.inspect_partition_metadata()
                    target_partition_metadata = self._target.inspect_partition_metadata()
                    comparison = _mssql_partition_comparison(
                        partition_source_metadata, target_partition_metadata
                    )
                    result["phases"]["partition_definition_validation"] = {
                        "comparison_available": True,
                        **comparison,
                    }
                    audit_log(
                        phase="partition_definition_validation",
                        status="matches" if comparison["definition_matches"] else "mismatch",
                        details={"definition_matches": comparison["definition_matches"],
                                 "mismatches": comparison["mismatches"]},
                    )
                    if not comparison["definition_matches"]:
                        partition_definition_validation_passed = False
                        failed_mismatch_objects: set[str] = set()
                        function_mismatches = {
                            str(item["object"]) for item in comparison["mismatches"]
                            if item["kind"] == "partition_function"
                        }
                        for mismatch in comparison["mismatches"]:
                            object_name = str(mismatch["object"])
                            source_definition = mismatch.get("source") or {}
                            related_function = (
                                source_definition.get("partition_function_name")
                                if mismatch["kind"] == "partition_scheme" else None
                            )
                            duplicate_scheme_mismatch = (
                                mismatch["kind"] == "partition_scheme"
                                and related_function in function_mismatches
                            )
                            reconciliation_error = partition_reconcile_errors.get(
                                str(mismatch.get("table_name") or object_name)
                            ) or partition_reconcile_errors.get(object_name)
                            if reconciliation_error:
                                mismatch["reconciliation_error"] = reconciliation_error
                                mismatch["reason"] = (
                                    f"{mismatch['reason']}; safe reconciliation was rejected: "
                                    f"{reconciliation_error}"
                                )
                            elif not partition_reconciliation_enabled:
                                mismatch["reason"] = (
                                    f"{mismatch['reason']}; automatic reconciliation is disabled. "
                                    "Target objects and data were preserved. Enable "
                                    "migration.reconcile_mssql_partition_functions only for the "
                                    "supported empty trailing DATE partition case, or reconcile manually."
                                )
                            reason = str(mismatch["reason"]).rstrip(". ")
                            message = (
                                f"Partition metadata mismatch for {object_name}: {reason}. "
                                "Target partition objects and data were preserved."
                            )
                            if not duplicate_scheme_mismatch:
                                self._record_object_failure(
                                    result["phases"]["object_failures"], all_errors,
                                    "partition_definition_validation", object_name, message,
                                )
                            failed_mismatch_objects.add(object_name)
                            failed_objects.add(str(mismatch.get("table_name") or object_name))
                            partition_results[f"{mismatch['kind']}:{object_name}"] = {
                                "status": "mismatch",
                                "definition_matches": False,
                                "source": mismatch["source"],
                                "target": mismatch["target"],
                                "reason": message,
                            }
                        if self._stop_on_error:
                            raise _PartitionDefinitionError(
                                "Partition definition validation failed: "
                                + ", ".join(sorted(failed_mismatch_objects))
                            )
                except _PartitionDefinitionError:
                    raise
                except Exception as exc:
                    partition_definition_validation_passed = False
                    message = f"Partition metadata comparison unavailable: {exc}"
                    result["phases"]["partition_definition_validation"] = {
                        "comparison_available": False,
                        "definition_matches": None,
                        "error": str(exc),
                    }
                    audit_log(
                        phase="partition_definition_validation", status="unavailable",
                        details={"comparison_available": False, "definition_matches": None,
                                 "error": str(exc)},
                    )
                    self._record_object_failure(
                        result["phases"]["object_failures"], all_errors,
                        "partition_definition_validation", "<partition metadata>", message,
                    )
                    if self._stop_on_error:
                        raise _PartitionDefinitionError(message) from exc
            self._update_status("create_partitions", 17, all_errors)

            # Oracle source objects are applied before FULL clearing/loading so
            # definition, dependency, privilege, and compilation failures fail
            # closed behind the existing schema-preparation guard.
            oracle_full_sync = self._config.get("target", {}).get("engine") == "oracle"
            if oracle_full_sync:
                oracle_full_objects = {
                    "sequences": [], "views": [], "materialized_views": [],
                    "routines": [], "triggers": [], "synonyms": [], "comments": [],
                }
                try:
                    if oracle_replacement_inventory is not None:
                        for object_group in (
                            "sequences", "views", "materialized_views", "routines",
                            "triggers", "synonyms", "comments",
                        ):
                            oracle_full_objects[object_group] = list(
                                oracle_replacement_inventory[object_group]
                            )
                    else:
                        oracle_full_objects["sequences"] = list(
                            getattr(self._source, "list_all_sequences", lambda: [])()
                        )
                        oracle_full_objects["views"] = list(self._source.list_views())
                        oracle_full_objects["materialized_views"] = list(self._source.list_materialized_views())
                        oracle_full_objects["routines"] = list(self._source.list_functions())
                        oracle_full_objects["types"] = list(self._source.list_types())
                        oracle_full_objects["triggers"] = list(self._source.get_all_triggers())
                        oracle_full_objects["synonyms"] = list(self._source.list_synonyms())
                        oracle_full_objects["comments"] = list(self._source.list_comments())
                except Exception as exc:
                    oracle_object_preparation_complete = False
                    self._record_object_failure(
                        object_failures, all_errors, "discover_oracle_objects", "<source>", exc
                    )
                    result["phases"]["oracle_object_preparation"] = {
                        "status": "failed", "error": str(exc),
                    }
                if oracle_object_preparation_complete and (
                    failed_objects or set(all_schemas) != set(objects)
                    or not partition_definition_validation_passed
                ):
                    oracle_object_preparation_complete = False
                    result["phases"]["oracle_object_preparation"] = {
                        "status": "skipped",
                        "reason": "Oracle table preparation failed; object DDL and target data remain protected.",
                    }
                if oracle_object_preparation_complete:
                    try:
                        if any(oracle_full_objects.values()):
                            set_managed_objects = getattr(self._target, "set_managed_source_objects", None)
                            validate_privileges = getattr(self._target, "validate_object_privileges", None)
                            if not callable(set_managed_objects) or not callable(validate_privileges):
                                raise RuntimeError(
                                    "Oracle target lacks managed-object dependency or privilege preflight"
                                )
                            set_managed_objects(oracle_full_objects)
                            validate_privileges(
                                tables=set(all_schemas),
                                views=oracle_full_objects["views"],
                                materialized_views=oracle_full_objects["materialized_views"],
                                sequences=oracle_full_objects["sequences"],
                                routines=oracle_full_objects["routines"],
                                types=oracle_full_objects.get("types", []),
                                triggers=oracle_full_objects["triggers"],
                                synonyms=oracle_full_objects["synonyms"],
                            )
                    except Exception as exc:
                        oracle_object_preparation_complete = False
                        self._record_object_failure(
                            object_failures, all_errors, "oracle_object_preflight", "<target owner>", exc
                        )
                if oracle_object_preparation_complete:
                    object_groups = (
                        ("synonyms", "prepare_synonym", oracle_full_objects["synonyms"], self._target.create_synonym),
                        ("views", "prepare_view", oracle_full_objects["views"], self._target.create_view),
                        ("routines", "prepare_routine", oracle_full_objects["routines"], self._target.create_function),
                        ("triggers", "prepare_trigger", oracle_full_objects["triggers"], self._target.create_trigger),
                    )
                    for group, phase, definitions, apply_object in object_groups:
                        for definition in definitions:
                            if group == "triggers":
                                object_key = f"{definition.schema_name}.{definition.table}.{definition.name}"
                            elif group == "synonyms":
                                object_key = f"{definition.schema_name}.{definition.name}"
                            elif group == "routines":
                                object_key = f"{definition.schema_name}.{definition.name}:{definition.kind}"
                            else:
                                object_key = f"{definition.schema_name}.{definition.name}"
                            try:
                                applied = apply_object(definition)
                                oracle_object_phase_results[group][object_key] = (
                                    "deferred_until_dependencies_exist"
                                    if group == "synonyms" and applied is False
                                    else "created"
                                )
                            except Exception as exc:
                                oracle_object_phase_results[group][object_key] = f"error: {exc}"
                                oracle_object_preparation_complete = False
                                failed_objects.add(object_key)
                                self._record_object_failure(
                                    object_failures, all_errors, phase, object_key, exc
                                )
                    if oracle_object_preparation_complete:
                        for mv in oracle_full_objects["materialized_views"]:
                            try:
                                self._target.create_materialized_view(mv)
                                oracle_object_phase_results["materialized_views"][
                                    f"{mv.schema_name}.{mv.name}"
                                ] = "created_or_revalidated"
                            except Exception as exc:
                                object_key = f"{mv.schema_name}.{mv.name}"
                                oracle_object_phase_results["materialized_views"][object_key] = (
                                    f"error: {exc}"
                                )
                                oracle_object_preparation_complete = False
                                failed_objects.add(object_key)
                                self._record_object_failure(
                                    object_failures, all_errors,
                                    "prepare_materialized_view", object_key, exc,
                                )
                if "oracle_object_preparation" not in result["phases"]:
                    result["phases"]["oracle_object_preparation"] = {
                        "status": "success" if oracle_object_preparation_complete else "failed",
                        "views": len(oracle_full_objects["views"]),
                        "sequences": len(oracle_full_objects["sequences"]),
                        "routines_and_packages": len(oracle_full_objects["routines"]),
                        "triggers": len(oracle_full_objects["triggers"]),
                        "materialized_views": len(oracle_full_objects["materialized_views"]),
                    }

            # ---------- Phase 5: Migrate Data ----------
            # Fresh targets have no triggers yet (they are created later in
            # Phase 15).  On a rerun the target triggers would otherwise record
            # migration DML as application activity.  Suspension is connector
            # specific and limited to the source trigger definitions.
            if not source_triggers:
                source_triggers = (
                    oracle_full_objects["triggers"]
                    if oracle_full_objects is not None
                    else list(self._source.get_all_triggers())
                )
            suspended_triggers = self._target.suspend_triggers_for_data_load(source_triggers)
            result["phases"]["trigger_data_load_handling"] = {
                "suspended": [t.name for t in suspended_triggers],
                "strategy": "disable-and-restore-after-data-load"
                if suspended_triggers
                else "fresh-target-no-triggers",
            }
            # Full mode is replacement synchronisation, not an incremental
            # upsert.  Clear every migrated target table before loading so
            # target-only rows (including rows from past trigger side effects)
            # cannot survive a successful run.  Connectors that do not define
            # replacement semantics retain the historical upsert-only behaviour.
            # Order and validate dependencies before clearing target data.
            data_load_order = order_data_load_objects(all_schemas, objects)
            result["phases"]["data_load_order"] = data_load_order
            data_dependencies, _, _ = build_dependency_graph(all_schemas, objects)
            schema_map = {name: (s.schema_name if hasattr(s, "schema_name") else None) for name, s in all_schemas.items()}
            schema_preparation_complete = (
                not (mssql_full_reconciliation or oracle_full_sync)
                or (
                    not failed_objects
                    and set(all_schemas) == set(objects)
                    and partition_definition_validation_passed
                    and oracle_object_preparation_complete
                )
            )
            if schema_preparation_complete:
                cleared_objects = self._target.clear_objects_for_full_sync(list(all_schemas))
                result["phases"]["full_target_sync"] = {
                    "strategy": "clear-before-load",
                    "status": "success",
                    "cleared": cleared_objects,
                }
            else:
                # FULL sync must never delete a parent while one of its
                # source-managed child tables failed schema preparation and
                # is therefore absent from the connector's managed set. Keep
                # the existing target data intact and report the incomplete
                # precondition explicitly instead of attempting a partial
                # replacement.
                missing_schemas = [name for name in objects if name not in all_schemas]
                result["phases"]["full_target_sync"] = {
                    "strategy": "clear-before-load",
                    "status": "skipped",
                    "cleared": [],
                    "reason": (
                        "Source table schema preparation was incomplete; "
                        "FULL replacement was skipped to preserve target data."
                    ),
                    "missing_schemas": missing_schemas,
                }
            if (
                schema_reconciliation_enabled
                and self._config.get("target", {}).get("engine") == "postgresql"
            ):
                try:
                    result["phases"]["enum_reconciliation"] = (
                        self._target.reconcile_postgresql_types(
                            source_types,
                            list(all_schemas.values()),
                            postgres_source_views or [],
                        )
                    )
                except Exception as exc:
                    result["phases"]["enum_reconciliation"] = {"error": str(exc)}
                    all_errors.append(f"ENUM reconciliation: {exc}")
            total_rows = self._estimate_total_rows(data_load_order, schema_map)
            processed_rows = 0
            data_load_statuses: dict[str, dict[str, str]] = {}

            def record_data_load_status(
                object_name: str,
                status: str,
                reason: str,
                *,
                source_rows: int | None = None,
                success: int = 0,
                failure: int = 0,
                errors: list[str] | None = None,
            ) -> None:
                status_entry = {"status": status, "reason": reason}
                data_load_statuses[object_name] = status_entry
                result["phases"][object_name] = {
                    "status": status,
                    "reason": reason,
                    "source_rows": source_rows,
                    "success": success,
                    "failure": failure,
                    "errors": list(errors or []),
                }

            def finalize_data_load(status: str | None = None) -> None:
                counts = {
                    name: sum(item["status"] == name for item in data_load_statuses.values())
                    for name in ("SUCCESS", "FAILED", "SKIPPED")
                }
                final_status = status
                if final_status is None:
                    if counts["FAILED"] or counts["SKIPPED"]:
                        final_status = "PARTIAL_SUCCESS"
                    elif counts["SUCCESS"]:
                        final_status = "SUCCESS"
                    else:
                        final_status = "SKIPPED"
                result["phases"]["data"] = {
                    "status": final_status,
                    "counts": counts,
                    "tables": data_load_statuses,
                }
                # Keep the historical phase key available to existing report
                # consumers while the registered "data" phase carries the
                # richer per-table outcome payload.
                result["phases"]["data_load"] = {
                    "status": final_status.lower(),
                    "counts": counts,
                    "tables": data_load_statuses,
                }

            def dependency_skip_reason(object_name: str) -> str | None:
                blocked_by = []
                for parent in sorted(data_dependencies.get(object_name, ())):
                    parent_status = data_load_statuses.get(parent)
                    if parent_status and parent_status["status"] in {"FAILED", "SKIPPED"}:
                        blocked_by.append(
                            f"{parent} ({parent_status['status']}: {parent_status['reason']})"
                        )
                if not blocked_by:
                    return None
                return (
                    "Skipped because FK prerequisite(s) were not loaded: "
                    + "; ".join(blocked_by)
                )

            if not schema_preparation_complete:
                preparation_reason = result["phases"]["full_target_sync"]["reason"]
                for object_name in data_load_order:
                    existing = result["phases"].get(object_name, {})
                    if not isinstance(existing, dict):
                        existing = {}
                    if object_name in failed_objects:
                        existing_reason = "; ".join(existing.get("errors", []))
                        reason = existing_reason or "Table preparation failed before data loading."
                        data_load_statuses[object_name] = {
                            "status": "FAILED", "reason": reason,
                        }
                    else:
                        data_load_statuses[object_name] = {
                            "status": "SKIPPED", "reason": preparation_reason,
                        }
                finalize_data_load("SKIPPED")
                result["phases"]["data"]["reason"] = preparation_reason
                result["phases"]["data_load"]["reason"] = preparation_reason

            for idx, obj_name in enumerate(
                data_load_order if schema_preparation_complete else (), start=1
            ):
                blocked_reason = dependency_skip_reason(obj_name)
                if blocked_reason:
                    record_data_load_status(obj_name, "SKIPPED", blocked_reason)
                    self._update_status(
                        f"data skipped: {obj_name}",
                        min(55, int(15 + (idx / max(len(data_load_order), 1)) * 40)),
                        all_errors,
                    )
                    continue

                schema = all_schemas[obj_name]
                upsert_result = UpsertResult()
                count: int | None = None
                failure_reason: str | None = None
                stop_exception: BaseException | None = None
                data_operation_connector = "Oracle source"
                try:
                    count = self._source.get_object_count(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None)
                    rows = self._source.export_full(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None)
                    for chunk in self._chunked(rows, self._batch_size):
                        data_operation_connector = "Oracle target"
                        chunk_result = self._target.upsert_batch(obj_name, iter(chunk), schema)
                        upsert_result.success_count += chunk_result.success_count
                        upsert_result.failure_count += chunk_result.failure_count
                        upsert_result.errors.extend(chunk_result.errors)
                        upsert_result.failed_items.extend(chunk_result.failed_items)
                        processed_rows += chunk_result.success_count
                        if total_rows > 0:
                            progress = min(55, int(15 + (processed_rows / total_rows) * 40))
                        else:
                            progress = 15 + int((idx / max(len(objects), 1)) * 40)
                        self._update_status(
                            f"data: {obj_name} ({idx}/{len(all_schemas)})", progress, all_errors,
                        )
                        # The next iteration of the lazy SOURCE iterator happens
                        # before the loop body runs again.
                        data_operation_connector = "Oracle source"

                    if upsert_result.failure_count:
                        data_operation_connector = "Oracle target"
                        details = "; ".join(upsert_result.errors) or (
                            f"target reported {upsert_result.failure_count} failed row(s)"
                        )
                        message = self._record_object_failure(
                            object_failures, all_errors, "data_load", obj_name, details,
                            connector=(
                                "Oracle target"
                                if self._config.get("target", {}).get("engine") == "oracle"
                                else None
                            ),
                        )
                        failed_objects.add(obj_name)
                        upsert_result.errors.append(message)
                        if self._stop_on_error:
                            failure_reason = message
                            stop_exception = RuntimeError(message)
                            run_full_failure_connector = (
                                data_operation_connector
                                if self._config.get("target", {}).get("engine") == "oracle"
                                else None
                            )
                        else:
                            failure_reason = message
                except Exception as exc:
                    if failure_reason is None:
                        failure_reason = self._record_object_failure(
                            object_failures, all_errors, "data_load", obj_name, exc,
                            connector=(
                                data_operation_connector
                                if self._config.get("target", {}).get("engine") == "oracle"
                                else None
                            ),
                        )
                        failed_objects.add(obj_name)
                        upsert_result.errors.append(failure_reason)
                    if self._stop_on_error:
                        stop_exception = exc
                        run_full_failure_connector = (
                            data_operation_connector
                            if self._config.get("target", {}).get("engine") == "oracle"
                            else None
                        )

                if failure_reason:
                    record_data_load_status(
                        obj_name,
                        "FAILED",
                        failure_reason,
                        source_rows=count or 0,
                        success=upsert_result.success_count,
                        failure=max(upsert_result.failure_count, 1),
                        errors=upsert_result.errors,
                    )
                    if self._stop_on_error:
                        for pending_name in data_load_order[idx:]:
                            dependency_reason = dependency_skip_reason(pending_name)
                            reason = (
                                f"Not attempted because migration.stop_on_error=true after "
                                f"{obj_name} failed data loading: {failure_reason}"
                            )
                            if dependency_reason:
                                reason += f"; {dependency_reason}"
                            record_data_load_status(pending_name, "SKIPPED", reason)
                        finalize_data_load("FAILED")
                        raise stop_exception or RuntimeError(failure_reason)
                else:
                    record_data_load_status(
                        obj_name,
                        "SUCCESS",
                        f"Loaded {upsert_result.success_count} target row(s) successfully.",
                        source_rows=count or 0,
                        success=upsert_result.success_count,
                        failure=0,
                        errors=upsert_result.errors,
                    )
            if schema_preparation_complete:
                finalize_data_load()

            # ---------- Phase 6+7+8: Indexes, FKs, Check Constraints ----------
            constraint_results: dict[str, str] = {}
            if oracle_full_sync and not schema_preparation_complete:
                constraint_results = {
                    obj_name: "skipped: Oracle schema preparation incomplete"
                    for obj_name in all_schemas
                }
            else:
                for obj_name, schema in all_schemas.items():
                    if oracle_target_unavailable:
                        constraint_results[obj_name] = (
                            "skipped: target Oracle connection unavailable"
                        )
                        continue
                    try:
                        constraint_errors = self._target.apply_constraints(schema)
                        if isinstance(constraint_errors, list) and constraint_errors:
                            constraint_results[obj_name] = "error: " + "; ".join(constraint_errors)
                            for error in constraint_errors:
                                self._record_object_failure(
                                    object_failures, all_errors, "apply_constraints",
                                    obj_name, error,
                                )
                            failed_objects.add(obj_name)
                        else:
                            constraint_results[obj_name] = "success"
                    except Exception as exc:
                        constraint_results[obj_name] = f"error: {exc}"
                        if self._config.get("target", {}).get("engine") == "oracle":
                            self._record_object_failure(
                                object_failures, all_errors, "apply_constraints",
                                obj_name, exc,
                            )
                            failed_objects.add(obj_name)
                            from core.connectors.oracle._models import (
                                is_oracle_connection_unavailable,
                            )

                            if is_oracle_connection_unavailable(exc):
                                oracle_target_unavailable = True
                                result["phases"]["apply_constraints"] = constraint_results
                                raise
                        else:
                            all_errors.append(str(exc))
            result["phases"]["apply_constraints"] = constraint_results
            self._update_status("apply_constraints", 60, all_errors)

            # ---------- MySQL AUTO_INCREMENT synchronisation ----------
            # MySQL AUTO_INCREMENT is table-bound rather than a standalone
            # sequence, so advance it once explicit source IDs have been loaded.
            # Only engines that populate Column.auto_increment reach this path;
            # every other connector leaves the flag False.
            auto_increment_results: dict[str, str] = {}
            for obj_name, schema in all_schemas.items():
                for column in schema.columns:
                    is_oracle_identity = (
                        self._config.get("target", {}).get("engine") == "oracle"
                        and getattr(column, "is_identity", False)
                    )
                    if not getattr(column, "auto_increment", False) and not is_oracle_identity:
                        continue
                    try:
                        self._target.sync_auto_increment(obj_name, column.name)
                        auto_increment_results[f"{obj_name}.{column.name}"] = "synchronized"
                    except Exception as exc:
                        auto_increment_results[f"{obj_name}.{column.name}"] = f"error: {exc}"
                        all_errors.append(str(exc))
            if auto_increment_results:
                result["phases"]["auto_increment"] = auto_increment_results

            # ---------- Phase 9: Sequence Ownership ----------
            seq_owner_results: dict[str, str] = {}
            try:
                all_sequences = self._source.list_all_sequences() if hasattr(self._source, "list_all_sequences") else []
                for seq in all_sequences:
                    if seq.owned_by or self._config.get("target", {}).get("engine") == "postgresql":
                        try:
                            self._target.apply_sequence_ownership(seq)
                            seq_owner_results[seq.name] = (
                                f"owned: {seq.owned_by}" if seq.owned_by else "unowned"
                            )
                        except Exception as exc:
                            seq_owner_results[seq.name] = f"skipped: {exc}"
            except Exception as exc:
                seq_owner_results["_error"] = str(exc)
            result["phases"]["apply_sequence_ownership"] = seq_owner_results
            self._update_status("apply_sequence_ownership", 65, all_errors)

            # ---------- Phase 10: Row-Level Security ----------
            rls_results: dict[str, Any] = {}
            try:
                for obj_name, schema in all_schemas.items():
                    if schema.rls_enabled:
                        rls_results[obj_name] = []
                        for policy in self._source.get_rls_policies(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None):
                            try:
                                self._target.apply_rls_policy(policy)
                                rls_results[obj_name].append(f"{policy.name}: created")
                            except Exception as exc:
                                rls_results[obj_name].append(f"{policy.name}: skipped ({exc})")
            except Exception as exc:
                rls_results["_error"] = str(exc)
            result["phases"]["row_level_security"] = rls_results
            self._update_status("row_level_security", 63, all_errors)

            # ---------- Phase 10: Advance Sequences (set to max(col)+1 after data load) ----------
            seq_advance_results: dict[str, list[str]] = {}
            try:
                all_sequences = self._source.list_all_sequences() if hasattr(self._source, "list_all_sequences") else []
                for seq in all_sequences:
                    if seq.owned_by:
                        # owned_by may be either "table.column" (legacy) or
                        # "schema.table.column" (now produced by the source
                        # connector for non-public schemas).
                        parts = seq.owned_by.rsplit(".", 2)
                        if len(parts) == 3:
                            seq_schema, tbl, col = parts
                        elif len(parts) == 2:
                            seq_schema, tbl, col = "public", parts[0], parts[1]
                        else:
                            continue
                        if tbl not in seq_advance_results:
                            seq_advance_results[tbl] = []
                        seq_qname = seq.name if seq_schema == "public" else f"{seq_schema}.{seq.name}"
                        try:
                            self._target.advance_sequence(seq_qname, tbl, col, seq.owned_by)
                            seq_advance_results[tbl].append(f"{seq_qname}: advanced")
                        except Exception as exc:
                            seq_advance_results[tbl].append(f"{seq_qname}: skipped ({exc})")
                            if self._config.get("target", {}).get("engine") == "oracle":
                                all_errors.append(f"sequence {seq_qname} state synchronization: {exc}")
                    elif self._config.get("target", {}).get("engine") in {"postgresql", "oracle"}:
                        try:
                            self._target.restore_standalone_sequence_state(seq)
                            seq_advance_results[seq.name] = ["standalone state restored"]
                        except Exception as exc:
                            seq_advance_results[seq.name] = [f"standalone state skipped ({exc})"]
                            if self._config.get("target", {}).get("engine") == "oracle":
                                all_errors.append(f"sequence {seq.name} state synchronization: {exc}")
            except Exception as exc:
                seq_advance_results["_error"] = [str(exc)]
            result["phases"]["advance_sequences"] = seq_advance_results
            self._update_status("advance_sequences", 66, all_errors)

            # ---------- Phase 11: Views ----------
            view_results: dict[str, str] = {}
            try:
                views_to_migrate = (
                    postgres_source_views
                    if postgres_source_views is not None
                    else oracle_full_objects["views"]
                    if oracle_full_objects is not None
                    else self._source.list_views()
                )
                if oracle_full_objects is not None:
                    view_results = {
                        view.name: oracle_object_phase_results["views"].get(
                            f"{view.schema_name}.{view.name}", "skipped: preflight failed"
                        )
                        for view in views_to_migrate
                    }
                    result["phases"]["views"] = view_results
                    self._update_status("views", 70, all_errors)
                    views_to_migrate = []
                for view in views_to_migrate:
                    try:
                        self._target.create_view(view)
                        view_results[view.name] = "created"
                    except Exception as exc:
                        view_results[view.name] = f"error: {exc}"
                        all_errors.append(str(exc))
                        # Track the failure so the run status cannot report
                        # success while the object is missing on the target.
                        failed_objects.add(view.name)
            except Exception as exc:
                view_results["_error"] = str(exc)
            result["phases"]["views"] = view_results
            self._update_status("views", 70, all_errors)

            # ---------- Phase 12: Materialized Views ----------
            mv_results: dict[str, str] = {}
            try:
                materialized_views = (
                    oracle_full_objects["materialized_views"]
                    if oracle_full_objects is not None else self._source.list_materialized_views()
                )
                for mv in materialized_views:
                    try:
                        if self._config.get("target", {}).get("engine") == "postgresql":
                            self._target.reconcile_materialized_view(mv)
                        elif oracle_full_objects is None:
                            self._target.create_materialized_view(mv)
                        self._target.refresh_materialized_view(mv.name, schema_name=mv.schema_name)
                        if self._config.get("target", {}).get("engine") == "oracle":
                            source_rows = self._source.materialized_view_query_rows(mv)
                            target_rows = self._target.materialized_view_rows(mv.name)
                            if sorted(map(repr, source_rows)) != sorted(map(repr, target_rows)):
                                raise RuntimeError(
                                    f"Oracle materialized view {mv.name} stored results do not match its source query"
                                )
                        mv_results[mv.name] = "created+refreshed"
                    except Exception as exc:
                        mv_results[mv.name] = f"error: {exc}"
                        failed_objects.add(mv.name)
                        all_errors.append(str(exc))
            except Exception as exc:
                mv_results["_error"] = str(exc)
            result["phases"]["materialized_views"] = mv_results
            self._update_status("materialized_views", 75, all_errors)

            # ---------- Phase 13: Functions & Stored Procedures ----------
            func_results: dict[str, str] = {}
            try:
                routines_to_migrate = (
                    oracle_full_objects["routines"]
                    if oracle_full_objects is not None
                    else self._source.list_functions()
                )
                for func in routines_to_migrate:
                    func_key = (
                        func.name if func.schema_name == "public"
                        else f"{func.schema_name}.{func.name}"
                    )
                    oracle_func_key = f"{func.schema_name}.{func.name}:{func.kind}"
                    if oracle_full_objects is not None:
                        func_results[oracle_func_key] = oracle_object_phase_results["routines"].get(
                            oracle_func_key, "skipped: preflight failed"
                        )
                        continue
                    try:
                        self._target.create_function(func)
                        func_results[func_key] = "created"
                    except Exception as exc:
                        func_results[func_key] = f"skipped: {exc}"
                        self._record_object_failure(
                            object_failures, all_errors, "create_function", func_key, exc
                        )
                        failed_objects.add(func_key)
            except Exception as exc:
                func_results["_error"] = str(exc)
                self._record_object_failure(
                    object_failures, all_errors, "list_functions", "<source>", exc
                )
                failed_objects.add("<source functions>")
            result["phases"]["functions"] = func_results
            self._update_status("functions", 80, all_errors)

            # ---------- Phase 14: Synonyms ----------
            synonym_results: dict[str, str] = {}
            try:
                synonyms_to_migrate = (
                    oracle_full_objects["synonyms"]
                    if oracle_full_objects is not None
                    else self._source.list_synonyms()
                )
                for synonym in synonyms_to_migrate:
                    syn_key = (
                        synonym.name if synonym.schema_name == "public"
                        else f"{synonym.schema_name}.{synonym.name}"
                    )
                    try:
                        created = self._target.create_synonym(synonym)
                        if created is False:
                            raise RuntimeError(
                                f"synonym target {synonym.base_owner}.{synonym.base_name} "
                                "was not created before the synonym phase"
                            )
                        synonym_results[syn_key] = "created"
                    except Exception as exc:
                        synonym_results[syn_key] = f"skipped: {exc}"
                        if oracle_full_objects is not None:
                            failed_objects.add(syn_key)
                            self._record_object_failure(
                                object_failures, all_errors, "create_synonym", syn_key, exc
                            )
                        else:
                            all_errors.append(f"synonym {syn_key}: {exc}")
            except Exception as exc:
                synonym_results["_error"] = str(exc)
                if oracle_full_objects is not None:
                    self._record_object_failure(
                        object_failures, all_errors, "list_synonyms", "<source>", exc
                    )
                    failed_objects.add("<source synonyms>")
                else:
                    all_errors.append(f"synonym discovery: {exc}")
            result["phases"]["synonyms"] = synonym_results
            self._update_status("synonyms", 82, all_errors)

            # ---------- Phase 15: Triggers ----------
            trigger_results: dict[str, str] = {}
            try:
                triggers_to_migrate = (
                    oracle_full_objects["triggers"]
                    if oracle_full_objects is not None
                    else self._source.get_all_triggers()
                )
                for trigger in triggers_to_migrate:
                    trigger_key = (
                        f"{trigger.table}.{trigger.name}"
                        if trigger.schema_name == "public"
                        else f"{trigger.schema_name}.{trigger.table}.{trigger.name}"
                    )
                    try:
                        if oracle_full_objects is not None:
                            preflight_key = f"{trigger.schema_name}.{trigger.table}.{trigger.name}"
                            preflight_result = oracle_object_phase_results["triggers"].get(preflight_key)
                            if preflight_result != "created":
                                trigger_results[trigger_key] = preflight_result or "skipped: preflight failed"
                                continue
                            self._target.set_trigger_source_state(trigger)
                            trigger_created = True
                        else:
                            trigger_created = self._target.create_trigger(trigger)
                        if trigger_created is False:
                            message = "trigger parent table missing on target"
                            trigger_results[trigger_key] = f"skipped: {message}"
                            self._record_object_failure(
                                object_failures, all_errors, "create_trigger", trigger_key, message
                            )
                            failed_objects.add(trigger_key)
                        else:
                            trigger_results[trigger_key] = "created"
                    except Exception as exc:
                        trigger_results[trigger_key] = f"skipped: {exc}"
                        self._record_object_failure(
                            object_failures, all_errors, "create_trigger", trigger_key, exc
                        )
                        failed_objects.add(trigger_key)
            except Exception as exc:
                trigger_results["_error"] = str(exc)
                self._record_object_failure(
                    object_failures, all_errors, "list_triggers", "<source>", exc
                )
                failed_objects.add("<source triggers>")
            result["phases"]["triggers"] = trigger_results
            if (
                hasattr(self._target, "complete_trigger_data_load")
                and all(value == "created" for value in trigger_results.values())
            ):
                self._target.complete_trigger_data_load()
            self._update_status("triggers", 85, all_errors)

            # ---------- Phase 15.5: Events (MySQL/MariaDB) ----------
            if source_events:
                event_results: dict[str, str] = {}
                for event in source_events:
                    try:
                        self._target.create_event(event)
                        event_results[event.name] = "created"
                    except Exception as exc:
                        event_results[event.name] = (
                            str(exc) if "BLOCKED" in str(exc).upper() else f"error: {exc}"
                        )
                        all_errors.append(str(exc))
                result["phases"]["events"] = event_results

            # ---------- Phase 16: Comments ----------
            comment_results: dict[str, str] = {}
            source_comments: list[Any] = []
            try:
                source_comments = list(
                    oracle_full_objects["comments"]
                    if oracle_full_objects is not None
                    else self._source.list_comments()
                )
                for comment in source_comments:
                    comment_key = (
                        comment.object_name
                        if comment.schema_name == "public"
                        else f"{comment.schema_name}.{comment.object_name}"
                    )
                    if str(comment.object_type).upper() == "COLUMN":
                        comment_key += f".{comment.column_name}"
                    try:
                        self._target.apply_comment(comment)
                        comment_results[comment_key] = "applied"
                    except Exception as exc:
                        comment_results[comment_key] = f"error: {exc}"
                        if oracle_full_objects is not None:
                            failed_objects.add(comment_key)
                            self._record_object_failure(
                                object_failures, all_errors, "apply_comment", comment_key, exc
                            )
                        else:
                            all_errors.append(f"comment {comment_key}: {exc}")
            except Exception as exc:
                comment_results["_error"] = str(exc)
                if oracle_full_objects is not None:
                    self._record_object_failure(
                        object_failures, all_errors, "list_comments", "<source>", exc
                    )
                    failed_objects.add("<source comments>")
                else:
                    all_errors.append(f"comment discovery: {exc}")
            if (
                schema_reconciliation_enabled
                and self._config.get("target", {}).get("engine") == "postgresql"
            ):
                try:
                    result["phases"]["column_comment_reconciliation"] = (
                        self._target.reconcile_postgresql_column_comments(
                            list(all_schemas.values()), source_comments
                        )
                    )
                except Exception as exc:
                    result["phases"]["column_comment_reconciliation"] = {
                        "error": str(exc)
                    }
                    all_errors.append(f"Column comment reconciliation: {exc}")
            result["phases"]["comments"] = comment_results
            self._update_status("comments", 88, all_errors)

            # ---------- Phase: Users, Roles & Role Memberships (Step 14) ----------
            security_results: dict[str, str] = {}
            # MySQL has no database-role object model, so the shared role path
            # must not be driven for a MySQL source.
            source_has_roles = self._config.get("source", {}).get("engine") != "mysql"
            try:
                if oracle_permission_plan is not None:
                    for role in oracle_permission_plan["roles"]:
                        security_results[f"role:{role.name}"] = (
                            f"mapped to same-name target role {role.name}"
                        )
                    if oracle_permission_plan.get("new_users") or oracle_permission_plan.get("new_roles"):
                        try:
                            apply_principals = getattr(
                                self._target, "apply_permission_principals", None
                            )
                            if not callable(apply_principals):
                                raise RuntimeError(
                                    "Oracle target lacks explicitly approved principal provisioning"
                                )
                            principals = apply_principals(oracle_permission_plan)
                            security_results["principal_provisioning"] = principals
                        except Exception as exc:
                            oracle_permission_application_failed = True
                            failed_objects.add("<Oracle principals>")
                            self._record_object_failure(
                                object_failures, all_errors,
                                "oracle_permission_principal_provisioning",
                                "<mapped principals>", exc,
                            )
                            security_results["principal_provisioning"] = f"failed: {self._redact_error(exc)}"
                    if not oracle_permission_application_failed and (
                        oracle_permission_plan.get("object_grants")
                        or oracle_permission_plan.get("system_grants")
                        or oracle_permission_plan.get("memberships")
                        or oracle_permission_plan.get("permission_reconciliation", {}).get("stale")
                    ):
                        try:
                            prepare_provenance = getattr(
                                self._target, "prepare_permission_provenance", None
                            )
                            if not callable(prepare_provenance):
                                raise RuntimeError(
                                    "Oracle target lacks durable permission provenance support"
                                )
                            security_results["permission_provenance"] = prepare_provenance(
                                oracle_permission_plan
                            )
                        except Exception as exc:
                            oracle_permission_application_failed = True
                            failed_objects.add("<Oracle permission provenance>")
                            self._record_object_failure(
                                object_failures, all_errors,
                                "oracle_permission_provenance", "<permission ledger>", exc,
                            )
                            security_results["permission_provenance"] = (
                                f"failed: {self._redact_error(exc)}"
                            )
                    if not oracle_permission_application_failed:
                        try:
                            memberships = self._target.apply_permission_role_memberships(
                                oracle_permission_plan
                            )
                            for item in memberships:
                                security_results[f"membership:{item}"] = "verified/applied"
                        except Exception as exc:
                            oracle_permission_application_failed = True
                            failed_objects.add("<Oracle role memberships>")
                            self._record_object_failure(
                                object_failures, all_errors,
                                "oracle_permission_role_membership", "<mapped roles>", exc,
                            )
                            security_results["role_memberships"] = f"failed: {self._redact_error(exc)}"
                elif source_has_roles:
                    for role in self._source.list_roles():
                        try:
                            self._target.create_role_if_not_exists(role.name)
                            security_results[f"role:{role.name}"] = "created"
                        except Exception as exc:
                            security_results[f"role:{role.name}"] = f"skipped: {exc}"
                if oracle_permission_plan is None:
                    users_to_migrate = self._source.list_users()
                else:
                    users_to_migrate = []
                for user in users_to_migrate:
                    host = getattr(user, "host", None)
                    label = f"{user.name}@{host}" if host is not None else user.name
                    if f"USER {label}" in mysql_user_results:
                        # Already created ahead of object DDL for its DEFINER ref.
                        security_results[f"user:{label}"] = mysql_user_results[f"USER {label}"]
                        continue
                    try:
                        if host is None:
                            self._target.create_user_if_not_exists(user.name)
                        else:
                            self._target.create_user_if_not_exists(user.name, host)
                        security_results[f"user:{label}"] = "created"
                    except Exception as exc:
                        security_results[f"user:{label}"] = f"skipped: {exc}"
                if source_has_roles and oracle_permission_plan is None:
                    for membership in self._source.list_role_memberships():
                        try:
                            self._target.create_role_membership(
                                membership.member_name, membership.role_name
                            )
                            security_results[
                                f"membership:{membership.member_name}->{membership.role_name}"
                            ] = "created"
                        except Exception as exc:
                            security_results[
                                f"membership:{membership.member_name}->{membership.role_name}"
                            ] = f"skipped: {exc}"
            except Exception as exc:
                security_results["_error"] = str(exc)
            result["phases"]["security"] = security_results
            self._update_status("security", 89, all_errors)

            # ---------- Phase 16: Grants ----------
            grant_results: list[str] = []
            try:
                if oracle_permission_plan is not None:
                    if oracle_permission_application_failed:
                        grant_results.append("skipped: Oracle role membership application failed")
                    else:
                        try:
                            applied = self._target.apply_permission_grants(oracle_permission_plan)
                            grant_results.extend(applied)
                            if (
                                oracle_permission_plan.get("system_grants")
                                or any(
                                    item.get("kind") == "SYSTEM"
                                    for item in oracle_permission_plan.get(
                                        "permission_reconciliation", {}
                                    ).get("stale", [])
                                )
                            ):
                                apply_system_grants = getattr(
                                    self._target, "apply_permission_system_grants", None
                                )
                                if not callable(apply_system_grants):
                                    raise RuntimeError(
                                        "Oracle target lacks the scoped permission executor for system grants"
                                    )
                                grant_results.extend(
                                    apply_system_grants(oracle_permission_plan)
                                )
                            self._target.verify_permission_migration(oracle_permission_plan)
                            result["phases"]["oracle_permission_verification"] = {
                                "status": "success",
                                "verified_object_and_column_grants": len(oracle_permission_plan["object_grants"]),
                                "verified_system_grants": len(oracle_permission_plan["system_grants"]),
                                "verified_role_memberships": len(oracle_permission_plan["memberships"]),
                            }
                        except Exception as exc:
                            oracle_permission_application_failed = True
                            failed_objects.add("<Oracle permissions>")
                            grant_results.append(
                                f"Oracle permission migration failed: {self._redact_error(exc)}"
                            )
                            self._record_object_failure(
                                object_failures, all_errors,
                                "oracle_permission_application_or_verification",
                                "<mapped principals>", exc,
                            )
                else:
                    grants = list(self._source.list_grants())
                    # Ensure all grantee roles exist on target before applying grants
                    # Only for engines with role support (not MySQL)
                    target_engine = self._config.get("target", {}).get("engine", "")
                    if target_engine != "mysql":
                        grantees = {g.grantee for g in grants}
                        for grantee in grantees:
                            try:
                                self._target.create_role_if_not_exists(grantee)
                            except Exception as exc:
                                grant_results.append(f"CREATE ROLE {grantee}: skipped ({exc})")
                    for grant in grants:
                        if grant.object_type == "SCHEMA":
                            grant_key = f"{grant.object_name} TO {grant.grantee}"
                        elif self._config.get("target", {}).get("engine") == "mysql":
                            target_database = self._config.get("target", {}).get("connection", {}).get("database", "")
                            if grant.object_type == "GLOBAL":
                                target_object = "*.*"
                            elif grant.object_type == "DATABASE":
                                target_object = f"{target_database}.*"
                            else:
                                target_object = f"{target_database}.{grant.object_name.rsplit('.', 1)[-1]}"
                            grant_key = f"{target_object} TO {grant.grantee}"
                        else:
                            grant_key = (
                                f"{grant.object_name} TO {grant.grantee}"
                                if grant.schema_name == "public"
                                else f"{grant.schema_name}.{grant.object_name} TO {grant.grantee}"
                            )
                        try:
                            self._target.apply_grant(grant)
                            grant_results.append(f"GRANT {grant.privileges} ON {grant_key}: ok")
                        except Exception as exc:
                            grant_results.append(f"GRANT ... ON {grant_key}: failed ({exc})")
                            all_errors.append(f"GRANT {grant.privileges} ON {grant_key}: {exc}")
            except Exception as exc:
                grant_results.append(f"_error: {exc}")
            result["phases"]["grants"] = grant_results
            self._update_status("grants", 91, all_errors)

            if oracle_full_replace:
                if oracle_replacement_inventory is None or failed_objects:
                    result["phases"]["oracle_schema_replacement_verification"] = {
                        "status": "skipped",
                        "reason": (
                            "target Oracle connection became unavailable during an earlier phase"
                            if oracle_target_unavailable
                            else "one or more migration objects failed"
                        ),
                    }
                else:
                    try:
                        verification = self._target.verify_schema_replacement(
                            **oracle_replacement_inventory
                        )
                        partition_counts: dict[str, Any] = {}
                        for schema in oracle_replacement_inventory["schemas"]:
                            if not schema.options.get("oracle_partitioning"):
                                continue
                            source_counts = self._source.partition_row_counts(schema.name)
                            target_counts = self._target.partition_row_counts(schema.name)
                            partition_counts[schema.name] = {
                                "source": source_counts, "target": target_counts,
                                "match": source_counts == target_counts,
                            }
                            if source_counts != target_counts:
                                raise RuntimeError(
                                    f"Oracle partition-wise row counts differ for {schema.name}: "
                                    f"source={source_counts}, target={target_counts}"
                                )
                        verification["partition_row_counts"] = partition_counts
                        result["phases"]["oracle_schema_replacement_verification"] = verification
                    except Exception as exc:
                        target_owner = str(
                            self._config.get("target", {}).get("connection", {}).get("username", "<target owner>")
                        )
                        result["phases"]["oracle_schema_replacement_verification"] = {
                            "status": "failed",
                            "phase": "oracle_schema_replacement_verification",
                            "object": target_owner,
                            "error": self._redact_error(exc),
                        }
                        self._record_object_failure(
                            object_failures, all_errors,
                            "oracle_schema_replacement_verification", target_owner, exc,
                        )
                        failed_objects.add(target_owner)

            # ---------- Phase 17: Validate ----------
            self._update_status("validation", 94, all_errors)
            validation_objects = [
                obj_name for obj_name in all_schemas
                if obj_name not in failed_objects
                and data_load_statuses.get(obj_name, {}).get("status") != "SKIPPED"
            ]
            if oracle_target_unavailable:
                validation_objects = []
                validation = {
                    "status": "failed",
                    "reason": (
                        "Validation could not run because the target Oracle connection "
                        "became unavailable after data loading. Inspect target state before rerunning."
                    ),
                    "objects": [],
                }
            elif oracle_full_sync and not schema_preparation_complete:
                validation_objects = []
                validation = {
                    "status": "skipped",
                    "reason": (
                        "FULL validation skipped because schema/object preparation failed "
                        "and data loading was not performed."
                    ),
                    "objects": [],
                }
            else:
                validation = self.validate(validation_objects, schema_map=schema_map)
            result["phases"]["validation"] = validation
            for object_name, check in validation.get("checks", {}).items():
                precision_check = check.get("precision_data", {})
                mismatches = precision_check.get("mismatches", [])
                mismatch_count = precision_check.get("mismatch_count", len(mismatches))
                if mismatch_count:
                    first = mismatches[0]
                    detail = (
                        f"row {first.get('row')!r}, column {first.get('column')}: "
                        f"source={first.get('source')!r}, target={first.get('target')!r}; "
                        f"{mismatch_count} precision/data mismatch(es)"
                    )
                    self._record_object_failure(
                        object_failures, all_errors, "data_validation", object_name, detail
                    )
                    failed_objects.add(object_name)
            if failed_objects:
                result["status"] = (
                    "partial_success"
                    if validation_objects or (
                        self._config.get("target", {}).get("engine") == "mssql"
                        and not partition_definition_validation_passed
                    )
                    else "failed"
                )
            elif all_errors:
                result["status"] = "partial_success"
            else:
                result["status"] = validation.get("status", "unknown")
            self._update_status("completed", 100, all_errors)

        except Exception as exc:
            safe_error = self._redact_error(exc)
            if self._config.get("target", {}).get("engine") == "oracle":
                if not oracle_target_unavailable:
                    self._record_object_failure(
                        object_failures, all_errors, "run_full", "<migration>", exc,
                        connector=run_full_failure_connector,
                    )
                if oracle_target_unavailable:
                    if oracle_full_replace:
                        result["phases"]["oracle_schema_replacement_verification"] = {
                            "status": "skipped",
                            "reason": (
                                "Target connection became unavailable during apply_constraints; "
                                "verification was not attempted on a dead connection."
                            ),
                        }
                    result["phases"]["validation"] = {
                        "status": "failed",
                        "reason": (
                            "Data loading completed, but target connection loss stopped later "
                            "phases. Inspect target state before rerunning."
                        ),
                        "objects": [],
                    }
            else:
                traceback.print_exc()
            result["status"] = "failed"
            result["error"] = safe_error
            audit_log(phase="run_full", status="failed", details={"error": safe_error})
            self._update_status("failed", 0, all_errors)
            if self._notifier is not None:
                self._notifier.notify({"phase": "run_full", "status": "failed", "details": {"error": safe_error}})
        finally:
            if self._config.get("target", {}).get("engine") == "oracle":
                cleanup_failures = False
                try:
                    if hasattr(self._target, "restore_suspended_triggers"):
                        self._target.restore_suspended_triggers()
                except Exception as exc:
                    cleanup_failures = True
                    self._record_object_failure(
                        object_failures, all_errors, "trigger_cleanup", "<target>", exc,
                    )
                for connector, label in (
                    (self._source, "Oracle source"),
                    (self._target, "Oracle target"),
                ):
                    try:
                        connector.close()
                    except Exception as exc:
                        cleanup_failures = True
                        self._record_object_failure(
                            object_failures, all_errors, "connection_cleanup",
                            "<connection>", exc, connector=label,
                        )
                if cleanup_failures:
                    result["status"] = "failed"
                    result["phases"]["cleanup"] = "failed; inspect diagnostic log"
                else:
                    result["phases"]["cleanup"] = "success"
            else:
                try:
                    if hasattr(self._target, "restore_suspended_triggers"):
                        self._target.restore_suspended_triggers()
                finally:
                    try:
                        self._source.close()
                    finally:
                        self._target.close()

        end_time = time.time()
        if self._config.get("target", {}).get("engine") == "oracle":
            # Keep the Oracle run's top-level error list aligned with its
            # object_failures ledger; preflighted objects are not retried in
            # the later report phases.
            result["errors"] = [self._redact_error(error) for error in all_errors]
            if result["errors"]:
                result["error"] = "; ".join(result["errors"])
                if result.get("status") in {"success", "completed"}:
                    result["status"] = "partial_success"
        audit_log(phase="run_full", status="completed", details={
            "status": result.get("status", "unknown"),
            "duration_s": round(end_time - start_time, 2),
        })
        return result

    def _resolve_connector_secrets(
        self,
        connector: Any,
        config: dict[str, Any],
        resolver: Any = None,
    ) -> None:
        # Endpoint-specific resolvers are passed by run_full/run_cdc. Fall back
        # to the global resolver for callers that do not provide one.
        resolver = resolver if resolver is not None else self._secret_resolver
        if resolver is None:
            return
        connection_config = config.get("connection", config)
        connector_config = getattr(connector, "_config", None)
        has_wallet_secret = (
            config.get("engine") == "oracle"
            and "ssl_wallet_password_secret" in connection_config
        )
        if not isinstance(connector_config, dict):
            if "password_secret" in connection_config or has_wallet_secret:
                raise TypeError(
                    "Connector cannot receive configured secret credentials"
                )
            endpoint_password = connection_config.get("password")
            if isinstance(endpoint_password, str) and endpoint_password:
                self._redaction_secrets.add(endpoint_password)
            return
        if "password_secret" in connection_config:
            connector_config["password"] = resolver.resolve(connection_config["password_secret"])
        endpoint_password = connector_config.get("password")
        if isinstance(endpoint_password, str) and endpoint_password:
            self._redaction_secrets.add(endpoint_password)
        if has_wallet_secret:
            connector_config["_ssl_wallet_password"] = resolver.resolve(
                connection_config["ssl_wallet_password_secret"]
            )
            wallet_password = connector_config.get("_ssl_wallet_password")
            if isinstance(wallet_password, str) and wallet_password:
                self._redaction_secrets.add(wallet_password)

    def _apply_schema_scope(self, source: Any) -> None:
        """
        STEP 3B — propagate ``migration.include_schemas`` from the top-level
        config down to the source connector before ``connect()`` is called.

        Backward compatible: if ``migration.include_schemas`` is not set,
        the connector falls back to its own default (``["public"]``), which
        preserves every pre-existing PostgreSQL → PostgreSQL behavior.
        """
        include_schemas = self._config.get("migration", {}).get("include_schemas")
        if include_schemas is None:
            return
        if not isinstance(include_schemas, (list, tuple)) or not include_schemas:
            return
        cleaned: list[str] = [
            s for s in include_schemas
            if isinstance(s, str) and s
            and s not in {"pg_catalog", "information_schema", "pg_toast"}
            and not s.startswith("pg_")
        ]
        if not cleaned:
            return
        source._config["include_schemas"] = cleaned

    def _chunked(self, iterable: Iterator[Any], chunk_size: int) -> Iterator[list[Any]]:
        it = iter(iterable)
        while True:
            chunk = list(itertools.islice(it, chunk_size))
            if not chunk:
                break
            yield chunk

    def _ensure_wal_level_logical(self) -> None:
        """
        Preflight check for CDC: ensure wal_level = logical on the source.

        If wal_level is already 'logical' (common on cloud PostgreSQL like Azure/AWS/GCP),
        this is a no-op. If not, an explicit cdc.allow_source_service_restart
        opt-in is required before the method:
          1. Reads postgresql.conf path from the database
          2. Patches/adds  wal_level = logical  in that file
          3. Restarts the PostgreSQL service (tries pg_ctl → net stop/start → systemctl)
          4. Reconnects the source connector and verifies the change
          5. Raises RuntimeError with manual instructions if auto-restart fails
        """
        # ── 1. Check current value ────────────────────────────────────────────
        try:
            with self._source._conn.cursor() as cur:
                cur.execute("SHOW wal_level")
                wal_level = cur.fetchone()[0]
        except Exception:
            # If source is not PostgreSQL (MySQL, MSSQL etc.) this will fail — skip silently
            return

        if wal_level == "logical":
            audit_log(phase="preflight_cdc", status="wal_level_ok",
                      details={"wal_level": wal_level})
            return

        audit_log(phase="preflight_cdc", status="wal_level_needs_fix",
                  details={"current": wal_level, "required": "logical"})

        if not self._allow_source_service_restart:
            audit_log(
                phase="preflight_cdc",
                status="restart_not_allowed",
                details={"current": wal_level, "required": "logical"},
            )
            raise RuntimeError(
                "PostgreSQL source requires wal_level = logical for CDC, which requires "
                "a source-service restart. Automatic source-service restart is disabled. "
                "Configure PostgreSQL for logical replication and restart it manually, or set "
                "cdc.allow_source_service_restart=true only if you explicitly authorize "
                "the Migration Platform to restart the source PostgreSQL service."
            )

        # ── 2. Find postgresql.conf ───────────────────────────────────────────
        try:
            with self._source._conn.cursor() as cur:
                cur.execute("SHOW config_file")
                config_file = pathlib.Path(cur.fetchone()[0])
        except Exception as exc:
            raise RuntimeError(
                f"Cannot locate postgresql.conf automatically: {exc}\n"
                "Please manually set  wal_level = logical  and restart PostgreSQL."
            ) from exc

        # ── 3. Patch postgresql.conf ──────────────────────────────────────────
        try:
            text = config_file.read_text(encoding="utf-8")
            # Replace any existing wal_level line (commented or not)
            new_text, n = re.subn(
                r"^#?\s*wal_level\s*=\s*\S+[^\n]*",
                "wal_level = logical\t\t\t# auto-set by migration-platform",
                text,
                flags=re.MULTILINE,
            )
            if n == 0:
                # Line not found at all — append it
                new_text = text.rstrip() + "\nwal_level = logical\t\t\t# auto-set by migration-platform\n"
            config_file.write_text(new_text, encoding="utf-8")
            audit_log(phase="preflight_cdc", status="config_patched",
                      details={"config_file": str(config_file), "replacements": n})
        except PermissionError as exc:
            raise RuntimeError(
                f"Cannot write to {config_file} — permission denied.\n"
                "Run this command as Administrator, or manually add:\n"
                "  wal_level = logical\n"
                f"to {config_file} and restart PostgreSQL."
            ) from exc

        # ── 4. Restart PostgreSQL ─────────────────────────────────────────────
        data_dir = config_file.parent
        self._restart_postgresql(data_dir)

        # ── 5. Reconnect and verify ───────────────────────────────────────────
        time.sleep(3)   # brief pause for service to fully start
        try:
            self._source.connect()
        except Exception as exc:
            raise RuntimeError(
                f"PostgreSQL was restarted but reconnect failed: {exc}\n"
                "Wait a few seconds and retry."
            ) from exc

        with self._source._conn.cursor() as cur:
            cur.execute("SHOW wal_level")
            new_level = cur.fetchone()[0]

        if new_level != "logical":
            raise RuntimeError(
                f"wal_level is still '{new_level}' after restart.\n"
                "Please restart PostgreSQL manually as Administrator and verify:\n"
                "  SHOW wal_level;   -- should return 'logical'"
            )

        audit_log(phase="preflight_cdc", status="wal_level_fixed",
                  details={"wal_level": new_level, "config_file": str(config_file)})

    def _restart_postgresql(self, data_dir: pathlib.Path) -> None:
        """
        Try multiple methods to restart PostgreSQL. Logs audit events for each attempt.
        Raises RuntimeError with manual instructions if all methods fail.
        """
        errors: list[str] = []

        # ── Method 1: pg_ctl (works if we have OS access to the data dir) ─────
        pg_bin = data_dir.parent / "bin"
        pg_ctl = pg_bin / ("pg_ctl.exe" if platform.system() == "Windows" else "pg_ctl")
        if pg_ctl.exists():
            try:
                result = subprocess.run(
                    [str(pg_ctl), "restart", "-D", str(data_dir), "-m", "fast", "-w"],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    audit_log(phase="preflight_cdc", status="service_restarted",
                              details={"method": "pg_ctl", "data_dir": str(data_dir)})
                    return
                errors.append(f"pg_ctl: {result.stderr.strip()}")
            except Exception as exc:
                errors.append(f"pg_ctl: {exc}")

        # ── Method 2: Windows — net stop / net start ─────────────────────────
        if platform.system() == "Windows":
            for svc in ["postgresql-x64-18", "postgresql-x64-17", "postgresql-x64-16",
                        "postgresql-x64-15", "postgresql"]:
                try:
                    subprocess.run(["net", "stop", svc], capture_output=True, timeout=30)
                    r = subprocess.run(["net", "start", svc], capture_output=True,
                                       text=True, timeout=30)
                    if r.returncode == 0:
                        audit_log(phase="preflight_cdc", status="service_restarted",
                                  details={"method": f"net stop/start {svc}"})
                        return
                    errors.append(f"net start {svc}: {r.stderr.strip()}")
                except Exception as exc:
                    errors.append(f"net stop/start {svc}: {exc}")

        # ── Method 3: Linux — systemctl ───────────────────────────────────────
        if platform.system() == "Linux":
            for svc in ["postgresql", "postgresql-18", "postgresql-17",
                        "postgresql-16", "postgresql-15"]:
                try:
                    r = subprocess.run(
                        ["systemctl", "restart", svc],
                        capture_output=True, text=True, timeout=60,
                    )
                    if r.returncode == 0:
                        audit_log(phase="preflight_cdc", status="service_restarted",
                                  details={"method": f"systemctl restart {svc}"})
                        return
                    errors.append(f"systemctl {svc}: {r.stderr.strip()}")
                except Exception as exc:
                    errors.append(f"systemctl {svc}: {exc}")

        # ── All methods failed ────────────────────────────────────────────────
        audit_log(phase="preflight_cdc", status="restart_failed",
                  details={"errors": errors})
        win_cmd = "Restart-Service postgresql-x64-18   # Run PowerShell as Administrator"
        linux_cmd = "sudo systemctl restart postgresql"
        raise RuntimeError(
            "Could not restart PostgreSQL automatically.\n"
            "postgresql.conf has been updated — please restart PostgreSQL manually:\n"
            f"  Windows: {win_cmd}\n"
            f"  Linux:   {linux_cmd}\n"
            f"Attempted methods failed with: {'; '.join(errors)}"
        )

    def run_cdc(self, max_iterations: int | None = None) -> dict[str, Any]:

        run_id = get_run_id()
        set_run_id(run_id)
        start_time = time.time()
        audit_log(phase="run_cdc", status="started", details={"run_id": run_id})

        result: dict[str, Any] = {
            "run_id": run_id,
            "mode": "cdc",
            "phases": {},
        }

        all_errors: list[str] = []
        cdc_engine = None  # kept in outer scope so finally can call cleanup()

        try:
            # Fix #4: resolve secrets on the connector objects FIRST so that
            # self._source._config["password"] is populated before we read it
            # back for the CDC engine connection below.
            self._resolve_connector_secrets(
                self._source,
                self._config.get("source", {}),
                self._source_secret_resolver,
            )
            self._resolve_connector_secrets(
                self._target,
                self._config.get("target", {}),
                self._target_secret_resolver,
            )
            self._apply_schema_scope(self._source)
            self._source.connect()
            self._target.connect()
            result["phases"]["connect"] = "success"
            self._update_status("connect", 5, [])

            self._target.ensure_database_exists()
            result["phases"]["ensure_database"] = "success"
            self._update_status("ensure_database", 8, [])

            # ---- Fix #3: Run all DDL phases before initial sync ----
            # Phase 1: Extensions
            ext_results: dict[str, str] = {}
            try:
                for ext in self._source.list_extensions():
                    try:
                        self._target.create_extension(ext)
                        ext_results[ext.name] = "created"
                    except Exception as exc:
                        ext_results[ext.name] = f"skipped: {exc}"
            except Exception as exc:
                ext_results["_error"] = str(exc)
            result["phases"]["extensions"] = ext_results
            self._update_status("extensions", 10, all_errors)

            # Phase 2: Schemas
            schema_results: dict[str, str] = {}
            try:
                for s in self._source.list_schemas():
                    try:
                        self._target.create_schema(s)
                        schema_results[s.name] = "created"
                    except Exception as exc:
                        schema_results[s.name] = f"skipped: {exc}"
            except Exception as exc:
                schema_results["_error"] = str(exc)
            result["phases"]["schemas"] = schema_results
            self._update_status("schemas", 11, all_errors)

            # Phase 3: Custom Types
            type_results: dict[str, str] = {}
            try:
                for t in self._source.list_types():
                    try:
                        self._target.create_type(t)
                        type_results[t.name] = f"created ({t.kind})"
                    except Exception as exc:
                        type_results[t.name] = f"skipped: {exc}"
            except Exception as exc:
                type_results["_error"] = str(exc)
            result["phases"]["custom_types"] = type_results
            self._update_status("custom_types", 12, all_errors)

            # Phase 3.5: Sequences
            seq_create_results: dict[str, str] = {}
            try:
                all_sequences = self._source.list_all_sequences()
                for seq in all_sequences:
                    try:
                        self._target.create_sequence(seq)
                        seq_create_results[seq.name] = "created"
                    except Exception as exc:
                        seq_create_results[seq.name] = f"skipped: {exc}"
            except AttributeError:
                pass
            except Exception as exc:
                seq_create_results["_error"] = str(exc)
            result["phases"]["create_sequences"] = seq_create_results
            self._update_status("create_sequences", 13, all_errors)

            # Identify partitioned tables to exclude from Phase 4 (created in Phase 4.5 instead)
            partitioned_table_keys: set[tuple[str, str]] = set()
            try:
                for pt in self._source.get_partitioned_tables():
                    schema_name = pt.schema_name or "dbo"
                    partitioned_table_keys.add((schema_name, pt.table_name))
            except AttributeError:
                pass

            # Phase 4: Create Tables
            objects = self._source.list_objects()
            all_schemas: dict[str, Any] = {}
            for obj_name in objects:
                schema = self._source.get_schema(obj_name)
                schema_name = schema.schema_name or "dbo"
                if (schema_name, obj_name) in partitioned_table_keys:
                    # Partitioned tables are created in Phase 4.5 with partition scheme applied
                    all_schemas[obj_name] = schema
                    continue
                self._apply_field_mappings(schema)
                self._target.create_object_if_missing(schema)
                all_schemas[obj_name] = schema
            result["phases"]["create_tables"] = "success"
            self._update_status("create_tables", 15, all_errors)

            # Phase 4.5: Partition Children
            partition_results: dict[str, str] = {}
            try:
                for part in self._source.list_partitions():
                    try:
                        self._target.create_partition(part)
                        partition_results[part.name] = f"created (parent: {part.parent_table})"
                    except Exception as exc:
                        partition_results[part.name] = f"skipped: {exc}"
            except AttributeError:
                try:
                    for pf in self._source.list_partition_functions():
                        try:
                            self._target.create_partition_function(pf)
                            partition_results[pf.name] = f"partition function created"
                        except Exception as exc:
                            partition_results[pf.name] = f"skipped: {exc}"
                    for ps in self._source.list_partition_schemes():
                        try:
                            self._target.create_partition_scheme(ps)
                            partition_results[ps.name] = f"partition scheme created"
                        except Exception as exc:
                            partition_results[ps.name] = f"skipped: {exc}"
                    for pt in self._source.get_partitioned_tables():
                        try:
                            schema = self._source.get_schema(pt.table_name)
                            self._target.create_partitioned_table(schema, pt.partition_scheme_name, pt.partition_column)
                            partition_results[pt.table_name] = f"partitioned table created"
                        except Exception as exc:
                            partition_results[pt.table_name] = f"skipped: {exc}"
                except AttributeError:
                    pass
            except Exception as exc:
                partition_results["_error"] = str(exc)
            result["phases"]["create_partitions"] = partition_results
            self._update_status("create_partitions", 16, all_errors)

            if (
                self._config.get("source", {}).get("engine") == "mssql"
                and self._config.get("target", {}).get("engine") == "mssql"
                and hasattr(self._source, "inspect_partition_metadata")
                and hasattr(self._target, "inspect_partition_metadata")
            ):
                try:
                    comparison = _mssql_partition_comparison(
                        self._source.inspect_partition_metadata(),
                        self._target.inspect_partition_metadata(),
                    )
                    result["phases"]["partition_definition_validation"] = {
                        "comparison_available": True, **comparison,
                    }
                    audit_log(
                        phase="partition_definition_validation",
                        status="matches" if comparison["definition_matches"] else "mismatch",
                        details={"definition_matches": comparison["definition_matches"],
                                 "mismatches": comparison["mismatches"]},
                    )
                    for mismatch in comparison["mismatches"]:
                        object_name = str(mismatch["object"])
                        message = (
                            f"Partition metadata mismatch for {object_name}: {mismatch['reason']}. "
                            "Target partition objects and data were preserved; SQL Server partition "
                            "rebuild is unsupported. Reconcile through an approved maintenance plan."
                        )
                        partition_results[f"{mismatch['kind']}:{object_name}"] = {
                            "status": "mismatch", "definition_matches": False,
                            "source": mismatch["source"], "target": mismatch["target"],
                            "reason": message,
                        }
                        audit_log(
                            phase="partition_definition_validation", status="mismatch",
                            details={"object": object_name, "kind": mismatch["kind"],
                                     "definition_matches": False, "source": mismatch["source"],
                                     "target": mismatch["target"], "reason": message},
                        )
                        self._record_object_failure(
                            result["phases"].setdefault("object_failures", []),
                            all_errors, "partition_definition_validation", object_name, message,
                        )
                        failed_objects.add(str(mismatch.get("table_name") or object_name))
                    if comparison["mismatches"] and self._stop_on_error:
                        raise _PartitionDefinitionError(
                            "Partition definition validation failed: " + ", ".join(
                                sorted(str(item["object"]) for item in comparison["mismatches"])
                            )
                        )
                except _PartitionDefinitionError:
                    raise
                except Exception as exc:
                    message = f"Partition metadata comparison unavailable: {exc}"
                    result["phases"]["partition_definition_validation"] = {
                        "comparison_available": False, "definition_matches": None,
                        "error": str(exc),
                    }
                    audit_log(
                        phase="partition_definition_validation", status="unavailable",
                        details={"comparison_available": False, "definition_matches": None,
                                 "error": str(exc)},
                    )
                    all_errors.append(message)
                    if self._stop_on_error:
                        raise _PartitionDefinitionError(message) from exc

            # ---- Phase 5: Initial Data Sync ----
            schema_map = {name: (s.schema_name if hasattr(s, "schema_name") else None) for name, s in all_schemas.items()}
            data_load_order = order_data_load_objects(all_schemas, objects)
            result["phases"]["data_load_order"] = data_load_order
            total_rows = self._estimate_total_rows(data_load_order, schema_map)
            processed_rows = 0
            for idx, obj_name in enumerate(data_load_order, start=1):
                schema = all_schemas[obj_name]
                count = self._source.get_object_count(obj_name, schema.schema_name if hasattr(schema, "schema_name") else None)
                rows = self._source.export_full(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None)
                upsert_result = UpsertResult()
                for chunk in self._chunked(rows, self._batch_size):
                    chunk_result = self._target.upsert_batch(obj_name, iter(chunk), schema)
                    upsert_result.success_count += chunk_result.success_count
                    upsert_result.failure_count += chunk_result.failure_count
                    upsert_result.errors.extend(chunk_result.errors)
                    upsert_result.failed_items.extend(chunk_result.failed_items)
                    processed_rows += chunk_result.success_count
                    if total_rows > 0:
                        progress = min(45, int(17 + (processed_rows / total_rows) * 28))
                    else:
                        progress = 17 + int((idx / max(len(objects), 1)) * 28)
                    self._update_status(
                        f"initial sync: {obj_name} ({idx}/{len(objects)})", progress, all_errors,
                    )
                result["phases"][obj_name] = {
                    "initial_sync_rows": count,
                    "success": upsert_result.success_count,
                    "failure": upsert_result.failure_count,
                }
                all_errors.extend(upsert_result.errors)

            result["phases"]["initial_sync"] = "complete"
            self._update_status("initial_sync complete", 45, all_errors)

            # Phase 6-8: Indexes, FKs, Check Constraints
            constraint_results: dict[str, str] = {}
            for obj_name, schema in all_schemas.items():
                try:
                    self._target.apply_constraints(schema)
                    constraint_results[obj_name] = "success"
                except Exception as exc:
                    constraint_results[obj_name] = f"error: {exc}"
                    all_errors.append(str(exc))
            result["phases"]["apply_constraints"] = constraint_results
            self._update_status("apply_constraints", 50, all_errors)

            # Phase 9: Sequence Ownership
            seq_owner_results: dict[str, str] = {}
            try:
                all_sequences = self._source.list_all_sequences() if hasattr(self._source, "list_all_sequences") else []
                for seq in all_sequences:
                    if seq.owned_by:
                        try:
                            self._target.apply_sequence_ownership(seq)
                            seq_owner_results[seq.name] = f"owned: {seq.owned_by}"
                        except Exception as exc:
                            seq_owner_results[seq.name] = f"skipped: {exc}"
            except Exception as exc:
                seq_owner_results["_error"] = str(exc)
            result["phases"]["apply_sequence_ownership"] = seq_owner_results
            self._update_status("apply_sequence_ownership", 55, all_errors)

            # Phase 10: Row-Level Security
            rls_results: dict[str, Any] = {}
            try:
                for obj_name, schema in all_schemas.items():
                    if schema.rls_enabled:
                        rls_results[obj_name] = []
                        for policy in self._source.get_rls_policies(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None):
                            try:
                                self._target.apply_rls_policy(policy)
                                rls_results[obj_name].append(f"{policy.name}: created")
                            except Exception as exc:
                                rls_results[obj_name].append(f"{policy.name}: skipped ({exc})")
            except Exception as exc:
                rls_results["_error"] = str(exc)
            result["phases"]["row_level_security"] = rls_results
            self._update_status("row_level_security", 52, all_errors)

            # Phase 10: Advance Sequences
            seq_advance_results: dict[str, list[str]] = {}
            try:
                all_seqs = (
                    self._source.list_all_sequences()
                    if hasattr(self._source, "list_all_sequences") else []
                )
                for seq in all_seqs:
                    if seq.owned_by:
                        parts = seq.owned_by.split(".", 1)
                        if len(parts) == 2:
                            tbl, col = parts
                            if tbl not in seq_advance_results:
                                seq_advance_results[tbl] = []
                            try:
                                self._target.advance_sequence(seq.name, tbl, col)
                                seq_advance_results[tbl].append(f"{seq.name}: advanced")
                            except Exception as exc:
                                seq_advance_results[tbl].append(f"{seq.name}: skipped ({exc})")
            except Exception as exc:
                seq_advance_results["_error"] = [str(exc)]
            result["phases"]["advance_sequences"] = seq_advance_results
            self._update_status("advance_sequences", 54, all_errors)

            # Phase 11: Views
            view_results: dict[str, str] = {}
            try:
                for view in self._source.list_views():
                    try:
                        self._target.create_view(view)
                        view_results[view.name] = "created"
                    except Exception as exc:
                        view_results[view.name] = f"error: {exc}"
            except Exception as exc:
                view_results["_error"] = str(exc)
            result["phases"]["views"] = view_results
            self._update_status("views", 56, all_errors)

            # Phase 12: Materialized Views
            mv_results: dict[str, str] = {}
            try:
                for mv in self._source.list_materialized_views():
                    try:
                        self._target.create_materialized_view(mv)
                        self._target.refresh_materialized_view(mv.name, schema_name=mv.schema_name)
                        mv_results[mv.name] = "created+refreshed"
                    except Exception as exc:
                        mv_results[mv.name] = f"error: {exc}"
            except Exception as exc:
                mv_results["_error"] = str(exc)
            result["phases"]["materialized_views"] = mv_results
            self._update_status("materialized_views", 58, all_errors)

            # Phase 13: Functions & Stored Procedures
            func_results: dict[str, str] = {}
            try:
                for func in self._source.list_functions():
                    func_key = (
                        func.name if func.schema_name == "public"
                        else f"{func.schema_name}.{func.name}"
                    )
                    try:
                        self._target.create_function(func)
                        func_results[func_key] = "created"
                    except Exception as exc:
                        func_results[func_key] = f"skipped: {exc}"
            except Exception as exc:
                func_results["_error"] = str(exc)
            result["phases"]["functions"] = func_results
            self._update_status("functions", 60, all_errors)

            # Phase 14: Synonyms
            synonym_results: dict[str, str] = {}
            try:
                for synonym in self._source.list_synonyms():
                    syn_key = (
                        synonym.name if synonym.schema_name == "public"
                        else f"{synonym.schema_name}.{synonym.name}"
                    )
                    try:
                        self._target.create_synonym(synonym)
                        synonym_results[syn_key] = "created"
                    except Exception as exc:
                        synonym_results[syn_key] = f"skipped: {exc}"
            except Exception as exc:
                synonym_results["_error"] = str(exc)
            result["phases"]["synonyms"] = synonym_results
            self._update_status("synonyms", 61, all_errors)

            # Phase 15: Triggers
            trigger_results: dict[str, str] = {}
            try:
                for trigger in self._source.get_all_triggers():
                    trigger_key = (
                        f"{trigger.table}.{trigger.name}"
                        if trigger.schema_name == "public"
                        else f"{trigger.schema_name}.{trigger.table}.{trigger.name}"
                    )
                    try:
                        self._target.create_trigger(trigger)
                        trigger_results[trigger_key] = "created"
                    except Exception as exc:
                        trigger_results[trigger_key] = f"skipped: {exc}"
            except Exception as exc:
                trigger_results["_error"] = str(exc)
            result["phases"]["triggers"] = trigger_results
            self._update_status("triggers", 62, all_errors)

            # Phase 16: Comments
            comment_results: dict[str, str] = {}
            try:
                for comment in self._source.list_comments():
                    comment_key = (
                        comment.object_name
                        if comment.schema_name == "public"
                        else f"{comment.schema_name}.{comment.object_name}"
                    )
                    try:
                        self._target.apply_comment(comment)
                        comment_results[comment_key] = "applied"
                    except Exception as exc:
                        comment_results[comment_key] = f"skipped: {exc}"
            except Exception as exc:
                comment_results["_error"] = str(exc)
            result["phases"]["comments"] = comment_results
            self._update_status("comments", 64, all_errors)

            # Phase 16: Grants
            grant_results: list[str] = []
            try:
                for grant in self._source.list_grants():
                    grant_key = (
                        f"{grant.object_name} TO {grant.grantee}"
                        if grant.schema_name == "public"
                        else f"{grant.schema_name}.{grant.object_name} TO {grant.grantee}"
                    )
                    try:
                        self._target.apply_grant(grant)
                        grant_results.append(f"GRANT {grant.privileges} ON {grant_key}: ok")
                    except Exception as exc:
                        grant_results.append(f"GRANT ... ON {grant_key}: skipped ({exc})")
                        all_errors.append(f"GRANT {grant.privileges} ON {grant_key}: {exc}")
                        failed_objects.add(grant_key)
            except Exception as exc:
                grant_results.append(f"_error: {exc}")
            result["phases"]["grants"] = grant_results
            self._update_status("grants", 66, all_errors)

            # ---- Start CDC loop ----
            source_type = self._config.get("source", {}).get("engine", "unknown")
            cdc_config = self._config.get("cdc", {})
            poll_interval = cdc_config.get("poll_interval", 10)

            try:
                # Auto-fix wal_level if needed before starting CDC
                self._ensure_wal_level_logical()

                # Fix #4: Build a resolved connection config so the CDC engine
                # receives the actual password (not just password_secret key).
                # self._source._config["password"] was populated by
                # _resolve_connector_secrets() called at the start of this method.
                resolved_conn = dict(
                    self._config.get("source", {}).get("connection", {})
                )
                if hasattr(self._source, "_config") and "password" in self._source._config:
                    resolved_conn["password"] = self._source._config["password"]

                cdc_engine = create_cdc_engine(source_type, resolved_conn)
                cdc_engine.connect()
                cdc_engine.start()
                cdc_result = self.run_cdc_loop(
                    cdc_engine,
                    self._target,
                    poll_interval=poll_interval,
                    max_iterations=max_iterations,
                )
                result["phases"]["cdc_loop"] = cdc_result
                result["status"] = cdc_result.get("status", "completed")
                self._update_status("completed", 100, cdc_result.get("errors", []))
            except Exception as cdc_exc:
                result["status"] = "cdc_failed"
                result["cdc_error"] = str(cdc_exc)
                audit_log(phase="run_cdc", status="cdc_loop_failed", details={"error": str(cdc_exc)})
                self._update_status("cdc_failed", 100, [str(cdc_exc)])
                if self._notifier is not None:
                    self._notifier.notify({"phase": "run_cdc", "status": "cdc_loop_failed", "details": {"error": str(cdc_exc)}})
            finally:
                # Fix #7: Always drop the replication slot to prevent WAL disk fill.
                if cdc_engine is not None and hasattr(cdc_engine, "cleanup"):
                    cdc_engine.cleanup()

        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            audit_log(phase="run_cdc", status="failed", details={"error": str(exc)})
            self._update_status("failed", 0, [str(exc)])
            if self._notifier is not None:
                self._notifier.notify({"phase": "run_cdc", "status": "failed", "details": {"error": str(exc)}})

        end_time = time.time()
        audit_log(phase="run_cdc", status="completed", details={"status": result.get("status", "unknown"), "duration_s": round(end_time - start_time, 2)})
        return result

    def run_assessment(self) -> AssessmentReport:
        self._source.connect()
        report = self._assessment_gen.generate(
            self._config.get("source", {}).get("engine", "unknown"),
            self._config.get("target", {}).get("engine", "unknown"),
            self._source,
        )
        return report

    def validate(self, objects: list[str] | None = None, schema_map: dict[str, str | None] | None = None) -> dict[str, Any]:
        validator = Validator(self._source, self._target)
        validation_mode = self._config.get("validation", {}).get("mode", "count")
        oracle_pair = (
            self._config.get("source", {}).get("engine") == "oracle"
            and self._config.get("target", {}).get("engine") == "oracle"
        )
        if oracle_pair:
            for connector in (self._source, self._target):
                enable_precision = getattr(
                    connector, "enable_precision_safe_full_load", None
                )
                if callable(enable_precision):
                    enable_precision()
        source_objects = objects if objects is not None else self._source.list_objects()

        results: dict[str, Any] = {
            "mode": validation_mode,
            "checks": {},
        }

        all_passed = True
        for obj_name in source_objects:
            schema_name = schema_map.get(obj_name) if schema_map else None
            source_schema = None
            has_lobs = False
            if oracle_pair:
                from core.connectors.oracle.precision import (
                    compare_oracle_rows,
                    oracle_lob_kind,
                )

                source_schema = self._source.get_schema(obj_name)
                has_lobs = any(
                    oracle_lob_kind(column.source_type)
                    for column in source_schema.columns
                )
            # Generic full validation materializes whole tables. Oracle LOB
            # checks stream one row and bounded chunks at a time instead.
            check = validator.validate(
                obj_name,
                mode="count" if oracle_pair and has_lobs else validation_mode,
                schema_name=schema_name,
            )
            if oracle_pair:
                precision_check = compare_oracle_rows(
                    self._source.export_full(obj_name, schema_name=schema_name),
                    self._target.export_full(obj_name, schema_name=schema_name),
                    source_schema,
                    all_columns=has_lobs and validation_mode in {"full", "checksum"},
                )
                check["precision_data"] = precision_check
                check["match"] = bool(check.get("match")) and precision_check["match"]
            if self._config.get("target", {}).get("engine") == "oracle":
                from core.connectors.oracle.target import _schemas_compatible

                if source_schema is None:
                    source_schema = self._source.get_schema(obj_name)
                target_schema = self._target.inspect_schema(obj_name, schema_name=schema_name)
                schema_match = target_schema is not None and _schemas_compatible(
                    source_schema,
                    target_schema,
                    str(self._config.get("source", {}).get("engine", "unknown")),
                )
                check["schema_structure"] = {"match": schema_match}
                check["match"] = bool(check.get("match")) and schema_match
            results["checks"][obj_name] = check
            if not check.get("match", False):
                all_passed = False

        results["status"] = "success" if all_passed else "mismatch"
        return results

    def rollback(self) -> dict[str, Any]:
        raise NotImplementedError(
            "Rollback is not implemented. Implement snapshot-based or target-teardown rollback before use."
        )

    def _apply_field_mappings(self, schema: Schema) -> None:
        for mapping in self._field_mappings:
            source_field = mapping.get("source_field")
            target_field = mapping.get("target_field")
            exclude = mapping.get("exclude", False)

            if exclude and source_field:
                schema.columns = [c for c in schema.columns if c.name != source_field]
                continue

            if source_field and target_field:
                for col in schema.columns:
                    if col.name == source_field:
                        col.name = target_field

    def run_cdc_loop(
        self,
        cdc_engine: CDCEngine,
        target: TargetConnector,
        poll_interval: int = 10,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        audit_log(phase="cdc_loop", status="started", details={"poll_interval": poll_interval})

        result: dict[str, Any] = {
            "mode": "cdc_loop",
            "iterations": 0,
            "total_applied": 0,
            "total_failures": 0,
        }

        iteration = 0
        all_errors: list[str] = []
        try:
            while True:
                iteration += 1
                result["iterations"] = iteration

                if max_iterations is not None and iteration > max_iterations:
                    audit_log(phase="cdc_loop", status="stopped", details={"reason": "max_iterations_reached"})
                    break

                events = cdc_engine.poll_changes()
                if not events:
                    time.sleep(poll_interval)
                    continue

                apply_result = cdc_engine.apply(events, target)
                cdc_engine.checkpoint(apply_result)

                result["total_applied"] += apply_result.success_count
                result["total_failures"] += apply_result.failure_count
                all_errors.extend(apply_result.errors)

                if apply_result.failure_count > 0:
                    audit_log(
                        phase="cdc_loop",
                        status="partial_failure",
                        details={"iteration": iteration, "success": apply_result.success_count, "failure": apply_result.failure_count},
                    )

                self._update_status(
                    f"cdc iteration {iteration}",
                    min(99, 30 + iteration * 5),
                    all_errors,
                )
                time.sleep(poll_interval)

        except KeyboardInterrupt:
            audit_log(phase="cdc_loop", status="interrupted", details={})
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            all_errors.append(str(exc))
            audit_log(phase="cdc_loop", status="failed", details={"error": str(exc)})
            if self._notifier is not None:
                self._notifier.notify({"phase": "cdc_loop", "status": "failed", "details": {"error": str(exc)}})

        result["status"] = result.get("status", "completed")
        result["errors"] = all_errors
        audit_log(phase="cdc_loop", status="completed", details=result)
        return result
