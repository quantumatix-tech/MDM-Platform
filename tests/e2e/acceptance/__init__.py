"""Acceptance E2E shared utilities.

Provides common helpers used by the per-engine acceptance setup and
validation runners (``run_setup.py`` / ``run_validation.py``).

This module contains **no engine-specific logic**.  Each engine supplies its
own connection, catalog, and validator classes; this module only handles
config loading, report formatting, JSON serialization, and safety-guard
helpers that are identical across engines.

Mode 2 — Real Migration / Acceptance E2E flow:

    SETUP  →  user runs actual migration CLI/UI  →  VALIDATION

The setup runner resets source + target DBs and loads deterministic fixtures
into source.  The validation runner then validates that the user's
externally-executed migration correctly reproduced the source state on the
target.  Neither runner performs migration itself.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "REPORTS_DIR",
    "E2EConfig",
    "fmt_phase",
    "fmt_source_target_counts",
    "json_default",
    "load_e2e_config",
    "write_acceptance_report",
]

REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports"
)


def _ensure_test_path() -> None:
    """Ensure the repository root is on ``sys.path`` so ``core`` imports work."""
    repo_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."
    )
    repo_root = os.path.abspath(repo_root)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


_ensure_test_path()


class E2EConfig:
    """Typed accessor over the raw YAML config dict.

    Provides convenient properties for the common E2E config sections so
    per-engine runners don't repeat verbose ``config["..."]["..."]`` chains.
    """

    def __init__(self, raw: dict) -> None:
        self.raw = raw

    @property
    def engine(self) -> str:
        return self.raw["e2e"]["engine"]

    @property
    def source_db(self) -> str:
        return self.raw["e2e"]["source_database"]

    @property
    def target_db(self) -> str:
        return self.raw["e2e"]["target_database"]

    @property
    def safe_pattern(self) -> str:
        return self.raw["e2e"]["safe_db_pattern"]

    @property
    def schema(self) -> str:
        return self.raw["e2e"].get("schema", "")

    @property
    def fixture_path(self) -> str:
        return self.raw["e2e"]["fixture_path"]

    @property
    def functional_enabled(self) -> bool:
        return self.raw["e2e"].get("functional", {}).get("enabled", True)

    @property
    def source_connection(self) -> dict[str, Any]:
        return self.raw["source"]["connection"]

    @property
    def target_connection(self) -> dict[str, Any]:
        return self.raw["target"]["connection"]

    def source_password_secret(self) -> str:
        return self.raw["source"]["connection"]["password_secret"]

    def target_password_secret(self) -> str:
        return self.raw["target"]["connection"]["password_secret"]

    def to_abs_fixture_path(self, config_dir: str) -> str:
        """Resolve *fixture_path* relative to the repository root.

        Acceptance configs specify ``fixture_path`` as a repo-root-relative path
        (e.g. ``tests/e2e/mysql/fixtures``), so we resolve from the repo root
        rather than applying additional relative traversal.

        The *config_dir* argument is accepted for backward compatibility but
        is not used — repo-root resolution makes the path unambiguous.
        """
        path = self.fixture_path
        if not os.path.isabs(path):
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
            )
            path = os.path.join(repo_root, path)
        return os.path.abspath(path)


def load_e2e_config(config_path: str | None = None) -> E2EConfig:
    """Load an E2E acceptance YAML config.

    If *config_path* is ``None``, falls back to ``E2E_CONFIG_PATH``
    environment variable, then to a default of
    ``config/<engine>_e2e_acceptance.yaml`` in the repo root.
    """
    import yaml

    if config_path is None:
        config_path = os.environ.get("E2E_CONFIG_PATH")
    if config_path is None:
        engine = os.environ.get("E2E_ENGINE", "mysql")
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            )))),
            "config", f"{engine}_e2e_acceptance.yaml",
        )
        if not os.path.isfile(config_path):
            raise FileNotFoundError(
                f"Config file not found: {config_path}\n"
                f"Set E2E_CONFIG_PATH or pass --config explicitly."
            )

    with open(config_path, encoding="utf-8") as f:
        return E2EConfig(yaml.safe_load(f))


def fmt_phase(phase) -> str:
    """Format a ``PhaseResult`` for terminal output.

    Reusable across all engine acceptance runners.
    """
    from tests.e2e.mssql.validation.models import STATUS_PASS

    failed = sum(1 for c in phase.checks if c.status != STATUS_PASS)
    total = len(phase.checks)
    if failed == 0:
        return f"  [+] {phase.name:<32} PASS  {total}/{total}"
    return f"  [!] {phase.name:<32} FAIL  {total - failed}/{total}"


def fmt_source_target_counts(
    src_rows: dict[str, int], tgt_rows: dict[str, int]
) -> str:
    """Format source/target row count comparison for terminal output."""
    lines = ["SOURCE", "TARGET"]
    all_tables = sorted(set(src_rows) | set(tgt_rows))
    for tbl in all_tables:
        s = src_rows.get(tbl, "?")
        t = tgt_rows.get(tbl, "?")
        match = "PASS" if s == t else "FAIL"
        lines.append(f"  [{match}] {tbl}: src={s}  tgt={t}")
    return "\n".join(lines)


def json_default(obj: Any) -> str:
    """JSON serializer for types not natively serializable."""
    if isinstance(obj, (Decimal, datetime, date)):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def write_acceptance_report(
    report_path: str,
    report: dict[str, Any],
) -> str:
    """Write an acceptance report dict as JSON.

    Returns the absolute path written.
    """
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=json_default)
    return report_path
