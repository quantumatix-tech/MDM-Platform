"""Focused offline tests for scripts/db2/verify_db2_e2e_preflight.py."""

import os
import sys

import pytest

# Ensure repo root on path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Mock yaml if needed (already available)
from unittest.mock import MagicMock, patch

import yaml


def _make_config(source_db="SRCDB", target_db="TGTDB", schemas=None):
    if schemas is None:
        schemas = ["TESTE2E"]
    return {
        "source": {
            "engine": "db2",
            "connection": {
                "host": "127.0.0.1",
                "port": 25000,
                "database": source_db,
                "username": "db2admin",
                "password_secret": "db2_source_pass",
                "driver": "IBM DB2 ODBC DRIVER - DB2COPY1",
            },
        },
        "target": {
            "engine": "db2",
            "connection": {
                "host": "127.0.0.1",
                "port": 25000,
                "database": target_db,
                "username": "db2admin",
                "password_secret": "db2_target_pass",
                "driver": "IBM DB2 ODBC DRIVER - DB2COPY1",
                "source_engine": "db2",
            },
        },
        "migration": {"mode": "full", "include_schemas": schemas, "batch_size": 1000},
        "secrets": {"provider": "env"},
    }


def _write_config(tmp_path, cfg):
    p = tmp_path / "test_config.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    return str(p)


class TestPreflightValidConfig:
    def test_valid_config_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_db2_source_pass", "srcpass")
        monkeypatch.setenv("SECRET_db2_target_pass", "tgtpass")
        cfg = _make_config()
        cfg_path = _write_config(tmp_path, cfg)

        mock_src = MagicMock()
        mock_src._get_selected_schemas.return_value = ["TESTE2E"]
        mock_src.preflight_validate_schemas.return_value = {"status": "ok"}
        mock_src.list_objects_with_schema.return_value = [("TESTE2E", "T1")]
        mock_src.get_object_count.return_value = 5
        mock_src._conn = MagicMock()

        mock_tgt = MagicMock()
        mock_tgt.preflight.return_value = {
            "current_server": "TGTDB",
            "session_user": "DB2ADMIN",
        }
        mock_tgt._conn = MagicMock()

        with patch("scripts.db2.verify_db2_e2e_preflight.DB2SourceConnector", return_value=mock_src), \
             patch("scripts.db2.verify_db2_e2e_preflight.DB2TargetConnector", return_value=mock_tgt), \
             patch("scripts.db2.verify_db2_e2e_preflight.create_secret_provider") as mock_sp:
            mock_sp.return_value.resolve.side_effect = lambda x: "resolved"
            import scripts.db2.verify_db2_e2e_preflight as preflight
            # Should not raise
            preflight.run(cfg_path)
            mock_src.connect.assert_called_once()
            mock_tgt.connect.assert_called_once()
            mock_src.close.assert_called_once()
            mock_tgt.close.assert_called_once()


class TestPreflightMissingSecret:
    def test_missing_secret_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECRET_db2_source_pass", raising=False)
        monkeypatch.delenv("SECRET_db2_target_pass", raising=False)
        cfg = _make_config()
        cfg_path = _write_config(tmp_path, cfg)

        with pytest.raises(Exception) as exc_info:
            import scripts.db2.verify_db2_e2e_preflight as preflight
            preflight.run(cfg_path)


class TestPreflightSourceConnectFailure:
    def test_source_connect_failure_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_db2_source_pass", "srcpass")
        monkeypatch.setenv("SECRET_db2_target_pass", "tgtpass")
        cfg = _make_config()
        cfg_path = _write_config(tmp_path, cfg)

        mock_src = MagicMock()
        mock_src.connect.side_effect = RuntimeError("source connection failed")

        with patch("scripts.db2.verify_db2_e2e_preflight.DB2SourceConnector", return_value=mock_src), \
             patch("scripts.db2.verify_db2_e2e_preflight.create_secret_provider") as mock_sp:
            mock_sp.return_value.resolve.side_effect = lambda x: "resolved"
            import scripts.db2.verify_db2_e2e_preflight as preflight
            with pytest.raises(RuntimeError, match="source connection failed"):
                preflight.run(cfg_path)


