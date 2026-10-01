# MySQL Object Support Matrix

Quick reference for MySQL migration capabilities and the evidence available in this repository. Directional status refers only to the cited run/dataset; it is not a guarantee for every object variation or server configuration.

## Classification

| Status | Meaning |
|---|---|
| **Supported / Verified** | Implemented and exercised in the cited E2E scenario. |
| **Supported / Partial** | Implemented, with known scope or behavior limits. |
| **Implemented / Not E2E tested** | Implementation or focused tests exist; direction-specific E2E evidence is absent. |
| **Not Exercised** | The cited source dataset had no such object or did not exercise the behavior. |
| **Environment Dependent** | Requires privileges, metadata visibility, server policy, or configuration. |
| **Out of Scope / N/A** | Intentionally excluded or no direct native MySQL equivalent. |

“Not Exercised” and “Environment Dependent” do not mean unsupported or failed.

## Evidence Direction Key

| Direction | Configured scenario / evidence |
|---|---|
| **Local → Local** | The local audit documents a historical broad run (11 tables, 44/44 rows). A checked-in report, `1584f5a5ebc04e8a8d9e288ee150b23c`, records a separate MySQL → MySQL run (5 tables, 5,710/5,710 rows); most object categories in that dataset had source count 0. The historical broad run's report artifact is not in the current reports directory. |
| **Local → Cloud** | `config/mysql_onpremise_cloud_test.yaml`; run `9f15eae2f1a84933a7ffe9746b828932`. |
| **Cloud → Local** | `config/mysql_local_test.yaml`; run `fb374e4480d84894b22d5917807b507f`. |

## 1. Databases / Namespace

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Target database selection/creation | Supported / Verified | Verified in checked-in report | Verified in run | Verified in run | MySQL database is the object namespace. Existing config names and target provisioning must be checked for each environment. |
| PostgreSQL-style schemas | Out of Scope / N/A | N/A | N/A | N/A | MySQL databases are not PostgreSQL schemas; `include_schemas` is not the MySQL namespace mapping mechanism. |

## 2. Tables / Columns

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Tables and row data | Supported / Verified | 5 tables / 5,710 rows in report `1584…`; historical broad audit 11 / 44 | 6 / 6,340 | 13 / 45 | All cited runs report successful migration and zero failed rows. The different fixtures are not directly comparable. |
| Columns and MySQL data types | Supported / Partial | 73 columns in report `1584…`; representative datatype audit | 66 columns | 65 columns | Data-type coverage is representative, not exhaustive. See `tests/unit/test_mysql_datatypes.py`. |
| NULL / NOT NULL, defaults | Supported / Verified | Defaults not exercised in report `1584…`; covered by historical audit | Not exercised in this run | 13 defaults in run | Preserve source behavior where the type/definition is supported. |
| Primary keys | Supported / Verified | 5 in report `1584…` | 6 | 13 | Counts are direction-specific. |
| UNIQUE constraints / indexes | Supported / Verified | Not exercised in report `1584…`; historical audit coverage | Not exercised in this run | 3 unique constraints | See audit/support details for local functional checks. |
| CHECK constraints | Supported / Verified | Not exercised in report `1584…`; historical audit coverage | Not exercised in this run | 6 | Constraint rejection is also covered by local audit evidence. |
| Foreign keys | Supported / Verified | Not exercised in report `1584…`; cross-database case documented in local audit | Not exercised in this run | 3 | Complex/circular cross-database graphs are not exhaustively validated. |
| `AUTO_INCREMENT` | Supported / Verified | Not exercised in report `1584…`; historical audit coverage | Not exercised in this run | 9 | MySQL-native auto-increment metadata/state, not MSSQL IDENTITY. |
| Generated columns | Supported / Verified | Not exercised in report `1584…`; historical audit coverage | Not exercised in this run | 2 | Generated values are recalculated by MySQL, not inserted as ordinary values. |
| Representative datatype fixture / SET values | Supported / Partial | Datatype-specific validation documented | Not exercised in cloud run | Dataset-specific | `SET` collection values are normalized by connector; focused unit tests exist. No claim of exhaustive datatype coverage. |

