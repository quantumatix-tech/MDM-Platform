"""Focused regression tests for DB2 full-mode UI reporting.

Verifies that the report-building phases dict contains truthful
discovered/created/existing/skipped counts for DB2 migrations, and that
no intermediate "skipped" status masks work actually performed.
"""

from unittest.mock import MagicMock, patch

from core.connectors.base import Schema, UpsertResult
from core.orchestrator import MigrationOrchestrator


# ---------------------------------------------------------------------------
# Minimal fake connectors for orchestration-level tests
# ---------------------------------------------------------------------------


class _TxConn:
    """Target connection mock that counts commits (no auto-commit on DDL)."""

    def __init__(self):
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.executed.append(sql)
        return None

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class _FakeSource:
    def __init__(self):
        self._conn = None
        self._config = {
            "engine": "db2",
            "include_schemas": ["TESTE2E"],
            "migration": {"include_schemas": ["TESTE2E"]},
        }
        self._tables = {
            "TESTE2E": {
                "EMPLOYEES": Schema(
                    name="EMPLOYEES",
                    schema_name="TESTE2E",
                    columns=[],
                    indexes=[],
                    unique_constraints=[],
                    foreign_keys=[],
                ),
                "DEPARTMENTS": Schema(
                    name="DEPARTMENTS",
                    schema_name="TESTE2E",
                    columns=[],
                    indexes=[],
                    unique_constraints=[],
                    foreign_keys=[],
                ),
            }
        }
        self._rows = {"TESTE2E": {"EMPLOYEES": [{"id": 1}, {"id": 2}], "DEPARTMENTS": [{"id": 1}]}}
        self._views = [MagicMock(name="V_EMP", schema_name="TESTE2E")]
        self._sequences = [{"name": "SEQ_EMP", "schema": "TESTE2E"}]
        self._udts = [{"name": "EMP_CODE", "schema": "TESTE2E"}]
        self._aliases = [{"name": "EMP_ALIAS", "schema": "TESTE2E"}]
        self._grants = [MagicMock(object_type="TABLE", object_name="EMPLOYEES", grantee="PUBLIC",
                                   privilege="SELECT", schema_name="TESTE2E")]

    def connect(self):
        pass

    def close(self):
        pass

    def list_schemas(self):
        return ["TESTE2E", "SYSIBM", "SYSCAT"]

    def list_objects_with_schema(self):
        return [("TESTE2E", "EMPLOYEES"), ("TESTE2E", "DEPARTMENTS")]

    def get_schema(self, name, schema_name=None):
        return self._tables[schema_name][name]

    def get_object_count(self, name, schema_name=None):
        return len(self._rows.get(schema_name, {}).get(name, []))

    def export_full(self, name, schema_name=None):
        return list(self._rows.get(schema_name, {}).get(name, []))

    def get_all_triggers(self):
        return []

    def list_events(self):
        return []

    def list_functions(self):
        return []

    def list_users(self):
        return []

    def list_db2_grants(self):
        return [dict(g, schema_name="TESTE2E") for g in self._grants]

    def list_grants(self):
        return list(self._grants)

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        return None

    def fetchall(self):
        return []

    def fetchone(self):
        return ["TESTE2E"]


class _FakeTarget:
    def __init__(self, conn):
        self._conn = conn
        self._config = {"engine": "db2"}
        self.loaded = {}

    def connect(self):
        pass

    def close(self):
        pass

    def ensure_database_exists(self):
        pass

    def create_schema_if_missing(self, n):
        pass

    def create_object_if_missing(self, s):
        pass

    def upsert_batch(self, object_name, rows, schema=None, commit=True):
        rows = list(rows)
        key = f"{schema.schema_name}.{object_name}" if schema and schema.schema_name else object_name
        self.loaded[key] = self.loaded.get(key, 0) + len(rows)
        return UpsertResult(success_count=len(rows), failure_count=0)

    def get_object_count(self, object_name, schema_name=None):
        key = f"{schema_name}.{object_name}" if schema_name else object_name
        return self.loaded.get(key, 0)

    def apply_constraints(self, s):
        return []

    def suspend_triggers_for_data_load(self, t):
        return []

    def clear_objects_for_full_sync(self, objs):
        return []

    def restore_suspended_triggers(self):
        pass

    def get_all_triggers(self):
        return []

    def list_events(self):
        return []

    def list_functions(self):
        return []

    def list_users(self):
        return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_orch(cfg=None):
    if cfg is None:
        cfg = {
            "source": {"engine": "db2"},
            "target": {"engine": "db2"},
            "migration": {
                "include_schemas": ["TESTE2E"],
                "batch_size": 1000,
                "mode": "full",
            },
        }
    src = _FakeSource()
    conn = _TxConn()
    tgt = _FakeTarget(conn)
    return MigrationOrchestrator(src, tgt, cfg), conn


