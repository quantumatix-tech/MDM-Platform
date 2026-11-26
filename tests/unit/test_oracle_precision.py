"""Regression tests for lossless Oracle NUMBER and temporal FULL transport."""
from __future__ import annotations

import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.connectors.base import Column, Schema
from core.connectors.oracle.precision import (
    compare_precision_rows,
    decimal_output_type_handler,
    transport_bind_expression,
    transport_select_expression,
)
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import OracleTargetConnector


def test_number_fetch_handler_preserves_38_digit_values_as_decimal():
    oracle = SimpleNamespace(DB_TYPE_NUMBER=object())
    cursor = MagicMock()
    cursor.arraysize = 200
    variable = object()
    cursor.var.return_value = variable

    handler = decimal_output_type_handler(oracle)
    assert handler(cursor, SimpleNamespace(type_code=oracle.DB_TYPE_NUMBER)) is variable
    assert cursor.var.call_args.args[0] is Decimal
    assert cursor.var.call_args.kwargs["arraysize"] == 200
    assert handler(cursor, SimpleNamespace(type_code=object())) is None


def test_temporal_transport_keeps_fractional_digits_offsets_and_utc_ltz():
    date_col = Column("D", "DATE")
    timestamp_col = Column("TS", "TIMESTAMP", precision=9)
    tz_col = Column("TS_TZ", "TIMESTAMP WITH TIME ZONE", precision=9)
    ltz_col = Column("TS_LTZ", "TIMESTAMP WITH LOCAL TIME ZONE", precision=9)

    assert "TO_CHAR(\"D\", 'YYYY-MM-DD\"T\"HH24:MI:SS'" in transport_select_expression(date_col)
    assert "FF9" in transport_select_expression(timestamp_col)
    assert "TZH:TZM" in transport_select_expression(tz_col)
    assert "SYS_EXTRACT_UTC" in transport_select_expression(ltz_col)
    assert "TO_TIMESTAMP(:1" in transport_bind_expression(timestamp_col, ":1")
    assert "TO_TIMESTAMP_TZ(:1" in transport_bind_expression(tz_col, ":1")
    assert "'+00:00'" in transport_bind_expression(ltz_col, ":1")


def test_oracle_source_selects_temporal_text_and_keeps_decimal_number_values(monkeypatch):
    oracle = SimpleNamespace(DB_TYPE_NUMBER=object())
    monkeypatch.setitem(sys.modules, "oracledb", oracle)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("AMOUNT",), ("TS",)]
    cursor.fetchmany.side_effect = [[(Decimal("12345678901234.123456"), "2024-02-29T23:59:59.123456789")], []]
    source = OracleSourceConnector({"username": "OWNER"})
    source._conn = connection
    source._schema_cache["T"] = Schema("T", columns=[
        Column("AMOUNT", "NUMBER", precision=20, scale=6),
        Column("TS", "TIMESTAMP", precision=9),
    ])
    source.enable_precision_safe_full_load()

    rows = list(source.export_full("T"))

    assert rows == [{
        "AMOUNT": Decimal("12345678901234.123456"),
        "TS": "2024-02-29T23:59:59.123456789",
    }]
    sql = cursor.execute.call_args.args[0]
    assert "TO_CHAR(\"TS\"" in sql and "FF9" in sql
    assert "SELECT \"AMOUNT\"" in sql
    assert cursor.outputtypehandler is not None


def test_oracle_target_binds_number_and_temporal_text_without_float_or_datetime(monkeypatch):
    oracle = SimpleNamespace(DB_TYPE_NUMBER=object(), DB_TYPE_VARCHAR=object())
    monkeypatch.setitem(sys.modules, "oracledb", oracle)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema("T", columns=[
        Column("AMOUNT", "NUMBER", precision=20, scale=6),
        Column("TS", "TIMESTAMP", precision=9),
        Column("TS_TZ", "TIMESTAMP WITH TIME ZONE", precision=9),
        Column("TS_LTZ", "TIMESTAMP WITH LOCAL TIME ZONE", precision=9),
    ], options={"oracle_precision_safe_transport": True})
    target.enable_precision_safe_full_load()

    result = target.upsert_batch("T", iter([{
        "AMOUNT": Decimal("12345678901234.123456"),
        "TS": "2024-02-29T23:59:59.123456789",
        "TS_TZ": "2024-02-29T23:59:59.123456789 -08:00",
        "TS_LTZ": "2024-03-01T05:29:59.123456789",
    }, {
        "AMOUNT": None, "TS": None, "TS_TZ": None, "TS_LTZ": None,
    }]), schema)

    assert result.success_count == 2
    statement = cursor.executemany.call_args.args[0]
    binds = cursor.executemany.call_args.args[1]
    assert "TO_TIMESTAMP(:2" in statement and "FF9" in statement
    assert "TO_TIMESTAMP_TZ(:3" in statement
    assert "FROM_TZ(TO_TIMESTAMP(:4" in statement
    assert binds[0][0] == Decimal("12345678901234.123456")
    assert binds[0][2].endswith("-08:00")
    assert binds[1] == (None, None, None, None)
    assert cursor.setinputsizes.called


def test_precision_comparison_covers_numeric_boundaries_and_temporal_mismatch_details():
    schema = Schema("T", columns=[
        Column("ID", "NUMBER", precision=38, scale=0),
        Column("AMOUNT", "NUMBER", precision=20, scale=6),
        Column("D", "DATE"),
        Column("TS6", "TIMESTAMP", precision=6),
        Column("TS9", "TIMESTAMP", precision=9),
        Column("TS_TZ", "TIMESTAMP WITH TIME ZONE", precision=9),
        Column("TS_LTZ", "TIMESTAMP WITH LOCAL TIME ZONE", precision=9),
    ], primary_key=["ID"])
    source = {
        "ID": Decimal(99999999999999999999999999999999999999),
        "AMOUNT": Decimal("-12345678901234.123456"),
        "D": "2024-02-29T23:59:59",
        "TS6": "2024-02-29T23:59:59.123456000",
        "TS9": "2024-02-29T23:59:59.123456789",
        "TS_TZ": "2024-02-29T23:59:59.123456789 +05:30",
        "TS_LTZ": "2024-02-29T18:29:59.123456789",
    }
    assert compare_precision_rows([source], [dict(source)], schema)["match"]
    target = dict(source, TS9="2024-02-29T23:59:59.123456000")
    mismatch = compare_precision_rows([source], [target], schema)
    assert mismatch["match"] is False
    assert mismatch["mismatches"] == [{
        "row": {"ID": str(source["ID"])},
        "column": "TS9",
        "source": "2024-02-29T23:59:59.123456789",
        "target": "2024-02-29T23:59:59.123456000",
    }]


def test_precision_comparison_preserves_null_zero_negative_and_mixed_tz_offsets():
    schema = Schema("T", columns=[
        Column("ID", "NUMBER", precision=3, scale=0),
        Column("N", "NUMBER", precision=20, scale=6),
        Column("TS_TZ", "TIMESTAMP WITH TIME ZONE", precision=9),
    ], primary_key=["ID"])
    rows = [
        {"ID": Decimal(1), "N": Decimal(0), "TS_TZ": "2024-02-29T23:59:59.123456789 +05:30"},
        {"ID": Decimal(2), "N": Decimal("-0.000001"), "TS_TZ": "2024-02-29T10:59:59.123456789 -08:00"},
        {"ID": Decimal(3), "N": None, "TS_TZ": None},
    ]
    assert compare_precision_rows(rows, list(reversed(rows)), schema)["match"]
