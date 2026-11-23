from __future__ import annotations

from types import SimpleNamespace

import pytest
import sys

from tests.integration import oracle_lob_locator_lifecycle as probe


class Cursor:
    def __init__(self, identity="OWNER", count=0):
        self.identity = identity
        self.count = count
        self.statement = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement):
        self.statement = statement

    def fetchone(self):
        if "SELECT USER" in self.statement:
            return (self.identity,)
        return (self.count,)


class Connection:
    def __init__(self, identity="OWNER", count=0):
        self._cursor = Cursor(identity, count)

    def cursor(self):
        return self._cursor


def test_probe_requires_positive_live_opt_in(monkeypatch):
    monkeypatch.delenv("RUN_ORACLE_LOB_PROBE", raising=False)
    cfg = SimpleNamespace(raw={"e2e": {"lob_probe": {"disposable": True}}})
    with pytest.raises(RuntimeError, match="RUN_ORACLE_LOB_PROBE=1"):
        probe._assert_probe_opt_in(cfg)


def test_probe_requires_config_to_mark_owners_disposable(monkeypatch):
    monkeypatch.setenv("RUN_ORACLE_LOB_PROBE", "1")
    cfg = SimpleNamespace(raw={"e2e": {}})
    with pytest.raises(RuntimeError, match="explicitly mark"):
        probe._assert_probe_opt_in(cfg)


def test_probe_refuses_owner_with_any_existing_objects():
    with pytest.raises(RuntimeError, match="not empty and disposable"):
        probe._verify_empty_disposable_owner(Connection(count=1), "OWNER", "source")


def test_probe_refuses_session_identity_mismatch():
    with pytest.raises(RuntimeError, match="not connected as the configured owner"):
        probe._verify_empty_disposable_owner(Connection(identity="OTHER"), "OWNER", "target")


def test_probe_accepts_only_matching_empty_owner():
    probe._verify_empty_disposable_owner(Connection(), "OWNER", "source")


def test_probe_cli_requires_explicit_configuration(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["oracle_lob_locator_lifecycle.py"])
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2
