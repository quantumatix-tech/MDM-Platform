"""Source structural validation for MySQL E2E Phase B.

Compares the live MySQL catalog against the expected fixture state defined
in ``expected.py``.  Produces a ``ValidationReport`` with per-category phases
and individual check results.

Key MySQL adaptations vs. the PostgreSQL ``SourceValidator``:
  * Identifiers are case-sensitive on Linux but lowercase-folded on Windows;
    the MySQL CLI/server on this platform reports them in lowercase.
  * No DEFAULT constraints table — defaults live on the column row.
  * No extended properties — MySQL has COMMENT.
  * No RLS policies — MySQL has no row-level security.
  * No standalone sequences — AUTO_INCREMENT is table-bound.
  * No user-defined types — ENUM is column-level.
  * No GRANT/RBAC role model — MySQL uses accounts and privileges.
"""
from __future__ import annotations

import time

from tests.e2e.mysql.validation.catalog import MySQLCatalog
from tests.e2e.mysql.validation.expected import (
    CHECK_CONSTRAINTS,
    COMMENT_COUNT,
    ENUM_TYPES,
    FOREIGN_KEYS,
    FUNCTIONS,
    INDEXES,
    PARTITIONS,
    PROCEDURES,
    ROW_COUNTS,
    TABLES,
    TRIGGERS,
    UNIQUE_CONSTRAINTS,
    VIEWS,
)
from tests.e2e.mysql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


