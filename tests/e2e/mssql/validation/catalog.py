"""Read-only MSSQL catalog access for E2E validation.

This module contains ONLY SQL queries — no validation/comparison logic.
It wraps a pyodbc connection and returns simple Python data structures
(list/dict/tuple) so the validator layer can focus on comparison.

Reuses query patterns from:
  - validate_fresh_target.py  (basic catalog queries)
  - core/connectors/mssql/source.py  (column identity/computed/UDT metadata)
  - core/connectors/mssql/objects/  (object-specific discovery patterns)
"""
from __future__ import annotations

from typing import Any

import pyodbc

_SYSTEM_SCHEMAS = frozenset({"sys", "INFORMATION_SCHEMA", "guest", "dbo"})


class MSSQLCatalog:
    """Read-only catalog queries against an MSSQL database."""

    def __init__(self, conn: pyodbc.Connection) -> None:
        self._conn = conn

    @property
    def database_name(self) -> str:
        """Return the name of the database this connection is attached to."""
        return self._conn.getinfo(pyodbc.SQL_DATABASE_NAME)

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #

    def database_exists(self, db_name: str) -> bool:
        """Check whether *db_name* exists on the server (queries from master)."""
        master_conn = self._conn
        with master_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.databases WHERE name = ?", (db_name,)
            )
            return cur.fetchone() is not None

    # ------------------------------------------------------------------ #
    # Schema helpers
    # ------------------------------------------------------------------ #

    def schema_exists(self, schema_name: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.schemas WHERE name = ?", (schema_name,)
            )
            return cur.fetchone() is not None

    def get_all_user_schemas(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM sys.schemas "
                "WHERE name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "ORDER BY name"
            )
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Tables
    # ------------------------------------------------------------------ #

    def get_base_tables(self, schema: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT t.name FROM sys.tables t "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE s.name = ? ORDER BY t.name",
                (schema,),
            )
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Columns — combines INFORMATION_SCHEMA + sys for identity/computed/UDT
    # ------------------------------------------------------------------ #

    def get_columns(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            # 1. Identity / computed metadata from sys
            cur.execute(
                """
                SELECT c.name, c.is_identity,
                       TRY_CAST(ic.seed_value AS INT),
                       TRY_CAST(ic.increment_value AS INT),
                       c.is_computed,
                       TRY_CAST(cc.definition AS NVARCHAR(MAX))
                FROM sys.columns c
                JOIN sys.tables t ON c.object_id = t.object_id
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                LEFT JOIN sys.identity_columns ic
                    ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                LEFT JOIN sys.computed_columns cc
                    ON cc.object_id = c.object_id AND cc.column_id = c.column_id
                WHERE t.name = ? AND s.name = ?
                ORDER BY c.column_id
                """,
                (table, schema),
            )
            meta = {
                r[0]: {
                    "is_identity": bool(r[1]),
                    "identity_seed": r[2],
                    "identity_increment": r[3],
                    "is_computed": bool(r[4]),
                    "computed_definition": r[5],
                }
                for r in cur.fetchall()
            }

            # 2. UDT resolution: columns whose user_type is a user-defined type
            cur.execute(
                """
                SELECT c.name, sy.name, ty.name
                FROM sys.columns c
                JOIN sys.types ty
                    ON c.user_type_id = ty.user_type_id AND ty.is_user_defined = 1
                JOIN sys.schemas sy ON ty.schema_id = sy.schema_id
                JOIN sys.tables t ON c.object_id = t.object_id
                JOIN sys.schemas ts ON t.schema_id = ts.schema_id
                WHERE t.name = ? AND ts.name = ?
                """,
                (table, schema),
            )
            udt_columns = {
                r[0]: f"[{r[1]}].[{r[2]}]" for r in cur.fetchall()
            }

            # 3. Base column info from INFORMATION_SCHEMA
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
                ORDER BY ORDINAL_POSITION
                """,
                (table, schema),
            )
            columns: list[dict[str, Any]] = []
            for row in cur.fetchall():
                col_name, data_type, nullable, max_len, precision, scale = row
                m = meta.get(col_name, {})
                udt = udt_columns.get(col_name)
                columns.append({
                    "name": col_name,
                    "base_type": data_type,
                    "is_nullable": nullable == "YES",
                    "max_length": max_len if max_len is not None and max_len != -1 else None,
                    "max_length_is_max": max_len == -1,
                    "numeric_precision": precision if precision else None,
                    "numeric_scale": scale if scale is not None else None,
                    "is_identity": m.get("is_identity", False),
                    "identity_seed": m.get("identity_seed"),
                    "identity_increment": m.get("identity_increment"),
                    "is_computed": m.get("is_computed", False),
                    "computed_definition": m.get("computed_definition"),
                    "udt_name": udt,
                })
            return columns

    # ------------------------------------------------------------------ #
    # Primary key
    # ------------------------------------------------------------------ #

    def get_primary_key(self, schema: str, table: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT kcu.COLUMN_NAME, tc.CONSTRAINT_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                    ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                    AND tc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
                    AND tc.TABLE_NAME = kcu.TABLE_NAME
                WHERE tc.TABLE_NAME = ? AND tc.TABLE_SCHEMA = ?
                    AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                ORDER BY kcu.ORDINAL_POSITION
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

    def get_foreign_keys(self, schema: str | None = None) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            if schema:
                cur.execute(
                    """
                    SELECT fk.name, pc.name, rc.name,
                           OBJECT_SCHEMA_NAME(fk.referenced_object_id),
                           OBJECT_NAME(fk.referenced_object_id),
                           fk.delete_referential_action_desc,
                           fk.update_referential_action_desc,
                           fkc.constraint_column_id
                    FROM sys.foreign_keys fk
                    JOIN sys.foreign_key_columns fkc
                      ON fk.object_id = fkc.constraint_object_id
                    JOIN sys.columns pc
                      ON fkc.parent_column_id = pc.column_id
                         AND fkc.parent_object_id = pc.object_id
                    JOIN sys.columns rc
                      ON fkc.referenced_column_id = rc.column_id
                         AND fkc.referenced_object_id = rc.object_id
                    JOIN sys.tables t ON fk.parent_object_id = t.object_id
                    JOIN sys.schemas s ON t.schema_id = s.schema_id
                    WHERE s.name = ?
                    ORDER BY fk.name, fkc.constraint_column_id
                    """,
                    (schema,),
                )
            else:
                cur.execute(
                    """
                    SELECT fk.name, pc.name, rc.name,
                           OBJECT_SCHEMA_NAME(fk.referenced_object_id),
                           OBJECT_NAME(fk.referenced_object_id),
                           fk.delete_referential_action_desc,
                           fk.update_referential_action_desc,
                           fkc.constraint_column_id
                    FROM sys.foreign_keys fk
                    JOIN sys.foreign_key_columns fkc
                      ON fk.object_id = fkc.constraint_object_id
                    JOIN sys.columns pc
                      ON fkc.parent_column_id = pc.column_id
                         AND fkc.parent_object_id = pc.object_id
                    JOIN sys.columns rc
                      ON fkc.referenced_column_id = rc.column_id
                         AND fkc.referenced_object_id = rc.object_id
                    WHERE fk.schema_id NOT IN (
                        SELECT schema_id FROM sys.schemas
                        WHERE name IN ('sys','INFORMATION_SCHEMA','guest')
                    )
                    ORDER BY fk.name, fkc.constraint_column_id
                    """,
                )

            fks: dict[str, dict[str, Any]] = {}
            for name, pcol, rcol, rschema, rtable, del_act, upd_act, ord_ in cur.fetchall():
                if name not in fks:
                    fks[name] = {
                        "name": name,
                        "columns": [],
                        "ref_table": rtable,
                        "ref_schema": rschema,
                        "ref_columns": [],
                        "delete_action": del_act,
                        "update_action": upd_act,
                    }
                fks[name]["columns"].append(pcol)
                fks[name]["ref_columns"].append(rcol)
            return list(fks.values())

    # ------------------------------------------------------------------ #
    # Unique constraints (excludes PK)
    # ------------------------------------------------------------------ #

    def get_unique_constraints(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.name, c.name, ic.key_ordinal, ic.is_included_column, ic.is_descending_key
                FROM sys.indexes i
                JOIN sys.tables t ON i.object_id = t.object_id
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                WHERE s.name = ? AND t.name = ?
                  AND i.is_unique_constraint = 1
                ORDER BY i.name, ic.key_ordinal
                """,
                (schema, table),
            )
            groups: dict[str, dict[str, Any]] = {}
            for idx_name, col_name, key_ord, is_incl, _ in cur.fetchall():
                if idx_name not in groups:
                    groups[idx_name] = {"name": idx_name, "columns": []}
                if not is_incl:
                    groups[idx_name]["columns"].append(col_name)
            return list(groups.values())

    # ------------------------------------------------------------------ #
    # Check constraints
    # ------------------------------------------------------------------ #

    def get_check_constraints(self, schema: str, table: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT cc.name, cc.definition
                FROM sys.check_constraints cc
                JOIN sys.tables t ON cc.parent_object_id = t.object_id
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                WHERE s.name = ? AND t.name = ? AND cc.is_disabled = 0
                ORDER BY cc.name
                """,
                (schema, table),
            )
            return [{"name": r[0], "definition": r[1]} for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Default constraints
    # ------------------------------------------------------------------ #

    def get_default_constraints(self, schema: str, table: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT dc.name, COL_NAME(dc.parent_object_id, dc.parent_column_id), dc.definition
                FROM sys.default_constraints dc
                JOIN sys.tables t ON dc.parent_object_id = t.object_id
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                WHERE s.name = ? AND t.name = ?
                ORDER BY dc.name
                """,
                (schema, table),
            )
            return [{"name": r[0], "column": r[1], "definition": r[2]} for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Indexes (excludes PK constraint indexes)
    # ------------------------------------------------------------------ #

    def get_indexes(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.name, i.is_unique, i.type_desc,
                       c.name, ic.is_included_column, ic.is_descending_key,
                       i.filter_definition
                FROM sys.indexes i
                JOIN sys.tables t ON i.object_id = t.object_id
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                JOIN sys.index_columns ic
                    ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                WHERE s.name = ? AND t.name = ?
                  AND i.is_primary_key = 0
                  AND i.is_hypothetical = 0
                  AND i.type_desc IN ('CLUSTERED', 'NONCLUSTERED')
                ORDER BY i.name, ic.key_ordinal
                """,
                (schema, table),
            )
            groups: dict[str, dict[str, Any]] = {}
            for row in cur.fetchall():
                idx_name, is_unique, type_desc, col_name, is_incl, is_desc, filt = row
                if idx_name not in groups:
                    groups[idx_name] = {
                        "name": idx_name,
                        "is_unique": bool(is_unique),
                        "type_desc": type_desc,
                        "columns": [],
                        "included_columns": [],
                        "filter_definition": filt,
                    }
                g = groups[idx_name]
                if is_incl:
                    g["included_columns"].append(col_name)
                else:
                    g["columns"].append(col_name)
            return list(groups.values())

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #

    def get_views(self, schema: str) -> dict[str, str]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.name, m.definition
                FROM sys.views v
                JOIN sys.objects o ON v.object_id = o.object_id
                JOIN sys.schemas s ON o.schema_id = s.schema_id
                JOIN sys.sql_modules m ON o.object_id = m.object_id
                WHERE s.name = ?
                ORDER BY o.name
                """,
                (schema,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}

    # ------------------------------------------------------------------ #
    # Functions (FN, IF, TF)
    # ------------------------------------------------------------------ #

    def get_functions(self, schema: str) -> dict[str, dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.name, o.type, m.definition
                FROM sys.objects o
                JOIN sys.schemas s ON o.schema_id = s.schema_id
                JOIN sys.sql_modules m ON o.object_id = m.object_id
                WHERE s.name = ? AND o.type IN ('FN', 'TF', 'IF')
                ORDER BY o.name
                """,
                (schema,),
            )
            return {r[0]: {"type": r[1], "definition": r[2]} for r in cur.fetchall()}

    # ------------------------------------------------------------------ #
    # Procedures
    # ------------------------------------------------------------------ #

    def get_procedures(self, schema: str) -> dict[str, str]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.name, m.definition
                FROM sys.objects o
                JOIN sys.schemas s ON o.schema_id = s.schema_id
                JOIN sys.sql_modules m ON o.object_id = m.object_id
                WHERE s.name = ? AND o.type = 'P'
                ORDER BY o.name
                """,
                (schema,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #

    def get_triggers(self, schema: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.name, OBJECT_NAME(t.parent_id) AS table_name, t.is_disabled
                FROM sys.triggers t
                JOIN sys.objects o ON t.parent_id = o.object_id
                JOIN sys.schemas s ON o.schema_id = s.schema_id
                WHERE s.name = ?
                ORDER BY t.name
                """,
                (schema,),
            )
            return [
                {"name": r[0], "table": r[1], "is_disabled": bool(r[2])}
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Sequences
    # ------------------------------------------------------------------ #

    def get_sequences(self, schema: str) -> dict[str, dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.name, TYPE_NAME(s.user_type_id) AS seq_type,
                       CAST(s.start_value AS BIGINT), CAST(s.increment AS BIGINT),
                       s.is_cycling, CAST(s.current_value AS BIGINT)
                FROM sys.sequences s
                JOIN sys.schemas sch ON s.schema_id = sch.schema_id
                WHERE sch.name = ?
                ORDER BY s.name
                """,
                (schema,),
            )
            return {
                r[0]: {
                    "data_type": r[1],
                    "start_value": r[2],
                    "increment": r[3],
                    "is_cycling": bool(r[4]),
                    "current_value": r[5],
                }
                for r in cur.fetchall()
            }

    # ------------------------------------------------------------------ #
    # Synonyms
    # ------------------------------------------------------------------ #

    def get_synonyms(self, schema: str) -> dict[str, str]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT syn.name, syn.base_object_name
                FROM sys.synonyms syn
                JOIN sys.schemas sch ON syn.schema_id = sch.schema_id
                WHERE sch.name = ?
                ORDER BY syn.name
                """,
                (schema,),
            )
            result = {}
            for r in cur.fetchall():
                raw_base = r[1] or ""
                result[r[0]] = raw_base.strip("[]").replace("].[", ".")
            return result

    # ------------------------------------------------------------------ #
    # User-defined types (alias types)
    # ------------------------------------------------------------------ #

    def get_user_types(self, schema: str) -> dict[str, dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.name, TYPE_NAME(t.system_type_id) AS base_type, t.is_nullable
                FROM sys.types t
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                WHERE s.name = ? AND t.is_user_defined = 1
                ORDER BY t.name
                """,
                (schema,),
            )
            return {
                r[0]: {"base_type": r[1], "is_nullable": bool(r[2])}
                for r in cur.fetchall()
            }

    # ------------------------------------------------------------------ #
    # Partitioning
    # ------------------------------------------------------------------ #

    def get_partition_functions(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name, type_desc, boundary_value_on_right FROM sys.partition_functions"
            )
            funcs = {}
            for r in cur.fetchall():
                funcs[r[0]] = {
                    "name": r[0], "type_desc": r[1],
                    "boundaries": [], "range_right": bool(r[2]),
                }

        if not funcs:
            return []

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pf.name, prv.value FROM sys.partition_range_values prv "
                "JOIN sys.partition_functions pf ON prv.function_id = pf.function_id "
                "ORDER BY pf.name, prv.boundary_id"
            )
            for pf_name, value in cur.fetchall():
                if pf_name in funcs:
                    funcs[pf_name]["boundaries"].append(str(value))
        return list(funcs.values())

    def get_partition_schemes(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ps.name, pf.name AS func_name, ds.name AS filegroup "
                "FROM sys.partition_schemes ps "
                "JOIN sys.partition_functions pf "
                "  ON ps.function_id = pf.function_id "
                "JOIN sys.destination_data_spaces dds "
                "  ON ps.data_space_id = dds.partition_scheme_id "
                "JOIN sys.data_spaces ds ON dds.data_space_id = ds.data_space_id "
                "ORDER BY ps.name"
            )
            schemes: dict[str, dict[str, Any]] = {}
            for r in cur.fetchall():
                name, func_name, fg = r
                if name not in schemes:
                    schemes[name] = {
                        "name": name, "function": func_name, "filegroups": []
                    }
                schemes[name]["filegroups"].append(fg)
            return list(schemes.values())

    def get_partitioned_tables(self, schema: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.name, c.name AS partition_column, ps.name AS scheme_name
                FROM sys.tables t
                JOIN sys.indexes i ON t.object_id = i.object_id AND i.index_id IN (0, 1)
                JOIN sys.data_spaces ds ON i.data_space_id = ds.data_space_id
                JOIN sys.partition_schemes ps ON ds.data_space_id = ps.data_space_id
                JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                WHERE s.name = ? AND i.type IN (0, 1)
                ORDER BY t.name
                """,
                (schema,),
            )
            return [
                {"table": r[0], "partition_column": r[1], "scheme": r[2]}
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Security (read-only)
    # ------------------------------------------------------------------ #

    def get_security_info(
        self, role_names: list[str] | None = None, user_names: list[str] | None = None
    ) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            # Roles
            if role_names:
                placeholders = ", ".join("?" for _ in role_names)
                cur.execute(
                    f"SELECT name FROM sys.database_principals "
                    f"WHERE type IN ('R', 'C') AND name IN ({placeholders}) "
                    f"ORDER BY name",
                    role_names,
                )
            else:
                cur.execute(
                    "SELECT name FROM sys.database_principals "
                    "WHERE type IN ('R', 'C') AND name NOT IN "
                    "('public','dbo','guest','INFORMATION_SCHEMA','sys',"
                    "'db_owner','db_securityadmin','db_accessadmin','db_ddladmin',"
                    "'db_datareader','db_datawriter','db_backupoperator',"
                    "'db_denydatareader','db_denydatawriter') "
                    "ORDER BY name"
                )
            roles = [r[0] for r in cur.fetchall()]

            # Users
            if user_names:
                placeholders = ", ".join("?" for _ in user_names)
                cur.execute(
                    f"SELECT name FROM sys.database_principals "
                    f"WHERE type IN ('S', 'U') AND name IN ({placeholders}) "
                    f"ORDER BY name",
                    user_names,
                )
            else:
                cur.execute(
                    "SELECT name FROM sys.database_principals "
                    "WHERE type IN ('S', 'U') "
                    "AND name NOT IN ('dbo','guest','INFORMATION_SCHEMA','sys') "
                    "ORDER BY name"
                )
            users = [r[0] for r in cur.fetchall()]

            # Memberships
            cur.execute(
                "SELECT m.name AS member_name, r.name AS role_name "
                "FROM sys.database_role_members drm "
                "JOIN sys.database_principals m ON drm.member_principal_id = m.principal_id "
                "JOIN sys.database_principals r ON drm.role_principal_id = r.principal_id "
                "WHERE r.type IN ('R','C') AND m.type IN ('S','U','R') "
                "ORDER BY r.name, m.name"
            )
            memberships = [
                {"member": r[0], "role": r[1]} for r in cur.fetchall()
            ]

            # Grants
            cur.execute(
                "SELECT dp.name AS grantee, perm.permission_name, "
                "perm.class_desc, OBJECT_NAME(perm.major_id) AS object_name, "
                "SCHEMA_NAME(o.schema_id) AS schema_name "
                "FROM sys.database_permissions perm "
                "JOIN sys.database_principals dp "
                "  ON perm.grantee_principal_id = dp.principal_id "
                "LEFT JOIN sys.objects o "
                "  ON perm.major_id = o.object_id "
                "  AND perm.class_desc = 'OBJECT_OR_COLUMN' "
                "WHERE dp.name NOT IN ('public','dbo','guest','INFORMATION_SCHEMA','sys') "
                "ORDER BY dp.name, perm.permission_name"
            )
            grants = [
                {
                    "grantee": r[0],
                    "privilege": r[1],
                    "class_desc": r[2],
                    "object_name": r[3] or "",
                    "schema_name": r[4] or "",
                }
                for r in cur.fetchall()
            ]

        return {
            "roles": roles,
            "users": users,
            "memberships": memberships,
            "grants": grants,
        }

    # ------------------------------------------------------------------ #
    # Extended properties
    # ------------------------------------------------------------------ #

    def get_extended_properties(self, schema: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                DECLARE @schema_id int = SCHEMA_ID('{schema}');

                SELECT ep.class_desc,
                       CASE ep.class
                           WHEN 1 THEN OBJECT_NAME(ep.major_id)
                           WHEN 3 THEN SCHEMA_NAME(ep.major_id)
                           WHEN 4 THEN DB_NAME()
                       END AS object_name,
                       ep.name AS prop_name,
                       ep.value AS prop_value,
                       COL_NAME(ep.major_id, ep.minor_id) AS column_name
                FROM sys.extended_properties ep
                WHERE ep.major_id IN (
                    SELECT object_id FROM sys.objects WHERE schema_id = @schema_id
                )
                   OR (ep.class = 3 AND ep.major_id = @schema_id)
                ORDER BY ep.class_desc, object_name, column_name, ep.name
                """,
            )
            return [
                {
                    "class_desc": r[0],
                    "object_name": r[1],
                    "prop_name": r[2],
                    "value": r[3],
                    "column_name": r[4] if r[4] else "",
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # Row counts
    # ------------------------------------------------------------------ #

    def get_row_count(self, schema: str, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM [{schema}].[{table}]"
            )
            return cur.fetchone()[0]

    def count_by_column_value(
        self, schema: str, table: str, column: str, value: str
    ) -> int:
        """Count rows where *column* equals *value*."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM [{schema}].[{table}] "
                f"WHERE [{column}] = ?",
                (value,),
            )
            return cur.fetchone()[0]
