# MySQL Local Audit — Final Report

**Project:** Migration Platform  
**Engine:** MySQL  
**Audit scope:** Local → Local FULL migration and supporting local object-validation scenarios  
**Status:** Evidence-aligned; endpoint/configuration caveat noted below  
**Primary reference:** `config/mysql_local_test.yaml` (current file is Azure → Local and does not reproduce this audit unchanged)

This audit separates report-backed migration results from validation recorded in the local audit history, unit-test coverage, and environment-dependent behavior. A zero source-object count means **Not Exercised** in that run, not unsupported.

## 1. Objective

Record the available evidence for MySQL Local → Local FULL migration: table/data movement, MySQL metadata and objects, representative runtime checks, count validation, reconciliation, and error handling. The audit does not claim complete MySQL feature or production coverage.

## 2. Environment

| Item | Audit environment recorded in documentation |
|---|---|
| Engine | MySQL Community Server |
| Local endpoint(s) | `127.0.0.1:3306` |
| Source database | `mysql_migration_source` |
| Target database | `mysql_migration_target` |
| Migration account | `mysql_test` |
| Mode | `full` |
| Target reconciliation | `migration.reconcile_target_schema: true` for the documented partition test |
| Secret provider | `env`; password values are not recorded |

**Configuration caveat:** The checked-in `config/mysql_local_test.yaml` currently specifies Azure MySQL as source and `127.0.0.1` as target. It is an Azure → Local config, not a Local → Local config. The report for run `1584f5a5ebc04e8a8d9e288ee150b23c` records MySQL source and target engines but does not contain endpoint hostnames. The existing audit history identifies the local test databases above; the current config and report artifacts do not independently establish host locality for that later run. Do not use the checked-in config unchanged to reproduce Local → Local.

Secrets are referenced as `password_secret` names and resolved from `SECRET_<name>` environment variables. No password values are included here.

## 3. Test Fixture

The documented local audit fixture contains representative customers, products, and orders, plus audit/log and procedure-test tables. It also includes scenarios for:

- Primary/foreign/unique keys, CHECK constraints, defaults, indexes, and `AUTO_INCREMENT`
- Generated columns and representative MySQL data types
- Views, functions, procedures, triggers, and Events
- A RANGE-partitioned table using `YEAR(created_at)`
- Table/column comments and grant/security metadata
- A cross-database foreign-key dependency

The fixture description is based on the local audit history. The report-backed 11-table run below verifies object counts and partition metadata; it does not by itself establish every runtime check listed in the historical audit notes. Datatype coverage is representative, not exhaustive.

## 4. Audit Method

1. Prepare the local source fixture and a dedicated target.
2. Record source object metadata and table row counts.
3. Run FULL migration and retain the JSON, HTML, and audit-log artifacts.
4. Compare target structure and source/target counts.
5. Run controlled positive/negative behavior checks for fixture objects.
6. Check cross-database dependencies, partition preservation, and the supported reconciliation case.
7. Review object-level results and classify each claim as report-backed, historical audit evidence, unit tested, not exercised, or environment dependent.

The detailed executable procedures are in [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md).

## 5. Object Coverage

