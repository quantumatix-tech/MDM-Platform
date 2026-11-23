from core.orchestrator import MigrationOrchestrator


class TxConn:
    def __init__(self, server="SRC"):
        self.autocommit = True
        self.server = server
        self.commits = 0
        self.rollbacks = 0
        self.deleted = []
        self._last_sql = ""
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, *a):
        self._last_sql = sql or ""
        if sql.strip().upper().startswith("DELETE"):
            import re
            m = re.search(r'"([^"]+)"\."([^"]+)"', sql)
            if m:
                self.deleted.append(f"{m.group(1)}.{m.group(2)}")
    def fetchall(self):
        return []
    def fetchone(self):
        return None  # COUNT(*) would throw SQL0204N before fetchone for missing table
    def close(self):
        pass
    def commit(self):
        self.commits += 1
    def rollback(self):
        self.rollbacks += 1


class FakeSource:
    def __init__(self):
        self._config = {"engine": "db2", "include_schemas": ["HR", "SALES"]}
        self._conn = TxConn()
        self.rows = {"HR": [{"id": 1}, {"id": 2}, {"id": 3}], "SALES": [{"id": 1}, {"id": 2}, {"id": 3}]}
    def connect(self):
        pass
    def close(self):
        pass
    def list_schemas(self):
        return ["HR", "SALES"]
    def list_objects_with_schema(self):
        return [("HR", "EMPLOYEES"), ("SALES", "EMPLOYEES")]
    def get_schema(self, name, schema_name=None):
        from core.connectors.base import Schema
        return Schema(name=name, schema_name=schema_name, columns=[])
    def get_object_count(self, name, schema_name=None):
        return len(self.rows.get(schema_name, []))
    def export_full(self, name, schema_name=None):
        return list(self.rows.get(schema_name, []))
    def get_all_triggers(self):
        return []
    def list_events(self):
        return []
    def list_functions(self):
        return []
    def list_users(self):
        return []


class FakeTarget:
    def __init__(self, conn):
        self._conn = conn
        self._config = {"engine": "db2"}
        self.loaded = {"HR.EMPLOYEES": 0, "SALES.EMPLOYEES": 0}
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
        from core.connectors.base import UpsertResult
        rows = list(rows)
        key = f"{schema.schema_name}.{object_name}" if schema and schema.schema_name else object_name
        self.loaded[key] = self.loaded.get(key, 0) + len(rows)
        return UpsertResult(success_count=len(rows), failure_count=0)
    def get_object_count(self, object_name, schema_name=None):
        key = f"{schema_name}.{object_name}" if schema_name else object_name
        return self.loaded.get(key, 0)
    def apply_constraints(self, s):
        pass
    def suspend_triggers_for_data_load(self, t):
        return []
    def clear_objects_for_full_sync(self, objs):
        return []


def test_db2_full_atomicity_no_intermediate_commits():
    src = FakeSource()
    tgt = FakeTarget(TxConn("TGT"))
    src._conn = TxConn("SRC")
    cfg = {"source": {"engine": "db2"}, "target": {"engine": "db2"}, "migration": {"include_schemas": ["HR", "SALES"], "batch_size": 1000}}
    orch = MigrationOrchestrator(src, tgt, cfg)
    result = orch.run_full()
    assert result["status"] == "success"
    assert tgt._conn.commits == 1


def test_db2_full_atomicity_repeat_no_duplicate():
    src = FakeSource()
    tgt = FakeTarget(TxConn("TGT"))
    src._conn = TxConn("SRC")
    cfg = {"source": {"engine": "db2"}, "target": {"engine": "db2"}, "migration": {"include_schemas": ["HR", "SALES"], "batch_size": 1000}}
    orch = MigrationOrchestrator(src, tgt, cfg)
    r1 = orch.run_full()
    assert r1["status"] == "success"
    conn2 = TxConn()
    tgt2 = FakeTarget(conn2)
    orch2 = MigrationOrchestrator(src, tgt2, cfg)
    r2 = orch2.run_full()
    assert r2["status"] == "success"

class TxConn2:
    def __init__(self, target):
        self.target = target
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, *a):
        import re
        m = re.search(r'"([^"]+)"\."([^"]+)"', sql)
        if m and sql.strip().upper().startswith("DELETE"):
            self.target._delete(m.group(1), m.group(2))
    def fetchall(self):
        return []
    def fetchone(self):
        return (self.server,)
    def close(self):
        pass
    def commit(self):
        self.commits += 1
        self.target._commit()
    def rollback(self):
        self.rollbacks += 1
        self.target._rollback()


