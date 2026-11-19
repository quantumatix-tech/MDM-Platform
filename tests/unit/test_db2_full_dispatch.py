from unittest.mock import patch, MagicMock

from core.orchestrator import MigrationOrchestrator


def _make_orchestrator(engine="db2"):
    source = MagicMock()
    target = MagicMock()
    source._config = {"engine": engine}
    target._config = {"engine": engine}
    config = {"source": {"engine": engine}, "target": {"engine": engine}, "migration": {}}
    return MigrationOrchestrator(source, target, config), source, target


def test_db2_full_dispatch_invoked_once():
    orch, source, target = _make_orchestrator("db2")
    # minimal all_schemas empty so schema prep complete
    with patch("core.orchestration.db2_full.run_db2_full_replacement", side_effect=NotImplementedError("x")) as mock_dispatch:
        try:
            orch.run_full()
        except Exception:
            pass
        assert mock_dispatch.call_count == 1


def test_db2_full_dispatch_failure_is_failed():
    orch, source, target = _make_orchestrator("db2")
    with patch("core.orchestration.db2_full.run_db2_full_replacement", side_effect=NotImplementedError("boom")):
        result = orch.run_full()
        assert result.get("status") == "failed"


def test_non_db2_uses_generic_path():
    orch, source, target = _make_orchestrator("mssql")
    with patch("core.orchestration.db2_full.run_db2_full_replacement") as mock_dispatch:
        try:
            orch.run_full()
        except Exception:
            pass
        assert mock_dispatch.call_count == 0
