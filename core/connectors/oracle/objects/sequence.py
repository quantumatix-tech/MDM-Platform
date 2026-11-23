"""Oracle standalone sequence discovery and state synchronization."""

from __future__ import annotations

import re
from typing import Any

from core.connectors.base import SequenceDef, quote_identifier
from core.connectors.oracle._models import validate_oracle_identifier
from core.connectors.oracle.objects._common import _assert_target_type, _value


def discover_sequences(conn: Any, owner: str) -> list[SequenceDef]:
    with conn.cursor() as cur:
        # USER_SEQUENCES gained columns in newer Oracle releases. Inspect the
        # view shape first and select only known option columns available on
        # this server, so older supported releases remain queryable.
        cur.execute("SELECT * FROM user_sequences WHERE 1 = 0")
        catalog_columns = {str(description[0]).upper() for description in (cur.description or [])}
        optional_columns = [
            name
            for name in (
                "ORDER_FLAG",
                "SCALE_FLAG",
                "EXTEND_FLAG",
                "SESSION_FLAG",
                "KEEP_VALUE",
                "SHARDED_FLAG",
            )
            if name in catalog_columns
        ]
        selected_columns = [
            "SEQUENCE_NAME",
            "MIN_VALUE",
            "MAX_VALUE",
            "INCREMENT_BY",
            "CYCLE_FLAG",
            "CACHE_SIZE",
            "LAST_NUMBER",
            *optional_columns,
        ]
        cur.execute(
            "SELECT " + ", ".join(selected_columns) + " FROM user_sequences ORDER BY sequence_name"
        )
        rows = cur.fetchall()
        identity_sequence_names: set[str] = set()
        if any(str(row[0]).startswith("ISEQ$$_") for row in rows):
            cur.execute(
                "SELECT o.object_id FROM user_objects o WHERE o.object_type = 'TABLE' "
                "AND EXISTS (SELECT 1 FROM user_tab_identity_cols i "
                "WHERE i.table_name = o.object_name)"
            )
            identity_sequence_names = {f"ISEQ$$_{int(row[0])}" for row in cur.fetchall()}
            observed = {str(row[0]) for row in rows if str(row[0]).startswith("ISEQ$$_")}
            unknown_identity_sequences = observed - identity_sequence_names
            if unknown_identity_sequences:
                raise RuntimeError(
                    "Oracle internal identity sequence metadata could not be mapped safely: "
                    + ", ".join(sorted(unknown_identity_sequences))
                )
        cur.execute(
            "SELECT table_name, column_name, data_default FROM user_tab_cols "
            "WHERE data_default IS NOT NULL ORDER BY table_name, column_id"
        )
        defaults = cur.fetchall()
    owners_by_sequence: dict[str, str] = {}
    for table_name, column_name, default in defaults:
        expression = str(_value(default))
        for sequence_row in rows:
            sequence_name = str(sequence_row[0])
            sequence_pattern = (
                r'(?i)(?:(?:"?([A-Z0-9_$#]+)"?)\s*\.\s*)?'
                r'"?' + re.escape(sequence_name) + r'"?\s*\.\s*NEXTVAL\b'
            )
            match = re.search(sequence_pattern, expression)
            if match:
                referenced_owner = match.group(1)
                if referenced_owner and referenced_owner.upper() != owner.upper():
                    raise RuntimeError(
                        f"Oracle column default {table_name}.{column_name} references "
                        f"cross-schema sequence {referenced_owner}.{sequence_name}; "
                        "provision and approve that dependency explicitly"
                    )
                column_owner = f"{table_name}.{column_name}"
                if (
                    sequence_name in owners_by_sequence
                    and owners_by_sequence[sequence_name] != column_owner
                ):
                    raise RuntimeError(
                        f"Oracle sequence {owner}.{sequence_name} is referenced by multiple column defaults; "
                        "safe automatic post-load state advancement is ambiguous"
                    )
                owners_by_sequence[sequence_name] = column_owner
                break
    option_names = [name.lower() for name in optional_columns]
    result = []
    for row in rows:
        name, minimum, maximum, increment, cycle, cache, last = row[:7]
        if str(name) in identity_sequence_names:
            # The target table DDL creates its own identity sequence. Its
            # state is advanced from loaded rows with START WITH LIMIT VALUE.
            continue
        sequence_options = dict(zip(option_names, row[7:]))
        validate_oracle_identifier(str(name), "sequence")
        if str(sequence_options.get("sharded_flag", "N")).upper() == "Y":
            raise RuntimeError(
                f"Oracle SHARDED sequence {owner}.{name} is unsupported because target sharding semantics cannot be preserved"
            )
        # Oracle exposes LAST_NUMBER as a cache high-water mark, not an exact
        # current value. Use it only as a conservative initial floor.
        result.append(
            SequenceDef(
                name=str(name),
                schema=owner,
                start_value=int(last),
                min_value=int(minimum),
                max_value=int(maximum),
                increment=int(increment),
                cycle=str(cycle).upper() == "Y",
                last_value=int(last),
                cache_size=int(cache),
                is_cached=int(cache) > 0,
                owned_by=owners_by_sequence.get(str(name)),
                options=sequence_options,
            )
        )
    return result


