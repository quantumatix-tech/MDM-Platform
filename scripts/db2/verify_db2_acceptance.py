#!/usr/bin/env python3
"""COMPREHENSIVE READ-ONLY acceptance verification for Db2-to-Db2 migration.

Compares source and target object DEFINITIONS, not just counts.
Validates: partitions, PK/FK/Unique/Indexes, UDTs, identities, sequences,
views/MQTs, aliases, routines, triggers, comments, grants, data equality.
"""

import os
import sys

# Ensure repository root is on sys.path when run as a script
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import argparse
import sys

import yaml

from core.connectors.db2.objects import (
    comment as db2_comment,
)
from core.connectors.db2.objects import (
    grant as db2_grant,
)
from core.connectors.db2.objects import (
    mqt as db2_mqt,
)
from core.connectors.db2.objects import (
    partition as db2_partition,
)
from core.connectors.db2.objects import (
    routine as db2_routine,
)
from core.connectors.db2.objects import (
    sequence as db2_sequence,
)
from core.connectors.db2.objects import (
    table as db2_table,
)
from core.connectors.db2.objects import (
    trigger as db2_trigger,
)
from core.connectors.db2.objects import (
    udt_alias as db2_udt_alias,
)
from core.connectors.db2.objects import (
    view as db2_view,
)
from core.connectors.db2.objects.check_default import discover_column_defaults
from core.connectors.db2.source import DB2SourceConnector
from core.connectors.db2.target import DB2TargetConnector
from core.orchestration.db2_full import is_partitioned_source
from core.secrets import create_secret_provider


class VerificationResult:
    def __init__(self):
        self.mismatches = []
        self.errors = []
        self.warnings = []

    def add_mismatch(self, category, detail):
        self.mismatches.append(f"[{category}] {detail}")

    def add_error(self, category, detail):
        self.errors.append(f"[{category}] {detail}")

    def add_warning(self, category, detail):
        self.warnings.append(f"[{category}] {detail}")

    def has_failures(self):
        return len(self.mismatches) > 0 or len(self.errors) > 0

    def summary(self):
        if self.errors:
            return "FAIL"
        if self.mismatches:
            return "MISMATCH"
        if self.warnings:
            return "INCOMPLETE"
        return "PASS"

    def exit_code(self):
        if self.errors:
            return 2
        if self.mismatches:
            return 1
        if self.warnings:
            return 0
        return 0


def connect_both(config_path, result):
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        result.add_error("CONFIG", f"Failed to load config: {e}")
        return None

    sp = create_secret_provider(cfg)

    # Source
    try:
        sc = dict(cfg['source']['connection'])
        sc['password'] = sp.resolve(sc['password_secret'])
        sc['migration'] = cfg.get('migration', {})
        src = DB2SourceConnector(sc)
        src.connect()
    except Exception as e:
        result.add_error("SOURCE_CONNECT", str(e))
        return None

    # Target
    try:
        tc = dict(cfg['target']['connection'])
        tc['password'] = sp.resolve(tc['password_secret'])
        tgt = DB2TargetConnector(tc)
        tgt.connect()
    except Exception as e:
        result.add_error("TARGET_CONNECT", str(e))
        try:
            src.close()
        except Exception:
            pass
        return None

    pf = tgt.preflight()
    if str(sc['database']).strip().upper() == str(pf.get('current_server')).strip().upper():
        result.add_error("SAFETY", 'Source and target resolve to same database')
        src.close()
        tgt.close()
        return None

    return src, tgt, sc['database'], pf['current_server'], src._get_selected_schemas(), cfg


def _norm_grant(g):
    """Normalize grant dict for comparison including grantable flag."""
    return (
        g.get('object_type'),
        g.get('object_name'),
        g.get('column'),
        g.get('grantee'),
        g.get('grantee_type'),
        g.get('privilege'),
        g.get('grantable'),
    )


def _norm_grant_privilege_only(g):
    """Normalize grant dict for privilege-presence comparison (excludes grantable)."""
    return (
        g.get('object_type'),
        g.get('object_name'),
        g.get('column'),
        g.get('grantee'),
        g.get('grantee_type'),
        g.get('privilege'),
    )