def _patch_repl(return_value):
    def _side_effect(orchestrator, result):
        result["status"] = return_value.get("status", "success")
        result["phases"].update(return_value.get("phases", {}))
        return return_value
    return patch("core.orchestration.db2_full.run_db2_full_replacement", side_effect=_side_effect)


def test_db2_data_phase_not_skipped():
    """Issue #1: data phase must report success with counts, not 'skipped'."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {
                "status": "success",
                "total_rows": 3,
                "success": 3,
                "failure": 0,
            },
            "validation": {
                "mode": "count",
                "status": "success",
                "checks": {
                    "TESTE2E.EMPLOYEES": {
                        "source_count": 3,
                        "target_count": 3,
                        "match": True,
                        "status": "success",
                    }
                },
            },
        },
    }):
        result = orch.run_full()

    assert result["status"] == "success"
    phases = result["phases"]

    # data phase must exist and not be "skipped"
    assert "data" in phases, "data phase missing from report"
    data_phase = phases["data"]
    assert data_phase.get("status") != "skipped", f"data phase is skipped: {data_phase}"
    assert data_phase.get("status") == "success"
    assert data_phase.get("total_rows", 0) > 0, "data phase should report total rows"
    assert data_phase.get("success", 0) > 0, "data phase should report success count"

    # data_load (legacy key) should NOT be set to "skipped" for DB2
    assert phases.get("data_load") is None or phases.get("data_load", {}).get("status") != "skipped"


def test_db2_object_type_phases_have_counts():
    """Issue #2: views, mqts, triggers, functions, sequences, UDTs must show counts > 0."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "views": {"V_EMP": "created"},
            "materialized_views": {},
            "triggers": {},
            "functions": {"TESTE2E.PROC1": "created"},
            "create_sequences": {"SEQ_EMP": "created"},
            "advance_sequences": {"SEQ_EMP": "created"},
            "custom_types": {"EMP_CODE": "created"},
            "grants": {"TABLE EMPLOYEES TO PUBLIC": "ok"},
            "validation": {
                "mode": "count",
                "checks": {
                    "TESTE2E.EMPLOYEES": {
                        "match": True,
                        "source_count": 2,
                        "target_count": 2,
                    },
                    "TESTE2E.DEPARTMENTS": {
                        "match": True,
                        "source_count": 1,
                        "target_count": 1,
                    },
                },
                "status": "success",
            },
            "full_target_sync": {"strategy": "db2-transactional", "status": "success"},
        },
    }):
        result = orch.run_full()

    phases = result["phases"]

    # Views
    views = phases.get("views", {})
    assert len(views) > 0, f"views should have count > 0, got {views}"

    # Functions/routines
    funcs = phases.get("functions", {})
    assert len(funcs) > 0, f"functions should have count > 0, got {funcs}"

    # Sequences
    seqs = phases.get("create_sequences", {})
    assert len(seqs) > 0, f"create_sequences should have count > 0, got {seqs}"

    # Custom types / UDTs
    types = phases.get("custom_types", {})
    assert len(types) > 0, f"custom_types should have count > 0, got {types}"

    # Grants — should be a dict with count semantics, not a bare list
    grants = phases.get("grants", {})
    assert isinstance(grants, dict), f"grants should be a dict, got {type(grants)}"
    assert len(grants) > 0, f"grants should have count > 0, got {grants}"


def test_db2_validation_checks_have_counts():
    """Issue #3: validation checks must include source_count and target_count."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "validation": {
                "mode": "count",
                "checks": {
                    "TESTE2E.EMPLOYEES": {
                        "match": True,
                        "source_count": 2,
                        "target_count": 2,
                        "status": "success",
                    },
                },
                "status": "success",
            },
            "full_target_sync": {"strategy": "db2-transactional", "status": "success"},
        },
    }):
        result = orch.run_full()

    validation = result["phases"]["validation"]
    checks = validation["checks"]
    for obj, check in checks.items():
        assert "source_count" in check, f"validation check for {obj} missing source_count"
        assert "target_count" in check, f"validation check for {obj} missing target_count"
        assert check["source_count"] is not None
        assert check["target_count"] is not None


def test_db2_grants_have_count_semantics():
    """Issue #4: grants must be a dict with per-grant status, not a bare list of strings."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "grants": {
                "TABLE EMPLOYEES TO PUBLIC": "ok",
                "TABLE DEPARTMENTS TO PUBLIC": "ok",
            },
            "validation": {"mode": "count", "checks": {}, "status": "success"},
            "full_target_sync": {"strategy": "db2-transactional", "status": "success"},
        },
    }):
        result = orch.run_full()

    grants = result["phases"]["grants"]
    assert isinstance(grants, dict), f"grants must be dict, got {type(grants)}"
    # Every value should be a status string, not a bare sentence fragment
    for key, val in grants.items():
        assert isinstance(val, str), f"grant {key} value must be string"
        assert val in ("ok", "already_present", "would_grant") or val.startswith("failed:"), \
            f"grant {key} has unexpected status: {val}"


