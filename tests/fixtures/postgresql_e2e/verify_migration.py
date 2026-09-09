"""
PostgreSQL E2E Audit Fixture — Verification Script

Usage:
    python verify_migration.py                     # verify target against fixture expectations
    python verify_migration.py --reset-source      # reset source database only
    python verify_migration.py --reset-source --run-migration   # reset + run migration + verify
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Defaults match config/postgresql_object_e2e.yaml
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55432
DEFAULT_USER = "postgres"
DEFAULT_SOURCE_DB = "migration_source"
DEFAULT_TARGET_DB = "migration_target"


def run_psql(sql: str, db: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
             user: str = DEFAULT_USER) -> str:
    env = os.environ.copy()
    env["PGPASSWORD"] = "postgres"
    result = subprocess.run(
        ["psql", "-h", host, "-p", str(port), "-U", user, "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout.strip()


def reset_source() -> None:
    base = os.path.dirname(os.path.abspath(__file__))
    ps1 = os.path.join(base, "reset_source.ps1")
    if os.name == "nt" and os.path.exists(ps1):
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1], check=True)
    else:
        bash = os.path.join(base, "reset_source.sh")
        subprocess.run(["bash", bash], check=True)


def run_migration() -> None:
    config = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "config", "postgresql_object_e2e.yaml")
    )
    cmd = [sys.executable, "-m", "migration_platform", "--config", config, "--mode", "full", "--no-live-ui"]
    subprocess.run(cmd, check=True)


def verify() -> bool:
    ok = True

    # 1. Schemas
    for schema in ("public", "audit_test"):
        count = run_psql(
            f"SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = '{schema}';",
            DEFAULT_TARGET_DB,
        )
        print(f"Schema {schema}: {'OK' if count == '1' else 'MISSING'} ({count})")
        if count != "1":
            ok = False

    # 2. Tables + row counts
    expected_tables = {
        ("public", "customers"): 5,
        ("public", "products"): 10,
        ("public", "orders"): 12,
        ("audit_test", "test_customers"): 3,
        ("audit_test", "test_orders"): 2,
        ("audit_test", "fk_parent"): 2,
        ("audit_test", "fk_child"): 3,
        ("audit_test", "procedure_test_log"): 0,
    }
    for (schema, table), expected_rows in expected_tables.items():
        count = run_psql(
            f"SELECT COUNT(*) FROM {schema}.{table};", DEFAULT_TARGET_DB
        )
        status = "OK" if count == str(expected_rows) else f"MISMATCH (expected {expected_rows})"
        print(f"Table {schema}.{table}: {status} ({count})")
        if count != str(expected_rows):
            ok = False

    # 3. Views
    expected_views = [
        ("public", "customer_order_summary"),
        ("audit_test", "order_summary"),
    ]
    for schema, view in expected_views:
        count = run_psql(
            f"SELECT COUNT(*) FROM information_schema.views WHERE table_schema = '{schema}' AND table_name = '{view}';",
            DEFAULT_TARGET_DB,
        )
        print(f"View {schema}.{view}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 4. Materialized views
    expected_mvs = [
        ("public", "customer_balance_summary"),
        ("audit_test", "audit_test_customer_summary"),
    ]
    for schema, mv in expected_mvs:
        count = run_psql(
            f"SELECT COUNT(*) FROM pg_matviews WHERE schemaname = '{schema}' AND matviewname = '{mv}';",
            DEFAULT_TARGET_DB,
        )
        print(f"Materialized view {schema}.{mv}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 5. Functions + Procedures
    funcs = [
        ("public", "get_customer_count", "f"),
        ("public", "update_customer_timestamp", "f"),
        ("public", "test_procedure", "p"),
        ("audit_test", "get_customer_count", "f"),
        ("audit_test", "update_customer_timestamp", "f"),
        ("audit_test", "test_procedure", "p"),
    ]
    for schema, name, kind in funcs:
        count = run_psql(
            f"SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid "
            f"WHERE n.nspname = '{schema}' AND p.proname = '{name}' AND p.prokind = '{kind}';",
            DEFAULT_TARGET_DB,
        )
        label = "Procedure" if kind == "p" else "Function"
        print(f"{label} {schema}.{name}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 6. Triggers
    triggers = [
        ("public", "customers", "trg_customer_timestamp"),
        ("audit_test", "test_customers", "trg_customer_timestamp"),
    ]
    for schema, table, trigger in triggers:
        count = run_psql(
            f"SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid "
            f"JOIN pg_namespace n ON c.relnamespace = n.oid "
            f"WHERE n.nspname = '{schema}' AND c.relname = '{table}' AND t.tgname = '{trigger}' "
            f"AND NOT t.tgisinternal;",
            DEFAULT_TARGET_DB,
        )
        print(f"Trigger {schema}.{table}.{trigger}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 7. Sequences
    sequences = [
        ("public", "customers_customer_id_seq"),
        ("public", "products_product_id_seq"),
        ("public", "orders_order_id_seq"),
        ("audit_test", "test_orders_order_id_seq"),
    ]
    for schema, seq in sequences:
        count = run_psql(
            f"SELECT COUNT(*) FROM pg_sequences WHERE schemaname = '{schema}' AND sequencename = '{seq}';",
            DEFAULT_TARGET_DB,
        )
        print(f"Sequence {schema}.{seq}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 8. Indexes
    indexes = [
        ("public", "customers", "idx_customers_email"),
        ("public", "customers", "idx_customers_city"),
        ("public", "orders", "idx_orders_status"),
        ("public", "orders", "idx_orders_date"),
        ("public", "products", "idx_products_name"),
        ("audit_test", "test_customers", "idx_test_customers_email"),
        ("audit_test", "test_orders", "idx_test_orders_date"),
        ("audit_test", "fk_child", "idx_fk_child_public_customer"),
    ]
    for schema, table, idx in indexes:
        count = run_psql(
            f"SELECT COUNT(*) FROM pg_indexes WHERE schemaname = '{schema}' AND tablename = '{table}' AND indexname = '{idx}';",
            DEFAULT_TARGET_DB,
        )
        print(f"Index {schema}.{table}.{idx}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 9. Comments (spot checks)
    comments = [
        ("SCHEMA", "public", "Standard public schema for migration E2E audit"),
        ("TABLE", "public.customers", "Customer master data for E2E audit"),
        ("COLUMN", "public.customers.email", "Unique customer email address"),
        ("FUNCTION", "public.get_customer_count()", "Returns total customer count in public schema"),
    ]
    for obj_type, obj_name, expected_text in comments:
        escaped = expected_text.replace("'", "''")
        if obj_type == "SCHEMA":
            sql = (
                f"SELECT d.description FROM pg_description d "
                f"JOIN pg_namespace n ON d.objoid = n.oid AND d.classoid = 'pg_namespace'::regclass "
                f"WHERE n.nspname = 'public' AND d.description = '{escaped}';"
            )
        elif obj_type == "COLUMN":
            schema, table, col = obj_name.split(".")
            sql = (
                f"SELECT d.description FROM pg_description d "
                f"JOIN pg_attribute a ON d.objoid = a.attrelid AND d.objsubid = a.attnum "
                f"JOIN pg_class c ON a.attrelid = c.oid "
                f"JOIN pg_namespace n ON c.relnamespace = n.oid "
                f"WHERE n.nspname = '{schema}' AND c.relname = '{table}' "
                f"AND a.attname = '{col}' AND d.description = '{escaped}';"
            )
        elif obj_type == "FUNCTION":
            schema, func_name = obj_name.split(".")
            func_name = func_name.rstrip("()")
            sql = (
                f"SELECT d.description FROM pg_description d "
                f"JOIN pg_proc p ON d.objoid = p.oid "
                f"JOIN pg_namespace n ON p.pronamespace = n.oid "
                f"WHERE n.nspname = '{schema}' AND p.proname = '{func_name}' "
                f"AND d.description = '{escaped}';"
            )
        else:
            sql = (
                f"SELECT d.description FROM pg_description d "
                f"JOIN pg_class c ON d.objoid = c.oid AND d.objsubid = 0 "
                f"JOIN pg_namespace n ON c.relnamespace = n.oid "
                f"WHERE n.nspname = 'public' AND c.relname = '{obj_name.split('.')[-1]}' "
                f"AND d.description = '{escaped}';"
            )
        result = run_psql(sql, DEFAULT_TARGET_DB)
        print(f"Comment on {obj_type} {obj_name}: {'OK' if result else 'MISSING'}")
        if not result:
            ok = False

    # 10. Grants (spot checks)
    grants = [
        ("TABLE", "public.customers", "audit_user", {"SELECT", "INSERT"}),
        ("SCHEMA", "audit_test", "audit_user", {"USAGE"}),
    ]
    for obj_type, obj_name, grantee, expected_privs in grants:
        if obj_type == "TABLE":
            sql = (
                f"SELECT string_agg(privilege_type, ',' ORDER BY privilege_type) "
                f"FROM information_schema.role_table_grants "
                f"WHERE table_schema = '{obj_name.split('.')[0]}' AND table_name = '{obj_name.split('.')[1]}' "
                f"AND grantee = '{grantee}';"
            )
        elif obj_type == "SCHEMA":
            sql = (
                f"SELECT string_agg(privilege_type, ',' ORDER BY privilege_type) "
                f"FROM aclexplode((SELECT nspacl FROM pg_namespace WHERE nspname = '{obj_name}')) "
                f"WHERE grantee = (SELECT oid FROM pg_roles WHERE rolname = '{grantee}');"
            )
        else:
            sql = ""
        result = run_psql(sql, DEFAULT_TARGET_DB)
        actual_privs = set(result.split(",")) if result else set()
        print(f"Grant on {obj_type} {obj_name} to {grantee}: {'OK' if expected_privs.issubset(actual_privs) else f'MISMATCH ({result})'}")
        if not expected_privs.issubset(actual_privs):
            ok = False

    # 11. RLS policies
    policies = [
        ("public", "customers", "customer_select_policy"),
        ("audit_test", "test_customers", "test_customer_select_policy"),
    ]
    for schema, table, policy in policies:
        count = run_psql(
            f"SELECT COUNT(*) FROM pg_policy pol "
            f"JOIN pg_class c ON c.oid = pol.polrelid "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{schema}' AND c.relname = '{table}' AND pol.polname = '{policy}';",
            DEFAULT_TARGET_DB,
        )
        print(f"RLS policy {schema}.{table}.{policy}: {'OK' if count == '1' else 'MISSING'}")
        if count != "1":
            ok = False

    # 12. Cross-schema FK spot check
    fk_count = run_psql(
        "SELECT COUNT(*) FROM information_schema.table_constraints tc "
        "JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
        "WHERE tc.table_schema = 'audit_test' AND tc.table_name = 'fk_child' "
        "AND tc.constraint_type = 'FOREIGN KEY' AND ccu.table_schema = 'public' AND ccu.table_name = 'customers';",
        DEFAULT_TARGET_DB,
    )
    print(f"Cross-schema FK audit_test.fk_child -> public.customers: {'OK' if fk_count == '1' else 'MISSING'}")
    if fk_count != "1":
        ok = False

    # 13. Sequence ownership spot checks
    seq_ownership = [
        ("public", "customers_customer_id_seq", "public.customers.customer_id"),
        ("audit_test", "test_orders_order_id_seq", "audit_test.test_orders.order_id"),
    ]
    for schema, seq, expected_owned in seq_ownership:
        owned = run_psql(
            f"SELECT pg_get_serial_sequence('{expected_owned.split('.')[0]}.{expected_owned.split('.')[1]}', '{expected_owned.split('.')[2]}');",
            DEFAULT_TARGET_DB,
        )
        print(f"Sequence ownership {schema}.{seq}: {'OK' if seq in owned else f'MISMATCH ({owned})'}")
        if seq not in owned:
            ok = False

    print()
    if ok:
        print("VERIFICATION: ALL CHECKS PASSED")
    else:
        print("VERIFICATION: SOME CHECKS FAILED")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL E2E audit fixture verifier")
    parser.add_argument("--reset-source", action="store_true", help="Reset source database")
    parser.add_argument("--run-migration", action="store_true", help="Run migration after reset")
    args = parser.parse_args()

    if args.reset_source:
        reset_source()

    if args.run_migration:
        run_migration()

    if args.reset_source or args.run_migration or True:
        verify()


if __name__ == "__main__":
    main()