def compare_partitions(src, tgt, schemas, result):
    print('\n=== PARTITION KEY & BOUNDARIES ===')
    for sch in schemas:
        for _, tbl in src.list_objects_with_schema():
            if _ != sch:
                continue
            try:
                parts = db2_partition.discover_partitions(src._conn, sch, tbl)
                if parts and is_partitioned_source(parts):
                    pk_src = db2_partition.discover_partition_column(src._conn, sch, tbl)
                    pk_tgt = db2_partition.discover_partition_column(tgt._conn, sch, tbl)
                    parts_tgt = db2_partition.discover_partitions(tgt._conn, sch, tbl)
                    key_match = pk_src == pk_tgt
                    bounds_match = parts == parts_tgt
                    print(f'  {sch}.{tbl}: src_key={pk_src} tgt_key={pk_tgt} KEY_MATCH={key_match}')
                    print(f'    src_bounds={parts}')
                    print(f'    tgt_bounds={parts_tgt} BOUNDS_MATCH={bounds_match}')
                    if not key_match:
                        result.add_mismatch(f"PARTITION_KEY.{sch}.{tbl}", f"keys differ: src={pk_src} tgt={pk_tgt}")
                    if not bounds_match:
                        result.add_mismatch(f"PARTITION_BOUNDS.{sch}.{tbl}", f"bounds differ: src={parts} tgt={parts_tgt}")
                else:
                    print(f'  {sch}.{tbl}: (ordinary table)')
            except Exception as e:
                result.add_error(f"PARTITION.{sch}.{tbl}", str(e))
                print(f'  {sch}.{tbl}: ERROR - {e}')


def compare_pk_fk_index_unique(src, tgt, schemas, result):
    print('\n=== PK / FK / UNIQUE / INDEXES ===')
    for sch in schemas:
        for _, tbl in src.list_objects_with_schema():
            if _ != sch:
                continue
            try:
                s = src.get_schema(tbl, schema_name=sch)
                ts = db2_table.get_schema_for_table(tgt._conn, tbl, sch)
            except Exception as e:
                result.add_error(f"SCHEMA_LOOKUP.{sch}.{tbl}", str(e))
                print(f'  {sch}.{tbl}: TARGET SCHEMA LOOKUP FAILED: {e}')
                continue

            # PK
            pk_match = set(s.primary_key) == set(ts.primary_key)
            print(f'  {sch}.{tbl}: PK_src={s.primary_key} PK_tgt={ts.primary_key} MATCH={pk_match}')
            if not pk_match:
                result.add_mismatch(f"PK.{sch}.{tbl}", f"src={s.primary_key} tgt={ts.primary_key}")

            # FK
            fk_src = [(fk.name, fk.columns, fk.ref_schema, fk.ref_table, fk.ref_columns, fk.on_delete, fk.on_update) for fk in getattr(s, 'foreign_keys', [])]
            fk_tgt = [(fk.name, fk.columns, fk.ref_schema, fk.ref_table, fk.ref_columns, fk.on_delete, fk.on_update) for fk in getattr(ts, 'foreign_keys', [])]
            fk_match = sorted(fk_src) == sorted(fk_tgt)
            print(f'  {sch}.{tbl}: FK_src={fk_src}')
            print(f'    FK_tgt={fk_tgt} MATCH={fk_match}')
            if not fk_match:
                result.add_mismatch(f"FK.{sch}.{tbl}", f"src={fk_src} tgt={fk_tgt}")

            # Indexes
            idx_src = [(i.name, i.columns, i.unique, i.descending_columns) for i in s.indexes]
            idx_tgt = [(i.name, i.columns, i.unique, i.descending_columns) for i in ts.indexes]
            idx_match = sorted(idx_src) == sorted(idx_tgt)
            print(f'  {sch}.{tbl}: IDX_src={idx_src}')
            print(f'    IDX_tgt={idx_tgt} MATCH={idx_match}')
            if not idx_match:
                result.add_mismatch(f"INDEX.{sch}.{tbl}", f"src={idx_src} tgt={idx_tgt}")

            # Unique constraints
            uc_src = [(u.name, u.columns) for u in s.unique_constraints]
            uc_tgt = [(u.name, u.columns) for u in ts.unique_constraints]
            uc_match = sorted(uc_src) == sorted(uc_tgt)
            print(f'  {sch}.{tbl}: UNIQ_src={uc_src}')
            print(f'    UNIQ_tgt={uc_tgt} MATCH={uc_match}')
            if not uc_match:
                result.add_mismatch(f"UNIQUE.{sch}.{tbl}", f"src={uc_src} tgt={uc_tgt}")


