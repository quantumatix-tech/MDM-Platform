"""Automated catalog check for a fresh MSSQL target.

Complements docs/mssql/demo_local_runbook.md (STEP 14) by running the same
object-inventory queries automatically. It never stores a password: the secret
is read from the environment variable that the platform's secret resolver
already uses, and the connection target is configured through SQLSERVER_* so
this script can never silently point at a production or cloud database.

Usage:
    $env:SQLSERVER_HOST = "localhost,1533"
    $env:SQLSERVER_DATABASE = "mssql_target_db2"
    $env:SQLSERVER_USER = "sa"
    $env:SECRET_mssql_target_pass = "<local-sa-password>"
    python validate_fresh_target.py
"""

import os

import pyodbc

# Local-only utility. The password is never stored in this file; supply it via
# the environment variable that the platform's secret resolver already uses.
PASSWORD = os.environ.get("SECRET_mssql_target_pass")
if not PASSWORD:
    raise SystemExit(
        "Set SECRET_mssql_target_pass before running, e.g.\n"
        '  $env:SECRET_mssql_target_pass = Read-Host -AsSecureString | ConvertFrom-SecureString -AsPlainText'
    )

# Connection target. Defaults match config/mssql_demo.yaml; override to inspect a
# different local target. There is no cloud/production default.
SERVER = os.environ.get("SQLSERVER_HOST", "localhost,1533")
DATABASE = os.environ.get("SQLSERVER_DATABASE", "mssql_target_db2")
USERNAME = os.environ.get("SQLSERVER_USER", "sa")
# Schemas to inventory, comma-separated. The demo creates a single `sales` schema.
SCHEMAS = [s.strip() for s in os.environ.get("SQLSERVER_SCHEMAS", "sales").split(",") if s.strip()]
if not SCHEMAS:
    raise SystemExit("SQLSERVER_SCHEMAS resolved to an empty schema list.")

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=" + SERVER
    + ";DATABASE=" + DATABASE
    + ";UID=" + USERNAME + ";PWD=" + PASSWORD
    + ";TrustServerCertificate=yes",
    timeout=10,
)
cursor = conn.cursor()

print(f"=== TARGET VALIDATION: {DATABASE} on {SERVER} ===")
print(f"=== SCHEMAS: {', '.join(SCHEMAS)} ===")

# Build the SCHEMA_ID(...) filter list once so every query below stays consistent.
schema_ids = ", ".join(f"SCHEMA_ID('{s}')" for s in SCHEMAS)
placeholders = ", ".join("?" for _ in SCHEMAS)
schema_params = tuple(SCHEMAS)


def in_schemas() -> str:
    """Return a SQL predicate restricting a query to the configured schemas."""
    return f"schema_id IN ({schema_ids})"


# 1. Schemas
cursor.execute(
    f"SELECT name FROM sys.schemas WHERE name IN ({placeholders})", schema_params
)
print('1. Schemas:', [row[0] for row in cursor.fetchall()])

# 2. Tables + row counts
cursor.execute(
    f"SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
    f"WHERE TABLE_SCHEMA IN ({placeholders}) ORDER BY TABLE_SCHEMA, TABLE_NAME",
    schema_params,
)
tables = cursor.fetchall()
print('2. Tables:', tables)

# Row counts per table
for schema, table in tables:
    cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
    count = cursor.fetchone()[0]
    print(f'   {schema}.{table}: {count} rows')

# 3. PK/FK/Check/Default constraints
cursor.execute(f"""
    SELECT OBJECT_NAME(parent_object_id) as table_name, name, type_desc
    FROM sys.key_constraints WHERE type = 'PK' AND {in_schemas()}
""")
print('3. PKs:', cursor.fetchall())

cursor.execute(f"""
    SELECT OBJECT_NAME(parent_object_id) as table_name, name
    FROM sys.foreign_keys WHERE {in_schemas()}
""")
print('   FKs:', cursor.fetchall())

cursor.execute(f"""
    SELECT OBJECT_NAME(parent_object_id) as table_name, name, definition
    FROM sys.check_constraints WHERE {in_schemas()}
""")
print('   CHECK constraints:', cursor.fetchall())

cursor.execute(f"""
    SELECT OBJECT_NAME(parent_object_id) as table_name, name, definition
    FROM sys.default_constraints WHERE {in_schemas()}
""")
print('   DEFAULT constraints:', cursor.fetchall())

# 4. Identity + computed columns
cursor.execute(f"""
    SELECT OBJECT_NAME(object_id) as table_name, name, is_identity, is_computed
    FROM sys.columns WHERE object_id IN (SELECT object_id FROM sys.objects WHERE {in_schemas()}) AND (is_identity = 1 OR is_computed = 1)
""")
print('4. Identity/Computed columns:', cursor.fetchall())

# 5. Sequences
cursor.execute(f"SELECT name, schema_id FROM sys.sequences WHERE {in_schemas()}")
print('5. Sequences:', cursor.fetchall())

# 6. Indexes
cursor.execute(f"""
    SELECT OBJECT_NAME(i.object_id) as table_name, i.name, i.type_desc, i.is_unique, i.is_primary_key
    FROM sys.indexes i JOIN sys.objects o ON i.object_id = o.object_id
    WHERE o.{in_schemas()} AND i.type_desc IN ('CLUSTERED', 'NONCLUSTERED') AND i.is_hypothetical = 0
""")
print('6. Indexes:', cursor.fetchall())

# 7. Views
cursor.execute(f"SELECT name FROM sys.views WHERE {in_schemas()}")
print('7. Views:', cursor.fetchall())

