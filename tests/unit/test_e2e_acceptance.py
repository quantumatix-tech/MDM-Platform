"""Unit tests for the shared acceptance E2E module.

Tests config loading, safety patterns, report serialization, and helper
functions in tests/e2e/acceptance/__init__.py.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.e2e.acceptance import (
    E2EConfig,
    fmt_phase,
    fmt_source_target_counts,
    json_default,
    load_e2e_config,
    write_acceptance_report,
)

# ---------------------------------------------------------------------------
# E2EConfig
# ---------------------------------------------------------------------------

class TestE2EConfig:
    """Tests for the E2EConfig typed accessor."""

    @pytest.fixture
    def sample_cfg(self) -> E2EConfig:
        return E2EConfig({
            "source": {"connection": {
                "host": "127.0.0.1", "port": 33062,
                "database": "MigrationE2E_MySQL_Source",
                "username": "root", "password_secret": "mysql_e2e_source_pass",
            }},
            "target": {"connection": {
                "host": "127.0.0.1", "port": 33062,
                "database": "MigrationE2E_MySQL_Target",
                "username": "root", "password_secret": "mysql_e2e_target_pass",
            }},
            "e2e": {
                "engine": "mysql",
                "schema": "",
                "fixture_path": "tests/e2e/mysql/fixtures",
                "source_database": "MigrationE2E_MySQL_Source",
                "target_database": "MigrationE2E_MySQL_Target",
                "safe_db_pattern": r"^MigrationE2E_MySQL_.+$",
                "cleanup": {"mode": "on_success"},
                "functional": {"enabled": True},
            },
        })

    def test_engine(self, sample_cfg):
        assert sample_cfg.engine == "mysql"

    def test_source_db(self, sample_cfg):
        assert sample_cfg.source_db == "MigrationE2E_MySQL_Source"

    def test_target_db(self, sample_cfg):
        assert sample_cfg.target_db == "MigrationE2E_MySQL_Target"

    def test_safe_pattern(self, sample_cfg):
        assert sample_cfg.safe_pattern == r"^MigrationE2E_MySQL_.+$"

    def test_schema_default_empty(self, sample_cfg):
        assert sample_cfg.schema == ""

    def test_source_connection(self, sample_cfg):
        conn = sample_cfg.source_connection
        assert conn["host"] == "127.0.0.1"
        assert conn["port"] == 33062
        assert conn["username"] == "root"

    def test_target_connection(self, sample_cfg):
        conn = sample_cfg.target_connection
        assert conn["database"] == "MigrationE2E_MySQL_Target"

    def test_source_password_secret(self, sample_cfg):
        assert sample_cfg.source_password_secret() == "mysql_e2e_source_pass"

    def test_target_password_secret(self, sample_cfg):
        assert sample_cfg.target_password_secret() == "mysql_e2e_target_pass"

    def test_to_abs_fixture_path_relative(self, sample_cfg, tmp_path):
        """Repo-root-relative fixture_path resolves from repo root, not config_dir."""
        result = sample_cfg.to_abs_fixture_path(str(tmp_path))
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        expected = os.path.abspath(os.path.join(repo_root, "tests/e2e/mysql/fixtures"))
        assert result == expected

    def test_to_abs_fixture_path_absolute(self, sample_cfg, tmp_path):
        config_file = tmp_path / "fixtures"
        sample_cfg.raw["e2e"]["fixture_path"] = str(config_file)
        result = sample_cfg.to_abs_fixture_path("/some/config/dir")
        assert result == str(config_file)


# ---------------------------------------------------------------------------
# load_e2e_config
# ---------------------------------------------------------------------------

class TestLoadE2EConfig:
    """Tests for config file loading."""

    def test_load_valid_config(self, tmp_path):
        config_data = {
            "source": {"connection": {"host": "localhost", "port": 3306,
                "database": "MigrationE2E_Test_Source", "username": "root",
                "password_secret": "test_pass"}},
            "target": {"connection": {"host": "localhost", "port": 3306,
                "database": "MigrationE2E_Test_Target", "username": "root",
                "password_secret": "test_pass"}},
            "e2e": {"engine": "mysql", "schema": "",
                "fixture_path": "tests/e2e/mysql/fixtures",
                "source_database": "MigrationE2E_Test_Source",
                "target_database": "MigrationE2E_Test_Target",
                "safe_db_pattern": r"^MigrationE2E_.+$"},
        }
        config_file = tmp_path / "test_config.yaml"
        import yaml
        config_file.write_text(yaml.dump(config_data))

        cfg = load_e2e_config(str(config_file))
        assert cfg.source_db == "MigrationE2E_Test_Source"
        assert cfg.target_db == "MigrationE2E_Test_Target"
        assert cfg.engine == "mysql"

    def test_load_missing_config_raises(self, monkeypatch):
        monkeypatch.delenv("E2E_CONFIG_PATH", raising=False)
        monkeypatch.setenv("E2E_ENGINE", "nonexistent_engine")
        with pytest.raises(FileNotFoundError):
            load_e2e_config(None)

    def test_load_via_env_var(self, tmp_path, monkeypatch):
        config_data = {
            "e2e": {"engine": "pg", "source_database": "src",
                "target_database": "tgt", "safe_db_pattern": "^.*$",
                "fixture_path": ".", "schema": ""},
        }
        config_file = tmp_path / "env_config.yaml"
        import yaml
        config_file.write_text(yaml.dump(config_data))
        monkeypatch.setenv("E2E_CONFIG_PATH", str(config_file))

        cfg = load_e2e_config(None)
        assert cfg.engine == "pg"


# ---------------------------------------------------------------------------
# json_default
# ---------------------------------------------------------------------------

class TestJsonDefault:
    """Tests for the JSON serializer fallback."""

    def test_decimal(self):
        assert json_default(Decimal("3.14")) == "3.14"

    def test_datetime(self):
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        assert json_default(dt) == "2024-01-15 10:30:00+00:00"

    def test_date(self):
        d = date(2024, 1, 15)
        assert json_default(d) == "2024-01-15"

    def test_unsupported_raises(self):
        with pytest.raises(TypeError):
            json_default(object())


# ---------------------------------------------------------------------------
# write_acceptance_report
# ---------------------------------------------------------------------------

class TestWriteAcceptanceReport:
    """Tests for report writing."""

    def test_write_report(self, tmp_path):
        report = {"test": "value", "count": 42}
        path = str(tmp_path / "subdir" / "report.json")
        result = write_acceptance_report(path, report)
        assert result == path
        with open(result) as f:
            data = json.load(f)
        assert data["test"] == "value"
        assert data["count"] == 42

    def test_write_report_creates_dir(self, tmp_path):
        report = {"test": "value"}
        path = str(tmp_path / "new_dir" / "nested" / "report.json")
        write_acceptance_report(path, report)
        assert os.path.isfile(path)

    def test_write_report_serializes_decimal(self, tmp_path):
        report = {"decimal_field": Decimal("99.99")}
        path = str(tmp_path / "report.json")
        write_acceptance_report(path, report)
        with open(path) as f:
            data = json.load(f)
        assert data["decimal_field"] == "99.99"


# ---------------------------------------------------------------------------
# fmt_phase
# ---------------------------------------------------------------------------

class FakeCheck:
    def __init__(self, status, name="check", message=""):
        self.status = status
        self.name = name
        self.message = message


class FakePhase:
    def __init__(self, name, checks):
        self.name = name
        self.checks = checks


class TestFmtPhase:
    """Tests for phase formatting."""

    def test_all_pass(self):
        phase = FakePhase("tables", [FakeCheck("PASS"), FakeCheck("PASS")])
        result = fmt_phase(phase)
        assert "PASS" in result
        assert "2/2" in result

    def test_some_fail(self):
        phase = FakePhase("tables", [FakeCheck("PASS"), FakeCheck("FAIL")])
        result = fmt_phase(phase)
        assert "FAIL" in result
        assert "1/2" in result

    def test_no_checks(self):
        phase = FakePhase("empty", [])
        result = fmt_phase(phase)
        assert "PASS" in result
        assert "0/0" in result


# ---------------------------------------------------------------------------
# fmt_source_target_counts
# ---------------------------------------------------------------------------

class TestFmtSourceTargetCounts:
    """Tests for source/target count formatting."""

    def test_all_match(self):
        src = {"t1": 10, "t2": 20}
        tgt = {"t1": 10, "t2": 20}
        result = fmt_source_target_counts(src, tgt)
        assert "[PASS]" in result
        assert "[FAIL]" not in result

    def test_mismatch(self):
        src = {"t1": 10}
        tgt = {"t1": 5}
        result = fmt_source_target_counts(src, tgt)
        assert "[FAIL]" in result

    def test_missing_in_target(self):
        src = {"t1": 10}
        tgt = {}
        result = fmt_source_target_counts(src, tgt)
        assert "[FAIL]" in result

    def test_missing_in_source(self):
        src = {}
        tgt = {"t1": 10}
        result = fmt_source_target_counts(src, tgt)
        assert "[FAIL]" in result
