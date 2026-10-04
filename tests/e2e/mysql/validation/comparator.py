"""Source-vs-target structural comparison for MySQL E2E.

Builds a reusable comparison layer that queries both source and target
catalogs via ``MySQLCatalog`` and reports per-category deltas.  This is
*not* a duplicate of the structural validator — it compares two live databases
rather than validating a single database against the fixture contract in
``expected.py``.

MySQL-specific notes:
  - MySQL has no RLS policies, sequences, extensions, custom types, or
    partitioned indexes. Columns use AUTO_INCREMENT instead of identity.
  - View definitions are normalized before comparison (whitespace,
    identifier quoting, keyword case) to avoid false mismatches.
  - MySQL's INFORMATION_SCHEMA includes routine definitions in plain text;
    these are normalized but not deeply compared (body comparison is
    best-effort — MySQL may rewrite formatting).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tests.e2e.mysql.validation.catalog import MySQLCatalog
from tests.e2e.mysql.validation.models import (
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


class MySQLSourceTargetComparator:
    """Compares source and target MySQL databases catalog-by-catalog."""

    def __init__(self, source: MySQLCatalog, target: MySQLCatalog) -> None:
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
        self._compare_partitions()
        self._compare_comments()
        self._compare_row_counts()

    @staticmethod
    def _normalize_ddl(definition: str | None) -> str:
        """Normalize a DDL/SQL definition for comparison.

        Strips comments, collapses whitespace, removes MySQL identifier
        quoting (backticks), and lowercases keywords so semantically-equivalent
        definitions that differ only in formatting or quoting are treated
        as identical.
        """
        if not definition:
            return ""
        if not isinstance(definition, str):
            return str(definition).lower()
        text = definition
        text = re.sub(r"--[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = text.lower()
        return text

    def _compare_schemas(self) -> None:
        cat = ComparisonCategory(name="schemas")
        src_schemas = set(self._src.get_schemas())
        tgt_schemas = set(self._tgt.get_schemas())
        cat.expected_count = len(src_schemas)
        cat.actual_count = len(tgt_schemas)
        cat.missing = sorted(src_schemas - tgt_schemas)
        cat.unexpected = sorted(tgt_schemas - src_schemas)
        if cat.missing or cat.unexpected:
            cat.fail(f"Schemas differ: missing={cat.missing}, unexpected={cat.unexpected}")
        self._categories.append(cat)

    def _compare_tables(self) -> None:
        cat = ComparisonCategory(name="tables")
        db = self._src.database_name
        src = set(self._src.get_base_tables(db))
        tgt = set(self._tgt.get_base_tables(db))
        cat.expected_count = len(src)
        cat.actual_count = len(tgt)
        cat.missing = sorted(src - tgt)
        cat.unexpected = sorted(tgt - src)
        if cat.missing or cat.unexpected:
            cat.fail(f"Tables differ: missing={cat.missing}, unexpected={cat.unexpected}")
        self._categories.append(cat)

    def _compare_columns(self) -> None:
        cat = ComparisonCategory(name="columns")
        db = self._src.database_name
        all_tables = set(self._src.get_base_tables(db))
        for tbl in sorted(all_tables):
            src_cols = {c["name"]: c for c in self._src.get_columns(db, tbl)}
            tgt_cols = {c["name"]: c for c in self._tgt.get_columns(db, tbl)}
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
                for key in ("source_type", "is_nullable", "is_identity",
                            "is_generated", "size", "precision", "scale",
                            "default", "column_key"):
                    sv = src_c.get(key)
                    tv = tgt_c.get(key)
                    if sv != tv:
                        cat.mismatches.append(
                            f"{tbl}.{col_name}.{key}: {sv!r} != {tv!r}"
                        )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_primary_keys(self) -> None:
        cat = ComparisonCategory(name="primary_keys")
        db = self._src.database_name
        for tbl in sorted(self._src.get_base_tables(db)):
            cat.expected_count += 1
            src_pk = self._src.get_primary_key(db, tbl)
            tgt_pk = self._tgt.get_primary_key(db, tbl)
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
        db = self._src.database_name
        src_fks = {fk["name"]: fk for fk in self._src.get_foreign_keys(db)}
        tgt_fks = {fk["name"]: fk for fk in self._tgt.get_foreign_keys(db)}
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
        db = self._src.database_name
        for tbl in sorted(self._src.get_base_tables(db)):
            src_list = self._src.get_unique_constraints_for_table(db, tbl)
            tgt_list = self._tgt.get_unique_constraints_for_table(db, tbl)
            cat.expected_count += len(src_list)
            cat.actual_count += len(tgt_list)
            src_map = {uc["name"]: uc["columns"] for uc in src_list}
            tgt_map = {uc["name"]: uc["columns"] for uc in tgt_list}
            for name in sorted(src_map):
                if name not in tgt_map:
                    cat.missing.append(f"{tbl}.{name}")
                elif src_map[name] != tgt_map[name]:
                    cat.mismatches.append(
                        f"UQ {tbl}.{name}: {src_map[name]} != {tgt_map[name]}"
                    )
            for name in tgt_map:
                if name not in src_map:
                    cat.unexpected.append(f"{tbl}.{name}")
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_check_constraints(self) -> None:
        cat = ComparisonCategory(name="check_constraints")
        db = self._src.database_name
        for tbl in sorted(self._src.get_base_tables(db)):
            src_list = self._src.get_check_constraints(db, tbl)
            tgt_list = self._tgt.get_check_constraints(db, tbl)
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
        db = self._src.database_name
        for tbl in sorted(self._src.get_base_tables(db)):
            src_idxs = {i["name"]: i for i in self._src.get_indexes(db, tbl)}
            tgt_idxs = {i["name"]: i for i in self._tgt.get_indexes(db, tbl)}
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
        db = self._src.database_name
        src_views = {v["name"]: v for v in self._src.get_views(db)}
        tgt_views = {v["name"]: v for v in self._tgt.get_views(db)}
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
        """Compare functions/procedures by name."""
        cat = ComparisonCategory(name="routines")
        db = self._src.database_name
        src_fns = {f["name"]: f for f in self._src.get_functions(db)}
        tgt_fns = {f["name"]: f for f in self._tgt.get_functions(db)}
        src_procs = {p["name"]: p for p in self._src.get_procedures(db)}
        tgt_procs = {p["name"]: p for p in self._tgt.get_procedures(db)}

        src_all = {**src_fns, **src_procs}
        tgt_all = {**tgt_fns, **tgt_procs}

        cat.expected_count = len(src_all)
        cat.actual_count = len(tgt_all)
        cat.missing = sorted(set(src_all) - set(tgt_all))
        cat.unexpected = sorted(set(tgt_all) - set(src_all))
        for name in sorted(set(src_all) & set(tgt_all)):
            if src_all[name]["kind"] != tgt_all[name]["kind"]:
                cat.mismatches.append(
                    f"Routine {name}: kind {src_all[name]['kind']} != {tgt_all[name]['kind']}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_triggers(self) -> None:
        cat = ComparisonCategory(name="triggers")
        db = self._src.database_name
        src_trig = {t["name"]: t for t in self._src.get_triggers(db)}
        tgt_trig = {t["name"]: t for t in self._tgt.get_triggers(db)}
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

    def _compare_partitions(self) -> None:
        cat = ComparisonCategory(name="partitions")
        db = self._src.database_name
        src_parts = self._src.get_partitions(db)
        tgt_parts = self._tgt.get_partitions(db)

        def _key(p: dict) -> str:
            return f"{p['table']}.{p['partition_name']}"

        src_map = {_key(p): p for p in src_parts}
        tgt_map = {_key(p): p for p in tgt_parts}
        cat.expected_count = len(src_map)
        cat.actual_count = len(tgt_map)
        cat.missing = sorted(set(src_map) - set(tgt_map))
        cat.unexpected = sorted(set(tgt_map) - set(src_map))
        for key in sorted(set(src_map) & set(tgt_map)):
            src_p = src_map[key]
            tgt_p = tgt_map[key]
            if src_p["method"] != tgt_p["method"]:
                cat.mismatches.append(
                    f"Partition {key}: method {src_p['method']} != {tgt_p['method']}"
                )
            src_desc = self._normalize_ddl(src_p["description"])
            tgt_desc = self._normalize_ddl(tgt_p["description"])
            if src_desc != tgt_desc:
                cat.mismatches.append(
                    f"Partition {key}: description {src_desc} != {tgt_desc}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_comments(self) -> None:
        cat = ComparisonCategory(name="comments")
        db = self._src.database_name
        src_comments = self._src.get_comments(db)
        tgt_comments = self._tgt.get_comments(db)
        src_set = {(c["object_type"], c["object_name"]): c["comment"] for c in src_comments}
        tgt_set = {(c["object_type"], c["object_name"]): c["comment"] for c in tgt_comments}
        cat.expected_count = len(src_set)
        cat.actual_count = len(tgt_set)
        for key in sorted(src_set):
            if key not in tgt_set:
                cat.missing.append(f"{key[0]}:{key[1]}")
            elif self._normalize_ddl(src_set[key]) != self._normalize_ddl(tgt_set[key]):
                cat.mismatches.append(f"Comment {key[1]}: text differs")
        for key in tgt_set:
            if key not in src_set:
                cat.unexpected.append(f"{key[0]}:{key[1]}")
        if cat.missing or cat.unexpected or cat.mismatches:
            cat.fail()
        self._categories.append(cat)

    def _compare_row_counts(self) -> None:
        cat = ComparisonCategory(name="row_counts")
        db = self._src.database_name
        for tbl in sorted(self._src.get_base_tables(db)):
            cat.expected_count += 1
            src_count = self._src.get_row_count(db, tbl)
            tgt_count = self._tgt.get_row_count(db, tbl)
            if src_count != tgt_count:
                cat.mismatches.append(f"{tbl}: src={src_count}, tgt={tgt_count}")
                cat.fail()
            else:
                cat.actual_count += 1
        self._categories.append(cat)

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