def compare_udts_aliases(src, tgt, schemas, result):
    print('\n=== UDTs & ALIASES ===')
    for sch in schemas:
        try:
            udts_src = db2_udt_alias.discover_udts(src._conn, sch)
            udts_tgt = db2_udt_alias.discover_udts(tgt._conn, sch)
            udt_match = udts_src == udts_tgt
            print(f'  {sch}: UDT_src={udts_src}')
            print(f'  {sch}: UDT_tgt={udts_tgt} MATCH={udt_match}')
            if not udt_match:
                result.add_mismatch(f"UDT.{sch}", f"src={udts_src} tgt={udts_tgt}")

            aliases_src = db2_udt_alias.discover_aliases(src._conn, sch)
            aliases_tgt = db2_udt_alias.discover_aliases(tgt._conn, sch)
            alias_match = aliases_src == aliases_tgt
            print(f'  {sch}: ALIAS_src={aliases_src}')
            print(f'  {sch}: ALIAS_tgt={aliases_tgt} MATCH={alias_match}')
            if not alias_match:
                result.add_mismatch(f"ALIAS.{sch}", f"src={aliases_src} tgt={aliases_tgt}")
        except Exception as e:
            result.add_error(f"UDT_ALIAS.{sch}", str(e))
            print(f'  {sch}: UDT/ALIAS ERROR - {e}')


def _identity_config_match(id_src, id_tgt):
    """Compare identity configuration (excluding runtime nextcache)."""
    if len(id_src) != len(id_tgt):
        return False
    for a, b in zip(id_src, id_tgt):
        for f in ("column", "type", "generated"):
            if a.get(f) != b.get(f):
                return False
        attr_a = a.get("attributes", {})
        attr_b = b.get("attributes", {})
        for f in ("increment", "minvalue", "maxvalue", "cycle", "cache", "order"):
            if attr_a.get(f) != attr_b.get(f):
                return False
    return True


def _sequence_config_match(seq_src, seq_tgt):
    """Compare sequence configuration (excluding runtime nextcache)."""
    if len(seq_src) != len(seq_tgt):
        return False
    for a, b in zip(seq_src, seq_tgt):
        config_fields = ["name", "datatype", "start", "increment", "minvalue", "maxvalue", "cycle", "cache", "order", "precision"]
        for f in config_fields:
            if a.get(f) != b.get(f):
                return False
    return True


def _get_max_id(src, sch, tbl, id_col):
    """Get current maximum ID value for identity column (read-only)."""
    try:
        with src._conn.cursor() as cur:
            cur.execute(f'SELECT MAX("{id_col}") FROM "{sch}"."{tbl}"')
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None
    except Exception:
        return None


def compare_identities_sequences(src, tgt, schemas, result):
    print('\n=== IDENTITIES & SEQUENCES ===')
    from core.connectors.db2.objects import identity as db2_identity

    for sch in schemas:
        for _, tbl in src.list_objects_with_schema():
            if _ != sch:
                continue
            try:
                id_src = db2_identity.discover_identity_columns(src._conn, sch, tbl)
                id_tgt = db2_identity.discover_identity_columns(tgt._conn, sch, tbl)
                if id_src or id_tgt:
                    config_match = _identity_config_match(id_src, id_tgt)
                    print(f'  {sch}.{tbl}: ID_src={id_src}')
                    print(f'  {sch}.{tbl}: ID_tgt={id_tgt} CONFIG_MATCH={config_match}')
                    if not config_match:
                        result.add_mismatch(f"IDENTITY_CONFIG.{sch}.{tbl}", f"src={id_src} tgt={id_tgt}")

                    # Report NEXTCACHE/runtime state separately
                    for i, (a, b) in enumerate(zip(id_src, id_tgt)):
                        nextcache_src = a.get("attributes", {}).get("nextcache")
                        nextcache_tgt = b.get("attributes", {}).get("nextcache")
                        if nextcache_src is not None or nextcache_tgt is not None:
                            max_id_src = _get_max_id(src, sch, tbl, a["column"])
                            max_id_tgt = _get_max_id(tgt, sch, tbl, b["column"])
                            print(f'    IDENTITY_RUNTIME: col={a["column"]} nextcache_src={nextcache_src} nextcache_tgt={nextcache_tgt} max_id_src={max_id_src} max_id_tgt={max_id_tgt}')
                            if nextcache_src != nextcache_tgt:
                                result.add_warning(f"IDENTITY_RUNTIME.{sch}.{tbl}.{a['column']}", f"nextcache differs (src={nextcache_src}, tgt={nextcache_tgt}); max_id: src={max_id_src}, tgt={max_id_tgt}; next generated value depends on runtime state")
            except Exception as e:
                result.add_error(f"IDENTITY.{sch}.{tbl}", str(e))
                print(f'  {sch}.{tbl}: IDENTITY ERROR - {e}')

        try:
            seq_src = db2_sequence.discover_standalone_sequences(src._conn, sch)
            seq_tgt = db2_sequence.discover_standalone_sequences(tgt._conn, sch)
            if seq_src or seq_tgt:
                config_match = _sequence_config_match(seq_src, seq_tgt)
                print(f'  {sch}: SEQ_src={seq_src}')
                print(f'  {sch}: SEQ_tgt={seq_tgt} CONFIG_MATCH={config_match}')
                if not config_match:
                    result.add_mismatch(f"SEQUENCE_CONFIG.{sch}", f"src={seq_src} tgt={seq_tgt}")

                # Report NEXTCACHE/runtime state separately - only warn if different
                for a, b in zip(seq_src, seq_tgt):
                    nextcache_src = a.get("nextcache")
                    nextcache_tgt = b.get("nextcache")
                    if nextcache_src is not None or nextcache_tgt is not None:
                        print(f'    SEQUENCE_RUNTIME: name={a["name"]} nextcache_src={nextcache_src} nextcache_tgt={nextcache_tgt}')
                        if nextcache_src != nextcache_tgt:
                            result.add_warning(f"SEQUENCE_RUNTIME.{sch}.{a['name']}", f"nextcache differs (src={nextcache_src}, tgt={nextcache_tgt}); next generated value depends on runtime state")
        except Exception as e:
            result.add_error(f"SEQUENCE.{sch}", str(e))
            print(f'  {sch}: SEQUENCE ERROR - {e}')


