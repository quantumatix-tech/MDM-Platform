"""Process-safe locks scoped to the endpoints of one migration."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MigrationAlreadyRunning(RuntimeError):
    """Raised when a live process owns the same migration scope."""


def _endpoint(config: dict[str, Any]) -> dict[str, Any]:
    connection = config.get("connection", {})
    # Credentials never form part of an on-disk lock payload or its identity.
    return {
        "engine": config.get("engine"),
        "host": connection.get("host", "localhost"),
        "port": connection.get("port"),
        "database": connection.get("database"),
    }


class MigrationLock:
    """Atomic lock which permits unrelated endpoint pairs to run together."""

    def __init__(self, config: dict[str, Any], mode: str, directory: str | Path = "state/locks") -> None:
        self.scope = {"mode": mode, "source": _endpoint(config.get("source", {})), "target": _endpoint(config.get("target", {}))}
        encoded = json.dumps(self.scope, sort_keys=True, separators=(",", ":")).encode()
        self.scope_id = hashlib.sha256(encoded).hexdigest()[:20]
        self.path = Path(directory) / f"migration-{self.scope_id}.lock"
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            # Windows can report invalid/stale PIDs as WinError 11 or another
            # generic OSError. Such a lock is recoverable, while access denial
            # above remains evidence of a live process.
            return False
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(), "scope": self.scope}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(existing.get("pid", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pid = 0
            if self._pid_alive(pid):
                raise MigrationAlreadyRunning(f"migration already running for this source/target scope (PID {pid})")
            # A dead or malformed owner cannot permanently block recovery.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return self.acquire()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False
