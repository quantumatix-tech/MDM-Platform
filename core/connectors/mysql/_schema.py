"""MySQL schema inspection and composition — engine-neutral shared logic.

This module holds the catalog extraction that assembles the cross-engine
``Schema`` DTO for a MySQL table. It is deliberately connector-agnostic: it
depends only on a live connection and a database name, so both the source
connector (``MySQLSourceConnector.get_schema``) and the target/table code
(``MySQLTargetConnector.inspect_schema`` and target partition verification
during reconciliation) can use it without importing each other.

It imports neither ``source`` nor ``target`` nor ``cdc``, so it is safe for
any module in the package to depend on.

The caller is responsible for validating ``object_name`` before calling
:func:`inspect_schema`; that keeps identifier validation at the public API
boundary exactly as it was when this logic lived on the source connector.
"""
from __future__ import annotations

from core.connectors.base import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Schema,
)

from core.connectors.mysql.objects import partition


def inspect_schema(conn, database: str, object_name: str) -> Schema:
    """Read MySQL catalog metadata for ``object_name`` and compose a ``Schema``.

    All SQL and metadata behaviour is carried over unchanged from the former
    ``MySQLSourceConnector.get_schema`` implementation.
    """
    db = database
    with conn.cursor(buffered=True) as cur:
        cur.execute("SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,CHARACTER_MAXIMUM_LENGTH,COLUMN_DEFAULT,EXTRA,GENERATION_EXPRESSION,COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (db, object_name))
        columns = [Column(name=n, source_type=t, nullable=(nullable == "YES"), size=size,
            default=default, generated=generated or None,
            generated_kind=("STORED" if "STORED GENERATED" in (extra or "") else "VIRTUAL") if generated else None,
            auto_increment="auto_increment" in (extra or "").lower(), comment=comment or None)
            for n, t, nullable, size, default, extra, generated, comment in cur.fetchall()]
        cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION", (db, object_name))
        primary_key = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT INDEX_NAME,NON_UNIQUE,COLUMN_NAME,INDEX_TYPE FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME<>'PRIMARY' ORDER BY INDEX_NAME,SEQ_IN_INDEX", (db, object_name))
        index_map: dict[str, Index] = {}
        for name, non_unique, column, index_type in cur.fetchall():
            index_map.setdefault(
                name,
                Index(name=name, columns=[], unique=not bool(non_unique), index_type=index_type),
            ).columns.append(column)
        cur.execute("SELECT k.CONSTRAINT_NAME,k.COLUMN_NAME,k.REFERENCED_TABLE_SCHEMA,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,r.UPDATE_RULE,r.DELETE_RULE FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA=%s AND k.TABLE_NAME=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL ORDER BY k.CONSTRAINT_NAME,k.ORDINAL_POSITION", (db, object_name))
        fk_map: dict[str, ForeignKey] = {}
        for name, col, ref_schema, ref_table, ref_col, on_update, on_delete in cur.fetchall():
            fk = fk_map.setdefault(name, ForeignKey(name=name, columns=[], ref_table=ref_table, ref_columns=[], ref_schema=ref_schema, on_update=on_update, on_delete=on_delete))
            fk.columns.append(col); fk.ref_columns.append(ref_col)
        cur.execute("SELECT tc.CONSTRAINT_NAME,cc.CHECK_CLAUSE FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA=%s AND tc.TABLE_NAME=%s AND tc.CONSTRAINT_TYPE='CHECK'", (db, object_name))
        # INFORMATION_SCHEMA returns escaped character-set string literals
        # on this MySQL build; DDL requires the unescaped form.
        checks = [CheckConstraint(name=n, expression=e.replace("\\'", "'")) for n, e in cur.fetchall()]
        cur.execute("SELECT TABLE_COMMENT,ENGINE,TABLE_COLLATION FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, object_name))
        comment, engine, collation = cur.fetchone()
        partition_metadata = partition.discover_partitions(cur, db, object_name)

    return Schema(name=object_name, columns=columns, primary_key=primary_key, indexes=list(index_map.values()), foreign_keys=list(fk_map.values()), check_constraints=checks, partition_method=partition_metadata.method, partition_expression=partition_metadata.expression, partitions=partition_metadata.partitions, comment=comment or None, options={"engine": engine, "collation": collation})