def compare_defaults(src, tgt, schemas, result):
    print('\n=== COLUMN DEFAULTS ===')
    for sch in schemas:
        try:
            defaults_src = discover_column_defaults(src._conn, sch)
            defaults_tgt = discover_column_defaults(tgt._conn, sch)

            src_map = {(d["table"], d["column"]): d["default"] for d in defaults_src if d.get("default") is not None}
            tgt_map = {(d["table"], d["column"]): d["default"] for d in defaults_tgt if d.get("default") is not None}

            all_keys = set(src_map.keys()) | set(tgt_map.keys())

            print(f'  {sch}: DEFAULT_src_count={len(src_map)} DEFAULT_tgt_count={len(tgt_map)}')
            for key in sorted(all_keys):
                table, column = key
                src_val = src_map.get(key)
                tgt_val = tgt_map.get(key)
                match = src_val == tgt_val
                print(f'    {table}.{column}: src={src_val} tgt={tgt_val} MATCH={match}')
                if not match:
                    if src_val is not None and tgt_val is None:
                        result.add_mismatch(f"DEFAULT_MISSING.{sch}.{table}.{column}", f"source has DEFAULT {src_val}, target has NULL")
                    elif src_val is None and tgt_val is not None:
                        result.add_warning(f"DEFAULT_EXTRA.{sch}.{table}.{column}", f"target has DEFAULT {tgt_val}, source has NULL")
                    else:
                        result.add_mismatch(f"DEFAULT_MISMATCH.{sch}.{table}.{column}", f"source={src_val} target={tgt_val}")
        except Exception as e:
            result.add_error(f"DEFAULT.{sch}", str(e))
            print(f'  {sch}: DEFAULT ERROR - {e}')


def compare_views_mqts(src, tgt, schemas, result):
    print('\n=== VIEWS & MQTs ===')
    for sch in schemas:
        try:
            views_src = db2_view.discover_views(src._conn, sch)
            views_tgt = db2_view.discover_views(tgt._conn, sch)
            view_match = views_src == views_tgt
            print(f'  {sch}: VIEW_src={views_src}')
            print(f'  {sch}: VIEW_tgt={views_tgt} MATCH={view_match}')
            if not view_match:
                result.add_mismatch(f"VIEW.{sch}", f"src={views_src} tgt={views_tgt}")

            mqts_src = db2_mqt.discover_mqts(src._conn, sch)
            mqts_tgt = db2_mqt.discover_mqts(tgt._conn, sch)
            mqt_match = mqts_src == mqts_tgt
            print(f'  {sch}: MQT_src={mqts_src}')
            print(f'  {sch}: MQT_tgt={mqts_tgt} MATCH={mqt_match}')
            if not mqt_match:
                result.add_mismatch(f"MQT.{sch}", f"src={mqts_src} tgt={mqts_tgt}")
        except Exception as e:
            result.add_error(f"VIEW_MQT.{sch}", str(e))
            print(f'  {sch}: VIEW/MQT ERROR - {e}')


