"""Source structural validation for MSSQL E2E.

Compares the live MSSQL catalog against the expected fixture state defined
in ``expected.py``.  Produces a ``ValidationReport`` with per-category
phases and individual check results.
"""
from __future__ import annotations

import time

from tests.e2e.mssql.validation.catalog import MSSQLCatalog
from tests.e2e.mssql.validation.expected import (
    CHECK_CONSTRAINTS,
    DEFAULT_CONSTRAINTS,
    EXPECTED_EXT_PROP_COUNT,
    FOREIGN_KEYS,
    FUNCTIONS,
    INDEXES,
    PARTITION_FUNCTION,
    PARTITION_SCHEME,
    PARTITIONED_TABLE,
    PROCEDURES,
    ROLE_MEMBERSHIPS,
    ROLES,
    ROW_COUNTS,
    SCHEMA_NAME,
    SEQUENCES,
    SYNONYMS,
    TABLES,
    TRIGGERS,
    UNIQUE_CONSTRAINTS,
    USER_TYPES,
    USERS,
    VIEWS,
)
from tests.e2e.mssql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


def _norm_udt(name: str | None) -> str | None:
    """Normalise a bracketed UDT name like [training].[CustomerCode] -> training.CustomerCode."""
    if name is None:
        return None
    return name.replace("[", "").replace("]", "")


