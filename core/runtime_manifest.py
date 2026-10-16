"""Runtime/Environment Manifest for Migration Platform.

Captures application/runtime/environment metadata for each migration execution.
Follows the existing architecture without duplicating version management mechanisms.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any

from core.secrets.base import SecretProvider


MANIFEST_SCHEMA_VERSION = "1.0"


def _get_app_version() -> str:
    """Get application version from installed package metadata."""
    try:
        return pkg_version("migration-platform")
    except PackageNotFoundError:
        return "unknown"


def _get_git_metadata() -> dict[str, Any]:
    """Capture git metadata gracefully - never fails if git unavailable."""
    metadata: dict[str, Any] = {
        "commit_sha": None,
        "commit_short": None,
        "tag": None,
        "dirty": None,
        "branch": None,
    }
    try:
        # Get commit SHA
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            metadata["commit_sha"] = sha
            metadata["commit_short"] = sha[:12]

        # Get current tag if any
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            metadata["tag"] = result.stdout.strip()

        # Check dirty status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            metadata["dirty"] = bool(result.stdout.strip())

        # Get branch name
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            metadata["branch"] = result.stdout.strip() or None
    except Exception:
        # Gracefully handle missing git, permission issues, timeouts
        pass
    return metadata


def _get_python_info() -> dict[str, Any]:
    """Capture Python runtime information."""
    return {
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


def _get_package_versions(packages: list[str] | None = None) -> dict[str, str]:
    """Get versions of key Python packages/drivers."""
    if packages is None:
        packages = [
            "psycopg",
            "mysql-connector-python",
            "pyodbc",
            "pymongo",
            "pymysql",
            "mysql-replication",
            "azure-identity",
            "azure-keyvault-secrets",
            "azure-cosmos",
            "azure-storage-blob",
            "boto3",
            "google-cloud-secret-manager",
            "hvac",
            "cryptography",
            "keyring",
            "rich",
            "pyyaml",
            "requests",
        ]
    versions: dict[str, str] = {}
    for pkg in packages:
        try:
            versions[pkg] = pkg_version(pkg)
        except PackageNotFoundError:
            versions[pkg] = "not_installed"
    return versions


def _get_config_hash(config: dict[str, Any], secret_provider: SecretProvider | None = None) -> str:
    """Compute deterministic hash of configuration with secrets redacted.

    Args:
        config: Full migration configuration dict
        secret_provider: Optional SecretProvider to resolve and redact secret refs

    Returns:
        SHA256 hex digest (first 16 chars) of canonical JSON config
    """
    # Deep copy and redact secrets
    def redact(obj: Any) -> Any:
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k in ("password", "password_secret", "token", "key", "secret", "api_key", "webhook_url"):
                    result[k] = "**REDACTED**"
                else:
                    result[k] = redact(v)
            return result
        elif isinstance(obj, list):
            return [redact(item) for item in obj]
        else:
            return obj

    safe_config = redact(config)
    # Canonical JSON for deterministic hashing
    canonical = json.dumps(safe_config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _get_db_version(connector: Any, role: str) -> str | None:
    """Get database engine version from a connected connector.

    Args:
        connector: SourceConnector or TargetConnector instance (must be connected)
        role: "source" or "target" for logging context

    Returns:
        Version string or None if unavailable
    """
    if connector is None:
        return None
    try:
        # Try common version query methods
        if hasattr(connector, "get_server_version"):
            return connector.get_server_version()
        if hasattr(connector, "_conn") and connector._conn is not None:
            conn = connector._conn
            # PostgreSQL
            if hasattr(conn, "execute"):
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT version()")
                        row = cur.fetchone()
                        if row:
                            return row[0]
                except Exception:
                    pass
            # MySQL
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT VERSION()")
                    row = cur.fetchone()
                    if row:
                        return row[0]
            except Exception:
                pass
            # MSSQL
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT @@VERSION")
                    row = cur.fetchone()
                    if row:
                        return row[0]
            except Exception:
                pass
    except Exception:
        pass
    return None


def build_runtime_manifest(
    config: dict[str, Any],
    source_connector: Any = None,
    target_connector: Any = None,
    secret_provider: SecretProvider | None = None,
) -> dict[str, Any]:
    """Build the complete runtime/environment manifest.

    Args:
        config: Full migration configuration
        source_connector: Connected source connector (optional)
        target_connector: Connected target connector (optional)
        secret_provider: SecretProvider for redacting secrets in config hash

    Returns:
        Manifest dictionary with all metadata
    """
    git_meta = _get_git_metadata()

    # Get DB versions if connectors are connected
    source_db_version = _get_db_version(source_connector, "source")
    target_db_version = _get_db_version(target_connector, "target")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "application": {
            "name": "migration-platform",
            "version": _get_app_version(),
        },
        "git": git_meta,
        "python": _get_python_info(),
        "packages": _get_package_versions(),
        "database": {
            "source": {
                "engine": config.get("source", {}).get("engine"),
                "version": source_db_version,
            },
            "target": {
                "engine": config.get("target", {}).get("engine"),
                "version": target_db_version,
            },
        },
        "config_hash": _get_config_hash(config, secret_provider),
    }
    return manifest


def save_manifest(manifest: dict[str, Any], directory: str = "reports") -> str:
    """Save manifest to JSON file.

    Args:
        manifest: Manifest dictionary
        directory: Output directory

    Returns:
        Path to saved manifest file
    """
    os.makedirs(directory, exist_ok=True)
    run_id = manifest.get("run_id", "unknown")
    # Use timestamp if no run_id
    if run_id == "unknown":
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"manifest_{timestamp}.json"
    else:
        filename = f"manifest_{run_id}.json"
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
    return path