"""Source-vs-target structural comparison for MSSQL E2E.

Builds a reusable comparison layer that queries both source and target
catalogs via ``MSSQLCatalog`` and reports per-category deltas.  This is
*not* a duplicate of the structural validator — it compares two live
databases rather than validating a single database against the fixture
contract in ``expected.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tests.e2e.mssql.validation.catalog import MSSQLCatalog
from tests.e2e.mssql.validation.expected import SCHEMA_NAME
from tests.e2e.mssql.validation.models import (
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
    """Compares source and target MSSQL databases catalog-by-catalog."""

    def __init__(self, source: MSSQLCatalog, target: MSSQLCatalog) -> None:
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
        self._compare_default_constraints()
        self._compare_indexes()
        self._compare_views()
        self._compare_functions()
        self._compare_procedures()
        self._compare_triggers()
        self._compare_sequences()
        self._compare_synonyms()
        self._compare_user_types()
        self._compare_partitioning()
        self._compare_security()
        self._compare_extended_properties()
        self._compare_row_counts()

    # ------------------------------------------------------------------ #
    # Per-category comparisons
    # ------------------------------------------------------------------ #

    def _compare_schemas(self) -> None:
        cat = ComparisonCategory(name="schemas")
        src_schemas = set(self._src.get_all_user_schemas())
        tgt_schemas = set(self._tgt.get_all_user_schemas())
        cat.expected_count = len(src_schemas)
        cat.actual_count = len(tgt_schemas)
        cat.missing = sorted(src_schemas - tgt_schemas)
        cat.unexpected = sorted(tgt_schemas - src_schemas)
        if cat.missing or cat.unexpected:
            cat.fail(
                f"Schemas differ: missing={cat.missing}, "
                f"unexpected={cat.unexpected}"
            )
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
            cat.fail(
                f"Tables differ: missing={cat.missing}, "
                f"unexpected={cat.unexpected}"
            )
        self._categories.append(cat)

    def _compare_columns(self) -> None:
        cat = ComparisonCategory(name="columns")
        all_tables = set(self._src.get_base_tables(SCHEMA_NAME))
        for tbl in sorted(all_tables):
            src_cols = {
                c["name"]: c for c in self._src.get_columns(SCHEMA_NAME, tbl)
            }
            tgt_cols = {
                c["name"]: c for c in self._tgt.get_columns(SCHEMA_NAME, tbl)
            }
            cat.expected_count += len(src_cols)
            cat.actual_count += len(tgt_cols)
            for col_name in sorted(src_cols):
                if col_name not in tgt_cols:
                    cat.missing.append(f"{tbl}.{col_name}")
                else:
                    src_c = src_cols[col_name]
                    tgt_c = tgt_cols[col_name]
                    for key in ("base_type", "is_nullable", "is_identity",
                              "is_computed", "max_length", "numeric_precision",
                              "numeric_scale", "udt_name"):
                        if src_c.get(key) != tgt_c.get(key):
                            cat.mismatches.append(
                                f"{tbl}.{col_name}.{key}: "
                                f"{src_c.get(key)!r} != {tgt_c.get(key)!r}"
                            )
            for col_name in tgt_cols:
                if col_name not in src_cols:
                    cat.unexpected.append(f"{tbl}.{col_name}")
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
        cat = self._compare_per_table_constraint(
            name="unique_constraints",
            src_fn=lambda t: self._src.get_unique_constraints(SCHEMA_NAME, t),
            tgt_fn=lambda t: self._tgt.get_unique_constraints(SCHEMA_NAME, t),
            key="name",
        )
        self._categories.append(cat)

    def _compare_check_constraints(self) -> None:
        cat = self._compare_per_table_constraint(
            name="check_constraints",
            src_fn=lambda t: self._src.get_check_constraints(SCHEMA_NAME, t),
            tgt_fn=lambda t: self._tgt.get_check_constraints(SCHEMA_NAME, t),
            key="name",
        )
        self._categories.append(cat)

    def _compare_default_constraints(self) -> None:
        cat = self._compare_per_table_constraint(
            name="default_constraints",
            src_fn=lambda t: self._src.get_default_constraints(SCHEMA_NAME, t),
            tgt_fn=lambda t: self._tgt.get_default_constraints(SCHEMA_NAME, t),
            key="name",
        )
        self._categories.append(cat)

    def _compare_per_table_constraint(
        self, name: str, src_fn, tgt_fn, key: str
    ) -> ComparisonCategory:
        cat = ComparisonCategory(name=name)
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            src_list = src_fn(tbl)
            tgt_list = tgt_fn(tbl)
            cat.expected_count += len(src_list)
            cat.actual_count += len(tgt_list)
            src_map = {item[key]: item for item in src_list}
            tgt_map = {item[key]: item for item in tgt_list}
            for k in sorted(src_map):
                if k not in tgt_map:
                    cat.missing.append(f"{tbl}.{k}")
                elif src_map[k] != tgt_map[k]:
                    cat.mismatches.append(
                        f"{tbl}.{k}: {src_map[k]} != {tgt_map[k]}"
                    )
            for k in tgt_map:
                if k not in src_map:
                    cat.unexpected.append(f"{tbl}.{k}")
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        return cat

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
                    for key in ("is_unique", "type_desc", "columns",
                              "included_columns", "filter_definition"):
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

    def _normalize_ddl(self, definition: str) -> str:
        """Normalize a DDL definition for comparison.

        Strips comments, normalizes whitespace, normalizes identifier
        quoting (brackets vs none), and lowercases keywords so that
        semantically equivalent definitions that differ only in formatting
        are treated as identical.
        """
        text = definition
        text = re.sub(r"--[^\n]*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\[(\w+)\]", r"\1", text)
        return text.lower()

    def _compare_views(self) -> None:
        cat = ComparisonCategory(name="views")
        src_views = self._src.get_views(SCHEMA_NAME)
        tgt_views = self._tgt.get_views(SCHEMA_NAME)
        cat.expected_count = len(src_views)
        cat.actual_count = len(tgt_views)
        cat.missing = sorted(set(src_views) - set(tgt_views))
        cat.unexpected = sorted(set(tgt_views) - set(src_views))
        for v in sorted(set(src_views) & set(tgt_views)):
            if self._normalize_ddl(src_views[v]) != self._normalize_ddl(tgt_views[v]):
                cat.mismatches.append(
                    f"View {v} definition differs"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_functions(self) -> None:
        cat = ComparisonCategory(name="functions")
        src_fn = self._src.get_functions(SCHEMA_NAME)
        tgt_fn = self._tgt.get_functions(SCHEMA_NAME)
        cat.expected_count = len(src_fn)
        cat.actual_count = len(tgt_fn)
        cat.missing = sorted(set(src_fn) - set(tgt_fn))
        cat.unexpected = sorted(set(tgt_fn) - set(src_fn))
        for f in sorted(set(src_fn) & set(tgt_fn)):
            if src_fn[f]["type"] != tgt_fn[f]["type"]:
                cat.mismatches.append(
                    f"Function {f}: type {src_fn[f]['type']} != {tgt_fn[f]['type']}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_procedures(self) -> None:
        cat = ComparisonCategory(name="procedures")
        src_proc = self._src.get_procedures(SCHEMA_NAME)
        tgt_proc = self._tgt.get_procedures(SCHEMA_NAME)
        cat.expected_count = len(src_proc)
        cat.actual_count = len(tgt_proc)
        cat.missing = sorted(set(src_proc) - set(tgt_proc))
        cat.unexpected = sorted(set(tgt_proc) - set(src_proc))
        if cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_triggers(self) -> None:
        cat = ComparisonCategory(name="triggers")
        src_trig = {
            t["name"]: t for t in self._src.get_triggers(SCHEMA_NAME)
        }
        tgt_trig = {
            t["name"]: t for t in self._tgt.get_triggers(SCHEMA_NAME)
        }
        cat.expected_count = len(src_trig)
        cat.actual_count = len(tgt_trig)
        cat.missing = sorted(set(src_trig) - set(tgt_trig))
        cat.unexpected = sorted(set(tgt_trig) - set(src_trig))
        for t in sorted(set(src_trig) & set(tgt_trig)):
            if src_trig[t] != tgt_trig[t]:
                cat.mismatches.append(
                    f"Trigger {t}: {src_trig[t]} != {tgt_trig[t]}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_sequences(self) -> None:
        cat = ComparisonCategory(name="sequences")
        src_seq = self._src.get_sequences(SCHEMA_NAME)
        tgt_seq = self._tgt.get_sequences(SCHEMA_NAME)
        cat.expected_count = len(src_seq)
        cat.actual_count = len(tgt_seq)
        cat.missing = sorted(set(src_seq) - set(tgt_seq))
        cat.unexpected = sorted(set(tgt_seq) - set(src_seq))
        for s in sorted(set(src_seq) & set(tgt_seq)):
            for key in ("data_type", "start_value"):
                if src_seq[s].get(key) != tgt_seq[s].get(key):
                    cat.mismatches.append(
                        f"Sequence {s}.{key}: "
                        f"{src_seq[s].get(key)!r} != {tgt_seq[s].get(key)!r}"
                    )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_synonyms(self) -> None:
        cat = ComparisonCategory(name="synonyms")
        src_syn = self._src.get_synonyms(SCHEMA_NAME)
        tgt_syn = self._tgt.get_synonyms(SCHEMA_NAME)
        cat.expected_count = len(src_syn)
        cat.actual_count = len(tgt_syn)
        cat.missing = sorted(set(src_syn) - set(tgt_syn))
        cat.unexpected = sorted(set(tgt_syn) - set(src_syn))
        for s in sorted(set(src_syn) & set(tgt_syn)):
            if src_syn[s] != tgt_syn[s]:
                cat.mismatches.append(
                    f"Synonym {s}: {src_syn[s]} != {tgt_syn[s]}"
                )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_user_types(self) -> None:
        cat = ComparisonCategory(name="user_types")
        src_ut = self._src.get_user_types(SCHEMA_NAME)
        tgt_ut = self._tgt.get_user_types(SCHEMA_NAME)
        cat.expected_count = len(src_ut)
        cat.actual_count = len(tgt_ut)
        cat.missing = sorted(set(src_ut) - set(tgt_ut))
        cat.unexpected = sorted(set(tgt_ut) - set(src_ut))
        for u in sorted(set(src_ut) & set(tgt_ut)):
            for key in ("base_type", "is_nullable"):
                if src_ut[u].get(key) != tgt_ut[u].get(key):
                    cat.mismatches.append(
                        f"UDT {u}.{key}: "
                        f"{src_ut[u].get(key)!r} != {tgt_ut[u].get(key)!r}"
                    )
        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_partitioning(self) -> None:
        cat = ComparisonCategory(name="partitioning")

        # Partition functions
        src_pf = {pf["name"]: pf for pf in self._src.get_partition_functions()}
        tgt_pf = {pf["name"]: pf for pf in self._tgt.get_partition_functions()}
        cat.expected_count += len(src_pf)
        cat.actual_count += len(tgt_pf)
        pf_missing = sorted(set(src_pf) - set(tgt_pf))
        pf_unexpected = sorted(set(tgt_pf) - set(src_pf))
        for pf in pf_missing:
            cat.missing.append(f"partition_function:{pf}")
        for pf in pf_unexpected:
            cat.unexpected.append(f"partition_function:{pf}")

        # Partition schemes
        src_ps = {ps["name"]: ps for ps in self._src.get_partition_schemes()}
        tgt_ps = {ps["name"]: ps for ps in self._tgt.get_partition_schemes()}
        cat.expected_count += len(src_ps)
        cat.actual_count += len(tgt_ps)
        for ps in sorted(set(src_ps) - set(tgt_ps)):
            cat.missing.append(f"partition_scheme:{ps}")
        for ps in sorted(set(tgt_ps) - set(src_ps)):
            cat.unexpected.append(f"partition_scheme:{ps}")
        for ps in sorted(set(src_ps) & set(tgt_ps)):
            if src_ps[ps]["function"] != tgt_ps[ps]["function"]:
                cat.mismatches.append(
                    f"Scheme {ps}: function "
                    f"{src_ps[ps]['function']} != {tgt_ps[ps]['function']}"
                )

        # Partitioned tables
        src_pt = {
            pt["table"]: pt for pt in self._src.get_partitioned_tables(SCHEMA_NAME)
        }
        tgt_pt = {
            pt["table"]: pt for pt in self._tgt.get_partitioned_tables(SCHEMA_NAME)
        }
        cat.expected_count += len(src_pt)
        cat.actual_count += len(tgt_pt)
        for pt in sorted(set(src_pt) - set(tgt_pt)):
            cat.missing.append(f"partitioned_table:{pt}")
        for pt in sorted(set(tgt_pt) - set(src_pt)):
            cat.unexpected.append(f"partitioned_table:{pt}")
        for pt in sorted(set(src_pt) & set(tgt_pt)):
            for key in ("partition_column", "scheme"):
                if src_pt[pt].get(key) != tgt_pt[pt].get(key):
                    cat.mismatches.append(
                        f"Partitioned table {pt}.{key}: "
                        f"{src_pt[pt].get(key)!r} != {tgt_pt[pt].get(key)!r}"
                    )

        if cat.mismatches or cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_security(self) -> None:
        cat = ComparisonCategory(name="security")
        src_sec = self._src.get_security_info()
        tgt_sec = self._tgt.get_security_info()

        for key in ("roles", "users", "memberships"):
            src_set = {
                tuple(sorted(d.items())) if isinstance(d, dict) else d
                for d in src_sec[key]
            }
            tgt_set = {
                tuple(sorted(d.items())) if isinstance(d, dict) else d
                for d in tgt_sec[key]
            }
            cat.expected_count += len(src_set)
            cat.actual_count += len(tgt_set)
            missing = sorted(src_set - tgt_set, key=str)
            unexpected = sorted(tgt_set - src_set, key=str)
            for m in missing:
                cat.missing.append(f"security:{key}:{m}")
            for u in unexpected:
                cat.unexpected.append(f"security:{key}:{u}")

        # Grants
        src_grants = {(g["grantee"], g["privilege"], g["object_name"])
                       for g in src_sec["grants"]}
        tgt_grants = {(g["grantee"], g["privilege"], g["object_name"])
                       for g in tgt_sec["grants"]}
        cat.expected_count += len(src_grants)
        cat.actual_count += len(tgt_grants)
        g_missing = sorted(src_grants - tgt_grants, key=str)
        g_unexpected = sorted(tgt_grants - src_grants, key=str)
        cat.missing.extend(f"grant:{m}" for m in g_missing)
        cat.unexpected.extend(f"grant:{u}" for u in g_unexpected)

        if cat.missing or cat.unexpected:
            cat.fail()
        self._categories.append(cat)

    def _compare_extended_properties(self) -> None:
        cat = ComparisonCategory(name="extended_properties")
        src_ep = self._src.get_extended_properties(SCHEMA_NAME)
        tgt_ep = self._tgt.get_extended_properties(SCHEMA_NAME)
        cat.expected_count = len(src_ep)
        cat.actual_count = len(tgt_ep)
        if len(src_ep) != len(tgt_ep):
            cat.fail(
                f"Extended property count differs: src={len(src_ep)}, "
                f"tgt={len(tgt_ep)}"
            )
        self._categories.append(cat)

    def _compare_row_counts(self) -> None:
        cat = ComparisonCategory(name="row_counts")
        for tbl in sorted(self._src.get_base_tables(SCHEMA_NAME)):
            cat.expected_count += 1
            src_count = self._src.get_row_count(SCHEMA_NAME, tbl)
            tgt_count = self._tgt.get_row_count(SCHEMA_NAME, tbl)
            if src_count != tgt_count:
                cat.mismatches.append(
                    f"{tbl}: src={src_count}, tgt={tgt_count}"
                )
                cat.fail()
            else:
                cat.actual_count += 1
        self._categories.append(cat)

    # ------------------------------------------------------------------ #
    # Report builders
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