# 8. Functions + Procedures
cursor.execute(f"SELECT name, type FROM sys.objects WHERE type IN ('FN', 'IF', 'TF', 'P') AND {in_schemas()}")
print('8. Functions/Procedures:', cursor.fetchall())

# 9. Triggers
cursor.execute(f"""
    SELECT t.name, OBJECT_NAME(t.parent_id) as table_name, t.is_disabled
    FROM sys.triggers t JOIN sys.objects o ON t.parent_id = o.object_id
    WHERE o.{in_schemas()}
""")
print('9. Triggers:', cursor.fetchall())

# 10. Synonyms
cursor.execute(f"SELECT name, base_object_name FROM sys.synonyms WHERE {in_schemas()}")
print('10. Synonyms:', cursor.fetchall())

# 11. UDTs
cursor.execute(f"SELECT name FROM sys.types WHERE is_user_defined = 1 AND {in_schemas()}")
print('11. UDTs:', cursor.fetchall())

# 12. Partition functions/schemes + actual partitioned tables
cursor.execute("SELECT name, type_desc, boundary_value_on_right FROM sys.partition_functions")
print('12. Partition functions:', cursor.fetchall())

cursor.execute("SELECT ps.name as scheme, pf.name as func FROM sys.partition_schemes ps JOIN sys.partition_functions pf ON ps.function_id = pf.function_id")
print('   Partition schemes:', cursor.fetchall())

# Check actual partitioned tables
cursor.execute(f"""
    SELECT t.name as table_name, ps.name as scheme_name, pf.name as function_name
    FROM sys.tables t
    JOIN sys.indexes i ON t.object_id = i.object_id AND i.index_id IN (0,1)
    JOIN sys.data_spaces ds ON i.data_space_id = ds.data_space_id
    JOIN sys.partition_schemes ps ON ds.data_space_id = ps.data_space_id
    JOIN sys.partition_functions pf ON ps.function_id = pf.function_id
    WHERE t.{in_schemas()}
""")
print('   Actual partitioned tables:', cursor.fetchall())

# Partition row distribution
cursor.execute(f"""
    SELECT t.name as table_name, p.partition_number, p.rows
    FROM sys.tables t
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE t.{in_schemas()}
    ORDER BY t.name, p.partition_number
""")
print('   Partition row distribution:', cursor.fetchall())

# 13. Specialized types
cursor.execute(f"""
    SELECT OBJECT_NAME(c.object_id) as table_name, c.name, t.name as type_name
    FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id
    WHERE c.object_id IN (SELECT object_id FROM sys.objects WHERE {in_schemas()})
      AND t.name IN ('xml', 'json', 'uniqueidentifier', 'sql_variant', 'varbinary', 'binary', 'hierarchyid', 'geometry', 'geography')
""")
print('13. Specialized type columns:', cursor.fetchall())

# 14. Users/roles/memberships/grants
cursor.execute("SELECT name, type FROM sys.database_principals WHERE type IN ('S', 'U', 'R', 'C') AND name NOT IN ('dbo', 'guest', 'INFORMATION_SCHEMA', 'sys') AND principal_id > 4")
print('14. Users/Roles:', cursor.fetchall())

# Role memberships and grants are reported for every non-system role/principal, so
# the output stays useful regardless of which demo names were used.
cursor.execute("""
    SELECT dp.name as member_name, dr.name as role_name
    FROM sys.database_role_members drm
    JOIN sys.database_principals dp ON drm.member_principal_id = dp.principal_id
    JOIN sys.database_principals dr ON drm.role_principal_id = dr.principal_id
    ORDER BY dr.name, dp.name
""")
print('   Role memberships:', cursor.fetchall())

cursor.execute("""
    SELECT dp.name as grantee, perm.permission_name, perm.class_desc, OBJECT_NAME(perm.major_id) as object_name
    FROM sys.database_permissions perm
    JOIN sys.database_principals dp ON perm.grantee_principal_id = dp.principal_id
    ORDER BY dp.name, perm.permission_name
""")
print('   Grants:', cursor.fetchall())

# 15. Extended properties
cursor.execute(f"""
    SELECT ep.class_desc,
        CASE
            WHEN ep.class = 1 THEN OBJECT_NAME(ep.major_id)
            WHEN ep.class = 3 THEN SCHEMA_NAME(ep.major_id)
            WHEN ep.class = 4 THEN (SELECT c.name FROM sys.columns c WHERE c.object_id = ep.major_id AND c.column_id = ep.minor_id)
            ELSE CAST(ep.major_id AS VARCHAR)
        END as objname,
        ep.name, ep.value
    FROM sys.extended_properties ep
    WHERE ep.major_id IN (SELECT object_id FROM sys.objects WHERE {in_schemas()})
       OR ep.class = 3 AND ep.major_id IN ({schema_ids})
""")
print('15. Extended Properties:', cursor.fetchall())

# 16. Cross-schema FKs (any FK whose referenced table sits in another schema)
cursor.execute(f"""
    SELECT OBJECT_NAME(fk.parent_object_id) as table_name, fk.name, SCHEMA_NAME(fk.schema_id) as fk_schema,
           OBJECT_NAME(fk.referenced_object_id) as ref_table, SCHEMA_NAME(o.schema_id) as ref_schema
    FROM sys.foreign_keys fk JOIN sys.objects o ON fk.referenced_object_id = o.object_id
    WHERE fk.{in_schemas()} AND SCHEMA_NAME(fk.schema_id) <> SCHEMA_NAME(o.schema_id)
""")
print('16. Cross-schema FKs:', cursor.fetchall())

conn.close()