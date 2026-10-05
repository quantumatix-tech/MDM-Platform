"""Read-only PostgreSQL catalog access for E2E Phase B validation.

This module wraps a ``psycopg`` connection and returns simple Python data
structures (list/dict/tuple) so the validator layer can focus on comparison.

All queries are read-only and use standard pg_catalog / information_schema views.
PostgreSQL identifier folding (lowercase when unquoted) is handled by returning
columns as-is from the catalog; callers that need case-insensitive matching
should normalise in the validator layer.
"""
from __future__ import annotations

from typing import Any

import psycopg


class PostgreSQLCatalog:
    """Read-only catalog queries against a PostgreSQL database."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @property
    def database_name(self) -> str:
        with self._conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            return cur.fetchone()[0]

    # ------------------------------------------------------------------ #
    # Schema helpers
    # ------------------------------------------------------------------ #

    def schema_exists(self, schema_name: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s",
                (schema_name,),
            )
            return cur.fetchone() is not None

    # ------------------------------------------------------------------ #
    # Tables
    # ------------------------------------------------------------------ #

    def get_base_tables(self, schema: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT relname FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = %s "
                "  AND c.relkind IN ('r', 'p') "
                "  AND NOT c.relispartition "
                "ORDER BY c.relname",
                (schema,),
            )
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Columns
    # ------------------------------------------------------------------ #

    def get_columns(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.attname AS column_name,
                    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                    c.is_nullable,
                    c.character_maximum_length,
                    c.numeric_precision,
                    c.numeric_scale,
                    c.column_default,
                    c.is_identity,
                    c.identity_generation,
                    c.identity_start,
                    c.identity_increment,
                    a.attgenerated,
                    a.attidentity
                FROM information_schema.columns c
                JOIN pg_class pc ON pc.relname = c.table_name
                JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                JOIN pg_attribute a ON a.attrelid = pc.oid AND a.attname = c.column_name
                WHERE c.table_name = %s AND c.table_schema = %s
                    AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY c.ordinal_position
                """,
                (table, schema),
            )
            columns: list[dict[str, Any]] = []
            for row in cur.fetchall():
                (
                    col_name, data_type, nullable, max_len,
                    precision, scale, col_default, is_identity,
                    identity_generation, identity_start, identity_increment,
                    attgenerated, attidentity,
                ) = row

                is_generated = attgenerated == "s"
                gen_expr = None
                if is_generated:
                    cur.execute(
                        "SELECT pg_get_expr(ad.adbin, ad.adrelid) "
                        "FROM pg_attrdef ad "
                        "JOIN pg_class pc ON pc.oid = ad.adrelid "
                        "JOIN pg_namespace pn ON pn.oid = pc.relnamespace "
                        "JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum "
                        "WHERE pc.relname = %s AND pn.nspname = %s AND a.attname = %s",
                        (table, schema, col_name),
                    )
                    result = cur.fetchone()
                    gen_expr = result[0] if result else None

                columns.append({
                    "name": col_name,
                    "base_type": data_type,
                    "is_nullable": nullable == "YES",
                    "size": max_len if max_len is not None and max_len != -1 else None,
                    "precision": precision if precision else None,
                    "scale": scale if scale is not None else None,
                    "default": col_default,
                    "is_identity": is_identity == "YES" or attidentity in ("a", "d"),
                    "identity_kind": identity_generation if is_identity == "YES" else None,
                    "identity_seed": int(identity_start) if identity_start else None,
                    "identity_increment": int(identity_increment) if identity_increment else None,
                    "is_generated": is_generated,
                    "generated_expression": gen_expr,
                })
            return columns

    # ------------------------------------------------------------------ #
    # Primary key
    # ------------------------------------------------------------------ #

    def get_primary_key(self, schema: str, table: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT kcu.column_name, tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                    AND tc.table_name = kcu.table_name
                WHERE tc.table_name = %s AND tc.table_schema = %s
                    AND tc.constraint_type = 'PRIMARY KEY'
                ORDER BY kcu.ordinal_position
                """,
                (table, schema),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            return {
                "name": rows[0][1],
                "columns": [r[0] for r in rows],
            }

    # ------------------------------------------------------------------ #
    # Foreign keys
    # ------------------------------------------------------------------ #

    def get_foreign_keys(self, schema: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    tc.constraint_name,
                    kcu.column_name,
                    ccu.table_name AS ref_table,
                    ccu.column_name AS ref_column,
                    rc.delete_rule,
                    rc.update_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                    AND tc.table_name = kcu.table_name
                JOIN information_schema.referential_constraints rc
                    ON tc.constraint_name = rc.constraint_name
                    AND tc.constraint_schema = rc.constraint_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON rc.unique_constraint_name = ccu.constraint_name
                    AND rc.unique_constraint_schema = ccu.table_schema
                    AND ccu.column_name = kcu.column_name
                WHERE tc.table_schema = %s
                    AND tc.constraint_type = 'FOREIGN KEY'
                ORDER BY tc.constraint_name, kcu.ordinal_position
                """,
                (schema,),
            )
            fks: dict[str, dict[str, Any]] = {}
            for row in cur.fetchall():
                fk_name, col, ref_table, ref_col, on_delete, on_update = row
                if fk_name not in fks:
                    fks[fk_name] = {
                        "name": fk_name,
                        "columns": [],
                        "ref_table": ref_table,
                        "ref_columns": [],
                        "on_delete": on_delete,
                        "on_update": on_update,
                    }
                fks[fk_name]["columns"].append(col)
                fks[fk_name]["ref_columns"].append(ref_col)
            return list(fks.values())

    # ------------------------------------------------------------------ #
    # Unique constraints (excluding PK)
    # ------------------------------------------------------------------ #

    def get_unique_constraints(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = (%s || '.' || %s)::regclass
                    AND contype = 'u'
                ORDER BY conname
                """,
                (schema, table),
            )
            groups: list[dict[str, Any]] = []
            for uc_name, uc_def in cur.fetchall():
                import re
                cols_match = re.search(r'UNIQUE\s*\(([^)]+)\)', uc_def, re.IGNORECASE)
                if cols_match:
                    cols = [c.strip().strip('"') for c in cols_match.group(1).split(',')]
                else:
                    cols = []
                groups.append({"name": uc_name, "columns": cols})
            return groups

    # ------------------------------------------------------------------ #
    # Check constraints
    # ------------------------------------------------------------------ #

    def get_check_constraints(self, schema: str, table: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT tc.constraint_name, cc.check_clause
                FROM information_schema.table_constraints tc
                JOIN information_schema.check_constraints cc
                    ON tc.constraint_name = cc.constraint_name
                    AND tc.constraint_schema = cc.constraint_schema
                WHERE tc.table_name = %s AND tc.table_schema = %s
                    AND tc.constraint_type = 'CHECK'
                    AND cc.check_clause NOT LIKE '%%IS NOT NULL%%'
                ORDER BY tc.constraint_name
                """,
                (table, schema),
            )
            return [{"name": r[0], "definition": r[1]} for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Indexes (excluding constraint-backed indexes)
    # ------------------------------------------------------------------ #

    def get_indexes(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    i.relname AS index_name,
                    ix.indisunique AS is_unique,
                    a.attname AS column_name
                FROM pg_class t
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_index ix ON ix.indrelid = t.oid
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
                WHERE n.nspname = %s AND t.relname = %s
                    AND NOT ix.indisprimary
                    AND NOT ix.indisunique
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_constraint c
                        WHERE c.conindid = i.oid
                    )
                ORDER BY i.relname, a.attnum
                """,
                (schema, table),
            )
            groups: dict[str, dict[str, Any]] = {}
            for row in cur.fetchall():
                idx_name, is_unique, col_name = row
                if idx_name not in groups:
                    groups[idx_name] = {
                        "name": idx_name,
                        "is_unique": bool(is_unique),
                        "columns": [],
                    }
                groups[idx_name]["columns"].append(col_name)
            return list(groups.values())

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #

    def get_views(self, schema: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, view_definition
                FROM information_schema.views
                WHERE table_schema = %s
                ORDER BY table_name
                """,
                (schema,),
            )
            return [
                {"name": r[0], "definition": r[1]}
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Functions & Procedures
    # ------------------------------------------------------------------ #

    def get_functions(self, schema: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.proname AS name,
                    CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure' END AS kind
                FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = %s
                    AND p.prokind IN ('f', 'p')
                ORDER BY p.proname
                """,
                (schema,),
            )
            return [
                {"name": r[0], "kind": r[1]}
                for r in cur.fetchall()
            ]

    def get_routines(self, schema: str) -> list[dict[str, Any]]:
        """Return routines with full signature info for D-diff comparison.

        PostgreSQL allows function overloading (same name, different argument
        types).  We identify routines by schema + name + argument signature
        so overloaded functions are compared independently.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    n.nspname AS schema_name,
                    p.proname AS name,
                    CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure' END AS kind,
                    pg_get_function_arguments(p.oid) AS arg_types,
                    pg_get_function_result(p.oid) AS return_type,
                    l.lanname AS language
                FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                JOIN pg_language l ON p.prolang = l.oid
                WHERE n.nspname = %s
                    AND p.prokind IN ('f', 'p')
                ORDER BY p.proname, pg_get_function_arguments(p.oid)
                """,
                (schema,),
            )
            return [
                {
                    "schema": r[0],
                    "name": r[1],
                    "kind": r[2],
                    "arg_types": r[3],
                    "return_type": r[4],
                    "language": r[5],
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #

    def get_triggers(self, schema: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.tgname AS name,
                    c.relname AS table_name,
                    t.tgenabled != 'O' AS is_disabled,
                    CASE t.tgtype & 2 WHEN 2 THEN 'BEFORE' ELSE 'AFTER' END AS timing,
                    CASE t.tgtype & 28 WHEN 28 THEN 'INSERT' WHEN 24 THEN 'DELETE'
                         WHEN 20 THEN 'UPDATE' WHEN 28 THEN 'INSERT,DELETE'
                         ELSE 'INSERT,UPDATE,DELETE' END AS event,
                    CASE t.tgtype & 1 WHEN 1 THEN 'ROW' ELSE 'STATEMENT' END AS granularity
                FROM pg_trigger t
                JOIN pg_class c ON t.tgrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = %s
                    AND NOT t.tgisinternal
                ORDER BY c.relname, t.tgname
                """,
                (schema,),
            )
            return [
                {
                    "name": r[0],
                    "table": r[1],
                    "is_disabled": bool(r[2]),
                    "timing": r[3],
                    "event": r[4],
                    "granularity": r[5],
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Sequences
    # ------------------------------------------------------------------ #

    def get_standalone_sequences(self, schema: str) -> dict[str, dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.sequencename,
                    s.start_value::bigint,
                    s.increment_by::bigint
                FROM pg_sequences s
                WHERE s.schemaname = %s
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_depend d
                        JOIN pg_class sc ON sc.oid = d.objid
                        JOIN pg_namespace sn ON sn.oid = sc.relnamespace
                        WHERE d.deptype IN ('a', 'i')
                            AND sc.relname = s.sequencename
                            AND sn.nspname = s.schemaname
                    )
                ORDER BY s.sequencename
                """,
                (schema,),
            )
            return {
                r[0]: {
                    "data_type": "bigint",
                    "start_value": r[1],
                    "increment": r[2],
                }
                for r in cur.fetchall()
            }

    # ------------------------------------------------------------------ #
    # Partitions
    # ------------------------------------------------------------------ #

    def get_partitions(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    nsp.nspname AS schema_name,
                    c.relname AS partition_name,
                    parent_ns.nspname AS parent_schema,
                    parent.relname AS parent_table,
                    pg_get_expr(c.relpartbound, c.oid) AS bound_expr
                FROM pg_class c
                JOIN pg_namespace nsp ON c.relnamespace = nsp.oid
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class parent ON i.inhparent = parent.oid
                JOIN pg_namespace parent_ns ON parent.relnamespace = parent_ns.oid
                WHERE c.relispartition = true
                  AND c.relkind IN ('r', 'p', 'f')
                ORDER BY parent.relname, c.relname
                """,
            )
            return [
                {
                    "schema_name": r[0],
                    "name": r[1],
                    "parent_schema": r[2],
                    "parent_table": r[3],
                    "bound_expr": r[4],
                }
                for r in cur.fetchall()
            ]

    def get_partitioned_tables(self, schema: str) -> list[dict[str, Any]]:
        """Return partitioned table parents with strategy and partition key."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.relname AS table_name,
                    pt.partstrat AS strategy,
                    pg_get_partkeydef(c.oid) AS partition_key
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                JOIN pg_partitioned_table pt ON pt.partrelid = c.oid
                WHERE n.nspname = %s
                ORDER BY c.relname
                """,
                (schema,),
            )
            results: list[dict[str, Any]] = []
            for table_name, strategy, part_key in cur.fetchall():
                if strategy == "r":
                    strat = "RANGE"
                elif strategy == "l":
                    strat = "LIST"
                elif strategy == "h":
                    strat = "HASH"
                else:
                    strat = strategy
                results.append({
                    "table": table_name,
                    "strategy": strat,
                    "partition_key": part_key,
                })
            return results

    # ------------------------------------------------------------------ #
    # RLS & policies
    # ------------------------------------------------------------------ #

    def get_rls_enabled(self, schema: str, table: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relrowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE c.relname = %s AND n.nspname = %s
                """,
                (table, schema),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False

    def get_rls_policies(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pol.polname,
                    pol.polpermissive,
                    pg_get_expr(pol.polqual, pol.polrelid),
                    pg_get_expr(pol.polwithcheck, pol.polrelid),
                    (pol.polcmd = 'r') AS is_select,
                    (pol.polcmd = 'a') AS is_insert,
                    (pol.polcmd = 'w') AS is_update,
                    (pol.polcmd = 'd') AS is_delete
                FROM pg_policy pol
                JOIN pg_class c ON c.oid = pol.polrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                ORDER BY pol.polname
                """,
                (schema, table),
            )
            return [
                {
                    "name": r[0],
                    "permissive": bool(r[1]),
                    "using": r[2],
                    "with_check": r[3],
                    "is_select": bool(r[4]),
                    "is_insert": bool(r[5]),
                    "is_update": bool(r[6]),
                    "is_delete": bool(r[7]),
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Security: roles
    # ------------------------------------------------------------------ #

    def get_roles(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT rolname FROM pg_roles "
                "WHERE rolname NOT LIKE 'pg_%' "
                "  AND rolname NOT IN ('postgres') "
                "ORDER BY rolname"
            )
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Grants
    # ------------------------------------------------------------------ #

    def get_grants(self, schema: str) -> list[dict[str, Any]]:
        grants: list[dict[str, Any]] = []

        with self._conn.cursor() as cur:
            # Table grants
            cur.execute(
                """
                SELECT
                    grantee, privilege_type, table_name
                FROM information_schema.role_table_grants
                WHERE table_schema = %s
                    AND grantee NOT IN ('PUBLIC')
                    AND grantor != grantee
                ORDER BY table_name, grantee, privilege_type
                """,
                (schema,),
            )
            for grantee, privilege_type, table_name in cur.fetchall():
                grants.append({
                    "grantee": grantee,
                    "privilege": privilege_type,
                    "object_type": "TABLE",
                    "object_name": table_name,
                })

            # Sequence grants (aclexplode on pg_class relacl)
            cur.execute(
                """
                SELECT
                    r.rolname AS grantee,
                    acl.privilege_type AS privilege,
                    c.relname AS object_name
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                JOIN aclexplode(c.relacl) acl ON true
                JOIN pg_roles r ON r.oid = acl.grantee
                WHERE n.nspname = %s
                    AND c.relkind = 'S'
                    AND c.relacl IS NOT NULL
                    AND acl.grantee != 0
                    AND acl.grantor != acl.grantee
                ORDER BY c.relname, r.rolname
                """,
                (schema,),
            )
            for grantee, privilege, obj_name in cur.fetchall():
                grants.append({
                    "grantee": grantee,
                    "privilege": privilege,
                    "object_type": "SEQUENCE",
                    "object_name": obj_name,
                })

        return grants

    # ------------------------------------------------------------------ #
    # Comments
    # ------------------------------------------------------------------ #

    def get_comments(self, schema: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    n.nspname AS schema_name,
                    c.relname AS object_name,
                    a.attname AS column_name,
                    NULLIF(d.description, '') AS comment
                FROM pg_description d
                JOIN pg_class c ON c.oid = d.objoid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attribute a ON a.attrelid = d.objoid AND a.attnum = d.objsubid
                WHERE n.nspname = %s
                    AND d.description IS NOT NULL
                ORDER BY c.relname, a.attnum
                """,
                (schema,),
            )
            return [
                {
                    "object_type": "COLUMN" if r[2] else "TABLE",
                    "schema_name": r[0],
                    "object_name": f"{r[1]}.{r[2]}" if r[2] else r[1],
                    "comment": r[3],
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # User-defined types (ENUM)
    # ------------------------------------------------------------------ #

    def get_user_types(self, schema: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT typname, typtype
                FROM pg_type t
                JOIN pg_namespace n ON t.typnamespace = n.oid
                WHERE n.nspname = %s AND t.typtype IN ('e', 'd', 'c')
                ORDER BY typname
                """,
                (schema,),
            )
            return [{"name": r[0], "kind": r[1]} for r in cur.fetchall()]

    def get_enum_labels(self, schema: str, type_name: str) -> list[str]:
        """Return ordered ENUM labels for an enum type."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT enums.enumlabel
                FROM pg_type t
                JOIN pg_namespace n ON t.typnamespace = n.oid
                JOIN pg_enum enums ON enums.enumtypid = t.oid
                WHERE n.nspname = %s AND t.typname = %s
                ORDER BY enums.enumsortorder
                """,
                (schema, type_name),
            )
            return [r[0] for r in cur.fetchall()]

    def get_all_schemas(self) -> list[str]:
        """Return all user-defined schema names (excluding system schemas)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE nspname NOT LIKE 'pg_%'
                  AND nspname != 'information_schema'
                ORDER BY nspname
                """,
            )
            return [r[0] for r in cur.fetchall()]

    def get_all_tables(self, schema: str) -> list[str]:
        """Return all base tables AND partitioned table parents in a schema."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT relname
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = %s
                  AND c.relkind IN ('r', 'p')
                ORDER BY relname
                """,
                (schema,),
            )
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Row counts
    # ------------------------------------------------------------------ #

    def get_row_count(self, schema: str, table: str) -> int:
        table_qname = f'"{schema}"."{table}"'
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table_qname}")
            return cur.fetchone()[0]

    def count_by_column_value(
        self, schema: str, table: str, column: str, value: str
    ) -> int:
        col_qname = f'"{column}"'
        table_qname = f'"{schema}"."{table}"'
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {table_qname} WHERE {col_qname} = %s",
                (value,),
            )
            return cur.fetchone()[0]
