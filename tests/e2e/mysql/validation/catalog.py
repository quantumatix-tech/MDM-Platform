"""Read-only MySQL catalog access for E2E Phase B validation.

This module wraps a ``mysql.connector`` connection and returns simple Python
data structures (list/dict/tuple) so the validator layer can focus on
comparison.

All queries are read-only and use standard INFORMATION_SCHEMA views.
MySQL folds unquoted identifiers to lowercase on Windows but preserves case
on Linux; we return names as-is from the catalog and let callers normalize.
"""
from __future__ import annotations

from typing import Any

from mysql.connector.connection import MySQLConnection

from core.connectors.mysql._models import _q


class MySQLCatalog:
    """Read-only catalog queries against a MySQL database."""

    def __init__(self, conn: MySQLConnection) -> None:
        self._conn = conn

    @property
    def database_name(self) -> str:
        with self._conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            row = cur.fetchone()
            return row[0] if row else ""

    def schema_exists(self, schema_name: str) -> bool:
        # MySQL databases are namespaces; check against INFORMATION_SCHEMA.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
                (schema_name,),
            )
            return cur.fetchone() is not None

    def get_schemas(self) -> list[str]:
        """Return all non-system schema (database) names."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
                "WHERE SCHEMA_NAME NOT IN "
                "('mysql','information_schema','performance_schema','sys') "
                "ORDER BY SCHEMA_NAME"
            )
            return [r[0] for r in cur.fetchall()]

    def get_base_tables(self, database: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME",
                (database,),
            )
            return [r[0] for r in cur.fetchall()]

    def get_table_names_with_types(self, database: str) -> list[tuple[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW', 'SYSTEM VIEW') "
                "ORDER BY TABLE_NAME",
                (database,),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def get_all_tables_and_views(self, database: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                (database,),
            )
            return [r[0] for r in cur.fetchall()]

    def get_columns(self, database: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
                    CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
                    COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT,
                    GENERATION_EXPRESSION, COLUMN_KEY
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    AND COLUMN_NAME IS NOT NULL
                ORDER BY ORDINAL_POSITION
                """,
                (database, table),
            )
            columns: list[dict[str, Any]] = []
            for row in cur.fetchall():
                (
                    col_name, col_type, nullable, max_len,
                    precision, scale, col_default, extra, comment,
                    gen_expr, column_key,
                ) = row

                is_identity = "auto_increment" in (extra or "").lower()
                is_generated = bool(gen_expr and gen_expr.strip())
                generated_kind = None
                if is_generated:
                    generated_kind = "VIRTUAL" if "virtual" in (extra or "").lower() else "STORED"

                # Determine if this is a generated column from EXTRA
                if not is_generated and "virtual" in (extra or "").lower():
                    is_generated = True
                    generated_kind = "VIRTUAL"
                if not is_generated and "stored" in (extra or "").lower():
                    is_generated = True
                    generated_kind = "STORED"

                columns.append({
                    "name": col_name,
                    "source_type": col_type,
                    "is_nullable": nullable == "YES",
                    "size": max_len if max_len is not None and max_len != -1 else None,
                    "precision": precision if precision else None,
                    "scale": scale if scale is not None else None,
                    "default": col_default,
                    "is_identity": is_identity,
                    "is_generated": is_generated,
                    "generated_kind": generated_kind,
                    "generated_expression": gen_expr if is_generated else None,
                    "comment": comment or None,
                    "column_key": column_key,
                })
            return columns

    def get_primary_key(self, database: str, table: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX",
                (database, table),
            )
            cols = [r[0] for r in cur.fetchall()]
            if not cols:
                return None
            return {"name": "PRIMARY", "columns": cols}

    def get_unique_constraints(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                "FROM INFORMATION_SCHEMA.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND NON_UNIQUE=0 AND INDEX_NAME <> 'PRIMARY' "
                "GROUP BY INDEX_NAME ORDER BY INDEX_NAME",
                (database,),
            )
            result: list[dict[str, Any]] = []
            for name, cols in cur.fetchall():
                col_list = cols.split(",") if cols else []
                result.append({"name": name, "columns": col_list})
            return result

    def get_unique_constraints_for_table(self, database: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                "FROM INFORMATION_SCHEMA.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND NON_UNIQUE=0 AND INDEX_NAME <> 'PRIMARY' "
                "GROUP BY INDEX_NAME ORDER BY INDEX_NAME",
                (database, table),
            )
            result: list[dict[str, Any]] = []
            for name, cols in cur.fetchall():
                col_list = cols.split(",") if cols else []
                result.append({"name": name, "columns": col_list})
            return result

    def get_check_constraints(self, database: str, table: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
                    ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                    AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                WHERE tc.TABLE_SCHEMA=%s AND tc.TABLE_NAME=%s
                    AND tc.CONSTRAINT_TYPE='CHECK'
                ORDER BY tc.CONSTRAINT_NAME
                """,
                (database, table),
            )
            return [{"name": r[0], "definition": r[1] or ""} for r in cur.fetchall()]

    def get_indexes(self, database: str, table: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.INDEX_NAME,
                    s.NON_UNIQUE,
                    s.COLUMN_NAME,
                    s.INDEX_TYPE,
                    s.COLLATION,
                    s.SUB_PART,
                    s.NULLABLE,
                    s.SEQ_IN_INDEX
                FROM INFORMATION_SCHEMA.STATISTICS s
                WHERE s.TABLE_SCHEMA=%s AND s.TABLE_NAME=%s
                    AND s.INDEX_NAME <> 'PRIMARY'
                ORDER BY s.INDEX_NAME, s.SEQ_IN_INDEX
                """,
                (database, table),
            )
            groups: dict[str, dict[str, Any]] = {}
            for name, non_unique, col, idx_type, collation, sub_part, nullable, seq in cur.fetchall():
                groups.setdefault(
                    name,
                    {
                        "name": name,
                        "is_unique": not bool(non_unique) if non_unique is not None else False,
                        "is_primary": False,
                        "columns": [],
                        "index_type": idx_type,
                        "collation": collation,
                        "sub_part": sub_part,
                        "nullable": nullable,
                    },
                )["columns"].append(col)
            return list(groups.values())

    def get_foreign_keys(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    kcu.CONSTRAINT_NAME,
                    kcu.COLUMN_NAME,
                    kcu.REFERENCED_TABLE_NAME AS ref_table,
                    kcu.REFERENCED_COLUMN_NAME AS ref_column,
                    rc.DELETE_RULE,
                    rc.UPDATE_RULE
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                LEFT JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                    ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
                    AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                WHERE kcu.TABLE_SCHEMA = %s
                    AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
                """,
                (database,),
            )
            fks: dict[str, dict[str, Any]] = {}
            for row in cur.fetchall():
                (
                    name, col, ref_table, ref_col,
                    on_delete, on_update,
                ) = row
                fk = fks.setdefault(name, {
                    "name": name,
                    "columns": [],
                    "ref_table": ref_table,
                    "ref_columns": [],
                    "on_delete": on_delete or "RESTRICT",
                    "on_update": on_update or "RESTRICT",
                })
                fk["columns"].append(col)
                fk["ref_columns"].append(ref_col)
            return list(fks.values())

    def get_views(self, database: str) -> list[dict[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, VIEW_DEFINITION FROM INFORMATION_SCHEMA.VIEWS "
                "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                (database,),
            )
            return [{"name": r[0], "definition": r[1] or ""} for r in cur.fetchall()]

    def get_functions(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM INFORMATION_SCHEMA.ROUTINES "
                "WHERE ROUTINE_SCHEMA=%s AND ROUTINE_TYPE='FUNCTION' "
                "ORDER BY ROUTINE_NAME",
                (database,),
            )
            return [{"name": r[0], "kind": r[1].lower()} for r in cur.fetchall()]

    def get_procedures(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM INFORMATION_SCHEMA.ROUTINES "
                "WHERE ROUTINE_SCHEMA=%s AND ROUTINE_TYPE='PROCEDURE' "
                "ORDER BY ROUTINE_NAME",
                (database,),
            )
            return [{"name": r[0], "kind": r[1].lower()} for r in cur.fetchall()]

    def get_routines(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION, CREATED, LAST_ALTERED, SQL_DATA_ACCESS "
                "FROM INFORMATION_SCHEMA.ROUTINES "
                "WHERE ROUTINE_SCHEMA=%s "
                "ORDER BY ROUTINE_NAME",
                (database,),
            )
            return [
                {
                    "name": r[0],
                    "kind": r[1].lower(),
                    "definition": r[2] or "",
                    "created": r[3],
                    "last_altered": r[4],
                    "sql_data_access": r[5],
                }
                for r in cur.fetchall()
            ]

    def get_triggers(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, "
                "EVENT_MANIPULATION "
                "FROM INFORMATION_SCHEMA.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=%s "
                "ORDER BY TRIGGER_NAME",
                (database,),
            )
            return [
                {
                    "name": r[0],
                    "table": r[1],
                    "timing": r[2],
                    "event": r[3],
                    "is_disabled": False,
                    "granularity": "ROW",
                }
                for r in cur.fetchall()
            ]

    def get_partitions(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, PARTITION_NAME, PARTITION_METHOD,
                       PARTITION_DESCRIPTION, TABLE_ROWS
                FROM INFORMATION_SCHEMA.PARTITIONS
                WHERE TABLE_SCHEMA=%s AND PARTITION_NAME IS NOT NULL
                ORDER BY TABLE_NAME, PARTITION_ORDINAL_POSITION
                """,
                (database,),
            )
            results: list[dict[str, Any]] = []
            for row in cur.fetchall():
                table_name, partition_name, method, description, table_rows = row
                results.append({
                    "table": table_name,
                    "partition_name": partition_name,
                    "method": method or "",
                    "description": description or "",
                    "table_rows": table_rows,
                })
            return results

    def get_partitioned_tables(self, database: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, PARTITION_METHOD
                FROM INFORMATION_SCHEMA.PARTITIONS
                WHERE TABLE_SCHEMA=%s AND PARTITION_NAME IS NOT NULL
                    AND PARTITION_METHOD IS NOT NULL
                GROUP BY TABLE_NAME, PARTITION_METHOD
                ORDER BY TABLE_NAME
                """,
                (database,),
            )
            return [
                {"table": r[0], "method": r[1] or ""}
                for r in cur.fetchall()
            ]

    def get_comments(self, database: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' "
                "AND TABLE_COMMENT <> '' "
                "ORDER BY TABLE_NAME",
                (database,),
            )
            for table_name, comment in cur.fetchall():
                result.append({
                    "object_type": "TABLE",
                    "schema_name": database,
                    "object_name": table_name,
                    "comment": comment,
                })

            cur.execute(
                """
                SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_COMMENT
                FROM INFORMATION_SCHEMA.COLUMNS c
                JOIN INFORMATION_SCHEMA.TABLES t
                    ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
                WHERE c.TABLE_SCHEMA=%s AND t.TABLE_TYPE='BASE TABLE'
                    AND c.COLUMN_COMMENT <> ''
                ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
                """,
                (database,),
            )
            for table_name, col_name, comment in cur.fetchall():
                result.append({
                    "object_type": "COLUMN",
                    "schema_name": database,
                    "object_name": f"{table_name}.{col_name}",
                    "comment": comment,
                })
        return result

    def get_row_count(self, database: str, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_q(database)}.{_q(table)}")
            return cur.fetchone()[0]

    def get_server_version(self) -> str:
        with self._conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            row = cur.fetchone()
            return row[0] if row else "unknown"

    def get_database(self) -> str:
        return self.database_name