def create_sequence(conn: Any, seq: SequenceDef) -> None:
    validate_oracle_identifier(seq.name, "sequence")
    if seq.owned_by and (seq.increment <= 0 or seq.cycle):
        raise RuntimeError(
            f"Oracle sequence {seq.name} is attached to {seq.owned_by}; "
            "FULL state synchronization requires a positive, non-cycling sequence"
        )
    _assert_target_type(conn, seq.name, "SEQUENCE")
    order = "ORDER" if str(seq.options.get("order_flag", "N")).upper() == "Y" else "NOORDER"
    if str(seq.options.get("scale_flag", "N")).upper() == "Y":
        scale = (
            "SCALE EXTEND"
            if str(seq.options.get("extend_flag", "N")).upper() == "Y"
            else "SCALE NOEXTEND"
        )
    else:
        # Oracle's SQL keyword is NOSCALE (one token).  ``NO SCALE`` is not
        # accepted by the 26ai sequence grammar and raises ORA-03049.
        scale = "NOSCALE" if "scale_flag" in seq.options else ""
    session = (
        "SESSION"
        if str(seq.options.get("session_flag", "N")).upper() == "Y"
        else ("GLOBAL" if "session_flag" in seq.options else "")
    )
    keep = (
        "KEEP"
        if str(seq.options.get("keep_value", "N")).upper() == "Y"
        else ("NOKEEP" if "keep_value" in seq.options else "")
    )
    option_sql = " ".join(value for value in (order, scale, session, keep) if value)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sequence_name FROM user_sequences WHERE sequence_name = :1", (seq.name,)
        )
        exists = cur.fetchone() is not None
        qname = quote_identifier(seq.name)
        if not exists:
            cache = f"CACHE {seq.cache_size}" if seq.is_cached and seq.cache_size > 0 else "NOCACHE"
            cycle = "CYCLE" if seq.cycle else "NOCYCLE"
            cur.execute(
                f"CREATE SEQUENCE {qname} START WITH {int(seq.start_value)} "
                f"INCREMENT BY {int(seq.increment)} MINVALUE {int(seq.min_value)} "
                f"MAXVALUE {int(seq.max_value)} {cycle} {cache} {option_sql}"
            )
        else:
            cur.execute(
                "SELECT last_number FROM user_sequences WHERE sequence_name = :1",
                (seq.name,),
            )
            state = cur.fetchone()
            if state and int(state[0]) < int(seq.start_value) and (seq.increment <= 0 or seq.cycle):
                raise RuntimeError(
                    f"Cannot safely synchronize existing Oracle sequence {seq.name} "
                    "to the source high-water mark without rewinding or wrapping"
                )
            # ALTER preserves the target's current position; FULL never rewinds
            # a sequence. Unsupported settings fail through Oracle's DDL error.
            cache = f"CACHE {seq.cache_size}" if seq.is_cached and seq.cache_size > 0 else "NOCACHE"
            cycle = "CYCLE" if seq.cycle else "NOCYCLE"
            cur.execute(
                f"ALTER SEQUENCE {qname} INCREMENT BY {int(seq.increment)} "
                f"MINVALUE {int(seq.min_value)} MAXVALUE {int(seq.max_value)} {cycle} {cache} {option_sql}"
            )
    conn.commit()