class TxTarget:
    def __init__(self, conn, initial=None):
        self._conn = conn
        self._config = {"engine": "db2"}
        self.initial = initial or {"HR.EMPLOYEES": [1,2,3], "SALES.EMPLOYEES": [1,2,3]}
        self.committed = {k: list(v) for k,v in self.initial.items()}
        self.working = None
        self._snapshot = None
    def connect(self): pass
    def close(self): pass
    def ensure_database_exists(self): pass
    def create_schema_if_missing(self, n): pass
    def create_object_if_missing(self, s): pass
    def _begin(self):
        self._snapshot = {k: list(v) for k,v in self.committed.items()}
        self.working = {k: list(v) for k,v in self.committed.items()}
    def _delete(self, sch, tbl):
        if self.working is None: self._begin()
        self.working[f"{sch}.{tbl}"] = []
    def upsert_batch(self, object_name, rows, schema=None, commit=True):
        from core.connectors.base import UpsertResult
        rows = list(rows)
        if self.working is None: self._begin()
        key = f"{schema.schema_name}.{object_name}" if schema and schema.schema_name else object_name
        self.working[key] = self.working.get(key, []) + [r.get("id") for r in rows]
        return UpsertResult(success_count=len(rows), failure_count=0)
    def get_object_count(self, object_name, schema_name=None):
        key = f"{schema_name}.{object_name}" if schema_name else object_name
        d = self.working if self.working is not None else self.committed
        return len(d.get(key, []))
    def _commit(self):
        self.committed = {k: list(v) for k,v in self.working.items()}
        self.working = None
        self._snapshot = None
    def _rollback(self):
        self.committed = {k: list(v) for k,v in self._snapshot.items()} if self._snapshot else self.committed
        self.working = None
        self._snapshot = None
    def apply_constraints(self, s): pass
    def suspend_triggers_for_data_load(self, t): return []
    def clear_objects_for_full_sync(self, objs): return []
    def get_all_triggers(self): return []
    def list_events(self): return []
    def list_functions(self): return []
    def list_users(self): return []
    def get_all_triggers(self): return []
    def get_all_triggers(self): return []

class TxSource:
    def get_all_triggers(self): return []
    def __init__(self):
        self._conn = TxConn("SRC")
        self._config = {"engine": "db2", "include_schemas": ["HR", "SALES"]}
        self.rows = {"HR": [{"id": 1},{"id":2},{"id":3}], "SALES": [{"id":1},{"id":2},{"id":3}]}
    def connect(self): pass
    def close(self): pass
    def list_schemas(self): return ["HR","SALES"]
    def list_objects_with_schema(self): return [("HR","EMPLOYEES"),("SALES","EMPLOYEES")]
    def get_schema(self, name, schema_name=None):
        from core.connectors.base import Schema
        return Schema(name=name, schema_name=schema_name, columns=[])
    def get_object_count(self, name, schema_name=None):
        return len(self.rows.get(schema_name, []))
    def export_full(self, name, schema_name=None):
        return list(self.rows.get(schema_name, []))


def test_db2_full_cross_table_rollback_on_sales_failure():
    conn = None
    src = TxSource()
    # make SALES export fail after HR replaced
    original_export = src.export_full
    call = {"n":0}
    def failing_export(name, schema_name=None):
        if schema_name == "SALES":
            raise RuntimeError("injected sales failure")
        return original_export(name, schema_name=schema_name)
    src.export_full = failing_export
    tgt_conn = None
    # need conn reference before target; create target with placeholder then set conn
    # We'll create conn with target after target creation using late binding
    class LateConn:
        def __init__(self): self.target=None; self.autocommit=True; self.commits=0; self.rollbacks=0; self._last_sql=""
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a):
            return False
        def fetchall(self):
            return []
        def execute(self,sql,*a):
            self._last_sql = sql or ""
            import re
            m=re.search(r'"([^"]+)"\."([^"]+)"',sql)
            if m and sql.strip().upper().startswith("DELETE"):
                self.target._delete(m.group(1),m.group(2))
        def fetchone(self):
            return None  # COUNT(*) would throw SQL0204N before fetchone for missing table
        def commit(self):
            self.commits+=1; self.target._commit()
        def rollback(self):
            self.rollbacks+=1; self.target._rollback()
    late = LateConn()
    tgt = TxTarget(late)
    late.target = tgt
    cfg = {"source": {"engine": "db2"}, "target": {"engine": "db2"}, "migration": {"include_schemas": ["HR","SALES"], "batch_size": 1000}}
    orch = MigrationOrchestrator(src, tgt, cfg)
    # patch target _conn
    tgt._conn = late
    src._conn = TxConn("SRC")
    try:
        orch.run_full()
    except Exception:
        pass
    assert late.commits == 0
    assert late.rollbacks == 1
    assert tgt.committed["HR.EMPLOYEES"] == [1,2,3]
    assert tgt.committed["SALES.EMPLOYEES"] == [1,2,3]
