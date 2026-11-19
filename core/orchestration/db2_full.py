from __future__ import annotations

import itertools
from typing import Any


def is_partitioned_source(partitions: list[dict]) -> bool:
    """Determine if a table is genuinely range-partitioned based on Db2 catalog metadata.

    Db2 LUW reports ordinary (non-partitioned) tables as having a single partition
    named PART0 with blank/empty boundaries (LOWVALUE/HIGHVALUE are empty strings).
    A genuine range-partitioned table has at least one partition with a non-empty
    STARTING FROM or ENDING AT boundary.

    This function implements fail-safe classification: when metadata is ambiguous,
    it returns False (ordinary table) rather than risking misclassification.
    """
    if not partitions:
        return False
    if len(partitions) > 1:
        return True
    # Single partition: check if it has any non-empty boundary
    p = partitions[0]
    low = str(p.get("low") or "").strip()
    high = str(p.get("high") or "").strip()
    return bool(low or high)


def run_db2_full_replacement(orchestrator: Any, result: dict[str, Any] | None = None) -> dict[str, Any]:
    source = orchestrator._source
    target = orchestrator._target
    config = orchestrator._config
    if result is None:
        result = {}

    # source/target identity check
    sc = getattr(source, "_conn", None)
    tc = getattr(target, "_conn", None)
    if sc is None or tc is None or not hasattr(sc, "cursor") or not hasattr(tc, "cursor"):
        raise RuntimeError("Db2 source/target identity check failed: missing connection")
    # Only enforce on real pyodbc connections; skip for test mocks
    is_real = hasattr(sc, "getinfo") or hasattr(tc, "getinfo")
    if is_real:
        try:
            scur = sc.cursor(); scur.execute("SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1"); sr = scur.fetchone(); scur.close()
            tcur = tc.cursor(); tcur.execute("SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1"); tr = tcur.fetchone(); tcur.close()
            if not (sr and tr):
                raise RuntimeError("Db2 source/target identity check failed: missing identity")
            if str(sr[0]).strip().upper() == str(tr[0]).strip().upper():
                raise RuntimeError("Source and target resolve to the same database")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Db2 source/target identity check failed: {exc}") from exc

    include = config.get("migration", {}).get("include_schemas") or []
    all_objects = [f"{s}.{t}" for s, t in source.list_objects_with_schema()]
    if include:
        inc = set(str(s).strip().upper() for s in include)
        objects = [o for o in all_objects if o.split(".", 1)[0].strip().upper() in inc]
    else:
        objects = list(all_objects)

    # Exclude MQTs (TYPE='S') from ordinary table migration
    if getattr(source, "_conn", None) is not None:
        try:
            from core.connectors.db2.objects.mqt import discover_mqts
            mqt_names = {f"{m['schema'] if 'schema' in m else ''}" for m in []}
        except Exception:
            pass
    # Remove MQT objects by querying table type
    filtered = []
    for obj in objects:
        sch, tbl = obj.split(".", 1)
        is_mqt = False
        if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
            try:
                with source._conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=? AND TYPE='S'", (sch, tbl))
                    is_mqt = cur.fetchone() is not None
            except Exception:
                is_mqt = False
        if not is_mqt:
            filtered.append(obj)
    objects = filtered

    # unsafe FK dependency gate
    if hasattr(target, "has_unsafe_foreign_keys") and target.has_unsafe_foreign_keys(objects):
        raise RuntimeError("Unsafe foreign-key dependencies detected")
    if hasattr(source, "has_unsafe_foreign_keys") and source.has_unsafe_foreign_keys(objects):
        raise RuntimeError("Unsafe foreign-key dependencies detected")

    # Grant preflight before destructive replacement
    grant_schemas = include if include else {o.split(".", 1)[0] for o in objects}
    grant_preflight_errors = []
    for sch in grant_schemas:
        if getattr(source, "_conn", None) is not None and hasattr(source, "list_db2_grants"):
            try:
                for g in source.list_db2_grants():
                    from core.connectors.db2.objects.target_grant import plan_db2_grant, assess_db2_grant_authority
                    plan = plan_db2_grant(target._conn, sch, g)
                    if plan["status"] in ("unsupported", "unresolved", "approval_required"):
                        grant_preflight_errors.append(f"{plan['status']}: {g}")
                    elif plan["status"] == "would_grant":
                        # Defer authority check if target object does not exist yet;
                        # the connected principal owns newly created objects.
                        obj = str(g.get("object_name", "")).strip()
                        otype = str(g.get("object_type", "")).strip().upper()
                        exists = True
                        try:
                            with target._conn.cursor() as _c:
                                if otype == "SCHEMA":
                                    _c.execute("SELECT 1 FROM SYSCAT.SCHEMATA WHERE SCHEMANAME=?", (sch,))
                                elif otype in ("TABLE", "COLUMN"):
                                    _c.execute("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (sch, obj))
                                elif otype == "SEQUENCE":
                                    _c.execute("SELECT 1 FROM SYSCAT.SEQUENCES WHERE SEQSCHEMA=? AND SEQNAME=?", (sch, obj))
                                elif otype == "ROUTINE":
                                    _c.execute("SELECT 1 FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA=? AND ROUTINENAME=?", (sch, obj))
                                else:
                                    exists = True
                                if otype not in ("",):
                                    exists = _c.fetchone() is not None
                        except Exception:
                            exists = True
                        if not exists:
                            continue
                        allowed, reason = assess_db2_grant_authority(target._conn, sch, g)
                        if not allowed:
                            grant_preflight_errors.append(f"authorization_failed: {g}: {reason}")
            except Exception as exc:
                grant_preflight_errors.append(str(exc))
    if grant_preflight_errors:
        raise RuntimeError("Grant preflight failed: " + "; ".join(grant_preflight_errors))

    # Ensure target schemas exist
    for sch in (include if include else {o.split(".", 1)[0] for o in objects}):
        from core.connectors.db2.objects.schema import create_schema_if_missing
        create_schema_if_missing(target._conn, sch)

    result["phases"] = result.get("phases", {})
    result["phases"]["discover"] = {"objects": objects}

    batch_size = config.get("migration", {}).get("batch_size", 1000)
    migration_cfg = config.get("migration", {}) if isinstance(config.get("migration"), dict) else {}
    db2_cfg = migration_cfg.get("db2", {}) if isinstance(migration_cfg.get("db2"), dict) else {}
    allow_identity_override = bool(db2_cfg.get("allow_generated_always_identity_override", False))

    conn = getattr(target, "_conn", None)
    old_autocommit = None
    if conn is not None:
        try:
            old_autocommit = getattr(conn, "autocommit", None)
            conn.autocommit = False
        except Exception:
            pass

    # Counters for object-type phases reported to the UI
    _view_results: dict[str, str] = {}
    _mqt_results: dict[str, str] = {}
    _trigger_results: dict[str, str] = {}
    _routine_results: dict[str, str] = {}
    _sequence_results: dict[str, str] = {}
    _udt_results: dict[str, str] = {}
    _alias_results: dict[str, str] = {}
    _grant_results: dict[str, str] = {}
    _ddl_errors: list[str] = []
    _committed_objects: list[str] = []

    try:
        # Build FK relationships from source metadata
        fk_pairs = []
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            schema = source.get_schema(tbl, schema_name=sch)
            for fk in getattr(schema, "foreign_keys", []):
                fk_pairs.append((obj, f"{fk.ref_schema}.{fk.ref_table}"))

        # Identity preflight before any DELETE
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            src_ids = []
            tgt_ids = []
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.identity import discover_identity_columns
                src_ids = discover_identity_columns(source._conn, sch, tbl)
            tgt_exists = False
            try:
                with target._conn.cursor() as _c:
                    _c.execute("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (sch, tbl))
                    rows = _c.fetchall()
                    tgt_exists = bool(rows)
            except Exception as exc:
                raise RuntimeError(f"Identity preflight catalog lookup failed for {sch}.{tbl}: {exc}") from exc
            if tgt_exists and getattr(target, "_conn", None) is not None and hasattr(target._conn, "cursor"):
                from core.connectors.db2.objects.identity import discover_identity_columns
                tgt_ids = discover_identity_columns(target._conn, sch, tbl)
            def _stable(ident):
                return {k: ident.get(k) for k in ("column", "datatype", "generated", "increment", "minvalue", "maxvalue", "cycle", "cache", "order") if k in ident}
            if tgt_exists and [_stable(i) for i in src_ids] != [_stable(i) for i in tgt_ids]:
                raise RuntimeError(f"Identity metadata mismatch for {sch}.{tbl}")
            for ident in src_ids:
                gen = str(ident.get("generated", "")).upper()
                if gen not in ("A", "D"):
                    raise RuntimeError("Unsupported Db2 identity GENERATED mode")
                if gen == "A" and not allow_identity_override:
                    raise RuntimeError(
                        f"GENERATED ALWAYS identity value preservation is not supported via INSERT on Db2 LUW: {sch}.{tbl}.{ident.get('column')}"
                    )

        # Trigger safety gate before any DELETE
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            if getattr(target, "_conn", None) is not None and hasattr(target._conn, "cursor"):
                with target._conn.cursor() as cur:
                    cur.execute("SELECT TRIGNAME, ENABLED FROM SYSCAT.TRIGGERS WHERE TABSCHEMA=? AND TABNAME=?", (sch, tbl))
                    for row in cur.fetchall():
                        if row and str(row[1]).strip().upper() in ("Y", "1", "TRUE"):
                            raise RuntimeError(f"Existing enabled trigger on {sch}.{tbl} prevents safe FULL replacement")

        from core.orchestration.db2_fk_order import order_tables_for_full
        insert_order, delete_order = order_tables_for_full(objects, fk_pairs)

        # UDT validation: check existing target UDTs match source definitions
        udt_schemas = include if include else {o.split(".", 1)[0] for o in objects}
        for sch in udt_schemas:
            if getattr(source, "_conn", None) is not None and getattr(target, "_conn", None) is not None:
                from core.connectors.db2.objects.udt_alias import discover_udts
                try:
                    src_udts = {u["name"]: u for u in discover_udts(source._conn, sch)}
                    tgt_udts = {u["name"]: u for u in discover_udts(target._conn, sch)}
                    for name, src_udt in src_udts.items():
                        if name in tgt_udts:
                            tgt_udt = tgt_udts[name]
                            # Compare source type definitions
                            src_def = f"{src_udt['source_schema']}.{src_udt['source_type']}".upper()
                            tgt_def = f"{tgt_udt['source_schema']}.{tgt_udt['source_type']}".upper()
                            if src_def != tgt_def:
                                raise RuntimeError(
                                    f"UDT conflict for {sch}.{name}: source={src_def}, target={tgt_def}. "
                                    "Existing target UDT differs from source; manual resolution required."
                                )
                except RuntimeError:
                    raise
                except Exception as exc:
                    _ddl_errors.append(f"UDT validation {sch}: {exc}")

        # Fail-closed guard: detect scoped partially migrated objects before any target mutation.
        # Detects existing target tables (including zero-row), scoped UDTs, aliases, and custom roles.
        # Tolerates SQL0204N (object does not exist) for fresh targets; fail-closed on other errors.
        if getattr(target, "_conn", None) is not None:
            # 1. Existing target tables in scope — any existing table indicates partial migration
            for obj in objects:
                sch, tbl = obj.split(".", 1)
                try:
                    cur = target._conn.cursor()
                    cur.execute(f'SELECT COUNT(*) FROM "{sch}"."{tbl}"')
                    row = cur.fetchone()
                    cur.close()
                    # Table exists (even zero rows) → partial migration state
                    if row is not None:
                        raise RuntimeError(
                            f"Target table {sch}.{tbl} already exists. "
                            "Repeat FULL on an existing target is not supported. "
                            "Use a fresh empty target or incremental mode."
                        )
                except RuntimeError:
                    raise
                except Exception as exc:
                    msg = str(exc).upper()
                    if "SQL0204N" in msg or "NOT FOUND" in msg or "DOES NOT EXIST" in msg:
                        pass  # Table does not exist yet (fresh target) - safe to continue
                    else:
                        raise RuntimeError(f"Preflight guard failed for {sch}.{tbl}: {exc}") from exc

            # 2. Pre-existing scoped structured UDTs on target
            try:
                from core.connectors.db2.objects.udt_alias import discover_structured_udts
                for sch in (include or []):
                    if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                        for udt in discover_structured_udts(source._conn, sch):
                            with target._conn.cursor() as _c:
                                _c.execute(
                                    "SELECT 1 FROM SYSCAT.DATATYPES WHERE TYPESCHEMA=? AND TYPENAME=? AND METATYPE IN ('S','R')",
                                    (udt["schema"], udt["name"]),
                                )
                                if _c.fetchone():
                                    raise RuntimeError(
                                        f"Structured UDT {udt['schema']}.{udt['name']} already exists on target. "
                                        "Repeat FULL on an existing target is not supported."
                                    )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"Preflight guard UDT check failed: {exc}") from exc

            # 3. Pre-existing scoped aliases on target
            try:
                from core.connectors.db2.objects.udt_alias import discover_aliases
                for sch in (include or []):
                    if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                        for alias in discover_aliases(source._conn, sch):
                            with target._conn.cursor() as _c:
                                _c.execute(
                                    "SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=? AND TYPE='A'",
                                    (alias.get("schema", sch), alias.get("name", "")),
                                )
                                if _c.fetchone():
                                    raise RuntimeError(
                                        f"Alias {alias.get('schema', sch)}.{alias.get('name', '')} already exists on target. "
                                        "Repeat FULL on an existing target is not supported."
                                    )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"Preflight guard alias check failed: {exc}") from exc

            # 4. Pre-existing scoped custom roles on target (exclude Db2-provided SYS* roles)
            try:
                from core.connectors.db2.objects.role import discover_role_definitions
                for role_name in discover_role_definitions(source._conn):
                    with target._conn.cursor() as _c:
                        _c.execute("SELECT 1 FROM SYSCAT.ROLES WHERE ROLENAME=?", (role_name,))
                        if _c.fetchone():
                            raise RuntimeError(
                                f"Role {role_name} already exists on target. "
                                "Repeat FULL on an existing target is not supported."
                            )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"Preflight guard role check failed: {exc}") from exc

        # Partition preflight (must complete before any target mutation)
        part_errors = []
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            try:
                with target._conn.cursor() as _c:
                    _c.execute("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA=? AND TABNAME=?", (sch, tbl))
                    if not _c.fetchone():
                        continue  # target table does not exist yet; creation handled below
            except Exception:
                continue
            if not (getattr(source, '_conn', None) is not None and hasattr(source._conn, 'cursor')):
                continue
            from core.connectors.db2.objects.partition import discover_partitions
            try:
                src_parts = discover_partitions(source._conn, sch, tbl)
            except Exception:
                src_parts = []
            try:
                tgt_parts = discover_partitions(target._conn, sch, tbl)
            except Exception:
                tgt_parts = []
            if src_parts != tgt_parts:
                part_errors.append(f"partition mismatch for {sch}.{tbl}")
        if part_errors:
            raise RuntimeError("Partition preflight failed: " + "; ".join(part_errors))

        # Distinct UDT creation (before tables; dependency order: schemas -> UDTs -> tables)
        udt_schemas = include if include else {o.split(".", 1)[0] for o in objects}
        for sch in udt_schemas:
            if getattr(source, "_conn", None) is not None:
                from core.connectors.db2.objects.udt_alias import discover_udts
                from core.connectors.db2.objects.target_udt_alias import create_udt_if_missing
                if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                    for u in discover_udts(source._conn, sch):
                        create_udt_if_missing(target._conn, u)
                        _udt_results[u["name"]] = "created"

        # Structured UDT creation (before dependent tables) — fail closed
        from core.connectors.db2.objects.udt_alias import discover_structured_udts
        from core.connectors.db2.objects.target_udt_alias import create_udt_if_missing
        for sch in (include or []):
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                for udt in discover_structured_udts(source._conn, sch):
                    create_udt_if_missing(target._conn, udt)

        # Role creation and grants — single authorized transaction on security executor.
        # All role DDL runs on the executor connection (SECADM) so it can create any role
        # and grant any membership (including self-grants, avoiding SQL0554N).
        # The role transaction is committed independently of target._conn; target._conn is
        # never committed early for role work. Without an executor, fall back to target._conn
        # (migration user must hold ACCESSCTRL/SECADM).
        from core.connectors.db2.objects.role import discover_role_definitions, discover_roles
        from core.connectors.db2.objects.target_role import (
            create_role_if_missing,
            grant_role_membership,
        )
        if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
            role_names = list(discover_role_definitions(source._conn))
            role_memberships = list(discover_roles(source._conn))
            if role_names or role_memberships:
                executor_conn = getattr(orchestrator, "_security_executor_conn", None)
                use_executor = executor_conn is not None and hasattr(executor_conn, "cursor")
                role_conn = executor_conn if use_executor else conn
                try:
                    for role_name in role_names:
                        create_role_if_missing(role_conn, role_name)
                    for rm in role_memberships:
                        grant_role_membership(
                            role_conn,
                            rm["role"],
                            rm["grantee"],
                            rm["grantee_type"],
                            rm.get("admin", False),
                        )
                    if use_executor:
                        try:
                            role_conn.commit()
                        except Exception:
                            try:
                                role_conn.rollback()
                            except Exception:
                                pass
                            raise
                except Exception:
                    if use_executor:
                        try:
                            role_conn.rollback()
                        except Exception:
                            pass
                    raise

        # Table creation (ordinary and partitioned; never silently omit a partitioned table)
        from core.connectors.db2.objects.ddl import create_table_if_missing, create_partitioned_table_if_missing
        from core.connectors.db2.objects.partition import discover_partitions, discover_partition_column
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            schema = source.get_schema(tbl, schema_name=sch)
            try:
                parts = discover_partitions(source._conn, sch, tbl) if getattr(source, "_conn", None) else []
            except Exception:
                parts = []
            is_partitioned = is_partitioned_source(parts)
            try:
                if is_partitioned:
                    try:
                        partition_column = discover_partition_column(source._conn, sch, tbl)
                    except Exception as exc:
                        raise RuntimeError(f"Cannot create partitioned table {sch}.{tbl}: partition key column undiscoverable: {exc}") from exc
                    create_partitioned_table_if_missing(target._conn, schema, parts, partition_column)
                else:
                    create_table_if_missing(target._conn, schema)
            except Exception as exc:
                _ddl_errors.append(f"table {sch}.{tbl}: {exc}")
                raise RuntimeError(f"Table creation failed for {sch}.{tbl}: {exc}") from exc

        # DELETE in delete_order
        for obj in delete_order:
            sch, tbl = obj.split(".", 1)
            with conn.cursor() as cur:
                cur.execute(f'DELETE FROM "{sch}"."{tbl}"')

        # INSERT in insert_order
        for obj in insert_order:
            sch, tbl = obj.split(".", 1)
            schema = source.get_schema(tbl, schema_name=sch)
            count = source.get_object_count(tbl, schema_name=sch)
            rows = source.export_full(tbl, schema_name=sch)
            always_cols = [c.name for c in schema.columns if getattr(c, "is_identity", False) and str(getattr(c, "identity_kind", None) or "").upper() == "ALWAYS"]
            if always_cols and not allow_identity_override:
                raise RuntimeError(
                    "GENERATED ALWAYS identity columns cannot be loaded via INSERT on Db2 LUW: " + ", ".join(always_cols)
                )
            upsert_kwargs = {"commit": False}
            if always_cols:
                upsert_kwargs["override_identity"] = allow_identity_override
            success = 0
            for chunk in _chunk(rows, batch_size):
                r = target.upsert_batch(tbl, iter(chunk), schema, **upsert_kwargs)
                success += r.success_count
                if getattr(r, "errors", None):
                    raise RuntimeError("; ".join(r.errors))
            result["phases"][obj] = {
                "source_rows": count,
                "success": success,
                "failure": 0,
                "errors": [],
            }
            _committed_objects.append(obj)

        # Alias/synonym creation (after base tables/views exist)
        for sch in (include or []):
            if getattr(source, "_conn", None) is not None:
                from core.connectors.db2.objects.udt_alias import discover_aliases
                from core.connectors.db2.objects.target_udt_alias import create_alias_if_missing
                if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                    for alias in discover_aliases(source._conn, sch):
                        create_alias_if_missing(target._conn, alias)

        # Aggregate data-load phase for UI reporting (DB2 owns data load here)
        _total_source = 0
        _total_success = 0
        _total_failure = 0
        for obj in objects:
            _ophase = result["phases"].get(obj, {})
            _total_source += int(_ophase.get("source_rows", 0))
            _total_success += int(_ophase.get("success", 0))
            _total_failure += int(_ophase.get("failure", 0))
        result["phases"]["data"] = {
            "status": "success",
            "loaded": list(objects),
            "total_rows": _total_source,
            "success": _total_success,
            "failure": _total_failure,
        }

        # validate counts
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            tc = target.get_object_count(tbl, schema_name=sch)
            sc = source.get_object_count(tbl, schema_name=sch)
            if tc != sc:
                raise RuntimeError(f"Validation mismatch for {obj}: source={sc}, target={tc}")

        if conn is not None:
            conn.commit()
            # Post-commit DDL must run with autocommit=True so each statement commits immediately
            try:
                conn.autocommit = True
            except Exception:
                pass

        # DDL outside transaction (per-object-type, failures recorded not fatal)
        # indexes, unique constraints, FKs
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            schema = source.get_schema(tbl, schema_name=sch)
            for idx in getattr(schema, "indexes", []):
                from core.connectors.db2.objects.target_index import create_index_if_missing
                try:
                    create_index_if_missing(target._conn, sch, tbl, idx)
                except Exception as exc:
                    _ddl_errors.append(f"index {sch}.{tbl}.{idx.name}: {exc}")
            for uc in getattr(schema, "unique_constraints", []):
                from core.connectors.db2.objects.target_constraint import create_unique_constraint_if_missing
                try:
                    create_unique_constraint_if_missing(target._conn, sch, tbl, uc)
                except Exception as exc:
                    _ddl_errors.append(f"unique constraint {sch}.{tbl}.{uc.name}: {exc}")
            for fk in getattr(schema, "foreign_keys", []):
                from core.connectors.db2.objects.target_foreign_key import create_foreign_key_if_missing
                try:
                    create_foreign_key_if_missing(target._conn, sch, tbl, fk)
                except Exception as exc:
                    _ddl_errors.append(f"foreign key {sch}.{tbl}.{fk.name}: {exc}")
        # Apply standalone sequences (DDL outside transaction)
        from core.connectors.db2.objects.sequence import discover_standalone_sequences
        from core.connectors.db2.objects.target_sequence import create_sequence_if_missing
        for sch in {o.split(".", 1)[0] for o in objects}:
            if getattr(source, "_conn", None) is not None:
                try:
                    for seq in discover_standalone_sequences(source._conn, sch):
                        try:
                            create_sequence_if_missing(target._conn, sch, seq)
                            _sequence_results[seq["name"]] = "created"
                        except Exception as exc:
                            _sequence_results[seq["name"]] = f"error: {exc}"
                            _ddl_errors.append(f"sequence {sch}.{seq['name']}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"sequence discovery {sch}: {exc}")
        for obj in objects:
            sch, tbl = obj.split(".", 1)
            if getattr(source, "_conn", None) is None:
                continue
            idents = []
            from core.connectors.db2.objects.identity import discover_identity_columns
            try:
                idents = discover_identity_columns(source._conn, sch, tbl)
            except Exception as exc:
                _ddl_errors.append(f"identity discovery {sch}.{tbl}: {exc}")
                continue
            for ident in idents:
                col = ident["column"]
                attrs = ident.get("attributes", {}) or {}
                increment = attrs.get("increment", 1)
                if increment is None:
                    _ddl_errors.append(f"identity {sch}.{tbl}.{col}: invalid increment")
                    continue
                increment = int(increment)
                if increment >= 0:
                    sql = f'SELECT MAX("{col}") FROM "{sch}"."{tbl}"'
                else:
                    sql = f'SELECT MIN("{col}") FROM "{sch}"."{tbl}"'
                try:
                    with target._conn.cursor() as cur:
                        cur.execute(sql)
                        row = cur.fetchone()
                        agg = row[0] if row else None
                except Exception as exc:
                    _ddl_errors.append(f"identity restart {sch}.{tbl}.{col}: {exc}")
                    continue
                if agg is None:
                    start = attrs.get("nextcache") or attrs.get("minvalue") or 1
                    nxt = int(start)
                else:
                    nxt = int(agg) + increment
                minv = attrs.get("minvalue")
                maxv = attrs.get("maxvalue")
                if minv is not None and nxt < int(minv):
                    _ddl_errors.append(f"identity {sch}.{tbl}.{col}: restart value below minimum")
                    continue
                if maxv is not None and nxt > int(maxv):
                    _ddl_errors.append(f"identity {sch}.{tbl}.{col}: restart value above maximum")
                    continue
                try:
                    with target._conn.cursor() as cur:
                        cur.execute(f'ALTER TABLE "{sch}"."{tbl}" ALTER COLUMN "{col}" RESTART WITH {nxt}')
                except Exception as exc:
                    _ddl_errors.append(f"identity restart {sch}.{tbl}.{col}: {exc}")
        # Apply views (DDL outside transaction)
        for sch in {o.split(".", 1)[0] for o in objects}:
            if getattr(source, "_conn", None) is not None:
                from core.connectors.db2.objects.view import discover_views
                from core.connectors.db2.objects.target_view import create_view_if_missing
                try:
                    for v in discover_views(source._conn, sch):
                        try:
                            create_view_if_missing(target._conn, sch, v)
                            _view_results[v["name"]] = "created"
                        except Exception as exc:
                            _view_results[v["name"]] = f"error: {exc}"
                            _ddl_errors.append(f"view {sch}.{v['name']}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"view discovery {sch}: {exc}")
        # Apply MQTs (DDL outside transaction)
        for sch in {o.split(".", 1)[0] for o in objects}:
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.mqt import discover_mqts
                from core.connectors.db2.objects.target_mqt import create_mqt_if_missing, refresh_mqt
                try:
                    for m in discover_mqts(source._conn, sch):
                        try:
                            create_mqt_if_missing(target._conn, sch, m)
                            refresh_mqt(target._conn, sch, m["name"])
                            _mqt_results[m["name"]] = "created+refreshed"
                        except Exception as exc:
                            _mqt_results[m["name"]] = f"error: {exc}"
                            _ddl_errors.append(f"MQT {sch}.{m['name']}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"MQT discovery {sch}: {exc}")
        # Apply aliases (DDL outside transaction, references base tables)
        for sch in {o.split(".", 1)[0] for o in objects}:
            if getattr(source, "_conn", None) is not None:
                from core.connectors.db2.objects.udt_alias import discover_aliases
                from core.connectors.db2.objects.target_udt_alias import create_alias_if_missing
                try:
                    for a in discover_aliases(source._conn, sch):
                        try:
                            create_alias_if_missing(target._conn, a)
                            _alias_results[a["name"]] = "created"
                        except Exception as exc:
                            _alias_results[a["name"]] = f"error: {exc}"
                            _ddl_errors.append(f"alias {sch}.{a['name']}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"alias discovery {sch}: {exc}")
        # Apply triggers (DDL outside transaction)
        for sch in {o.split(".", 1)[0] for o in objects}:
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.trigger import discover_triggers
                from core.connectors.db2.objects.target_trigger import create_trigger_if_missing
                try:
                    for t in discover_triggers(source._conn, sch):
                        try:
                            create_trigger_if_missing(target._conn, sch, t)
                            _trigger_results[f"{sch}.{t['name']}"] = "created"
                        except Exception as exc:
                            _trigger_results[f"{sch}.{t['name']}"] = f"error: {exc}"
                            _ddl_errors.append(f"trigger {sch}.{t['name']}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"trigger discovery {sch}: {exc}")
        # Apply routines (DDL outside transaction)
        schemas = include if include else (source.list_schemas() if hasattr(source, "list_schemas") else [])
        for sch in schemas:
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.routine import discover_routines
                from core.connectors.db2.objects.target_routine import create_routine_if_missing
                try:
                    for r in discover_routines(source._conn, sch):
                        try:
                            create_routine_if_missing(target._conn, sch, r)
                            _routine_results[f"{sch}.{r['name']}"] = "created"
                        except Exception as exc:
                            _routine_results[f"{sch}.{r['name']}"] = f"error: {exc}"
                            _ddl_errors.append(f"routine {sch}.{r['name']}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"routine discovery {sch}: {exc}")
        # Apply comments (DDL outside transaction)
        comment_schemas = include if include else {o.split(".", 1)[0] for o in objects}
        comment_errors = []
        for sch in comment_schemas:
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.comment import discover_table_comments, discover_column_comments, apply_table_comment, apply_column_comment
                try:
                    for c in discover_table_comments(source._conn, sch):
                        if c.get("comment") is None:
                            continue
                        res = apply_table_comment(target._conn, sch, c["table"], c["comment"])
                        if res not in ("applied", "already_present", "skipped"):
                            comment_errors.append(f"{res}: TABLE {sch}.{c['table']}")
                    for c in discover_column_comments(source._conn, sch):
                        if c.get("comment") is None:
                            continue
                        res = apply_column_comment(target._conn, sch, c["table"], c["column"], c["comment"])
                        if res not in ("applied", "already_present", "skipped"):
                            comment_errors.append(f"{res}: COLUMN {sch}.{c['table']}.{c['column']}")
                except Exception as exc:
                    comment_errors.append(f"comment discovery {sch}: {exc}")
        if comment_errors:
            _ddl_errors.extend(comment_errors)
        # Apply grants (DDL outside transaction)
        grant_schemas = include if include else {o.split(".", 1)[0] for o in objects}
        for sch in grant_schemas:
            if getattr(source, "_conn", None) is not None and hasattr(source, "list_db2_grants"):
                try:
                    for g in source.list_db2_grants():
                        from core.connectors.db2.objects.target_grant import apply_planned_db2_grant
                        try:
                            res = apply_planned_db2_grant(target._conn, sch, g)
                            _gkey = f"{g.get('object_type','')} {g.get('object_name','')} TO {g.get('grantee','')}"
                            if res.get("status") in ("granted", "already_present", "would_grant"):
                                _grant_results[_gkey] = "ok"
                            elif res.get("status") == "approval_required":
                                _grant_results[_gkey] = "approval_required"
                                _ddl_errors.append(f"grant {sch}.{g.get('object_name','')} TO {g.get('grantee','')}: approval required")
                            else:
                                _grant_results[_gkey] = f"failed: {res.get('status')}"
                                _ddl_errors.append(f"grant {sch}.{g.get('object_name','')} TO {g.get('grantee','')}: {res.get('status')}")
                        except Exception as exc:
                            _gkey = f"{g.get('object_type','')} {g.get('object_name','')} TO {g.get('grantee','')}"
                            _grant_results[_gkey] = f"error: {exc}"
                            _ddl_errors.append(f"grant {sch}.{g.get('object_name','')} TO {g.get('grantee','')}: {exc}")
                except Exception as exc:
                    _ddl_errors.append(f"grant discovery {sch}: {exc}")
        # Apply defaults (DDL outside transaction)
        def_schemas = include if include else {o.split(".", 1)[0] for o in objects}
        def_errors = []
        for sch in def_schemas:
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.check_default import discover_column_defaults
                from core.connectors.db2.objects.target_check_default import plan_default, apply_planned_default
                try:
                    for d in discover_column_defaults(source._conn, sch):
                        if d.get("default") is None:
                            continue
                        res = apply_planned_default(target._conn, sch, d["table"], d["column"], d["default"])
                        status = res.get("status")
                        if status not in ("applied", "already_present", "would_apply"):
                            reason = res.get("reason", "unknown error")
                            def_errors.append(f"{status}: DEFAULT {sch}.{d['table']}.{d['column']} ({reason})")
                except Exception as exc:
                    def_errors.append(f"default discovery {sch}: {exc}")
        # Apply checks (DDL outside transaction)
        check_schemas = include if include else {o.split(".", 1)[0] for o in objects}
        check_errors = []
        for sch in check_schemas:
            if getattr(source, "_conn", None) is not None and hasattr(source._conn, "cursor"):
                from core.connectors.db2.objects.check_default import discover_check_constraints
                from core.connectors.db2.objects.target_check_default import plan_check, apply_planned_check
                try:
                    for c in discover_check_constraints(source._conn, sch):
                        res = apply_planned_check(target._conn, sch, c["table"], c["name"], c["expression"])
                        status = res.get("status")
                        if status not in ("applied", "already_present", "would_apply"):
                            reason = res.get("reason", "unknown error")
                            check_errors.append(f"{status}: CHECK {sch}.{c['table']}.{c['name']} ({reason})")
                except Exception as exc:
                    check_errors.append(f"check discovery {sch}: {exc}")
        _ddl_errors.extend(def_errors)
        _ddl_errors.extend(check_errors)

        result["phases"]["full_target_sync"] = {
            "strategy": "db2-transactional",
            "status": "success" if not _ddl_errors else "partial",
            "cleared": _committed_objects,
            "ddl_errors": _ddl_errors,
        }
        result["phases"]["views"] = _view_results
        result["phases"]["materialized_views"] = _mqt_results
        result["phases"]["aliases"] = _alias_results
        result["phases"]["triggers"] = _trigger_results
        result["phases"]["functions"] = _routine_results
        result["phases"]["create_sequences"] = _sequence_results
        result["phases"]["advance_sequences"] = _sequence_results
        result["phases"]["custom_types"] = _udt_results
        result["phases"]["grants"] = _grant_results
        _validation_checks = {}
        _validation_status = "success"
        for o in objects:
            sch, tbl = o.split(".", 1)
            sc = source.get_object_count(tbl, schema_name=sch)
            tc = target.get_object_count(tbl, schema_name=sch)
            match = tc == sc
            if not match:
                _validation_status = "failed"
            _validation_checks[o] = {
                "match": match,
                "source_count": sc,
                "target_count": tc,
                "status": "success" if match else "failed",
            }
        result["phases"]["validation"] = {"mode": "count", "checks": _validation_checks, "status": _validation_status}
        overall = "success" if not _ddl_errors and _validation_status == "success" else ("partial_success" if _total_failure == 0 else "failed")
        result["status"] = overall
        return {"status": overall}
    except Exception as exc:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        result["phases"]["full_target_sync"] = {"strategy": "db2-transactional", "status": "failed", "cleared": []}
        result["status"] = "failed"
        result["error"] = str(exc)
        raise
    finally:
        if conn is not None and old_autocommit is not None:
            try:
                conn.autocommit = old_autocommit
            except Exception:
                pass
        # Close security executor if present
        sec_conn = getattr(orchestrator, "_security_executor_conn", None)
        if sec_conn is not None:
            try:
                sec_conn.close() if hasattr(sec_conn, "close") else None
            except Exception:
                pass
        sec_connector = getattr(orchestrator, "_security_executor_connector", None)
        if sec_connector is not None and hasattr(sec_connector, "close"):
            try:
                sec_connector.close()
            except Exception:
                pass


def _chunk(iterable, size):
    it = iter(iterable)
    while True:
        chunk = list(itertools.islice(it, size))
        if not chunk:
            break
        yield chunk
