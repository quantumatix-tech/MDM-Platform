from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from threading import Thread

# Reconfigure stdout/stderr to UTF-8 on Windows (avoids cp1252 UnicodeEncodeError with rich)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.audit_logger import audit_log, configure_file_logging, get_run_id
from core.connectors import (
    CosmosMongoTargetConnector,
    MongoSourceConnector,
    MongoTargetConnector,
    MSSQLSourceConnector,
    MSSQLTargetConnector,
    MySQLSourceConnector,
    MySQLTargetConnector,
    PostgresSourceConnector,
    PostgresTargetConnector,
)
from core.orchestrator import MigrationOrchestrator
from core.migration_lock import MigrationAlreadyRunning, MigrationLock
from core.progress_display import create_progress_display
from core.reporting.report_builder import ReportBuilder
from core.status_server import StatusServer

SOURCE_CONNECTORS = {
    "postgresql": PostgresSourceConnector,
    "mongodb": MongoSourceConnector,
    "mysql": MySQLSourceConnector,
    "mssql": MSSQLSourceConnector,
}

TARGET_CONNECTORS = {
    "postgresql": PostgresTargetConnector,
    "mongodb": MongoTargetConnector,
    "mysql": MySQLTargetConnector,
    "mssql": MSSQLTargetConnector,
    "cosmos_mongo": CosmosMongoTargetConnector,
}


# ---------------------------------------------------------------------------
# Compound reporter — fans out updates to StatusServer + RichProgressDisplay
# ---------------------------------------------------------------------------
class _CompoundReporter:
    """Forwards all status and table-stats updates to every registered reporter."""

    def __init__(self, reporters: list) -> None:
        self._reporters = reporters

    def update_status(self, phase: str, progress: int, errors: list[str] | None = None) -> None:
        for r in self._reporters:
            if hasattr(r, "update_status"):
                r.update_status(phase, progress, errors)

    def record_table_stats(self, table: str, source_rows: int, success: int, failure: int) -> None:
        for r in self._reporters:
            if hasattr(r, "record_table_stats"):
                r.record_table_stats(table, source_rows, success, failure)

    def record_preflight(self, preflight: dict) -> None:
        for r in self._reporters:
            if hasattr(r, "record_preflight"):
                r.record_preflight(preflight)


def _format_object_result_lines(object_migration: dict) -> list[str]:
    lines = []
    for category, details in (object_migration.get("categories", {}) or {}).items():
        object_status = details.get("status", "UNKNOWN")
        color = "\033[91m" if object_status in {"BLOCKED", "FAILED"} else "\033[92m" if object_status == "MIGRATED" else ""
        reset = "\033[0m" if color else ""
        lines.append(
            f"    {category}: {color}{object_status}{reset} "
            f"(migrated={details.get('migrated', 0)}, "
            f"blocked={details.get('blocked', 0)}, "
            f"failed={details.get('failed', 0)})"
        )
        if object_status == "BLOCKED" and category in {"functions", "triggers"}:
            label = "FUNCTION" if category == "functions" else "TRIGGER"
            lines.extend([
                f"      {label}: BLOCKED",
                "      Please ask a MySQL administrator to run:",
                "      SET GLOBAL log_bin_trust_function_creators = ON;",
                "      Then re-run the migration.",
            ])
        else:
            lines.extend(f"      {detail}" for detail in dict.fromkeys(details.get("details", [])))
    return lines


def instantiate_connector(connector_cls, connection_config):
    return connector_cls(connection_config)


def _parse_security_accounts(values: list[str], flag: str) -> list[dict[str, str]]:
    """Parse repeated runtime user@host selections without reading secrets."""
    accounts: list[dict[str, str]] = []
    for value in values:
        user, separator, host = value.rpartition("@")
        if not separator or not user or not host:
            raise ValueError(f"{flag} must use user@host, for example security_test_user@%")
        accounts.append({"user": user, "host": host})
    return accounts


