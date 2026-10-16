"""Validate missing-table detection after an FK-safe target-only mutation.

This helper is intentionally scoped to the dedicated MSSQL E2E target DB. It
drops only foreign keys that reference ``training.Products`` and then that
table, allowing the normal target validator to prove that Products is missing
without disabling constraints globally or touching the source database.
"""
from __future__ import annotations

import argparse

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.mssql.run_validation import _connect
from tests.e2e.mssql.setup.database import is_safe_e2e_database
from tests.e2e.mssql.setup.target import is_protected_database
from tests.e2e.mssql.validation.catalog import MSSQLCatalog
from tests.e2e.mssql.validation.expected import SCHEMA_NAME


def drop_product_target_safely(conn) -> list[str]:
    """Drop target foreign keys referencing Products, then Products itself."""
    dropped_constraints: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fk.name, OBJECT_SCHEMA_NAME(fk.parent_object_id), "
            "OBJECT_NAME(fk.parent_object_id) "
            "FROM sys.foreign_keys fk "
            "WHERE fk.referenced_object_id = OBJECT_ID(?)",
            (f"{SCHEMA_NAME}.Products",),
        )
        for name, schema, table in cur.fetchall():
            cur.execute(
                f"ALTER TABLE [{schema}].[{table}] DROP CONSTRAINT [{name}]"
            )
            dropped_constraints.append(name)
        cur.execute(f"DROP TABLE [{SCHEMA_NAME}].[Products]")
    conn.commit()
    return dropped_constraints


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/mssql_e2e_acceptance.yaml")
    args = parser.parse_args()
    cfg = load_e2e_config(args.config)
    target_db = cfg.target_db
    if is_protected_database(target_db) or not is_safe_e2e_database(
        target_db, cfg.safe_pattern
    ):
        raise SystemExit(f"Refusing mutation of non-E2E target database: {target_db}")

    conn = _connect(cfg, "target", target_db)
    try:
        dropped_constraints = drop_product_target_safely(conn)
        tables = set(MSSQLCatalog(conn).get_base_tables(SCHEMA_NAME))
        print(f"Dropped FK constraints: {dropped_constraints}")
        print(f"Products present after drop: {'Products' in tables}")
        return 0 if "Products" not in tables else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
