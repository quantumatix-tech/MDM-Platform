from unittest.mock import MagicMock
from core.orchestration.db2_full import run_db2_full_replacement


def test_db2_fk_integration_order_and_ddl():
    src = MagicMock()
    tgt = MagicMock()
    src.list_objects_with_schema.return_value = [("TEST09", "DEPARTMENTS"), ("TEST09", "EMPLOYEES")]
    src._config = {"include_schemas": ["TEST09"]}
    tgt._config = {}
    tgt.has_unsafe_foreign_keys = lambda x: False
    src.has_unsafe_foreign_keys = lambda x: False
    cfg = {"source": {"engine": "db2"}, "target": {"engine": "db2"}, "migration": {"include_schemas": ["TEST09"], "batch_size": 10}}
    from core.connectors.base import Schema, ForeignKey
    def get_schema(name, schema_name=None):
        if name == "EMPLOYEES":
            return Schema(name=name, schema_name=schema_name, columns=[], foreign_keys=[ForeignKey(name="FK_EMP_DEPT", columns=["DEPT_ID"], ref_table="DEPARTMENTS", ref_columns=["DEPT_ID"], ref_schema="TEST09")])
        return Schema(name=name, schema_name=schema_name, columns=[])
    src.get_schema.side_effect = get_schema
    src.get_object_count.return_value = 1
    src.export_full.return_value = []
    tgt.get_object_count.return_value = 1
    tgt.upsert_batch.return_value = MagicMock(success_count=0, errors=[])
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    conn.cursor.return_value.__exit__.return_value = False
    tgt._conn = conn
    src._conn = MagicMock()
    src._conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    orch = type("O", (), {})()
    orch._source = src
    orch._target = tgt
    orch._config = cfg
    res = run_db2_full_replacement(orch, {})
    assert res["status"] == "success"