def compare_routines(src, tgt, schemas, result):
    print('\n=== ROUTINES (FUNCTIONS/PROCEDURES) ===')
    for sch in schemas:
        try:
            routines_src = db2_routine.discover_routines(src._conn, sch)
            routines_tgt = db2_routine.discover_routines(tgt._conn, sch)
            # Routine dict keys: name, specific, type, language, definition
            def routine_key(r):
                return (r.get("name"), r.get("type"), r.get("definition"))

            src_map = {routine_key(r): r for r in routines_src}
            tgt_map = {routine_key(r): r for r in routines_tgt}

            src_names = set(src_map.keys())
            tgt_names = set(tgt_map.keys())

            only_src = src_names - tgt_names
            only_tgt = tgt_names - src_names
            common = src_names & tgt_names

            print(f'  {sch}: ROUTINE_src_count={len(routines_src)} ROUTINE_tgt_count={len(routines_tgt)}')
            if only_src:
                print(f'    ONLY_IN_SOURCE: {[k[0] for k in only_src]}')
                for k in only_src:
                    result.add_mismatch(f"ROUTINE_MISSING.{sch}.{k[0]}", "routine exists in source but not target")
            if only_tgt:
                print(f'    ONLY_IN_TARGET: {[k[0] for k in only_tgt]}')
                for k in only_tgt:
                    result.add_warning(f"ROUTINE_EXTRA.{sch}.{k[0]}", "routine exists in target but not source")
            if common:
                def_match = all(src_map[k].get("definition") == tgt_map[k].get("definition") for k in common)
                print(f'    COMMON_DEFINITIONS_MATCH: {def_match}')
                if not def_match:
                    for k in common:
                        if src_map[k].get("definition") != tgt_map[k].get("definition"):
                            result.add_mismatch(f"ROUTINE_DEF.{sch}.{k[0]}", "definition differs")
        except Exception as e:
            result.add_error(f"ROUTINE.{sch}", str(e))
            print(f'  {sch}: ROUTINE ERROR - {e}')


def compare_triggers(src, tgt, schemas, result):
    print('\n=== TRIGGERS ===')
    for sch in schemas:
        try:
            trig_src = db2_trigger.discover_triggers(src._conn, sch)
            trig_tgt = db2_trigger.discover_triggers(tgt._conn, sch, include_invalid=True)
            # discover_triggers returns: {"name": name, "definition": text, "valid": "Y|N", "enabled": "Y|N"}
            src_map = {t["name"]: t for t in trig_src}
            tgt_map = {t["name"]: t for t in trig_tgt}

            only_src = set(src_map.keys()) - set(tgt_map.keys())
            only_tgt = set(tgt_map.keys()) - set(src_map.keys())
            common = set(src_map.keys()) & set(tgt_map.keys())

            print(f'  {sch}: TRIG_src_names={list(src_map.keys())}')
            print(f'  {sch}: TRIG_tgt_names={[(n, tgt_map[n].get("valid"), tgt_map[n].get("enabled")) for n in tgt_map.keys()]}')
            if only_src:
                print(f'    ONLY_IN_SOURCE: {list(only_src)}')
                for n in only_src:
                    result.add_mismatch(f"TRIGGER_MISSING.{sch}.{n}", "trigger exists in source but not target")
            if only_tgt:
                print(f'    ONLY_IN_TARGET: {list(only_tgt)}')
                for n in only_tgt:
                    result.add_warning(f"TRIGGER_EXTRA.{sch}.{n}", "trigger exists in target but not source")
            if common:
                def_match = all(src_map[n]["definition"] == tgt_map[n]["definition"] for n in common)
                print(f'    COMMON_DEFINITIONS_MATCH: {def_match}')
                if not def_match:
                    for n in common:
                        if src_map[n]["definition"] != tgt_map[n]["definition"]:
                            result.add_mismatch(f"TRIGGER_DEF.{sch}.{n}", "definition differs")
                # Check for INVALID triggers in common
                for n in common:
                    tgt_valid = tgt_map[n].get("valid", "N")
                    tgt_enabled = tgt_map[n].get("enabled", "N")
                    if tgt_valid != "Y":
                        result.add_mismatch(f"TRIGGER_INVALID.{sch}.{n}", f"target trigger is INVALID (VALID={tgt_valid}, ENABLED={tgt_enabled})")
        except Exception as e:
            result.add_error(f"TRIGGER.{sch}", str(e))
            print(f'  {sch}: TRIGGER ERROR - {e}')


