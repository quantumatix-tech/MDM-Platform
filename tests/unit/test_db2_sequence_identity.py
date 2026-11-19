import pytest
from core.connectors.db2.objects.sequence import discover_standalone_sequences


def test_discover_standalone_sequences():
    conn = type("C", (), {})()
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return [("SEQ_ORDER_ID", 24, 100, 5, 100, 2147483647, "N", 20, "N", 100, 10)]
    conn.cursor = lambda: Cur()
    seqs = discover_standalone_sequences(conn, "TEST10")
    assert len(seqs) == 1
    assert seqs[0]["name"] == "SEQ_ORDER_ID"
    assert seqs[0]["start"] == 100
