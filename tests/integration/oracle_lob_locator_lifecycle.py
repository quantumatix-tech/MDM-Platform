"""Opt-in real-Oracle regression probe for streamed LOB/XMLTYPE lifecycle.

Run only against the dedicated source/target owners in an Oracle E2E config.
The probe creates two randomly named tables and drops only those tables in a
finally block. It never addresses configured migration fixture tables.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.connectors.base import quote_identifier
from core.connectors.oracle.precision import (
    compare_oracle_rows,
    oracle_lob_units,
)
from core.connectors.oracle.source import OracleSourceConnector
from core.connectors.oracle.target import OracleTargetConnector, _schemas_compatible
from tests.e2e.oracle.config import (
    connection_config,
    resolver_for_side,
    validated_oracle_context,
)
from tests.e2e.acceptance import load_e2e_config


def _assert_probe_opt_in(cfg: Any) -> None:
    if os.environ.get("RUN_ORACLE_LOB_PROBE") != "1":
        raise RuntimeError("Set RUN_ORACLE_LOB_PROBE=1 to opt in to the LIVE Oracle LOB probe")
    policy = cfg.raw.get("e2e", {}).get("lob_probe", {})
    if not isinstance(policy, dict) or policy.get("disposable") is not True:
        raise RuntimeError("Selected Oracle config must explicitly mark both probe owners disposable")


def _verify_empty_disposable_owner(connection: Any, owner: str, side: str) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT USER FROM dual")
        identity = cur.fetchone()
        if not identity or str(identity[0]).casefold() != owner.casefold():
            raise RuntimeError(f"Oracle {side} session is not connected as the configured owner")
        cur.execute("SELECT COUNT(*) FROM user_objects")
        object_count = cur.fetchone()
        if not object_count or int(object_count[0]) != 0:
            raise RuntimeError(f"Oracle {side} probe owner is not empty and disposable")


def _seed_source_table(connection: Any, table: str) -> None:
    import oracledb

    qtable = quote_identifier(table)
    with connection.cursor() as cur:
        cur.execute(
            f"INSERT INTO {qtable} (\"ID\", \"BIN_DATA\", \"TEXT_DATA\", \"XML_DATA\", \"RAW_DATA\") "
            "VALUES (1, NULL, NULL, NULL, NULL)"
        )
        cur.execute(
            f"INSERT INTO {qtable} (\"ID\", \"BIN_DATA\", \"TEXT_DATA\", \"XML_DATA\", \"RAW_DATA\") "
            "VALUES (2, EMPTY_BLOB(), EMPTY_CLOB(), NULL, NULL)"
        )
        blob_var = cur.var(oracledb.DB_TYPE_BLOB)
        clob_var = cur.var(oracledb.DB_TYPE_CLOB)
        xml = '<root attr="雪"><text>🙂 Oracle LOB lifecycle</text></root>'
        raw_hex = "00FF102030405060"
        cur.execute(
            f"INSERT INTO {qtable} (\"ID\", \"BIN_DATA\", \"TEXT_DATA\", \"XML_DATA\", \"RAW_DATA\") "
            "VALUES (:1, EMPTY_BLOB(), EMPTY_CLOB(), XMLTYPE(:2), HEXTORAW(:3)) "
            "RETURNING \"BIN_DATA\", \"TEXT_DATA\" INTO :4, :5",
            [3, xml, raw_hex, blob_var, clob_var],
        )
        blob_locator = blob_var.getvalue()
        clob_locator = clob_var.getvalue()
        binary = (bytes(range(256)) * 512) + b"end"
        text = "雪🙂<payload>Oracle CLOB lifecycle</payload>" * 12000
        offset = 1
        for start in range(0, len(binary), 64 * 1024):
            chunk = binary[start:start + 64 * 1024]
            blob_locator.write(chunk, offset=offset)
            offset += len(chunk)
        offset = 1
        for start in range(0, len(text), 16 * 1024):
            chunk = text[start:start + 16 * 1024]
            clob_locator.write(chunk, offset=offset)
            offset += oracle_lob_units(chunk, "CLOB")
        connection.commit()


def run(config_path: str) -> None:
    cfg = load_e2e_config(config_path)
    _assert_probe_opt_in(cfg)
    owners, _ = validated_oracle_context(cfg)
    source_connection = connection_config(
        cfg, "source", owners["source"], resolver_for_side(cfg.raw, "source")
    )
    target_connection = connection_config(
        cfg, "target", owners["target"], resolver_for_side(cfg.raw, "target")
    )
    source = OracleSourceConnector(source_connection)
    target = OracleTargetConnector({**target_connection, "source_engine": "oracle"})
    table = "DMS_LOB_LIFECYCLE_" + secrets.token_hex(4).upper()
    source_created = target_created = False
    try:
        source.connect()
        target.connect()
        for side, connection, owner in (
            ("source", source._conn, owners["source"]),
            ("target", target._conn, owners["target"]),
        ):
            _verify_empty_disposable_owner(connection, owner, side)
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM user_tables WHERE table_name = :1", (table,)
                )
                if cur.fetchone() is not None:
                    raise RuntimeError("Generated integration table name unexpectedly exists")
        with source._conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {quote_identifier(table)} ("
                '"ID" NUMBER PRIMARY KEY, "BIN_DATA" BLOB, "TEXT_DATA" CLOB, '
                '"XML_DATA" XMLTYPE, "RAW_DATA" RAW(32))'
            )
        source_created = True
        _seed_source_table(source._conn, table)
        schema = source.get_schema(table)
        target.create_object_if_missing(schema)
        target_created = True
        source.enable_precision_safe_full_load()
        target.enable_precision_safe_full_load()

        load = target.upsert_batch(table, source.export_full(table), schema)
        if load.failure_count or load.success_count != 3:
            raise AssertionError(
                f"LOB transport failed: loaded={load.success_count}, "
                f"failed={load.failure_count}, errors={load.errors}"
            )
        data_check = compare_oracle_rows(
            source.export_full(table), target.export_full(table), schema,
            all_columns=True,
        )
        target_schema = target.inspect_schema(table)
        if not data_check["match"]:
            raise AssertionError(f"LOB value comparison failed: {data_check['mismatches']}")
        if target_schema is None or not _schemas_compatible(schema, target_schema, "oracle"):
            raise AssertionError("LOB lifecycle target schema comparison failed")
        print("PASS: 3 rows; NULL, EMPTY, large BLOB/CLOB, XMLTYPE, and RAW verified")
    finally:
        cleanup_errors: list[Exception] = []
        active_error = sys.exception()
        for connector, created in ((target, target_created), (source, source_created)):
            if created and connector._conn is not None:
                try:
                    with connector._conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM user_tables WHERE table_name = :1", (table,)
                        )
                        exists = cur.fetchone() is not None
                        if exists:
                            cur.execute(f"DROP TABLE {quote_identifier(table)} PURGE")
                    connector._conn.commit()
                except Exception as exc:
                    cleanup_errors.append(exc)
        try:
            try:
                source.close()
            except Exception as exc:
                cleanup_errors.append(exc)
        finally:
            try:
                target.close()
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            cleanup_message = (
                "Temporary Oracle LOB probe cleanup failed: "
                + ", ".join(type(exc).__name__ for exc in cleanup_errors)
            )
            if active_error is not None:
                active_error.add_note(cleanup_message)
            else:
                raise RuntimeError(cleanup_message) from cleanup_errors[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True,
        help="Explicit Oracle config whose source and target owners are disposable and empty",
    )
    args = parser.parse_args()
    try:
        run(args.config)
        return 0
    except Exception as exc:
        code = re.search(r"\b(?:ORA-\d{5}|DPY-\d{4})\b", str(exc), re.IGNORECASE)
        suffix = f" ({code.group(0).upper()})" if code else ""
        print(f"FAIL: Oracle LOB lifecycle probe: {type(exc).__name__}{suffix}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
