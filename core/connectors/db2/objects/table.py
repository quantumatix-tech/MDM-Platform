from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import Column, Schema, validate_identifier


def discover_tables(conn: Any, config: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    if conn is None:
        raise RuntimeError("Not connected")
    tables: list[tuple[str, str]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT TABSCHEMA, TABNAME
            FROM SYSCAT.TABLES
            WHERE TYPE = 'T'
              AND TABSCHEMA NOT IN ('SYSIBM','SYSCAT','SYSSTAT','SYSTOOLS')
              AND TABSCHEMA NOT LIKE 'SYS%'
            ORDER BY TABSCHEMA, TABNAME
            """
        )
        for row in cur.fetchall():
            if row and len(row) >= 2 and row[0] and row[1]:
                tables.append((str(row[0]).strip(), str(row[1]).strip()))
    return tables


def discover_schemas(conn: Any) -> list[str]:
    if conn is None:
        raise RuntimeError("Not connected")
    schemas: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT SCHEMANAME
            FROM SYSCAT.SCHEMATA
            WHERE SCHEMANAME NOT IN ('SYSIBM','SYSCAT','SYSSTAT','SYSTOOLS')
              AND SCHEMANAME NOT LIKE 'SYS%'
            ORDER BY SCHEMANAME
            """
        )
        for row in cur.fetchall():
            if row and row[0]:
                schemas.append(str(row[0]).strip())
    return schemas


def _qualify_table(schema_name: str | None, table_name: str) -> str:
    if schema_name:
        return f'"{schema_name}"."{table_name}"'
    return f'"{table_name}"'


def get_row_count(conn: Any, object_name: str, schema_name: str | None = None) -> int:
    if isinstance(object_name, str) and "." in object_name:
        parts = object_name.split(".", 1)
        if schema_name is None:
            schema_name = parts[0].strip().strip('"')
        object_name = parts[1].strip().strip('"')
    validate_identifier(object_name, "table")
    if schema_name:
        validate_identifier(schema_name, "schema")
    qual = _qualify_table(schema_name, object_name)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {qual}")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


def export_table_data(conn: Any, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
    validate_identifier(object_name, "table")
    if schema_name:
        validate_identifier(schema_name, "schema")
    qual = _qualify_table(schema_name, object_name)
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT * FROM {qual}")
        columns = [col[0] for col in cur.description] if cur.description else []
        for row in cur:
            yield dict(zip(columns, row))
    finally:
        try:
            cur.close()
        except Exception:
            pass


def get_schema_for_table(conn: Any, object_name: str, schema_name: str | None = None) -> Schema:
    validate_identifier(object_name, "table")
    columns: list[Column] = []
    primary_key: list[str] = []
    table_schema_resolved = schema_name

    with conn.cursor() as cur:
        if table_schema_resolved is None:
            cur.execute(
                """
                SELECT TABSCHEMA
                FROM SYSCAT.TABLES
                WHERE TABNAME = ? AND TYPE = 'T'
                  AND TABSCHEMA NOT IN ('SYSIBM','SYSCAT','SYSSTAT','SYSTOOLS')
                  AND TABSCHEMA NOT LIKE 'SYS%'
                ORDER BY TABSCHEMA
                """,
                (object_name,),
            )
            rows = cur.fetchall()
            if not rows:
                raise ValueError(f"Table {object_name!r} not found in user schemas")
            if len(rows) > 1:
                schemas_found = [str(r[0]).strip() for r in rows if r and r[0]]
                raise ValueError(
                    f"Ambiguous table name {object_name!r} found in multiple schemas: {schemas_found}. "
                    "Specify schema_name to disambiguate."
                )
            table_schema_resolved = str(rows[0][0]).strip()

        cur.execute(
            """
            SELECT COLNAME, TYPENAME, TYPESCHEMA, NULLS, LENGTH, SCALE, COLNO, GENERATED, IDENTITY, CAST(TEXT AS VARCHAR(32672)) AS TEXT
            FROM SYSCAT.COLUMNS
            WHERE TABNAME = ? AND TABSCHEMA = ?
            ORDER BY COLNO
            """,
            (object_name, table_schema_resolved),
        )
        col_rows = cur.fetchall()

        cur.execute(
            """
            SELECT COLNAME, CAST(DEFAULT AS VARCHAR(32672))
            FROM SYSCAT.COLUMNS
            WHERE TABNAME = ? AND TABSCHEMA = ?
            """,
            (object_name, table_schema_resolved),
        )
        default_map = {}
        for drow in cur.fetchall():
            if drow and drow[0]:
                dval = drow[1]
                default_map[str(drow[0]).strip()] = None if dval is None else str(dval).strip()

        for row in col_rows:
            if not row:
                continue
            colname = str(row[0]).strip() if row[0] else ""
            typename = str(row[1]).strip() if row[1] else ""
            typeschema = str(row[2]).strip() if row[2] else ""
            if typeschema and typeschema.upper() not in ("SYSIBM", "SYSCAT"):
                typename = f"{typeschema}.{typename}"
            nulls = str(row[3]).strip() if len(row) > 3 and row[3] else "Y"
            length = row[4] if len(row) > 4 else None
            scale = row[5] if len(row) > 5 else None
            generated = str(row[7]).strip() if len(row) > 7 and row[7] is not None else " "
            identity = str(row[8]).strip() if len(row) > 8 and row[8] is not None else "N"
            raw_gen = None if len(row) <= 9 or row[9] is None else str(row[9]).strip()
            if raw_gen and raw_gen.upper().startswith("AS "):
                raw_gen = raw_gen[3:].strip()
            gen_expr = raw_gen
            nullable = (nulls == "Y")
            source_type = typename
            if length is not None and typename.upper() in ("VARCHAR", "CHAR", "VARGRAPHIC", "GRAPHIC", "CLOB", "BLOB"):
                try:
                    source_type = f"{typename}({length})"
                except Exception:
                    source_type = typename
            elif scale is not None and typename.upper() in ("DECIMAL", "NUMERIC"):
                try:
                    if length is not None:
                        source_type = f"{typename}({length},{scale})"
                    else:
                        source_type = typename
                except Exception:
                    source_type = typename
            columns.append(
                Column(
                    name=colname,
                    source_type=source_type,
                    target_type=None,
                    nullable=nullable,
                    default=default_map.get(colname),
                    generated=gen_expr if generated in ("A", "D") and gen_expr else None,
                    is_computed=generated in ("A", "D") and bool(gen_expr),
                    computed_definition=gen_expr if (generated in ("A", "D") and gen_expr) else None,
                    is_identity=(identity == "Y"),
                    identity_kind=("ALWAYS" if (identity == "Y" and generated == "A") else "BY DEFAULT" if (identity == "Y") else None),
                    size=length if isinstance(length, int) else None,
                    precision=length if isinstance(length, int) and typename.upper() in ("DECIMAL", "NUMERIC") else None,
                    scale=scale if isinstance(scale, int) else None,
                )
            )

        try:
            cur.execute(
                """
                SELECT K.COLNAME
                FROM SYSCAT.KEYCOLUSE K
                JOIN SYSCAT.TABCONST C ON C.CONSTNAME = K.CONSTNAME AND C.TABSCHEMA = K.TABSCHEMA AND C.TABNAME = K.TABNAME
                WHERE K.TABNAME = ? AND K.TABSCHEMA = ? AND C.TYPE = 'P'
                ORDER BY K.COLSEQ
                """,
                (object_name, table_schema_resolved),
            )
            for row in cur.fetchall():
                if row and row[0]:
                    primary_key.append(str(row[0]).strip())
        except Exception:
            primary_key = []

    from .constraint import discover_unique_constraints
    from .index import discover_indexes
    try:
        ucs = discover_unique_constraints(conn, table_schema_resolved, object_name)
    except Exception as exc:
        raise RuntimeError(f"Db2 unique constraint discovery failed for {table_schema_resolved}.{object_name}: {exc}") from exc
    try:
        idxs = discover_indexes(conn, table_schema_resolved, object_name)
    except Exception as exc:
        raise RuntimeError(f"Db2 index discovery failed for {table_schema_resolved}.{object_name}: {exc}") from exc
    try:
        from .foreign_key import discover_foreign_keys
        fks = discover_foreign_keys(conn, table_schema_resolved, object_name)
    except Exception as exc:
        raise RuntimeError(f"Db2 foreign key discovery failed for {table_schema_resolved}.{object_name}: {exc}") from exc
    return Schema(
        name=object_name,
        schema_name=table_schema_resolved,
        columns=columns,
        primary_key=primary_key,
        indexes=idxs,
        unique_constraints=ucs,
        foreign_keys=fks,
    )