def compare_comments(src, tgt, schemas, result):
    print('\n=== COMMENTS ===')
    for sch in schemas:
        try:
            # discover_table_comments(conn, schema_name) -> list[{"object_type": "TABLE", "table": ..., "comment": ...}]
            tc_src = db2_comment.discover_table_comments(src._conn, sch)
            tc_tgt = db2_comment.discover_table_comments(tgt._conn, sch)

            # discover_column_comments(conn, schema_name) -> list[{"object_type": "COLUMN", "table": ..., "column": ..., "comment": ...}]
            cc_src = db2_comment.discover_column_comments(src._conn, sch)
            cc_tgt = db2_comment.discover_column_comments(tgt._conn, sch)

            # Normalize for comparison
            def norm_table_comment(c):
                return (c.get("object_type"), c.get("table"), c.get("comment"))
            def norm_col_comment(c):
                return (c.get("object_type"), c.get("table"), c.get("column"), c.get("comment"))

            tc_src_norm = sorted([norm_table_comment(c) for c in tc_src])
            tc_tgt_norm = sorted([norm_table_comment(c) for c in tc_tgt])
            cc_src_norm = sorted([norm_col_comment(c) for c in cc_src])
            cc_tgt_norm = sorted([norm_col_comment(c) for c in cc_tgt])

            tbl_match = tc_src_norm == tc_tgt_norm
            col_match = cc_src_norm == cc_tgt_norm

            print(f'  {sch}: TBL_COMMENT_src={tc_src_norm}')
            print(f'  {sch}: TBL_COMMENT_tgt={tc_tgt_norm} MATCH={tbl_match}')
            print(f'  {sch}: COL_COMMENT_src_count={len(cc_src_norm)} tgt_count={len(cc_tgt_norm)} MATCH={col_match}')

            if not tbl_match:
                result.add_mismatch(f"COMMENT_TABLE.{sch}", f"src={tc_src_norm} tgt={tc_tgt_norm}")
            if not col_match:
                result.add_mismatch(f"COMMENT_COL.{sch}", f"src={cc_src_norm} tgt={cc_tgt_norm}")
        except Exception as e:
            result.add_error(f"COMMENT.{sch}", str(e))
            print(f'  {sch}: COMMENT ERROR: {e}')


