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
            self._resolve_connector_secrets(self._source, self._config.get("source", {}))
            self._resolve_connector_secrets(self._target, self._config.get("target", {}))
            self._apply_schema_scope(self._source)
            self._source.connect()
            self._target.connect()
            result["phases"]["connect"] = "success"
            self._update_status("connect", 2, [])

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
            object_failures: list[dict[str, str]] = []
            failed_objects: set[str] = set()
            result["phases"]["object_failures"] = object_failures
            for obj_name in objects:
                try:
                    schema = self._source.get_schema(obj_name)
                    self._apply_field_mappings(schema)
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
            self._update_status("create_tables", 16, all_errors)

            # ---------- Phase 4.5: Create Partition Children ----------
            partition_results: dict[str, str] = {}
            try:
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
            schema_map = {name: (s.schema_name if hasattr(s, "schema_name") else None) for name, s in all_schemas.items()}
            total_rows = self._estimate_total_rows(objects, schema_map)
            processed_rows = 0
            for idx, (obj_name, schema) in enumerate(all_schemas.items(), start=1):
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
                            f"data: {obj_name} ({idx}/{len(all_schemas)})", progress, all_errors,
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

            # ---------- Phase 9: Row-Level Security ----------
            rls_results: dict[str, Any] = {}
            try:
                for obj_name, schema in all_schemas.items():
                    if schema.rls_enabled:
                        rls_results[obj_name] = []
                        for policy in self._source.get_rls_policies(obj_name):
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
            self._update_status("functions", 80, all_errors)

            # ---------- Phase 14: Triggers ----------
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
            self._update_status("triggers", 85, all_errors)

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
            except Exception as exc:
                grant_results.append(f"_error: {exc}")
            result["phases"]["grants"] = grant_results
            self._update_status("grants", 91, all_errors)

            # ---------- Phase 17: Validate ----------
            self._update_status("validation", 94, all_errors)
            validation_objects = [
                obj_name for obj_name in all_schemas if obj_name not in failed_objects
            ]
            validation = self.validate(validation_objects, schema_map=schema_map)
            result["phases"]["validation"] = validation
            if failed_objects:
                result["status"] = "partial_success" if validation_objects else "failed"
            else:
                result["status"] = validation.get("status", "unknown")
            self._update_status("completed", 100, all_errors)

        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            audit_log(phase="run_full", status="failed", details={"error": str(exc)})
            self._update_status("failed", 0, all_errors + [str(exc)])
            if self._notifier is not None:
                self._notifier.notify({"phase": "run_full", "status": "failed", "details": {"error": str(exc)}})

        end_time = time.time()
        audit_log(phase="run_full", status="completed", details={
            "status": result.get("status", "unknown"),
            "duration_s": round(end_time - start_time, 2),
        })
        return result

    def _resolve_connector_secrets(self, connector: Any, config: dict[str, Any]) -> None:
        if self._secret_resolver is None:
            return
        # password_secret lives under config["connection"], not at the top-level config
        connection_config = config.get("connection", config)
        if "password_secret" in connection_config:
            connector._config["password"] = self._secret_resolver.resolve(connection_config["password_secret"])

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

            # Phase 9: Row-Level Security
            rls_results: dict[str, Any] = {}
            try:
                for obj_name, schema in all_schemas.items():
                    if schema.rls_enabled:
                        rls_results[obj_name] = []
                        for policy in self._source.get_rls_policies(obj_name):
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
