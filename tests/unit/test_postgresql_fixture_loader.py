"""Unit tests for PostgreSQL E2E fixture client resolution and execution."""
from __future__ import annotations

import os
import subprocess

import pytest

from tests.e2e.postgresql.setup import fixture_loader


def _make_executable(path) -> str:
    path.write_text("native client placeholder", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_psql_path_override_takes_precedence(monkeypatch, tmp_path):
    configured = _make_executable(tmp_path / "configured-psql.exe")
    discovered = _make_executable(tmp_path / "path-psql.exe")
    monkeypatch.setattr(fixture_loader, "_IS_WINDOWS", True)
    monkeypatch.setenv("PSQL_PATH", configured)
    monkeypatch.setattr(fixture_loader.shutil, "which", lambda _: discovered)

    assert fixture_loader._psql_path() == os.path.abspath(configured)


def test_valid_psql_path_is_accepted(monkeypatch, tmp_path):
    configured = _make_executable(tmp_path / "psql.exe")
    monkeypatch.setattr(fixture_loader, "_IS_WINDOWS", True)
    monkeypatch.setenv("PSQL_PATH", configured)

    assert fixture_loader._psql_path() == os.path.abspath(configured)


def test_missing_psql_path_target_fails_clearly(monkeypatch, tmp_path):
    missing = str(tmp_path / "missing-psql.exe")
    monkeypatch.setattr(fixture_loader, "_IS_WINDOWS", True)
    monkeypatch.setenv("PSQL_PATH", missing)

    with pytest.raises(SystemExit, match="PSQL_PATH must point to an existing"):
        fixture_loader._psql_path()


@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_windows_wrapper_psql_path_is_rejected(monkeypatch, tmp_path, suffix):
    wrapper = _make_executable(tmp_path / f"psql{suffix}")
    monkeypatch.setattr(fixture_loader, "_IS_WINDOWS", True)
    monkeypatch.setenv("PSQL_PATH", wrapper)

    with pytest.raises(SystemExit, match="wrappers are not supported"):
        fixture_loader._psql_path()


def test_path_native_psql_exe_discovery(monkeypatch, tmp_path):
    native = _make_executable(tmp_path / "psql.exe")
    monkeypatch.setattr(fixture_loader, "_IS_WINDOWS", True)
    monkeypatch.delenv("PSQL_PATH", raising=False)
    calls = []

    def which(command):
        calls.append(command)
        return native

    monkeypatch.setattr(fixture_loader.shutil, "which", which)

    assert fixture_loader._psql_path() == native
    assert calls == ["psql.exe"]


def test_path_stale_wrapper_is_not_accepted(monkeypatch, tmp_path):
    wrapper = _make_executable(tmp_path / "psql.CMD")
    monkeypatch.setattr(fixture_loader, "_IS_WINDOWS", True)
    monkeypatch.delenv("PSQL_PATH", raising=False)
    monkeypatch.setattr(fixture_loader.shutil, "which", lambda _: wrapper)

    with pytest.raises(SystemExit, match="not a native executable"):
        fixture_loader._psql_path()


def _mock_psql(monkeypatch, *, returncode: int, stdout: str = "", stderr: str = ""):
    calls = {}

    def run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(fixture_loader, "_psql_path", lambda: "psql-native")
    monkeypatch.setattr(fixture_loader.subprocess, "run", run)
    return calls


def test_nonzero_return_code_is_failure(monkeypatch, tmp_path):
    _mock_psql(monkeypatch, returncode=2, stderr="connection failed")

    result = fixture_loader.run_sql_file(
        str(tmp_path / "fixture.sql"), "localhost", 5432, "postgres", None, "e2e"
    )

    assert not result.ok
    assert result.error == "connection failed"


def test_zero_return_code_preserves_stderr(monkeypatch, tmp_path):
    _mock_psql(monkeypatch, returncode=0, stderr="client diagnostic")

    result = fixture_loader.run_sql_file(
        str(tmp_path / "fixture.sql"), "localhost", 5432, "postgres", None, "e2e"
    )

    assert result.ok
    assert result.error == "client diagnostic"


def test_command_disables_psqlrc_and_password_prompt(monkeypatch, tmp_path):
    calls = _mock_psql(monkeypatch, returncode=0)

    fixture_loader.run_sql_file(
        str(tmp_path / "fixture.sql"), "localhost", 5432, "postgres", None, "e2e"
    )

    assert "-X" in calls["args"]
    assert "-w" in calls["args"]

