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
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

__all__ = [
    "REPORTS_DIR",
    "E2EConfig",
    "E2EResource",
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


@dataclass(frozen=True)
class E2EResource:
    """One independently configured acceptance target.

    ``database`` resources are the legacy acceptance resource. ``schema``
    resources represent a schema/owner inside the endpoint's configured
    database or service. ``owner`` is accepted as an input alias for schema.
    """

    kind: Literal["database", "schema"]
    name: str
    safe_pattern: str | None = None

    @property
    def matches_safety_pattern(self) -> bool:
        if not self.safe_pattern:
            return False
        try:
            return re.match(self.safe_pattern, self.name) is not None
        except re.error:
            return False


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
        resource = self.source_resource
        if resource.kind != "database":
            raise ValueError(
                "The source acceptance resource is a schema/owner, not a database"
            )
        return resource.name

    @property
    def target_db(self) -> str:
        resource = self.target_resource
        if resource.kind != "database":
            raise ValueError(
                "The target acceptance resource is a schema/owner, not a database"
            )
        return resource.name

    @property
    def safe_pattern(self) -> str:
        return self.raw["e2e"]["safe_db_pattern"]

    def _resource(self, side: str) -> E2EResource:
        if side not in {"source", "target"}:
            raise ValueError("Acceptance resource side must be 'source' or 'target'")
        e2e = self.raw["e2e"]
        resources = e2e.get("resources", {})
        if not isinstance(resources, dict):
            raise TypeError("e2e.resources must be a mapping with source/target entries")

        raw_resource = resources.get(side)
        if raw_resource is None:
            legacy_key = f"{side}_database"
            name = e2e.get(legacy_key)
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"Missing e2e.resources.{side} and legacy e2e.{legacy_key}"
                )
            pattern = e2e.get("safe_db_pattern")
            return E2EResource("database", name, pattern)

        if not isinstance(raw_resource, dict):
            raise TypeError(f"e2e.resources.{side} must be a mapping")
        kind = raw_resource.get("kind")
        if kind == "owner":
            kind = "schema"
        if kind not in {"database", "schema"}:
            raise ValueError(
                f"e2e.resources.{side}.kind must be 'database' or 'schema' (owner)"
            )
        name = raw_resource.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"e2e.resources.{side}.name must be a non-empty string")
        pattern = raw_resource.get("safe_pattern")
        if pattern is None and kind == "database":
            pattern = e2e.get("safe_db_pattern")
        if pattern is not None and not isinstance(pattern, str):
            raise ValueError(f"e2e.resources.{side}.safe_pattern must be a string")
        return E2EResource(kind, name, pattern)

    @property
    def source_resource(self) -> E2EResource:
        return self._resource("source")

    @property
    def target_resource(self) -> E2EResource:
        return self._resource("target")

    @property
    def resources(self) -> dict[str, E2EResource]:
        return {
            "source": self.source_resource,
            "target": self.target_resource,
        }

    @property
    def database_cleanup_pattern(self) -> str:
        """Return a safe database-only cleanup pattern or fail closed.

        Schema/owner cleanup needs its own engine adapter. This guard prevents
        existing database-drop adapters from receiving schema resources or an
        Oracle service name such as FREEPDB1.
        """
        resources = self.resources
        for side, resource in resources.items():
            if resource.kind != "database":
                raise ValueError(
                    f"Database cleanup is not available for {side} schema/owner "
                    f"resource {resource.name!r}"
                )
            endpoint_config = self.raw.get(side, {})
            if str(endpoint_config.get("engine", "")).casefold() == "oracle":
                raise ValueError(
                    f"Database cleanup is not supported for Oracle {side} endpoints; "
                    "Oracle acceptance targets are service/schema scoped"
                )
            endpoint = self.raw.get(side, {}).get("connection", {})
            service_name = endpoint.get("service_name")
            if (
                isinstance(service_name, str)
                and resource.name.casefold() == service_name.casefold()
            ):
                raise ValueError(
                    f"Refusing database cleanup for {side} resource {resource.name!r}: "
                    "it matches the endpoint service_name"
                )
            if not resource.matches_safety_pattern:
                raise ValueError(
                    f"Refusing database cleanup for {side} resource {resource.name!r}: "
                    "it does not match its safety pattern"
                )

        patterns = {resource.safe_pattern for resource in resources.values()}
        if len(patterns) == 1:
            return next(iter(patterns)) or ""
        # Existing adapters accept one pattern for both names. Narrow that
        # pattern to the two already validated resource names.
        names = "|".join(re.escape(resource.name) for resource in resources.values())
        return rf"^(?:{names})$"

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
    *,
    preserve_existing: bool = False,
) -> str:
    """Write an acceptance report dict as JSON.

    If ``preserve_existing`` is true, never replace an existing report. The
    requested path is used when free; otherwise a UTC timestamped sibling is
    selected. Returns the absolute path written.
    """
    report_dir = os.path.dirname(report_path)
    os.makedirs(report_dir, exist_ok=True)
    serialized = json.dumps(
        report, indent=2, ensure_ascii=False, default=json_default
    )
    if not preserve_existing:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(serialized)
        return report_path

    stem, extension = os.path.splitext(os.path.basename(report_path))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for attempt in range(1000):
        if attempt == 0:
            candidate = report_path
        elif attempt == 1:
            candidate = os.path.join(report_dir, f"{stem}_{timestamp}{extension}")
        else:
            candidate = os.path.join(
                report_dir, f"{stem}_{timestamp}_{attempt - 1:03d}{extension}"
            )
        try:
            with open(candidate, "x", encoding="utf-8") as f:
                f.write(serialized)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"Could not allocate a unique acceptance report for {report_path}")
