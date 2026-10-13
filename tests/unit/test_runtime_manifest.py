"""Unit tests for runtime/environment manifest."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import core.runtime_manifest as runtime_manifest


def test_manifest_schema_version():
    assert runtime_manifest.MANIFEST_SCHEMA_VERSION == "1.0"


def test_get_app_version(monkeypatch):
    monkeypatch.setattr(
        runtime_manifest,
        "pkg_version",
        lambda name: "0.2.3",
    )

    assert runtime_manifest._get_app_version() == "0.2.3"


def test_get_app_version_when_package_not_installed(monkeypatch):
    def raise_not_found(_name):
        raise runtime_manifest.PackageNotFoundError("not installed")

    monkeypatch.setattr(runtime_manifest, "pkg_version", raise_not_found)

    assert runtime_manifest._get_app_version() == "unknown"


def test_get_python_info():
    info = runtime_manifest._get_python_info()

    assert "version" in info
    assert "implementation" in info
    assert "platform" in info
    assert isinstance(info["version"], str)
    assert isinstance(info["implementation"], str)
    assert isinstance(info["platform"], str)


def test_get_package_versions(monkeypatch):
    def fake_version(package):
        if package == "psycopg":
            return "3.2.9"
        raise runtime_manifest.PackageNotFoundError(package)

    monkeypatch.setattr(runtime_manifest, "pkg_version", fake_version)

    result = runtime_manifest._get_package_versions(
        ["psycopg", "package-that-does-not-exist"]
    )

    assert result["psycopg"] == "3.2.9"
    assert result["package-that-does-not-exist"] == "not_installed"


def test_get_git_metadata(monkeypatch):
    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    responses = [
        Result("abcdef1234567890abcdef1234567890abcdef12\n"),
        Result("v0.2.3\n"),
        Result(" M some_file.py\n"),
        Result("feature/version-management\n"),
    ]

    def fake_run(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(runtime_manifest.subprocess, "run", fake_run)

    result = runtime_manifest._get_git_metadata()

    assert result["commit_sha"] == "abcdef1234567890abcdef1234567890abcdef12"
    assert result["commit_short"] == "abcdef123456"
    assert result["tag"] == "v0.2.3"
    assert result["dirty"] is True
    assert result["branch"] == "feature/version-management"


def test_get_git_metadata_gracefully_handles_missing_git(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(runtime_manifest.subprocess, "run", fake_run)

    result = runtime_manifest._get_git_metadata()

    assert result == {
        "commit_sha": None,
        "commit_short": None,
        "tag": None,
        "dirty": None,
        "branch": None,
    }


def test_config_hash_is_deterministic():
    config_a = {
        "target": {
            "engine": "postgresql",
            "host": "localhost",
            "port": 5432,
        },
        "source": {
            "engine": "postgresql",
            "host": "localhost",
            "port": 5433,
        },
    }

    config_b = {
        "source": {
            "port": 5433,
            "host": "localhost",
            "engine": "postgresql",
        },
        "target": {
            "port": 5432,
            "host": "localhost",
            "engine": "postgresql",
        },
    }

    assert runtime_manifest._get_config_hash(config_a) == (
        runtime_manifest._get_config_hash(config_b)
    )


def test_config_changes_affect_hash():
    config = {
        "source": {
            "engine": "postgresql",
            "host": "localhost",
            "port": 5432,
        }
    }

    changed_config = {
        "source": {
            "engine": "postgresql",
            "host": "localhost",
            "port": 5433,
        }
    }

    assert runtime_manifest._get_config_hash(config) != (
        runtime_manifest._get_config_hash(changed_config)
    )


def test_config_hash_redacts_secrets():
    config_a = {
        "source": {
            "host": "localhost",
            "password": "secret-one",
            "token": "token-one",
            "api_key": "api-key-one",
        }
    }

    config_b = {
        "source": {
            "host": "localhost",
            "password": "secret-two",
            "token": "token-two",
            "api_key": "api-key-two",
        }
    }

    # Changing secret values must not change the configuration hash.
    assert runtime_manifest._get_config_hash(config_a) == (
        runtime_manifest._get_config_hash(config_b)
    )


def test_db_version_from_connector():
    connector = MagicMock()
    connector.get_server_version.return_value = "PostgreSQL 17.4"

    result = runtime_manifest._get_db_version(
        connector,
        "source",
    )

    assert result == "PostgreSQL 17.4"
    connector.get_server_version.assert_called_once()


def test_db_version_is_optional_when_connector_missing():
    assert runtime_manifest._get_db_version(None, "source") is None


def test_db_version_failure_is_non_fatal():
    connector = MagicMock()
    connector.get_server_version.side_effect = RuntimeError("connection failed")

    result = runtime_manifest._get_db_version(
        connector,
        "source",
    )

    assert result is None


def test_build_runtime_manifest(monkeypatch):
    monkeypatch.setattr(
        runtime_manifest,
        "_get_app_version",
        lambda: "0.2.3",
    )
    monkeypatch.setattr(
        runtime_manifest,
        "_get_git_metadata",
        lambda: {
            "commit_sha": "abcdef123456",
            "commit_short": "abcdef123456",
            "tag": "v0.2.3",
            "dirty": False,
            "branch": "main",
        },
    )
    monkeypatch.setattr(
        runtime_manifest,
        "_get_python_info",
        lambda: {
            "version": "3.12.6",
            "implementation": "CPython",
            "platform": "Windows",
        },
    )
    monkeypatch.setattr(
        runtime_manifest,
        "_get_package_versions",
        lambda: {
            "psycopg": "3.2.9",
            "pyodbc": "5.2.0",
        },
    )

    source_connector = MagicMock()
    source_connector.get_server_version.return_value = "PostgreSQL 17.4"

    target_connector = MagicMock()
    target_connector.get_server_version.return_value = "PostgreSQL 16"

    config = {
        "source": {
            "engine": "postgresql",
            "host": "localhost",
            "password": "source-secret",
        },
        "target": {
            "engine": "postgresql",
            "host": "azure.example",
            "password": "target-secret",
        },
    }

    manifest = runtime_manifest.build_runtime_manifest(
        config=config,
        source_connector=source_connector,
        target_connector=target_connector,
    )

    assert manifest["schema_version"] == "1.0"
    assert manifest["application"]["name"] == "migration-platform"
    assert manifest["application"]["version"] == "0.2.3"

    assert manifest["git"]["commit_sha"] == "abcdef123456"
    assert manifest["git"]["tag"] == "v0.2.3"

    assert manifest["python"]["version"] == "3.12.6"
    assert manifest["python"]["implementation"] == "CPython"

    assert manifest["packages"]["psycopg"] == "3.2.9"
    assert manifest["packages"]["pyodbc"] == "5.2.0"

    assert manifest["database"]["source"]["engine"] == "postgresql"
    assert manifest["database"]["source"]["version"] == "PostgreSQL 17.4"

    assert manifest["database"]["target"]["engine"] == "postgresql"
    assert manifest["database"]["target"]["version"] == "PostgreSQL 16"

    assert "config_hash" in manifest
    assert len(manifest["config_hash"]) == 16
    assert "generated_at" in manifest

    # Raw secret values must never be present in the manifest.
    serialized = json.dumps(manifest)
    assert "source-secret" not in serialized
    assert "target-secret" not in serialized


def test_save_manifest(tmp_path: Path):
    manifest = {
        "schema_version": "1.0",
        "application": {
            "name": "migration-platform",
            "version": "0.2.3",
        },
        "config_hash": "1234567890abcdef",
    }

    output_path = runtime_manifest.save_manifest(
        manifest,
        directory=str(tmp_path),
    )

    path = Path(output_path)

    assert path.exists()
    assert path.parent == tmp_path
    assert path.name.startswith("manifest_")
    assert path.suffix == ".json"

    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved == manifest