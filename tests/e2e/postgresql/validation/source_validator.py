"""Source structural validation for PostgreSQL E2E Phase B.

Compares the live PostgreSQL catalog against the expected fixture state defined
in ``expected.py``.  Produces a ``ValidationReport`` with per-category phases
and individual check results.

Key PostgreSQL adaptations vs. the MSSQL ``SourceValidator``:
  * Identifiers are lowercase when unquoted — all expectations use lowercase.
  * No DEFAULT constraints table — defaults live on the column row.
  * No extended properties — PostgreSQL has COMMENT.
  * Partition children are discovered via ``pg_inherits`` (not functions/schemes).
  * Functions and procedures share ``pg_proc``; ``prokind`` distinguishes them.
  * RLS policies are a PostgreSQL-first concept.
"""
from __future__ import annotations

import time

from tests.e2e.postgresql.validation.catalog import PostgreSQLCatalog
from tests.e2e.postgresql.validation.expected import (
    CHECK_CONSTRAINTS,
    COMMENT_COUNT,
    FOREIGN_KEYS,
    FUNCTIONS,
    GRANTS,
    INDEXES,
    PARTITIONS,
    PROCEDURES,
    RLS_POLICIES,
    ROLES,
    ROW_COUNTS,
    SCHEMA_NAME,
    SEQUENCES,
    TABLES,
    TRIGGERS,
    UNIQUE_CONSTRAINTS,
    USER_TYPES,
    VIEWS,
)
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


