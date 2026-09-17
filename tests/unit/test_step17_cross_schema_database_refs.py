from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from core.connectors.base import ForeignKey, Schema, Column
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])