| Category | Status | Evidence |
|---|---|---|
| Tables / data / columns | **Live Verified** | Run `74c3…`: 11 tables, 44 rows, 78 columns; all reported rows migrated. Run `1584…`: separate 5-table, 5,710-row MySQL → MySQL report. |
| Primary keys / `AUTO_INCREMENT` | **Live Verified** | Run `74c3…`: 11 primary keys and 9 auto-increment columns migrated. |
| UNIQUE constraints / indexes | **Live Verified** | Run `74c3…`: 3 unique constraints and 6 indexes; local audit history records duplicate-value rejection (MySQL 1062). |
| CHECK constraints | **Live Verified** | Run `74c3…`: 4 checks; local audit history records invalid-value rejection (MySQL 3819). |
| Foreign keys | **Live Verified** | Run `74c3…`: 2 foreign keys; local audit history records invalid-reference rejection (MySQL 1452). |
| Cross-database FK mapping | **Live Verified for tested scenario** | Dedicated local audit scenario records valid dependency behavior and invalid-reference rejection; not evidence for arbitrary/circular dependency graphs. |
| Defaults / generated columns | **Live Verified** | Run `74c3…`: 15 defaults and 2 generated columns. Audit history records target-side generated-value calculation. |
| Views | **Live Verified** | Run `74c3…`: 1 view; audit history records validation of migrated database-qualified dependencies against target objects. |
| Functions / procedures | **Live Verified; environment dependent** | Run `74c3…`: 2 functions and 2 procedures; local audit history records DDL/runtime checks after the Error 1419 prerequisite was addressed. |
| Triggers | **Live Verified; environment dependent** | Run `74c3…`: 2 triggers; audit history records controlled runtime validation. Creation still depends on target privileges/server policy. |
| Events | **Definition migrated; runtime conditional** | Run `74c3…`: 1 Event migrated. Event Scheduler, timing, definer, and privileges govern execution; the report does not prove automatic runtime. |
| Partitions | **Live Verified for tested RANGE case** | Run `74c3…`: 3 partitions on `tbl_partition_test`, `RANGE` over `YEAR(created_at)`; report partition checks passed. |
| Representative datatypes | **Live Verified for fixture** | Run includes `tbl_datatype_test`; local audit history describes representative numeric, character/text, binary, temporal, JSON, ENUM, and SET data. Not exhaustive. |
| Table/column comments | **Live Verified** | Run `74c3…`: 8 comment records; coverage is table/column comments. |
| Grants / routine privileges | **Conditionally supported; not exercised in run 74c…** | Run `74c…` reports grant source count 0. Separate local audit work found privilege metadata visibility can differ by account; routine EXECUTE discovery depends on metadata visibility. |
| User/security principals | **Live Verified in separate report; scope limited** | Run `1584…` reports 3 principals migrated; its endpoints are not recorded. Passwords/authentication secrets are not copied. |
| Target partition reconciliation | **Live Verified for tested scenario** | Local audit records an incompatible existing unpartitioned target table reconciled against a source partitioned table. Reconciliation is opt-in and scoped; it is not universal schema repair. |
| Object-level error isolation | **Implemented / unit tested** | Local audit records per-object results and `stop_on_error` behavior; do not infer a fault-injected E2E result from successful runs. |
| Count validation | **Live Verified** | Runs `74c…` and `1584…` report successful source/target count validation. |
| CDC / continuous replication | **Not E2E verified** | This audit covers FULL migration, not live CDC/binlog replication. |

The run IDs abbreviated in this table are expanded in Section 6. `74c…` object counts come from its report. Runtime behavior claims are identified as local audit-history evidence and are not inferred from those counts alone.

## 6. E2E Results

### Earlier broad Local → Local run

The report and audit artifacts for run `74c3b8076a6244bf94104b997b2cf89d` are present:

- **Mode/status:** FULL / SUCCESS
- **Tables:** 11
- **Source/migrated rows:** 44 / 44
- **Failed rows:** 0
- **Duration:** 2.48 seconds
- **Count validation:** success
- **Object results:** 78 columns, 11 primary keys, 9 `AUTO_INCREMENT`, 6 indexes, 3 unique constraints, 2 foreign keys, 4 CHECK constraints, 2 generated columns, 15 defaults, 3 partitions, 8 comments, 1 view, 2 functions, 2 procedures, 2 triggers, and 1 Event
- **Grants:** source count 0; not exercised

The report's row validation matched for all 11 tables. This is report-backed evidence for the specific fixture, not a statement of exhaustive MySQL support.

Artifacts: `reports/74c3b8076a6244bf94104b997b2cf89d.json`, `.html`, and `logs/74c3b8076a6244bf94104b997b2cf89d.jsonl`.

### Later-dated MySQL → MySQL report