class SourceValidator:
    """Validates a PostgreSQL source database against the E2E expected fixture."""

    def __init__(self, catalog: PostgreSQLCatalog, database_name: str = "") -> None:
        self._cat = catalog
        self._db_name = database_name
        self._report = ValidationReport(database=database_name)

    def validate(self) -> ValidationReport:
        start = time.time()
        self._validate_database()
        self._validate_schemas()
        self._validate_tables()
        self._validate_columns()
        self._validate_primary_keys()
        self._validate_foreign_keys()
        self._validate_unique_constraints()
        self._validate_check_constraints()
        self._validate_indexes()
        self._validate_views()
        self._validate_functions()
        self._validate_procedures()
        self._validate_triggers()
        self._validate_sequences()
        self._validate_partitions()
        self._validate_user_types()
        self._validate_security()
        self._validate_rls_policies()
        self._validate_grants()
        self._validate_comments()
        self._validate_row_counts()
        self._validate_trigger_data()
        self._report.total_duration_s = time.time() - start
        return self._report

    # ------------------------------------------------------------------ #
    # Phase scaffolding
    # ------------------------------------------------------------------ #

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _validate_database(self) -> None:
        phase = self._new_phase("database")
        t0 = time.time()
        db_exists = self._db_name and self._cat.database_name == self._db_name
        phase.add_check(CheckResult(
            name=f"database:{self._db_name or '<unknown>'}",
            status=STATUS_PASS if db_exists else STATUS_FAIL,
            expected=True,
            actual=db_exists,
            message=(
                f"Database '{self._db_name}' is accessible"
                if db_exists else
                f"Database '{self._db_name}' NOT FOUND / connection failed"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_schemas(self) -> None:
        phase = self._new_phase("schemas")
        t0 = time.time()
        exists = self._cat.schema_exists(SCHEMA_NAME)
        phase.add_check(CheckResult(
            name=f"schema:{SCHEMA_NAME}",
            status=STATUS_PASS if exists else STATUS_FAIL,
            expected=True,
            actual=exists,
            message=f"Schema '{SCHEMA_NAME}' {'exists' if exists else 'MISSING'}",
        ))
        phase.duration_s = time.time() - t0

    def _validate_tables(self) -> None:
        phase = self._new_phase("tables")
        t0 = time.time()
        actual = set(self._cat.get_base_tables(SCHEMA_NAME))
        expected = set(TABLES.keys())
        missing = expected - actual
        extra = actual - expected
        phase.add_check(CheckResult(
            name="table_set",
            status=STATUS_PASS if not missing and not extra else STATUS_FAIL,
            expected=sorted(expected),
            actual=sorted(actual),
            message=(
                f"Expected {len(expected)} tables, got {len(actual)}. "
                f"Missing: {sorted(missing) or 'none'}, "
                f"Extra: {sorted(extra) or 'none'}"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_columns(self) -> None:
        phase = self._new_phase("columns")
        t0 = time.time()
        all_ok = True
        for table_name, expected_tbl in TABLES.items():
            actual_cols = {c["name"]: c for c in self._cat.get_columns(SCHEMA_NAME, table_name)}
            expected_cols = {c.name: c for c in expected_tbl.columns}
            missing = set(expected_cols) - set(actual_cols)
            extra = set(actual_cols) - set(expected_cols)

            detail: dict = {}
            if missing:
                detail["missing"] = sorted(missing)
                all_ok = False
            if extra:
                detail["extra"] = sorted(extra)
                all_ok = False

            mismatches: list[str] = []
            for col_name, exp_col in expected_cols.items():
                if col_name not in actual_cols:
                    continue
                act_col = actual_cols[col_name]

                if act_col["is_identity"] != exp_col.is_identity:
                    mismatches.append(
                        f"{col_name}: identity expected {exp_col.is_identity}, "
                        f"got {act_col['is_identity']}"
                    )
                    all_ok = False

                if exp_col.udt_name is not None:
                    # PostgreSQL returns schema-qualified type names for UDTs
                    # (e.g. "training.orderstatus"); compare by stripped suffix.
                    actual_udt = act_col["base_type"]
                    actual_udt_stripped = actual_udt.split(".")[-1] if "." in actual_udt else actual_udt
                    if actual_udt_stripped != exp_col.udt_name:
                        mismatches.append(
                            f"{col_name}: UDT expected '{exp_col.udt_name}', "
                            f"got '{actual_udt}'"
                        )
                        all_ok = False
                elif act_col["base_type"] != exp_col.base_type:
                    mismatches.append(
                        f"{col_name}: type expected '{exp_col.base_type}', "
                        f"got '{act_col['base_type']}'"
                    )
                    all_ok = False

                if act_col["is_nullable"] != exp_col.nullable:
                    mismatches.append(
                        f"{col_name}: nullable expected {exp_col.nullable}, "
                        f"got {act_col['is_nullable']}"
                    )
                    all_ok = False

                if act_col["is_generated"] != exp_col.is_generated:
                    mismatches.append(
                        f"{col_name}: generated expected {exp_col.is_generated}, "
                        f"got {act_col['is_generated']}"
                    )
                    all_ok = False

            if mismatches:
                detail["mismatches"] = mismatches

            phase.add_check(CheckResult(
                name=f"columns:{table_name}",
                status=STATUS_PASS if not mismatches and not missing and not extra else STATUS_FAIL,
                expected={c: e.base_type for c, e in expected_cols.items()},
                details=detail,
            ))
        phase.duration_s = time.time() - t0
        if not all_ok:
            phase.status = STATUS_FAIL

    def _validate_primary_keys(self) -> None:
        phase = self._new_phase("primary_keys")
        t0 = time.time()
        for table_name, expected_tbl in TABLES.items():
            actual = self._cat.get_primary_key(SCHEMA_NAME, table_name)
            if expected_tbl.pk_name:
                ok = actual is not None and actual["name"] == expected_tbl.pk_name
            else:
                ok = actual is not None
            cols_ok = actual is not None and actual["columns"] == expected_tbl.pk_columns
            phase.add_check(CheckResult(
                name=f"pk:{table_name}",
                status=STATUS_PASS if ok and cols_ok else STATUS_FAIL,
                expected={
                    "name": expected_tbl.pk_name,
                    "columns": expected_tbl.pk_columns,
                },
                actual={
                    "name": actual["name"] if actual else None,
                    "columns": actual["columns"] if actual else [],
                } if actual else None,
                message=(
                    f"PK for {table_name}: expected columns {expected_tbl.pk_columns}, "
                    f"got {actual['columns'] if actual else 'NONE'}"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_foreign_keys(self) -> None:
        phase = self._new_phase("foreign_keys")
        t0 = time.time()
        actual_fks = {fk["name"]: fk for fk in self._cat.get_foreign_keys(SCHEMA_NAME)}
        for table_name, fk_list in FOREIGN_KEYS.items():
            for fk in fk_list:
                fk_name = fk.constraint_name
                actual = actual_fks.get(fk_name)
                ok = (
                    actual is not None
                    and actual["columns"] == fk.columns
                    and actual["ref_table"] == fk.ref_table
                    and actual["ref_columns"] == fk.ref_columns
                )
                actual_repr = None
                if actual and not ok:
                    actual_repr = {
                        "columns": actual["columns"],
                        "ref_table": actual["ref_table"],
                        "ref_columns": actual["ref_columns"],
                    }
                phase.add_check(CheckResult(
                    name=f"fk:{fk_name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected={"columns": fk.columns, "ref_table": fk.ref_table, "ref_columns": fk.ref_columns},
                    actual=actual_repr,
                    message=(
                        f"FK {fk_name} on {table_name}: "
                        f"expected {fk.columns} -> {fk.ref_table}.{fk.ref_columns}"
                    ) if not ok else f"FK {fk_name} verified",
                ))
        phase.duration_s = time.time() - t0

    def _validate_unique_constraints(self) -> None:
        phase = self._new_phase("unique_constraints")
        t0 = time.time()
        for table_name, expected_list in UNIQUE_CONSTRAINTS.items():
            actual_list = self._cat.get_unique_constraints(SCHEMA_NAME, table_name)
            actual_map = {uc["name"]: uc["columns"] for uc in actual_list}
            for uc in expected_list:
                actual_cols = actual_map.get(uc.name, [])
                ok = uc.name in actual_map and actual_cols == uc.columns
                phase.add_check(CheckResult(
                    name=f"unique:{table_name}.{uc.name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected=uc.columns,
                    actual=actual_cols if ok else None,
                    message=(
                        f"Unique constraint {uc.name}: expected columns {uc.columns}"
                    ) if not ok else None,
                ))
        phase.duration_s = time.time() - t0

    def _validate_check_constraints(self) -> None:
        phase = self._new_phase("check_constraints")
        t0 = time.time()
        for table_name, expected_names in CHECK_CONSTRAINTS.items():
            actual_list = self._cat.get_check_constraints(SCHEMA_NAME, table_name)
            actual_map = {c["name"]: c for c in actual_list}
            for c_name in expected_names:
                ok = c_name in actual_map
                phase.add_check(CheckResult(
                    name=f"check:{table_name}.{c_name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected=True,
                    actual=ok,
                    message=f"Check constraint {c_name} {'found' if ok else 'MISSING'}",
                ))
        phase.duration_s = time.time() - t0

    def _validate_indexes(self) -> None:
        phase = self._new_phase("indexes")
        t0 = time.time()
        for table_name, expected_list in INDEXES.items():
            actual_list = self._cat.get_indexes(SCHEMA_NAME, table_name)
            actual_map = {i["name"]: i for i in actual_list}
            for idx in expected_list:
                actual = actual_map.get(idx.name)
                ok = (
                    actual is not None
                    and actual["is_unique"] == idx.unique
                    and actual["columns"] == idx.columns
                )
                phase.add_check(CheckResult(
                    name=f"index:{table_name}.{idx.name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected={"unique": idx.unique, "columns": idx.columns},
                    actual=None if not actual else {
                        "unique": actual["is_unique"],
                        "columns": actual["columns"],
                    },
                    message=(
                        f"Index {idx.name}: expected cols={idx.columns}, unique={idx.unique}"
                    ) if not ok else None,
                ))
        phase.duration_s = time.time() - t0

    def _validate_views(self) -> None:
        phase = self._new_phase("views")
        t0 = time.time()
        actual = {v["name"]: v for v in self._cat.get_views(SCHEMA_NAME)}
        for v in VIEWS:
            exists = v.name in actual
            has_def = exists and bool(actual[v.name]["definition"])
            ok = has_def
            phase.add_check(CheckResult(
                name=f"view:{v.name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=True,
                actual=exists and has_def,
                message=(
                    f"View {v.name} exists with definition"
                    if ok else
                    f"View {v.name} MISSING or no definition"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_functions(self) -> None:
        phase = self._new_phase("functions")
        t0 = time.time()
        actual = {f["name"]: f for f in self._cat.get_functions(SCHEMA_NAME)}
        for f_name, f_expected in FUNCTIONS.items():
            info = actual.get(f_name)
            ok = info is not None and info["kind"] == f_expected.kind
            phase.add_check(CheckResult(
                name=f"function:{f_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=f_expected.kind,
                actual=info["kind"] if info else None,
                message=(
                    f"Function {f_name}: expected kind '{f_expected.kind}', "
                    f"got {info['kind'] if info else 'MISSING'}"
                ) if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_procedures(self) -> None:
        phase = self._new_phase("procedures")
        t0 = time.time()
        actual = {f["name"]: f for f in self._cat.get_functions(SCHEMA_NAME)}
        for p_name in PROCEDURES:
            info = actual.get(p_name)
            ok = info is not None and info["kind"] == "procedure"
            phase.add_check(CheckResult(
                name=f"procedure:{p_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=True,
                actual=ok,
                message=f"Procedure {p_name} {'exists' if ok else 'MISSING'}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_triggers(self) -> None:
        phase = self._new_phase("triggers")
        t0 = time.time()
        actual_list = self._cat.get_triggers(SCHEMA_NAME)
        actual_map = {t["name"]: t for t in actual_list}
        for t_name, expected in TRIGGERS.items():
            actual = actual_map.get(t_name)
            ok = (
                actual is not None
                and actual["table"] == expected.table
                and actual["is_disabled"] == expected.is_disabled
            )
            phase.add_check(CheckResult(
                name=f"trigger:{t_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected={"table": expected.table, "disabled": expected.is_disabled},
                actual=None if not actual else {
                    "table": actual["table"],
                    "disabled": actual["is_disabled"],
                },
                message=(
                    f"Trigger {t_name}: expected table={expected.table}, disabled={expected.is_disabled}"
                ) if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_sequences(self) -> None:
        phase = self._new_phase("sequences")
        t0 = time.time()
        actual = self._cat.get_standalone_sequences(SCHEMA_NAME)
        for s_name, expected in SEQUENCES.items():
            info = actual.get(s_name)
            ok = (
                info is not None
                and info["start_value"] == expected["start_value"]
                and info["increment"] == expected["increment"]
            )
            phase.add_check(CheckResult(
                name=f"sequence:{s_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected={"start": expected["start_value"], "increment": expected["increment"]},
                actual=None if not info else {
                    "start": info["start_value"],
                    "increment": info["increment"],
                },
                message=(
                    f"Sequence {s_name}: expected start={expected['start_value']}, "
                    f"increment={expected['increment']}"
                ) if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_partitions(self) -> None:
        phase = self._new_phase("partitions")
        t0 = time.time()
        actual = self._cat.get_partitions()
        actual_by_parent: dict[str, set[str]] = {}
        for p in actual:
            actual_by_parent.setdefault(p["parent_table"], set()).add(p["name"])

        for parent_table, expected_parts in PARTITIONS.items():
            actual_parts = actual_by_parent.get(parent_table, set())
            missing = set(expected_parts) - actual_parts
            extra = actual_parts - set(expected_parts)
            ok = not missing and not extra
            phase.add_check(CheckResult(
                name=f"partitions:{parent_table}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=sorted(expected_parts),
                actual=sorted(actual_parts),
                message=(
                    f"Partitions for {parent_table}: "
                    f"missing={sorted(missing) or 'none'}, extra={sorted(extra) or 'none'}"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_user_types(self) -> None:
        phase = self._new_phase("user_defined_types")
        t0 = time.time()
        actual_types = {t["name"] for t in self._cat.get_user_types(SCHEMA_NAME)}

        for t_name in USER_TYPES:
            ok = t_name in actual_types
            phase.add_check(CheckResult(
                name=f"udt:{t_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=True,
                actual=ok,
                message=f"Usertype {t_name} {'exists' if ok else 'MISSING'}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_security(self) -> None:
        phase = self._new_phase("security")
        t0 = time.time()
        actual_roles = set(self._cat.get_roles())
        expected_roles = set(ROLES)
        # Check that all expected roles exist (but don't fail on extras —
        # pre-existing database roles may be present in the environment)
        missing_roles = expected_roles - actual_roles
        roles_ok = len(missing_roles) == 0
        phase.add_check(CheckResult(
            name="roles",
            status=STATUS_PASS if roles_ok else STATUS_FAIL,
            expected=sorted(expected_roles),
            actual=sorted(actual_roles),
            message=(
                f"Roles: expected {sorted(expected_roles)}, got {len(actual_roles)} roles "
                f"missing={sorted(missing_roles)}"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_rls_policies(self) -> None:
        phase = self._new_phase("rls_policies")
        t0 = time.time()
        for table_name, expected_policy_names in RLS_POLICIES.items():
            rls_on = self._cat.get_rls_enabled(SCHEMA_NAME, table_name)
            rls_ok = rls_on
            phase.add_check(CheckResult(
                name=f"rls:{table_name}",
                status=STATUS_PASS if rls_ok else STATUS_FAIL,
                expected=True,
                actual=rls_on,
                message=f"RLS on {table_name}: {'enabled' if rls_on else 'NOT enabled'}",
            ))

            actual_policies_raw = self._cat.get_rls_policies(SCHEMA_NAME, table_name)
            actual_policy_names = {p["name"] for p in actual_policies_raw}
            expected_policies = set(expected_policy_names)
            policies_ok = actual_policy_names == expected_policies
            phase.add_check(CheckResult(
                name=f"rls_policies:{table_name}",
                status=STATUS_PASS if policies_ok else STATUS_FAIL,
                expected=sorted(expected_policies),
                actual=sorted(actual_policy_names),
                message=(
                    f"RLS policies on {table_name}: "
                    f"missing={sorted(expected_policies - actual_policy_names) or 'none'}, "
                    f"extra={sorted(actual_policy_names - expected_policies) or 'none'}"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_grants(self) -> None:
        phase = self._new_phase("grants")
        t0 = time.time()
        # Fetch grants and normalise: split comma-separated privilege lists
        raw_grants = self._cat.get_grants(SCHEMA_NAME)
        # Build a set of (grantee, privilege, object_type, object_name) tuples
        actual_set: set[tuple[str, str, str, str]] = set()
        for g in raw_grants:
            for priv in g["privilege"].split(", "):
                actual_set.add((
                    g["grantee"], priv.strip(), g["object_type"], g["object_name"],
                ))
        expected_set = {
            (g.grantee, g.privilege, g.object_type, g.object_name)
            for g in GRANTS
        }
        missing = expected_set - actual_set
        # Extra grants are not failures (postgres has owner grants we don't model)
        ok = len(missing) == 0
        phase.add_check(CheckResult(
            name="grants",
            status=STATUS_PASS if ok else STATUS_FAIL,
            expected=len(expected_set),
            actual=len(actual_set & expected_set),
            message=(
                f"Missing grants: {sorted(missing) or 'none'}"
            ),
            details={"missing_count": len(missing)} if missing else {},
        ))
        phase.duration_s = time.time() - t0

    def _validate_comments(self) -> None:
        phase = self._new_phase("comments")
        t0 = time.time()
        actual = self._cat.get_comments(SCHEMA_NAME)
        actual_count = len(actual)
        ok = actual_count >= COMMENT_COUNT
        phase.add_check(CheckResult(
            name="comment_count",
            status=STATUS_PASS if ok else STATUS_FAIL,
            expected=COMMENT_COUNT,
            actual=actual_count,
            message=(
                f"Expected at least {COMMENT_COUNT} comments, got {actual_count}"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_row_counts(self) -> None:
        phase = self._new_phase("row_counts")
        t0 = time.time()
        for table_name, expected_count in ROW_COUNTS.items():
            actual_count = self._cat.get_row_count(SCHEMA_NAME, table_name)
            ok = actual_count == expected_count
            msg = (
                f"{table_name}: expected {expected_count}, got {actual_count}"
            ) if not ok else None
            phase.add_check(CheckResult(
                name=f"rows:{table_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=expected_count,
                actual=actual_count,
                message=msg,
            ))
        phase.duration_s = time.time() - t0

    def _validate_trigger_data(self) -> None:
        """Verify that trigger-generated OrderAudit rows match Orders rows."""
        phase = self._new_phase("trigger_data")
        t0 = time.time()

        orders_count = self._cat.get_row_count(SCHEMA_NAME, "orders")
        audit_count = self._cat.get_row_count(SCHEMA_NAME, "orderaudit")
        ok_count = audit_count == orders_count
        phase.add_check(CheckResult(
            name="audit_row_count",
            status=STATUS_PASS if ok_count else STATUS_FAIL,
            expected=orders_count,
            actual=audit_count,
            message=(
                f"OrderAudit rows ({audit_count}) should match "
                f"Orders rows ({orders_count}) via trigger"
            ),
        ))

        actual_insert = self._cat.count_by_column_value(
            SCHEMA_NAME, "orderaudit", "action", "INSERT",
        )
        expected_insert = orders_count
        ok_action = actual_insert == expected_insert
        phase.add_check(CheckResult(
            name="audit_insert_action",
            status=STATUS_PASS if ok_action else STATUS_FAIL,
            expected=expected_insert,
            actual=actual_insert,
            message=(
                f"OrderAudit INSERT action rows: expected {expected_insert}, "
                f"got {actual_insert}"
            ),
        ))
        phase.duration_s = time.time() - t0
