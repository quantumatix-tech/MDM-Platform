#!/usr/bin/env python3
"""Bootstrap script: install the project from pyproject.toml and verify drivers."""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent

ENGINE_IMPORTS: dict[str, dict[str, Any]] = {
    "postgresql": {"imports": ["psycopg"], "label": "PostgreSQL (psycopg)"},
    "mysql": {"imports": ["mysql.connector", "pymysqlreplication"], "label": "MySQL (mysql-connector-python, mysql-replication)"},
    "mongodb": {"imports": ["pymongo"], "label": "MongoDB (pymongo)"},
    "mssql": {"imports": ["pyodbc"], "odbc_check": "ODBC Driver 18 for SQL Server", "label": "MSSQL (pyodbc + ODBC Driver 18)"},
    "cosmos_mongo": {"imports": ["pymongo", "azure.cosmos"], "label": "Cosmos DB (pymongo, azure-cosmos)"},
}


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def install_project() -> None:
    """Install the project itself from pyproject.toml, which owns dependencies."""
    probe = run([sys.executable, "-m", "pip", "--version"])
    if probe.returncode != 0:
        print("pip not found. Install Python first.")
        sys.exit(1)

    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        print(f"pyproject.toml not found at {pyproject}.")
        sys.exit(1)

    print(f"Installing migration-platform from {pyproject}...")
    result = run([sys.executable, "-m", "pip", "install", str(PROJECT_ROOT)])
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)
    print("migration-platform installed.")


def _canonical(name: str) -> str:
    """PEP 503 normalization so Migration_Platform matches migration-platform."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_project_version() -> str | None:
    """Version declared in pyproject.toml, or None if it cannot be determined."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
    except ImportError:
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version_txt = data.get("project", {}).get("version")
    return version_txt if isinstance(version_txt, str) else None


def verify_installed_metadata() -> bool:
    """Confirm a real installed distribution exists, not a repository-local artifact.

    ``importlib.metadata`` resolves against ``sys.path``, and running this script
    puts the repository root on it. A gitignored ``migration_platform.egg-info/``
    from a previous build would otherwise satisfy the lookup even when nothing
    was ever installed. Any candidate whose metadata lives inside the project root
    is therefore rejected, while site-packages metadata is accepted for both
    normal and editable installs.
    """
    from importlib.metadata import distributions

    root = PROJECT_ROOT.resolve()
    external: list[str] = []
    repository_local = False

    for dist in distributions():
        try:
            name = dist.metadata["Name"]
        except (KeyError, TypeError, OSError, UnicodeDecodeError):
            continue
        if not name or _canonical(name) != "migration-platform":
            continue
        located = getattr(dist, "_path", None)
        if located is None:
            external.append(dist.version)
            continue
        try:
            resolved = Path(located).resolve()
        except OSError:
            external.append(dist.version)
            continue
        if resolved == root or root in resolved.parents:
            repository_local = True
        else:
            external.append(dist.version)

    if not external:
        if repository_local:
            print(
                "MISSING: migration-platform is not installed. A repository-local "
                f"{'migration_platform.egg-info'} was found at {PROJECT_ROOT}, but "
                "build artifacts do not count as an installation. Re-run without "
                "--skip-pip."
            )
        else:
            print("MISSING: migration-platform is not installed after pip install.")
        return False

    installed = external[0]
    declared = _declared_project_version()
    if declared and installed != declared:
        print(
            f"MISMATCH: installed migration-platform {installed} does not match "
            f"pyproject.toml {declared}. Re-run pip install."
        )
        return False

    print(f"OK: migration-platform {installed}")
    return True


def detect_configured_engines(config_path: str | None = None) -> list[str]:
    if config_path is None:
        return list(ENGINE_IMPORTS.keys())
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    source = config.get("source", {}).get("engine")
    target = config.get("target", {}).get("engine")
    engines = {source, target}
    return [e for e in engines if e in ENGINE_IMPORTS]


def check_imports(engines: list[str]) -> bool:
    ok = True
    for engine in engines:
        spec = ENGINE_IMPORTS.get(engine)
        if spec is None:
            continue
        for mod in spec.get("imports", []):
            try:
                __import__(mod)
            except ImportError:
                print(f"MISSING: {mod} ({spec['label']})")
                ok = False
            else:
                print(f"OK: {mod}")
        if "odbc_check" in spec:
            try:
                import pyodbc

                drivers = pyodbc.drivers()
                if spec["odbc_check"] not in drivers:
                    print(f"MISSING ODBC DRIVER: {spec['odbc_check']} not found in pyodbc.drivers()")
                    ok = False
                else:
                    print(f"OK: {spec['odbc_check']}")
            except ImportError:
                print(f"MISSING: pyodbc ({spec['label']})")
                ok = False
    return ok


def print_odbc_install_instructions() -> None:
    system = platform.system()
    print("\nMSSQL ODBC driver missing. Install it manually:")
    if system == "Windows":
        print("  choco install msodbcsql18")
    elif system == "Darwin":
        print("  brew tap microsoft/mssql-release && brew install msodbcsql18")
    elif system == "Linux":
        print("  curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -")
        print("  curl https://packages.microsoft.com/config/$(lsb_release -rs)/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list")
        print("  sudo apt-get update && sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18")
    else:
        print(f"  Unknown OS ({system}). Install ODBC Driver 18 for SQL Server manually.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Bootstrap migration platform dependencies")
    parser.add_argument("--config", help="Path to YAML config file")
    parser.add_argument("--skip-pip", action="store_true", help="Skip project installation")
    args = parser.parse_args()

    if not args.skip_pip:
        install_project()

    metadata_ok = verify_installed_metadata()
    engines = detect_configured_engines(args.config)
    print(f"\nVerifying drivers for engines: {', '.join(engines)}")
    all_ok = check_imports(engines)
    if not all_ok and "mssql" in engines:
        print_odbc_install_instructions()
        sys.exit(1)
    if not all_ok:
        sys.exit(1)
    if not metadata_ok:
        sys.exit(1)
    print("\nAll dependencies satisfied.")


if __name__ == "__main__":
    main()
