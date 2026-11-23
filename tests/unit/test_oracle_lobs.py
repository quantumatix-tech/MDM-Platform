"""Regression coverage for Oracle NULL, EMPTY, and populated LOB values."""
from __future__ import annotations

import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.connectors.base import Column, Schema
from core.connectors.oracle.precision import (
    OracleLobPayload,
    compare_oracle_rows,
    oracle_xml_storage_supported,
    transport_bind_expression,
    transport_select_expression,
)
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import OracleTargetConnector, _oracle_index_inventory


class FakeOracleLob:
    def __init__(self, value: bytes | str, close_error: str | None = None) -> None:
        self.value = value
        self.closed = False
        self.close_error = close_error

    def size(self) -> int:
        return len(self.value) if isinstance(self.value, bytes) else len(self.value.encode("utf-16-be")) // 2

    def read(self, offset: int, amount: int):
        return self.value[offset - 1:offset - 1 + amount]

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError(self.close_error)


class FakeTargetLob:
    def __init__(self) -> None:
        self.writes: list[tuple[bytes | str, int]] = []
        self.closed = False

    def write(self, value, offset=1) -> None:
        self.writes.append((value, offset))

    def close(self) -> None:
        self.closed = True


def _payload(value: bytes | str, kind: str) -> OracleLobPayload:
    return OracleLobPayload.from_value(value, kind)


def test_source_preserves_null_empty_and_populated_blob_clob_values(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "oracledb", SimpleNamespace(DB_TYPE_NUMBER=object())
    )
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("ID",), ("BLOB_VALUE",), ("CLOB_VALUE",)]
    source_lob_rows = [
        (Decimal(1), None, None),
        (Decimal(2), FakeOracleLob(b""), FakeOracleLob("")),
        (Decimal(3), FakeOracleLob(b"\x00\x01payload"), FakeOracleLob("雪🙂")),
    ]
    fetched_lobs = [
        value for row in source_lob_rows for value in row
        if isinstance(value, FakeOracleLob)
    ]
    cursor.fetchmany.side_effect = [source_lob_rows, []]
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = connection
    source._schema_cache["T"] = Schema("T", primary_key=["ID"], columns=[
        Column("ID", "NUMBER"),
        Column("BLOB_VALUE", "BLOB"),
        Column("CLOB_VALUE", "CLOB"),
    ])
    source.enable_precision_safe_full_load()

    rows = list(source.export_full("T"))

    assert rows[0]["BLOB_VALUE"] is None and rows[0]["CLOB_VALUE"] is None
    assert rows[1]["BLOB_VALUE"].empty and rows[1]["CLOB_VALUE"].empty
    assert b"".join(rows[2]["BLOB_VALUE"].chunks()) == b"\x00\x01payload"
    assert "".join(rows[2]["CLOB_VALUE"].chunks()) == "雪🙂"
    assert all(not locator.closed for locator in fetched_lobs)
    assert "ORDER BY \"ID\"" in cursor.execute.call_args.args[0]
    for row in rows:
        for name in ("BLOB_VALUE", "CLOB_VALUE"):
            if isinstance(row[name], OracleLobPayload):
                row[name].close()


def test_source_serializes_xmltype_to_spooled_unicode_clob(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(DB_TYPE_NUMBER=object()))
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("ID",), ("X",)]
    fetched_xml = FakeOracleLob(
        "<文書>雪🙂</文書>", "ORA-22289: cannot close unopened LOB"
    )
    cursor.fetchmany.side_effect = [[(1, fetched_xml)], []]
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = connection
    source._schema_cache["T"] = Schema("T", primary_key=["ID"], columns=[
        Column("ID", "NUMBER"), Column("X", "XMLTYPE"),
    ])
    source.enable_precision_safe_full_load()

    rows = list(source.export_full("T"))

    assert "XMLSERIALIZE(DOCUMENT \"X\" AS CLOB" in cursor.execute.call_args.args[0]
    assert "".join(rows[0]["X"].chunks()) == "<文書>雪🙂</文書>"
    assert not fetched_xml.closed
    rows[0]["X"].close()


