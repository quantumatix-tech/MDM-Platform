"""Offline contracts for Oracle user-defined TYPE support."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from core.connectors.base import Column, Schema, TypeDef
from core.connectors.oracle.objects import type as oracle_type
from core.connectors.oracle.precision import oracle_full_unsupported_datatypes
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import OracleTargetConnector


class _Cursor:
    def __init__(self, results, statements=None):
        self.results = results
        self.sql = ""
        self.statements = statements if statements is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, *_args):
        self.sql = sql
        self.statements.append(sql)

    def fetchall(self):
        for fragment, rows in self.results.items():
            if fragment in self.sql:
                return list(rows)
        return []

    def fetchone(self):
        for fragment, row in self.results.items():
            if fragment in self.sql:
                return row
        return None


class _Connection:
    def __init__(self, results):
        self.results = results
        self.statements = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self.results, self.statements)

    def commit(self):
        self.commits += 1


def test_discovery_builds_dynamic_object_type_ddl_and_uses_dependencies(monkeypatch):
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [("ADDRESS_T", "VALID", "OBJECT", "NO", "NO")],
        "object_type = 'TYPE BODY'": [],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [],
        "DBMS_METADATA.GET_DDL": ("CREATE TYPE ADDRESS_T AS OBJECT (CITY VARCHAR2(40))",),
    })
    monkeypatch.setattr(
        "core.connectors.oracle.objects.type._get_ddl",
        lambda _conn, _kind, name, _owner: f"CREATE TYPE {name} AS OBJECT (CITY VARCHAR2(40))",
    )

    found = oracle_type.discover_types(conn, "SOURCE_OWNER")

    assert found == [TypeDef(
        name="ADDRESS_T", kind="object",
        ddl="CREATE TYPE ADDRESS_T AS OBJECT (CITY VARCHAR2(40))",
        schema="SOURCE_OWNER",
    )]


def test_type_body_catalog_uses_object_name_while_type_views_use_type_name():
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [],
        "FROM user_types WHERE supertype_name": [],
        "FROM user_coll_types": [],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [],
        "FROM user_type_attrs ORDER BY": [],
    })

    assert oracle_type.discover_types(conn, "SOURCE_OWNER") == []

    body_query = next(
        statement for statement in conn.statements
        if "object_type = 'TYPE BODY'" in statement
    )
    assert "SELECT object_name FROM user_objects" in body_query
    assert "type_name" not in body_query.casefold()
    assert any(
        "SELECT type_name FROM user_types" in statement
        for statement in conn.statements
    )


@pytest.mark.parametrize(
    "row,body,expected",
    [
        (("ADDR_VARRAY", "VALID", "COLLECTION", "NO", "NO"), [], "collection metadata unavailable"),
        (("ADDRESS_T", "VALID", "OBJECT", "NO", "NO"), [("ADDRESS_T",)], "OBJECT ADDRESS_T"),
        (("BROKEN_T", "INVALID", "OBJECT", "NO", "NO"), [], "invalid TYPE BROKEN_T"),
    ],
)
def test_discovery_reports_unsupported_type_categories(row, body, expected):
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [row],
        "object_type = 'TYPE BODY'": body,
    })
    with pytest.raises(RuntimeError, match=expected):
        oracle_type.discover_types(conn, "SOURCE_OWNER")


def test_discovery_fails_closed_on_type_dependency_cycle():
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [
            ("FIRST_T", "VALID", "OBJECT", "NO", "NO"),
            ("SECOND_T", "VALID", "OBJECT", "NO", "NO"),
        ],
        "object_type = 'TYPE BODY'": [],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [
            ("FIRST_T", "TYPE", "SECOND_T", "TYPE"),
            ("SECOND_T", "TYPE", "FIRST_T", "TYPE"),
        ],
    })
    with pytest.raises(RuntimeError, match="dependency cycle"):
        oracle_type.discover_types(conn, "SOURCE_OWNER")


def test_discovery_orders_object_and_collection_dependencies(monkeypatch):
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [
            ("OUTER_T", "VALID", "OBJECT", "NO", "NO"),
            ("TEXTS_VA", "VALID", "COLLECTION", "NO", "NO"),
            ("INNER_T", "VALID", "OBJECT", "NO", "NO"),
        ],
        "object_type = 'TYPE BODY'": [],
        "FROM user_types WHERE supertype_name": [],
        "FROM user_coll_types": [
            ("TEXTS_VA", "VARYING ARRAY", "SOURCE_OWNER", "INNER_T", None),
        ],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [],
        "FROM user_type_attrs ORDER BY": [
            ("OUTER_T", "SOURCE_OWNER", "INNER_T", None),
            ("INNER_T", "SYS", "NUMBER", None),
        ],
    })
    monkeypatch.setattr(
        "core.connectors.oracle.objects.type._get_ddl",
        lambda _conn, _kind, name, _owner: f"CREATE TYPE {name} AS OBJECT (VALUE NUMBER)",
    )
    found = oracle_type.discover_types(conn, "SOURCE_OWNER")
    assert [(item.name, item.kind) for item in found] == [
        ("INNER_T", "object"), ("OUTER_T", "object"), ("TEXTS_VA", "varray"),
    ]


def test_discovery_rejects_inheritance_external_attributes_and_lob_attributes():
    base = {
        "FROM user_objects o LEFT JOIN user_types": [
            ("OUTER_T", "VALID", "OBJECT", "NO", "NO"),
        ],
        "object_type = 'TYPE BODY'": [],
        "FROM user_types WHERE supertype_name": [],
        "FROM user_coll_types": [],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [],
    }
    for supertype, attrs, expected in [
        ([("OUTER_T",)], [], "inherited OBJECT TYPE"),
        ([], [("OUTER_T", "OTHER_OWNER", "DETAIL_T", None)], "OTHER_OWNER.DETAIL_T"),
        ([], [("OUTER_T", "SYS", "BLOB", None)], "SYS.BLOB"),
    ]:
        conn = _Connection({
            **base,
            "FROM user_types WHERE supertype_name": supertype,
            "FROM user_type_attrs ORDER BY": attrs,
        })
        with pytest.raises(RuntimeError, match=expected):
            oracle_type.discover_types(conn, "SOURCE_OWNER")


@pytest.mark.parametrize(
    "catalog_type,expected_canonical",
    [
        ("TIMESTAMP WITH TZ", "TIMESTAMP WITH TIME ZONE"),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE"),
        ("TIMESTAMP WITH LOCAL TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"),
    ],
)
def test_type_timezone_attribute_is_normalized_then_rejected_before_ddl(
    catalog_type, expected_canonical,
):
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [
            ("VALUE_T", "VALID", "OBJECT", "NO", "NO"),
        ],
        "object_type = 'TYPE BODY'": [],
        "FROM user_types WHERE supertype_name": [],
        "FROM user_types WHERE methods": [],
        "FROM user_coll_types": [],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [],
        "FROM user_type_attrs ORDER BY": [
            ("VALUE_T", "SYS", catalog_type, None),
        ],
    })

    with pytest.raises(RuntimeError, match="naive datetime without the source time-zone displacement") as exc:
        oracle_type.discover_types(conn, "SOURCE_OWNER")

    assert expected_canonical in str(exc.value)
    assert "lossless OBJECT value transport is unsupported" in str(exc.value)
    assert not any(statement.startswith("CREATE TYPE") for statement in conn.statements)


def test_python_datetime_representation_truncates_ninth_fractional_digit():
    # Experimentally demonstrate why the current DbObject -> Python datetime
    # route cannot represent Oracle FF9 exactly, even when a textual value is
    # available before conversion.
    source_text = "2024-11-03T01:30:00.123456789-07:00"
    converted = datetime.fromisoformat(source_text)

    assert converted.microsecond == 123456
    assert converted.isoformat(timespec="microseconds") == (
        "2024-11-03T01:30:00.123456-07:00"
    )
    driver_default_shape = converted.replace(tzinfo=None)
    assert driver_default_shape.tzinfo is None


def test_discovery_rejects_object_type_method_declarations(monkeypatch):
    conn = _Connection({
        "FROM user_objects o LEFT JOIN user_types": [("METHOD_T", "VALID", "OBJECT", "NO", "NO")],
        "object_type = 'TYPE BODY'": [],
        "FROM user_dependencies WHERE UPPER(referenced_owner)": [],
    })
    monkeypatch.setattr(
        "core.connectors.oracle.objects.type._get_ddl",
        lambda *_args: "CREATE TYPE METHOD_T AS OBJECT (VALUE NUMBER, MEMBER FUNCTION LABEL RETURN VARCHAR2)",
    )
    with pytest.raises(RuntimeError, match="declares methods"):
        oracle_type.discover_types(conn, "SOURCE_OWNER")


def test_create_type_remaps_source_owner_and_is_idempotent_for_matching_target():
    conn = _Connection({
        "SELECT object_type FROM user_objects": [],
        "SELECT 1 FROM user_objects": None,
        "FROM user_dependencies WHERE referenced_name": [],
    })
    oracle_type.create_type(
        conn,
        TypeDef(
            name="ADDRESS_T", kind="object",
            ddl='CREATE TYPE "SOURCE_OWNER"."ADDRESS_T" AS OBJECT (CITY VARCHAR2(40))',
            schema="SOURCE_OWNER",
        ),
        target_owner="TARGET_OWNER",
    )
    assert conn.commits == 1


def test_create_collection_type_preserves_remapped_element_owner():
    ddl = 'CREATE TYPE "SOURCE_OWNER"."TEXTS_VA" AS VARRAY(20) OF "SOURCE_OWNER"."TEXT_T"'
    remapped = oracle_type._remap_owner_qualifiers(ddl, "SOURCE_OWNER", "TARGET_OWNER")
    assert '"TARGET_OWNER"."TEXTS_VA"' in remapped
    assert '"TARGET_OWNER"."TEXT_T"' in remapped
    conn = _Connection({
        "SELECT object_type FROM user_objects": [],
        "SELECT 1 FROM user_objects": None,
        "FROM user_dependencies WHERE referenced_name": [],
    })
    oracle_type.create_type(
        conn,
        TypeDef(
            name="TEXTS_VA", kind="varray",
            ddl=ddl,
            schema="SOURCE_OWNER",
        ),
        target_owner="TARGET_OWNER",
    )
    assert conn.commits == 1


def test_type_ddl_owner_mapping_preserves_dynamic_type_reference():
    ddl = 'CREATE TYPE "SOURCE_OWNER"."ADDRESS_T" AS OBJECT (CITY VARCHAR2(40))'
    remapped = oracle_type._remap_owner_qualifiers(ddl, "SOURCE_OWNER", "TARGET_OWNER")
    assert '"TARGET_OWNER"."ADDRESS_T"' in remapped
    assert "ADDRESS_T" in remapped


def test_oracle_type_api_delegates_to_object_module(monkeypatch):
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    target = OracleTargetConnector({"username": "TARGET_OWNER"})
    source._conn = object()
    target._conn = object()
    expected = [TypeDef("POINT_T", "object", "CREATE TYPE POINT_T AS OBJECT (X NUMBER)", "SOURCE_OWNER")]
    discovered = MagicMock(return_value=expected)
    created = MagicMock()
    monkeypatch.setattr("core.connectors.oracle.objects.discover_types", discovered)
    monkeypatch.setattr("core.connectors.oracle.objects.create_type", created)

    assert source.list_types() == expected
    target.create_type(expected[0])
    discovered.assert_called_once_with(source._conn, "SOURCE_OWNER")
    created.assert_called_once_with(target._conn, expected[0], target_owner="TARGET_OWNER")


def test_object_typed_table_columns_use_dynamic_type_preflight():
    schema = Schema(
        "DYNAMIC_TABLE", columns=[Column("SHAPE", "POINT_T")],
        options={"oracle_object_types": {"SHAPE": {"owner": "SOURCE_OWNER", "name": "POINT_T"}}},
    )
    assert oracle_full_unsupported_datatypes([schema]) == []


def test_source_schema_resolves_object_and_collection_column_metadata():
    conn = _Connection({
        "FROM user_tab_cols WHERE": [
            ("VALUES", "TEXTS_VA", 20, None, None, None, None, "Y", None, "NO", "NO"),
        ],
        "FROM user_tab_columns c LEFT JOIN user_types": [
            ("VALUES", "SOURCE_OWNER", "TEXTS_VA", "COLLECTION"),
        ],
        "FROM user_constraints c JOIN user_cons_columns": [],
        "FROM user_indexes i": [],
        "FROM user_part_tables": None,
    })
    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    source._conn = conn

    schema = source.get_schema("T")

    assert schema.columns[0].source_type == "TEXTS_VA"
    assert schema.columns[0].target_type == "TEXTS_VA"
    assert schema.options["oracle_object_types"]["VALUES"] == {
        "owner": "SOURCE_OWNER", "name": "TEXTS_VA", "typecode": "COLLECTION",
    }


def test_object_value_serialization_preserves_null_attributes_and_decimal():
    class Attr:
        def __init__(self, name):
            self.name = name
            self.type = object()

    class Value:
        CITY = "Paris"
        AMOUNT = Decimal("12345678901234567890.123456789")
        NOTE = None

    class ObjectType:
        name = "ADDRESS_T"
        iscollection = False
        attributes = (Attr("CITY"), Attr("AMOUNT"), Attr("NOTE"))

        def newobject(self):
            return type("ObjectValue", (), {})()

    db_type = ObjectType()
    payload = oracle_type.serialize_object(Value(), db_type)
    assert payload == {
        "__oracle_object__": "ADDRESS_T",
        "attributes": {
            "CITY": "Paris",
            "AMOUNT": Decimal("12345678901234567890.123456789"),
            "NOTE": None,
        },
    }
    hydrated = oracle_type.hydrate_object(payload, db_type)
    assert hydrated.CITY == "Paris"
    assert hydrated.AMOUNT == Decimal("12345678901234567890.123456789")
    assert hydrated.NOTE is None
    assert oracle_type.serialize_object(None, db_type) is None
    assert oracle_type.hydrate_object(None, db_type) is None


def test_target_batch_binds_populated_and_null_object_values():
    class Attr:
        def __init__(self, name):
            self.name = name
            self.type = object()

    class ObjectType:
        name = "ADDRESS_T"
        iscollection = False
        attributes = (Attr("CITY"), Attr("AMOUNT"))

        def newobject(self):
            return type("ObjectValue", (), {})()

    type_object = ObjectType()
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    connection.gettype.return_value = type_object
    target = OracleTargetConnector({})
    target._conn = connection
    schema = Schema(
        "CUSTOMERS", columns=[Column("ID", "NUMBER"), Column("ADDRESS", "ADDRESS_T")],
        options={"oracle_object_types": {
            "ADDRESS": {"owner": "TARGET_OWNER", "name": "ADDRESS_T"},
        }},
    )
    populated = {
        "__oracle_object__": "ADDRESS_T",
        "attributes": {"CITY": "Oslo", "AMOUNT": Decimal("12345678901234567890.123")},
    }
    result = target.upsert_batch("CUSTOMERS", iter([
        {"ID": 1, "ADDRESS": populated},
        {"ID": 2, "ADDRESS": None},
    ]), schema)

    assert result.success_count == 2
    connection.gettype.assert_called_once_with("TARGET_OWNER.ADDRESS_T")
    cursor.setinputsizes.assert_called_once_with(None, type_object)
    values = cursor.executemany.call_args.args[1]
    assert values[0][1].CITY == "Oslo"
    assert values[0][1].AMOUNT == Decimal("12345678901234567890.123")
    assert values[1][1] is None


def test_target_batch_binds_collection_values_and_null_collection():
    class CollectionType:
        name = "TEXTS_VA"
        iscollection = True
        element_type = object()

        def newobject(self, items=None):
            return type("CollectionValue", (), {"items": list(items or [])})()

    type_object = CollectionType()
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    connection.gettype.return_value = type_object
    target = OracleTargetConnector({})
    target._conn = connection
    target._oracle_collection_kinds = {"texts_va": "varray"}
    schema = Schema(
        "T", columns=[Column("ID", "NUMBER"), Column("VALUES", "TEXTS_VA")],
        options={"oracle_object_types": {
            "VALUES": {"owner": "TARGET_OWNER", "name": "TEXTS_VA", "typecode": "COLLECTION"},
        }},
    )
    value = {
        "__oracle_collection__": "TEXTS_VA", "kind": "varray",
        "items": ["first", None, "last"],
    }
    result = target.upsert_batch("T", iter([
        {"ID": 1, "VALUES": value}, {"ID": 2, "VALUES": None},
    ]), schema)
    assert result.success_count == 2
    inserted = cursor.executemany.call_args.args[1]
    assert inserted[0][1].items == ["first", None, "last"]
    assert inserted[1][1] is None


def test_object_transport_recursively_preserves_nested_object_values():
    class Attr:
        def __init__(self, name):
            self.name = name
            self.type = nested_type

    nested_type = type(
        "NestedType", (),
        {"name": "INNER_T", "iscollection": False, "attributes": [type("Field", (), {
            "name": "TEXT", "type": object(),
        })()]},
    )()
    nested_value = type("Nested", (), {"TEXT": "nested"})()
    value = type("Value", (), {"NESTED": nested_value})()
    object_type = type(
        "ObjectType", (),
        {"name": "OUTER_T", "iscollection": False, "attributes": [Attr("NESTED")]},
    )()
    object_type.newobject = lambda: type("Hydrated", (), {})()
    nested_type.newobject = lambda: type("HydratedNested", (), {})()
    payload = oracle_type.serialize_object(value, object_type)
    assert payload["attributes"]["NESTED"]["__oracle_object__"] == "INNER_T"
    assert oracle_type.hydrate_object(payload, object_type).NESTED.TEXT == "nested"


def test_collection_transport_preserves_varray_order_and_null_vs_empty():
    class CollectionType:
        name = "TEXTS_VA"
        iscollection = True
        element_type = object()

        def newobject(self, items=None):
            return type("CollectionValue", (), {"aslist": lambda self: list(items or [])})()

    collection_type = CollectionType()
    value = collection_type.newobject(["first", None, "third"])
    payload = oracle_type.serialize_object(
        value, collection_type, {"TEXTS_VA": "varray"}
    )
    assert payload["items"] == ["first", None, "third"]
    assert oracle_type.hydrate_object(
        payload, collection_type, {"TEXTS_VA": "varray"}
    ).aslist() == ["first", None, "third"]
    assert oracle_type.serialize_object(None, collection_type, {"TEXTS_VA": "varray"}) is None
    assert oracle_type.serialize_object(
        collection_type.newobject([]), collection_type, {"TEXTS_VA": "varray"}
    )["items"] == []


def test_source_batch_extracts_collection_values_through_driver_type():
    class CollectionType:
        name = "TEXTS_VA"
        iscollection = True
        element_type = object()

    class CollectionValue:
        def aslist(self):
            return ["first", "second"]

    source = OracleSourceConnector({"username": "SOURCE_OWNER"})
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("COLLECTION",)
    cursor.description = [("VALUES",)]
    cursor.fetchmany.side_effect = [[(CollectionValue(),)], []]
    connection.gettype.return_value = CollectionType()
    source._conn = connection
    source._oracle_collection_kinds = {"texts_va": "varray"}
    source._schema_cache["T"] = Schema(
        "T", columns=[Column("VALUES", "TEXTS_VA")],
        options={"oracle_object_types": {
            "VALUES": {"owner": "SOURCE_OWNER", "name": "TEXTS_VA"},
        }},
    )

    row = next(source.export_full("T"))
    assert row["VALUES"] == {
        "__oracle_collection__": "TEXTS_VA", "kind": "varray",
        "items": ["first", "second"],
    }


def test_nested_table_validation_ignores_element_order_but_varray_does_not():
    from core.connectors.oracle.precision import compare_oracle_rows

    schema = Schema(
        "T", columns=[Column("VALUES", "NUMBERS_T")],
        options={"oracle_object_types": {"VALUES": {"owner": "OWNER", "name": "NUMBERS_T"}}},
    )
    left = {"__oracle_collection__": "NUMBERS_T", "kind": "nested_table", "items": [1, 2, 2]}
    right = {"__oracle_collection__": "NUMBERS_T", "kind": "nested_table", "items": [2, 1, 2]}
    assert compare_oracle_rows([{"VALUES": left}], [{"VALUES": right}], schema)["match"]
    left["kind"] = right["kind"] = "varray"
    assert not compare_oracle_rows([{"VALUES": left}], [{"VALUES": right}], schema)["match"]


def test_object_transport_rejects_incompatible_target_definition():
    source = Schema(
        "DYNAMIC_TABLE", columns=[Column("SHAPE", "POINT_T")],
        options={"oracle_object_types": {"SHAPE": {"owner": "SOURCE_OWNER", "name": "POINT_T"}}},
    )
    target = Schema(
        "DYNAMIC_TABLE", columns=[Column("SHAPE", "OTHER_T")],
        options={"oracle_object_types": {"SHAPE": {"owner": "TARGET_OWNER", "name": "OTHER_T"}}},
    )
    from core.connectors.oracle.target import _schemas_compatible
    assert not _schemas_compatible(source, target, "oracle")




@pytest.mark.parametrize("target_ddl,blocked", [
    ('CREATE TYPE ADDRESS_T AS OBJECT (CITY VARCHAR2(40))', False),
    ('CREATE TYPE ADDRESS_T AS OBJECT (CITY VARCHAR2(80))', True),
])
def test_replacement_plan_compares_existing_type_definition_before_cleanup(
    monkeypatch, target_ddl, blocked,
):
    from core.connectors.oracle import replacement

    monkeypatch.setattr(
        replacement, "_target_inventory",
        lambda *_args: (
            [{"type": "TYPE", "name": "ADDRESS_T"}], [], {}, [], [], [], [], {}
        ),
    )
    monkeypatch.setattr(
        "core.connectors.oracle.objects._get_ddl",
        lambda *_args: target_ddl,
    )
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    source_type = TypeDef(
        "ADDRESS_T", "object",
        "CREATE TYPE ADDRESS_T AS OBJECT (CITY VARCHAR2(40))",
        "SOURCE_OWNER",
    )

    plan = replacement.build_plan(
        conn, "TARGET_OWNER", tables=set(), schemas=[], views=[], sequences=[],
        routines=[], triggers=[], types=[source_type],
    )

    mismatch = any("definition differs from source" in item for item in plan.blockers)
    assert mismatch is blocked
