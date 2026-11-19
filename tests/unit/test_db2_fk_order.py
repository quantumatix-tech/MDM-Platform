from core.orchestration.db2_fk_order import order_tables_for_full


def test_fk_order_test09():
    tables = ["TEST09.DEPARTMENTS", "TEST09.EMPLOYEES"]
    fks = [("TEST09.EMPLOYEES", "TEST09.DEPARTMENTS")]
    insert, delete = order_tables_for_full(tables, fks)
    assert insert == ["TEST09.DEPARTMENTS", "TEST09.EMPLOYEES"]
    assert delete == ["TEST09.EMPLOYEES", "TEST09.DEPARTMENTS"]


def test_fk_order_cross_schema():
    tables = ["A.T1", "B.T2"]
    fks = [("B.T2", "A.T1")]
    insert, delete = order_tables_for_full(tables, fks)
    assert insert == ["A.T1", "B.T2"]
    assert delete == ["B.T2", "A.T1"]


def test_fk_order_missing_parent():
    try:
        order_tables_for_full(["A.T1"], [("A.T1", "B.T2")])
    except ValueError as e:
        assert "Missing selected parent" in str(e)


def test_fk_order_cycle():
    try:
        order_tables_for_full(["A.T1", "A.T2"], [("A.T1", "A.T2"), ("A.T2", "A.T1")])
    except ValueError as e:
        assert "cycle" in str(e).lower()


def test_fk_order_duplicate():
    try:
        order_tables_for_full(["A.T1", "A.T1"], [])
    except ValueError as e:
        assert "Duplicate" in str(e)