def test_source_materialization_never_closes_fetched_unopened_or_invalid_lob_locator():
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    unopened = FakeOracleLob(
        b"blob-value", "ORA-22289: cannot perform operation on an unopened LOB"
    )
    invalid = FakeOracleLob(
        "<x>雪</x>", "ORA-64219: invalid LOB locator encountered"
    )

    blob = source._materialize(unopened, "BLOB", spool_lob=True)
    xml = source._materialize(invalid, "CLOB", spool_lob=True)
    try:
        assert b"".join(blob.chunks()) == b"blob-value"
        assert "".join(xml.chunks()) == "<x>雪</x>"
        assert not unopened.closed and not invalid.closed
    finally:
        blob.close()
        xml.close()


def test_closing_source_stream_closes_cursor_without_closing_owned_connection(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(DB_TYPE_NUMBER=object()))
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("ID",), ("B",)]
    fetched = FakeOracleLob(b"blob", "ORA-22289: unopened locator")
    cursor.fetchmany.return_value = [(1, fetched)]
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = connection
    source._schema_cache["T"] = Schema("T", columns=[
        Column("ID", "NUMBER"), Column("B", "BLOB"),
    ])
    source.enable_precision_safe_full_load()

    rows = source.export_full("T")
    row = next(rows)
    rows.close()

    assert b"".join(row["B"].chunks()) == b"blob"
    assert not fetched.closed
    row["B"].close()
    connection.close.assert_not_called()
    connection.cursor.return_value.__exit__.assert_called_once()


def test_target_uses_empty_lob_literals_and_streams_large_payloads(monkeypatch):
    oracle = SimpleNamespace(
        DB_TYPE_NUMBER=object(), DB_TYPE_VARCHAR=object(),
        DB_TYPE_BLOB=object(), DB_TYPE_CLOB=object(), DB_TYPE_NCLOB=object(),
    )
    monkeypatch.setitem(sys.modules, "oracledb", oracle)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    target_lobs = [FakeTargetLob(), FakeTargetLob()]
    connection.createlob.side_effect = target_lobs
    target = OracleTargetConnector({})
    target._conn = connection
    target.enable_precision_safe_full_load()
    schema = Schema("T", columns=[
        Column("ID", "NUMBER"), Column("B", "BLOB"), Column("C", "CLOB"),
    ])
    large_blob = bytes(range(256)) * 1024
    large_text = ("雪🙂Oracle" * 12000)
    payload_blob = _payload(large_blob, "BLOB")
    payload_clob = _payload(large_text, "CLOB")

    result = target.upsert_batch("T", iter([
        {"ID": 1, "B": None, "C": None},
        {"ID": 2, "B": _payload(b"", "BLOB"), "C": _payload("", "CLOB")},
        {"ID": 3, "B": payload_blob, "C": payload_clob},
    ]), schema)

    assert result.success_count == 3
    statements = [call.args[0] for call in cursor.executemany.call_args_list]
    assert "VALUES (:1, NULL, NULL)" in statements[0]
    assert "EMPTY_BLOB()" in statements[1] and "EMPTY_CLOB()" in statements[1]
    assert "VALUES (:1, :2, :3)" in statements[2]
    assert b"".join(chunk for chunk, _ in target_lobs[0].writes) == large_blob
    assert "".join(chunk for chunk, _ in target_lobs[1].writes) == large_text
    assert max(len(chunk) for chunk, _ in target_lobs[0].writes) <= 64 * 1024
    assert max(len(chunk) for chunk, _ in target_lobs[1].writes) <= 64 * 1024


def test_target_reconstructs_native_xmltype_from_streamed_clob(monkeypatch):
    oracle = SimpleNamespace(
        DB_TYPE_NUMBER=object(), DB_TYPE_VARCHAR=object(), DB_TYPE_CLOB=object(),
    )
    monkeypatch.setitem(sys.modules, "oracledb", oracle)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    locator = FakeTargetLob()
    connection.createlob.return_value = locator
    target = OracleTargetConnector({})
    target._conn = connection
    target.enable_precision_safe_full_load()
    schema = Schema("T", columns=[Column("ID", "NUMBER"), Column("X", "XMLTYPE")])

    result = target.upsert_batch("T", iter([{
        "ID": 1, "X": _payload('<doc a="雪">🙂</doc>', "CLOB"),
    }]), schema)

    assert result.success_count == 1
    statement = cursor.executemany.call_args.args[0]
    assert "XMLTYPE(:2)" in statement
    assert "".join(chunk for chunk, _ in locator.writes) == '<doc a="雪">🙂</doc>'
    assert locator.closed is False
    connection.close.assert_not_called()


