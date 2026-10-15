"""MSSQL Sequence object implementation.

Reusable Sequence-specific logic for source discovery and target creation.
Functions accept an explicit *conn* parameter so that neither
``MSSQLSourceConnector`` nor ``MSSQLTargetConnector`` is imported,
avoiding circular dependencies.
"""
from __future__ import annotations

from typing import Any

from core.connectors.base import (
    SequenceDef,
    quote_identifier,
    validate_identifier,
)
from core.connectors.mssql._models import (
    _qualify,
    _resolve_mssql_schemas,
)
from core.audit_logger import audit_log


# ============================================================================
# SOURCE-SIDE SEQUENCE OPERATIONS
# Called/delegated by MSSQLSourceConnector.list_all_sequences.
# Discovery of user sequences from the source database.
# ============================================================================

def discover_sequences(conn: Any, config: dict[str, Any]) -> list[SequenceDef]:
    """Discover user sequences in the configured schemas.

    Replaces ``MSSQLSourceConnector.list_all_sequences``.

    SQL Server exposes sequence metadata via ``sys.sequences`` joined to
    ``sys.schemas`` for the schema name, and ``sys.sql_modules``-like
    attributes (start, increment, min, max, cycle, cache, current value).
    """
    schemas = _resolve_mssql_schemas(config)
    results: list[SequenceDef] = []
    with conn.cursor() as cur:
        cur.execute("SELECT TRY_CONVERT(int, SERVERPROPERTY('ProductMajorVersion'))")
        version_row = cur.fetchone()
        product_major = version_row[0] if version_row else None
        last_used_sql = (
            "CAST(s.last_used_value AS BIGINT)"
            if isinstance(product_major, int) and product_major >= 14
            else "CAST(NULL AS BIGINT)"
        )
        if schemas:
            placeholders = ", ".join("?" for _ in schemas)
            cur.execute(
                "SELECT s.name, sch.name, "
                "CAST(TYPE_NAME(s.user_type_id) AS NVARCHAR(128)) AS sequence_type, "
                "CAST(s.start_value AS BIGINT) AS start_value, "
                "CAST(s.increment AS BIGINT) AS increment, "
                "CAST(s.minimum_value AS BIGINT) AS minimum_value, "
                "CAST(s.maximum_value AS BIGINT) AS maximum_value, "
                "s.is_cycling, "
                "CAST(s.cache_size AS BIGINT) AS cache_size, "
                "CAST(s.current_value AS BIGINT) AS current_value, "
                "s.is_cached, " + last_used_sql + " AS last_used_value, "
                "s.is_exhausted "
                "FROM sys.sequences AS s "
                "JOIN sys.schemas AS sch ON sch.schema_id = s.schema_id "
                f"WHERE sch.name IN ({placeholders}) "
                "ORDER BY sch.name, s.name",
                list(schemas),
            )
        else:
            cur.execute(
                "SELECT s.name, sch.name, "
                "CAST(TYPE_NAME(s.user_type_id) AS NVARCHAR(128)) AS sequence_type, "
                "CAST(s.start_value AS BIGINT) AS start_value, "
                "CAST(s.increment AS BIGINT) AS increment, "
                "CAST(s.minimum_value AS BIGINT) AS minimum_value, "
                "CAST(s.maximum_value AS BIGINT) AS maximum_value, "
                "s.is_cycling, "
                "CAST(s.cache_size AS BIGINT) AS cache_size, "
                "CAST(s.current_value AS BIGINT) AS current_value, "
                "s.is_cached, " + last_used_sql + " AS last_used_value, "
                "s.is_exhausted "
                "FROM sys.sequences AS s "
                "JOIN sys.schemas AS sch ON sch.schema_id = s.schema_id "
                "WHERE sch.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "ORDER BY sch.name, s.name"
            )

        for row in cur.fetchall():
            (
                seq_name,
                seq_schema,
                seq_type,
                start_value,
                increment,
                minimum_value,
                maximum_value,
                is_cycling,
                cache_size,
                current_value,
                is_cached,
                last_used_value,
                is_exhausted,
            ) = row
            validate_identifier(seq_name, "sequence")
            validate_identifier(seq_schema, "schema")
            results.append(
                SequenceDef(
                    name=seq_name,
                    schema=seq_schema,
                    start_value=int(start_value),
                    increment=int(increment),
                    min_value=int(minimum_value),
                    max_value=int(maximum_value),
                    cycle=bool(is_cycling),
                    last_value=(
                        int(current_value) if current_value is not None else None
                    ),
                    owned_by=None,
                    data_type=seq_type,
                    cache_size=int(cache_size) if cache_size is not None else 1,
                    is_cached=bool(is_cached),
                    last_used_value=(
                        int(last_used_value) if last_used_value is not None else None
                    ),
                    last_used_value_available=(
                        isinstance(product_major, int) and product_major >= 14
                    ),
                    is_exhausted=bool(is_exhausted),
                )
            )
    return results