def advance_sequence(conn: Any, seq_name: str, table: str, column: str) -> None:
    """Ensure a positive increment sequence is beyond loaded explicit values."""
    for value, kind in ((seq_name, "sequence"), (table, "table"), (column, "column")):
        validate_oracle_identifier(value, kind)
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX({quote_identifier(column)}) FROM {quote_identifier(table)}")
        row = cur.fetchone()
        maximum = row[0] if row else None
        if maximum is None:
            return
        cur.execute(
            "SELECT last_number, increment_by, cache_size, cycle_flag "
            "FROM user_sequences WHERE sequence_name = :1",
            (seq_name,),
        )
        state = cur.fetchone()
        if state is None:
            raise RuntimeError(f"Oracle target sequence {seq_name} is missing after FULL load")
        last_number, increment, cache_size, cycle_flag = state
        if int(increment) <= 0 or str(cycle_flag).upper() == "Y":
            raise RuntimeError(
                f"Cannot safely advance Oracle sequence {seq_name}: only positive, non-cycling sequences can be synchronized past loaded rows"
            )
        if int(last_number) > int(maximum):
            return
        # LAST_NUMBER is a cache high-water mark. Add a full cache margin so
        # the allocated NEXTVAL is beyond MAX(column), even with cached values.
        step = int(increment)
        delta = int(maximum) - int(last_number) + max(int(cache_size), 1) * step + step
        qseq = quote_identifier(seq_name)
        cur.execute(f"ALTER SEQUENCE {qseq} INCREMENT BY {delta}")
        cur.execute(f"SELECT {qseq}.NEXTVAL FROM dual")
        cur.fetchone()
        cur.execute(f"ALTER SEQUENCE {qseq} INCREMENT BY {step}")
    conn.commit()


def restore_standalone_sequence_state(conn: Any, seq: SequenceDef) -> None:
    """Move target forward to the source's catalog high-water mark, never back."""
    validate_oracle_identifier(seq.name, "sequence")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_number, increment_by, cache_size, cycle_flag "
            "FROM user_sequences WHERE sequence_name = :1",
            (seq.name,),
        )
        state = cur.fetchone()
        if state is None:
            raise RuntimeError(
                f"Oracle target sequence {seq.name} is missing after FULL preparation"
            )
        current, increment, cache_size, cycle_flag = state
        source_floor = int(seq.last_value if seq.last_value is not None else seq.start_value)
        if int(current) >= source_floor:
            return
        if int(increment) <= 0 or str(cycle_flag).upper() == "Y":
            raise RuntimeError(
                f"Cannot safely synchronize Oracle sequence {seq.name} to source high-water mark: "
                "descending and cycling sequences cannot use the monotonic state policy"
            )
        step = int(increment)
        delta = source_floor - int(current) + max(int(cache_size), 1) * step + step
        qseq = quote_identifier(seq.name)
        cur.execute(f"ALTER SEQUENCE {qseq} INCREMENT BY {delta}")
        cur.execute(f"SELECT {qseq}.NEXTVAL FROM dual")
        cur.fetchone()
        cur.execute(f"ALTER SEQUENCE {qseq} INCREMENT BY {step}")
    conn.commit()


def sequence_name(seq: SequenceDef) -> str:
    return seq.name
