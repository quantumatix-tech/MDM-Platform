"""Regression tests for sanitized Oracle provisioning-admin login diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.oracle.live_launcher import _probe_login

ROOT = Path(__file__).parents[2]


class _OracleDriverError(Exception):
    pass


class _Driver:
    Error = _OracleDriverError

    @staticmethod
    def makedsn(host: str, port: int, *, service_name: str) -> str:
        return f"{host}:{port}/{service_name}"

    @staticmethod
    def connect(**_kwargs):
        raise AssertionError("test must replace the connection method")


def _config() -> dict:
    return load_e2e_config(
        str(ROOT / "config" / "oracle_e2e_acceptance.yaml")
    ).raw


@pytest.mark.parametrize(
    ("driver_message", "expected"),
    [
        ("ORA-01017: invalid username/password; secret=never-show-this", "ORA-01017"),
        ("ORA-28000: account is locked", "ORA-28000"),
        ("DPY-6005: connection failed; secret=never-show-this", "DPY-6005"),
    ],
)
def test_admin_login_failure_preserves_sanitized_driver_cause(
    monkeypatch, driver_message: str, expected: str
):
    raw = _config()

    def fail_connect(**kwargs):
        assert kwargs["user"] == "ORACLE_SOURCE_ADMIN"
        assert kwargs["dsn"] == "127.0.0.1:1521/ORACLE_SOURCE_DB"
        assert kwargs["password"] == "secret-value-not-reported"
        raise _OracleDriverError(driver_message)

    monkeypatch.setattr(_Driver, "connect", staticmethod(fail_connect))
    with pytest.raises(RuntimeError, match=expected) as error:
        _probe_login(
            raw,
            "source",
            "ORACLE_SOURCE_ADMIN",
            "secret-value-not-reported",
            "FREE",
            _Driver,
        )
    assert "never-show-this" not in str(error.value)
    assert "secret-value-not-reported" not in str(error.value)