def test_target_connection_remains_owned_through_19_lob_rows_and_verification(monkeypatch):
    oracle = SimpleNamespace(
        DB_TYPE_NUMBER=object(), DB_TYPE_VARCHAR=object(),
        DB_TYPE_CLOB=object(), DB_TYPE_BLOB=object(),
    )
    monkeypatch.setitem(sys.modules, "oracledb", oracle)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    locators = [FakeTargetLob() for _ in range(38)]
    connection.createlob.side_effect = locators
    target = OracleTargetConnector({"source_engine": "oracle"})
    target._conn = connection
    target.enable_precision_safe_full_load()
    schema = Schema("T", primary_key=["ID"], columns=[
        Column("ID", "NUMBER"), Column("X", "XMLTYPE"), Column("B", "BLOB"),
    ])
    rows = [{
        "ID": i,
        "X": _payload(f"<x>{i}</x>", "CLOB"),
        "B": _payload(bytes([i]), "BLOB"),
    } for i in range(1, 20)]

    result = target.upsert_batch("T", iter(rows), schema)

    assert result.success_count == 19
    target.verify_replacement_table_definitions = MagicMock()
    plan = SimpleNamespace(
        blockers=[], inspection_errors=[], cleanup=[], missing=[],
        as_dict=lambda: {"status": "ready"},
    )
    target.plan_schema_replacement = MagicMock(return_value=plan)
    verification = target.verify_schema_replacement(schemas=[schema])

    assert verification["status"] == "verified"
    assert target._conn is connection
    target.verify_replacement_table_definitions.assert_called_once_with([schema])
    connection.close.assert_not_called()


def test_target_export_keeps_empty_lob_locator_distinct_from_null(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "oracledb", SimpleNamespace(DB_TYPE_NUMBER=object())
    )
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("ID",), ("B",)]
    cursor.fetchmany.side_effect = [[(1, FakeOracleLob(b""))], []]
    target = OracleTargetConnector({})
    target._conn = connection
    target._managed_schemas["T"] = Schema(
        "T", primary_key=["ID"], columns=[Column("ID", "NUMBER"), Column("B", "BLOB")]
    )
    target.enable_precision_safe_full_load()

    rows = target.export_full("T")
    row = next(rows)

    assert row["B"].empty
    assert "ORDER BY \"ID\"" in cursor.execute.call_args.args[0]
    row["B"].close()
    rows.close()


def test_oracle_lob_validation_distinguishes_null_empty_content_and_reports_row_column():
    schema = Schema("T", primary_key=["ID"], columns=[
        Column("ID", "NUMBER"), Column("B", "BLOB"), Column("C", "CLOB"),
    ])
    source = [
        {"ID": 1, "B": None, "C": None},
        {"ID": 2, "B": _payload(b"", "BLOB"), "C": _payload("", "CLOB")},
        {"ID": 3, "B": _payload(b"same", "BLOB"), "C": _payload("雪", "CLOB")},
    ]
    target = [
        {"ID": 1, "B": None, "C": None},
        {"ID": 2, "B": _payload(b"", "BLOB"), "C": _payload("", "CLOB")},
        {"ID": 3, "B": _payload(b"same", "BLOB"), "C": _payload("月", "CLOB")},
    ]

    result = compare_oracle_rows(iter(source), iter(target), schema)

    assert result["match"] is False
    assert result["mismatches"] == [{
        "row": {"ID": "3"}, "column": "C",
        "source": "CLOB payload (1 units)",
        "target": "CLOB payload (1 units)",
    }]

    null_empty = compare_oracle_rows(
        iter([{"ID": 1, "B": None, "C": None}]),
        iter([{"ID": 1, "B": _payload(b"", "BLOB"), "C": _payload("", "CLOB")}]),
        schema,
    )
    assert [item["column"] for item in null_empty["mismatches"]] == ["B", "C"]
    assert all(item["source"] is None and item["target"] == "EMPTY"
               for item in null_empty["mismatches"])