def _is_ownership_generated(conn, schema_name, grant):
    """Check if a grant is ownership-generated (grantee owns the object).

    Ownership-generated privileges are implicit grants that arise because
    the connected user is the owner of the schema/object. They are not
    real excess grants or security risks.
    """
    try:
        grantee = str(grant.get('grantee', '')).strip().upper()
        obj_type = str(grant.get('object_type', '')).upper()
        obj_name = grant.get('object_name')
        with conn.cursor() as cur:
            if obj_type == 'SCHEMA':
                cur.execute("SELECT OWNER FROM SYSCAT.SCHEMATA WHERE SCHEMANAME=?", (obj_name,))
                row = cur.fetchone()
                if row and str(row[0]).strip().upper() == grantee:
                    return True
            elif obj_type in ('TABLE', 'COLUMN'):
                cur.execute("SELECT OWNER FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (schema_name, obj_name))
                row = cur.fetchone()
                if row and str(row[0]).strip().upper() == grantee:
                    return True
            elif obj_type == 'SEQUENCE':
                cur.execute("SELECT OWNER FROM SYSCAT.SEQUENCES WHERE SEQSCHEMA=? AND SEQNAME=?", (schema_name, obj_name))
                row = cur.fetchone()
                if row and str(row[0]).strip().upper() == grantee:
                    return True
            elif obj_type == 'ROUTINE':
                cur.execute("SELECT OWNER FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?", (schema_name, obj_name))
                row = cur.fetchone()
                if row and str(row[0]).strip().upper() == grantee:
                    return True
    except Exception:
        pass
    return False


def compare_grants(src, tgt, schemas, result, strict_grant_parity=False):
    print('\n=== GRANTS ===')
    for sch in schemas:
        try:
            grants_src = db2_grant.discover_grants(src._conn, sch)
            grants_tgt = db2_grant.discover_grants(tgt._conn, sch)

            # Privilege-presence comparison (ignores grantable flag)
            src_priv = {_norm_grant_privilege_only(g) for g in grants_src}
            tgt_priv = {_norm_grant_privilege_only(g) for g in grants_tgt}

            # Full comparison (includes grantable flag) for detecting grant-option differences
            src_full = {_norm_grant(g) for g in grants_src}
            tgt_full = {_norm_grant(g) for g in grants_tgt}

            missing_priv = src_priv - tgt_priv  # privilege entirely absent from target
            excess_priv = tgt_priv - src_priv   # privilege entirely absent from source

            # Grant-option differences: privileges present in both but grantable differs
            grant_option_diffs = []
            for src_grant in src_full:
                src_priv_key = src_grant[:6]  # without grantable
                # Find matching target grants with same privilege key
                matching_tgt = [g for g in tgt_full if g[:6] == src_priv_key]
                for tgt_grant in matching_tgt:
                    if src_grant[6] != tgt_grant[6]:
                        grant_option_diffs.append({
                            'object_type': src_grant[0],
                            'object_name': src_grant[1],
                            'column': src_grant[2],
                            'grantee': src_grant[3],
                            'grantee_type': src_grant[4],
                            'privilege': src_grant[5],
                            'src_grantable': src_grant[6],
                            'tgt_grantable': tgt_grant[6],
                        })

            print(f'  {sch}: GRANT_src_count={len(grants_src)} GRANT_tgt_count={len(grants_tgt)}')
            print(f'  {sch}: MISSING_PRIVILEGES={len(missing_priv)} EXCESS_PRIVILEGES={len(excess_priv)} GRANT_OPTION_DIFFS={len(grant_option_diffs)}')

            grant_match = len(missing_priv) == 0

            if missing_priv:
                print('    MISSING_PRIVILEGES_IN_TARGET:')
                for m in sorted(missing_priv):
                    print(f'      {m}')
                    result.add_mismatch(f"GRANT_MISSING.{sch}", f"grantee={m[3]} obj={m[1]}.{m[2] or ''} priv={m[5]}")

            if grant_option_diffs:
                print('    GRANT_OPTION_DIFFERENCES:')
                for d in grant_option_diffs:
                    src_go = 'WITH GRANT OPTION' if d['src_grantable'] else 'no grant option'
                    tgt_go = 'WITH GRANT OPTION' if d['tgt_grantable'] else 'no grant option'
                    print(f'      {d["grantee"]} -> {d["object_type"]} {d["object_name"]}{"(" + d["column"] + ")" if d["column"] else ""} ({d["privilege"]}): source={src_go}, target={tgt_go}')
                    if strict_grant_parity:
                        result.add_mismatch(f"GRANT_OPTION.{sch}", f"grantee={d['grantee']} obj={d['object_type']} {d['object_name']}{d['column'] or ''} priv={d['privilege']} source_grantable={d['src_grantable']} target_grantable={d['tgt_grantable']}")
                    else:
                        result.add_warning(f"GRANT_OPTION.{sch}", f"grantee={d['grantee']} obj={d['object_type']} {d['object_name']}{d['column'] or ''} priv={d['privilege']} source_grantable={d['src_grantable']} target_grantable={d['tgt_grantable']}")

            if excess_priv:
                ownership_generated = []
                real_excess = []
                for e in sorted(excess_priv):
                    # Check if this is an ownership-generated privilege
                    # Reconstruct grant dict from the normalized tuple
                    grant_dict = {
                        'object_type': e[0],
                        'object_name': e[1],
                        'column': e[2],
                        'grantee': e[3],
                        'grantee_type': e[4],
                        'privilege': e[5],
                    }
                    if _is_ownership_generated(tgt._conn, sch, grant_dict):
                        ownership_generated.append(e)
                    else:
                        real_excess.append(e)

                if ownership_generated:
                    print('    OWNERSHIP_GENERATED_PRIVILEGES (informational):')
                    for e in ownership_generated:
                        print(f'      {e}')
                    result.add_warning(f"GRANT_OWNERSHIP.{sch}", f"{len(ownership_generated)} ownership-generated privileges (grantee is object owner; not a security risk)")

                if real_excess:
                    print('    EXCESS_PRIVILEGES_IN_TARGET:')
                    for e in real_excess:
                        print(f'      {e}')
                        if strict_grant_parity:
                            result.add_mismatch(f"GRANT_EXCESS.{sch}", f"grantee={e[3]} obj={e[1]}.{e[2] or ''} priv={e[5]} (exists in target but not source)")
                        else:
                            result.add_warning(f"GRANT_EXCESS.{sch}", f"grantee={e[3]} obj={e[1]}.{e[2] or ''} priv={e[5]} (exists in target but not source)")

            print(f'  {sch}: GRANT_MATCH={grant_match}')
        except Exception as e:
            result.add_error(f"GRANT.{sch}", str(e))
            print(f'  {sch}: GRANT ERROR - {e}')


def compare_data_equality(src, tgt, schemas, result):
    print('\n=== DATA EQUALITY (sample) ===')
    for sch in schemas:
        for _, tbl in src.list_objects_with_schema():
            if _ != sch:
                continue
            try:
                src_rows = list(src.export_full(tbl, schema_name=sch))
                tgt_rows = list(tgt.export_full(tbl, schema_name=sch))
                match = src_rows == tgt_rows
                print(f'  {sch}.{tbl}: rows_src={len(src_rows)} tgt={len(tgt_rows)} MATCH={match}')
                if not match:
                    print(f'    DIFF src={src_rows}')
                    print(f'    DIFF tgt={tgt_rows}')
                    result.add_mismatch(f"DATA.{sch}.{tbl}", f"row count src={len(src_rows)} tgt={len(tgt_rows)} or content differs")
            except Exception as e:
                result.add_error(f"DATA.{sch}.{tbl}", str(e))
                print(f'  {sch}.{tbl}: DATA COMPARE ERROR: {e}')


def check_unsupported_skipped(src, tgt, schemas, result):
    print('\n=== UNSUPPORTED / INTENTIONALLY SKIPPED ===')
    for sch in schemas:
        try:
            from core.connectors.db2.objects import synonym as db2_synonym
            syn_src = db2_synonym.discover_synonyms(src._conn, sch)
            syn_tgt = db2_synonym.discover_synonyms(tgt._conn, sch)
            if syn_src:
                print(f'  {sch}: SYNONYMS_src={syn_src} tgt={syn_tgt} SKIPPED={syn_src and not syn_tgt}')
                if syn_src and not syn_tgt:
                    result.add_warning(f"SYNONYM.{sch}", "synonyms exist in source but not target (not migrated)")
            else:
                print(f'  {sch}: SYNONYMS - none in source')
        except ImportError:
            print(f'  {sch}: SYNONYMS - module not available (not checked)')
            result.add_warning(f"SYNONYM.{sch}", "synonym verification not available (module missing)")
        except Exception as e:
            print(f'  {sch}: SYNONYMS check failed: {e}')
            result.add_warning(f"SYNONYM.{sch}", f"check failed: {e}")

        print(f'  {sch}: (manual review for any additional object types)')


def run(config_path):
    result = VerificationResult()
    conn_result = connect_both(config_path, result)
    if conn_result is None:
        print('\n=== VERIFICATION ABORTED ===')
        print(f'RESULT: {result.summary()}')
        print(f'Errors: {result.errors}')
        return result.exit_code()

    src, tgt, src_db, tgt_db, schemas, cfg = conn_result
    strict_grant_parity = cfg.get('validation', {}).get('strict_grant_parity', False)
    print('=== ACCEPTANCE VERIFICATION ===')
    print(f'Source: {src_db} | Target: {tgt_db} | Schemas: {schemas}')
    print(f'Strict grant parity: {strict_grant_parity}')

    compare_partitions(src, tgt, schemas, result)
    compare_pk_fk_index_unique(src, tgt, schemas, result)
    compare_udts_aliases(src, tgt, schemas, result)
    compare_identities_sequences(src, tgt, schemas, result)
    compare_defaults(src, tgt, schemas, result)
    compare_views_mqts(src, tgt, schemas, result)
    compare_routines(src, tgt, schemas, result)
    compare_triggers(src, tgt, schemas, result)
    compare_comments(src, tgt, schemas, result)
    compare_grants(src, tgt, schemas, result, strict_grant_parity)
    compare_data_equality(src, tgt, schemas, result)
    check_unsupported_skipped(src, tgt, schemas, result)

    print('\n=== VERIFICATION SUMMARY ===')
    print(f'Source: {src_db} | Target: {tgt_db} | Schemas: {schemas}')
    print(f'Mismatches: {len(result.mismatches)}')
    print(f'Errors: {len(result.errors)}')
    print(f'Warnings: {len(result.warnings)}')
    print(f'RESULT: {result.summary()}')

    if result.mismatches:
        print('\n--- MISMATCHES ---')
        for m in result.mismatches:
            print(f'  {m}')
    if result.errors:
        print('\n--- ERRORS ---')
        for e in result.errors:
            print(f'  {e}')
    if result.warnings:
        print('\n--- WARNINGS (runtime state / unsupported / excess grants) ---')
        for w in result.warnings:
            print(f'  {w}')

    src.close()
    tgt.close()
    return result.exit_code()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='COMPREHENSIVE Db2 acceptance verification')
    p.add_argument('--config', default='config/db2_e2e_final.yaml', help='Path to YAML config file')
    args = p.parse_args()
    sys.exit(run(args.config))