# ============================================================================
# TARGET-SIDE SEQUENCE OPERATIONS
# Called/delegated by MSSQLTargetConnector.create_sequence.
# Sequence creation on the target database.
# ============================================================================

def _sequence_definition(seq: SequenceDef) -> dict[str, Any]:
    return {
        "data_type": (seq.data_type or "bigint").lower(),
        "start_value": int(seq.start_value),
        "increment": int(seq.increment),
        "minimum_value": int(seq.min_value),
        "maximum_value": int(seq.max_value),
        "cycle": bool(seq.cycle),
        "is_cached": bool(seq.is_cached),
        "cache_size": int(seq.cache_size) if seq.is_cached else None,
    }


def _sequence_next_value(seq: SequenceDef) -> int | None:
    """Return the next source value without consuming the source sequence."""
    if seq.increment == 0 or seq.min_value > seq.max_value:
        raise RuntimeError(f"Invalid source sequence definition for {seq.schema}.{seq.name}")
    if seq.is_exhausted:
        return None
    if seq.last_used_value_available:
        last_used = seq.last_used_value
        if last_used is None:
            return int(seq.start_value)
    else:
        last_used = seq.last_value
        if last_used is None:
            return int(seq.start_value)
        if int(last_used) == int(seq.start_value):
            raise RuntimeError(
                f"Cannot safely determine next value for {seq.schema}.{seq.name}: "
                "this SQL Server version does not expose last_used_value and the current value "
                "equals START WITH, so the sequence may be unused or may have emitted its first value."
            )
    candidate = int(last_used) + int(seq.increment)
    if seq.min_value <= candidate <= seq.max_value:
        return candidate
    if seq.cycle:
        return int(seq.min_value if seq.increment > 0 else seq.max_value)
    return None


def _create_sequence_ddl(cur: Any, seq: SequenceDef, qname: str) -> None:
    cache_clause = "NO CACHE" if not seq.is_cached else f"CACHE {int(seq.cache_size)}"
    cur.execute(
        f"CREATE SEQUENCE {qname} "
        f"AS {(seq.data_type or 'bigint').upper()} "
        f"START WITH {int(seq.start_value)} "
        f"INCREMENT BY {int(seq.increment)} "
        f"MINVALUE {int(seq.min_value)} "
        f"MAXVALUE {int(seq.max_value)} "
        f"{'CYCLE' if seq.cycle else 'NO CYCLE'} {cache_clause}"
    )


def _sequence_alter_options(seq: SequenceDef) -> str:
    cache_clause = "NO CACHE" if not seq.is_cached else f"CACHE {int(seq.cache_size)}"
    return (
        f"INCREMENT BY {int(seq.increment)} MINVALUE {int(seq.min_value)} "
        f"MAXVALUE {int(seq.max_value)} {'CYCLE' if seq.cycle else 'NO CYCLE'} {cache_clause}"
    )


def _sequence_replay_count(seq: SequenceDef, last_used: int) -> int:
    """Count values to emit from START WITH to reproduce last_used_value.

    SQL Server changes sys.sequences.start_value when ALTER SEQUENCE RESTART
    is used. Replaying the source's consumed values preserves both the source
    START WITH metadata and its runtime state. Bound the work rather than
    silently leaving the target in a different state for very distant values.
    """
    value = int(seq.start_value)
    target = int(last_used)
    for count in range(1, 100_001):
        if value == target:
            return count
        candidate = value + int(seq.increment)
        if seq.min_value <= candidate <= seq.max_value:
            value = candidate
        elif seq.cycle:
            value = int(seq.min_value if seq.increment > 0 else seq.max_value)
        else:
            break
    raise RuntimeError(
        f"Cannot reproduce source runtime state for {seq.schema}.{seq.name}: "
        f"last-used value {target} is unreachable from START WITH {seq.start_value} "
        "within the safe 100000-value replay limit"
    )


