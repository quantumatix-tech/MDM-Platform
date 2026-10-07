"""Source-vs-target structural comparison for PostgreSQL E2E.

Builds a reusable comparison layer that queries both source and target
catalogs via ``PostgreSQLCatalog`` and reports per-category deltas.  This is
*not* a duplicate of the structural validator — it compares two live databases
rather than validating a single database against the fixture contract in
``expected.py``.

PostgreSQL-specific notes:
  - Functions/procedures are identified by schema + name + argument signature
    (via ``pg_get_function_arguments``) to handle overloading.
  - Partitioned indexes (PG 17 ``relkind = 'I'``) are excluded — only
    table partitions (``relkind IN ('r','p','f')``) are compared.
  - View definitions and routine bodies are normalized before comparison
    (whitespace, identifier quoting, keyword case) to avoid false mismatches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tests.e2e.postgresql.validation.catalog import PostgreSQLCatalog
from tests.e2e.postgresql.validation.expected import SCHEMA_NAME
from tests.e2e.postgresql.validation.models import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckResult,
    PhaseResult,
    ValidationReport,
)


@dataclass
class ComparisonCategory:
    """Result of comparing one object category between source and target."""

    name: str
    status: str = STATUS_PASS
    expected_count: int = 0
    actual_count: int = 0
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)

    def fail(self, reason: str = "") -> None:
        self.status = STATUS_FAIL
        if reason:
            self.mismatches.append(reason)

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASS


class SourceTargetComparator:
    """Compares source and target PostgreSQL databases catalog-by-catalog."""

    def __init__(self, source: PostgreSQLCatalog, target: PostgreSQLCatalog) -> None:
        self._src = source
        self._tgt = target
        self._categories: list[ComparisonCategory] = []

    @property
    def categories(self) -> list[ComparisonCategory]:
        return self._categories

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self._categories)

    def compare(self) -> None:
        """Run all comparison categories and populate results."""
        self._compare_schemas()
        self._compare_tables()
        self._compare_columns()
        self._compare_primary_keys()
        self._compare_foreign_keys()
        self._compare_unique_constraints()
        self._compare_check_constraints()
        self._compare_indexes()
        self._compare_views()
        self._compare_routines()
        self._compare_triggers()
        self._compare_sequences()
        self._compare_user_types()
        self._compare_partitions()
        self._compare_partitioned_tables()
        self._compare_security()
        self._compare_rls_policies()
        self._compare_grants()
        self._compare_comments()
        self._compare_row_counts()

    # ------------------------------------------------------------------ #
    # Normalization helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_ddl(definition: str | None) -> str:
        """Normalize a DDL/SQL definition for comparison.

        Strips comments, collapses whitespace, removes PostgreSQL identifier
        quoting, and lowercases keywords so semantically-equivalent definitions
        that differ only in formatting or quoting are treated as identical.
        """
        if not definition:
            return ""
        if not isinstance(definition, str):
            return str(definition).lower()
        text = definition
        text = re.sub(r"--[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r'"([^"]+)"', r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = text.lower()
        return text

    # ------------------------------------------------------------------ #
    # Per-category comparisons
    # ------------------------------------------------------------------ #

    def _compare_schemas(self) -> None:
        cat = ComparisonCategory(name="schemas")
        src_schemas = set(self._src.get_all_schemas())
        tgt_schemas = set(self._tgt.get_all_schemas())
        cat.expected_count = len(src_schemas)
        cat.actual_count = len(tgt_schemas)
        cat.missing = sorted(src_schemas - tgt_schemas)
        cat.unexpected = sorted(tgt_schemas - src_schemas)
        if cat.missing or cat.unexpected:
            cat.fail(f"Schemas differ: missing={cat.missing}, unexpected={cat.unexpected}")
        self._categories.append(cat)

    def _compare_tables(self) -> None:
        cat = ComparisonCategory(name="tables")
        src = set(self._src.get_base_tables(SCHEMA_NAME))
        tgt = set(self._tgt.get_base_tables(SCHEMA_NAME))
        cat.expected_count = len(src)
        cat.actual_count = len(tgt)
        cat.missing = sorted(src - tgt)
        cat.unexpected = sorted(tgt - src)
        if cat.missing or cat.unexpected:
            cat.fail(f"Tables differ: missing={cat.missing}, unexpected={cat.unexpected}")
        self._categories.append(cat)

    def _compare_columns(self) -> None:
        cat = ComparisonCategory(name="columns")
        all_tables = set(self._src.get_base_tables(SCHEMA_NAME))
        for tbl in sorted(all_tables):
            src_cols = {c["name"]: c for c in self._src.get_columns(SCHEMA_NAME, tbl)}
            tgt_cols = {c["name"]: c for c in self._tgt.get_columns(SCHEMA_NAME, tbl)}
            cat.expected_count += len(src_cols)
            cat.actual_count += len(tgt_cols)
            missing = set(src_cols) - set(tgt_cols)
            extra = set(tgt_cols) - set(src_cols)
            if missing:
                cat.missing.extend(f"{tbl}.{c}" for c in sorted(missing))
                cat.fail()
            if extra:
                cat.unexpected.extend(f"{tbl}.{c}" for c in sorted(extra))
                cat.fail()
            for col_name in sorted(set(src_cols) & set(tgt_cols)):
                src_c = src_cols[col_name]
                tgt_c = tgt_cols[col_name]
                for key in ("base_type", "is_nullable", "is_identity",
                            "is_generated", "size", "precision", "scale",
                            "udt_name", "default"):
                    sv = src_c.get(key)
                    tv = tgt_c.get(key)
                    if sv != tv:
                        if key == "udt_name" and sv is not None:
                            sv_stripped = sv.split(".")[-1] if "." in sv else sv
                            tv_stripped = tv.split(".")[-1] if tv and "." in tv else tv
                            if sv_stripped == tv_stripped:
                                continue
                        cat.mismatches.append(
                            f"{tbl}.{col_name}.{key}: {sv!r} != {tv!r}"
                        )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_primary_keys(self) -> None:
        cat = ComparisonCategory(name="primary_keys")
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            cat.expected_count += 1
            src_pk = self._src.get_primary_key(SCHEMA_NAME, tbl)
            tgt_pk = self._tgt.get_primary_key(SCHEMA_NAME, tbl)
            if src_pk is None and tgt_pk is None:
                cat.actual_count += 1
                continue
            cat.actual_count += 1
            if src_pk != tgt_pk:
                cat.mismatches.append(
                    f"PK {tbl}: src={src_pk} vs tgt={tgt_pk}"
                )
                cat.fail()
        self._categories.append(cat)

    def _compare_foreign_keys(self) -> None:
        cat = ComparisonCategory(name="foreign_keys")
        src_fks = {fk["name"]: fk for fk in self._src.get_foreign_keys(SCHEMA_NAME)}
        tgt_fks = {fk["name"]: fk for fk in self._tgt.get_foreign_keys(SCHEMA_NAME)}
        cat.expected_count = len(src_fks)
        cat.actual_count = len(tgt_fks)
        cat.missing = sorted(set(src_fks) - set(tgt_fks))
        cat.unexpected = sorted(set(tgt_fks) - set(src_fks))
        for name in sorted(set(src_fks) & set(tgt_fks)):
            if src_fks[name] != tgt_fks[name]:
                cat.mismatches.append(
                    f"FK {name}: {src_fks[name]} != {tgt_fks[name]}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_unique_constraints(self) -> None:
        cat = ComparisonCategory(name="unique_constraints")
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            src_list = self._src.get_unique_constraints(SCHEMA_NAME, tbl)
            tgt_list = self._tgt.get_unique_constraints(SCHEMA_NAME, tbl)
            cat.expected_count += len(src_list)
            cat.actual_count += len(tgt_list)
            src_map = {uc["name"]: uc for uc in src_list}
            tgt_map = {uc["name"]: uc for uc in tgt_list}
            for name in sorted(src_map):
                if name not in tgt_map:
                    cat.missing.append(f"{tbl}.{name}")
                elif src_map[name] != tgt_map[name]:
                    cat.mismatches.append(f"UQ {tbl}.{name}: {src_map[name]} != {tgt_map[name]}")
            for name in tgt_map:
                if name not in src_map:
                    cat.unexpected.append(f"{tbl}.{name}")
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_check_constraints(self) -> None:
        cat = ComparisonCategory(name="check_constraints")
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            src_list = self._src.get_check_constraints(SCHEMA_NAME, tbl)
            tgt_list = self._tgt.get_check_constraints(SCHEMA_NAME, tbl)
            cat.expected_count += len(src_list)
            cat.actual_count += len(tgt_list)
            src_map = {c["name"]: c for c in src_list}
            tgt_map = {c["name"]: c for c in tgt_list}
            for name in sorted(src_map):
                if name not in tgt_map:
                    cat.missing.append(f"{tbl}.{name}")
                elif src_map[name]["definition"] != tgt_map[name]["definition"]:
                    cat.mismatches.append(
                        f"CHECK {tbl}.{name}: definition differs"
                    )
            for name in tgt_map:
                if name not in src_map:
                    cat.unexpected.append(f"{tbl}.{name}")
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_indexes(self) -> None:
        cat = ComparisonCategory(name="indexes")
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            src_idxs = {i["name"]: i for i in self._src.get_indexes(SCHEMA_NAME, tbl)}
            tgt_idxs = {i["name"]: i for i in self._tgt.get_indexes(SCHEMA_NAME, tbl)}
            cat.expected_count += len(src_idxs)
            cat.actual_count += len(tgt_idxs)
            for idx_name in sorted(src_idxs):
                if idx_name not in tgt_idxs:
                    cat.missing.append(f"{tbl}.{idx_name}")
                else:
                    src_i = src_idxs[idx_name]
                    tgt_i = tgt_idxs[idx_name]
                    for key in ("is_unique", "columns"):
                        if src_i.get(key) != tgt_i.get(key):
                            cat.mismatches.append(
                                f"{tbl}.{idx_name}.{key}: "
                                f"{src_i.get(key)!r} != {tgt_i.get(key)!r}"
                            )
            for idx_name in tgt_idxs:
                if idx_name not in src_idxs:
                    cat.unexpected.append(f"{tbl}.{idx_name}")
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_views(self) -> None:
        cat = ComparisonCategory(name="views")
        src_views = {v["name"]: v for v in self._src.get_views(SCHEMA_NAME)}
        tgt_views = {v["name"]: v for v in self._tgt.get_views(SCHEMA_NAME)}
        cat.expected_count = len(src_views)
        cat.actual_count = len(tgt_views)
        cat.missing = sorted(set(src_views) - set(tgt_views))
        cat.unexpected = sorted(set(tgt_views) - set(src_views))
        for v in sorted(set(src_views) & set(tgt_views)):
            src_def = self._normalize_ddl(src_views[v]["definition"])
            tgt_def = self._normalize_ddl(tgt_views[v]["definition"])
            if src_def != tgt_def:
                cat.mismatches.append(f"View {v} definition differs (normalized)")
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_routines(self) -> None:
        """Compare functions/procedures by signature (handles overloading)."""
        cat = ComparisonCategory(name="routines")
        src_fns = self._src.get_routines(SCHEMA_NAME)
        tgt_fns = self._tgt.get_routines(SCHEMA_NAME)

        def _sig(r: dict) -> str:
            return f"{r['name']}({r['arg_types']})"

        src_map = {_sig(f): f for f in src_fns}
        tgt_map = {_sig(f): f for f in tgt_fns}
        cat.expected_count = len(src_map)
        cat.actual_count = len(tgt_map)
        cat.missing = sorted(set(src_map) - set(tgt_map))
        cat.unexpected = sorted(set(tgt_map) - set(src_map))
        for sig in sorted(set(src_map) & set(tgt_map)):
            if src_map[sig]["kind"] != tgt_map[sig]["kind"]:
                cat.mismatches.append(
                    f"Routine {sig}: kind {src_map[sig]['kind']} != {tgt_map[sig]['kind']}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_triggers(self) -> None:
        cat = ComparisonCategory(name="triggers")
        src_trig = {t["name"]: t for t in self._src.get_triggers(SCHEMA_NAME)}
        tgt_trig = {t["name"]: t for t in self._tgt.get_triggers(SCHEMA_NAME)}
        cat.expected_count = len(src_trig)
        cat.actual_count = len(tgt_trig)
        cat.missing = sorted(set(src_trig) - set(tgt_trig))
        cat.unexpected = sorted(set(tgt_trig) - set(src_trig))
        for t in sorted(set(src_trig) & set(tgt_trig)):
            for key in ("table", "is_disabled", "timing", "event"):
                if src_trig[t].get(key) != tgt_trig[t].get(key):
                    cat.mismatches.append(
                        f"Trigger {t}.{key}: {src_trig[t].get(key)!r} != {tgt_trig[t].get(key)!r}"
                    )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_sequences(self) -> None:
        cat = ComparisonCategory(name="sequences")
        src_seq = self._src.get_standalone_sequences(SCHEMA_NAME)
        tgt_seq = self._tgt.get_standalone_sequences(SCHEMA_NAME)
        cat.expected_count = len(src_seq)
        cat.actual_count = len(tgt_seq)
        cat.missing = sorted(set(src_seq) - set(tgt_seq))
        cat.unexpected = sorted(set(tgt_seq) - set(src_seq))
        for s in sorted(set(src_seq) & set(tgt_seq)):
            for key in ("data_type", "start_value", "increment"):
                if src_seq[s].get(key) != tgt_seq[s].get(key):
                    cat.mismatches.append(
                        f"Sequence {s}.{key}: "
                        f"{src_seq[s].get(key)!r} != {tgt_seq[s].get(key)!r}"
                    )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_user_types(self) -> None:
        cat = ComparisonCategory(name="user_types")
        src_ut = {t["name"]: t for t in self._src.get_user_types(SCHEMA_NAME)}
        tgt_ut = {t["name"]: t for t in self._tgt.get_user_types(SCHEMA_NAME)}
        cat.expected_count = len(src_ut)
        cat.actual_count = len(tgt_ut)
        cat.missing = sorted(set(src_ut) - set(tgt_ut))
        cat.unexpected = sorted(set(tgt_ut) - set(src_ut))
        for u in sorted(set(src_ut) & set(tgt_ut)):
            if src_ut[u]["kind"] != tgt_ut[u]["kind"]:
                cat.mismatches.append(
                    f"Type {u}: kind {src_ut[u]['kind']} != {tgt_ut[u]['kind']}"
                )
            if src_ut[u]["kind"] == "e":
                src_labels = self._src.get_enum_labels(SCHEMA_NAME, u)
                tgt_labels = self._tgt.get_enum_labels(SCHEMA_NAME, u)
                if src_labels != tgt_labels:
                    cat.mismatches.append(
                        f"Enum {u}: labels {src_labels} != {tgt_labels}"
                    )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_partitions(self) -> None:
        """Compare partition children, excluding partitioned indexes."""
        cat = ComparisonCategory(name="partitions")
        src_parts = self._src.get_partitions()
        tgt_parts = self._tgt.get_partitions()

        def _key(p: dict) -> str:
            return f"{p['schema_name']}.{p['name']}"

        src_map = {_key(p): p for p in src_parts}
        tgt_map = {_key(p): p for p in tgt_parts}
        cat.expected_count = len(src_map)
        cat.actual_count = len(tgt_map)
        cat.missing = sorted(set(src_map) - set(tgt_map))
        cat.unexpected = sorted(set(tgt_map) - set(src_map))
        for key in sorted(set(src_map) & set(tgt_map)):
            src_p = src_map[key]
            tgt_p = tgt_map[key]
            if src_p["parent_table"] != tgt_p["parent_table"]:
                cat.mismatches.append(
                    f"Partition {key}: parent {src_p['parent_table']} != {tgt_p['parent_table']}"
                )
            src_bound = self._normalize_ddl(src_p["bound_expr"])
            tgt_bound = self._normalize_ddl(tgt_p["bound_expr"])
            if src_bound != tgt_bound:
                cat.mismatches.append(
                    f"Partition {key}: bound {src_p['bound_expr']} != {tgt_p['bound_expr']}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_partitioned_tables(self) -> None:
        """Compare partitioned table parents: strategy and partition key."""
        cat = ComparisonCategory(name="partitioned_tables")
        src_pts = {pt["table"]: pt for pt in self._src.get_partitioned_tables(SCHEMA_NAME)}
        tgt_pts = {pt["table"]: pt for pt in self._tgt.get_partitioned_tables(SCHEMA_NAME)}
        cat.expected_count = len(src_pts)
        cat.actual_count = len(tgt_pts)
        cat.missing = sorted(set(src_pts) - set(tgt_pts))
        cat.unexpected = sorted(set(tgt_pts) - set(src_pts))
        for tbl in sorted(set(src_pts) & set(tgt_pts)):
            for key in ("strategy", "partition_key"):
                src_val = self._normalize_ddl(src_pts[tbl].get(key))
                tgt_val = self._normalize_ddl(tgt_pts[tbl].get(key))
                if src_val != tgt_val:
                    cat.mismatches.append(
                        f"Partitioned table {tbl}.{key}: "
                        f"{src_pts[tbl].get(key)} != {tgt_pts[tbl].get(key)}"
                    )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_security(self) -> None:
        cat = ComparisonCategory(name="security")
        src_roles = set(self._src.get_roles())
        tgt_roles = set(self._tgt.get_roles())
        cat.expected_count = len(src_roles)
        cat.actual_count = len(tgt_roles)
        cat.missing = sorted(src_roles - tgt_roles)
        cat.unexpected = sorted(tgt_roles - src_roles)
        if cat.missing or cat.unexpected:
            cat.fail(f"Roles differ: missing={cat.missing}, unexpected={cat.unexpected}")
        self._categories.append(cat)

    def _compare_rls_policies(self) -> None:
        cat = ComparisonCategory(name="rls_policies")
        from tests.e2e.postgresql.validation.expected import RLS_POLICIES
        for table_name in RLS_POLICIES:
            src_policies = self._src.get_rls_policies(SCHEMA_NAME, table_name)
            tgt_policies = self._tgt.get_rls_policies(SCHEMA_NAME, table_name)
            src_names = {p["name"] for p in src_policies}
            tgt_names = {p["name"] for p in tgt_policies}
            cat.expected_count += 1
            if src_names != tgt_names:
                cat.mismatches.append(
                    f"RLS {table_name}: src={sorted(src_names)} vs tgt={sorted(tgt_names)}"
                )
                cat.fail()
            for p in src_policies:
                tgt_p = next((tp for tp in tgt_policies if tp["name"] == p["name"]), None)
                if tgt_p:
                    for key in ("using", "with_check", "permissive"):
                        if self._normalize_ddl(p.get(key)) != self._normalize_ddl(tgt_p.get(key)):
                            cat.mismatches.append(
                                f"RLS policy {table_name}.{p['name']}.{key}: "
                                f"definition differs"
                            )
                            cat.fail()
        if not cat.mismatches:
            cat.actual_count = len(RLS_POLICIES)
        self._categories.append(cat)

    def _compare_grants(self) -> None:
        cat = ComparisonCategory(name="grants")
        src_grants = self._src.get_grants(SCHEMA_NAME)
        tgt_grants = self._tgt.get_grants(SCHEMA_NAME)

        def _gr_key(g: dict) -> str:
            return f"{g['grantee']}|{g['privilege']}|{g['object_type']}|{g['object_name']}"

        src_set = {_gr_key(g) for g in src_grants}
        tgt_set = {_gr_key(g) for g in tgt_grants}
        cat.expected_count = len(src_set)
        cat.actual_count = len(tgt_set)
        cat.missing = sorted(src_set - tgt_set)
        cat.unexpected = sorted(tgt_set - src_set)
        if cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_comments(self) -> None:
        cat = ComparisonCategory(name="comments")
        src_comments = self._src.get_comments(SCHEMA_NAME)
        tgt_comments = self._tgt.get_comments(SCHEMA_NAME)
        src_set = {(c["object_type"], c["object_name"]): c["comment"] for c in src_comments}
        tgt_set = {(c["object_type"], c["object_name"]): c["comment"] for c in tgt_comments}
        cat.expected_count = len(src_set)
        cat.actual_count = len(tgt_set)
        for key in sorted(src_set):
            if key not in tgt_set:
                cat.missing.append(f"{key[0]}:{key[1]}")
            elif self._normalize_ddl(src_set[key]) != self._normalize_ddl(tgt_set[key]):
                cat.mismatches.append(f"Comment {key[1]}: text differs")
        if cat.missing or cat.unexpected or cat.mismatches:
            cat.fail()
        self._categories.append(cat)

    def _compare_row_counts(self) -> None:
        cat = ComparisonCategory(name="row_counts")
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            cat.expected_count += 1
            src_count = self._src.get_row_count(SCHEMA_NAME, tbl)
            tgt_count = self._tgt.get_row_count(SCHEMA_NAME, tbl)
            if src_count != tgt_count:
                cat.mismatches.append(f"{tbl}: src={src_count}, tgt={tgt_count}")
                cat.fail()
            else:
                cat.actual_count += 1
        self._categories.append(cat)

    # ------------------------------------------------------------------ #
    # Report
    # ------------------------------------------------------------------ #

    def to_report(self) -> ValidationReport:
        report = ValidationReport(database="source-vs-target")
        for cat in self._categories:
            phase = PhaseResult(name=cat.name)
            phase.status = cat.status
            phase.add_check(CheckResult(
                name="comparison",
                status=cat.status,
                expected=cat.expected_count,
                actual=cat.actual_count,
                message=(
                    f"missing={cat.missing[:10]}, unexpected={cat.unexpected[:10]}, "
                    f"mismatches={cat.mismatches[:10]}"
                    if cat.missing or cat.unexpected or cat.mismatches
                    else "All objects match"
                ),
                details={
                    "missing": cat.missing,
                    "unexpected": cat.unexpected,
                    "mismatches": cat.mismatches,
                },
            ))
            phase.duration_s = 0.0
            report.phases.append(phase)
        return report
