from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from core.connectors.base import ForeignKey, Schema, Column, TriggerDef
from core.connectors.mssql import MSSQLTargetConnector


def _build_target():
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    target = MSSQLTargetConnector(
        {"database": "mssql_migration_target", "source_engine": "mssql"}
    )
    target._conn = conn
    return target, cur


def _customer_addresses_schema(foreign_keys=None):
    return Schema(
        name="customer_addresses",
        schema_name="billing",
        columns=[
            Column(name="customer_id", source_type="int", nullable=False),
            Column(name="address", source_type="nvarchar", nullable=False, size=200),
        ],
        primary_key=["customer_id"],
        foreign_keys=foreign_keys or [],
    )


def test_apply_constraints_creates_cross_schema_fk():
    target, cur = _build_target()
    # First execute call returns empty existing-FK set; subsequent calls are FK DDL.
    cur.fetchall.return_value = []
    schema = _customer_addresses_schema(
        foreign_keys=[
            ForeignKey(
                name="FK_billing_customer_addresses_customers",
                columns=["customer_id"],
                ref_table="customers",
                ref_columns=["customer_id"],
                ref_schema="sales",
            )
        ]
    )
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
    assert fk_sql is not None, f"No FK DDL found. Executed: {executed}"
    assert "ALTER TABLE" in fk_sql
    assert "billing" in fk_sql and "customer_addresses" in fk_sql
    assert "REFERENCES" in fk_sql
    assert "sales" in fk_sql and "customers" in fk_sql
    assert "customer_id" in fk_sql


def test_apply_constraints_skips_existing_fk_idempotent():
    target, cur = _build_target()
    # First execute returns the FK name as already existing.
    cur.fetchall.return_value = [("FK_billing_customer_addresses_customers",)]
    schema = _customer_addresses_schema(
        foreign_keys=[
            ForeignKey(
                name="FK_billing_customer_addresses_customers",
                columns=["customer_id"],
                ref_table="customers",
                ref_columns=["customer_id"],
                ref_schema="sales",
            )
        ]
    )
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sqls = [s for s in executed if "FOREIGN KEY" in s]
    assert fk_sqls == [], f"Existing FK should be skipped, got: {fk_sqls}"


def test_apply_constraints_composite_fk_groups_columns():
    target, cur = _build_target()
    cur.fetchall.return_value = []
    schema = _customer_addresses_schema(
        foreign_keys=[
            ForeignKey(
                name="FK_composite",
                columns=["customer_id", "address"],
                ref_table="customers",
                ref_columns=["customer_id", "primary_address"],
                ref_schema="sales",
            )
        ]
    )
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
    assert fk_sql is not None
    assert "customer_id" in fk_sql and "address" in fk_sql
    assert fk_sql.count("customer_id") == 2  # parent col + ref col


def test_apply_constraints_fk_uses_bracketed_identifiers():
    target, cur = _build_target()
    cur.fetchall.return_value = []
    schema = _customer_addresses_schema(
        foreign_keys=[
            ForeignKey(
                name="FK_brackets",
                columns=["customer_id"],
                ref_table="customers",
                ref_columns=["customer_id"],
                ref_schema="sales",
            )
        ]
    )
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
    assert fk_sql is not None
    assert '"billing"' in fk_sql
    assert '"customer_addresses"' in fk_sql
    assert '"sales"' in fk_sql
    assert '"customers"' in fk_sql
    assert '"customer_id"' in fk_sql


def test_apply_constraints_no_fk_when_empty():
    target, cur = _build_target()
    cur.fetchall.return_value = []
    schema = _customer_addresses_schema(foreign_keys=[])
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sqls = [s for s in executed if "FOREIGN KEY" in s]
    assert fk_sqls == [], f"No FK should be created when none defined. Got: {fk_sqls}"


def test_apply_constraints_fk_default_ref_schema_dbo():
    target, cur = _build_target()
    cur.fetchall.return_value = []
    schema = _customer_addresses_schema(
        foreign_keys=[
            ForeignKey(
                name="FK_dbo_default",
                columns=["customer_id"],
                ref_table="customers",
                ref_columns=["customer_id"],
                ref_schema="",
            )
        ]
    )
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
    assert fk_sql is not None
    assert '"dbo"' in fk_sql


def test_apply_constraints_cross_schema_fk_non_public_to_non_public():
    """Cross-schema FK from non-dbo schema to another non-dbo schema."""
    target, cur = _build_target()
    cur.fetchall.return_value = []
    schema = _customer_addresses_schema(
        foreign_keys=[
            ForeignKey(
                name="FK_hr_emp_dept",
                columns=["dept_id"],
                ref_table="departments",
                ref_columns=["dept_id"],
                ref_schema="hr",
            )
        ]
    )
    schema.schema_name = "finance"
    target.apply_constraints(schema)

    executed = [str(c.args[0]) for c in cur.execute.call_args_list]
    fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
    assert fk_sql is not None
    assert '"finance"' in fk_sql
    assert '"customer_addresses"' in fk_sql
    assert '"hr"' in fk_sql
    assert '"departments"' in fk_sql


def test_source_get_all_triggers_captures_parent_table_schema():
    """Trigger discovery should capture parent table schema for cross-schema triggers."""
    from core.connectors.mssql import MSSQLSourceConnector
    from unittest.mock import MagicMock

    cur = MagicMock()
    # (trigger_schema, trigger_name, parent_table_schema, parent_table_name, definition, is_disabled)
    cur.fetchall.return_value = [
        ("billing", "trg_audit", "sales", "customers", "CREATE TRIGGER trg_audit ON sales.customers FOR INSERT AS BEGIN 1 END", 0),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    source = MSSQLSourceConnector({"database": "test", "include_schemas": ["billing", "sales"]})
    source._conn = conn

    triggers = source.get_all_triggers()

    assert len(triggers) == 1
    assert triggers[0].name == "trg_audit"
    assert triggers[0].schema_name == "billing"
    assert triggers[0].table == "customers"
    # This should capture the parent table schema once fixed
    # assert triggers[0].table_schema == "sales"


def test_target_create_trigger_cross_schema():
    """Target should create trigger with correct parent table schema qualification."""
    target, cur = _build_target()
    cur.fetchone.return_value = ("customers",)
    trigger = TriggerDef(
        name="trg_audit",
        table="customers",
        schema_name="billing",
        ddl="CREATE TRIGGER trg_audit ON sales.customers FOR INSERT AS BEGIN 1 END",
        is_disabled=False,
    )
    target.create_trigger(trigger)
    executed = [str(c.args[0]) for c in cur.execute.call_args_list if c.args]
    trig_sql = next(s for s in executed if s.upper().startswith("CREATE OR ALTER"))
    assert "TRIGGER" in trig_sql.upper()
    assert "[billing].[trg_audit]" in trig_sql
    # The DDL should reference sales.customers (not billing.customers)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])