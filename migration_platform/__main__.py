from __future__ import annotations

import argparse
import json
import os
import re
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

from core.audit_logger import configure_file_logging, get_run_id
from core.connectors import (
    CosmosMongoTargetConnector,
    MongoSourceConnector,
    MongoTargetConnector,
    MSSQLSourceConnector,
    MSSQLTargetConnector,
    MySQLSourceConnector,
    MySQLTargetConnector,
    OracleSourceConnector,
    OracleTargetConnector,
    PostgresSourceConnector,
    PostgresTargetConnector,
)
from core.orchestrator import MigrationOrchestrator
from core.preflight import assert_python_version
from core.progress_display import NoopProgressDisplay, create_progress_display
from core.reporting.report_builder import ReportBuilder
from core.runtime_manifest import build_runtime_manifest, save_manifest
from core.secrets import create_secret_provider
from core.status_server import StatusServer

from . import __version__

SOURCE_CONNECTORS = {
    "postgresql": PostgresSourceConnector,
    "mongodb": MongoSourceConnector,
    "mysql": MySQLSourceConnector,
    "mssql": MSSQLSourceConnector,
    "oracle": OracleSourceConnector,
}

TARGET_CONNECTORS = {
    "postgresql": PostgresTargetConnector,
    "mongodb": MongoTargetConnector,
    "mysql": MySQLTargetConnector,
    "mssql": MSSQLTargetConnector,
    "oracle": OracleTargetConnector,
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

    def prompt(self, message: str) -> str:
        for reporter in self._reporters:
            prompt = getattr(reporter, "prompt", None)
            if callable(prompt):
                return prompt(message)
        raise RuntimeError("Interactive approval is unavailable in this CLI session")


def instantiate_connector(connector_cls, connection_config):
    return connector_cls(connection_config)


def _safe_summary_text(value: object, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    text = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|token|access_key)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>", text,
    )
    text = re.sub(r"(://[^:/\s]+:)[^@/\s]+@", r"\1<redacted>@", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def print_object_failures(result: dict, limit: int = 5) -> None:
    """Print concise, credential-redacted object failures in the final summary."""
    failures = result.get("phases", {}).get("object_failures", []) or []
    if not failures:
        failures = [
            {"phase": "migration", "object": "<migration>", "error": error}
            for error in (result.get("errors", []) or [])
        ]
        if not failures and result.get("error"):
            failures = [{
                "phase": "migration", "object": "<migration>",
                "error": result["error"],
            }]
    if not failures:
        return
    print(f"  Object Errors : {len(failures)}")
    for failure in failures[:limit]:
        phase = _safe_summary_text(failure.get("phase", "migration"), 40)
        obj = _safe_summary_text(failure.get("object", "<unknown>"), 80)
        reason = _safe_summary_text(failure.get("error", "Migration failed"), 8192)
        print(f"    - [{phase}] {obj}: {reason}")
    if len(failures) > limit:
        print(f"    - … {len(failures) - limit} more; see JSON report for details")


def print_data_load_statuses(result: dict) -> None:
    """Print per-table data outcomes, including dependency and policy skips."""
    data = result.get("phases", {}).get("data", {}) or {}
    tables = data.get("tables", {}) if isinstance(data, dict) else {}
    if not tables:
        return
    counts = data.get("counts", {})
    print(
        "  Data Tables : "
        f"SUCCESS {counts.get('SUCCESS', 0)}, "
        f"FAILED {counts.get('FAILED', 0)}, "
        f"SKIPPED {counts.get('SKIPPED', 0)}"
    )
    for table, outcome in tables.items():
        status = _safe_summary_text(outcome.get("status", "UNKNOWN"), 20)
        reason = _safe_summary_text(outcome.get("reason", ""), 8192)
        print(f"    - [{status}] {_safe_summary_text(table, 120)}: {reason}")


def main():
    parser = argparse.ArgumentParser(
        description="Migration Platform — enterprise-grade data migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"migration-platform {__version__}",
        help="Show the Migration Platform version and exit",
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
        help="Status dashboard port (default: 8080)",
    )
    parser.add_argument(
        "--no-live-ui", action="store_true",
        help="Disable rich terminal UI (raw JSON output)",
    )
    parser.add_argument(
        "--exit-after-run",
        action="store_true",
        help="Exit after writing reports instead of keeping the status server open",
    )
    parser.add_argument(
        "--oracle-overwrite-plan", action="store_true",
        help="Print a read-only Oracle FULL schema replacement plan and exit",
    )
    parser.add_argument(
        "--oracle-permission-adopt-legacy", action="store_true",
        help="Explicitly review and approve legacy Oracle grants whose framework ownership is untracked",
    )
    args = parser.parse_args()

    # requires-python is only enforced by pip at install time; re-check at
    # startup so an interpreter downgraded after install fails fast.
    assert_python_version()

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    source_cfg = config.get("source", {})
    target_cfg = config.get("target", {})
    source_type = source_cfg.get("engine")
    target_type = target_cfg.get("engine")
    if args.oracle_permission_adopt_legacy and not (
        args.mode == "full" and source_type == "oracle" and target_type == "oracle"
    ):
        raise SystemExit("--oracle-permission-adopt-legacy requires Oracle-to-Oracle FULL mode")

    if source_type not in SOURCE_CONNECTORS:
        raise SystemExit(f"Unknown source engine: {source_type!r}. Available: {list(SOURCE_CONNECTORS)}")
    if target_type not in TARGET_CONNECTORS:
        raise SystemExit(f"Unknown target engine: {target_type!r}. Available: {list(TARGET_CONNECTORS)}")

    source_connection = dict(source_cfg.get("connection", {}))
    # security_users may be declared under migration: rather than under
    # source.connection:; merge it so the source connector can apply the
    # account allowlist without querying mysql.user.
    if "security_users" not in source_connection:
        migration_security_users = config.get("migration", {}).get("security_users")
        if migration_security_users is not None:
            source_connection["security_users"] = migration_security_users
    source = instantiate_connector(SOURCE_CONNECTORS[source_type], source_connection)
    target_connection = dict(target_cfg.get("connection", {}))
    target_connection["source_engine"] = source_type
    target = instantiate_connector(TARGET_CONNECTORS[target_type], target_connection)

    if args.oracle_overwrite_plan:
        orchestrator = MigrationOrchestrator(source, target, config)
        try:
            plan = orchestrator.plan_oracle_schema_replacement(mode=args.mode)
            print(json.dumps(plan, indent=2, sort_keys=True))
            return
        except Exception as exc:
            print(f"Oracle schema replacement planning failed: {exc}", file=sys.stderr)
            raise SystemExit(2) from None

    # ---- Audit log to file (suppress stdout noise when rich is active) ----
    use_live_ui = not args.no_live_ui
    log_path = configure_file_logging(log_dir="logs", suppress_stdout=use_live_ui)
    run_id = get_run_id()
    mode = args.mode

    # ---- Status server (browser dashboard at http://localhost:{port}) ----
    status_server = StatusServer(host="0.0.0.0", port=args.port)
    status_server.start()
    status_server.set_mode(mode)
    status_thread = Thread(target=status_server.run, daemon=True)
    status_thread.start()

    # ---- Rich terminal progress display ----
    progress_display = create_progress_display(run_id, mode)
    if args.no_live_ui:
        progress_display = NoopProgressDisplay()

    # ---- Compound reporter (fans out to both) ----
    reporter = _CompoundReporter([status_server, progress_display])

    orchestrator = MigrationOrchestrator(source, target, config, status_server=reporter)

    start_time = time.time()

    with progress_display:
        if not use_live_ui:
            print(f"Live dashboard: http://localhost:{args.port}/")
            print(f"Audit log:      {log_path}")

        if args.mode == "full":
            result = orchestrator.run_full(
                approve_legacy_permissions=args.oracle_permission_adopt_legacy
            )
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

    # ---- Build runtime manifest ----
    secret_provider = create_secret_provider(config)
    manifest = build_runtime_manifest(
        config=config,
        source_connector=source,
        target_connector=target,
        secret_provider=secret_provider,
    )
    manifest["run_id"] = run_id
    manifest_path = save_manifest(manifest)

    # ---- Generate reports ----
    builder = ReportBuilder(result, start_time, end_time, manifest=manifest)
    html_path, json_path = builder.save()
    result["report"] = {"html": html_path, "json": json_path, "manifest": manifest_path}

    # ---- Detect local network IP (so same-network colleagues can open reports) ----
    import socket
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
    print_object_failures(result)
    print_data_load_statuses(result)
    print(f"  Audit Log   : {log_path}")
    print(f"  HTML Report : {os.path.abspath(html_path)}")
    print(f"  JSON Report : {os.path.abspath(json_path)}")
    print(sep)
    print(f"  Local URL   : http://localhost:{args.port}/reports/{os.path.basename(html_path)}")
    print(f"  Network URL : http://{local_ip}:{args.port}/reports/{os.path.basename(html_path)}")
    print(f"  All Reports : http://{local_ip}:{args.port}/reports/")
    if azure_url:
        print(f"  Public URL  : {azure_url}")
    print(sep)

    if args.exit_after_run:
        status_server.stop()
        raise SystemExit(0 if result.get("status") in ("success", "completed") else 1)

    # ---- Open latest report automatically ----
    try:
        import subprocess
        subprocess.Popen(["explorer", os.path.abspath(html_path)], shell=True)
    except Exception:
        pass

    # ---- Keep report server alive permanently (Ctrl+C to stop) ----
    # Make the server thread non-daemon so the process stays alive.
    # This means http://localhost:{port}/reports/ remains accessible
    # until the user explicitly stops it.
    print(f"\n  Report server is running at http://{local_ip}:{args.port}/")
    print(f"  Share that URL with your team to view all migration reports.")
    print(f"  Press Ctrl+C to stop the server.\n")

    try:
        status_server.run()   # blocks until Ctrl+C
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    main()