Run `1584f5a5ebc04e8a8d9e288ee150b23c` (started 2026-09-29) records:

| Metric | Result |
|---|---:|
| Status / mode | SUCCESS / FULL |
| Tables | 5 |
| Source/migrated rows | 5,710 / 5,710 |
| Failed rows | 0 |
| Columns / primary keys | 73 / 5 |
| Security principals | 3 |
| Count validation | All five table counts matched |

Other object categories in this run had source count zero and were **Not Exercised**. The report/log record engine types, not endpoint hostnames; existing audit notes identify this as local, but locality cannot be independently checked in the artifact. The current checked-in config is Azure → Local, so it does not resolve this provenance gap.

### Additional Cloud → Local evidence

Run `fb374e4480d84894b22d5917807b507f` is a separate Azure → Local result: 13 tables, 45/45 rows, 0 failed, SUCCESS. It is not included as Local → Local evidence. See [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md) and [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md) for directional context.

## 7. Regression / Row Counts

Run `74c…` records the following per-table count validation; each source/target pair matched:

| Table | Source rows | Target rows |
|---|---:|---:|
| `customer_audit` | 1 | 1 |
| `customer_insert_log` | 1 | 1 |
| `customers` | 15 | 15 |
| `manual_test_core` | 2 | 2 |
| `migration_log` | 11 | 11 |
| `orders` | 4 | 4 |
| `parent_cross_schema_test` | 1 | 1 |
| `procedure_test_log` | 1 | 1 |
| `products` | 4 | 4 |
| `tbl_datatype_test` | 1 | 1 |
| `tbl_partition_test` | 3 | 3 |

The separate run `1584…` validated five tables with 5,710 source and target rows total. Its detailed per-table counts are in its JSON report.

## 8. Cross-Database Foreign-Key Validation

The local audit history records a foreign key whose referenced object is in another MySQL database. A valid reference succeeded and an invalid reference was rejected. When both the referencing object and dependency are included in migration scope, the target reference is mapped to the configured target database rather than left pointing at the source database.

This is evidence for the tested dependency scenario only; circular and more complex cross-database graphs were not exhaustively tested.

## 9. Dependency Ordering

For the audited MySQL path, tables and partition definitions are prepared before loading. Data is loaded parent-before-child where foreign-key dependencies are known. Indexes and constraints are applied after data; `AUTO_INCREMENT` state is synchronized after explicit IDs. Views, routines, triggers, and Events follow their table dependencies; comments and grants/security work follow object creation. Stored-object DEFINER accounts may be prepared before dependent routines/triggers/Events.

The order follows MySQL object dependencies; it is not evidence that every cyclic dependency graph will succeed.

## 10. Error Isolation

The configured default is `migration.stop_on_error: false`. At implemented object-level failure points, failures are recorded and eligible independent work can continue; a run may finish `partial_success`. `migration.stop_on_error: true` requests fail-fast behavior at applicable points. Dependencies can prevent downstream work, and not every phase has identical isolation behavior.

This behavior has implementation and unit-test evidence. Runs `74c…` and `1584…` succeeded; they are not fault-injected E2E demonstrations of partial-success behavior.

## 11. Known / Pre-existing Limitations

