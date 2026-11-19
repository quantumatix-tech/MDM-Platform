from unittest.mock import MagicMock
from core.connectors.db2.objects.loader import upsert_batch
from core.connectors.base import Schema, Column

def _schema(cols):
    return Schema(name="T", schema_name="S", columns=cols)

def test_no_override_always():
    cols = [Column(name="ID", source_type="INTEGER", nullable=False, is_identity=True, identity_kind="ALWAYS"),
            Column(name="NAME", source_type="VARCHAR", nullable=True, size=50)]
    schema = _schema(cols)
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    try:
        upsert_batch(conn, "S.T", [{"ID": 1, "NAME": "A"}], schema=schema)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "GENERATED ALWAYS" in str(e)

def test_by_default_allowed():
    cols = [Column(name="ID", source_type="INTEGER", nullable=False, is_identity=True, identity_kind="BY DEFAULT"),
            Column(name="NAME", source_type="VARCHAR", nullable=True, size=50)]
    schema = _schema(cols)
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    res = upsert_batch(conn, "S.T", [{"ID": 1, "NAME": "A"}], schema=schema, commit=False)
    assert res.success_count == 1
    executed = cur.execute.call_args[0][0]
    assert "OVERRIDING SYSTEM VALUE" not in executed