def test_db2_schemas_filtered_by_include_schemas():
    """Issue #5: schemas phase must only include schemas from include_schemas, not all catalog schemas."""
    orch, conn = _build_orch()
    with _patch_repl({"status": "success"}):
        result = orch.run_full()

    schemas = result["phases"]["schemas"]
    # Only TESTE2E should be present, not SYSIBM/SYSCAT
    assert "TESTE2E" in schemas, f"TESTE2E should be in schemas, got {schemas}"
    assert "SYSIBM" not in schemas, f"SYSIBM should not be in schemas (not in include_schemas): {schemas}"
    assert "SYSCAT" not in schemas, f"SYSCAT should not be in schemas (not in include_schemas): {schemas}"
    assert len(schemas) == 1, f"Expected exactly 1 schema (TESTE2E), got {len(schemas)}: {schemas}"


def test_db2_full_target_sync_not_fabricated():
    """FULL safety: if replacement did not report full_target_sync, orchestrator must not fabricate success."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "validation": {"mode": "count", "checks": {}, "status": "success"},
            # NOTE: no full_target_sync phase reported by the replacement
        },
    }):
        result = orch.run_full()

    assert "full_target_sync" not in result["phases"], \
        f"full_target_sync must not be fabricated: {result['phases'].get('full_target_sync')}"


def test_db2_full_target_sync_preserves_replacement_status():
    """full_target_sync must reflect the replacement's actual status, never hardcoded success."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "full_target_sync": {
                "strategy": "db2-transactional",
                "status": "failed",
                "cleared": [],
                "reason": "replacement did not execute replacement DML",
            },
            "validation": {"mode": "count", "checks": {}, "status": "success"},
        },
    }):
        result = orch.run_full()

    fts = result["phases"]["full_target_sync"]
    assert fts.get("status") == "failed", f"full_target_sync status must not be overwritten: {fts}"
    assert fts.get("reason") is not None


def test_db2_count_mismatch_fails_status():
    """A source/target validation count mismatch must produce a failed final status."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "validation": {
                "mode": "count",
                "status": "success",
                "checks": {
                    "TESTE2E.EMPLOYEES": {
                        "source_count": 3,
                        "target_count": 2,
                        "match": False,
                        "status": "failed",
                    },
                },
            },
        },
    }):
        result = orch.run_full()

    assert result["status"] == "failed", f"count mismatch must fail the run: {result.get('status')}"


def test_db2_missing_validation_fails_status():
    """Missing Db2 FULL validation checks must produce a failed final status."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            # NOTE: no validation phase reported by the replacement
        },
    }):
        result = orch.run_full()

    assert result["status"] == "failed", f"missing validation must fail the run: {result.get('status')}"


def test_db2_views_mqts_aliases_separate_counts():
    """Views, MQTs and aliases must be reported with separate counts, never merged."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "success",
        "phases": {
            "data": {"status": "success", "total_rows": 3, "success": 3, "failure": 0},
            "views": {"V_EMP": "created"},
            "materialized_views": {"MQT_ORDER_SUMMARY": "created+refreshed"},
            "aliases": {"EMP_ALIAS": "created"},
            "validation": {"mode": "count", "checks": {}, "status": "success"},
            "full_target_sync": {"strategy": "db2-transactional", "status": "success"},
        },
    }):
        result = orch.run_full()

    phases = result["phases"]

    views = phases.get("views", {})
    mqts = phases.get("materialized_views", {})
    aliases = phases.get("aliases", {})

    # Separate dictionaries with separate counts
    assert isinstance(views, dict) and isinstance(mqts, dict) and isinstance(aliases, dict)
    assert len(views) == 1, f"views count should be 1, got {views}"
    assert len(mqts) == 1, f"materialized_views count should be 1, got {mqts}"
    assert len(aliases) == 1, f"aliases count should be 1, got {aliases}"
    assert "V_EMP" in views
    assert "MQT_ORDER_SUMMARY" in mqts
    assert "EMP_ALIAS" in aliases
    # Views and MQTs must never be the same object/merged
    assert views is not mqts


def test_db2_replacement_failure_propagates_failed_status():
    """If Db2 FULL replacement reports failure, the run status must be failed."""
    orch, conn = _build_orch()
    with _patch_repl({
        "status": "failed",
        "error": "replacement DML failed",
        "phases": {
            "full_target_sync": {"strategy": "db2-transactional", "status": "failed", "cleared": []},
            "validation": {"mode": "count", "checks": {}, "status": "failed"},
        },
    }):
        result = orch.run_full()

    assert result["status"] == "failed", f"replacement failure must fail the run: {result.get('status')}"
    assert "replacement DML failed" in str(result.get("error", ""))