def _set_sequence_runtime_state(
    cur: Any, seq: SequenceDef, qname: str, expected_next: int | None,
) -> None:
    if seq.last_used_value_available:
        last_used = seq.last_used_value
    else:
        # Older SQL Server releases lack last_used_value. A non-START current
        # value is usable as the last emitted value; START itself is ambiguous.
        last_used = seq.last_value
        if last_used is not None and int(last_used) == int(seq.start_value):
            raise RuntimeError(
                f"Cannot safely reconcile runtime state for {qname}: this SQL Server version "
                "does not expose last_used_value and current_value equals START WITH"
            )
    if seq.is_exhausted and not seq.cycle:
        # Replaying through the source's final emitted boundary value makes the
        # target exhausted without changing its configured START WITH value.
        if last_used is None:
            raise RuntimeError(f"Cannot reproduce exhausted source state for {qname}")

    cur.execute(
        f"ALTER SEQUENCE {qname} RESTART WITH {int(seq.start_value)} "
        f"{_sequence_alter_options(seq)}"
    )
    if last_used is None:
        if expected_next != int(seq.start_value):
            raise RuntimeError(
                f"Source sequence {qname} is unused but expected next value {expected_next} "
                f"does not equal START WITH {seq.start_value}"
            )
        return

    replay_count = _sequence_replay_count(seq, int(last_used))
    if replay_count > 1:
        cur.execute(
            "DECLARE @replay_count BIGINT = ?; DECLARE @replayed BIGINT = 0; "
            "DECLARE @last_value BIGINT; "
            f"WHILE @replayed < @replay_count BEGIN SELECT @last_value = NEXT VALUE FOR {qname}; "
            "SET @replayed += 1; END; SELECT @last_value",
            (replay_count,),
        )
    else:
        cur.execute(f"SELECT NEXT VALUE FOR {qname}")
    emitted = cur.fetchone()
    if emitted is None or int(emitted[0]) != int(last_used):
        raise RuntimeError(
            f"Could not reproduce runtime state for {qname}: expected last-used value {last_used}, "
            f"got {None if emitted is None else emitted[0]}"
        )


def _sequence_recreation_blockers(
    cur: Any, object_id: int, schema: str, name: str,
) -> list[str]:
    """List catalog-known consumers before DROP/CREATE of a sequence."""
    blockers: list[str] = []
    cur.execute(
        "SELECT OBJECT_SCHEMA_NAME(d.referencing_id), OBJECT_NAME(d.referencing_id), "
        "COALESCE(o.type_desc, 'DATABASE_OBJECT') "
        "FROM sys.sql_expression_dependencies d "
        "LEFT JOIN sys.objects o ON o.object_id=d.referencing_id "
        "WHERE d.referenced_id=? ORDER BY 1,2",
        (object_id,),
    )
    for row in cur.fetchall():
        blockers.append(f"SQL dependency {row[0]}.{row[1]} ({row[2]})")

    names = (name, f"{schema}.{name}", f"[{schema}].[{name}]")
    cur.execute(
        "SELECT OBJECT_SCHEMA_NAME(m.object_id), OBJECT_NAME(m.object_id), o.type_desc "
        "FROM sys.sql_modules m JOIN sys.objects o ON o.object_id=m.object_id "
        "WHERE m.object_id<>? AND (CHARINDEX(?,m.definition)>0 OR CHARINDEX(?,m.definition)>0 "
        "OR CHARINDEX(?,m.definition)>0) ORDER BY 1,2",
        (object_id, *names),
    )
    for row in cur.fetchall():
        blockers.append(f"SQL text/dynamic reference {row[0]}.{row[1]} ({row[2]})")

    cur.execute(
        "SELECT s.name, t.name, dc.name FROM sys.default_constraints dc "
        "JOIN sys.tables t ON t.object_id=dc.parent_object_id "
        "JOIN sys.schemas s ON s.schema_id=t.schema_id "
        "WHERE CHARINDEX(?,dc.definition)>0 OR CHARINDEX(?,dc.definition)>0 "
        "OR CHARINDEX(?,dc.definition)>0 ORDER BY 1,2,3",
        names,
    )
    for row in cur.fetchall():
        blockers.append(f"default constraint {row[0]}.{row[1]}.{row[2]}")

    cur.execute(
        "SELECT s.name, sy.name FROM sys.synonyms sy "
        "JOIN sys.schemas s ON s.schema_id=sy.schema_id "
        "WHERE CHARINDEX(?,sy.base_object_name)>0 OR CHARINDEX(?,sy.base_object_name)>0 "
        "OR CHARINDEX(?,sy.base_object_name)>0 ORDER BY 1,2",
        names,
    )
    for row in cur.fetchall():
        blockers.append(f"synonym {row[0]}.{row[1]}")

    try:
        cur.execute(
            "SELECT j.name, st.step_name FROM msdb.dbo.sysjobsteps st "
            "JOIN msdb.dbo.sysjobs j ON j.job_id=st.job_id "
            "WHERE CHARINDEX(?,st.command)>0 OR CHARINDEX(?,st.command)>0 "
            "OR CHARINDEX(?,st.command)>0 ORDER BY 1,2",
            names,
        )
        for row in cur.fetchall():
            blockers.append(f"SQL Agent job step {row[0]}.{row[1]}")
    except Exception as exc:
        blockers.append(f"could not inspect SQL Agent job references ({exc})")
    return blockers

