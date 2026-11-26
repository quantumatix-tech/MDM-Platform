"""Live Oracle FULL migration acceptance test using the project YAML/secrets."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from core.connectors.oracle import OracleSourceConnector, OracleTargetConnector
from core.orchestrator import MigrationOrchestrator
from tests.e2e.acceptance import E2EConfig, load_e2e_config
from tests.e2e.oracle.config import (
    connect,
    connection_config,
    resolver_for_side,
    validated_oracle_context,
)
from tests.e2e.oracle.run_validation import run_validation


class _RecordingCursor:
    def __init__(self, cursor, writes):
        self._cursor = cursor
        self._writes = writes

    def __enter__(self):
        self._cursor = self._cursor.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._cursor.__exit__(*exc_info)

    def execute(self, statement, *args, **kwargs):
        if re.match(
            r"\s*(CREATE|ALTER|DROP|TRUNCATE|INSERT|UPDATE|DELETE|MERGE|GRANT|REVOKE|BEGIN)\b",
            str(statement), re.IGNORECASE,
        ):
            self._writes.append(str(statement))
        return self._cursor.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RecordingConnection:
    def __init__(self, connection, writes):
        self._connection = connection
        self._writes = writes

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._connection.cursor(*args, **kwargs), self._writes)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _RecordingOracleTarget(OracleTargetConnector):
    def __init__(self, config):
        super().__init__(config)
        self.writes = []

    def connect(self):
        super().connect()
        self._conn = _RecordingConnection(self._conn, self.writes)


class _ObservedOracleSource(OracleSourceConnector):
    def __init__(self, config):
        super().__init__(config)
        self.discovery_errors = []

    def list_objects(self):
        try:
            return super().list_objects()
        except Exception as exc:
            self.discovery_errors.append(exc)
            raise


@pytest.mark.integration
def test_configured_oracle_full_migration_and_acceptance_validation():
    repo_root = Path(__file__).parents[3]
    config_path = repo_root / "config" / "oracle_e2e_acceptance.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    if os.environ.get("RUN_ORACLE_FULL_E2E") != "1":
        pytest.skip("Set RUN_ORACLE_FULL_E2E=1 to run migration against configured Oracle owners")
    secret_names = [
        "SECRET_" + raw[side]["connection"]["password_secret"]
        for side in ("source", "target")
    ]
    missing = [name for name in secret_names if not os.environ.get(name)]
    if missing:
        pytest.skip("Oracle live E2E prerequisites missing: " + ", ".join(missing))

    cfg = load_e2e_config(str(config_path))
    validated_oracle_context(cfg)
    source = OracleSourceConnector(dict(raw["source"]["connection"]))
    target = OracleTargetConnector(dict(raw["target"]["connection"]))
    migration = MigrationOrchestrator(source, target, raw).run_full()

    assert migration.get("status") == "success", migration.get("error") or migration.get("phases")
    validation = run_validation(E2EConfig(raw))
    assert validation["result"] == "PASS", validation.get("checks")

    owners, _ = validated_oracle_context(E2EConfig(raw))
    import oracledb

    endpoints = {
        side: connect(
            connection_config(
                E2EConfig(raw), side, owners[side], resolver_for_side(raw, side)
            ),
            oracledb,
        )
        for side in ("source", "target")
    }
    try:
        identity_definitions = {}
        for side, connection in endpoints.items():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name, column_name, generation_type "
                    "FROM user_tab_identity_cols"
                )
                identity_definitions[side] = {
                    (str(table).upper(), str(column).upper()): str(mode).upper()
                    for table, column, mode in cursor.fetchall()
                }
        assert identity_definitions["source"] == identity_definitions["target"], (
            "Configured source/target identity definitions differ: "
            f"{identity_definitions}"
        )
    finally:
        for connection in endpoints.values():
            connection.close()


@pytest.mark.integration
def test_missing_selected_oracle_table_fails_before_target_writes():
    repo_root = Path(__file__).parents[3]
    config_path = repo_root / "config" / "oracle_e2e_acceptance.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    if os.environ.get("RUN_ORACLE_FULL_E2E") != "1":
        pytest.skip("Set RUN_ORACLE_FULL_E2E=1 to connect to the configured Oracle owners")
    secret_names = [
        "SECRET_" + raw[side]["connection"]["password_secret"]
        for side in ("source", "target")
    ]
    missing = [name for name in secret_names if not os.environ.get(name)]
    if missing:
        pytest.skip("Oracle live E2E prerequisites missing: " + ", ".join(missing))

    cfg = load_e2e_config(str(config_path))
    validated_oracle_context(cfg)
    raw["source"]["connection"]["include_tables"] = ["DISCOVERY_MISSING_TABLE"]
    source = _ObservedOracleSource(dict(raw["source"]["connection"]))
    target = _RecordingOracleTarget(dict(raw["target"]["connection"]))

    migration = MigrationOrchestrator(source, target, raw).run_full()

    expected_error = (
        "Configured Oracle source include_tables were not found: DISCOVERY_MISSING_TABLE"
    )
    assert migration["status"] == "failed"
    assert migration["error"] == expected_error
    assert len(source.discovery_errors) == 1
    assert type(source.discovery_errors[0]) is ValueError
    assert str(source.discovery_errors[0]) == expected_error
    assert target.writes == []
