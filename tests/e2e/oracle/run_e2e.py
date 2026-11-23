"""Single fail-fast Oracle E2E workflow using existing project entry points."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tests.e2e.acceptance import E2EConfig, load_e2e_config, write_acceptance_report
from tests.e2e.oracle import run_preflight, run_setup, run_validation
from tests.e2e.oracle.config import validated_oracle_context
from tests.e2e.oracle.dba_bootstrap import (
    grant_source_owner_catalog_access,
    grant_target_owner_catalog_access,
    validate_dba_bootstrap_config,
    verify_permission_executor,
)
from tests.e2e.oracle.dba_bootstrap import (
    run_dba_bootstrap as execute_dba_bootstrap,
)
from tests.e2e.oracle.owner_provisioning import (
    _fixture_privileges,
    _storage_policy,
    bootstrap_storage,
    provision,
    validate_onboarding_config,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
CORRECTIVE_ACTIONS = {
    "configuration": "Check endpoint services, owner mapping, expected containers, fixture manifest, and configured secret references.",
    "storage": "Ask the DBA to provide the approved permanent tablespace and datafile, finite quota, and required authority; configure explicit storage approval before retrying.",
    "provisioning": "Check owner_provisioning.enabled, its admin secret references, direct grant authority, PDB identity, and target emptiness. Existing users are never reset.",
    "permission_executor": "Verify the configured TARGET-local executor account and secret; existing accounts are never reset. Then resolve only the missing permission authority through the configured permission onboarding policy.",
    "preflight": "Resolve the reported owner identity, service/container, privilege, quota, or emptiness failure. Do not clean or reset schemas automatically.",
    "setup": "Use only empty configured owners. Inspect any partially loaded SOURCE or populated TARGET and select a separately approved empty test owner; this runner never resets schemas.",
    "migration": "Review the migration CLI report and Oracle object failures. Inspect the TARGET catalog before any retry; this runner performs no cleanup.",
    "validation": "Review the validation report and resolve every SOURCE/TARGET parity failure before accepting the E2E run.",
}


class WorkflowStageError(RuntimeError):
    def __init__(self, stage: str, cause: Exception):
        self.stage = stage
        self.cause = cause
        safe_detail = (
            str(cause) if isinstance(cause, (ValueError, RuntimeError)) else type(cause).__name__
        )
        super().__init__(f"{stage}: {safe_detail}")


def _stage(name: str, action: Callable[[], Any]) -> Any:
    print(f"[ORACLE E2E] START {name}")
    try:
        result = action()
    except Exception as exc:  # noqa: BLE001 - stage boundary reports a corrective action
        raise WorkflowStageError(name, exc) from None
    outcome = result.get("status") if isinstance(result, dict) else None
    print(f"[ORACLE E2E] {outcome if outcome == 'SKIP' else 'PASS'} {name}")
    return result


def _corrective_action(stage: str, detail: str) -> str:
    lowered = detail.casefold()
    if stage == "configuration" and "owner_provisioning.enabled" in lowered:
        return (
            "Configure both owner_provisioning sides with the expected container, approved admin "
            "secret reference, permanent tablespace, and finite quota; enable only after DBA approval."
        )
    if stage == "configuration" and "storage_bootstrap" in lowered:
        return "Leave bootstrap disabled for existing approved storage, or configure both explicit datafile paths and sizes and obtain DBA approval."
    if stage == "storage" and "missing" in lowered:
        return "Ask the DBA to provision an approved permanent tablespace, or enable the configured storage bootstrap with explicit paths/sizes and rerun the workflow with --apply."
    if stage == "provisioning" and ("grant authority" in lowered or "create user" in lowered):
        return "Ask the DBA to authorize CREATE USER and direct grant authority for the fixture-derived privileges, or have the DBA provision the missing owner."
    if stage in {"preflight", "setup"} and ("not empty" in lowered or "empty" in lowered):
        return "Use a separately approved empty SOURCE/TARGET owner pair; this workflow never drops or resets existing schemas."
    return CORRECTIVE_ACTIONS.get(
        stage,
        "Review the stage failure and correct its configuration or prerequisite.",
    )


def run_workflow(
    cfg: E2EConfig,
    *,
    config_path: str | Path,
    driver: Any,
    apply: bool,
    dba_bootstrap: bool = False,
    command_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run configuration, storage, provisioning, preflight, setup, FULL, validation."""
    if not apply:
        raise ValueError("The complete E2E workflow requires explicit --apply authorization")
    config_file = Path(config_path).resolve()
    stages: list[str] = []

    def record(stage_name: str, action: Callable[[], Any]) -> Any:
        result = _stage(stage_name, action)
        stages.append(stage_name)
        return result

    def validate_config() -> None:
        validated_oracle_context(cfg)
        validate_onboarding_config(cfg)
        preloaded = cfg.raw.get("e2e", {}).get("source_mode", "fixtures") == "preloaded"
        if dba_bootstrap:
            if preloaded:
                raise ValueError("--dba-bootstrap is unavailable in preloaded mode; use existing SOURCE/TARGET owners and storage")
            validate_dba_bootstrap_config(cfg)
        elif cfg.raw.get("e2e", {}).get("dba_bootstrap", {}).get("enabled") is True:
            raise ValueError(
                "e2e.dba_bootstrap.enabled requires the separate --dba-bootstrap opt-in"
            )
        _fixture_privileges(cfg)
        if cfg.raw.get("e2e", {}).get("storage_bootstrap", {}).get("enabled") is True:
            for side in ("source", "target"):
                _storage_policy(cfg, side)
        if cfg.raw.get("migration", {}).get("mode", "full").casefold() != "full":
            raise ValueError("migration.mode must be 'full' for the Oracle E2E workflow")

    record("configuration", validate_config)

    if dba_bootstrap:
        record(
            "dba_bootstrap",
            lambda: execute_dba_bootstrap(cfg, apply=apply),
        )

    if cfg.raw.get("oracle_permission_migration", {}).get("enabled") is True:
        record(
            "permission_executor",
            lambda: verify_permission_executor(cfg, driver=driver),
        )

    preloaded = cfg.raw.get("e2e", {}).get("source_mode", "fixtures") == "preloaded"
    if preloaded:
        record("storage", lambda: {"status": "SKIP", "reason": "preloaded mode reuses existing storage"})
        record("provisioning", lambda: {"status": "SKIP", "reason": "preloaded mode requires existing configured owners"})
    else:
        def storage() -> dict[str, str]:
            return bootstrap_storage(cfg, driver=driver, apply_storage=apply)

        record("storage", storage)

        def provision_owners() -> dict[str, Any]:
            return provision(cfg, driver=driver, apply=True)

        record("provisioning", provision_owners)

    approved_source_catalog_views = cfg.raw.get("e2e", {}).get("dba_bootstrap", {}).get(
        "source_owner_catalog_views_to_grant", []
    )
    if approved_source_catalog_views:
        record(
            "source_permission_catalog",
            lambda: grant_source_owner_catalog_access(cfg, apply=apply),
        )
    approved_target_catalog_views = cfg.raw.get("e2e", {}).get("dba_bootstrap", {}).get(
        "target_owner_catalog_views_to_grant", []
    )
    if approved_target_catalog_views:
        record(
            "target_replacement_catalog",
            lambda: grant_target_owner_catalog_access(cfg, apply=apply),
        )

    def preflight() -> dict[str, Any]:
        return run_preflight.run(cfg, driver=driver)

    record("preflight", preflight)

    def setup() -> dict[str, Any]:
        result = run_setup.run_setup(cfg, driver=driver)
        write_acceptance_report(
            str(REPORTS_DIR / "setup_report.json"), result, preserve_existing=True
        )
        if result.get("setup_complete") is not True:
            raise RuntimeError("Oracle setup did not report setup_complete")
        return result

    record("setup", setup)

    def migrate() -> dict[str, Any]:
        command = [
            sys.executable,
            "-m",
            "migration_platform",
            "--config",
            str(config_file),
            "--mode",
            "full",
            "--exit-after-run",
        ]
        result = command_runner(command, cwd=str(REPOSITORY_ROOT), check=False)
        if int(result.returncode) != 0:
            raise RuntimeError(f"migration CLI exited with status {result.returncode}")
        return {"returncode": result.returncode}

    record("migration", migrate)

    def validate() -> dict[str, Any]:
        result = run_validation.run_validation(cfg, driver=driver)
        write_acceptance_report(
            str(REPORTS_DIR / "validation_report.json"), result, preserve_existing=True
        )
        if result.get("result") != "PASS":
            raise RuntimeError("SOURCE/TARGET validation result is not PASS")
        return result

    record("validation", validate)
    return {
        "result": "PASS",
        "stages": stages,
        "completed_at": datetime.now(UTC).isoformat(),
        "config": str(config_file),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the configuration-driven Oracle SOURCE-to-TARGET E2E workflow"
    )
    parser.add_argument("--config", required=True, help="Oracle E2E YAML configuration")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Authorize owner provisioning, SOURCE fixture DDL, and FULL migration",
    )
    parser.add_argument(
        "--dba-bootstrap",
        action="store_true",
        help="Additionally authorize configured local SQL*Plus OS-SYSDBA onboarding DDL",
    )
    args = parser.parse_args()
    stage = "configuration"
    try:
        cfg = load_e2e_config(args.config)
        import oracledb

        stage = "workflow"
        result = run_workflow(
            cfg,
            config_path=args.config,
            driver=oracledb,
            apply=args.apply,
            dba_bootstrap=args.dba_bootstrap,
        )
    except WorkflowStageError as exc:
        action = _corrective_action(exc.stage, str(exc.cause))
        print(
            f"[ORACLE E2E] FAIL stage={exc.stage}: {exc}\n  Next action: {action}", file=sys.stderr
        )
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 - top-level boundary keeps credentials out of tracebacks
        safe_detail = (
            str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        )
        action = _corrective_action(stage, safe_detail)
        print(
            f"[ORACLE E2E] FAIL stage={stage}: {safe_detail}\n  Next action: {action}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"[ORACLE E2E] COMPLETE: {result['result']}; stages={', '.join(result['stages'])}")


if __name__ == "__main__":
    main()