def create_sequence(conn: Any, seq: SequenceDef) -> None:
    """Create a schema-qualified SQL Server sequence with source metadata.

    Replaces ``MSSQLTargetConnector.create_sequence``.

    Uses ``CREATE SEQUENCE`` with all source metadata (start, increment,
    min, max, cycle, cache, data type). Schema creation is not committed
    separately: CREATE SCHEMA is not the start of a new batch and can be
    followed by additional statements in the same transaction.

    Existing source-managed sequences are reconciled to the source
    definition and next-value state. A sequence is recreated only when its
    type or original START WITH metadata cannot be changed by ALTER SEQUENCE;
    recreation is rejected when catalog or known SQL Agent dependencies exist.
    """
    seq_schema = seq.schema or "dbo"
    validate_identifier(seq.name, "sequence")
    validate_identifier(seq_schema, "schema")
    seq_qname = _qualify(seq_schema, seq.name)

    with conn.cursor() as cur:
        # Ensure the target schema exists (dbo always exists in SQL Server).
        if seq_schema != "dbo":
            cur.execute("SELECT name FROM sys.schemas WHERE name = ?", (seq_schema,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {quote_identifier(seq_schema)}")
                audit_log(
                    phase="create_schema",
                    status="created",
                    details={"schema": seq_schema},
                )

        # Query complete target definition/state. SQL Server 2017+ exposes
        # last_used_value, which disambiguates an unused sequence from one
        # whose first value happened to equal START WITH.
        cur.execute("SELECT TRY_CONVERT(int, SERVERPROPERTY('ProductMajorVersion'))")
        version_row = cur.fetchone()
        product_major = version_row[0] if version_row else None
        has_last_used = isinstance(product_major, int) and product_major >= 14
        last_used_sql = "CAST(s.last_used_value AS BIGINT)" if has_last_used else "CAST(NULL AS BIGINT)"
        cur.execute(
            "SELECT TYPE_NAME(s.user_type_id), CAST(s.start_value AS BIGINT), "
            "CAST(s.increment AS BIGINT), CAST(s.minimum_value AS BIGINT), "
            "CAST(s.maximum_value AS BIGINT), s.is_cycling, s.is_cached, "
            "CAST(s.cache_size AS BIGINT), CAST(s.current_value AS BIGINT), "
            + last_used_sql + ", s.is_exhausted, s.object_id "
            "FROM sys.sequences s WHERE s.name = ? AND s.schema_id = SCHEMA_ID(?)",
            (seq.name, seq_schema),
        )
        target_row = cur.fetchone()
        try:
            if target_row is None:
                cur.execute(
                    "SELECT o.type_desc FROM sys.objects o "
                    "JOIN sys.schemas s ON s.schema_id=o.schema_id "
                    "WHERE s.name=? AND o.name=?",
                    (seq_schema, seq.name),
                )
                conflict = cur.fetchone()
                if conflict is not None:
                    raise RuntimeError(
                        f"Cannot create source SEQUENCE {seq_schema}.{seq.name}: "
                        f"target contains {conflict[0]}"
                    )
                _create_sequence_ddl(cur, seq, seq_qname)
                expected_next = _sequence_next_value(seq)
                _set_sequence_runtime_state(cur, seq, seq_qname, expected_next)
                outcome = "created"
            else:
                target = {
                    "data_type": str(target_row[0]).lower(),
                    "start_value": int(target_row[1]),
                    "increment": int(target_row[2]),
                    "minimum_value": int(target_row[3]),
                    "maximum_value": int(target_row[4]),
                    "cycle": bool(target_row[5]),
                    "is_cached": bool(target_row[6]),
                    "cache_size": (
                        int(target_row[7]) if bool(target_row[6]) and target_row[7] is not None else None
                    ),
                    "current_value": int(target_row[8]) if target_row[8] is not None else None,
                    "last_used_value": int(target_row[9]) if target_row[9] is not None else None,
                    "is_exhausted": bool(target_row[10]),
                    "object_id": int(target_row[11]),
                }
                expected_next = _sequence_next_value(seq)
                source_definition = _sequence_definition(seq)
                target_definition = {key: target[key] for key in source_definition}
                definition_matches = source_definition == target_definition
                runtime_matches = (
                    target["current_value"]
                    == (int(seq.last_value) if seq.last_value is not None else int(seq.start_value))
                    and (
                        not seq.last_used_value_available
                        or target["last_used_value"] == seq.last_used_value
                    )
                    and target["is_exhausted"] == bool(seq.is_exhausted)
                )
                must_recreate = (
                    source_definition["data_type"] != target_definition["data_type"]
                    or source_definition["start_value"] != target_definition["start_value"]
                )
                if must_recreate:
                    blockers = _sequence_recreation_blockers(
                        cur, target["object_id"], seq_schema, seq.name
                    )
                    if blockers:
                        raise RuntimeError(
                            f"Cannot source-authoritatively recreate sequence {seq_qname}; "
                            "dependency review found: " + "; ".join(blockers)
                        )
                    cur.execute(f"DROP SEQUENCE {seq_qname}")
                    _create_sequence_ddl(cur, seq, seq_qname)
                    outcome = "recreated"
                else:
                    outcome = (
                        "reconciled" if not definition_matches
                        else "state_reconciled" if not runtime_matches
                        else "unchanged"
                    )

                if target_row is None or must_recreate or not runtime_matches:
                    _set_sequence_runtime_state(cur, seq, seq_qname, expected_next)

            cur.execute(
                "SELECT LOWER(TYPE_NAME(s.user_type_id)), CAST(s.start_value AS BIGINT), "
                "CAST(s.increment AS BIGINT), CAST(s.minimum_value AS BIGINT), "
                "CAST(s.maximum_value AS BIGINT), s.is_cycling, s.is_cached, "
                "CAST(s.cache_size AS BIGINT), CAST(s.current_value AS BIGINT), "
                + last_used_sql + ", s.is_exhausted FROM sys.sequences s "
                "WHERE s.name=? AND s.schema_id=SCHEMA_ID(?)",
                (seq.name, seq_schema),
            )
            after = cur.fetchone()
            if after is None:
                raise RuntimeError(f"Sequence {seq_qname} is missing after reconciliation")
            after_definition = {
                "data_type": str(after[0]).lower(), "start_value": int(after[1]),
                "increment": int(after[2]), "minimum_value": int(after[3]),
                "maximum_value": int(after[4]), "cycle": bool(after[5]),
                "is_cached": bool(after[6]),
                "cache_size": int(after[7]) if bool(after[6]) and after[7] is not None else None,
            }
            after_runtime = {
                "current_value": int(after[8]) if after[8] is not None else None,
                "last_used_value": int(after[9]) if after[9] is not None else None,
                "is_exhausted": bool(after[10]),
            }
            if after_definition != _sequence_definition(seq):
                raise RuntimeError(
                    f"Sequence {seq_qname} definition did not match source after reconciliation: "
                    f"source={_sequence_definition(seq)!r}, target={after_definition!r}"
                )
            expected_current = int(seq.last_value) if seq.last_value is not None else int(seq.start_value)
            if (
                after_runtime["current_value"] != expected_current
                or (
                    seq.last_used_value_available
                    and after_runtime["last_used_value"] != seq.last_used_value
                )
                or after_runtime["is_exhausted"] != bool(seq.is_exhausted)
            ):
                raise RuntimeError(
                    f"Sequence {seq_qname} runtime state did not match source after reconciliation: "
                    f"source={{'current_value': {expected_current}, 'last_used_value': {seq.last_used_value}, "
                    f"'is_exhausted': {bool(seq.is_exhausted)}}}, target={after_runtime!r}"
                )
            conn.commit()
            audit_log(
                phase="create_sequence",
                status=outcome,
                details={
                    "sequence": seq_qname,
                    "source_definition": _sequence_definition(seq),
                    "expected_next_value": expected_next,
                    "target_before": (
                        None if target_row is None else {
                            "data_type": str(target_row[0]).lower(),
                            "start_value": int(target_row[1]), "increment": int(target_row[2]),
                            "minimum_value": int(target_row[3]), "maximum_value": int(target_row[4]),
                            "cycle": bool(target_row[5]), "is_cached": bool(target_row[6]),
                            "cache_size": int(target_row[7]) if target_row[7] is not None else None,
                            "current_value": int(target_row[8]) if target_row[8] is not None else None,
                            "last_used_value": int(target_row[9]) if target_row[9] is not None else None,
                            "is_exhausted": bool(target_row[10]),
                        }
                    ),
                    "definition_matches_before": target_row is not None and definition_matches,
                    "definition_matches_after": True,
                    "runtime_state_matches_source_after": True,
                    "target_after": {**after_definition, **after_runtime},
                    "action": outcome,
                },
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="create_sequence", status="failed",
                details={"sequence": seq_qname, "reason": str(exc)},
            )
            raise


def advance_sequence(
    conn: Any,
    sequence_name: str,
    table: str,
    column: str,
    owned_by: str | None = None,
) -> None:
    """Ensure a sequence's next value exceeds loaded data and current state.

    SQL Server does not expose a non-consuming current-value probe via
    ``NEXT VALUE FOR``. ``sys.sequences.current_value`` is the last reserved
    value (including cached reservations), so compare it with MAX(column) and
    only issue RESTART WITH when the loaded data requires a higher next value.
    """
    validate_identifier(column, "column")
    parts = owned_by.rsplit(".", 2) if owned_by else []
    if len(parts) == 3:
        seq_schema, table_name, owned_column = parts
        if owned_column != column:
            raise ValueError(f"Sequence ownership column mismatch: {owned_by}")
        table_qname = _qualify(seq_schema, table_name)
        if table_name != table:
            raise ValueError(f"Sequence ownership table mismatch: {owned_by}")
    elif len(parts) == 2:
        table_name, owned_column = parts
        if owned_column != column or table_name != table:
            raise ValueError(f"Sequence ownership mismatch: {owned_by}")
        table_qname = _qualify("dbo", table_name)
    else:
        table_qname = _qualify("dbo", table)
    seq_schema, _, seq_name = sequence_name.rpartition(".")
    seq_schema = seq_schema or "dbo"
    if "." in seq_name:
        raise ValueError(f"Invalid sequence name: {sequence_name}")
    seq_qname = _qualify(seq_schema, seq_name)
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT CAST(s.current_value AS BIGINT), CAST(s.increment AS BIGINT), "
                "CAST((SELECT MAX(" + quote_identifier(column) + ") FROM " + table_qname + ") AS BIGINT) "
                "FROM sys.sequences s JOIN sys.schemas sch ON sch.schema_id = s.schema_id "
                "WHERE sch.name = ? AND s.name = ?",
                (seq_schema, seq_name),
            )
            state = cur.fetchone()
            if state is None:
                raise RuntimeError(f"Sequence {seq_qname} does not exist on target")
            current_value, increment, maximum = state
            if increment == 0:
                raise RuntimeError(f"Sequence {seq_qname} has an invalid zero increment")
            desired_next = (maximum + increment) if maximum is not None else None
            needs_advance = desired_next is not None and (
                current_value is None
                or (increment > 0 and desired_next > current_value)
                or (increment < 0 and desired_next < current_value)
            )
            if needs_advance:
                cur.execute(f"ALTER SEQUENCE {seq_qname} RESTART WITH {int(desired_next)}")
                outcome = "advanced"
            else:
                outcome = "preserved_current_value"
            conn.commit()
            audit_log(
                phase="advance_sequence", status=outcome,
                details={"sequence": seq_qname, "current_value": current_value,
                         "table_max": maximum, "next_value_floor": desired_next},
            )
        except Exception as exc:
            conn.rollback()
            audit_log(
                phase="advance_sequence", status="failed",
                details={"sequence": seq_qname, "table": table_qname,
                         "column": column, "reason": str(exc)},
            )
            raise
