from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from core.migration_lock import MigrationAlreadyRunning, MigrationLock
from core import status_server as status_server_module
from core.status_server import StatusServer


def _local_temp_dir(name: str) -> Path:
    path = Path(".pytest-local") / f"{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_server(server: StatusServer) -> threading.Thread:
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not server._serving and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server._serving
    return thread


def _stop_server(server: StatusServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_status_server_starts_specifically_on_port_8080():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 8080))
    except OSError:
        pytest.skip("port 8080 is occupied by an external process")
    finally:
        probe.close()
    server = StatusServer(host="127.0.0.1", port=8080)
    server.start()
    thread = _run_server(server)
    try:
        assert server.bound_host == "127.0.0.1"
        assert server.port == 8080
    finally:
        _stop_server(server, thread)


def test_status_server_fails_clearly_when_port_8080_is_occupied():
    class OccupiedServer:
        def __init__(self, *_args, **_kwargs):
            error = OSError(10048, "address already in use")
            error.winerror = 10048
            raise error

    original_server = status_server_module._StatusHTTPServer
    status_server_module._StatusHTTPServer = OccupiedServer
    server = StatusServer(host="127.0.0.1", port=8080)

    try:
        with pytest.raises(RuntimeError, match="Status server port 8080 is unavailable"):
            server.start()
    finally:
        status_server_module._StatusHTTPServer = original_server


def _lock_config() -> dict:
    connection = {"host": "127.0.0.1", "port": 3306, "database": "db"}
    return {
        "source": {"engine": "mysql", "connection": dict(connection)},
        "target": {"engine": "mysql", "connection": dict(connection)},
    }


def test_stale_lock_recovers_from_windows_invalid_pid(monkeypatch):
    lock_dir = _local_temp_dir("stale-lock")
    lock = MigrationLock(_lock_config(), "full", lock_dir)
    lock.path.write_text(json.dumps({"pid": 123456}), encoding="utf-8")

    def invalid_pid(_pid: int, _signal: int) -> None:
        raise OSError(11, "resource temporarily unavailable")

    monkeypatch.setattr(os, "kill", invalid_pid)
    lock.acquire()
    try:
        assert lock.acquired
    finally:
        lock.release()


def test_live_lock_still_blocks_concurrent_migration(monkeypatch):
    lock_dir = _local_temp_dir("live-lock")
    first = MigrationLock(_lock_config(), "full", lock_dir)
    second = MigrationLock(_lock_config(), "full", lock_dir)
    first.acquire()
    try:
        monkeypatch.setattr(MigrationLock, "_pid_alive", staticmethod(lambda _pid: True))
        with pytest.raises(MigrationAlreadyRunning):
            second.acquire()
    finally:
        first.release()


def test_main_releases_status_server_and_lock_after_migration_failure(monkeypatch):
    import migration_platform.__main__ as cli

    config_path = _local_temp_dir("cli-failure") / "config.yaml"
    config_path.write_text(
        "source:\n  engine: mysql\n  connection: {}\n"
        "target:\n  engine: mysql\n  connection: {}\n",
        encoding="utf-8",
    )
    events: list[str] = []

    class FakeLock:
        scope_id = "test-scope"

        def acquire(self) -> None:
            events.append("lock.acquire")

        def release(self) -> None:
            events.append("lock.release")

    class FakeStatusServer:
        port = 19001

        def start(self) -> None:
            events.append("status.start")

        def set_mode(self, _mode: str) -> None:
            pass

        def run(self) -> None:
            pass

        def stop(self) -> None:
            events.append("status.stop")

        def update_status(self, *_args, **_kwargs) -> None:
            pass

        def record_table_stats(self, *_args, **_kwargs) -> None:
            pass

        def record_preflight(self, *_args, **_kwargs) -> None:
            pass

    class FakeProgress:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

    class FailingOrchestrator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run_full(self):
            raise RuntimeError("migration failed")

    monkeypatch.setattr(cli, "SOURCE_CONNECTORS", {"mysql": lambda _config: object()})
    monkeypatch.setattr(cli, "TARGET_CONNECTORS", {"mysql": lambda _config: object()})
    monkeypatch.setattr(cli, "MigrationLock", lambda *_args, **_kwargs: FakeLock())
    monkeypatch.setattr(cli, "StatusServer", lambda **_kwargs: FakeStatusServer())
    monkeypatch.setattr(cli, "MigrationOrchestrator", FailingOrchestrator)
    monkeypatch.setattr(cli, "create_progress_display", lambda *_args: FakeProgress())
    monkeypatch.setattr(cli, "configure_file_logging", lambda **_kwargs: "logs/test.jsonl")
    monkeypatch.setattr(cli, "audit_log", lambda **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["migration_platform", "--config", str(config_path), "--mode", "full"])

    with pytest.raises(RuntimeError, match="migration failed"):
        cli.main()

    assert events == ["lock.acquire", "status.start", "status.stop", "lock.release"]