def test_xmltype_transport_is_native_and_compares_xml_infosets_preserving_text():
    xml = Column("XML_VALUE", "XMLTYPE")
    assert "XMLSERIALIZE(DOCUMENT \"XML_VALUE\" AS CLOB" in transport_select_expression(xml)
    assert transport_bind_expression(xml, ":1") == "XMLTYPE(:1)"

    schema = Schema("T", primary_key=["ID"], columns=[
        Column("ID", "NUMBER"), xml,
    ])
    same_info_set = compare_oracle_rows(
        iter([{"ID": 1, "XML_VALUE": _payload('<r b="2" a="1">雪<x/></r>', "CLOB")}]),
        iter([{"ID": 1, "XML_VALUE": _payload('<r a="1" b="2">雪<x></x></r>', "CLOB")}]),
        schema,
    )
    assert same_info_set["match"]

    changed_text = compare_oracle_rows(
        iter([{"ID": 1, "XML_VALUE": _payload("<r>雪</r>", "CLOB")}]),
        iter([{"ID": 1, "XML_VALUE": _payload("<r>月</r>", "CLOB")}]),
        schema,
    )
    assert changed_text["match"] is False

    null_mismatch = compare_oracle_rows(
        iter([{"ID": 1, "XML_VALUE": None}]),
        iter([{"ID": 1, "XML_VALUE": _payload("<r/>", "CLOB")}]),
        schema,
    )
    assert null_mismatch["match"] is False


def test_xmltype_storage_gate_accepts_known_untyped_modes_and_rejects_unknown_or_schema_based():
    assert oracle_xml_storage_supported({
        "xmlschema": None, "storage_type": "TRANSPORTABLE BINARY", "nonschema": "YES",
    })
    assert oracle_xml_storage_supported({
        "xmlschema": None, "storage_type": "CLOB", "nonschema": None,
    })
    assert not oracle_xml_storage_supported(None)
    assert not oracle_xml_storage_supported({
        "xmlschema": "urn:example", "storage_type": "BINARY", "nonschema": "YES",
    })
    assert not oracle_xml_storage_supported({
        "xmlschema": None, "storage_type": "OBJECT-RELATIONAL", "nonschema": None,
    })


def test_json_semantic_and_raw_byte_validation_and_null_preservation():
    schema = Schema("T", primary_key=["ID"], columns=[
        Column("ID", "NUMBER"), Column("J", "JSON"), Column("R", "RAW"),
    ])
    result = compare_oracle_rows(
        iter([{"ID": 1, "J": '{"n":1,"a":[true,null]}', "R": b"\x00\xff"},
              {"ID": 2, "J": None, "R": None}]),
        iter([{"ID": 1, "J": '{"a":[true,null],"n":1}', "R": b"\x00\xff"},
              {"ID": 2, "J": None, "R": None}]),
        schema,
    )
    assert result["match"]
    mismatch = compare_oracle_rows(
        iter([{"ID": 1, "J": "1", "R": b"\x00\xff"}]),
        iter([{"ID": 1, "J": "1", "R": b"\x00\xfe"}]),
        schema,
    )
    assert [item["column"] for item in mismatch["mismatches"]] == ["R"]
    scalar_type_mismatch = compare_oracle_rows(
        iter([{"ID": 1, "J": "true", "R": b"x"}]),
        iter([{"ID": 1, "J": "1", "R": b"x"}]),
        schema,
    )
    assert [item["column"] for item in scalar_type_mismatch["mismatches"]] == ["J"]


def test_native_json_uses_oracledb_json_bind_type(monkeypatch):
    json_type = object()
    oracle = SimpleNamespace(DB_TYPE_NUMBER=object(), DB_TYPE_JSON=json_type)
    monkeypatch.setitem(sys.modules, "oracledb", oracle)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    target = OracleTargetConnector({})
    target._conn = connection
    target.enable_precision_safe_full_load()
    schema = Schema("T", columns=[Column("J", "JSON")])

    result = target.upsert_batch("T", iter([{"J": {"n": 1, "ok": True}}]), schema)

    assert result.success_count == 1
    assert cursor.executemany.call_args.args[1] == [({"n": 1, "ok": True},)]
    assert cursor.setinputsizes.call_args.args == (json_type,)


def test_target_index_inventory_filters_only_indexes_linked_in_user_lobs():
    cursor = MagicMock()
    cursor.fetchall.return_value = [(
        "IX_REAL", "T", "NONUNIQUE", "NORMAL", "VALID", "NO", None,
        "VALUE", 1, "ASC",
    )]

    indexes = _oracle_index_inventory(cursor, "T")

    sql = cursor.execute.call_args.args[0].upper()
    assert "FROM USER_LOBS" in sql and "L.INDEX_NAME = I.INDEX_NAME" in sql
    assert set(indexes) == {"IX_REAL"}