class SourceValidator:
    """Validates an MSSQL source database against the E2E expected fixture."""

    def __init__(self, catalog: MSSQLCatalog, database_name: str = "") -> None:
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
        self._validate_default_constraints()
        self._validate_indexes()
        self._validate_views()
        self._validate_functions()
        self._validate_procedures()
        self._validate_triggers()
        self._validate_sequences()
        self._validate_synonyms()
        self._validate_user_types()
        self._validate_partitioning()
        self._validate_security()
        self._validate_extended_properties()
        self._validate_row_counts()
        self._validate_trigger_data()
        self._report.total_duration_s = time.time() - start
        return self._report

    # ------------------------------------------------------------------ #
    # Individual phase implementations
    # ------------------------------------------------------------------ #

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _validate_database(self) -> None:
        phase = self._new_phase("database")
        t0 = time.time()
        db_exists = self._cat.database_exists(self._db_name) if self._db_name else False
        phase.add_check(CheckResult(
            name=f"database:{self._db_name or '<unknown>'}",
            status=STATUS_PASS if db_exists else STATUS_FAIL,
            expected=True,
            actual=db_exists,
            message=(
                f"Database '{self._db_name}' exists"
                if db_exists else
                f"Database '{self._db_name}' NOT FOUND"
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

            detail = {}
            if missing:
                detail["missing"] = sorted(missing)
                all_ok = False
            if extra:
                detail["extra"] = sorted(extra)
                all_ok = False

            # Check each expected column's metadata
            mismatches = []
            for col_name, exp_col in expected_cols.items():
                if col_name not in actual_cols:
                    continue
                act_col = actual_cols[col_name]
                if exp_col.udt_name:
                    actual_udt = _norm_udt(act_col.get("udt_name"))
                    if actual_udt != exp_col.udt_name:
                        mismatches.append(
                            f"{col_name}: UDT expected '{exp_col.udt_name}', "
                            f"got '{actual_udt}'"
                        )
                        all_ok = False
                elif act_col["base_type"].lower() != exp_col.base_type.lower():
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
                if act_col["is_identity"] != exp_col.is_identity:
                    mismatches.append(
                        f"{col_name}: identity expected {exp_col.is_identity}, "
                        f"got {act_col['is_identity']}"
                    )
                    all_ok = False
                if act_col["is_computed"] != exp_col.is_computed:
                    mismatches.append(
                        f"{col_name}: computed expected {exp_col.is_computed}, "
                        f"got {act_col['is_computed']}"
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
                    f"PK for {table_name}: "
                    f"expected columns {expected_tbl.pk_columns}, "
                    f"got {actual['columns'] if actual else 'NONE'}"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_foreign_keys(self) -> None:
        phase = self._new_phase("foreign_keys")
        t0 = time.time()
        actual_fks = {fk["name"]: fk for fk in self._cat.get_foreign_keys(SCHEMA_NAME)}
        expected_fks = FOREIGN_KEYS

        for table_name, fk_list in expected_fks.items():
            for fk_name, pcols, ref_table, ref_cols in fk_list:
                actual = actual_fks.get(fk_name)
                ok = (
                    actual is not None
                    and actual["columns"] == pcols
                    and actual["ref_table"] == ref_table
                    and actual["ref_columns"] == ref_cols
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
                    expected={"columns": pcols, "ref_table": ref_table, "ref_columns": ref_cols},
                    actual=actual_repr,
                    message=(
                        f"FK {fk_name} on {table_name}: "
                        f"expected {pcols} -> {ref_table}.{ref_cols}"
                    ) if not ok else f"FK {fk_name} verified",
                ))
        phase.duration_s = time.time() - t0

    def _validate_unique_constraints(self) -> None:
        phase = self._new_phase("unique_constraints")
        t0 = time.time()
        for table_name, expected_list in UNIQUE_CONSTRAINTS.items():
            actual_list = self._cat.get_unique_constraints(SCHEMA_NAME, table_name)
            actual_map = {uc["name"]: uc["columns"] for uc in actual_list}
            for uc_name, cols in expected_list:
                actual_cols = actual_map.get(uc_name, [])
                ok = uc_name in actual_map and actual_cols == cols
                msg = (
                    f"Unique constraint {uc_name}: "
                    f"expected columns {cols}"
                ) if not ok else None
                phase.add_check(CheckResult(
                    name=f"unique:{table_name}.{uc_name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected=cols,
                    actual=actual_cols if ok else None,
                    message=msg,
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

    def _validate_default_constraints(self) -> None:
        phase = self._new_phase("default_constraints")
        t0 = time.time()
        for table_name, expected_names in DEFAULT_CONSTRAINTS.items():
            actual_list = self._cat.get_default_constraints(SCHEMA_NAME, table_name)
            actual_names = {d["name"] for d in actual_list}
            for d_name in expected_names:
                ok = d_name in actual_names
                phase.add_check(CheckResult(
                    name=f"default:{table_name}.{d_name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected=True,
                    actual=ok,
                    message=f"Default constraint {d_name} {'found' if ok else 'MISSING'}",
                ))
        phase.duration_s = time.time() - t0

    def _validate_indexes(self) -> None:
        phase = self._new_phase("indexes")
        t0 = time.time()
        for table_name, expected_list in INDEXES.items():
            actual_list = self._cat.get_indexes(SCHEMA_NAME, table_name)
            actual_map = {i["name"]: i for i in actual_list}
            for idx_name, is_unique, cols, included, is_clustered in expected_list:
                actual = actual_map.get(idx_name)
                actual_clustered = (
                    actual["type_desc"] == "CLUSTERED" if actual else None
                )
                ok = (
                    actual is not None
                    and actual["is_unique"] == is_unique
                    and actual["columns"] == cols
                    and actual_clustered == is_clustered
                )
                msg = (
                    f"Index {idx_name}: expected cols={cols}, "
                    f"unique={is_unique}, clustered={is_clustered}"
                ) if not ok else None
                phase.add_check(CheckResult(
                    name=f"index:{table_name}.{idx_name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected={
                        "unique": is_unique, "columns": cols,
                        "included": included, "clustered": is_clustered,
                    },
                    actual=None if not actual else {
                        "unique": actual["is_unique"],
                        "columns": actual["columns"],
                        "clustered": actual_clustered,
                        "filter": actual.get("filter_definition"),
                    },
                    message=msg,
                ))
        phase.duration_s = time.time() - t0

    def _validate_views(self) -> None:
        phase = self._new_phase("views")
        t0 = time.time()
        actual = self._cat.get_views(SCHEMA_NAME)
        for v_name in VIEWS:
            exists = v_name in actual
            has_def = exists and bool(actual[v_name])
            ok = has_def
            msg = (
                f"View {v_name} exists with definition"
                if ok else
                f"View {v_name} MISSING or no definition"
            )
            phase.add_check(CheckResult(
                name=f"view:{v_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=True,
                actual=exists and has_def,
                message=msg,
            ))
        phase.duration_s = time.time() - t0

    def _validate_functions(self) -> None:
        phase = self._new_phase("functions")
        t0 = time.time()
        actual = self._cat.get_functions(SCHEMA_NAME)
        for f_name, f_type in FUNCTIONS.items():
            info = actual.get(f_name)
            ok = info is not None and info["type"] == f_type
            phase.add_check(CheckResult(
                name=f"function:{f_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=f_type,
                actual=info["type"] if info else None,
                message=(
                    f"Function {f_name}: expected type '{f_type}', "
                    f"got {info['type'] if info else 'MISSING'}"
                ) if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_procedures(self) -> None:
        phase = self._new_phase("procedures")
        t0 = time.time()
        actual = self._cat.get_procedures(SCHEMA_NAME)
        for p_name in PROCEDURES:
            exists = p_name in actual
            phase.add_check(CheckResult(
                name=f"procedure:{p_name}",
                status=STATUS_PASS if exists else STATUS_FAIL,
                expected=True,
                actual=exists,
                message=f"Procedure {p_name} {'exists' if exists else 'MISSING'}",
            ))
        phase.duration_s = time.time() - t0

    def _validate_triggers(self) -> None:
        phase = self._new_phase("triggers")
        t0 = time.time()
        actual_list = self._cat.get_triggers(SCHEMA_NAME)
        actual_map = {t["name"]: t for t in actual_list}
        for t_name, (t_table, t_disabled) in TRIGGERS.items():
            actual = actual_map.get(t_name)
            ok = (
                actual is not None
                and actual["table"] == t_table
                and actual["is_disabled"] == t_disabled
            )
            phase.add_check(CheckResult(
                name=f"trigger:{t_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected={"table": t_table, "disabled": t_disabled},
                actual=None if not actual else {
                    "table": actual["table"],
                    "disabled": actual["is_disabled"],
                },
                message=(
                    f"Trigger {t_name}: expected table={t_table}, disabled={t_disabled}"
                ) if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_sequences(self) -> None:
        phase = self._new_phase("sequences")
        t0 = time.time()
        actual = self._cat.get_sequences(SCHEMA_NAME)
        for s_name, (s_type, s_start) in SEQUENCES.items():
            info = actual.get(s_name)
            ok = (
                info is not None
                and info["data_type"] == s_type
                and info["start_value"] == s_start
            )
            phase.add_check(CheckResult(
                name=f"sequence:{s_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected={"type": s_type, "start": s_start},
                actual=None if not info else {
                    "type": info["data_type"],
                    "start": info["start_value"],
                },
                message=(
                    f"Sequence {s_name}: expected type={s_type}, start={s_start}"
                ) if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_synonyms(self) -> None:
        phase = self._new_phase("synonyms")
        t0 = time.time()
        actual = self._cat.get_synonyms(SCHEMA_NAME)
        for s_name, base_obj in SYNONYMS.items():
            actual_base = actual.get(s_name)
            ok = actual_base is not None and actual_base == base_obj
            phase.add_check(CheckResult(
                name=f"synonym:{s_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=base_obj,
                actual=actual_base,
                message=f"Synonym {s_name}: expected {base_obj}" if not ok else None,
            ))
        phase.duration_s = time.time() - t0

    def _validate_user_types(self) -> None:
        phase = self._new_phase("user_defined_types")
        t0 = time.time()
        actual = self._cat.get_user_types(SCHEMA_NAME)
        for t_name, (base_type, nullable) in USER_TYPES.items():
            info = actual.get(t_name)
            ok = (
                info is not None
                and info["base_type"] == base_type
                and info["is_nullable"] == nullable
            )
            msg = (
                f"UDT {t_name}: expected base={base_type}, "
                f"nullable={nullable}"
            ) if not ok else None
            phase.add_check(CheckResult(
                name=f"udt:{t_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected={"base_type": base_type, "nullable": nullable},
                actual=None if not info else {
                    "base_type": info["base_type"],
                    "nullable": info["is_nullable"],
                },
                message=msg,
            ))
        phase.duration_s = time.time() - t0

    def _validate_partitioning(self) -> None:
        phase = self._new_phase("partitioning")
        t0 = time.time()
        # Check partition function exists
        funcs = self._cat.get_partition_functions()
        func_names = {f["name"] for f in funcs}
        func_ok = PARTITION_FUNCTION in func_names
        phase.add_check(CheckResult(
            name=f"partition_function:{PARTITION_FUNCTION}",
            status=STATUS_PASS if func_ok else STATUS_FAIL,
            expected=True,
            actual=func_ok,
            message=f"Partition function {PARTITION_FUNCTION} {'exists' if func_ok else 'MISSING'}",
        ))

        # Check partition scheme exists
        schemes = self._cat.get_partition_schemes()
        scheme_ok = any(s["name"] == PARTITION_SCHEME for s in schemes)
        phase.add_check(CheckResult(
            name=f"partition_scheme:{PARTITION_SCHEME}",
            status=STATUS_PASS if scheme_ok else STATUS_FAIL,
            expected=True,
            actual=scheme_ok,
            message=f"Partition scheme {PARTITION_SCHEME} {'exists' if scheme_ok else 'MISSING'}",
        ))

        # Check partitioned table exists
        pt_tables = self._cat.get_partitioned_tables(SCHEMA_NAME)
        pt_ok = any(t["table"] == PARTITIONED_TABLE for t in pt_tables)
        phase.add_check(CheckResult(
            name=f"partitioned_table:{PARTITIONED_TABLE}",
            status=STATUS_PASS if pt_ok else STATUS_FAIL,
            expected=True,
            actual=pt_ok,
            message=(
                f"Partitioned table {PARTITIONED_TABLE} {'exists' if pt_ok else 'MISSING'}"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_security(self) -> None:
        phase = self._new_phase("security")
        t0 = time.time()
        info = self._cat.get_security_info(
            role_names=ROLES, user_names=USERS
        )

        # Roles
        actual_roles = set(info["roles"])
        expected_roles = set(ROLES)
        roles_ok = expected_roles == actual_roles
        phase.add_check(CheckResult(
            name="roles",
            status=STATUS_PASS if roles_ok else STATUS_FAIL,
            expected=sorted(expected_roles),
            actual=sorted(actual_roles),
            message=(
                f"Roles: expected {sorted(expected_roles)}, got {sorted(actual_roles)} "
                f"missing={sorted(expected_roles - actual_roles)} "
                f"extra={sorted(actual_roles - expected_roles)}"
            ),
        ))

        # Users
        actual_users = set(info["users"])
        expected_users = set(USERS)
        users_ok = expected_users == actual_users
        phase.add_check(CheckResult(
            name="users",
            status=STATUS_PASS if users_ok else STATUS_FAIL,
            expected=sorted(expected_users),
            actual=sorted(actual_users),
            message=f"Users match: {users_ok}",
        ))

        # Role memberships
        actual_memberships = {
            (m["member"], m["role"]) for m in info["memberships"]
        }
        expected_memberships = {
            (member, role)
            for role, members in ROLE_MEMBERSHIPS.items()
            for member in members
        }
        missing_memberships = expected_memberships - actual_memberships
        mem_ok = len(missing_memberships) == 0
        phase.add_check(CheckResult(
            name="role_memberships",
            status=STATUS_PASS if mem_ok else STATUS_FAIL,
            expected=sorted(expected_memberships),
            actual=sorted(actual_memberships),
            message=(
                "All expected role memberships verified"
                if mem_ok else
                f"Missing memberships: {sorted(missing_memberships)}"
            ),
        ))

        # Grants (check count is reasonable — at least one grant exists)
        grant_count = len(info["grants"])
        grants_ok = grant_count > 0
        phase.add_check(CheckResult(
            name="grants",
            status=STATUS_PASS if grants_ok else STATUS_FAIL,
            expected=">0 grants",
            actual=grant_count,
            message=f"Found {grant_count} grants on E2E objects",
        ))
        phase.duration_s = time.time() - t0

    def _validate_extended_properties(self) -> None:
        phase = self._new_phase("extended_properties")
        t0 = time.time()
        actual = self._cat.get_extended_properties(SCHEMA_NAME)
        actual_count = len(actual)
        ok = actual_count >= EXPECTED_EXT_PROP_COUNT
        phase.add_check(CheckResult(
            name="ext_prop_count",
            status=STATUS_PASS if ok else STATUS_FAIL,
            expected=EXPECTED_EXT_PROP_COUNT,
            actual=actual_count,
            message=(
                f"Expected at least {EXPECTED_EXT_PROP_COUNT} extended properties, "
                f"got {actual_count}"
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
                f"{table_name}: expected {expected_count}, "
                f"got {actual_count}"
            ) if not ok else None
            phase.add_check(CheckResult(
                name=f"rows:{table_name}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=expected_count,
                actual=actual_count,
                message=msg,
            ))
        phase.duration_s = time.time() - t0

    # ------------------------------------------------------------------ #
    # Trigger-generated data behaviour
    # ------------------------------------------------------------------ #

    def _validate_trigger_data(self) -> None:
        """Verify that trigger-generated OrderAudit rows match the Orders
        rows that were inserted (trg_Orders_Insert fires INSERT -> 'INSERT').
        """
        phase = self._new_phase("trigger_data")
        t0 = time.time()

        orders_count = self._cat.get_row_count(SCHEMA_NAME, "Orders")
        audit_count = self._cat.get_row_count(SCHEMA_NAME, "OrderAudit")
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

        # Verify the audit rows were INSERT actions (not UPDATE)
        actual_insert = self._cat.count_by_column_value(
            SCHEMA_NAME, "OrderAudit", "Action", "INSERT"
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