## 3. Indexes / Partitions

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Secondary, unique, composite indexes | Supported / Verified | Historical audit; not exercised in report `1584…` | Not exercised in this run | 13 indexes reported | Index variants should be checked against the actual source fixture. |
| MySQL partition definitions | Supported / Partial | RANGE/YEAR scenario and reconciliation documented in local audit | Not exercised in this run | 14 partitions reported | Connector handles RANGE, RANGE COLUMNS, LIST, LIST COLUMNS, HASH, and KEY; live audit coverage is narrower than implementation scope. |
| Target schema reconciliation | Supported / Partial | Tested for a supported partition mismatch | Not exercised in this run | Not exercised in this run | Opt-in `migration.reconcile_target_schema`; not a general repair facility. A clean target is preferred for normal E2E. |

## 4. Views / Stored Routines

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Views | Supported / Verified | Historical local audit; 0 in report `1584…` | Not exercised in this run | 2 | Source database qualifiers are rewritten for migrated dependencies only; arbitrary SQL/external references are not rewritten. |
| Stored functions | Supported / Environment Dependent | Historical local audit; 0 in report `1584…` | Not exercised in this run | 2 definitions migrated | Creation/runtime depends on target privileges and binary-log policy. |
| Stored procedures | Supported / Verified | Historical local audit; 0 in report `1584…` | Not exercised in this run | 2 definitions migrated | Runtime should be tested separately from metadata creation. |
| `DEFINER` handling | Supported / Partial | Unit and audit evidence | Not exercised in this run | Present in routine/event coverage | MySQL-to-MySQL config preserves compatible source definers when possible; fallback/override behavior and privileges are configuration/environment dependent. Exact identity preservation is not unconditional. |

## 5. Triggers / Events

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Triggers | Supported / Environment Dependent | Historical local audit (2); 0 in report `1584…` | Not exercised in this run | 2 | DDL/metadata and runtime evidence exist for cited fixtures; creation may be blocked by target server policy. |
| Event DDL / metadata | Supported / Partial | Historical local audit; 0 in report `1584…` | Event metadata in Azure audit, direction-specific run categories vary | 2 in run | Definition migration is distinct from scheduler-driven execution. |
| Recurring event schedule/state | Supported / Partial | Fixture-specific local evidence | Metadata/state checked in Azure audit | Included in event evidence | Do not infer automatic runtime from `STATUS=ENABLED`. |
| Enabled one-time event safety window | Supported / Unit tested | Not counted as broad E2E | Not E2E verified for all schedules | Unit tests cover due/near-event blocking | Default safety lead is 300 seconds; unsafe events are blocked rather than silently shifted/enabled. |
| Event Scheduler runtime | Environment Dependent | Depends on server state | Blocked/not executed in documented Azure environment (`event_scheduler=OFF`) | Depends on local target state | Runtime also depends on timing, definer, and privileges. |
| Target-only events in FULL mode | Supported / Verified behavior | Unit tested | Azure audit observed retention | Unit tested behavior | FULL replaces applicable source events but does not prune unrelated target-only events. |
| Function/trigger Error 1419 policy | Environment Dependent | Local audit required approved policy | Azure target privileges/policy apply | Target policy applies | If `log_bin=ON` and `log_bin_trust_function_creators=OFF`, MySQL may reject creation. DMS does not change the global setting. |

## 6. Comments / Metadata

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Table and column comments | Supported / Verified | Historical local audit; 0 in report `1584…` | Not exercised in this run | 17 comment records | Other comment/metadata locations are not established by this coverage. |

## 7. Users / Grants / Security Principals

| Object / capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| User/account identity | Supported / Partial | 3 principals in report `1584…`; historical audit | 3 principals | 12 principals | Locked accounts are filtered; target authentication passwords/secrets are not migrated. |
| Table/database/column grants | Supported / Environment Dependent | Historical audit; report `1584…` has 0 grants | 18 grants | 1 grant | Migration account must see source grant metadata; target account must have permission and grantee prerequisites may apply. |
| Routine `EXECUTE` privileges | Partial / Environment Dependent | Not exercised in report `1584…` | Not exercised in this run | Not separately established by count | Discovery depends on `INFORMATION_SCHEMA.ROUTINE_PRIVILEGES` visibility. |
| Global direct privileges | Partial / Environment Dependent | User-selection dependent | User-selection dependent | User-selection dependent | Limited to explicitly selected accounts and supported privilege subset. No password/role-derived privilege migration claim. |

## 8. Validation / Reporting / Operational Controls

| Capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Count validation and reports | Supported / Verified | Count validation in report `1584…` | Run report; 0 failed | Run report; 0 failed | Reports include run status, object counts, row counts, and validation results. |
| Per-object error isolation | Supported / Unit tested | Partial-success path covered by MySQL unit tests | Run had 0 failed; isolation not fault-injected | Run had 0 failed; isolation not fault-injected | Default `migration.stop_on_error: false`; a failed object can be recorded while independent work continues. |
| `stop_on_error: true` | Supported / Unit tested | E2E fault behavior not established | Not exercised | Not exercised | Fail-fast policy is configurable. Unit-tested behavior is not direction-specific E2E evidence. |
| Partial-success reporting | Supported / Unit tested | Partial result path tested | No partial result in cited run | No partial result in cited run | Actual direction runs completed successfully; they do not prove fault-path behavior. |
| Source connector cleanup | Supported / Implemented and tested | Local audit records lifecycle fix | Shared MySQL code path | Shared MySQL code path | Autocommit and rollback/close cleanup are implemented; this is not a claim of exhaustive soak testing. |
| MySQL CDC / continuous mode | Implemented / Not E2E tested | Not E2E tested | Not E2E tested | Not E2E tested | FULL migration success does not verify CDC, continuous mode, or incremental INSERT/UPDATE/DELETE replication. |

## 9. Cross-Engine Type Safety

| Capability | Support | Local → Local | Local → Cloud | Cloud → Local | Evidence / Notes |
|---|---|---|---|---|---|
| Same-engine MySQL type preservation | Supported / Verified | MySQL→MySQL report and datatype tests | MySQL→MySQL run | MySQL→MySQL run | Native MySQL types are retained where the mapping path permits. |
| Explicit cross-engine type mappings | Supported / Partial | Not applicable | Not applicable | Not applicable | Cross-engine path uses configured/implemented target mappings; each required type must be mapped. |
| Unmapped/unknown cross-engine type | Safety guard / Unit tested | Not applicable | Not applicable | Not applicable | Raises `UnmappedTypeError`; prevents unsafe source-native type passthrough. Does not imply every type has a mapping. |

## 10. Out of Scope / Native MySQL Limitations

| Feature | Status | Notes |
|---|---|---|
| Passwords/authentication secrets | Out of Scope | Accounts may be migrated within supported scope; authentication credentials are not copied. |
| PostgreSQL materialized views, RLS policies, extensions, domains/custom types, standalone sequences | N/A for native MySQL | No direct native MySQL equivalent with the same semantics; these are cross-engine capability differences. MySQL `AUTO_INCREMENT` is table-bound. |
| Exhaustive MySQL production schema coverage | Not Exercised | Audits cover selected datasets, versions, object combinations, and workloads only. |

## Evidence Summary

### Local → Local

- Checked-in report `1584f5a5ebc04e8a8d9e288ee150b23c`: MySQL → MySQL, SUCCESS, 5 tables, 5,710/5,710 rows, 0 failed. It migrated 73 columns and 5 primary keys; most other object categories had source count 0 in this dataset.
- The local audit/migration-flow docs also record historical broad evidence: 11 tables, 44/44 rows, 0 failures. The corresponding report artifact is not present in the current reports directory; treat the object-level result as documented historical evidence, not as the current report's inventory.

### Local → Cloud

- Run `9f15eae2f1a84933a7ffe9746b828932`: FULL, 6 tables, 6,340 source/migrated rows, 0 failed, 100%, about 355.7 seconds; 66 columns, 6 primary keys, 18 grants, and 3 security principals.

### Cloud → Local

- Run `fb374e4480d84894b22d5917807b507f`: FULL, 13 tables, 45 source/migrated rows, 0 failed, 100%, about 15.7 seconds. The report records 65 columns, 13 primary keys, 9 `AUTO_INCREMENT` columns, 13 indexes, 3 unique constraints, 3 foreign keys, 6 CHECK constraints, 2 generated columns, 13 defaults, 14 partitions, 17 comments, 1 grant, 12 security principals, 2 views, 2 functions, 2 procedures, 2 triggers, and 2 events.

Evidence applies only to the stated direction, report, and source dataset. Zero source-object count means **Not Exercised**, not unsupported. Environment blocked is not implementation failure. A successful FULL run is not proof of every object feature, event runtime, or CDC behavior.

## References

- [MySQL Local Audit](MYSQL_LOCAL_AUDIT.md)
- [MySQL Local → Azure Audit](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
- [MySQL Test Guide](MYSQL_TEST_GUIDE.md)
- [MySQL Limitations](MYSQL_LIMITATIONS.md)
- [MySQL Migration Flow](MYSQL_MIGRATION_FLOW.md)
- [MySQL E2E Runbook](MYSQL_E2E_RUNBOOK.md)
