"""Non-destructive Oracle acceptance setup and source fixture loading."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from core.connectors.oracle._models import OracleConnectionError, oracle_error_code
from tests.e2e.acceptance import E2EConfig, load_e2e_config, write_acceptance_report
from tests.e2e.oracle.config import (
    OracleE2EError,
    configured_source_table_allowlist,
    connect,
    connection_config,
    expected_row_counts,
    fixture_names_and_expected_tables,
    resolver_for_side,
    validated_oracle_context,
)
from tests.e2e.oracle.owner_provisioning import _check_identity
from tests.e2e.oracle.setup.fixture_loader import load_fixture, resolve_fixture_files

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


class _SetupFailure(OracleE2EError):
    """A sanitized setup failure with the report accumulated before aborting."""

    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def _safe_operation_error(exc: Exception, operation: str) -> str:
    if isinstance(exc, OracleConnectionError):
        return f"{operation} failed: {exc}"
    code = oracle_error_code(exc)
    if code is not None:
        return f"{operation} failed (ORA-{code:05d})"
    return f"{operation} failed ({type(exc).__name__}; details withheld)"


def _print_setup_summary(report: dict[str, Any], report_path: Path) -> None:
    complete = report.get("setup_complete")
    status = "COMPLETE" if complete is True else "FAILED" if complete is False else "UNKNOWN"
    source_service = report.get("source_service_name", report.get("service_name", "unknown"))
    target_service = report.get("target_service_name", source_service)
    print("=" * 60)
    print(f"[E2E SETUP] Engine: {str(report.get('engine', 'oracle')).upper()}")
    print(f"  Source owner: {report.get('source_owner', 'unknown')}  Service: {source_service}")
    print(f"  Target owner: {report.get('target_owner', 'unknown')}  Service: {target_service}")
    print("  Checks:")
    for check in report.get("checks", []):
        check_status = check.get("status", "UNKNOWN")
        print(f"    [{check_status}] {check.get('name', 'unnamed check')}")
        if check.get("message"):
            print(f"      {check['message']}")
        for fixture in check.get("scripts", []):
            fixture_ok = fixture.get("ok")
            fixture_status = (
                "PASS" if fixture_ok is True else "FAIL" if fixture_ok is False else "UNKNOWN"
            )
            print(f"      [{fixture_status}] {fixture.get('script', 'configured fixture')}")
            code = fixture.get("error_code")
            if isinstance(code, int):
                print(f"        Oracle error: ORA-{code:05d}")
        if "object_count" in check:
            print(f"      Object count: {check['object_count']}")
        if "objects" in check:
            print(f"      Objects: {', '.join(check['objects'])}")
        if "missing" in check:
            print(f"      Missing: {', '.join(check['missing']) or '(none)'}")
        if "unexpected" in check:
            print(f"      Unexpected: {', '.join(check['unexpected']) or '(none)'}")
    print(f"\n  SETUP: {status}")
    print("=" * 60)
    print(f"  Report: {report_path}")


def _object_count(connection: Any) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM USER_OBJECTS")
        row = cursor.fetchone()
        return int(row[0])
    finally:
        cursor.close()


def _table_names(connection: Any) -> set[str]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT t.table_name FROM user_tables t "
            "WHERE t.nested = 'NO' AND NOT EXISTS ("
            "SELECT 1 FROM user_mviews m WHERE m.mview_name = t.table_name) "
            "ORDER BY t.table_name"
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}
    finally:
        cursor.close()


def _row_count(connection: Any, table_name: str) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(f'SELECT COUNT(*) FROM "{table_name.upper()}"')
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def run_setup(cfg: E2EConfig, *, driver: Any | None = None) -> dict[str, Any]:
    """Validate both owners, verify preloaded data or load source fixtures.

    This function never removes existing objects. Fixture mode requires empty
    schemas. Preloaded mode reads and verifies the source and still requires an
    empty target.
    """
    owners, services = validated_oracle_context(cfg)

    fixture_names, expected_objects = fixture_names_and_expected_tables(
        cfg, allow_source_discovery=True
    )
    source_mode = cfg.raw.get("e2e", {}).get("source_mode", "fixtures")
    source_allowlist = configured_source_table_allowlist(cfg, expected_objects)
    raw_expected_counts = cfg.raw.get("e2e", {}).get("expected", {}).get("row_counts")
    source_row_expectations = (
        expected_row_counts(cfg, expected_objects)
        if source_mode == "preloaded" and expected_objects and raw_expected_counts is not None
        else {}
    )
    fixture_dir = (
        Path(cfg.to_abs_fixture_path(str(Path(__file__).parent)))
        if source_mode == "fixtures"
        else None
    )
    if source_mode == "fixtures":
        assert fixture_dir is not None
        resolve_fixture_files(fixture_dir, fixture_names)

    source_config = connection_config(
        cfg, "source", owners["source"], resolver_for_side(cfg.raw, "source")
    )
    target_config = connection_config(
        cfg, "target", owners["target"], resolver_for_side(cfg.raw, "target")
    )

    if driver is None:
        import oracledb as driver

    source_conn = target_conn = None
    report: dict[str, Any] = {
        "mode": "acceptance_setup",
        "engine": "oracle",
        "timestamp": datetime.now(UTC).isoformat(),
        "source_owner": owners["source"],
        "target_owner": owners["target"],
        "service_name": services["source"],
        "source_service_name": services["source"],
        "target_service_name": services["target"],
        "checks": [],
        "setup_complete": False,
    }
    report["checks"].append({"name": "configuration_safety", "status": "PASS"})
    try:
        try:
            source_conn = connect(source_config, driver)
            report["checks"].append(
                {
                    "name": "source_connection",
                    "status": "PASS",
                    "owner": owners["source"],
                    "service_name": services["source"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - convert driver errors to safe report detail
            message = _safe_operation_error(exc, "Oracle source connection")
            report["checks"].append(
                {
                    "name": "source_connection",
                    "status": "FAIL",
                    "owner": owners["source"],
                    "service_name": services["source"],
                    "message": message,
                }
            )
            raise _SetupFailure(message, report) from None
        try:
            target_conn = connect(target_config, driver)
            report["checks"].append(
                {
                    "name": "target_connection",
                    "status": "PASS",
                    "owner": owners["target"],
                    "service_name": services["target"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - convert driver errors to safe report detail
            message = _safe_operation_error(exc, "Oracle target connection")
            report["checks"].append(
                {
                    "name": "target_connection",
                    "status": "FAIL",
                    "owner": owners["target"],
                    "service_name": services["target"],
                    "message": message,
                }
            )
            raise _SetupFailure(message, report) from None

        # Canonical acceptance YAML pins the expected container separately
        # from the connect descriptor. Check the live session before fixture
        # DDL; configs without this optional field remain usable by legacy
        # offline harnesses, while the maintained LIVE config always sets it.
        owner_policy = cfg.raw.get("e2e", {}).get("owner_provisioning", {})
        if isinstance(owner_policy, dict):
            for side, connection in (("source", source_conn), ("target", target_conn)):
                side_policy = owner_policy.get(side, {})
                expected_container = (
                    side_policy.get("expected_container_name")
                    if isinstance(side_policy, dict)
                    else None
                )
                if expected_container:
                    try:
                        _check_identity(
                            connection,
                            username=owners[side],
                            service=services[side],
                            container=expected_container,
                        )
                    except Exception as exc:  # noqa: BLE001 - identity query failures are sanitized for the report
                        message = _safe_operation_error(exc, f"Oracle {side} PDB identity check")
                        report["checks"].append(
                            {"name": f"{side}_pdb_identity", "status": "FAIL", "message": message}
                        )
                        raise _SetupFailure(message, report) from None
                    report["checks"].append({"name": f"{side}_pdb_identity", "status": "PASS"})

        if source_mode == "fixtures":
            try:
                source_count = _object_count(source_conn)
            except Exception as exc:  # noqa: BLE001 - driver messages may contain details
                message = _safe_operation_error(exc, "Oracle source owner inspection")
                report["checks"].append(
                    {"name": "source_empty", "status": "FAIL", "message": message}
                )
                raise _SetupFailure(message, report) from None
            if source_count:
                message = (
                    f"Oracle source owner {owners['source']!r} already contains "
                    f"{source_count} objects; refusing to load fixtures"
                )
                report["checks"].append(
                    {"name": "source_empty", "status": "FAIL", "object_count": source_count}
                )
                raise _SetupFailure(message, report)
            report["checks"].append({"name": "source_empty", "status": "PASS", "object_count": 0})

        try:
            target_count = _object_count(target_conn)
        except Exception as exc:  # noqa: BLE001 - driver errors may contain connection details
            message = _safe_operation_error(exc, "Oracle target owner inspection")
            report["checks"].append({"name": "target_empty", "status": "FAIL", "message": message})
            raise _SetupFailure(message, report) from None
        if target_count:
            message = (
                f"Oracle target owner {owners['target']!r} already contains "
                f"{target_count} objects; refusing setup"
            )
            report["checks"].append(
                {"name": "target_empty", "status": "FAIL", "object_count": target_count}
            )
            raise _SetupFailure(message, report)
        report["checks"].append({"name": "target_empty", "status": "PASS", "object_count": 0})

        if source_mode == "fixtures":
            assert fixture_dir is not None
            loaded = load_fixture(source_conn, fixture_dir, fixture_names)
            fixture_check = {
                "name": "fixture_load",
                "status": "PASS" if loaded.all_ok else "FAIL",
                "scripts": loaded.results,
            }
            report["checks"].append(fixture_check)
            if not loaded.all_ok:
                failed = next((item for item in loaded.results if not item["ok"]), {})
                detail = f"Oracle fixture loading failed for {failed.get('script', 'configured fixture')}"
                code = failed.get("error_code")
                if isinstance(code, int):
                    detail += f" (ORA-{code:05d})"
                raise _SetupFailure(detail, report)
        else:
            report["checks"].append(
                {"name": "fixture_load", "status": "SKIP", "message": "preloaded source mode"}
            )
        try:
            actual_objects = _table_names(source_conn)
        except Exception as exc:  # noqa: BLE001 - driver errors may contain connection details
            message = _safe_operation_error(exc, "Oracle source table validation")
            report["checks"].append(
                {"name": "expected_objects", "status": "FAIL", "message": message}
            )
            raise _SetupFailure(message, report) from None
        expected_normalized = {name.upper() for name in expected_objects}
        if source_allowlist is not None:
            discovery_scope = {name.upper() for name in source_allowlist}
            missing = sorted(discovery_scope - actual_objects)
            selected_objects = actual_objects & discovery_scope
            excluded = sorted(actual_objects - discovery_scope)
            inventory_matches = not missing and bool(selected_objects)
        elif expected_normalized:
            missing = sorted(expected_normalized - actual_objects)
            selected_objects = actual_objects & expected_normalized
            excluded = sorted(actual_objects - expected_normalized)
            inventory_matches = actual_objects == expected_normalized
        else:
            missing = []
            selected_objects = actual_objects
            excluded = []
            inventory_matches = bool(selected_objects)
        if not inventory_matches:
            message = (
                "Oracle source table validation failed "
                f"(missing={missing}, unexpected={excluded}, "
                f"discovered={sorted(actual_objects)})"
            )
            report["checks"].append(
                {
                    "name": "source_tables",
                    "status": "FAIL",
                    "objects": sorted(selected_objects),
                    "missing": missing,
                    "unexpected": excluded,
                    "discovered": sorted(actual_objects),
                }
            )
            raise _SetupFailure(message, report)
        expected_objects = sorted(selected_objects)
        report["checks"].append(
            {
                "name": "source_tables",
                "status": "PASS",
                "objects": sorted(expected_objects),
                "discovered": sorted(actual_objects),
                "excluded": excluded,
                "scope": "allowlist"
                if source_allowlist is not None
                else ("configured_expectations" if expected_normalized else "schema_wide"),
            }
        )
        if source_mode == "preloaded":
            for table_name in expected_objects:
                try:
                    count = _row_count(source_conn, table_name)
                except Exception as exc:  # noqa: BLE001 - keep diagnostics sanitized
                    message = _safe_operation_error(
                        exc, f"Oracle source row count for {table_name}"
                    )
                    report["checks"].append(
                        {
                            "name": f"source_row_count:{table_name}",
                            "status": "FAIL",
                            "message": message,
                        }
                    )
                    raise _SetupFailure(message, report) from None
                expected_count = source_row_expectations.get(table_name)
                passed = expected_count is None or count == expected_count
                count_check = {
                    "name": f"source_row_count:{table_name}",
                    "status": "PASS" if passed else "FAIL",
                    "actual": count,
                    "comparison": "configured_expectation"
                    if expected_count is not None
                    else "discovered_baseline",
                }
                if expected_count is not None:
                    count_check["expected"] = expected_count
                report["checks"].append(count_check)
                if not passed:
                    raise _SetupFailure(
                        f"Oracle source row count validation failed for {table_name}", report
                    )
        report["checks"].append(
            {
                "name": "expected_objects",
                "status": "PASS",
                "objects": expected_objects,
                "excluded": excluded if source_allowlist is not None else [],
            }
        )
        report["setup_complete"] = True
        return report
    finally:
        for connection in (target_conn, source_conn):
            if connection is not None:
                with suppress(Exception):
                    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Non-destructive Oracle E2E setup")
    parser.add_argument("--config", help="Path to Oracle acceptance configuration")
    args = parser.parse_args()
    cfg = load_e2e_config(args.config)
    try:
        report = run_setup(cfg)
    except Exception as exc:  # noqa: BLE001 - never print raw driver/provider messages
        failed_report = getattr(exc, "report", None)
        if isinstance(failed_report, dict):
            path = Path(
                write_acceptance_report(
                    str(REPORTS_DIR / "setup_report.json"),
                    failed_report,
                    preserve_existing=True,
                )
            )
            _print_setup_summary(failed_report, path)
            raise SystemExit(1) from None
        code = oracle_error_code(exc)
        if code is not None:
            message = f"Oracle setup failed (ORA-{code:05d})"
        elif isinstance(exc, OracleE2EError):
            # These failures are constructed by the Oracle config/setup helpers
            # and contain configuration identifiers or sanitized diagnostics only.
            message = str(exc)
        elif isinstance(exc, (ValueError, TypeError)):
            message = str(exc)
        else:
            message = f"Oracle setup failed ({type(exc).__name__}; details withheld)"
        print(
            f"[SETUP] FAILED: {message}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    path = Path(
        write_acceptance_report(
            str(REPORTS_DIR / "setup_report.json"),
            report,
            preserve_existing=True,
        )
    )
    _print_setup_summary(report, path)
    raise SystemExit(0 if report["setup_complete"] else 1)


if __name__ == "__main__":
    main()