| Limitation | Classification / audit impact |
|---|---|
| Current `mysql_local_test.yaml` is Azure → Local, not Local → Local | Configuration/provenance gap; prepare a reviewed Local → Local config before reproducing this audit. |
| Run `1584…` artifacts omit endpoint hostnames | MySQL → MySQL success is report-backed; locality is attributed by existing audit notes, not independently verifiable from report/log. |
| Grant count is zero in run `74c…` | Grants were not exercised in that fixture; account metadata visibility also limits discovery. |
| Passwords/authentication secrets are not copied | Implementation scope; configure target authentication separately. |
| Global privileges and some grant scopes are limited | Only selected accounts and supported direct privileges are handled; target privileges/grantee and metadata visibility apply. |
| Event execution depends on scheduler, schedule, definer, and privileges | Event DDL/count evidence is not proof of automatic runtime. |
| Error 1419 can block function/trigger creation | Environment/server policy when binary logging is on and trust setting is off; the platform does not change the global setting. The local audit records administrator-approved `SET PERSIST log_bin_trust_function_creators = ON;` before successful routine/trigger validation. |
| Reconciliation is opt-in and scoped | Verified for the documented partition mismatch, not every MySQL schema difference. |
| Datatype and partition test coverage is representative | Other type variants and partition strategies are not exhaustively validated. |
| Circular cross-database FKs and external unmanaged dependencies | Not exhaustively tested; dependencies must be available on target. |
| CDC/binlog replication | Not live-verified by this FULL-mode audit. |
| Azure/remote behavior | Separate direction-specific evidence; not part of Local → Local conclusions. |

See [MYSQL_LIMITATIONS.md](MYSQL_LIMITATIONS.md) for the full limitation catalog.

## 12. Final Assessment

The repository contains a successful, report-backed 11-table/44-row MySQL FULL run with object counts, matching table counts, and passing partition checks. A separate later-dated MySQL → MySQL report records five tables and 5,710 matching rows, but does not record endpoint hostnames; the currently checked-in config is Azure → Local, so that report's Local → Local attribution cannot be independently confirmed from its artifacts.

The local audit history also records positive/negative constraint checks, generated-column behavior, views, routines, triggers, cross-database FK behavior, and a supported partition-reconciliation scenario. These findings apply to the tested fixtures. Grants were not exercised in run `74c…`; Event runtime remains environment-dependent; error isolation is supported by implementation/unit evidence rather than fault-injected success runs. This evidence does not establish exhaustive MySQL or production coverage.

## 13. Reproduction Instructions

The checked-in `config/mysql_local_test.yaml` currently runs Azure MySQL → local MySQL. Do not use it unchanged for this Local → Local audit. Prepare and review a dedicated Local → Local config with the documented source/target databases, local hosts, `migration.mode: full`, `secrets.provider: env`, and the intended reconciliation setting.

1. Confirm both MySQL servers/databases are available and dedicated to testing.
2. Set the configured secret references in PowerShell:

   ```powershell
   $env:SECRET_mysql_source_pass = "<source-password>"
   $env:SECRET_mysql_target_pass = "<target-password>"
   ```

3. Check server version and, if functions/triggers are in scope, inspect binary-log policy:

   ```sql
   SELECT VERSION(), @@hostname, DATABASE();
   SHOW VARIABLES LIKE 'log_bin';
   SHOW VARIABLES LIKE 'log_bin_trust_function_creators';
   ```

   An administrator may apply `SET PERSIST log_bin_trust_function_creators = ON;` if required and approved. The DMS does not do this automatically.

4. Verify source and target databases, then run from the repository root:

   ```powershell
   python -m migration_platform --config <reviewed-local-to-local-config>.yaml --mode full --no-live-ui
   ```

5. Save the run ID; inspect `reports/<run_id>.json`, `.html`, and `logs/<run_id>.jsonl` when generated.
6. Compare table inventories, `SHOW CREATE TABLE` output, and exact source/target `COUNT(*)` for each migrated table. Check partition metadata, views, routines, triggers, Events, comments, and visible grants only where present in the fixture.
7. Run the current unit suite and record its actual result:

   ```powershell
   python -m pytest tests/unit -q
   ```

Do not use the Azure → Local run as Local → Local reproduction evidence. Detailed SQL and fixture checks are in [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md).

## 14. Reference

- [MySQL Object Support Matrix](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- [MySQL Limitations](MYSQL_LIMITATIONS.md)
- [MySQL Test Guide](MYSQL_TEST_GUIDE.md)
- [MySQL E2E Runbook](MYSQL_E2E_RUNBOOK.md)
- [MySQL Migration Flow](MYSQL_MIGRATION_FLOW.md)
- [MySQL Local → Azure Audit](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
