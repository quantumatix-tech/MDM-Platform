from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.connectors.base import Column, Schema
from core.connectors.mssql.objects.table import export_table_data, upsert_table_data


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self.rows = []
        self.sql = ""
        self.parameters = ()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, parameters=()):
        self.sql = sql
        self.parameters = parameters
        self.conn.statements.append((sql, parameters))
        if "t.name = 'sql_variant'" in sql:
            self.rows = []
        elif "c.is_computed = 0" in sql:
            self.rows = [("OrderID",), ("OrderDate",), ("ModifiedAt",)]
        elif "ty.name = 'datetime2'" in sql:
            self.rows = [("OrderDate",), ("ModifiedAt",)]
        elif 'FROM "training"."Orders"' in sql:
            self.description = [("OrderID",), ("OrderDate",), ("ModifiedAt",)]
            if 'CONVERT(VARCHAR(27), "OrderDate", 126)' in sql:
                # SQL Server text conversion retains the datetime2(7) digit.
                self.rows = [(
                    1,
                    "2026-10-12T12:23:51.1843298",
                    "2026-10-12T12:23:52.1843298",
                )]
            else:
                # The pyodbc-native path materializes datetime at microsecond
                # precision, which reproduces the live .…3298 -> .…3290 loss.
                self.rows = [(
                    1,
                    datetime(2026, 10, 12, 12, 23, 51, 184329),
                    datetime(2026, 10, 12, 12, 23, 52, 184329),
                )]
        else:
            self.rows = []
        return self

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self):
        self.statements = []
        self.bound_values = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_datetime2_7_export_and_upsert_preserve_seventh_digit():
    conn = _Connection()
    rows = list(export_table_data(conn, "Orders", schema_name="training"))

    expected_order_date = "2026-10-12T12:23:51.1843298"
    expected_modified_at = "2026-10-12T12:23:52.1843298"
    export_sql = next(
        sql for sql, _ in conn.statements if 'FROM "training"."Orders"' in sql
    )
    assert 'CONVERT(VARCHAR(27), "OrderDate", 126)' in export_sql, conn.statements
    assert 'CONVERT(VARCHAR(27), "ModifiedAt", 126)' in export_sql
    assert rows == [{
        "OrderID": 1,
        "OrderDate": expected_order_date,
        "ModifiedAt": expected_modified_at,
    }]

    target_conn = _Connection()
    schema = Schema(
        name="Orders",
        schema_name="training",
        columns=[
            Column(name="OrderID", source_type="int"),
            Column(name="OrderDate", source_type="datetime2", scale=7),
            Column(name="ModifiedAt", source_type="datetime2", scale=7),
        ],
        primary_key=["OrderID"],
    )
    result = upsert_table_data(target_conn, "Orders", rows, schema)

    merge_sql, bound_parameters = next(
        (sql, params) for sql, params in target_conn.statements
        if sql.startswith("MERGE INTO")
    )
    assert result.success_count == 1
    assert 'OrderDate' in merge_sql and 'ModifiedAt' in merge_sql
    assert merge_sql.count("CONVERT(datetime2(7), ?, 126)") == 2
    assert bound_parameters == [1, expected_order_date, expected_modified_at]


def test_datetime2_export_keeps_null_and_fractional_values():
    conn = _Connection()
    # Replace the simulated row without changing the real export query path.
    original_cursor = conn.cursor

    def cursor_with_null_row():
        cursor = original_cursor()
        execute = cursor.execute

        def execute_with_edge_row(sql, parameters=()):
            result = execute(sql, parameters)
            if 'FROM "training"."Orders"' in sql:
                cursor.rows = [(2, None, "2026-10-12T12:23:52.1")]
            return result

        cursor.execute = execute_with_edge_row
        return cursor

    conn.cursor = cursor_with_null_row
    rows = list(export_table_data(conn, "Orders", schema_name="training"))

    assert rows == [{"OrderID": 2, "OrderDate": None, "ModifiedAt": "2026-10-12T12:23:52.1"}]