class MySQLSourceValidator:
    """Validates a MySQL source database against the E2E expected fixture."""

    def __init__(self, catalog: MySQLCatalog, database_name: str = "") -> None:
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
        self._validate_routines()
        self._validate_triggers()
        self._validate_partitions()
        self._validate_enum_types()
        self._validate_comments()
        self._validate_row_counts()
        self._validate_trigger_data()
        self._report.total_duration_s = time.time() - start
        return self._report

    def _new_phase(self, name: str) -> PhaseResult:
        phase = PhaseResult(name=name)
        self._report.phases.append(phase)
        return phase

    def _validate_database(self) -> None:
        phase = self._new_phase("database")
        t0 = time.time()
        # MySQL on Windows lowercases unquoted database names; compare
        # case-insensitively to handle cross-platform environments.
        db_name_actual = self._cat.database_name
        db_exists = bool(self._db_name) and db_name_actual.lower() == self._db_name.lower()
        phase.add_check(CheckResult(
            name=f"database:{self._db_name or '<unknown>'}",
            status=STATUS_PASS if db_exists else STATUS_FAIL,
            expected=True,
            actual=db_exists,
            message=(
                f"Database '{self._db_name}' is accessible "
                f"(actual: '{db_name_actual}')"
                if db_exists else
                f"Database '{self._db_name}' NOT FOUND / connection failed "
                f"(actual: '{db_name_actual}')"
            ),
        ))
        phase.duration_s = time.time() - t0

    def _validate_schemas(self) -> None:
        phase = self._new_phase("schemas")
        t0 = time.time()
        # MySQL: the database itself is the schema namespace.
        exists = self._cat.schema_exists(self._db_name) if self._db_name else False
        phase.add_check(CheckResult(
            name=f"schema:{self._db_name}",
            status=STATUS_PASS if exists else STATUS_FAIL,
            expected=True,
            actual=exists,
            message=f"Schema '{self._db_name}' {'exists' if exists else 'MISSING'}",
        ))
        phase.duration_s = time.time() - t0

    def _validate_tables(self) -> None:
        phase = self._new_phase("tables")
        t0 = time.time()
        actual = set(self._cat.get_base_tables(self._db_name))
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
            actual_cols = {c["name"]: c for c in self._cat.get_columns(self._db_name, table_name)}
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

                if act_col["source_type"] != exp_col.base_type:
                    mismatches.append(
                        f"{col_name}: type expected '{exp_col.base_type}', "
                        f"got '{act_col['source_type']}'"
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
            actual = self._cat.get_primary_key(self._db_name, table_name)
            cols_ok = actual is not None and actual["columns"] == expected_tbl.pk_columns
            phase.add_check(CheckResult(
                name=f"pk:{table_name}",
                status=STATUS_PASS if cols_ok else STATUS_FAIL,
                expected=expected_tbl.pk_columns,
                actual=actual["columns"] if actual else [],
                message=(
                    f"PK for {table_name}: expected {expected_tbl.pk_columns}, "
                    f"got {actual['columns'] if actual else 'NONE'}"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_foreign_keys(self) -> None:
        phase = self._new_phase("foreign_keys")
        t0 = time.time()
        actual_fks = {fk["name"]: fk for fk in self._cat.get_foreign_keys(self._db_name)}
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
                phase.add_check(CheckResult(
                    name=f"fk:{table_name}.{fk_name}",
                    status=STATUS_PASS if ok else STATUS_FAIL,
                    expected={"columns": fk.columns, "ref_table": fk.ref_table, "ref_columns": fk.ref_columns},
                    actual=None if not actual else {
                        "columns": actual["columns"],
                        "ref_table": actual["ref_table"],
                        "ref_columns": actual["ref_columns"],
                    },
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
            actual_list = self._cat.get_unique_constraints_for_table(self._db_name, table_name)
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
            actual_list = self._cat.get_check_constraints(self._db_name, table_name)
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
        all_ok = True
        for table_name, expected_list in INDEXES.items():
            actual_list = self._cat.get_indexes(self._db_name, table_name)
            actual_map = {i["name"]: i for i in actual_list}
            for idx in expected_list:
                actual = actual_map.get(idx.name)
                ok = (
                    actual is not None
                    and actual["is_unique"] == idx.unique
                    and actual["columns"] == idx.columns
                )
                if not ok:
                    all_ok = False
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
        if not all_ok:
            phase.status = STATUS_FAIL

    def _validate_views(self) -> None:
        phase = self._new_phase("views")
        t0 = time.time()
        actual = {v["name"]: v for v in self._cat.get_views(self._db_name)}
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

    def _validate_routines(self) -> None:
        phase = self._new_phase("routines")
        t0 = time.time()
        actual_funcs = {f["name"]: f for f in self._cat.get_functions(self._db_name)}
        actual_procs = {p["name"]: p for p in self._cat.get_procedures(self._db_name)}

        for f_name, f_expected in FUNCTIONS.items():
            info = actual_funcs.get(f_name)
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

        for p_name in PROCEDURES:
            info = actual_procs.get(p_name)
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
        actual_list = self._cat.get_triggers(self._db_name)
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

    def _validate_partitions(self) -> None:
        phase = self._new_phase("partitions")
        t0 = time.time()
        all_parts = self._cat.get_partitions(self._db_name)
        actual_by_table: dict[str, set[str]] = {}
        for p in all_parts:
            actual_by_table.setdefault(p["table"], set()).add(p["partition_name"])

        for parent_table, expected_part in PARTITIONS.items():
            actual_parts = actual_by_table.get(parent_table, set())
            missing = set(expected_part.partitions) - actual_parts
            extra = actual_parts - set(expected_part.partitions)
            ok = not missing and not extra
            phase.add_check(CheckResult(
                name=f"partitions:{parent_table}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=sorted(expected_part.partitions),
                actual=sorted(actual_parts),
                message=(
                    f"Partitions for {parent_table}: "
                    f"missing={sorted(missing) or 'none'}, extra={sorted(extra) or 'none'}"
                ),
            ))
        phase.duration_s = time.time() - t0

    def _validate_enum_types(self) -> None:
        phase = self._new_phase("enum_types")
        t0 = time.time()
        all_ok = True
        for col_path, expected_labels in ENUM_TYPES.items():
            table_name, col_name = col_path.split(".")
            cols = self._cat.get_columns(self._db_name, table_name)
            col = next((c for c in cols if c["name"] == col_name), None)
            if col is None:
                phase.add_check(CheckResult(
                    name=f"enum:{col_path}",
                    status=STATUS_FAIL,
                    expected=expected_labels,
                    actual=None,
                    message=f"Column {col_path} not found",
                ))
                all_ok = False
                continue

            col_type = col["source_type"]
            if not col_type.lower().startswith("enum("):
                phase.add_check(CheckResult(
                    name=f"enum:{col_path}",
                    status=STATUS_FAIL,
                    expected=expected_labels,
                    actual=col_type,
                    message=f"Column {col_path} is not an ENUM type",
                ))
                all_ok = False
                continue

            # Extract labels from the ENUM definition
            enum_inner = col_type[col_type.index("(") + 1:col_type.rindex(")")]
            actual_labels = [
                label.strip().strip("'") for label in enum_inner.split(",")
            ]
            ok = actual_labels == expected_labels
            if not ok:
                all_ok = False
            phase.add_check(CheckResult(
                name=f"enum:{col_path}",
                status=STATUS_PASS if ok else STATUS_FAIL,
                expected=expected_labels,
                actual=actual_labels,
                message=None if ok else f"ENUM labels differ for {col_path}",
            ))
        phase.duration_s = time.time() - t0
        if not all_ok:
            phase.status = STATUS_FAIL

    def _validate_comments(self) -> None:
        phase = self._new_phase("comments")
        t0 = time.time()
        actual = self._cat.get_comments(self._db_name)
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
            actual_count = self._cat.get_row_count(self._db_name, table_name)
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

        orders_count = self._cat.get_row_count(self._db_name, "orders")
        audit_count = self._cat.get_row_count(self._db_name, "orderaudit")
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
        phase.duration_s = time.time() - t0
