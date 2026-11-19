from __future__ import annotations

from typing import Any

from core.connectors.base import Column, Schema, validate_identifier


def _quote_ident(name: str) -> str:
    if not name:
        return name
    # Db2 typically uses double quotes for delimited identifiers
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _db2_type_for_column(col: Column) -> str:
    stype = (col.source_type or '').strip().upper()
    # If source_type already looks formatted, prefer as-is but quote if needed
    if col.is_identity:
        base = stype.split('(')[0] if '(' in stype else stype
        if not base:
            base = 'INTEGER'
        kind = (col.identity_kind or "BY DEFAULT").upper()
        return f"{base} GENERATED {kind} AS IDENTITY"
    if col.is_computed or col.generated:
        base = stype.split('(')[0] if '(' in stype else stype
        if not base:
            base = 'INTEGER'
        expr = col.generated or col.computed_definition or col.computed_definition
        return f"{base} GENERATED ALWAYS AS ({expr})"
    # Try to preserve common types
    if 'VARCHAR' in stype and col.size and '(' not in stype:
        return f"VARCHAR({col.size})"
    if 'CHAR' in stype and col.size and '(' not in stype and 'VARCHAR' not in stype:
        return f"CHAR({col.size})"
    if stype in ('DECIMAL', 'NUMERIC') and col.precision is not None and col.scale is not None and '(' not in stype:
        return f"{stype}({col.precision},{col.scale})"
    if stype in ('DECIMAL', 'NUMERIC') and col.size is not None and col.scale is not None and '(' not in stype:
        return f"{stype}({col.size},{col.scale})"
    if stype == '' and col.size:
        return f"VARCHAR({col.size})"
    return stype or 'VARCHAR(255)'


def build_create_table_sql(schema: Schema) -> str:
    validate_identifier(schema.name, "table")
    schema_name = schema.schema_name
    if not schema_name:
        raise ValueError(f"schema_name required for table {schema.name!r}; no default schema assumed")
    validate_identifier(schema_name, "schema")
    cols_sql = []
    for col in schema.columns:
        cname = _quote_ident(col.name)
        ctype = _db2_type_for_column(col)
        col_def = f"  {cname} {ctype}"
        if not col.nullable:
            col_def += " NOT NULL"
        # Add DEFAULT clause for non-identity, non-generated columns
        if col.default is not None and not col.is_identity and not col.is_computed:
            col_def += f" DEFAULT {col.default}"
        cols_sql.append(col_def)
    pk = schema.primary_key or []
    if pk:
        pk_cols = ', '.join(_quote_ident(c) for c in pk)
        cols_sql.append(f"  PRIMARY KEY ({pk_cols})")
    table = f'{_quote_ident(schema_name)}.{_quote_ident(schema.name)}'
    return f"CREATE TABLE {table} (\n" + ',\n'.join(cols_sql) + "\n)"


def _escape_sql_str(value) -> str:
    """Escape a boundary value for safe use inside a Db2 single-quoted string literal."""
    return str(value).replace("'", "''")


def _partition_inclusive(flag: str) -> str:
    f = (flag or "").strip().upper()
    if f in ("Y", "I", "B"):
        return "INCLUSIVE"
    if f in ("N", "E"):
        return "EXCLUSIVE"
    return "INCLUSIVE"


def build_create_partitioned_table_sql(schema: Schema, partitions: list[dict], partition_column: str | list[str]) -> str:
    """Build CREATE TABLE ... PARTITION BY RANGE DDL for a range-partitioned table.

    Never silently omits the partition key: raises when no partition column or
    no partitions are supplied. Every partition must declare at least one of
    STARTING FROM / ENDING AT; a completely unbounded partition is invalid in
    Db2 LUW and causes a fail-closed RuntimeError.
    """
    validate_identifier(schema.name, "table")
    schema_name = schema.schema_name
    if not schema_name:
        raise ValueError(f"schema_name required for partitioned table {schema.name!r}")
    validate_identifier(schema_name, "schema")
    if not partitions:
        raise RuntimeError(f"Cannot create partitioned table {schema_name}.{schema.name} without partition definitions")
    cols = partition_column if isinstance(partition_column, list) else [partition_column]
    for c in cols:
        validate_identifier(c, "column")
    col_list = ", ".join(_quote_ident(c) for c in cols)
    cols_sql = []
    for col in schema.columns:
        cname = _quote_ident(col.name)
        ctype = _db2_type_for_column(col)
        col_def = f"  {cname} {ctype}"
        if not col.nullable:
            col_def += " NOT NULL"
        # Add DEFAULT clause for non-identity, non-generated columns
        if col.default is not None and not col.is_identity and not col.is_computed:
            col_def += f" DEFAULT {col.default}"
        cols_sql.append(col_def)
    pk = schema.primary_key or []
    if pk:
        pk_cols = ', '.join(_quote_ident(c) for c in pk)
        cols_sql.append(f"  PRIMARY KEY ({pk_cols})")
    part_sql = []
    for p in partitions:
        pname = str(p.get("name", "")).strip()
        low = p.get("low")
        high = p.get("high")
        low_inc = _partition_inclusive(p.get("low_inclusive"))
        high_inc = _partition_inclusive(p.get("high_inclusive"))
        if not pname:
            raise RuntimeError("Partition definition missing name")
        validate_identifier(pname, "partition")
        if low is None and high is None:
            raise RuntimeError(
                f"Partition {pname} has no STARTING FROM or ENDING AT boundary; "
                "cannot create an unbounded range partition"
            )
        start = f"STARTING FROM ('{_escape_sql_str(low)}') {low_inc}" if low is not None else ""
        end = f"ENDING AT ('{_escape_sql_str(high)}') {high_inc}" if high is not None else ""
        part_sql.append(f"  PARTITION {_quote_ident(pname)} {start} {end}".strip())
    table = f'{_quote_ident(schema_name)}.{_quote_ident(schema.name)}'
    return (
        f"CREATE TABLE {table} (\n" + ',\n'.join(cols_sql) + "\n)\n"
        f"PARTITION BY RANGE ({col_list})\n(\n" + ',\n'.join(part_sql) + "\n)"
    )


def table_exists(conn: Any, schema_name: str, table_name: str) -> bool:
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = ? AND TYPE = 'T'", (schema_name.strip(), table_name.strip()))
            rows = cur.fetchall()
            return bool(rows)
    except Exception:
        raise


def create_table_if_missing(conn: Any, schema: Schema) -> None:
    schema_name = schema.schema_name
    if not schema_name:
        raise ValueError(f"schema_name required for table {schema.name!r}; no default schema assumed")
    if table_exists(conn, schema_name, schema.name):
        return  # do not overwrite
    sql = build_create_table_sql(schema)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    except Exception:
        raise


def create_partitioned_table_if_missing(conn: Any, schema: Schema, partitions: list[dict], partition_column: str | list[str]) -> None:
    schema_name = schema.schema_name
    if not schema_name:
        raise ValueError(f"schema_name required for partitioned table {schema.name!r}; no default schema assumed")
    if table_exists(conn, schema_name, schema.name):
        return  # do not overwrite
    sql = build_create_partitioned_table_sql(schema, partitions, partition_column)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    except Exception:
        raise
