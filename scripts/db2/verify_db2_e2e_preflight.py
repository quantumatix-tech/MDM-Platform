#!/usr/bin/env python3
"""READ-ONLY Db2 E2E preflight verification.

Validates live source/target connectivity, schemas, tables, row counts,
partition keys/boundaries, UDTs, and target privileges.
No DDL, no data modification, no migration execution.
"""

import os
import sys

# Ensure repository root is on sys.path when run as a script
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import argparse

import yaml

from core.connectors.db2.objects import grant as db2_grant
from core.connectors.db2.objects import partition as db2_partition
from core.connectors.db2.objects import schema as db2_schema
from core.connectors.db2.objects import udt_alias as db2_udt_alias
from core.connectors.db2.source import DB2SourceConnector
from core.connectors.db2.target import DB2TargetConnector
from core.orchestration.db2_full import is_partitioned_source
from core.secrets import create_secret_provider


def run(config_path: str):
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    sp = create_secret_provider(cfg)

    # ---- Source ----
    sc = dict(cfg['source']['connection'])
    sc['password'] = sp.resolve(sc['password_secret'])
    sc['migration'] = cfg.get('migration', {})
    src = DB2SourceConnector(sc)
    src.connect()
    print('=== SOURCE ===')
    print(f'SOURCE_DB: {sc["database"]}')
    schemas = src._get_selected_schemas()
    print(f'CONFIGURED_SCHEMAS: {schemas}')

    # Validate source schemas exist
    pre = src.preflight_validate_schemas()
    print(f'SCHEMA_VALIDATION: {pre}')

    # Discover all source tables with schema
    table_pairs = src.list_objects_with_schema()
    print(f'SOURCE_TABLES: {table_pairs}')

    # Row counts per table
    total_rows = 0
    for sch, tbl in table_pairs:
        cnt = src.get_object_count(tbl, schema_name=sch)
        print(f'COUNT {sch}.{tbl}: {cnt}')
        total_rows += cnt
    print(f'TOTAL_SOURCE_ROWS: {total_rows}')

    # Partition info for each table
    for sch, tbl in table_pairs:
        try:
            parts = db2_partition.discover_partitions(src._conn, sch, tbl)
            if parts:
                print(f'PARTITIONS {sch}.{tbl}:')
                for p in parts:
                    print(f'  {p}')
                if is_partitioned_source(parts):
                    pk_cols = db2_partition.discover_partition_column(src._conn, sch, tbl)
                    print(f'PARTITION_KEY {sch}.{tbl}: {pk_cols}')
                else:
                    print(f'PARTITIONS {sch}.{tbl}: (ordinary table - PART0 with blank boundaries)')
            else:
                print(f'PARTITIONS {sch}.{tbl}: (none - ordinary table)')
        except Exception as e:
            print(f'PARTITION_ERROR {sch}.{tbl}: {e}')

    # ---- Target ----
    tc = dict(cfg['target']['connection'])
    tc['password'] = sp.resolve(tc['password_secret'])
    tgt = DB2TargetConnector(tc)
    tgt.connect()
    print('\n=== TARGET ===')
    pf = tgt.preflight()
    print(f'TARGET_DB: {pf["current_server"]}')
    print(f'TARGET_USER: {pf["session_user"]}')

    # Safety check
    if str(sc['database']).strip().upper() == str(pf.get('current_server')).strip().upper():
        raise RuntimeError('Source and target resolve to the same database - refusing DDL')

    # Target schema/table existence
    for sch in schemas:
        exists = db2_schema.schema_exists(tgt._conn, sch)
        print(f'TARGET_SCHEMA_EXISTS[{sch}]: {exists}')
        if exists:
            tables = db2_schema.list_tables_in_schema(tgt._conn, sch)
            print(f'TARGET_TABLES[{sch}]: {tables}')

    # Target UDTs and aliases (leftover from failed runs)
    for sch in schemas:
        try:
            udts = db2_udt_alias.discover_udts(tgt._conn, sch)
            if udts:
                print(f'TARGET_UDTS[{sch}]: {udts}')
            aliases = db2_udt_alias.discover_aliases(tgt._conn, sch)
            if aliases:
                print(f'TARGET_ALIASES[{sch}]: {aliases}')
        except Exception as e:
            print(f'TARGET_UDT_ALIAS_ERROR[{sch}]: {e}')

    # Target privileges (what the connected user can do)
    for sch in schemas:
        try:
            grants = db2_grant.discover_grants(tgt._conn, sch)
            if grants:
                print(f'TARGET_GRANTS[{sch}]:')
                for g in grants:
                    print(f'  {g}')
        except Exception as e:
            print(f'TARGET_GRANT_ERROR[{sch}]: {e}')

    print('\n=== SUMMARY ===')
    print(f'Source: {sc["database"]} | Target: {pf["current_server"]}')
    print(f'Schemas: {schemas}')
    print(f'Tables: {len(table_pairs)} | Total rows: {total_rows}')
    print('READ-ONLY VERIFICATION COMPLETE - no DDL executed')

    src.close()
    tgt.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='READ-ONLY Db2 E2E preflight verification')
    p.add_argument('--config', default='config/db2_connection_test.yaml', help='Path to YAML config file')
    args = p.parse_args()
    run(args.config)