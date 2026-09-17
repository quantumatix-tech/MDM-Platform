from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.migration_lock import MigrationAlreadyRunning, MigrationLock


def _config(target_database: str = "target") -> dict:
    return {
        "source": {"engine": "mysql", "connection": {"host": "127.0.0.1", "port": 3306, "database": "source"}},
        "target": {"engine": "mysql", "connection": {"host": "127.0.0.1", "port": 3306, "database": target_database}},
    }


def test_same_scope_rejects_second_live_owner(tmp_path: Path):
    first = MigrationLock(_config(), "full", tmp_path)
    first.acquire()
    second = MigrationLock(_config(), "full", tmp_path)
    with pytest.raises(MigrationAlreadyRunning, match="migration already running"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_dead_owner_is_recovered_and_unrelated_scope_is_distinct(tmp_path: Path):
    first = MigrationLock(_config(), "full", tmp_path)
    first.path.parent.mkdir(parents=True, exist_ok=True)
    first.path.write_text(json.dumps({"pid": -1}), encoding="utf-8")
    first.acquire()
    unrelated = MigrationLock(_config("another_target"), "full", tmp_path)
    unrelated.acquire()
    first.release()
    unrelated.release()