class TestPreflightTargetConnectFailure:
    def test_target_connect_failure_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_db2_source_pass", "srcpass")
        monkeypatch.setenv("SECRET_db2_target_pass", "tgtpass")
        cfg = _make_config()
        cfg_path = _write_config(tmp_path, cfg)

        mock_src = MagicMock()
        mock_src._get_selected_schemas.return_value = ["TESTE2E"]
        mock_src.preflight_validate_schemas.return_value = {"status": "ok"}
        mock_src.list_objects_with_schema.return_value = []
        mock_src._conn = MagicMock()

        mock_tgt = MagicMock()
        mock_tgt.connect.side_effect = RuntimeError("target connection failed")

        with patch("scripts.db2.verify_db2_e2e_preflight.DB2SourceConnector", return_value=mock_src), \
             patch("scripts.db2.verify_db2_e2e_preflight.DB2TargetConnector", return_value=mock_tgt), \
             patch("scripts.db2.verify_db2_e2e_preflight.create_secret_provider") as mock_sp:
            mock_sp.return_value.resolve.side_effect = lambda x: "resolved"
            import scripts.db2.verify_db2_e2e_preflight as preflight
            with pytest.raises(RuntimeError, match="target connection failed"):
                preflight.run(cfg_path)


class TestPreflightMissingSourceSchema:
    def test_missing_schema_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_db2_source_pass", "srcpass")
        monkeypatch.setenv("SECRET_db2_target_pass", "tgtpass")
        cfg = _make_config()
        cfg_path = _write_config(tmp_path, cfg)

        mock_src = MagicMock()
        mock_src._get_selected_schemas.return_value = ["TESTE2E"]
        mock_src.preflight_validate_schemas.side_effect = RuntimeError("SCHEMA_NOT_FOUND: TESTE2E")
        mock_src._conn = MagicMock()

        mock_tgt = MagicMock()
        mock_tgt._conn = MagicMock()

        with patch("scripts.db2.verify_db2_e2e_preflight.DB2SourceConnector", return_value=mock_src), \
             patch("scripts.db2.verify_db2_e2e_preflight.DB2TargetConnector", return_value=mock_tgt), \
             patch("scripts.db2.verify_db2_e2e_preflight.create_secret_provider") as mock_sp:
            mock_sp.return_value.resolve.side_effect = lambda x: "resolved"
            import scripts.db2.verify_db2_e2e_preflight as preflight
            with pytest.raises(RuntimeError, match="SCHEMA_NOT_FOUND"):
                preflight.run(cfg_path)


class TestPreflightNonemptyTarget:
    def test_same_db_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_db2_source_pass", "srcpass")
        monkeypatch.setenv("SECRET_db2_target_pass", "tgtpass")
        cfg = _make_config(source_db="SAME", target_db="SAME")
        cfg_path = _write_config(tmp_path, cfg)

        mock_src = MagicMock()
        mock_src._get_selected_schemas.return_value = ["TESTE2E"]
        mock_src.preflight_validate_schemas.return_value = {"status": "ok"}
        mock_src.list_objects_with_schema.return_value = []
        mock_src.get_object_count.return_value = 0
        mock_src._conn = MagicMock()

        mock_tgt = MagicMock()
        mock_tgt.preflight.return_value = {
            "current_server": "SAME",
            "session_user": "DB2ADMIN",
        }
        mock_tgt._conn = MagicMock()

        with patch("scripts.db2.verify_db2_e2e_preflight.DB2SourceConnector", return_value=mock_src), \
             patch("scripts.db2.verify_db2_e2e_preflight.DB2TargetConnector", return_value=mock_tgt), \
             patch("scripts.db2.verify_db2_e2e_preflight.create_secret_provider") as mock_sp:
            mock_sp.return_value.resolve.side_effect = lambda x: "resolved"
            import scripts.db2.verify_db2_e2e_preflight as preflight
            with pytest.raises(RuntimeError, match="same database"):
                preflight.run(cfg_path)


class TestPreflightExitCode:
    def test_main_returns_zero_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_db2_source_pass", "srcpass")
        monkeypatch.setenv("SECRET_db2_target_pass", "tgtpass")
        cfg = _make_config()
        cfg_path = _write_config(tmp_path, cfg)

        mock_src = MagicMock()
        mock_src._get_selected_schemas.return_value = ["TESTE2E"]
        mock_src.preflight_validate_schemas.return_value = {"status": "ok"}
        mock_src.list_objects_with_schema.return_value = []
        mock_src.get_object_count.return_value = 0
        mock_src._conn = MagicMock()

        mock_tgt = MagicMock()
        mock_tgt.preflight.return_value = {
            "current_server": "TGTDB",
            "session_user": "DB2ADMIN",
        }
        mock_tgt._conn = MagicMock()

        with patch("scripts.db2.verify_db2_e2e_preflight.DB2SourceConnector", return_value=mock_src), \
             patch("scripts.db2.verify_db2_e2e_preflight.DB2TargetConnector", return_value=mock_tgt), \
             patch("scripts.db2.verify_db2_e2e_preflight.create_secret_provider") as mock_sp:
            mock_sp.return_value.resolve.side_effect = lambda x: "resolved"
            import scripts.db2.verify_db2_e2e_preflight as preflight
            # main() calls sys.exit(run(...)) — run returns None, so no exception
            # Just verify run() completes without error
            preflight.run(cfg_path)
