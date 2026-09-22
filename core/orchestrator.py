from __future__ import annotations

import itertools
import pathlib
import platform
import re
import subprocess
import time
from collections.abc import Iterator
from typing import Any

from core.alerting import create_notifier
from core.assessment.report_generator import AssessmentReport, AssessmentReportGenerator
from core.audit_logger import audit_log, get_run_id, set_run_id
from core.connectors import create_cdc_engine
from core.connectors.base import (
    CDCEngine,
    Schema,
    SourceConnector,
    TargetConnector,
    UpsertResult,
)
from core.migration_plan import MigrationPlan, PostgresMigrationPlanner
from core.schema_mapping.registry import TypeMappingRegistry
from core.secrets import create_secret_provider
from core.status_server import StatusServer
from core.validator import Validator


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
        self._secret_resolver = create_secret_provider(config)
        self._status = status_server

    def _resolve_secrets(self, config: dict[str, Any]) -> dict[str, Any]:
        if self._secret_resolver is None:
            return config
        resolved = dict(config)
        if "password_secret" in resolved:
            resolved["password"] = self._secret_resolver.resolve(resolved["password_secret"])
        return resolved

    def _update_status(self, phase: str, progress: int, errors: list[str]) -> None:
        if self._status is not None:
            self._status.update_status(phase, progress, errors)

    def _record_preflight(self, preflight: dict[str, Any]) -> None:
        if self._status is not None and hasattr(self._status, "record_preflight"):
            self._status.record_preflight(preflight)

    def _estimate_total_rows(
        self,
        objects: list[str],
        schema_map: dict[str, str | None] | None = None,
    ) -> int:
        total = 0
        for obj_name in objects:
            try:
                schema_name = schema_map.get(obj_name) if schema_map else None
                total += self._source.get_object_count(
                    obj_name,
                    schema_name=schema_name,
                )
            except Exception:
                pass
        return total

    def _record_object_failure(
        self,
        failures: list[dict[str, str]],
        all_errors: list[str],
        phase: str,
        object_name: str,
        error: Exception | str,
    ) -> str:
        message = f"{phase} failed for object {object_name!r}: {error}"
        failures.append({"phase": phase, "object": object_name, "error": str(error)})
        all_errors.append(message)
        audit_log(
            phase=phase,
            status="error",
            details={"object": object_name, "error": str(error)},
        )
        return message

    @staticmethod
    def _routine_failure_message(kind: str, error: Exception) -> str:
        text = str(error)
        if "error 1419" in text.lower() or "log_bin_trust_function_creators" in text.lower():
            return (
                f"{kind.upper()}: BLOCKED\n"
                "Please ask a MySQL administrator to run once:\n"
                "SET PERSIST log_bin_trust_function_creators = ON;\n"
                "This server configuration persists across MySQL restarts.\n"
                "Then re-run the migration."
            )
        return f"{kind.upper()}: FAILED\n{text}"

    @staticmethod
    def _full_migration_status(
        validation: dict[str, Any],
        all_errors: list[str],
        failed_objects: set[str],
        object_summary: dict[str, Any],
    ) -> str:
        if (
            all_errors
            or failed_objects
            or object_summary.get("failed", 0) > 0
            or object_summary.get("blocked", 0) > 0
        ):
            return "partial_success"
        return validation.get("status", "unknown")

    def run_full(self) -> dict[str, Any]:
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
            plan = self._build_postgresql_plan()
            if plan is not None:
                result["preflight"] = plan.to_dict()
            else:
                self._resolve_connector_secrets(self._source, self._config.get("source", {}))
                self._resolve_connector_secrets(self._target, self._config.get("target", {}))
                self._apply_schema_scope(self._source)
                self._source.connect()
                self._target.connect()
            result["phases"]["connect"] = "success"
            self._update_status("connect", 2, [])

            # Snapshot scheduled events before table/data work.  MySQL changes
            # a preserved one-time event to DISABLED after it fires, so late
            # discovery cannot represent migration-start state.
            source_events = self._source.list_events()
            event_safety_lead_seconds = int(
                self._config.get("migration", {}).get("one_time_event_safety_lead_seconds", 300)
            )
            for event in source_events:
                event.safety_lead_seconds = event_safety_lead_seconds
            result["phases"]["snapshot_events"] = {
                event.name: {
                    "event_type": event.event_type,
                    "status": event.status,
                    "execute_at": str(event.execute_at) if event.execute_at else None,
                    "time_zone": event.time_zone,
                }
                for event in source_events
            }

            if plan is not None:
                self._record_preflight(plan.to_dict())
                if not plan.ready:
                    result["status"] = "blocked"
                    result["error"] = "Migration preflight contains blockers; no DDL or DML was performed."
                    audit_log(
                        phase="preflight",
                        status="blocked",
                        details={"plan": plan.to_dict()},
                    )
                    return result

            self._target.ensure_database_exists()
            result["phases"]["ensure_database"] = "success"
            self._update_status("ensure_database", 4, [])

            objects = self._source.list_objects()
            result["phases"]["discover"] = {"objects": objects}

            # ---------- Phase 1: Extensions ----------
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
            self._update_status("extensions", 6, all_errors)

            # ---------- Phase 2: Schemas ----------
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
            self._update_status("schemas", 8, all_errors)

            # ---------- Phase 3: Custom Types ----------
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
            except AttributeError:
                # Non-PostgreSQL sources don't have list_all_sequences — skip silently
                pass
            except Exception as exc:
                seq_create_results["_error"] = str(exc)
            result["phases"]["create_sequences"] = seq_create_results
            self._update_status("create_sequences", 12, all_errors)

            # ---------- Phase 4: Create Tables ----------
            all_schemas: dict[str, Any] = {}
            table_creation_results: dict[str, str | None] = {}
            object_failures: list[dict[str, str]] = []
            failed_objects: set[str] = set()
            result["phases"]["object_failures"] = object_failures
            for obj_name in objects:
                try:
                    schema = self._source.get_schema(obj_name)
                    self._apply_field_mappings(schema)
                    table_creation_results[obj_name] = self._target.create_object_if_missing(schema)
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
            self._update_status("create_tables", 16, all_errors)

            # Opt-in MySQL reconciliation is deliberately scoped to source
            # partition mismatches. The target connector stages and verifies a
            # replacement before the atomic swap, retaining the old table as a
            # backup until the complete run has succeeded.
            reconcile_results: dict[str, str] = {}
            if (
                self._config.get("migration", {}).get("reconcile_target_schema", False)
                and self._config.get("source", {}).get("engine") == "mysql"
                and self._config.get("target", {}).get("engine") == "mysql"
            ):
                for table_name, schema in all_schemas.items():
                    if table_creation_results.get(table_name) != "already_exists" or not schema.mysql_partitions:
                        continue
                    target_schema = self._target.inspect_schema(table_name)
                    if target_schema is not None and self._mysql_partition_signature(schema) == self._mysql_partition_signature(target_schema):
                        reconcile_results[table_name] = "verified_existing"
                        continue
                    try:
                        self._target.reconcile_mysql_table(schema, set(all_schemas))
                        verified = self._target.inspect_schema(table_name)
                        if verified is None or self._mysql_partition_signature(schema) != self._mysql_partition_signature(verified):
                            raise RuntimeError("recreated target partition metadata does not match source")
                        table_creation_results[table_name] = "reconciled"
                        reconcile_results[table_name] = "recreated_and_verified"
                    except Exception as exc:
                        message = f"partition reconciliation failed for {table_name}: {exc}"
                        all_errors.append(message)
                        reconcile_results[table_name] = f"failed: {message}"
            result["phases"]["reconcile_target_schema"] = reconcile_results

            # ---------- Phase 4.5: Create Partition Children ----------
            partition_results: dict[str, str] = {}
            try:
                if self._config.get("source", {}).get("engine") == "mysql":
                    for table_name, schema in all_schemas.items():
                        source_partitions = getattr(schema, "mysql_partitions", [])
                        if not source_partitions:
                            continue
                        target_schema = self._target.inspect_schema(table_name)
                        if target_schema is not None and self._mysql_partition_signature(schema) == self._mysql_partition_signature(target_schema):
                            outcome = (
                                "created (table DDL)"
                                if table_creation_results.get(table_name) == "created"
                                else "reconciled and verified"
                                if table_creation_results.get(table_name) == "reconciled"
                                else "verified_existing (partition signature matches source)"
                            )
                            for partition in source_partitions:
                                partition_results[f"{table_name}.{partition.name}"] = outcome
                            continue

                        mismatch = self._mysql_partition_mismatch_message(schema, target_schema)
                        all_errors.append(mismatch)
                        for partition in source_partitions:
                            partition_results[f"{table_name}.{partition.name}"] = f"failed: {mismatch}"
                else:
                    partitions = self._source.list_partitions()
                    for part in partitions:
                        try:
                            self._target.create_partition(part)
                            partition_results[part.name] = f"created (parent: {part.parent_table})"
                        except Exception as exc:
                            partition_results[part.name] = f"skipped: {exc}"
            except AttributeError:
                # Non-PostgreSQL sources don't have list_partitions — skip silently
                pass
            except Exception as exc:
                partition_results["_error"] = str(exc)
            result["phases"]["create_partitions"] = partition_results
            self._update_status("create_partitions", 17, all_errors)

            # ---------- Phase 5: Migrate Data ----------
            # Fresh targets have no triggers yet (they are created in Phase 14).
            # On rerun MySQL triggers already exist and would otherwise record
            # migration DML as application activity.  Connector-specific
            # suspension is deliberately limited to source trigger names.
            source_triggers = self._source.get_all_triggers()
            suspended_triggers = self._target.suspend_triggers_for_data_load(source_triggers)
            result["phases"]["trigger_data_load_handling"] = {
                "suspended": [t.name for t in suspended_triggers],
                "strategy": "drop-and-recreate-after-data-load" if suspended_triggers else "fresh-target-no-triggers",
            }
            # Full mode is replacement synchronization, not an incremental
            # upsert.  Clear every migrated target table before loading so
            # target-only rows (including rows from past trigger side effects)
            # cannot survive a successful run.  MySQL uses FK-safe DELETEs.
            cleared_objects = self._target.clear_objects_for_full_sync(list(all_schemas))
            result["phases"]["full_target_sync"] = {
                "strategy": "clear-before-load",
                "cleared": cleared_objects,
            }
            # Parents must be loaded before children because existing target
            # foreign keys are deliberately retained during a full rerun.
            # Discovery order is not a dependency order (e.g. ``orders`` may
            # precede ``products`` alphabetically).
            remaining = dict(all_schemas)
            ordered_schemas: list[tuple[str, Schema]] = []
            while remaining:
                ready = []
                for name, schema in remaining.items():
                    dependencies = {
                        fk.ref_table.rsplit(".", 1)[-1]
                        for fk in getattr(schema, "foreign_keys", [])
                        if fk.ref_table.rsplit(".", 1)[-1] in remaining
                    }
                    if not dependencies:
                        ready.append(name)
                # A cycle cannot be satisfied by ordering; preserve discovery
                # order so the database reports the actual constraint failure.
                if not ready:
                    ready = [next(iter(remaining))]
                for name in ready:
                    ordered_schemas.append((name, remaining.pop(name)))

            schema_map = {name: (s.schema_name if hasattr(s, "schema_name") else None) for name, s in all_schemas.items()}
            total_rows = self._estimate_total_rows(objects, schema_map)
            processed_rows = 0
            for idx, (obj_name, schema) in enumerate(ordered_schemas, start=1):
                upsert_result = UpsertResult()
                count: int | None = None
                try:
                    count = self._source.get_object_count(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None)
                    rows = self._source.export_full(obj_name, schema_name=schema.schema_name if hasattr(schema, "schema_name") else None)
                    for chunk in self._chunked(rows, self._batch_size):
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
                            f"data: {obj_name} ({idx}/{len(ordered_schemas)})", progress, all_errors,
                        )

                    if upsert_result.failure_count:
                        details = "; ".join(upsert_result.errors) or (
                            f"target reported {upsert_result.failure_count} failed row(s)"
                        )
                        message = self._record_object_failure(
                            object_failures, all_errors, "data_load", obj_name, details
                        )
                        failed_objects.add(obj_name)
                        upsert_result.errors.append(message)
                        if self._stop_on_error:
                            raise RuntimeError(message)
                except Exception as exc:
                    if obj_name not in failed_objects:
                        message = self._record_object_failure(
                            object_failures, all_errors, "data_load", obj_name, exc
                        )
                        failed_objects.add(obj_name)
                        upsert_result.errors.append(message)
                    if self._stop_on_error:
                        raise

                result["phases"][obj_name] = {
                    "source_rows": count or 0,
                    "success": upsert_result.success_count,
                    "failure": max(upsert_result.failure_count, int(obj_name in failed_objects)),
                    "errors": upsert_result.errors,
                }

            # ---------- Phase 6+7+8: Indexes, FKs, Check Constraints ----------
            constraint_results: dict[str, str] = {}
            for obj_name, schema in all_schemas.items():
                try:
                    self._target.apply_constraints(schema)
                    constraint_results[obj_name] = "success"
                except Exception as exc:
                    constraint_results[obj_name] = f"error: {exc}"
                    all_errors.append(str(exc))
            result["phases"]["apply_constraints"] = constraint_results
            self._update_status("apply_constraints", 60, all_errors)

            # MySQL AUTO_INCREMENT is table-bound rather than a standalone
            # sequence, so synchronize it after explicit source IDs are loaded.
            auto_increment_results: dict[str, str] = {}
            for obj_name, schema in all_schemas.items():
                for column in schema.columns:
                    if getattr(column, "auto_increment", False):
                        try:
                            self._target.sync_auto_increment(obj_name, column.name)
                            auto_increment_results[f"{obj_name}.{column.name}"] = "synchronized"
                        except Exception as exc:
                            auto_increment_results[f"{obj_name}.{column.name}"] = f"error: {exc}"
                            all_errors.append(str(exc))
            result["phases"]["auto_increment"] = auto_increment_results

            # ---------- Phase 9: Sequence Ownership ----------
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
            except Exception as exc:
                seq_advance_results["_error"] = [str(exc)]
            result["phases"]["advance_sequences"] = seq_advance_results
            self._update_status("advance_sequences", 66, all_errors)

            # ---------- Phase 11: Views ----------
            view_results: dict[str, str] = {}
            try:
                for view in self._source.list_views():
                    try:
                        self._target.create_view(view)
                        view_results[view.name] = "created"
                    except Exception as exc:
                        view_results[view.name] = f"error: {exc}"
                        all_errors.append(str(exc))
            except Exception as exc:
                view_results["_error"] = str(exc)
            result["phases"]["views"] = view_results
            self._update_status("views", 70, all_errors)

            # ---------- Phase 12: Materialized Views ----------
            mv_results: dict[str, str] = {}
            try:
                for mv in self._source.list_materialized_views():
                    try:
                        self._target.create_materialized_view(mv)
                        self._target.refresh_materialized_view(mv.name, schema_name=mv.schema_name)
                        mv_results[mv.name] = "created+refreshed"
                    except Exception as exc:
                        mv_results[mv.name] = f"error: {exc}"
                        all_errors.append(str(exc))
            except Exception as exc:
                mv_results["_error"] = str(exc)
            result["phases"]["materialized_views"] = mv_results
            self._update_status("materialized_views", 75, all_errors)

            # ---------- Phase 13: Functions & Stored Procedures ----------
            func_results: dict[str, str] = {}
            try:
                routines = self._source.list_functions()
                result["object_inventory"] = {
                    "functions": sum(func.kind == "function" for func in routines),
                    "procedures": sum(func.kind == "procedure" for func in routines),
                    "routine_kinds": {},
                }
                for func in routines:
                    func_key = (
                        func.name if func.schema_name == "public"
                        else f"{func.schema_name}.{func.name}"
                    )
                    result["object_inventory"]["routine_kinds"][func_key] = func.kind
                    try:
                        self._target.create_function(func)
                        func_results[func_key] = "created"
                    except Exception as exc:
                        failure_message = self._routine_failure_message("FUNCTION", exc)
                        func_results[func_key] = failure_message
                        all_errors.append(failure_message)
            except Exception as exc:
                func_results["_error"] = str(exc)
            result["phases"]["functions"] = func_results
            self._update_status("functions", 80, all_errors)

            # ---------- Phase 14: Triggers ----------
            trigger_results: dict[str, str] = {}
            try:
                for trigger in source_triggers:
                    trigger_key = (
                        f"{trigger.table}.{trigger.name}"
                        if trigger.schema_name == "public"
                        else f"{trigger.schema_name}.{trigger.table}.{trigger.name}"
                    )
                    try:
                        self._target.create_trigger(trigger)
                        trigger_results[trigger_key] = "created"
                    except Exception as exc:
                        failure_message = self._routine_failure_message("TRIGGER", exc)
                        trigger_results[trigger_key] = failure_message
                        all_errors.append(failure_message)
            except Exception as exc:
                trigger_results["_error"] = str(exc)
            result["phases"]["triggers"] = trigger_results
            self._update_status("triggers", 85, all_errors)

            # ---------- Phase 14.5: Events (MySQL/MariaDB) ----------
            event_results: dict[str, str] = {}
            try:
                for event in source_events:
                    try:
                        self._target.create_event(event)
                        event_results[event.name] = "created"
                    except Exception as exc:
                        event_results[event.name] = str(exc) if "BLOCKED" in str(exc).upper() else f"error: {exc}"
                        all_errors.append(str(exc))
            except Exception as exc:
                event_results["_error"] = str(exc)
                all_errors.append(str(exc))
            result["phases"]["events"] = event_results

            # ---------- Phase 15: Comments ----------
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
            self._update_status("comments", 88, all_errors)

            # ---------- Phase 16: Grants ----------
            # Connector-owned security support is deliberately capability-gated:
            # non-MySQL targets retain their existing migration behaviour.
            security_results: dict[str, str] = {}
            if self._target.get_capabilities().get("security_principals", {}).get("supported", False):
                try:
                    scope_status = self._source.security_scope_status()
                    authorized, authorization_message = self._target.security_migration_authorization()
                    if not authorized:
                        security_results["_status"] = "SKIPPED_NOT_AUTHORIZED"
                        security_results["_detail"] = authorization_message
                    elif scope_status and scope_status.startswith("OUT_OF_SCOPE"):
                        security_results["_status"] = scope_status
                    else:
                        if scope_status:
                            security_results["_scope"] = scope_status
                        principals = self._source.list_security_principals()
                        for principal in principals:
                            key = f"{principal.principal_type} {principal.user}@{principal.host}"
                            try:
                                self._target.create_security_principal(principal)
                                security_results[key] = "created"
                            except Exception as exc:
                                security_results[key] = f"failed: {exc}"
                        for membership in self._source.list_role_memberships():
                            key = f"{membership.role_user}@{membership.role_host} TO {membership.grantee_user}@{membership.grantee_host}"
                            try:
                                self._target.apply_role_membership(membership)
                                security_results[key] = "applied"
                            except Exception as exc:
                                security_results[key] = f"failed: {exc}"
                        for grant in self._source.list_security_grants():
                            key = f"{grant.object_type} {grant.object_name} TO {grant.grantee}"
                            try:
                                self._target.apply_grant(grant)
                                security_results[key] = "applied"
                            except Exception as exc:
                                security_results[key] = f"failed: {exc}"
                except Exception as exc:
                    security_results["_error"] = str(exc)
            else:
                security_results["_status"] = "out_of_scope"
            result["phases"]["security_principals"] = security_results
            self._update_status("security_principals", 90, all_errors)
            grant_results: dict[str, str] = {}
            try:
                for grant in self._source.list_grants():
                    grant_key = (
                        f"{grant.object_name} TO {grant.grantee}"
                        if grant.schema_name == "public"
                        else f"{grant.schema_name}.{grant.object_name} TO {grant.grantee}"
                    )
                    try:
                        self._target.apply_grant(grant)
                        grant_results[grant_key] = "applied"
                    except Exception as exc:
                        grant_results[grant_key] = f"skipped: {exc}"
            except Exception as exc:
                grant_results["_error"] = str(exc)
            result["phases"]["grants"] = grant_results
            self._update_status("grants", 91, all_errors)

            partition_testing = self._build_partition_testing(all_schemas, result["phases"])
            result["phases"]["partition_testing"] = partition_testing
            result["partition_testing"] = partition_testing

            result["object_migration"] = self._build_object_migration_summary(
                all_schemas, result["phases"], result.get("object_inventory", {})
            )
            if self._status is not None and hasattr(self._status, "record_object_migration"):
                self._status.record_object_migration(result["object_migration"])

            # ---------- Phase 17: Validate ----------
            self._update_status("validation", 94, all_errors)
            validation_objects = [
                obj_name for obj_name in all_schemas if obj_name not in failed_objects
            ]
            validation = self.validate(validation_objects, schema_map=schema_map)
            result["phases"]["validation"] = validation
            # Preserve the old table until all supported phases and validation
            # have completed. Cleanup failure is reported rather than hidden.
            if not all_errors and validation.get("status") == "success":
                try:
                    result["phases"]["finalize_reconciliation"] = self._target.finalize_schema_reconciliations()
                except Exception as exc:
                    all_errors.append(f"reconciliation backup cleanup failed: {exc}")
            result["status"] = self._full_migration_status(
                validation,
                all_errors,
                failed_objects,
                result["object_migration"],
            )
            self._update_status("completed", 100, all_errors)

        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            audit_log(phase="run_full", status="failed", details={"error": str(exc)})
            self._update_status("failed", 0, all_errors + [str(exc)])
            if self._notifier is not None:
                self._notifier.notify({"phase": "run_full", "status": "failed", "details": {"error": str(exc)}})

        self._close_connectors()

        end_time = time.time()
        audit_log(phase="run_full", status="completed", details={
            "status": result.get("status", "unknown"),
            "duration_s": round(end_time - start_time, 2),
        })
        return result

    def _build_partition_testing(
        self, schemas: dict[str, Schema], phases: dict[str, Any]
    ) -> dict[str, Any]:
        """Compare source and target MySQL partition definitions and row counts."""
        entries: list[dict[str, Any]] = []
        target_engine = self._config.get("target", {}).get("engine")
        if target_engine and target_engine.casefold() != "mysql":
            return {"status": "NOT_APPLICABLE", "tables": []}
        inspect_schema = getattr(self._target, "inspect_schema", None)
        for table_name, source_schema in schemas.items():
            if not getattr(source_schema, "mysql_partition_method", None):
                continue
            entry: dict[str, Any] = {
                "table": table_name,
                "source": {
                    "partition_method": source_schema.mysql_partition_method,
                    "partition_expression": source_schema.mysql_partition_expression,
                    "partition_count": len(source_schema.mysql_partitions),
                    "partitions": [
                        {"name": p.name, "boundary": p.description}
                        for p in source_schema.mysql_partitions
                    ],
                    "row_count": phases.get(table_name, {}).get("source_rows", 0),
                },
                "target": {},
                "checks": {
                    "data_migration": "FAIL",
                    "structure_preservation": "FAIL",
                    "overall": "FAIL",
                },
                "status": "FAIL",
                "errors": [],
            }
            try:
                target_schema = inspect_schema(table_name) if callable(inspect_schema) else None
                if target_schema is None:
                    raise RuntimeError("target table metadata is unavailable")
                target_row_count = self._target.get_object_count(table_name)
                entry["target"] = {
                    "partition_method": target_schema.mysql_partition_method,
                    "partition_expression": target_schema.mysql_partition_expression,
                    "partition_count": len(target_schema.mysql_partitions),
                    "partitions": [
                        {"name": p.name, "boundary": p.description}
                        for p in target_schema.mysql_partitions
                    ],
                    "row_count": target_row_count,
                }
                phase = phases.get(table_name, {})
                data_ok = (
                    phase.get("failure", 0) == 0
                    and phase.get("success", 0) == entry["source"]["row_count"]
                    and target_row_count == entry["source"]["row_count"]
                )
                source_signature = self._mysql_partition_signature(source_schema)
                target_signature = self._mysql_partition_signature(target_schema)
                structure_ok = source_signature == target_signature
                entry["checks"] = {
                    "data_migration": "PASS" if data_ok else "FAIL",
                    "structure_preservation": "PASS" if structure_ok else "FAIL",
                    "overall": "PASS" if data_ok and structure_ok else "FAIL",
                }
                entry["status"] = entry["checks"]["overall"]
            except Exception as exc:
                entry["errors"].append(str(exc))
            entries.append(entry)

        overall = "PASS" if entries and all(e["status"] == "PASS" for e in entries) else (
            "FAIL" if entries else "NOT_APPLICABLE"
        )
        audit_log(
            phase="partition_testing",
            status=overall.casefold(),
            details={"tables": entries},
        )
        return {"status": overall, "tables": entries}

    @staticmethod
    def _mysql_partition_signature(schema: Schema) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """Comparable MySQL partition metadata, including ordered boundaries."""
        return (
            str(schema.mysql_partition_method or "").strip().casefold(),
            str(schema.mysql_partition_expression or "").strip().casefold(),
            tuple(
                (str(partition.name).strip().casefold(), (partition.description or "").strip().casefold())
                for partition in schema.mysql_partitions
            ),
        )

    @classmethod
    def _mysql_partition_mismatch_message(cls, source: Schema, target: Schema | None) -> str:
        source_signature = cls._mysql_partition_signature(source)
        if target is None:
            actual = "target table metadata is unavailable"
        elif not target.mysql_partition_method:
            actual = "target table is unpartitioned"
        else:
            actual = f"target signature is {cls._mysql_partition_signature(target)!r}"
        return (
            f"partition schema mismatch for {source.name}: expected signature "
            f"{source_signature!r}; {actual}. Existing target tables are not altered or recreated."
        )

    def _build_object_migration_summary(self, schemas: dict[str, Schema], phases: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
        """Create a capability-aware result without pair-specific orchestration."""
        target_capabilities = self._target.get_capabilities()
        if not isinstance(target_capabilities, dict):
            target_capabilities = {}
        counts = {
            "tables": len(schemas), "columns": sum(len(s.columns) for s in schemas.values()),
            "primary_keys": sum(bool(s.primary_key) for s in schemas.values()),
            "auto_increment": sum(c.auto_increment for s in schemas.values() for c in s.columns),
            "indexes": sum(len(s.indexes) for s in schemas.values()),
            "unique_constraints": sum(i.unique for s in schemas.values() for i in s.indexes),
            "foreign_keys": sum(len(s.foreign_keys) for s in schemas.values()),
            "check_constraints": sum(len(s.check_constraints) for s in schemas.values()),
            "generated_columns": sum(bool(c.generated) for s in schemas.values() for c in s.columns),
            "defaults": sum(c.default is not None for s in schemas.values() for c in s.columns),
            "partitions": sum(len(getattr(s, "mysql_partitions", [])) for s in schemas.values()) or len(phases.get("create_partitions", {})),
            "comments": sum(bool(s.comment) + sum(bool(c.comment) for c in s.columns) for s in schemas.values()),
            "grants": len(phases.get("grants", {})),
            "security_principals": len([value for key, value in phases.get("security_principals", {}).items() if not key.startswith("_")]),
            "views": len(phases.get("views", {})), "functions": inventory.get("functions", 0),
            "procedures": inventory.get("procedures", 0), "triggers": len(phases.get("triggers", {})), "events": len(phases.get("events", {})),
        }
        categories: dict[str, Any] = {}
        failed = 0
        blocked = 0
        for category, count in counts.items():
            capability = target_capabilities.get(category, {"supported": True, "mode": "direct"})
            phase_key = {"indexes": "apply_constraints", "unique_constraints": "apply_constraints", "foreign_keys": "apply_constraints", "check_constraints": "apply_constraints", "auto_increment": "auto_increment", "columns": "create_tables", "primary_keys": "create_tables", "defaults": "create_tables", "generated_columns": "create_tables", "partitions": "create_partitions"}.get(category, category)
            phase = phases.get(phase_key, {})
            if category in {"functions", "procedures"}:
                routine_kinds = inventory.get("routine_kinds", {})
                wanted = category[:-1]
                phase = {key: value for key, value in phase.items() if routine_kinds.get(key) == wanted}
            if isinstance(phase, dict):
                entries = list(phase.items())
            else:
                entries = [("_phase", phase)]
            blocked_entries = [value for _, value in entries if "blocked" in str(value).lower()]
            failed_entries = [
                value for _, value in entries
                if any(marker in str(value).lower() for marker in ("failed", "error:", "skipped:"))
                and "blocked" not in str(value).lower()
            ]
            blocked_count = len(blocked_entries)
            failed_count = len(failed_entries)
            unsupported_count = count if count and not capability.get("supported", False) else 0
            if unsupported_count:
                status = "UNSUPPORTED"
            elif blocked_count:
                status = "BLOCKED"
            elif failed_count:
                status = "FAILED"
            else:
                status = "MIGRATED"
            migrated_count = max(count - blocked_count - failed_count - unsupported_count, 0)
            failed += failed_count
            blocked += blocked_count
            categories[category] = {
                "source_count": count,
                "migrated": migrated_count,
                "blocked": blocked_count,
                "unsupported": unsupported_count,
                "failed": failed_count,
                "status": status,
                "details": blocked_entries + failed_entries,
                "capability": capability,
            }
        return {
            "categories": categories,
            "failed": failed,
            "blocked": blocked,
            "unsupported": sum(v["unsupported"] for v in categories.values()),
        }

    def run_dry_run(self) -> dict[str, Any]:
        """Build and report a PostgreSQL full-migration plan without writing data."""
        run_id = get_run_id()
        set_run_id(run_id)
        audit_log(phase="dry_run", status="started", details={"run_id": run_id})

        plan = self._build_postgresql_plan()
        if plan is None:
            return {
                "run_id": run_id,
                "mode": "dry-run",
                "status": "blocked",
                "phases": {},
                "error": "Dry-run planning is currently supported only for PostgreSQL-to-PostgreSQL full migrations.",
            }

        status = "ready" if plan.ready else "blocked"
        result = {
            "run_id": run_id,
            "mode": "dry-run",
            "status": status,
            "phases": {"discover": {"objects": [obj.object_name for obj in plan.objects]}},
            "preflight": plan.to_dict(),
        }
        audit_log(phase="dry_run", status=status, details={"plan": plan.to_dict()})
        self._update_status("dry-run complete", 100, [])
        return result

    def _is_postgresql_full_migration(self) -> bool:
        return (
            self._config.get("source", {}).get("engine") == "postgresql"
            and self._config.get("target", {}).get("engine") == "postgresql"
        )

    def _build_postgresql_plan(self) -> MigrationPlan | None:
        if not self._is_postgresql_full_migration():
            return None
        self._resolve_connector_secrets(self._source, self._config.get("source", {}))
        self._resolve_connector_secrets(self._target, self._config.get("target", {}))
        self._apply_schema_scope(self._source)
        return PostgresMigrationPlanner(self._source, self._target, self._config).build()

    def _resolve_connector_secrets(self, connector: Any, config: dict[str, Any]) -> None:
        if self._secret_resolver is None:
            return
        # password_secret lives under config["connection"], not at the top-level config
        connection_config = config.get("connection", config)
        if "password_secret" in connection_config:
            connector._config["password"] = self._secret_resolver.resolve(connection_config["password_secret"])

    def _close_connectors(self) -> None:
        for connector in (self._source, self._target):
            try:
                connector.close()
            except Exception as exc:
                audit_log(
                    phase="close_connectors",
                    status="error",
                    details={"connector": type(connector).__name__, "error": str(exc)},
                )

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
            self._resolve_connector_secrets(self._source, self._config.get("source", {}))
            self._resolve_connector_secrets(self._target, self._config.get("target", {}))
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

            # Phase 4: Create Tables
            objects = self._source.list_objects()
            all_schemas: dict[str, Any] = {}
            for obj_name in objects:
                schema = self._source.get_schema(obj_name)
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
                pass
            except Exception as exc:
                partition_results["_error"] = str(exc)
            result["phases"]["create_partitions"] = partition_results
            self._update_status("create_partitions", 16, all_errors)

            # ---- Phase 5: Initial Data Sync ----
            schema_map = {name: (s.schema_name if hasattr(s, "schema_name") else None) for name, s in all_schemas.items()}
            total_rows = self._estimate_total_rows(objects, schema_map)
            processed_rows = 0
            for idx, obj_name in enumerate(objects, start=1):
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

            # Phase 14: Triggers
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

            # Phase 15: Comments
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
            grant_results: dict[str, str] = {}
            try:
                for grant in self._source.list_grants():
                    grant_key = (
                        f"{grant.object_name} TO {grant.grantee}"
                        if grant.schema_name == "public"
                        else f"{grant.schema_name}.{grant.object_name} TO {grant.grantee}"
                    )
                    try:
                        self._target.apply_grant(grant)
                        grant_results[grant_key] = "applied"
                    except Exception as exc:
                        grant_results[grant_key] = f"skipped: {exc}"
            except Exception as exc:
                grant_results["_error"] = str(exc)
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

        self._close_connectors()

        end_time = time.time()
        audit_log(phase="run_cdc", status="completed", details={"status": result.get("status", "unknown"), "duration_s": round(end_time - start_time, 2)})
        return result

    def run_assessment(self) -> AssessmentReport:
        try:
            self._source.connect()
            return self._assessment_gen.generate(
                self._config.get("source", {}).get("engine", "unknown"),
                self._config.get("target", {}).get("engine", "unknown"),
                self._source,
            )
        finally:
            self._close_connectors()

    def validate(self, objects: list[str] | None = None, schema_map: dict[str, str | None] | None = None) -> dict[str, Any]:
        validator = Validator(self._source, self._target)
        validation_mode = self._config.get("validation", {}).get("mode", "count")
        source_objects = objects if objects is not None else self._source.list_objects()

        results: dict[str, Any] = {
            "mode": validation_mode,
            "checks": {},
        }

        all_passed = True
        for obj_name in source_objects:
            schema_name = schema_map.get(obj_name) if schema_map else None
            check = validator.validate(obj_name, mode=validation_mode, schema_name=schema_name)
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