def _runtime_security_principals(users: list[str], roles: list[str]) -> dict[str, list[dict[str, str]]] | None:
    """Return an opt-in runtime scope; no selection deliberately means none."""
    if not users and not roles:
        return None
    return {
        "users": _parse_security_accounts(users, "--security-user"),
        "roles": _parse_security_accounts(roles, "--security-role"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Migration Platform — enterprise-grade data migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--mode",
        choices=["full", "cdc-incremental", "cdc-continuous"],
        default="full",
        help="Migration mode (default: full)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Status dashboard port; only 8080 is supported (default: 8080)",
    )
    parser.add_argument(
        "--no-live-ui", action="store_true",
        help="Disable rich terminal UI (raw JSON output)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build a PostgreSQL full-migration plan without DDL or DML",
    )
    parser.add_argument(
        "--security-user", action="append", default=[], metavar="USER@HOST",
        help="MySQL user account to migrate; repeat for each selected account",
    )
    parser.add_argument(
        "--security-role", action="append", default=[], metavar="ROLE@HOST",
        help="MySQL role to migrate; repeat for each selected role",
    )
    args = parser.parse_args()

    if args.port != 8080:
        raise SystemExit("The status dashboard must use port 8080; choose an available port 8080 and retry.")

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    runtime_security_principals = _runtime_security_principals(args.security_user, args.security_role)
    if runtime_security_principals is not None:
        config.setdefault("migration", {})["security_principals"] = runtime_security_principals

    source_cfg = config.get("source", {})
    target_cfg = config.get("target", {})
    source_type = source_cfg.get("engine")
    target_type = target_cfg.get("engine")
    dry_run = args.dry_run or config.get("migration", {}).get("dry_run", False)
    if dry_run and args.mode != "full":
        raise SystemExit("--dry-run is supported only with the full migration mode.")

    if source_type not in SOURCE_CONNECTORS:
        raise SystemExit(f"Unknown source engine: {source_type!r}. Available: {list(SOURCE_CONNECTORS)}")
    if target_type not in TARGET_CONNECTORS:
        raise SystemExit(f"Unknown target engine: {target_type!r}. Available: {list(TARGET_CONNECTORS)}")

    source_connection = dict(source_cfg.get("connection", {}))
    # Connectors own the interpretation of this optional, engine-neutral
    # migration scope.  MySQL uses it to avoid broad server-principal reads.
    source_connection["security_principals"] = config.get("migration", {}).get("security_principals")
    source = instantiate_connector(SOURCE_CONNECTORS[source_type], source_connection)
    target_connection = dict(target_cfg.get("connection", {}))
    target_connection["source_engine"] = source_type
    target = instantiate_connector(TARGET_CONNECTORS[target_type], target_connection)

    # ---- Audit log to file (suppress stdout noise when rich is active) ----
    use_live_ui = not args.no_live_ui
    log_path = configure_file_logging(log_dir="logs", suppress_stdout=use_live_ui)
    run_id = get_run_id()
    mode = args.mode
    migration_lock = MigrationLock(config, mode)
    try:
        migration_lock.acquire()
    except MigrationAlreadyRunning as exc:
        audit_log(phase="migration_lock", status="rejected", details={"scope_id": migration_lock.scope_id, "error": str(exc)})
        raise SystemExit(str(exc)) from exc
    audit_log(phase="migration_lock", status="acquired", details={"scope_id": migration_lock.scope_id})

    # ---- Status server (browser dashboard at http://localhost:{port}) ----
    status_server: StatusServer | None = None
    status_thread: Thread | None = None
    try:
        status_server = StatusServer(host="127.0.0.1", port=8080)
        status_server.start()
        status_server.set_mode(mode)
        status_thread = Thread(target=status_server.run, daemon=True)
        status_thread.start()

        # ---- Rich terminal progress display ----
        progress_display = create_progress_display(run_id, mode)

        # ---- Compound reporter (fans out to both) ----
        reporter = _CompoundReporter([status_server, progress_display])

        orchestrator = MigrationOrchestrator(source, target, config, status_server=reporter)
        start_time = time.time()

        with progress_display:
            if not use_live_ui:
                print(f"Live dashboard: http://localhost:{status_server.port}/")
                print(f"Audit log:      {log_path}")

            if dry_run:
                result = orchestrator.run_dry_run()
            elif args.mode == "full":
                result = orchestrator.run_full()
            elif args.mode == "cdc-incremental":
                result = orchestrator.run_cdc(max_iterations=1)
            elif args.mode == "cdc-continuous":
                result = orchestrator.run_cdc(max_iterations=None)
            else:
                raise SystemExit(f"Unsupported mode: {args.mode!r}")

            # ---- Push final table stats to both reporters ----
            phases = result.get("phases", {})
            objects = phases.get("discover", {}).get("objects", []) or []
            for obj in objects:
                od = phases.get(obj, {})
                src = od.get("source_rows", od.get("initial_sync_rows", 0)) or 0
                suc = od.get("success", 0) or 0
                fail = od.get("failure", 0) or 0
                reporter.record_table_stats(obj, src, suc, fail)
        end_time = time.time()

        # ---- Generate reports ----
        builder = ReportBuilder(result, start_time, end_time)
        html_path, json_path = builder.save()
        result["report"] = {"html": html_path, "json": json_path}

        # ---- Detect local network IP (so same-network colleagues can open reports) ----
        try:
            _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            local_ip = _s.getsockname()[0]
            _s.close()
        except Exception:
            local_ip = "localhost"

        # ---- Optional: upload to Azure Blob Storage for a public shareable URL ----
        azure_url: str | None = None
        azure_cfg = config.get("reporting", {}).get("azure_blob", {})
        if azure_cfg.get("connection_string"):
            try:
                from core.report_uploader import upload_report
                upload_result = upload_report(html_path, json_path, run_id, azure_cfg)
                azure_url = upload_result["html_url"]
                expiry = upload_result["expiry"][:10]  # just the date
                print(f"\n  [Azure] Report uploaded! Expires: {expiry}")
                print(f"  Shareable URL: {azure_url}")
            except Exception as exc:
                print(f"\n  [Azure] Upload skipped: {exc}")

        # ---- Final summary ----
        status_icon = "+" if result.get("status") in ("success", "completed") else "!"
        sep = "-" * 62
        print(f"\n{sep}")
        print(f"  {status_icon}  Migration {result.get('status', 'done').upper()}")
        print(sep)
        print(f"  Run ID      : {run_id}")
        print(f"  Mode        : {mode}")
        print(f"  Duration    : {round(end_time - start_time, 1)}s")
        print(f"  Audit Log   : {log_path}")
        print(f"  HTML Report : {os.path.abspath(html_path)}")
        print(f"  JSON Report : {os.path.abspath(json_path)}")
        print(sep)
        print("  Object Results:")
        for line in _format_object_result_lines(result.get("object_migration", {})):
            print(line)
        print(sep)
        print(f"  Local URL   : http://127.0.0.1:8080/reports/{os.path.basename(html_path)}")
        print(f"  All Reports : http://127.0.0.1:8080/reports/")
        if azure_url:
            print(f"  Public URL  : {azure_url}")
        print(sep)

        # ---- Open latest report automatically ----
        try:
            import subprocess
            subprocess.Popen(["explorer", os.path.abspath(html_path)], shell=True)
        except Exception:
            pass

        # ``--no-live-ui`` is a batch CLI mode, so it must return after reporting
        # instead of looking like a hung migration process.
        if not use_live_ui:
            return

        print("\n  Report server is running at http://127.0.0.1:8080/")
        print("  Share that URL with your team to view all migration reports.")
        print("  Press Ctrl+C to stop the server.\n")

        try:
            status_server.run()   # blocks until Ctrl+C
        except KeyboardInterrupt:
            print("\n  Server stopped.")
    finally:
        try:
            if status_server is not None:
                status_server.stop()
        finally:
            migration_lock.release()
            audit_log(phase="migration_lock", status="released", details={"scope_id": migration_lock.scope_id})


if __name__ == "__main__":
    main()
