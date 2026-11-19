
from core.connectors.base import Schema, UpsertResult
from core.orchestrator import MigrationOrchestrator


class _Db2MockCursor:
    """Query-aware mock cursor for Db2 catalog queries used by tests."""
    def __init__(self, server):
        self.server = server
        self._last_sql = ""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, *a):
        self._last_sql = sql or ""
    def fetchone(self):
        sql = self._last_sql.upper()
        if "COUNT(" in sql:
            return None  # COUNT(*) would throw SQL0204N before fetchone for missing table
        if "CURRENT SERVER" in sql:
            return (self.server,)
        if "TYPE='S'" in sql or "TYPE = 'S'" in sql:
            return None  # not an MQT
        return None
    def fetchall(self):
        return []
    def close(self):
        pass


class _Db2MockConn:
    def __init__(self, server):
        self.server = server
        self.autocommit = True
        self.commits = 0
    def cursor(self):
        return _Db2MockCursor(self.server)
    def commit(self):
        self.commits += 1
    def rollback(self):
        pass
    def close(self):
        pass


class FakeSource:
    def __init__(self):
        self._config = {"engine": "db2", "include_schemas": ["HR", "SALES"]}
        self._conn = _Db2MockConn("SRC")

    def connect(self):
        pass

    def close(self):
        pass

    def _get_selected_schemas(self):
        return ["HR", "SALES"]

    def get_all_triggers(self):
        return []

    def list_events(self):
        return []

    def list_functions(self):
        return []

    def list_users(self):
        return []

    def list_schemas(self):
        return ["HR", "SALES"]

    def list_objects(self):
        return ["EMPLOYEES"]

    def list_objects_with_schema(self):
        return [("HR", "EMPLOYEES"), ("SALES", "EMPLOYEES")]

    def list_extensions(self):
        return []

    def list_types(self):
        return []

    def list_views(self):
        return []

    def list_all_sequences(self):
        return []

    def get_schema(self, object_name, schema_name=None):
        return Schema(name=object_name, schema_name=schema_name or "HR", columns=[])

    def get_object_count(self, object_name, schema_name=None):
        return 3

    def export_full(self, object_name, schema_name=None):
        return [ {"id": i} for i in range(3) ]

    def get_partitioned_tables(self):
        return []

    def list_partitions(self):
        return []


class FakeTarget:
    def __init__(self):
        self._conn = _Db2MockConn("TGT")
        self.loaded = {"HR.EMPLOYEES": 0, "SALES.EMPLOYEES": 0}

    def connect(self):
        pass

    def close(self):
        pass

    def ensure_database_exists(self):
        pass

    def create_schema_if_missing(self, name):
        pass

    def create_object_if_missing(self, schema):
        pass

    def suspend_triggers_for_data_load(self, triggers):
        return []

    def upsert_batch(self, object_name, rows, schema=None, commit=True):
        rows = list(rows)
        key = f"{schema.schema_name}.{object_name}" if schema and schema.schema_name else object_name
        self.loaded[key] = self.loaded.get(key, 0) + len(rows)
        return UpsertResult(success_count=len(rows), failure_count=0)

    def get_object_count(self, object_name, schema_name=None):
        key = f"{schema_name}.{object_name}" if schema_name else object_name
        return self.loaded.get(key, 0)

    def clear_objects_for_full_sync(self, objects):
        return []


def test_db2_real_cli_data_path_loads_3_and_3():
    src = FakeSource()
    tgt = FakeTarget()
    cfg = {
        "source": {"engine": "db2"},
        "target": {"engine": "db2"},
        "migration": {"include_schemas": ["HR", "SALES"]},
    }
    orch = MigrationOrchestrator(src, tgt, cfg)
    result = orch.run_full()
    assert "HR.EMPLOYEES" in result["phases"]["discover"]["objects"]
    assert "SALES.EMPLOYEES" in result["phases"]["discover"]["objects"]
    assert result["phases"]["HR.EMPLOYEES"]["success"] == 3
    assert result["phases"]["SALES.EMPLOYEES"]["success"] == 3
    assert tgt.loaded["HR.EMPLOYEES"] == 3
    assert tgt.loaded["SALES.EMPLOYEES"] == 3
    assert not any("Invalid" in str(e) for e in result.get("errors", []))


def test_db2_validation_empty_checks_not_success():
    src = FakeSource()
    tgt = FakeTarget()
    cfg = {"source": {"engine": "db2"}, "target": {"engine": "db2"}, "migration": {"include_schemas": ["HR", "SALES"]}}
    orch = MigrationOrchestrator(src, tgt, cfg)
    v = orch.validate(objects=[])
    assert v["status"] != "success"
